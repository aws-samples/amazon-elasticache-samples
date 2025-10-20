import redis
import json
import time
import os

def handler(event, context):
    try:
        # Wait a bit for cluster readiness
        time.sleep(10)
        
        valkey_host = os.environ['VALKEY_HOST']
        r = redis.Redis(host=valkey_host, port=6379, decode_responses=True)
        r.ping()
        
        # Create VSS index
        try:
            r.execute_command('FT.INFO', 'idx:pqa_vss')
            return {'statusCode': 200, 'body': 'Index already exists'}
        except:
            r.execute_command(
                'FT.CREATE', 'idx:pqa_vss',
                'ON', 'HASH',
                'PREFIX', '1', 'pqa:',
                'SCHEMA',
                'question_vector', 'VECTOR', 'HNSW', '6',
                'TYPE', 'FLOAT32',
                'DIM', '1024',
                'DISTANCE_METRIC', 'COSINE'
            )
            return {'statusCode': 200, 'body': 'Valkey VSS index created'}
    except Exception as e:
        return {'statusCode': 500, 'body': f'Error: {str(e)}'}
