#!/bin/bash

# Load Test Data into Valkey - Various Data Types
# This script creates approximately 100 keys with different Valkey data types
# Usage: ./load_valkey_test_data.sh

set -e

# Configuration
VALKEY_HOST="your-valkey-cluster.com"
VALKEY_PORT="6379"
USE_TLS="--tls"  # Remove this if not using TLS

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_section() {
    echo -e "${YELLOW}=== $1 ===${NC}"
}

# Function to execute valkey-cli commands with error handling
execute_valkey_cmd() {
    local cmd="$1"
    local result
    result=$(valkey-cli -h "$VALKEY_HOST" -p "$VALKEY_PORT" $USE_TLS $cmd 2>&1)
    if [[ $result == *"error"* ]] || [[ $result == *"ERR"* ]]; then
        echo "⚠️  Command failed: $cmd"
        echo "   Error: $result"
        return 1
    fi
    return 0
}

# Function to check if a command is supported
check_command_support() {
    local cmd="$1"
    local test_result
    test_result=$(valkey-cli -h "$VALKEY_HOST" -p "$VALKEY_PORT" $USE_TLS COMMAND INFO "$cmd" 2>&1)
    if [[ $test_result == *"error"* ]] || [[ $test_result == *"ERR"* ]] || [[ $test_result == "(nil)" ]]; then
        return 1
    fi
    return 0
}

print_status "🚀 Starting Valkey test data loading..."
print_status "Target: $VALKEY_HOST:$VALKEY_PORT"
echo ""

# 1. STRING Data Type (20 keys)
print_section "Loading STRING Keys (20 keys)"
for i in {1..20}; do
    execute_valkey_cmd "SET user:$i:name \"User_$i\""
    execute_valkey_cmd "SET user:$i:email \"user$i@example.com\""
    if [ $((i % 5)) -eq 0 ]; then
        print_status "Loaded $i string keys..."
    fi
done
print_success "✅ Loaded 20 STRING keys"
echo ""

# 2. LIST Data Type (10 keys)
print_section "Loading LIST Keys (10 keys)"
for i in {1..10}; do
    execute_valkey_cmd "DEL user:$i:sessions"  # Clear if exists
    execute_valkey_cmd "RPUSH user:$i:sessions \"session_${i}_1\" \"session_${i}_2\" \"session_${i}_3\""
    if [ $((i % 3)) -eq 0 ]; then
        print_status "Loaded $i list keys..."
    fi
done
print_success "✅ Loaded 10 LIST keys"
echo ""

# 3. HASH Data Type (15 keys)
print_section "Loading HASH Keys (15 keys)"
for i in {1..15}; do
    execute_valkey_cmd "HSET user:$i:profile name \"User_$i\" age $((20 + i)) city \"City_$i\" active true"
    if [ $((i % 5)) -eq 0 ]; then
        print_status "Loaded $i hash keys..."
    fi
done
print_success "✅ Loaded 15 HASH keys"
echo ""

# 4. SET Data Type (10 keys)
print_section "Loading SET Keys (10 keys)"
for i in {1..10}; do
    execute_valkey_cmd "DEL user:$i:tags"  # Clear if exists
    execute_valkey_cmd "SADD user:$i:tags \"tag_$i\" \"category_$((i % 3))\" \"type_$((i % 2))\" \"status_active\""
    if [ $((i % 3)) -eq 0 ]; then
        print_status "Loaded $i set keys..."
    fi
done
print_success "✅ Loaded 10 SET keys"
echo ""

# 5. SORTED SET Data Type (10 keys)
print_section "Loading SORTED SET Keys (10 keys)"
for i in {1..10}; do
    execute_valkey_cmd "DEL leaderboard:game_$i"  # Clear if exists
    execute_valkey_cmd "ZADD leaderboard:game_$i $((1000 + i * 50)) \"player_1\" $((900 + i * 30)) \"player_2\" $((800 + i * 20)) \"player_3\""
    if [ $((i % 3)) -eq 0 ]; then
        print_status "Loaded $i sorted set keys..."
    fi
done
print_success "✅ Loaded 10 SORTED SET keys"
echo ""

# 6. GEOSPATIAL Data Type (10 keys)
print_section "Loading GEOSPATIAL Keys (10 keys)"
if check_command_support "GEOADD"; then
    locations=(
        "locations:stores -122.4194 37.7749 san_francisco_store"
        "locations:stores -74.0060 40.7128 new_york_store"
        "locations:stores -87.6298 41.8781 chicago_store"
        "locations:stores -118.2437 34.0522 los_angeles_store"
        "locations:stores -71.0589 42.3601 boston_store"
        "locations:offices -122.3321 47.6062 seattle_office"
        "locations:offices -97.7431 30.2672 austin_office"
        "locations:offices -104.9903 39.7392 denver_office"
        "locations:offices -80.1918 25.7617 miami_office"
        "locations:offices -121.4944 38.5816 sacramento_office"
    )

    geo_count=0
    for i in {0..9}; do
        if execute_valkey_cmd "GEOADD ${locations[$i]}"; then
            ((geo_count++))
        fi
        if [ $((i % 3)) -eq 0 ]; then
            print_status "Loaded $((i + 1)) geospatial keys..."
        fi
    done
    print_success "✅ Loaded $geo_count GEOSPATIAL keys"
else
    print_status "⚠️  GEOSPATIAL commands not supported - creating as STRING instead"
    for i in {1..10}; do
        execute_valkey_cmd "SET location:store_$i \"lat:$((37 + i)),lon:$((122 + i))\""
    done
    print_success "✅ Loaded 10 location STRING keys (GEOSPATIAL alternative)"
fi
echo ""

# 7. HYPERLOGLOG Data Type (5 keys)
print_section "Loading HYPERLOGLOG Keys (5 keys)"
if check_command_support "PFADD"; then
    hll_count=0
    for i in {1..5}; do
        execute_valkey_cmd "DEL analytics:unique_visitors:day_$i"  # Clear if exists
        # Add multiple items to simulate unique visitors
        if execute_valkey_cmd "PFADD analytics:unique_visitors:day_$i user_${i}_1 user_${i}_2 user_${i}_3 visitor_${i}_1 visitor_${i}_2"; then
            ((hll_count++))
        fi
        if [ $((i % 2)) -eq 0 ]; then
            print_status "Loaded $i hyperloglog keys..."
        fi
    done
    print_success "✅ Loaded $hll_count HYPERLOGLOG keys"
else
    print_status "⚠️  HYPERLOGLOG commands not supported - creating as SET instead"
    for i in {1..5}; do
        execute_valkey_cmd "DEL analytics:unique_visitors:day_$i"
        execute_valkey_cmd "SADD analytics:unique_visitors:day_$i user_${i}_1 user_${i}_2 user_${i}_3 visitor_${i}_1 visitor_${i}_2"
    done
    print_success "✅ Loaded 5 unique visitor SET keys (HYPERLOGLOG alternative)"
fi
echo ""

# 8. JSON Data Type (20 keys)
print_section "Loading JSON Keys (20 keys)"
if check_command_support "JSON.SET"; then
    json_count=0
    for i in {1..20}; do
        json_data="{\"user_id\": $i, \"name\": \"User_$i\", \"profile\": {\"age\": $((20 + i)), \"city\": \"City_$i\", \"preferences\": [\"email\", \"sms\"]}, \"metadata\": {\"created\": \"2024-01-$((i % 28 + 1))T10:00:00Z\", \"active\": true}}"
        if execute_valkey_cmd "JSON.SET user:$i:document \$ '$json_data'"; then
            ((json_count++))
        fi
        if [ $((i % 5)) -eq 0 ]; then
            print_status "Loaded $i JSON keys..."
        fi
    done
    print_success "✅ Loaded $json_count JSON keys"
else
    print_status "⚠️  JSON commands not supported - creating as STRING instead"
    for i in {1..20}; do
        json_data="{\"user_id\": $i, \"name\": \"User_$i\", \"profile\": {\"age\": $((20 + i)), \"city\": \"City_$i\", \"preferences\": [\"email\", \"sms\"]}, \"metadata\": {\"created\": \"2024-01-$((i % 28 + 1))T10:00:00Z\", \"active\": true}}"
        execute_valkey_cmd "SET user:$i:document '$json_data'"
    done
    print_success "✅ Loaded 20 JSON STRING keys (JSON alternative)"
fi
echo ""

# Summary
print_section "DATA LOADING SUMMARY"
print_success "🎉 Successfully loaded test data into Valkey!"
echo ""
echo "Data Type Distribution:"
echo "  • STRING:      20 keys (user names and emails)"
echo "  • LIST:        10 keys (user sessions)"
echo "  • HASH:        15 keys (user profiles)"
echo "  • SET:         10 keys (user tags)"
echo "  • SORTED SET:  10 keys (game leaderboards)"
echo "  • GEOSPATIAL:  Variable (depends on module availability)"
echo "  • HYPERLOGLOG: Variable (depends on module availability)"
echo "  • JSON:        Variable (depends on module availability)"
echo ""
echo "Note: Some advanced data types may fall back to basic types if modules aren't available"
echo ""
echo "Check actual key count with: valkey-cli -h $VALKEY_HOST -p $VALKEY_PORT $USE_TLS DBSIZE"
echo ""

# Verification commands
print_section "VERIFICATION COMMANDS"
echo "You can verify the data using these commands:"
echo ""
echo "# Check total key count"
echo "valkey-cli -h $VALKEY_HOST -p $VALKEY_PORT $USE_TLS DBSIZE"
echo ""
echo "# Sample data access commands:"
echo "valkey-cli -h $VALKEY_HOST -p $VALKEY_PORT $USE_TLS GET user:1:name"
echo "valkey-cli -h $VALKEY_HOST -p $VALKEY_PORT $USE_TLS LRANGE user:1:sessions 0 -1"
echo "valkey-cli -h $VALKEY_HOST -p $VALKEY_PORT $USE_TLS HGETALL user:1:profile"
echo "valkey-cli -h $VALKEY_HOST -p $VALKEY_PORT $USE_TLS SMEMBERS user:1:tags"
echo "valkey-cli -h $VALKEY_HOST -p $VALKEY_PORT $USE_TLS ZRANGE leaderboard:game_1 0 -1 WITHSCORES"
echo "valkey-cli -h $VALKEY_HOST -p $VALKEY_PORT $USE_TLS GEOPOS locations:stores san_francisco_store"
echo "valkey-cli -h $VALKEY_HOST -p $VALKEY_PORT $USE_TLS PFCOUNT analytics:unique_visitors:day_1"
echo "valkey-cli -h $VALKEY_HOST -p $VALKEY_PORT $USE_TLS JSON.GET user:1:document"
echo ""

print_success "✅ Test data loading completed successfully!"
