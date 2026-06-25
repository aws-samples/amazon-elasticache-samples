#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

cleanup() {
    echo -e "\n${YELLOW}Shutting down...${NC}"
    [ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" 2>/dev/null || true
    [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
    echo -e "${GREEN}Done.${NC}"
}
trap cleanup EXIT INT TERM

# --- Frontend deps ---
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    echo -e "${YELLOW}Installing frontend dependencies...${NC}"
    npm install --prefix "$FRONTEND_DIR"
fi

# --- Start backend ---
echo -e "${YELLOW}Starting Spring Boot backend on :8080...${NC}"
"$BACKEND_DIR/gradlew" -p "$BACKEND_DIR" bootRun > /tmp/sample-app-backend.log 2>&1 &
BACKEND_PID=$!

# Wait for backend to be ready
echo -n "Waiting for backend"
for i in $(seq 1 30); do
    if curl -sf http://localhost:8080/api/query/info > /dev/null 2>&1; then
        echo -e " ${GREEN}ready${NC}"
        break
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo -e "\n${RED}Backend failed to start. Logs:${NC}"
        cat /tmp/sample-app-backend.log
        exit 1
    fi
    echo -n "."
    sleep 2
done

# --- Start frontend ---
echo -e "${YELLOW}Starting React frontend on :5173...${NC}"
npm run dev --prefix "$FRONTEND_DIR" > /tmp/sample-app-frontend.log 2>&1 &
FRONTEND_PID=$!

sleep 2
if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    echo -e "${RED}Frontend failed to start. Logs:${NC}"
    cat /tmp/sample-app-frontend.log
    exit 1
fi

echo -e "${GREEN}"
echo "========================================"
echo "  App running at http://localhost:5173  "
echo "  Backend API  at http://localhost:8080 "
echo "  Press Ctrl+C to stop both             "
echo "========================================"
echo -e "${NC}"

# Keep script alive
wait "$BACKEND_PID" "$FRONTEND_PID"
