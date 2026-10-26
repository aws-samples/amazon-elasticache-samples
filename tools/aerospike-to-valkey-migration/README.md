# Aerospike to ElastiCache for Valkey Migration System

A comprehensive, production-ready migration system for migrating data from Aerospike to Amazon ElastiCache for Valkey with intelligent data mapping, parallel processing, and enterprise-grade error recovery.

---

## 🎯 Key Features

- **Complete data type coverage** - All Aerospike data types mapped to optimal ElastiCache for Valkey structures
- **High-performance parallel processing** - 4-8x speed improvement with automatic worker optimization
- **Intelligent data mapping** - Automatic GeoJSON to geospatial and large list to HyperLogLog conversion
- **Enterprise security** - Encryption at rest support and TLS transmission
- **Production-ready error recovery** - Automatic partition retry and comprehensive logging
- **Comprehensive testing framework** - Unit tests for all data types and large-scale validation

---

## 🚀 Quick Start

### 1. Test Individual Data Types
```bash
# Test specific data types
./test_datatype_migration.sh string 10
./test_datatype_migration.sh geojson 5
./test_datatype_migration.sh hll 3

# Test all data types
./test_datatype_migration.sh all

# Get help
./test_datatype_migration.sh -h
```

### 2. Run High-Performance Migration
```bash
# Automatic worker detection (recommended)
./smart_parallel_migration.sh

# Manual worker specification
./smart_parallel_migration.sh 8    # Use 8 workers
./smart_parallel_migration.sh 16   # Use 16 workers
```

### 3. Configure Migration Settings
Edit `mapping_config.yaml`:
```yaml
connection_settings:
  aerospike:
    host: "127.0.0.1"
    port: 3000
  valkey:
    host: "your-elasticache-cluster.amazonaws.com"
    port: 6379
    tls: true

global_settings:
  default_strategy: "hybrid"
  parallel_workers: 8
```

---

## 📊 Migration Strategies

### Separate Keys Strategy
Each Aerospike bin becomes an individual ElastiCache for Valkey key:
```
Aerospike: {user_id: 123, name: "John", email: "john@example.com"}
↓
ElastiCache for Valkey:
- "users:123:user_id" → "123"
- "users:123:name" → "John"  
- "users:123:email" → "john@example.com"
```

### JSON Document Strategy
Entire record becomes a single JSON document:
```
Aerospike: {user_id: 123, name: "John", profile: {...}}
↓
ElastiCache for Valkey:
- "users:123" → {"user_id": 123, "name": "John", "profile": {...}}
```

### Hybrid Strategy
Simple fields as separate keys, complex data as JSON:
```
Aerospike: {user_id: 123, name: "John", profile: {...}}
↓
ElastiCache for Valkey:
- "users:123:user_id" → "123"
- "users:123:name" → "John"
- "users:123:complex" → {"profile": {...}}
```

---

## 🧪 Testing and Validation

### Data Type Testing
```bash
# Available data types for testing
string      - Simple string values
integer     - Integer values
float       - Float/double values
binary      - Binary data with base64 encoding
list        - Simple and complex lists
map         - Simple and nested maps
geojson     - GeoJSON Point data → geospatial indexes
hll         - Large lists → HyperLogLog optimization
mixed       - Multiple data types in one record
```

### Large-Scale Testing
```bash
# Generate 1M test records
./load_million_records_in_aerospike.sh

# Load test data into ElastiCache for Valkey
./load_valkey_test_data.sh
```

### Data Validation
```bash
# Inspect migrated data
python dump_valkey_data.py \
  --valkey-host your-cluster.amazonaws.com \
  --tls
```

---

## 🔄 Error Recovery

### Automatic Recovery
When migrations fail, the system automatically generates recovery scripts:
```bash
# Auto-generated after migration failures
./retry_failed_partitions.sh
```

### Manual Recovery
```bash
# Retry specific partition ranges
./retry_partition.sh 1024 2047

# Show available options
./retry_partition.sh
```

---

## ⚡ Performance

### Parallel Processing Benefits
| Workers | 100K Records | 1M Records | 10M Records |
|---------|--------------|------------|-------------|
| 1 (single) | ~10-30 min | ~2-5 hours | ~20-50 hours |
| 4 workers | ~3-8 min | ~30-75 min | ~5-12 hours |
| 8 workers | ~2-5 min | ~20-45 min | ~3-8 hours |

**Speedup**: 4-8x faster with optimal worker count

### Data Type Optimizations
- **GeoJSON Points** → Native geospatial indexes (GEOADD)
- **Large lists (50+ items)** → HyperLogLog for memory efficiency (PFADD)
- **Binary data** → Base64 encoded JSON
- **Simple maps** → Native hashes (HSET)
- **Complex structures** → JSON documents (JSON.SET)

---

## 🔧 Core Scripts

### Migration Scripts
- **`smart_parallel_migration.sh`** - Main parallel migration orchestrator
- **`aerospike_to_valkey_improved.py`** - Core migration engine
- **`mapping_config.yaml`** - Configuration management
- **`mapping_engine.py`** - Configurable mapping system
- **`config_security.py`** - Verified loader for `mapping_config.yaml` (trust checks, integrity pin, schema validation)

### Testing and Validation
- **`test_datatype_migration.sh`** - Comprehensive unit testing framework
- **`load_million_records_in_aerospike.sh`** - Large-scale data generation
- **`load_valkey_test_data.sh`** - ElastiCache for Valkey test data loading
- **`dump_valkey_data.py`** - Data inspection and validation

### Error Recovery
- **`retry_partition.sh`** - Manual partition recovery
- **`retry_failed_partitions.sh`** - Auto-generated recovery script

### Utilities
- **`aerospike_aql.sh`** - Aerospike query utility
- **`valkey-cli.sh`** - ElastiCache for Valkey CLI access

---

## 🔐 Security Features

### Transport encryption — both legs, fail-closed

Every record crosses two connections, and **both** must be encrypted independently:

| Leg | Flags | Default port |
|-----|-------|--------------|
| Aerospike (source) | `--aerospike-tls --aerospike-tls-ca-cert <ca> --aerospike-tls-name <cn>` | 4333 with TLS, 3000 without |
| Valkey (destination) | `--tls --tls-ca-cert <ca>` | 6379 |

`--aerospike-tls` requires both `--aerospike-tls-name` (the TLS name configured on the
Aerospike nodes) and `--aerospike-tls-ca-cert`; the migration refuses to start without them
rather than falling back to an unverified connection. `--aerospike-port` is inferred from the
TLS setting unless you pass it explicitly.

**If either leg would be plaintext, the migration refuses to run.** To proceed anyway you must
pass `--allow-plaintext`, which logs an audit record naming each unencrypted leg. Use it only
for throwaway local test data. `--require-tls` is stricter still and overrides
`--allow-plaintext`. The same rule applies to `dump_valkey_data.py`, which reads back every
migrated record.

Encryption at rest on the Aerospike side needs no code changes — it is transparent to the client.

### Trusted configuration

`mapping_config.yaml` controls the destination host, the TLS settings and the Valkey key format,
so anyone who can write it can redirect or corrupt the migration. It is loaded through
`config_security.py`, never read directly. Before parsing, the loader:

- rejects symlinks and non-regular files
- rejects group- or world-writable configs, and group- or world-writable parent directories
- requires the file to be owned by you or by root
- verifies a pinned SHA-256, if one is set
- validates the schema against allowlists: top-level keys, Valkey commands, Valkey types,
  fallback strategies, `{placeholder}` names in key formats, hostnames, ports and field patterns

A `key_format` must contain `{primary_key}`; a `multi_key_format` must also contain `{bin_name}`.
Attribute-access format strings such as `{namespace.__class__}` and catastrophic regexes such as
`(a+)+$` are rejected. Every check fails closed — the migration exits rather than continuing on a
config it cannot vouch for.

```bash
chmod 600 mapping_config.yaml                       # required, not advisory

python3 config_security.py --pin mapping_config.yaml    # write mapping_config.yaml.sha256
python3 config_security.py --check mapping_config.yaml  # verify trust + schema, exit non-zero on failure
python3 config_security.py --fingerprint mapping_config.yaml
```

Pin the digest after every intentional edit, then re-pin. The workers re-verify the pin
independently just before connecting, so a config swapped mid-run is caught rather than used.

| Environment variable | Effect |
|----------------------|--------|
| `MAPPING_CONFIG_SHA256` | Expected digest; takes precedence over the `.sha256` sidecar |
| `MAPPING_CONFIG_REQUIRE_PIN=1` | Refuse to run against an unpinned config instead of warning |
| `MAPPING_CONFIG_ALLOW_UNSAFE_PERMS=1` | Downgrade permission failures to warnings (escape hatch; avoid) |

`--mapping-config-sha256` passes the digest explicitly on the command line and outranks both.

### Configuration

TLS and plaintext policy live in `mapping_config.yaml`, not in the shell scripts — the scripts
read them through the verified loader and pass the matching flags to each worker:

```yaml
connection_settings:
  aerospike:
    host: "127.0.0.1"
    port: 3000
    tls: false            # true => port 4333 unless overridden
    tls_ca_cert: ""       # required when tls: true
    tls_name: ""          # required when tls: true
  valkey:
    host: "my-cache.example.com"
    port: 6379
    tls: true
    tls_ca_cert: "/path/to/ca.pem"

transport_security:
  allow_plaintext: false  # true only for throwaway test data
```

Logging knobs remain in `smart_parallel_migration.sh`:

```bash
SECURE_LOGS=true       # Restrict log permissions
LOG_RETENTION_DAYS=7   # Automatic cleanup
```

### Audit trail

Refusals and security-relevant decisions are emitted as single-line JSON carrying `run_id`,
`operator` and `host` — including `migration_refused` (with the reason) and
`plaintext_transport_accepted` (with the affected legs). Collect these alongside the worker logs.

---

## 📈 Data Type Coverage

### Aerospike → ElastiCache for Valkey Mapping
| Aerospike Type | ElastiCache for Valkey Type | Command | Use Case |
|----------------|----------------------------|---------|----------|
| Integer | STRING | SET | Numeric values |
| String | STRING | SET | Text data |
| Float | STRING | SET | Decimal values |
| Binary | JSON (base64) | JSON.SET | Images, documents |
| List (simple) | LIST | RPUSH | Ordered collections |
| List (large) | HYPERLOGLOG | PFADD | Unique counting |
| Map (simple) | HASH | HSET | Key-value pairs |
| Map (complex) | JSON | JSON.SET | Nested structures |
| GeoJSON Point | GEOSPATIAL | GEOADD | Location data |

---

## 🎯 Migration Workflow

### 1. Pre-Migration Testing
```bash
# Test data type conversions
./test_datatype_migration.sh all

# Generate large test dataset
./load_million_records_in_aerospike.sh
```

### 2. Configuration
```bash
# Configure connection settings
vim mapping_config.yaml
chmod 600 mapping_config.yaml

# Verify trust checks and schema, then pin the digest
python3 config_security.py --check mapping_config.yaml
python3 config_security.py --pin mapping_config.yaml
```

### 3. Migration Execution
```bash
# Run parallel migration
./smart_parallel_migration.sh

# Monitor progress in real-time
tail -f migration_logs/worker_*.log
```

### 4. Validation
```bash
# Inspect migrated data
python dump_valkey_data.py --valkey-host your-cluster.amazonaws.com --tls

# Verify data integrity
./test_datatype_migration.sh mixed 100
```

### 5. Error Recovery (if needed)
```bash
# Retry failed partitions
./retry_failed_partitions.sh

# Manual partition retry
./retry_partition.sh 1024 2047
```

---

## 🏗️ AWS ElastiCache for Valkey Benefits

### Managed Service Features
- **Auto-scaling** - Automatic capacity adjustment based on demand
- **Serverless options** - On-demand scaling without capacity planning
- **Multi-AZ deployment** - High availability with automatic failover
- **Automated backups** - Point-in-time recovery capabilities
- **Performance monitoring** - CloudWatch integration and insights

### Cost Optimization
- **Pay-as-you-scale pricing** - No upfront licensing costs
- **Reduced operational overhead** - Eliminates database administration
- **AWS ecosystem integration** - Native integration with VPC, IAM, CloudFormation

---

## 📚 Documentation

- **[Configuration Examples](mapping_config.yaml)** - Production-ready configurations
- **[Testing Framework](test_datatype_migration.sh)** - Unit testing for all data types

---

## 🎉 Getting Started

1. **Clone and configure**:
   ```bash
   git clone <repository>
   cd aerospike-to-valkey-migration
   vim mapping_config.yaml  # Update connection settings
   ```

2. **Test your setup**:
   ```bash
   ./test_datatype_migration.sh string 5
   ```

3. **Run migration**:
   ```bash
   ./smart_parallel_migration.sh
   ```

4. **Validate results**:
   ```bash
   python dump_valkey_data.py --valkey-host your-cluster.amazonaws.com --tls
   ```

**Your data migration to ElastiCache for Valkey is now complete with enterprise-grade reliability and performance! 🚀**
