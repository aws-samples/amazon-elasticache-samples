#!/bin/bash

# Stop all local services for Semantic Cache Demo

GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
NC=$'\033[0m'

echo -e "${YELLOW}Stopping local services...${NC}"

# Stop processes by PID files
for pidfile in /tmp/agentcore.pid /tmp/ramp-simulator.pid /tmp/cache-management.pid /tmp/sam-api.pid; do
  if [ -f "$pidfile" ]; then
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
      echo -e "${GREEN}✓${NC} Stopped process $pid"
    fi
    rm -f "$pidfile"
  fi
done

# Kill any remaining processes on our ports
for port in 8080 8081 8082 3000; do
  pid=$(lsof -ti :$port 2>/dev/null)
  if [ -n "$pid" ]; then
    kill $pid 2>/dev/null
    echo -e "${GREEN}✓${NC} Killed process on port $port"
  fi
done

echo -e "${GREEN}All services stopped${NC}"
