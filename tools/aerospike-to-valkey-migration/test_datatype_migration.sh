#!/bin/bash

# Unit Testing Script for Aerospike to Valkey Data Type Migrations
# Usage: ./test_datatype_migration.sh <datatype> [record_count]
# Example: ./test_datatype_migration.sh geojson 5

set -e

# Configuration
AEROSPIKE_HOST="127.0.0.1"
AEROSPIKE_PORT="3000"
NAMESPACE="test"
MAPPING_CONFIG="${MAPPING_CONFIG:-mapping_config.yaml}"
RECORD_COUNT=${2:-10}  # Default to 10 records if not specified

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo -e "${CYAN}$1${NC}"
}

print_subheader() {
    echo -e "${MAGENTA}$1${NC}"
}

# Function to show detailed help
show_help() {
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║                    Aerospike to Valkey Migration Tester                      ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${GREEN}USAGE:${NC}"
    echo "  $0 <datatype> [record_count]"
    echo "  $0 -h | --help"
    echo ""
    echo -e "${GREEN}EXAMPLES:${NC}"
    echo "  $0 geojson 5        # Test 5 GeoJSON Point records"
    echo "  $0 hll 3            # Test 3 HyperLogLog candidate records"
    echo "  $0 mixed 10         # Test 10 mixed-type records"
    echo "  $0 all              # Run comprehensive test of all data types"
    echo ""
    echo -e "${GREEN}AVAILABLE DATA TYPES:${NC}"
    echo ""
    
    print_subheader "📝 SCALAR DATA TYPES:"
    echo "  string      - Simple string values"
    echo "              Pattern: S50 (50-character strings)"
    echo "              Valkey:  Native string keys"
    echo "              Example: 'sample_string_value_here'"
    echo ""
    echo "  integer     - Integer values"
    echo "              Pattern: I8 (8-byte integers)"
    echo "              Valkey:  Native integer keys"
    echo "              Example: 123456789"
    echo ""
    echo "  float       - Float/double values"
    echo "              Pattern: D (double precision)"
    echo "              Valkey:  Native float keys"
    echo "              Example: 3.14159"
    echo ""
    echo "  binary      - Binary data"
    echo "              Pattern: B200 (200 bytes of binary data)"
    echo "              Valkey:  Base64-encoded strings in JSON"
    echo "              Example: 'iVBORw0KGgoAAAANSUhEUgAA...'"
    echo ""
    
    print_subheader "📋 COLLECTION DATA TYPES:"
    echo "  list        - Simple lists with scalar values"
    echo "              Pattern: [5*I4] (list of 5 integers)"
    echo "              Valkey:  Redis LIST or JSON array"
    echo "              Example: [42, 84, 126, 168, 210]"
    echo ""
    echo "  map         - Simple maps/dictionaries"
    echo "              Pattern: {S10:I4,S10:S20,S10:D} (3 key-value pairs)"
    echo "              Valkey:  Redis HASH or JSON object"
    echo "              Example: {'name': 'John', 'age': 30, 'score': 95.5}"
    echo ""
    echo "  nested_list - Complex nested lists"
    echo "              Pattern: [[3*I4],[2*S10]] (list containing sublists)"
    echo "              Valkey:  JSON array with nested structures"
    echo "              Example: [[1, 2, 3], ['hello', 'world']]"
    echo ""
    echo "  nested_map  - Complex nested maps"
    echo "              Pattern: {S15:{S5:I2,S5:S10}} (map containing submaps)"
    echo "              Valkey:  JSON object with nested structures"
    echo "              Example: {'user': {'id': 123, 'name': 'Alice'}}"
    echo ""
    
    print_subheader "🌍 GEOSPATIAL DATA TYPES:"
    echo "  geojson     - GeoJSON Point data"
    echo "              Pattern: {\"type\":\"Point\",\"coordinates\":[-122.4194,37.7749]}"
    echo "              Valkey:  Geospatial sorted set (GEOADD)"
    echo "              Example: San Francisco coordinates"
    echo "              Commands: GEORADIUS, GEOPOS, GEODIST"
    echo ""
    
    print_subheader "📊 PROBABILISTIC DATA TYPES:"
    echo "  hll         - HyperLogLog candidate data"
    echo "              Pattern: [100*S5] (large list of 100 strings)"
    echo "              Valkey:  HyperLogLog for cardinality estimation"
    echo "              Example: Unique visitor counting"
    echo "              Commands: PFADD, PFCOUNT, PFMERGE"
    echo ""
    
    print_subheader "🔀 MIXED DATA TYPES:"
    echo "  mixed       - Multiple data types in one record"
    echo "              Pattern: I8,S40,D,[3*I4],{S10:I4,S10:S20}"
    echo "              Valkey:  Multiple keys with different types"
    echo "              Example: User profile with various fields"
    echo ""
    echo "  all         - Comprehensive test of all data types"
    echo "              Runs all individual tests sequentially"
    echo "              Use for complete migration validation"
    echo ""
    
    print_subheader "🔧 MIGRATION BEHAVIOR:"
    echo "  • Scalar values    → Native Valkey types (STRING, INTEGER, FLOAT)"
    echo "  • Simple lists     → Redis LIST commands (RPUSH)"
    echo "  • Simple maps      → Redis HASH commands (HSET)"
    echo "  • Complex data     → JSON documents (JSON.SET)"
    echo "  • GeoJSON Points   → Geospatial indexes (GEOADD)"
    echo "  • Large lists      → HyperLogLog (PFADD) if detected"
    echo "  • Binary data      → Base64-encoded in JSON"
    echo ""
    
    print_subheader "📈 EXPECTED RESULTS:"
    echo "  Record Count: Specified count (default: 10)"
    echo "  Key Pattern:  test.<set_name>:<record_id>[:<field_name>]"
    echo "  Data Types:   Shown in migration summary"
    echo "  Verification: Use dump_valkey_data.py to inspect results"
    echo ""
    
    print_subheader "⚙️  CONFIGURATION:"
    echo "  Aerospike:   $AEROSPIKE_HOST:$AEROSPIKE_PORT"
    echo "  Namespace:   $NAMESPACE"
    echo "  Valkey:      Read from mapping_config.yaml"
    echo "  Migration:   Single-threaded for precise testing"
    echo ""
    
    print_subheader "🚀 WORKFLOW:"
    echo "  1. Clean existing data in test set"
    echo "  2. Load new data using asbench"
    echo "  3. Verify data was loaded correctly"
    echo "  4. Run migration with detailed logging"
    echo "  5. Show migration results and statistics"
    echo ""
    
    echo -e "${GREEN}For more information about asbench patterns:${NC}"
    echo "  https://docs.aerospike.com/tools/asbench"
    echo ""
    echo -e "${GREEN}For Valkey data type documentation:${NC}"
    echo "  https://valkey.io/docs/"
}

# Function to show usage
show_usage() {
    echo "Usage: $0 <datatype> [record_count]"
    echo "       $0 -h | --help"
    echo ""
    echo "Available data types:"
    echo "  string, integer, float, binary, list, map"
    echo "  nested_list, nested_map, geojson, hll, mixed, all"
    echo ""
    echo "Examples:"
    echo "  $0 geojson 5        # Test 5 GeoJSON records"
    echo "  $0 hll 3            # Test 3 HyperLogLog records"
    echo "  $0 -h               # Show detailed help"
}

# Function to clean up existing data
cleanup_data() {
    local set_name=$1
    print_status "Cleaning up existing data in set: $set_name"
    
    # Try TRUNCATE first (newer Aerospike versions)
    if docker run -it --rm --network container:aerospike \
        aerospike/aerospike-tools \
        aql -h $AEROSPIKE_HOST -c "TRUNCATE $NAMESPACE.$set_name" 2>/dev/null; then
        print_success "✅ Successfully truncated set: $set_name"
    else
        print_warning "⚠️  TRUNCATE not supported, trying alternative cleanup..."
        
        # Alternative: Delete records individually (slower but works)
        docker run -it --rm --network container:aerospike \
            aerospike/aerospike-tools \
            aql -h $AEROSPIKE_HOST -c "DELETE FROM $NAMESPACE.$set_name" 2>/dev/null || {
            print_warning "⚠️  DELETE also not supported, data may accumulate"
            print_status "Continuing with data loading (records will be overwritten if keys match)"
        }
    fi
}

# Function to verify data was loaded
verify_data_loaded() {
    local set_name=$1
    local expected_count=$2
    
    print_status "Verifying data was loaded..."
    
    local actual_count=$(docker run -it --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asinfo -h $AEROSPIKE_HOST -v "sets/$NAMESPACE/$set_name" | \
        grep -o 'objects=[0-9]*' | cut -d'=' -f2 || echo "0")
    
    if [ "$actual_count" -eq "$expected_count" ]; then
        print_success "✅ Loaded $actual_count records as expected"
        return 0
    else
        print_error "❌ Expected $expected_count records, but found $actual_count"
        return 1
    fi
}

# Function to run migration and show results
run_migration_test() {
    local set_name=$1
    
    print_status "Running migration for set: $set_name"

    # Read connection settings through the verified loader (threat T4) instead of
    # an inline YAML read that silently defaults to localhost on any error.
    if ! CONFIG_VARS=$(python3 config_security.py --emit-shell "$MAPPING_CONFIG"); then
        print_error "Mapping configuration failed its trust checks - aborting test."
        return 1
    fi
    eval "$CONFIG_VARS"

    CMD=(python3 aerospike_to_valkey_improved.py
        --aerospike-host "$AEROSPIKE_HOST"
        --aerospike-port "$AEROSPIKE_PORT"
        --valkey-host "$CFG_VALKEY_HOST"
        --valkey-port "$CFG_VALKEY_PORT"
        --namespace "$NAMESPACE"
        --set "$set_name"
        --use-json
        --json-threshold 1)

    if [ "$CFG_VALKEY_TLS" = "true" ]; then
        CMD+=(--tls --tls-ca-cert "$CFG_VALKEY_TLS_CA_CERT")
    fi
    if [ "$CFG_AEROSPIKE_TLS" = "true" ]; then
        CMD+=(--aerospike-tls
              --aerospike-tls-ca-cert "$CFG_AEROSPIKE_TLS_CA_CERT"
              --aerospike-tls-name "$CFG_AEROSPIKE_TLS_NAME")
    fi
    # This harness drives a local throwaway Aerospike container over plaintext,
    # so it acknowledges unencrypted transport explicitly. Never do this against
    # a cluster holding real data (threat T1).
    if [ "$CFG_VALKEY_TLS" != "true" ] || [ "$CFG_AEROSPIKE_TLS" != "true" ]; then
        print_warning "Test harness: accepting unencrypted transport for local test data"
        CMD+=(--allow-plaintext)
    fi

    "${CMD[@]}" 2>&1 | tail -20

    print_status "Migration completed. Use dump_valkey_data.py to inspect results."
}

# Main test functions for each data type
test_string() {
    local set_name="test_string"
    cleanup_data $set_name
    
    print_status "Testing STRING data type with $RECORD_COUNT records"
    
    docker run --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asbench -h $AEROSPIKE_HOST -p $AEROSPIKE_PORT \
        -n $NAMESPACE -s $set_name -k $RECORD_COUNT \
        -o "S50" --start-key 1
    
    verify_data_loaded $set_name $RECORD_COUNT && run_migration_test $set_name
}

test_integer() {
    local set_name="test_integer"
    cleanup_data $set_name
    
    print_status "Testing INTEGER data type with $RECORD_COUNT records"
    
    docker run --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asbench -h $AEROSPIKE_HOST -p $AEROSPIKE_PORT \
        -n $NAMESPACE -s $set_name -k $RECORD_COUNT \
        -o "I8" --start-key 1
    
    verify_data_loaded $set_name $RECORD_COUNT && run_migration_test $set_name
}

test_float() {
    local set_name="test_float"
    cleanup_data $set_name
    
    print_status "Testing FLOAT data type with $RECORD_COUNT records"
    
    docker run --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asbench -h $AEROSPIKE_HOST -p $AEROSPIKE_PORT \
        -n $NAMESPACE -s $set_name -k $RECORD_COUNT \
        -o "D" --start-key 1
    
    verify_data_loaded $set_name $RECORD_COUNT && run_migration_test $set_name
}

test_binary() {
    local set_name="test_binary"
    cleanup_data $set_name
    
    print_status "Testing BINARY data type with $RECORD_COUNT records"
    
    docker run --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asbench -h $AEROSPIKE_HOST -p $AEROSPIKE_PORT \
        -n $NAMESPACE -s $set_name -k $RECORD_COUNT \
        -o "B200" --start-key 1
    
    verify_data_loaded $set_name $RECORD_COUNT && run_migration_test $set_name
}

test_list() {
    local set_name="test_list"
    cleanup_data $set_name
    
    print_status "Testing LIST data type with $RECORD_COUNT records"
    
    docker run --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asbench -h $AEROSPIKE_HOST -p $AEROSPIKE_PORT \
        -n $NAMESPACE -s $set_name -k $RECORD_COUNT \
        -o "[5*I4]" --start-key 1
    
    verify_data_loaded $set_name $RECORD_COUNT && run_migration_test $set_name
}

test_map() {
    local set_name="test_map"
    cleanup_data $set_name
    
    print_status "Testing MAP data type with $RECORD_COUNT records"
    
    docker run --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asbench -h $AEROSPIKE_HOST -p $AEROSPIKE_PORT \
        -n $NAMESPACE -s $set_name -k $RECORD_COUNT \
        -o "{S10:I4,S10:S20,S10:D}" --start-key 1
    
    verify_data_loaded $set_name $RECORD_COUNT && run_migration_test $set_name
}

test_nested_list() {
    local set_name="test_nested_list"
    cleanup_data $set_name
    
    print_status "Testing NESTED LIST data type with $RECORD_COUNT records"
    
    docker run --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asbench -h $AEROSPIKE_HOST -p $AEROSPIKE_PORT \
        -n $NAMESPACE -s $set_name -k $RECORD_COUNT \
        -o "[[3*I4],[2*S10]]" --start-key 1
    
    verify_data_loaded $set_name $RECORD_COUNT && run_migration_test $set_name
}

test_nested_map() {
    local set_name="test_nested_map"
    cleanup_data $set_name
    
    print_status "Testing NESTED MAP data type with $RECORD_COUNT records"
    
    docker run --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asbench -h $AEROSPIKE_HOST -p $AEROSPIKE_PORT \
        -n $NAMESPACE -s $set_name -k $RECORD_COUNT \
        -o "{S15:{S5:I2,S5:S10}}" --start-key 1
    
    verify_data_loaded $set_name $RECORD_COUNT && run_migration_test $set_name
}

test_geojson() {
    local set_name="test_geojson"
    cleanup_data $set_name
    
    print_status "Testing GEOJSON data type with $RECORD_COUNT records"
    
    docker run --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asbench -h $AEROSPIKE_HOST -p $AEROSPIKE_PORT \
        -n $NAMESPACE -s $set_name -k $RECORD_COUNT \
        -o "{\"type\":\"Point\",\"coordinates\":[-122.4194,37.7749]}" --start-key 1
    
    verify_data_loaded $set_name $RECORD_COUNT && run_migration_test $set_name
}

test_hll() {
    local set_name="test_hll"
    cleanup_data $set_name
    
    print_status "Testing HLL (HyperLogLog) data type with $RECORD_COUNT records"
    print_warning "Creating large lists (100+ items) to trigger HLL detection"
    
    docker run --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asbench -h $AEROSPIKE_HOST -p $AEROSPIKE_PORT \
        -n $NAMESPACE -s $set_name -k $RECORD_COUNT \
        -o "[100*S5]" --start-key 1
    
    verify_data_loaded $set_name $RECORD_COUNT && run_migration_test $set_name
}

test_mixed() {
    local set_name="test_mixed"
    cleanup_data $set_name
    
    print_status "Testing MIXED data types with $RECORD_COUNT records"
    
    docker run --rm --network container:aerospike \
        aerospike/aerospike-tools \
        asbench -h $AEROSPIKE_HOST -p $AEROSPIKE_PORT \
        -n $NAMESPACE -s $set_name -k $RECORD_COUNT \
        -o "I8,S40,D,[3*I4],{S10:I4,S10:S20}" --start-key 1
    
    verify_data_loaded $set_name $RECORD_COUNT && run_migration_test $set_name
}

test_all() {
    print_status "Testing ALL data types - comprehensive test"
    
    test_string
    echo ""
    test_integer  
    echo ""
    test_float
    echo ""
    test_binary
    echo ""
    test_list
    echo ""
    test_map
    echo ""
    test_nested_list
    echo ""
    test_nested_map
    echo ""
    test_geojson
    echo ""
    test_hll
    echo ""
    test_mixed
    
    print_success "🎉 All data type tests completed!"
}

# Main script logic
if [ $# -eq 0 ]; then
    show_usage
    exit 1
fi

# Handle help flags
if [ "$1" = "-h" ] || [ "$1" = "--help" ] || [ "$1" = "help" ]; then
    show_help
    exit 0
fi

DATATYPE=$1

print_header "🧪 Starting Data Type Migration Test"
echo ""
print_status "Data type: $DATATYPE"
print_status "Record count: $RECORD_COUNT"
print_status "Namespace: $NAMESPACE"
print_status "Aerospike: $AEROSPIKE_HOST:$AEROSPIKE_PORT"
echo ""

case $DATATYPE in
    string)
        test_string
        ;;
    integer)
        test_integer
        ;;
    float)
        test_float
        ;;
    binary)
        test_binary
        ;;
    list)
        test_list
        ;;
    map)
        test_map
        ;;
    nested_list)
        test_nested_list
        ;;
    nested_map)
        test_nested_map
        ;;
    geojson)
        test_geojson
        ;;
    hll)
        test_hll
        ;;
    mixed)
        test_mixed
        ;;
    all)
        test_all
        ;;
    *)
        print_error "Unknown data type: $DATATYPE"
        echo ""
        show_usage
        echo ""
        echo -e "${YELLOW}💡 Use '$0 -h' for detailed help with examples${NC}"
        exit 1
        ;;
esac

echo ""
print_success "✅ Test completed for data type: $DATATYPE"
print_status "💡 Use 'python dump_valkey_data.py --valkey-host <host> --tls --tls-ca-cert <ca> --confirm-data-dump' to inspect migrated data"
print_status "💡 Use '$0 -h' for detailed help and documentation"
