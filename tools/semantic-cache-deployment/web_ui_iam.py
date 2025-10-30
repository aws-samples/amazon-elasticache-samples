#!/usr/bin/env python3
"""
Semantic Cache Web UI with IAM Authentication
Updated to work with API Gateway IAM auth
"""

from flask import Flask, render_template_string, request, jsonify
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import requests
import json
import time
import argparse
import sys

# Parse command line arguments
parser = argparse.ArgumentParser(description='Semantic Cache Web UI')
parser.add_argument('--api-url', required=True, help='API Gateway URL')
parser.add_argument('--region', required=True, help='AWS Region')
args = parser.parse_args()

# Configuration from command line
API_GATEWAY_URL = args.api_url
AWS_REGION = args.region

app = Flask(__name__)

def call_api_with_iam(query, score_threshold=0.7):
    """Call API Gateway with IAM authentication"""
    try:
        session = boto3.Session()
        credentials = session.get_credentials()
        
        if not credentials:
            raise Exception("AWS credentials not found. Configure AWS CLI or set environment variables.")
        
        payload = json.dumps({
            "query": query,
            "score_threshold": score_threshold
        })
        
        # Create signed request
        request_obj = AWSRequest(method='POST', url=API_GATEWAY_URL, data=payload)
        request_obj.headers['Content-Type'] = 'application/json'
        
        # Sign with AWS credentials
        SigV4Auth(credentials, 'execute-api', AWS_REGION).add_auth(request_obj)
        
        # Make the call
        start_time = time.time()
        response = requests.post(
            API_GATEWAY_URL, 
            headers=dict(request_obj.headers), 
            data=request_obj.body,
            timeout=30
        )
        end_time = time.time()
        
        response_time = int((end_time - start_time) * 1000)  # Convert to milliseconds
        
        if response.status_code == 200:
            result = response.json()
            result['total_request_time_ms'] = response_time  # Keep total time separate
            return result
        else:
            return {
                'error': f'API returned status {response.status_code}: {response.text}',
                'response_time_ms': response_time
            }
            
    except Exception as e:
        return {'error': str(e)}

# HTML template
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Semantic Cache Demo</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
        .container { background: #f5f5f5; padding: 20px; border-radius: 10px; margin: 20px 0; }
        .query-box { width: 100%; padding: 10px; font-size: 16px; border: 1px solid #ddd; border-radius: 5px; }
        .search-btn { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; margin-top: 10px; }
        .search-btn:hover { background: #0056b3; }
        .result { background: white; padding: 15px; border-radius: 5px; margin: 10px 0; border-left: 4px solid #007bff; }
        .cache-hit { border-left-color: #28a745; }
        .cache-miss { border-left-color: #ffc107; }
        .error { border-left-color: #dc3545; background: #f8d7da; }
        .metrics { font-size: 12px; color: #666; margin-top: 10px; }
        .loading { color: #666; font-style: italic; }
        .powered-by { text-align: center; color: #666; font-size: 14px; margin: 10px 0; }
        .highlight { color: #ff6b35; font-weight: bold; }
        .tech-stack { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 8px 12px; border-radius: 20px; display: inline-block; font-size: 12px; margin: 0 3px; }
    </style>
</head>
<body>
    <h1>Semantic Cache Demo</h1>
    <div class="powered-by">
        Ask questions about headsets and see the power of semantic caching! <br>
        Powered by <span class="tech-stack">🔥 ElastiCache Valkey</span> & <span class="tech-stack">🧠 Bedrock Knowledge Base</span>
    </div>
    
    <div class="container">
        <form id="searchForm">
            <input type="text" id="queryInput" class="query-box" placeholder="Ask a question about headsets..." required>
            <br>
            <button type="submit" class="search-btn">Search</button>
            <label style="margin-left: 20px;">
                Score Threshold: 
                <input type="number" id="scoreThreshold" value="0.7" min="0" max="1" step="0.1" style="width: 60px;">
            </label>
        </form>
    </div>
    
    <div id="results"></div>
    
    <script>
        document.getElementById('searchForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const query = document.getElementById('queryInput').value;
            const scoreThreshold = parseFloat(document.getElementById('scoreThreshold').value);
            const resultsDiv = document.getElementById('results');
            
            // Show loading
            resultsDiv.innerHTML = '<div class="result loading">🔍 Searching...</div>';
            
            try {
                const response = await fetch('/search', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        query: query,
                        score_threshold: scoreThreshold
                    })
                });
                
                const data = await response.json();
                
                if (data.error) {
                    resultsDiv.innerHTML = `
                        <div class="result error">
                            <strong>ERROR: Error:</strong> ${data.error}
                        </div>
                    `;
                } else {
                    const isCacheHit = data.source === 'valkey_cache';
                    const resultClass = isCacheHit ? 'cache-hit' : 'cache-miss';
                    const sourceIcon = isCacheHit ? '⚡' : '🧠';
                    const sourceText = isCacheHit ? 'Valkey Cache' : 'Knowledge Base + LLM (Claude 3.7 Sonnet)';
                    
                    let resultHtml = `
                        <div class="result ${resultClass}">
                            <strong>${sourceIcon} ${sourceText}</strong>
                            <p><strong>Query:</strong> ${data.question || query}</p>
                            <p><strong>Answer:</strong> ${data.answer}</p>
                    `;
                    
                    if (data.cached_question) {
                        resultHtml += `<p><strong>Similar cached question:</strong> ${data.cached_question}</p>`;
                    }
                    
                    if (data.score !== undefined) {
                        resultHtml += `<p><strong>Similarity score:</strong> ${data.score.toFixed(4)}</p>`;
                    }
                    
                    if (data.response_time_ms) {
                        resultHtml += `
                            <div class="metrics">
                                <strong>Response time:</strong> <span style="background: ${isCacheHit ? '#d4edda' : '#fff3cd'}; padding: 2px 6px; border-radius: 3px; font-weight: bold;">${Math.round(data.response_time_ms)}ms</span> | 
                                <strong>Source:</strong> ${sourceText}`;
                        
                        if (data.total_request_time_ms) {
                            resultHtml += ` | <strong>Total:</strong> ${Math.round(data.total_request_time_ms)}ms`;
                        }
                        
                        resultHtml += `</div>`;
                    }
                    
                    resultHtml += '</div>';
                    resultsDiv.innerHTML = resultHtml;
                }
            } catch (error) {
                resultsDiv.innerHTML = `
                    <div class="result error">
                        <strong>ERROR: Network Error:</strong> ${error.message}
                        <br><small>Check if the API Gateway URL is correct and you have proper AWS credentials.</small>
                    </div>
                `;
            }
        });
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/search', methods=['POST'])
def search():
    data = request.get_json()
    query = data.get('query', '').strip()
    score_threshold = data.get('score_threshold', 0.7)
    
    if not query:
        return jsonify({'error': 'Query is required'})
    
    result = call_api_with_iam(query, score_threshold)
    return jsonify(result)

if __name__ == '__main__':
    print(f"Starting web UI...")
    print(f"API Gateway URL: {API_GATEWAY_URL}")
    print(f"Open browser to: http://localhost:5000")
    app.run(debug=False, host='127.0.0.1', port=5000)
    
 