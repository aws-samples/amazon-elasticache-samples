#!/bin/bash
# Manual test script for memory system using curl
# Run this to test both short-term and long-term memory

BASE_URL="http://localhost:8000"

echo "🧪 MANUAL MEMORY SYSTEM TEST"
echo "============================"
echo ""

# Test 1: Store long-term memory
echo "📝 TEST 1: Storing long-term memory (with userId)"
echo "   User: test-user-manual"
echo "   Session: session-1"
echo "   Message: 'I prefer Nike running shoes in size 10'"
echo ""

curl -s -X POST "$BASE_URL/invocations" \
  -H "Content-Type: application/json" \
  -d '{
    "chatId": "session-1",
    "userId": "test-user-manual",
    "prompt": "I prefer Nike running shoes in size 10",
    "cacheMode": "off"
  }' | grep -o '"delta":"[^"]*"' | sed 's/"delta":"//g' | sed 's/"//g' | tr -d '\n'

echo ""
echo ""
echo "⏳ Waiting 5 seconds for memory to be stored and indexed..."
sleep 5
echo ""

# Test 2: Retrieve long-term memory in new session
echo "🔍 TEST 2: Retrieving long-term memory (new session, same user)"
echo "   User: test-user-manual (same)"
echo "   Session: session-2 (different)"
echo "   Message: 'What brand of shoes do I like?'"
echo ""

RESPONSE=$(curl -s -X POST "$BASE_URL/invocations" \
  -H "Content-Type: application/json" \
  -d '{
    "chatId": "session-2",
    "userId": "test-user-manual",
    "prompt": "What brand of shoes do I like?",
    "cacheMode": "off"
  }' | grep -o '"delta":"[^"]*"' | sed 's/"delta":"//g' | sed 's/"//g' | tr -d '\n')

echo "$RESPONSE"
echo ""
echo ""

# Check if Nike was mentioned
if echo "$RESPONSE" | grep -qi "nike"; then
    echo "✅ SUCCESS! Agent remembered Nike preference across sessions"
    echo "   Long-term memory is working!"
else
    echo "❌ FAILED! Agent didn't mention Nike"
    echo "   Check application logs for [MEMORY] messages"
fi

echo ""
echo "============================"
echo ""
echo "To check application logs, look for:"
echo "  [MEMORY] Retrieved X memories"
echo "  [MEMORY] Injected X memories into prompt"
echo "  [MEMORY] Stored memories: short_term=X, long_term=X"
