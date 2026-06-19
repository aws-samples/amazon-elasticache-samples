#!/bin/sh
set -e

CONFIG_FILE="/opt/redisshake/shake.toml"

# If SHAKE_CONFIG_BASE64 is set, decode it and use as the config file.
if [ -n "$SHAKE_CONFIG_BASE64" ]; then
  echo "$SHAKE_CONFIG_BASE64" | base64 -d > "$CONFIG_FILE"
  echo "Using base64-decoded config from SHAKE_CONFIG_BASE64"
fi

# Always substitute placeholders — use env var value or empty string.
sed -i "s|__SRC_ADDRESS__|${SHAKE_SRC_ADDRESS:-}|g"   "$CONFIG_FILE"
sed -i "s|__SRC_PASSWORD__|${SHAKE_SRC_PASSWORD:-}|g"  "$CONFIG_FILE"
sed -i "s|__SRC_USERNAME__|${SHAKE_SRC_USERNAME:-}|g"  "$CONFIG_FILE"
sed -i "s|__SRC_TLS__|${SHAKE_SRC_TLS:-false}|g"      "$CONFIG_FILE"
sed -i "s|__SRC_CLUSTER__|${SHAKE_SRC_CLUSTER:-false}|g" "$CONFIG_FILE"
sed -i "s|__DST_ADDRESS__|${SHAKE_DST_ADDRESS:-}|g"   "$CONFIG_FILE"
sed -i "s|__DST_PASSWORD__|${SHAKE_DST_PASSWORD:-}|g"  "$CONFIG_FILE"
sed -i "s|__DST_USERNAME__|${SHAKE_DST_USERNAME:-}|g"  "$CONFIG_FILE"
sed -i "s|__DST_TLS__|${SHAKE_DST_TLS:-false}|g"      "$CONFIG_FILE"
sed -i "s|__DST_CLUSTER__|${SHAKE_DST_CLUSTER:-false}|g" "$CONFIG_FILE"

echo "--- RedisShake Config ---"
cat "$CONFIG_FILE"
echo "--- Starting RedisShake ---"

exec /app/redis-shake "$CONFIG_FILE"
