#!/bin/bash
# Reset semantic cache for demo reruns
# Run this on EC2 jump host: ./reset-cache.sh

CACHE_HOST="${CACHE_HOST:-sevoxy28zhyaiz6.xkacez.ng.0001.use2.cache.amazonaws.com}"

echo "Clearing semantic cache at $CACHE_HOST..."

# Delete vector entries
redis6-cli -h $CACHE_HOST --scan --pattern "request:vector:*" | xargs -L 100 redis6-cli -h $CACHE_HOST DEL 2>/dev/null

# Delete request-response entries
redis6-cli -h $CACHE_HOST --scan --pattern "rr:*" | xargs -L 100 redis6-cli -h $CACHE_HOST DEL 2>/dev/null

# Delete global metrics
redis6-cli -h $CACHE_HOST DEL metrics:global 2>/dev/null

# Verify
echo ""
echo "Verification:"
echo "  DB Size: $(redis6-cli -h $CACHE_HOST DBSIZE)"
echo "  Index:   $(redis6-cli -h $CACHE_HOST FT._LIST)"
echo ""
echo "Cache reset complete. Ready for demo."
