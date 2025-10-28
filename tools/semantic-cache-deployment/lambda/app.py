#!/usr/bin/env python3
import json
import boto3
import numpy as np
import redis
import time
import os

def lambda_handler(event, context):
    """Complete semantic caching logic: Valkey → Knowledge Base → Cache"""
    
    try:
        # Handle API Gateway event format
        if 'body' in event:
            body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
        else:
            body = event
            
        # Parse input
        query = body.get('query', '').strip()
        score_threshold = body.get('score_threshold', 0.7)  # Configurable threshold
        
        if not query:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'No query provided'})
            }
        
        # Initialize clients
        bedrock_agent = boto3.client('bedrock-agent-runtime', region_name=os.environ['REGION'])
        bedrock_runtime = boto3.client('bedrock-runtime', region_name=os.environ['REGION'])
        valkey_client = redis.Redis(
            host=os.environ['VALKEY_HOST'],
            port=6379,
            decode_responses=False
        )
        kb_id = os.environ['KNOWLEDGE_BASE_ID']
        max_score = float(score_threshold)  # Use configurable threshold from UI
        
        # Generate query embedding
        embedding_response = bedrock_runtime.invoke_model(
            modelId='amazon.titan-embed-text-v2:0',
            body=json.dumps({'inputText': query})
        )
        embedding_result = json.loads(embedding_response['body'].read())
        query_vector = np.array(embedding_result['embedding'], dtype=np.float32)
        
        # 1. Check Valkey cache first
        cache_start_time = time.time()
        
        search_cmd = [
            'FT.SEARCH', 'idx:pqa_vss',
            '*=>[KNN 1 @question_vector $query_vec AS score]',
            'PARAMS', '2', 'query_vec', query_vector.tobytes()
        ]
        
        cache_result = valkey_client.execute_command(*search_cmd)
        cache_end_time = time.time()
        cache_duration = (cache_end_time - cache_start_time) * 1000
        
        # Debug: Print cache search results
        print(f"Cache search result: {cache_result}")
        
        # Check if we have a good cache hit
        if cache_result and len(cache_result) > 2:
            doc_id = cache_result[1]
            fields = cache_result[2]
            
            # Parse score
            score = 0.0
            if isinstance(fields, list):
                for i in range(0, len(fields), 2):
                    if i + 1 < len(fields) and fields[i] == b'score':
                        score = float(fields[i + 1])
                        break
            
            print(f"Found cache entry with score: {score}, threshold: {max_score}")
            
            if score <= max_score:
                # Cache hit! Get the cached answer
                doc_id_str = doc_id.decode('utf-8') if isinstance(doc_id, bytes) else doc_id
                hash_data = valkey_client.hgetall(doc_id_str)
                cached_question = hash_data.get(b'question', b'').decode('utf-8')
                cached_answer = hash_data.get(b'answer', b'').decode('utf-8')
                
                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'application/json'},
                    'body': json.dumps({
                        'answer': cached_answer,
                        'question': cached_question,
                        'source': 'valkey_cache',
                        'response_time_ms': cache_duration,
                        'score': score
                    })
                }
        
        # 2. Cache miss or low score - query Knowledge Base
        
        kb_start_time = time.time()
        
        # Enhanced prompt for better synthesis
        enhanced_query = f"{query}\n\nInstructions: Use available context clues, user comments, and related information to make reasonable inferences. Provide the best possible answer based on available data. Frame responses positively - instead of 'I cannot find...', say 'Based on the available information...' and provide what you can determine."
        
        response = bedrock_agent.retrieve_and_generate(
            input={'text': enhanced_query},
            retrieveAndGenerateConfiguration={
                'type': 'KNOWLEDGE_BASE',
                'knowledgeBaseConfiguration': {
                    'knowledgeBaseId': kb_id,
                    'modelArn': f'arn:aws:bedrock:{os.environ["REGION"]}:{os.environ["AWS_ACCOUNT_ID"]}:inference-profile/{os.environ["INFERENCE_PROFILE"]}',
                    'retrievalConfiguration': {
                        'vectorSearchConfiguration': {
                            'numberOfResults': 5
                        }
                    }
                }
            }
        )
        
        kb_end_time = time.time()
        kb_duration = (kb_end_time - kb_start_time) * 1000
        
        answer = response.get('output', {}).get('text', 'No answer generated')
        
        # 3. Cache the new result in Valkey
        cache_key = f'pqa:{hash(query) % 1000000}'
        valkey_client.hset(cache_key, mapping={
            'question': query,
            'answer': answer,
            'question_vector': query_vector.tobytes()
        })
        
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'answer': answer,
                'question': query,
                'source': 'knowledge_base',
                'response_time_ms': kb_duration,
                'cache_time_ms': cache_duration,
                'score': 1.0
            })
        }
        
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': str(e)})
        }
