#!/bin/bash
# Force complete reload of frontend and backend

echo "=== Forcing complete reload ==="

# Step 1: Clean Python cache
echo "Cleaning Python cache..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true

# Step 2: Rebuild frontend
echo "Rebuilding frontend..."
cd react-ui
npm run build
cd ..

# Step 3: Instructions for restart
echo ""
echo "=== NEXT STEPS ==="
echo "1. Stop the running backend (Ctrl+C in the terminal running 'hatch run start')"
echo "2. Run: hatch run start"
echo "3. Open browser to http://localhost:5173"
echo "4. Test memory system"
echo ""
echo "When testing, check backend logs for [MEMORY] messages"
