#!/usr/bin/env python3
import argparse
import json
import base64
from redis.cluster import RedisCluster, ClusterNode
from datetime import datetime
import logging
import aerospike
import uuid
import time

# Import the mapping engine
try:
    from mapping_engine import MappingEngine, MappingResult
    MAPPING_ENGINE_AVAILABLE = True
except ImportError:
    MAPPING_ENGINE_AVAILABLE = False
    logging.warning("Mapping engine not available, using legacy mapping")

# T4: config trust checks. Imported separately from the mapping engine so the
# failure mode stays distinguishable from "PyYAML is missing".
from config_security import ConfigSecurityError

import os

# Configure logging.
# T3: Migration log files can leak sensitive record keys and values. Default to
# INFO and never emit raw data values unless the operator explicitly opts in via
# LOG_DATA_VALUES=1. Log level can be overridden with MIGRATION_LOG_LEVEL.
_log_level_name = os.environ.get('MIGRATION_LOG_LEVEL', 'INFO').upper()
_log_level = getattr(logging, _log_level_name, logging.INFO)
logging.basicConfig(
    level=_log_level,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Whether raw data values may be written to logs. Off by default to prevent
# sensitive production data from being persisted to plaintext log files.
LOG_DATA_VALUES = os.environ.get('LOG_DATA_VALUES', '0') == '1'


def redact(value):
    """Return a log-safe representation of a value.

    Unless LOG_DATA_VALUES is explicitly enabled, this hides the actual content
    and only reports the type and size so logs remain useful for debugging
    without leaking sensitive data (threat T3).
    """
    if LOG_DATA_VALUES:
        return value
    try:
        if isinstance(value, (list, dict, str, bytes, bytearray)):
            return f"<{type(value).__name__} len={len(value)} redacted>"
        return f"<{type(value).__name__} redacted>"
    except Exception:
        return "<redacted>"

# Argument parsing
parser = argparse.ArgumentParser(description="Migrate Aerospike data to Valkey with configurable data type mappings")
parser.add_argument('--aerospike-host', required=True)
parser.add_argument('--aerospike-port', type=int,
                   help="Aerospike service port. Defaults to 4333 when --aerospike-tls "
                        "is set, otherwise 3000.")
parser.add_argument('--valkey-host', required=True)
parser.add_argument('--valkey-port', type=int, default=6379)
parser.add_argument('--namespace')
parser.add_argument('--set')
parser.add_argument('--key-prefix')
parser.add_argument('--tls', action='store_true',
                   help="Enable TLS on the Valkey (destination) connection.")
parser.add_argument('--tls-ca-cert',
                   help="Path to CA certificate bundle used to verify the Valkey server certificate. "
                        "Required when --tls is set unless --allow-insecure-tls is explicitly provided.")
parser.add_argument('--allow-insecure-tls', action='store_true',
                   help="DANGEROUS: disable TLS certificate validation on the Valkey "
                        "connection (vulnerable to MITM). Only use for isolated testing, "
                        "never for production data.")
# T1: the Aerospike (source) leg carries exactly the same records as the Valkey
# leg, so it needs the same protection. Without these options the source leg was
# always plaintext, which left the migration only half encrypted.
parser.add_argument('--aerospike-tls', action='store_true',
                   help="Enable TLS on the Aerospike (source) connection.")
parser.add_argument('--aerospike-tls-ca-cert',
                   help="Path to the CA certificate bundle that signed the Aerospike "
                        "server certificates. Required with --aerospike-tls.")
parser.add_argument('--aerospike-tls-name',
                   help="TLS certificate name configured on the Aerospike nodes "
                        "(the server's tls-name). Required with --aerospike-tls so the "
                        "server certificate is actually verified.")
parser.add_argument('--aerospike-tls-certfile',
                   help="Client certificate for mutual TLS to Aerospike (optional).")
parser.add_argument('--aerospike-tls-keyfile',
                   help="Client private key for mutual TLS to Aerospike (optional).")
parser.add_argument('--require-tls', action='store_true',
                   help="Refuse to run unless TLS is enabled on both legs. Kept for "
                        "compatibility: TLS is now required by default and this flag "
                        "additionally forbids --allow-plaintext.")
parser.add_argument('--allow-plaintext', action='store_true',
                   help="DANGEROUS: permit unencrypted transport on either leg. Required "
                        "to run without TLS; without it the migration refuses to move "
                        "data in the clear (threat T1).")
parser.add_argument('--mapping-config-sha256',
                   help="Expected SHA-256 of the mapping configuration file. The migration "
                        "aborts if the file's contents do not match (threat T4).")
parser.add_argument('--dry-run', action='store_true',
                   help="Simulate the migration without writing any data to Valkey.")
parser.add_argument('--overwrite', action='store_true',
                   help="Allow overwriting keys that already exist in the destination. "
                        "By default existing keys are skipped to prevent accidental data corruption.")
parser.add_argument('--timestamp-format', default='iso', choices=['iso', 'unix', 'preserve'],
                   help="Format for timestamps: 'iso' for ISO format, 'unix' to preserve as unix timestamp, 'preserve' for original format")
parser.add_argument('--use-json', action='store_true', help="Use native JSON for complex nested structures")
parser.add_argument('--mapping-config', default='mapping_config.yaml', 
                   help="Path to mapping configuration file")
parser.add_argument('--use-mapping-engine', action='store_true',
                   help="Use the configurable mapping engine instead of legacy mapping")
parser.add_argument('--json-threshold', type=int, default=1, 
                   help="Minimum number of complex fields required to use JSON instead of standard types")
parser.add_argument('--start-partition', type=int, default=0, help='Starting partition (0-4095)')
parser.add_argument('--end-partition', type=int, default=4095, help='Ending partition (0-4095)')
args = parser.parse_args()

# Record start time with high precision
start_time = time.time()
start_datetime = datetime.now()

# Structured audit context (threat T12: accountability).
# A unique run id ties log lines together and records who/what/where/when.
import getpass
import socket

RUN_ID = str(uuid.uuid4())
try:
    _operator = getpass.getuser()
except Exception:
    _operator = "unknown"
try:
    _hostname = socket.gethostname()
except Exception:
    _hostname = "unknown"


def audit(event, **fields):
    """Emit a single-line structured audit record."""
    payload = {
        "audit": event,
        "run_id": RUN_ID,
        "operator": _operator,
        "host": _hostname,
        "ts": datetime.now().isoformat(),
    }
    payload.update(fields)
    logging.info("AUDIT " + json.dumps(payload, default=str))

# Resolve the Aerospike port: the TLS service port differs from the plaintext one,
# so defaulting to 3000 under --aerospike-tls would silently fail to connect.
AEROSPIKE_PORT = args.aerospike_port if args.aerospike_port else (4333 if args.aerospike_tls else 3000)

# Log security and encryption context
logging.info(f"Migration started at: {start_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")  # Show milliseconds
logging.info(f"Aerospike host: {args.aerospike_host}:{AEROSPIKE_PORT}")
logging.info(f"Valkey host: {args.valkey_host}:{args.valkey_port}")
logging.info(f"TLS (Aerospike source leg): {args.aerospike_tls}")
logging.info(f"TLS (Valkey destination leg): {args.tls}")
logging.info(f"Target namespace: {args.namespace if args.namespace else 'ALL'}")
logging.info(f"Target set: {args.set if args.set else 'ALL'}")

audit(
    "migration_start",
    aerospike_host=args.aerospike_host,
    aerospike_port=AEROSPIKE_PORT,
    valkey_host=args.valkey_host,
    valkey_port=args.valkey_port,
    tls=args.tls,
    aerospike_tls=args.aerospike_tls,
    namespace=args.namespace or "ALL",
    set=args.set or "ALL",
    start_partition=args.start_partition,
    end_partition=args.end_partition,
    dry_run=args.dry_run,
    overwrite=args.overwrite,
)

# ---------------------------------------------------------------------------
# Transport encryption policy (T1: plaintext transport)
#
# Records travel over two hops: Aerospike -> this process -> Valkey. Encrypting
# only the destination hop still exposes every record on the wire, so both legs
# are evaluated and the migration fails closed: running any leg unencrypted now
# requires the explicit --allow-plaintext acknowledgement.
# ---------------------------------------------------------------------------
_plaintext_legs = []
if not args.aerospike_tls:
    _plaintext_legs.append("Aerospike source (--aerospike-tls)")
if not args.tls:
    _plaintext_legs.append("Valkey destination (--tls)")

if _plaintext_legs:
    logging.error("❌ Unencrypted transport on: " + "; ".join(_plaintext_legs))
    if args.require_tls:
        logging.error("   --require-tls is set. Refusing to run.")
        raise SystemExit(2)
    if not args.allow_plaintext:
        logging.error("   Every migrated record would cross the network in plaintext,")
        logging.error("   readable by anything on the path (threat T1).")
        logging.error("   Enable TLS on the leg(s) above, or pass --allow-plaintext to")
        logging.error("   explicitly accept plaintext transport (never for production data).")
        audit("migration_refused", reason="plaintext_transport", legs=_plaintext_legs)
        raise SystemExit(2)
    logging.warning("⚠️  --allow-plaintext given: proceeding with UNENCRYPTED transport on:")
    for _leg in _plaintext_legs:
        logging.warning(f"⚠️    - {_leg}")
    logging.warning("⚠️  Data is exposed to anyone able to observe the network path.")
    audit("plaintext_transport_accepted", legs=_plaintext_legs)
else:
    logging.info("✅ TLS enabled end to end (Aerospike source and Valkey destination)")

# ---------------------------------------------------------------------------
# Mapping-config integrity (T4). When a digest is pinned, verify it here -
# independently of whether the mapping engine is used - because the launcher
# derives the destination host from this same file. Each worker re-checks it, so
# a file swapped between the launcher's read and a worker's start is caught.
# ---------------------------------------------------------------------------
if args.mapping_config_sha256:
    try:
        from config_security import load_trusted_config
        _cfg, _cfg_path, _cfg_digest, _cfg_warnings = load_trusted_config(
            args.mapping_config, args.mapping_config_sha256
        )
        for _warning in _cfg_warnings:
            logging.warning(_warning)
        logging.info(f"✅ Mapping config verified: {_cfg_path} (sha256={_cfg_digest})")
        audit("mapping_config_verified", path=_cfg_path, sha256=_cfg_digest)
    except ConfigSecurityError as e:
        logging.error(f"❌ Mapping configuration failed its trust checks: {e}")
        audit("migration_refused", reason="untrusted_mapping_config",
              path=args.mapping_config)
        raise SystemExit(2)

# Resolve TLS certificate-validation policy (T2: MITM via disabled cert validation)
# Default to full verification. Only disable when the operator explicitly opts in.
ssl_settings = {}
if args.tls:
    if args.allow_insecure_tls:
        logging.warning("⚠️  TLS certificate validation is DISABLED (--allow-insecure-tls).")
        logging.warning("⚠️  The connection is vulnerable to man-in-the-middle attacks. Do NOT use in production.")
        ssl_settings = {"ssl": True, "ssl_cert_reqs": "none"}
    else:
        if not args.tls_ca_cert:
            logging.error("❌ TLS is enabled but no CA certificate was provided.")
            logging.error("   Pass --tls-ca-cert <path> to verify the Valkey server certificate,")
            logging.error("   or explicitly pass --allow-insecure-tls to disable validation (unsafe).")
            raise SystemExit(2)
        logging.info(f"✅ TLS certificate validation enabled using CA bundle: {args.tls_ca_cert}")
        ssl_settings = {
            "ssl": True,
            "ssl_cert_reqs": "required",
            "ssl_ca_certs": args.tls_ca_cert,
        }
else:
    ssl_settings = {"ssl": False}

if args.dry_run:
    logging.info("🧪 DRY-RUN mode enabled - no data will be written to Valkey.")
if args.overwrite:
    logging.warning("⚠️  Overwrite mode enabled - existing destination keys may be replaced.")

# Connect to Aerospike (T1: encrypt the source leg, not just the destination leg)
if args.aerospike_tls:
    # The tls-name is the identity the server certificate is checked against;
    # without it the client cannot verify who it is talking to, so it is required
    # rather than optional.
    if not args.aerospike_tls_name:
        logging.error("❌ --aerospike-tls requires --aerospike-tls-name (the tls-name")
        logging.error("   configured on the Aerospike nodes) so the server certificate")
        logging.error("   can be verified. Refusing to run.")
        raise SystemExit(2)
    if not args.aerospike_tls_ca_cert:
        logging.error("❌ --aerospike-tls requires --aerospike-tls-ca-cert so the Aerospike")
        logging.error("   server certificate can be verified. Refusing to run.")
        raise SystemExit(2)
    if not os.path.isfile(args.aerospike_tls_ca_cert):
        logging.error(f"❌ Aerospike CA bundle not found: {args.aerospike_tls_ca_cert}")
        raise SystemExit(2)
    if bool(args.aerospike_tls_certfile) != bool(args.aerospike_tls_keyfile):
        logging.error("❌ --aerospike-tls-certfile and --aerospike-tls-keyfile must be "
                      "supplied together for mutual TLS.")
        raise SystemExit(2)

    aero_tls = {
        'enable': True,
        'cafile': args.aerospike_tls_ca_cert,
        # Encrypt the whole session, not only the login exchange - the records
        # themselves are what need protecting.
        'for_login_only': False,
    }
    if args.aerospike_tls_certfile:
        aero_tls['certfile'] = args.aerospike_tls_certfile
        aero_tls['keyfile'] = args.aerospike_tls_keyfile

    aero_conf = {
        'hosts': [(args.aerospike_host, AEROSPIKE_PORT, args.aerospike_tls_name)],
        'tls': aero_tls,
    }
    logging.info(f"✅ Aerospike TLS enabled (tls-name={args.aerospike_tls_name}, "
                 f"CA bundle={args.aerospike_tls_ca_cert})")
else:
    aero_conf = {'hosts': [(args.aerospike_host, AEROSPIKE_PORT)]}

aero_client = aerospike.client(aero_conf).connect()

# Connect to Valkey
_real_valkey_client = RedisCluster(
    startup_nodes=[ClusterNode(args.valkey_host, args.valkey_port)],
    decode_responses=True,
    **ssl_settings
)


class SafeValkeyClient:
    """Wrapper enforcing dry-run and overwrite-protection policies (threat T6).

    - When dry_run is True, all mutating commands are logged and skipped.
    - When overwrite is False, mutating commands targeting an already-existing
      key are skipped so a re-run cannot silently corrupt/overwrite production
      data. Read commands and metadata commands always pass through.
    """

    # Commands that create/modify data and are keyed on their first argument.
    _WRITE_COMMANDS = {
        'set', 'rpush', 'hset', 'geoadd', 'pfadd', 'delete', 'expire'
    }
    # Commands that are always allowed even in dry-run (reads / introspection).
    _READ_COMMANDS = {
        'get', 'exists', 'type', 'hgetall', 'lrange', 'smembers',
        'zrange', 'geopos', 'get_nodes', 'scan', 'close'
    }

    def __init__(self, client, dry_run=False, overwrite=False):
        self._client = client
        self._dry_run = dry_run
        self._overwrite = overwrite
        self.skipped_existing = 0
        self.skipped_dry_run = 0

    def _key_exists(self, key):
        try:
            return bool(self._client.exists(key))
        except Exception:
            # If we cannot determine existence, fail safe by treating as existing
            # only when overwrite protection is active.
            return not self._overwrite

    def _guard(self, name, args_):
        """Return True if the operation should proceed, False if skipped."""
        key = args_[0] if args_ else None
        if self._dry_run:
            logging.info(f"[dry-run] would {name.upper()} {key}")
            self.skipped_dry_run += 1
            return False
        # delete/expire operate on keys we just wrote; only guard creators.
        if not self._overwrite and name in ('set', 'rpush', 'hset', 'geoadd', 'pfadd') and key is not None:
            if self._key_exists(key):
                logging.warning(
                    f"Skipping {name.upper()} for existing key {key} "
                    f"(use --overwrite to replace)"
                )
                self.skipped_existing += 1
                return False
        return True

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        if name in self._WRITE_COMMANDS:
            def wrapper(*a, **kw):
                key = a[0] if a else None
                # Allow cleanup of the internal RedisJSON capability probe key.
                if isinstance(key, str) and key.startswith('__json_test_'):
                    return attr(*a, **kw)
                if not self._guard(name, a):
                    return None
                return attr(*a, **kw)
            return wrapper

        if name == 'execute_command':
            def exec_wrapper(command, *a, **kw):
                cmd = str(command).lower()
                key = a[0] if a else None
                # Always allow the internal RedisJSON capability probe so module
                # detection works even in dry-run / overwrite-protected modes.
                if isinstance(key, str) and key.startswith('__json_test_'):
                    return attr(command, *a, **kw)
                # Guard JSON.SET / PFADD / GEOADD issued via execute_command.
                if cmd in ('json.set', 'pfadd', 'geoadd', 'set'):
                    if not self._guard(cmd.split('.')[0] if cmd == 'json.set' else cmd, a):
                        return None
                return attr(command, *a, **kw)
            return exec_wrapper

        return attr


valkey_client = SafeValkeyClient(
    _real_valkey_client,
    dry_run=args.dry_run,
    overwrite=args.overwrite,
)

# Initialize counters and tracking
record_count = 0
json_count = 0
standard_count = 0
native_type_count = 0
geospatial_count = 0
hyperloglog_count = 0
geo_count = 0
hll_count = 0
multi_key_records = 0
single_key_records = 0

# Track record to key mapping for summary
record_to_keys_map = {}
multi_key_records = 0
single_key_records = 0

# Check if RedisJSON is available
has_json_support = False
try:
    # First try the MODULE LIST command
    try:
        result = valkey_client.execute_command('MODULE', 'LIST')
        has_json_support = any('ReJSON' in str(module) for module in result)
    except Exception:
        # If MODULE LIST fails, try a direct JSON command test
        try:
            # Try a simple JSON.SET command on a temporary key
            test_key = f"__json_test_{uuid.uuid4()}"
            valkey_client.execute_command('JSON.SET', test_key, '$', '{"test": true}')
            valkey_client.delete(test_key)  # Clean up
            has_json_support = True
            logging.info("RedisJSON module detected via command test")
        except Exception:
            has_json_support = False
            logging.warning("RedisJSON module not detected")
    
    if has_json_support:
        logging.info("RedisJSON module detected - will use native JSON for complex nested structures")
    else:
        logging.warning("RedisJSON module not detected - will use standard Redis types")
        if args.use_json:
            logging.warning("--use-json flag ignored as RedisJSON module is not available")
except Exception as e:
    logging.warning(f"Could not check for RedisJSON module: {e}")
    logging.warning("Will use standard Redis types for all data")

# Initialize mapping engine if requested
mapping_engine = None
use_mapping_engine = False

if MAPPING_ENGINE_AVAILABLE and args.use_mapping_engine:
    try:
        mapping_engine = MappingEngine(args.mapping_config, args.mapping_config_sha256)
        use_mapping_engine = True
        logging.info(f"✅ Initialized mapping engine with verified config: "
                     f"{mapping_engine.config_path} (sha256={mapping_engine.config_sha256})")
        # T4/T12: record which configuration governed this run so the destination
        # key layout can be reconstructed from the audit trail.
        audit("mapping_config_loaded",
              path=mapping_engine.config_path,
              sha256=mapping_engine.config_sha256)
    except ConfigSecurityError as e:
        # A config that fails its trust checks must never be silently downgraded
        # to "legacy mapping" - the operator asked for configured behaviour and
        # the file may have been tampered with (threat T4).
        logging.error(f"❌ Mapping configuration failed its trust checks: {e}")
        logging.error("   Refusing to run. Fix the configuration (or re-pin it after")
        logging.error("   reviewing the change) and try again.")
        audit("migration_refused", reason="untrusted_mapping_config",
              path=args.mapping_config)
        raise SystemExit(2)
    except Exception as e:
        logging.error(f"❌ Failed to initialize mapping engine: {e}")
        logging.error("   Refusing to fall back to legacy mapping, which would write a")
        logging.error("   different key layout than the one that was configured.")
        audit("migration_refused", reason="mapping_engine_init_failed",
              path=args.mapping_config)
        raise SystemExit(2)
elif args.use_mapping_engine:
    logging.error("❌ Mapping engine requested but not available - install PyYAML.")
    logging.error("   Refusing to fall back to legacy mapping, which would write a")
    logging.error("   different key layout than the one that was configured.")
    audit("migration_refused", reason="mapping_engine_unavailable")
    raise SystemExit(2)
else:
    logging.info("Using legacy mapping (use --use-mapping-engine for configurable mapping)")

def migrate_record_with_mapping_engine(record, prefix):
    """Migrate record using the configurable mapping engine"""
    global record_count, multi_key_records, single_key_records
    
    try:
        # Get mapping results from engine
        mapping_results = mapping_engine.map_record(record, prefix)
        
        # Execute each mapping result and track success
        successful_operations = 0
        for result in mapping_results:
            success = execute_mapping_result(result)
            if success:
                successful_operations += 1
            else:
                logging.error(f"Failed to execute mapping for key: {result.key}")
        
        # Only count as successful if ALL operations succeeded
        if successful_operations == len(mapping_results) and len(mapping_results) > 0:
            record_count += 1
            if len(mapping_results) > 1:
                multi_key_records += 1
            else:
                single_key_records += 1
            
            # Track keys for summary
            key, meta, bins = record
            record_id = extract_key(key)
            if record_id not in record_to_keys_map:
                record_to_keys_map[record_id] = []
            
            for result in mapping_results:
                if result.key not in record_to_keys_map[record_id]:
                    record_to_keys_map[record_id].append(result.key)
            
            return True
        else:
            logging.warning(f"Record migration failed: {successful_operations}/{len(mapping_results)} operations succeeded")
            return False
        
    except Exception as e:
        logging.error(f"Error in mapping engine migration: {e}")
        return False

def execute_mapping_result(result: MappingResult) -> bool:
    """Execute a single mapping result"""
    global native_type_count, json_count, geo_count, hll_count
    
    def serialize_for_json(obj):
        """Convert objects to JSON-serializable format"""
        if isinstance(obj, (bytes, bytearray)):
            import base64
            return base64.b64encode(obj).decode('utf-8')
        elif isinstance(obj, dict):
            return {k: serialize_for_json(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [serialize_for_json(item) for item in obj]
        else:
            return obj
    
    try:
        if result.valkey_command == "SET":
            valkey_client.set(result.key, result.value)
            native_type_count += 1
            logging.debug(f"SET {result.key} = {redact(result.value)}")
            
        elif result.valkey_command == "RPUSH":
            valkey_client.delete(result.key)  # Clear existing
            if isinstance(result.value, list) and result.value:
                valkey_client.rpush(result.key, *result.value)
                logging.debug(f"RPUSH {result.key} {len(result.value)} items")
            native_type_count += 1
            
        elif result.valkey_command == "HSET":
            if isinstance(result.value, dict) and result.value:
                valkey_client.hset(result.key, mapping=result.value)
                logging.debug(f"HSET {result.key} {len(result.value)} fields")
            native_type_count += 1
            
        elif result.valkey_command == "JSON.SET":
            if has_json_support:
                path = result.additional_params.get('path', '$') if result.additional_params else '$'
                # Serialize binary data before JSON conversion
                serializable_value = serialize_for_json(result.value)
                json_data = json.dumps(serializable_value)
                valkey_client.execute_command('JSON.SET', result.key, path, json_data)
                logging.debug(f"JSON.SET {result.key} {path}")
            else:
                # Fallback to string
                serializable_value = serialize_for_json(result.value)
                valkey_client.set(result.key, json.dumps(serializable_value))
                logging.debug(f"SET {result.key} (JSON fallback)")
            json_count += 1
            
        elif result.valkey_command == "GEOADD":
            # Extract coordinates and add to geospatial index
            if isinstance(result.value, dict) and 'coordinates' in result.value:
                coords = result.value['coordinates']
                if len(coords) >= 2:
                    lon, lat = coords[0], coords[1]
                    member = result.key.split(':')[-1]  # Use last part as member
                    geo_key = result.key + "_geo"
                    valkey_client.geoadd(geo_key, lon, lat, member)
                    logging.debug(f"GEOADD {geo_key} {lon} {lat} {member}")
                    geo_count += 1
            
        elif result.valkey_command == "PFADD":
            if isinstance(result.value, list):
                for item in result.value:
                    valkey_client.pfadd(result.key, str(item))
                logging.debug(f"PFADD {result.key} {len(result.value)} items")
                hll_count += 1
        
        else:
            logging.warning(f"Unknown command: {result.valkey_command}")
            return False
        
        # Set TTL if specified
        if result.ttl:
            valkey_client.expire(result.key, result.ttl)
        
        return True
        
    except Exception as e:
        logging.error(f"Failed to execute {result.valkey_command} for {result.key}: {e}")
        return False

# Detect if a value is likely a timestamp (integer within reasonable range)
def is_likely_timestamp(value):
    # Explicitly exclude boolean values first
    if isinstance(value, bool):
        return False
    # Check if it's an integer in a reasonable timestamp range
    # This covers timestamps from 1970 to 2100 in seconds or milliseconds
    if isinstance(value, int):
        return 0 < value < 4102444800000  # Jan 1, 2100 in milliseconds
    return False

# Detect if a record or field should be treated as HyperLogLog data.
#
# HyperLogLog conversion is LOSSY: the original list values are replaced by a
# probabilistic cardinality estimate and cannot be recovered. To avoid silently
# and irreversibly destroying data (threat T13), conversion requires an explicit
# opt-in signal rather than heuristics on list size or duplicate ratio. A field
# is only treated as HyperLogLog when:
#   - the record explicitly declares type == 'hyperloglog', OR
#   - the field name clearly indicates HLL intent (hll / hyperloglog).
_HLL_FIELD_INDICATORS = ('hll', 'hyperloglog')

def is_hyperloglog(value, field_name=None, record_bins=None):
    # Value must be a list to be eligible for HyperLogLog.
    if not isinstance(value, list):
        return False

    name = field_name.lower() if field_name else ''

    # Explicit record-level declaration.
    if record_bins and record_bins.get('type') == 'hyperloglog':
        if name and any(ind in name for ind in _HLL_FIELD_INDICATORS):
            return True

    # Explicit field-name opt-in.
    if name and any(ind in name for ind in _HLL_FIELD_INDICATORS):
        return True

    # No implicit heuristic coercion: preserve the list as-is by default.
    return False

# Detect if a value is a GeoJSON object (either native Aerospike GeoJSON or dictionary)
def is_geojson(value):
    logging.debug(f"Checking if value is GeoJSON: {type(value)} = {redact(value)}")
    
    # Check for Aerospike's Geospatial type (this is the key fix!)
    if type(value).__name__ == 'Geospatial' and hasattr(value, '__str__'):
        logging.info(f"Detected aerospike.Geospatial object: {redact(value)}")
        return True
    
    # Check for Aerospike's native GeoJSON type (MAP type)
    if hasattr(value, 'type') and value.type == aerospike.MAP:
        logging.debug("Found Aerospike MAP type, checking if it's GeoJSON")
        try:
            # Try to unwrap and check if it has GeoJSON structure
            unwrapped = value.unwrap() if hasattr(value, 'unwrap') else value
            logging.debug(f"Unwrapped value: {redact(unwrapped)}")
            if isinstance(unwrapped, dict) and 'type' in unwrapped and 'coordinates' in unwrapped:
                logging.info(f"Detected Aerospike GeoJSON object: {redact(unwrapped)}")
                return True
        except Exception as e:
            logging.debug(f"Error unwrapping potential GeoJSON: {e}")
            pass
    
    # Check for regular dictionary with GeoJSON structure
    if isinstance(value, dict):
        has_geojson_structure = 'type' in value and 'coordinates' in value and isinstance(value.get('coordinates'), list)
        if has_geojson_structure:
            logging.info(f"Detected dictionary GeoJSON object: {redact(value)}")
        return has_geojson_structure
    
    logging.debug("Value is not GeoJSON")
    return False

# Format timestamp according to user preference
def format_timestamp(timestamp):
    if args.timestamp_format == 'iso':
        # Determine if timestamp is in seconds or milliseconds
        if timestamp < 10000000000:  # Seconds (before 2286)
            return datetime.utcfromtimestamp(timestamp).isoformat() + 'Z'
        else:  # Milliseconds
            return datetime.utcfromtimestamp(timestamp/1000).isoformat() + 'Z'
    elif args.timestamp_format == 'unix':
        return str(timestamp)
    else:  # preserve
        return timestamp

# Extract key from Aerospike record
def extract_key(key):
    if len(key) > 2 and key[2] is not None:
        return str(key[2])
    elif len(key) > 3 and key[3] is not None:
        if isinstance(key[3], (bytes, bytearray)):
            return key[3].hex()
        else:
            return str(key[3])
    else:
        return str(uuid.uuid4())

# Count complex nested fields in a record
def count_complex_nested_fields(bins):
    count = 0
    for v in bins.values():
        # Check for nested structures that would benefit from JSON
        if isinstance(v, dict) and any(isinstance(sv, (dict, list)) for sv in v.values()):
            count += 1
        elif isinstance(v, list) and any(isinstance(item, (dict, list)) for item in v):
            count += 1
    return count

# Function to migrate data to Valkey HyperLogLog
def migrate_hyperloglog(key_str, field, value, record_id, ttl=None):
    global hyperloglog_count
    logging.info(f"migrate_hyperloglog called: key={key_str}, field={field}, value_type={type(value)}")
    logging.debug(f"HyperLogLog value: {redact(value)}")
    
    try:
        if isinstance(value, list):
            # Create HyperLogLog key
            hll_key = f"{key_str}:{field}"
            
            logging.info(f"Creating HyperLogLog: key={hll_key}")
            
            # Use PFADD to add all elements to HyperLogLog
            if value:  # Only if list is not empty
                try:
                    # Convert all values to strings for HyperLogLog
                    string_values = [str(item) for item in value]
                    logging.info(f"Adding {len(string_values)} items to HyperLogLog: {redact(string_values)}")
                    
                    # Use PFADD to add elements
                    result = valkey_client.execute_command('PFADD', hll_key, *string_values)
                    logging.info(f"PFADD result: {result}")
                    
                    # Get cardinality estimate
                    cardinality = valkey_client.execute_command('PFCOUNT', hll_key)
                    logging.info(f"HyperLogLog cardinality estimate: {cardinality}")
                    
                    hyperloglog_count += 1
                    
                    # Track the key being created
                    if record_id not in record_to_keys_map:
                        record_to_keys_map[record_id] = []
                    if hll_key not in record_to_keys_map[record_id]:
                        record_to_keys_map[record_id].append(hll_key)
                    
                    # Set TTL if provided
                    if ttl:
                        valkey_client.expire(hll_key, ttl)
                        logging.info(f"Set TTL {ttl} for HyperLogLog key {hll_key}")
                    
                    logging.info(f"Successfully converted list to HyperLogLog: {hll_key}")
                    return True
                    
                except Exception as e:
                    logging.error(f"Error executing PFADD command: {e}")
                    return False
            else:
                # Create empty HyperLogLog
                try:
                    # Initialize empty HyperLogLog by adding and removing a dummy value
                    valkey_client.execute_command('PFADD', hll_key, 'dummy')
                    valkey_client.execute_command('PFADD', hll_key)  # This will create empty HLL
                    
                    hyperloglog_count += 1
                    
                    # Track the key being created
                    if record_id not in record_to_keys_map:
                        record_to_keys_map[record_id] = []
                    if hll_key not in record_to_keys_map[record_id]:
                        record_to_keys_map[record_id].append(hll_key)
                    
                    if ttl:
                        valkey_client.expire(hll_key, ttl)
                    
                    logging.info(f"Successfully created empty HyperLogLog: {hll_key}")
                    return True
                except Exception as e:
                    logging.error(f"Error creating empty HyperLogLog: {e}")
                    return False
        else:
            logging.warning(f"Value is not a list, cannot convert to HyperLogLog: {type(value)}")
            return False
    except Exception as e:
        logging.error(f"Failed to process HyperLogLog: {e}")
        return False

# Function to migrate GeoJSON to Valkey
def migrate_geojson(key_str, field, value, record_id, ttl=None):
    global geospatial_count
    logging.info(f"migrate_geojson called: key={key_str}, field={field}, value_type={type(value)}")
    logging.debug(f"GeoJSON value: {redact(value)}")
    
    try:
        # Handle aerospike.Geospatial objects
        if type(value).__name__ == 'Geospatial':
            logging.info("Processing aerospike.Geospatial object")
            # Convert the Geospatial object to string and parse as JSON
            geo_json_str = str(value)
            logging.debug(f"Geospatial string representation: {redact(geo_json_str)}")
            geo_data = json.loads(geo_json_str)
            logging.debug(f"Parsed GeoJSON data: {redact(geo_data)}")
        # Unwrap Aerospike GeoJSON if needed (MAP type)
        elif hasattr(value, 'unwrap'):
            logging.info("Unwrapping Aerospike GeoJSON object")
            geo_data = value.unwrap()
            logging.debug(f"Unwrapped GeoJSON data: {redact(geo_data)}")
        else:
            geo_data = value
            logging.debug(f"Using direct GeoJSON data: {redact(geo_data)}")
        
        if geo_data['type'].lower() == 'point':
            # Extract coordinates
            lon, lat = geo_data['coordinates']
            logging.info(f"Extracted Point coordinates: lon={lon}, lat={lat}")
            
            # Create geospatial key - use the record key without field suffix
            geo_key = key_str
            member_name = field
            
            logging.info(f"Creating geospatial index: key={geo_key}, member={member_name}")
            
            # Use GEOADD to store the coordinates
            try:
                # First check if key exists
                if valkey_client.exists(geo_key):
                    key_type = valkey_client.type(geo_key)
                    logging.info(f"Existing key {geo_key} has type: {key_type}")
                    
                    # If it's not a zset, delete it
                    if key_type != 'zset':
                        logging.info(f"Deleting existing key {geo_key} of type {key_type}")
                        valkey_client.delete(geo_key)
                
                # Use execute_command directly for GEOADD to avoid parameter formatting issues
                logging.info(f"Executing GEOADD command: GEOADD {geo_key} {lon} {lat} {member_name}")
                result = valkey_client.execute_command('GEOADD', geo_key, lon, lat, member_name)
                logging.info(f"GEOADD result: {result}")
                
                if result > 0:
                    logging.info(f"Successfully converted GeoJSON to Geospatial: {geo_key}")
                    geospatial_count += 1
                    
                    # Track the key being created
                    if record_id not in record_to_keys_map:
                        record_to_keys_map[record_id] = []
                    if geo_key not in record_to_keys_map[record_id]:
                        record_to_keys_map[record_id].append(geo_key)
                    
                    # Set TTL if provided
                    if ttl:
                        valkey_client.expire(geo_key, ttl)
                        logging.info(f"Set TTL {ttl} for geospatial key {geo_key}")
                    
                    # Verify the geospatial data was stored correctly
                    try:
                        pos = valkey_client.geopos(geo_key, member_name)
                        logging.info(f"Verification - GEOPOS {geo_key} {member_name}: {pos}")
                    except Exception as e:
                        logging.warning(f"Could not verify geospatial data: {e}")
                    
                    return True
                else:
                    logging.warning(f"GEOADD returned {result} for {geo_key} - no data was added")
                    return False
            except Exception as e:
                logging.error(f"Error executing GEOADD command: {e}")
                return False
        else:
            logging.warning(f"Unsupported GeoJSON type: {geo_data['type']} (only Point is supported)")
            return False
    except Exception as e:
        logging.error(f"Failed to process GeoJSON: {e}")
        return False

# Migrate a single value using the appropriate Valkey data type
def migrate_value(key, field, value, record_id, ttl=None, record_bins=None):
    global native_type_count, json_count
    
    # Track the key being created
    key_name = f"{key}:{field}"
    if record_id not in record_to_keys_map:
        record_to_keys_map[record_id] = []
    if key_name not in record_to_keys_map[record_id]:
        record_to_keys_map[record_id].append(key_name)
    
    # Handle None/null values
    if value is None:
        return
    
    # Handle timestamp values
    if is_likely_timestamp(value):
        formatted_timestamp = format_timestamp(value)
        valkey_client.set(f"{key}:{field}", formatted_timestamp)
        if ttl:
            valkey_client.expire(f"{key}:{field}", ttl)
        return
    
    # Handle HyperLogLog values - convert to Valkey HyperLogLog
    if is_hyperloglog(value, field, record_bins):
        logging.info(f"Processing HyperLogLog field: {field} in record {key}")
        success = migrate_hyperloglog(key, field, value, record_id, ttl)
        if success:
            logging.info(f"Successfully migrated HyperLogLog field {field}")
            return
        
        # Fall back to regular list if HyperLogLog conversion failed
        logging.warning(f"Falling back to regular list for HyperLogLog: {key}:{field}")
    
    # Handle GeoJSON values - convert to Valkey Geospatial
    if is_geojson(value):
        logging.info(f"Processing GeoJSON field: {field} in record {key}")
        success = migrate_geojson(key, field, value, record_id, ttl)
        if success:
            logging.info(f"Successfully migrated GeoJSON field {field} to geospatial")
            return
        
        # Fall back to JSON string if conversion failed
        logging.warning(f"Falling back to JSON string for GeoJSON: {key}:{field}")
        # Handle different types of GeoJSON objects
        if type(value).__name__ == 'Geospatial':
            # Convert Geospatial object to string
            geo_json_str = str(value)
            valkey_client.set(f"{key}:{field}", geo_json_str)
        elif hasattr(value, 'unwrap'):
            geo_data = value.unwrap()
            valkey_client.set(f"{key}:{field}", json.dumps(geo_data))
        else:
            valkey_client.set(f"{key}:{field}", json.dumps(value))
        if ttl:
            valkey_client.expire(f"{key}:{field}", ttl)
        return
    
    # Handle list values - use Valkey List type or native JSON for complex lists
    if isinstance(value, list):
        logging.info(f"Processing list value: {redact(value)}")
        # Check if it's a simple list (no nested complex structures)
        is_simple_list = not any(isinstance(item, (dict, list)) for item in value)
        logging.info(f"Is simple list: {is_simple_list}")
        
        if is_simple_list:
            try:
                # Convert all items to strings for Redis list
                string_items = [str(item) for item in value]
                # Delete any existing list
                valkey_client.delete(f"{key}:{field}")
                # Use RPUSH to create the list
                if string_items:
                    valkey_client.rpush(f"{key}:{field}", *string_items)
                    if ttl:
                        valkey_client.expire(f"{key}:{field}", ttl)
                    native_type_count += 1
                    logging.info(f"Stored simple list as native Redis list: {key}:{field}")
                    return
            except Exception as e:
                logging.warning(f"Failed to convert to Redis list: {e}")
        
        # For complex lists, use native JSON if available
        logging.info(f"Complex list detected, has_json_support: {has_json_support}")
        if has_json_support:
            try:
                # Use JSON.SET to store as native JSON
                json_key = f"{key}:{field}"
                json_data = json.dumps(value)
                logging.info(f"Attempting JSON.SET for key: {json_key}, data: {json_data}")
                valkey_client.execute_command('JSON.SET', json_key, '$', json_data)
                logging.info(f"Successfully stored complex list as native JSON: {json_key}")
                if ttl:
                    valkey_client.expire(json_key, ttl)
                
                # Track the key being created
                if record_id not in record_to_keys_map:
                    record_to_keys_map[record_id] = []
                if json_key not in record_to_keys_map[record_id]:
                    record_to_keys_map[record_id].append(json_key)
                
                # Increment the appropriate counter
                json_count += 1
                return
            except Exception as e:
                logging.error(f"Failed to store as native JSON: {e}")
                logging.warning(f"Falling back to JSON string for complex list")
        else:
            logging.warning(f"JSON support not available, storing complex list as JSON string")
        
        # Fall back to JSON string if native JSON is not available or fails
        valkey_client.set(f"{key}:{field}", json.dumps(value))
        logging.info(f"Stored complex list as JSON string: {key}:{field}")
        if ttl:
            valkey_client.expire(f"{key}:{field}", ttl)
        return
    
    # Handle binary data - base64 encode
    if isinstance(value, (bytes, bytearray)):
        try:
            encoded = base64.b64encode(value).decode('utf-8')
            valkey_client.set(f"{key}:{field}", encoded)
            if ttl:
                valkey_client.expire(f"{key}:{field}", ttl)
            logging.info(f"Converted binary data to base64: {key}:{field}")
            return
        except Exception as e:
            logging.warning(f"Failed to convert binary data: {e}")
            # If we can't encode it, store as empty string
            valkey_client.set(f"{key}:{field}", "")
            if ttl:
                valkey_client.expire(f"{key}:{field}", ttl)
            return
    
    # Handle map values - use Valkey Hash if simple, native JSON if complex
    if isinstance(value, dict):
        logging.info(f"Processing map value: {redact(value)}")
        # Check if it's a simple map (no nested complex structures)
        is_simple_map = not any(isinstance(v, (dict, list)) for v in value.values())
        logging.info(f"Is simple map: {is_simple_map}")
        
        if is_simple_map:
            try:
                # Convert all values to strings for Redis hash
                string_map = {k: str(v) for k, v in value.items()}
                valkey_client.hset(f"{key}:{field}", mapping=string_map)
                if ttl:
                    valkey_client.expire(f"{key}:{field}", ttl)
                native_type_count += 1
                logging.info(f"Stored simple map as native Redis hash: {key}:{field}")
                return
            except Exception as e:
                logging.warning(f"Failed to convert to Redis hash: {e}")
        
        # For complex maps, use native JSON if available
        logging.info(f"Complex map detected, has_json_support: {has_json_support}")
        if has_json_support:
            try:
                # Use JSON.SET to store as native JSON
                json_key = f"{key}:{field}"
                json_data = json.dumps(value)
                logging.info(f"Attempting JSON.SET for key: {json_key}, data: {json_data}")
                valkey_client.execute_command('JSON.SET', json_key, '$', json_data)
                logging.info(f"Successfully stored complex map as native JSON: {json_key}")
                if ttl:
                    valkey_client.expire(json_key, ttl)
                
                # Track the key being created
                if record_id not in record_to_keys_map:
                    record_to_keys_map[record_id] = []
                if json_key not in record_to_keys_map[record_id]:
                    record_to_keys_map[record_id].append(json_key)
                
                # Increment the appropriate counter
                json_count += 1
                return
            except Exception as e:
                logging.error(f"Failed to store as native JSON: {e}")
                logging.warning(f"Falling back to JSON string for complex map")
        else:
            logging.warning(f"JSON support not available, storing complex map as JSON string")
        
        # Fall back to JSON string if native JSON is not available or fails
        valkey_client.set(f"{key}:{field}", json.dumps(value))
        logging.info(f"Stored complex map as JSON string: {key}:{field}")
        if ttl:
            valkey_client.expire(f"{key}:{field}", ttl)
        valkey_client.set(f"{key}:{field}", json.dumps(value))
        if ttl:
            valkey_client.expire(f"{key}:{field}", ttl)
        return
    
    # Handle boolean values - store as string "true" or "false"
    if isinstance(value, bool):
        valkey_client.set(f"{key}:{field}", str(value).lower())
        if ttl:
            valkey_client.expire(f"{key}:{field}", ttl)
        return
    
    # Default case - store as string
    valkey_client.set(f"{key}:{field}", str(value))
    if ttl:
        valkey_client.expire(f"{key}:{field}", ttl)

# Migrate record using native JSON
def migrate_record_json(key_str, bins, ttl, record_id):
    global json_count
    
    # Process all values
    processed_bins = {}
    for k, v in bins.items():
        if v is None:
            continue
        if is_likely_timestamp(v):
            processed_bins[k] = format_timestamp(v)
        elif is_geojson(v):
            # Handle GeoJSON - unwrap if needed
            if hasattr(v, 'unwrap'):
                processed_bins[k] = v.unwrap()
            else:
                processed_bins[k] = v
        elif isinstance(v, (bytes, bytearray)):
            try:
                processed_bins[k] = {
                    "_datatype": "binary",
                    "data": base64.b64encode(v).decode('utf-8')
                }
            except Exception:
                processed_bins[k] = {"_datatype": "binary", "data": ""}
        else:
            try:
                processed_bins[k] = v
            except Exception:
                # If we can't process it, convert to string
                processed_bins[k] = str(v)
    
    # Use JSON.SET to store the entire record
    if processed_bins:
        try:
            # Try using JSON.SET command
            try:
                valkey_client.execute_command('JSON.SET', key_str, '$', json.dumps(processed_bins))
                
                # Track the key being created
                if record_id not in record_to_keys_map:
                    record_to_keys_map[record_id] = []
                if key_str not in record_to_keys_map[record_id]:
                    record_to_keys_map[record_id].append(key_str)
                
                if ttl:
                    valkey_client.expire(key_str, ttl)
                json_count += 1
                
                # Also check for GeoJSON and HyperLogLog fields and create specialized indexes
                for field, value in bins.items():
                    if is_geojson(value):
                        migrate_geojson(key_str, field, value, record_id, ttl)
                    elif is_hyperloglog(value, field, bins):
                        migrate_hyperloglog(key_str, field, value, record_id, ttl)
                
                return True
            except Exception as e:
                if "unknown command" in str(e).lower():
                    # If JSON.SET is not available, mark JSON as not supported for future records
                    global has_json_support
                    has_json_support = False
                    logging.warning(f"JSON.SET command not supported: {e}")
                    logging.warning("Disabling JSON support for remaining records")
                    return False
                else:
                    # Other errors might be record-specific
                    logging.error(f"Error using JSON.SET for {key_str}: {e}")
                    return False
        except Exception as e:
            logging.error(f"Error using JSON.SET for {key_str}: {e}")
            return False
    return False

# Migrate record using native Valkey data types
def migrate_record_native(key_str, bins, ttl, record_id):
    global standard_count
    
    try:
        # For single-bin records
        if len(bins) == 1:
            field, value = next(iter(bins.items()))
            migrate_value(key_str, field, value, record_id, ttl, bins)
            standard_count += 1
            return True
        
        # For multi-bin records with simple types, use hash
        if not any(isinstance(v, (dict, list)) for v in bins.values()) and not any(is_geojson(v) for v in bins.values()):
            # Convert all values to strings for Redis hash
            string_map = {}
            for k, v in bins.items():
                if v is None:
                    continue
                elif is_likely_timestamp(v):
                    string_map[k] = format_timestamp(v)
                elif isinstance(v, bool):
                    string_map[k] = str(v).lower()
                elif isinstance(v, (int, float, str)):
                    string_map[k] = str(v)
                elif isinstance(v, (bytes, bytearray)):
                    try:
                        string_map[k] = base64.b64encode(v).decode('utf-8')
                    except Exception:
                        string_map[k] = ""
                else:
                    try:
                        string_map[k] = json.dumps(v)
                    except TypeError:
                        # Handle non-serializable objects
                        string_map[k] = str(v)
            
            # Only proceed if we have key-value pairs to add
            if string_map:
                try:
                    valkey_client.hset(key_str, mapping=string_map)
                    # Track the key being created
                    if record_id not in record_to_keys_map:
                        record_to_keys_map[record_id] = []
                    if key_str not in record_to_keys_map[record_id]:
                        record_to_keys_map[record_id].append(key_str)
                    
                    if ttl:
                        valkey_client.expire(key_str, ttl)
                    standard_count += 1
                    return True
                except Exception as e:
                    logging.warning(f"Failed to convert to Redis hash: {e}")
                    # Fall back to storing each field separately
                    for field, value in string_map.items():
                        valkey_client.set(f"{key_str}:{field}", value)
                        # Track the key being created
                        if record_id not in record_to_keys_map:
                            record_to_keys_map[record_id] = []
                        if f"{key_str}:{field}" not in record_to_keys_map[record_id]:
                            record_to_keys_map[record_id].append(f"{key_str}:{field}")
                    
                    if ttl:
                        for field in string_map.keys():
                            valkey_client.expire(f"{key_str}:{field}", ttl)
                    standard_count += 1
                    return True
            else:
                # If we have no valid fields, create a placeholder
                valkey_client.set(key_str, "empty_record")
                # Track the key being created
                if record_id not in record_to_keys_map:
                    record_to_keys_map[record_id] = []
                if key_str not in record_to_keys_map[record_id]:
                    record_to_keys_map[record_id].append(key_str)
                
                if ttl:
                    valkey_client.expire(key_str, ttl)
                logging.warning(f"Created placeholder for empty record: {key_str}")
                standard_count += 1
                return True
        
        # For multi-bin records with complex types, store each field separately
        for field, value in bins.items():
            migrate_value(key_str, field, value, record_id, ttl, bins)
        
        standard_count += 1
        return True
    except Exception as e:
        logging.error(f"Error in native migration for {key_str}: {e}")
        return False

# Improved migration logic with correct scalar/complex mapping
def migrate_record_improved(record, prefix):
    """
    Migrate Aerospike record with optimal mapping strategy:
    - Single scalar bin → Single native Valkey key
    - Single complex bin → Single JSON key  
    - Multiple bins → Separate keys with appropriate types
    """
    global record_count, multi_key_records, single_key_records
    try:
        key, meta, bins = record
        record_id = extract_key(key)
        base_key = f"{prefix}:{record_id}"
        ttl = meta.get('ttl')

        # Handle single bin records
        if len(bins) == 1:
            field_name, field_value = next(iter(bins.items()))
            
            if is_scalar_type(field_value):
                # Single scalar bin → Single native key (no field suffix)
                migrate_scalar_value(base_key, field_value, ttl, record_id)
                single_key_records += 1
            else:
                # Single complex bin → Single JSON key (no field suffix)
                migrate_complex_value(base_key, field_value, ttl, record_id)
                single_key_records += 1
        else:
            # Multiple bins → Separate keys with field suffixes
            for field_name, field_value in bins.items():
                field_key = f"{base_key}:{field_name}"
                
                if is_scalar_type(field_value):
                    migrate_scalar_value(field_key, field_value, ttl, record_id)
                else:
                    migrate_complex_value(field_key, field_value, ttl, record_id)
            
            multi_key_records += 1

        record_count += 1
        return True
        
    except Exception as e:
        logging.error(f"Error migrating record {record_id}: {e}")
        return False

def is_scalar_type(value):
    """Check if value is a scalar type that should use native Valkey storage"""
    return isinstance(value, (int, float, str, bool)) and not is_geojson(value)

def migrate_scalar_value(key, value, ttl, record_id):
    """Migrate scalar values to native Valkey types"""
    global native_type_count
    
    # Track the key being created
    if record_id not in record_to_keys_map:
        record_to_keys_map[record_id] = []
    if key not in record_to_keys_map[record_id]:
        record_to_keys_map[record_id].append(key)
    
    try:
        if value is None:
            valkey_client.set(key, "")
        elif isinstance(value, bool):
            # Store boolean as integer (Redis standard)
            valkey_client.set(key, 1 if value else 0)
        elif isinstance(value, (int, float)):
            # Store numbers as native types
            valkey_client.set(key, value)
        elif isinstance(value, str):
            # Store strings as native strings
            valkey_client.set(key, value)
        elif isinstance(value, (bytes, bytearray)):
            # Store binary as base64
            encoded = base64.b64encode(value).decode('utf-8')
            valkey_client.set(key, f"BINARY:{encoded}")
        else:
            # Fallback to string representation
            valkey_client.set(key, str(value))
        
        if ttl:
            valkey_client.expire(key, ttl)
        
        native_type_count += 1
        logging.debug(f"Stored scalar as native type: {key} = {redact(value)}")
        
    except Exception as e:
        logging.error(f"Failed to store scalar value {key}: {e}")

def migrate_complex_value(key, value, ttl, record_id):
    """Migrate complex values using appropriate Valkey data structures"""
    global json_count, native_type_count, geo_count, hll_count
    
    # Track the key being created
    if record_id not in record_to_keys_map:
        record_to_keys_map[record_id] = []
    if key not in record_to_keys_map[record_id]:
        record_to_keys_map[record_id].append(key)
    
    try:
        # Handle special types first
        if is_geojson(value):
            if migrate_geojson_to_native(key, value, ttl):
                geo_count += 1
                return
        
        if is_hyperloglog(value, key.split(':')[-1], None):
            if migrate_hyperloglog_to_native(key, value, ttl):
                hll_count += 1
                return
        
        # Handle lists
        if isinstance(value, list):
            if is_simple_list(value):
                # Simple list → Native Redis list
                migrate_to_redis_list(key, value, ttl)
                native_type_count += 1
            else:
                # Complex list → JSON
                migrate_to_json(key, value, ttl)
                json_count += 1
            return
        
        # Handle maps/dictionaries
        if isinstance(value, dict):
            if is_simple_map(value):
                # Simple map → Native Redis hash
                migrate_to_redis_hash(key, value, ttl)
                native_type_count += 1
            else:
                # Complex map → JSON
                migrate_to_json(key, value, ttl)
                json_count += 1
            return
        
        # Default: store as JSON
        migrate_to_json(key, value, ttl)
        json_count += 1
        
    except Exception as e:
        logging.error(f"Failed to store complex value {key}: {e}")

def is_simple_list(lst):
    """Check if list contains only scalar values"""
    return all(isinstance(item, (int, float, str, bool)) for item in lst)

def is_simple_map(dct):
    """Check if map contains only scalar values"""
    return all(isinstance(value, (int, float, str, bool)) for value in dct.values())

def migrate_to_redis_list(key, lst, ttl):
    """Store list as native Redis list"""
    try:
        valkey_client.delete(key)  # Clear any existing data
        if lst:  # Only create if list is not empty
            # Convert all items to strings for Redis list
            string_items = [str(item) for item in lst]
            valkey_client.rpush(key, *string_items)
        
        if ttl:
            valkey_client.expire(key, ttl)
        
        logging.debug(f"Stored as Redis list: {key}")
    except Exception as e:
        logging.error(f"Failed to store Redis list {key}: {e}")
        raise

def migrate_to_redis_hash(key, dct, ttl):
    """Store map as native Redis hash"""
    try:
        if dct:  # Only create if dict is not empty
            # Convert all values to strings for Redis hash
            string_map = {k: str(v) for k, v in dct.items()}
            valkey_client.hset(key, mapping=string_map)
        
        if ttl:
            valkey_client.expire(key, ttl)
        
        logging.debug(f"Stored as Redis hash: {key}")
    except Exception as e:
        logging.error(f"Failed to store Redis hash {key}: {e}")
        raise

def serialize_for_json(obj):
    """Convert objects to JSON-serializable format"""
    if isinstance(obj, (bytes, bytearray)):
        import base64
        return base64.b64encode(obj).decode('utf-8')
    elif isinstance(obj, dict):
        return {k: serialize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serialize_for_json(item) for item in obj]
    else:
        return obj

def migrate_to_json(key, value, ttl):
    """Store value as JSON with binary data handling"""
    try:
        # Serialize binary data before JSON conversion
        serializable_value = serialize_for_json(value)
        
        if has_json_support:
            # Use native JSON
            json_data = json.dumps(serializable_value)
            valkey_client.execute_command('JSON.SET', key, '$', json_data)
        else:
            # Fallback to JSON string
            valkey_client.set(key, json.dumps(serializable_value))
        
        if ttl:
            valkey_client.expire(key, ttl)
        
        logging.debug(f"Stored as JSON: {key}")
    except Exception as e:
        logging.error(f"Failed to store JSON {key}: {e}")
        raise

def migrate_geojson_to_native(key, geojson_value, ttl):
    """Migrate GeoJSON to native Valkey geospatial"""
    try:
        logging.info(f"Processing GeoJSON: key={key}, value={geojson_value}")
        
        # Extract coordinates for Point geometries
        if isinstance(geojson_value, dict) and geojson_value.get('type') == 'Point':
            coords = geojson_value.get('coordinates', [])
            logging.info(f"Found Point coordinates: {coords}")
            
            if len(coords) >= 2:
                lon, lat = coords[0], coords[1]
                geo_key = f"{key}_geo"
                member_name = key.split(':')[-1]
                
                logging.info(f"Creating geospatial entry: GEOADD {geo_key} {lon} {lat} {member_name}")
                
                # Use the correct GEOADD format without conflicting parameters
                result = valkey_client.geoadd(geo_key, (lon, lat, member_name))
                
                if ttl:
                    valkey_client.expire(geo_key, ttl)
                
                logging.info(f"Successfully created geospatial key: {geo_key}, result: {result}")
                return True
            else:
                logging.warning(f"Invalid coordinates length: {len(coords)}")
        else:
            logging.info(f"Not a Point geometry, type: {geojson_value.get('type', 'unknown')}")
        
        # For non-Point geometries, fall back to JSON
        logging.info("Falling back to JSON storage for non-Point geometry")
        return False
    except Exception as e:
        logging.error(f"Failed to store GeoJSON {key}: {e}")
        return False

def migrate_record(record, prefix):
    """Main record migration function - chooses between mapping engine and legacy"""
    
    # Use mapping engine if available and enabled
    if use_mapping_engine and mapping_engine:
        return migrate_record_with_mapping_engine(record, prefix)
    else:
        # Fall back to legacy improved mapping
        return migrate_record_improved(record, prefix)

def migrate_hyperloglog_to_native(key, hll_value, ttl):
    """Migrate HyperLogLog data to native Valkey HLL"""
    try:
        if isinstance(hll_value, list):
            # Add all items to HyperLogLog
            for item in hll_value:
                valkey_client.pfadd(key, str(item))
            
            if ttl:
                valkey_client.expire(key, ttl)
            return True
        
        return False
    except Exception as e:
        logging.error(f"Failed to store HyperLogLog {key}: {e}")
        return False
    global record_count, multi_key_records, single_key_records
    try:
        key, meta, bins = record
        record_id = extract_key(key)
        redis_key = f"{prefix}:{record_id}"
        ttl = meta.get('ttl')

        # Debug: Log all fields in records that might contain GeoJSON
        if any(field_name in ['geo', 'geo_val'] for field_name in bins.keys()):
            logging.info(f"Found record with potential GeoJSON fields: {record_id}")
            for field_name, field_value in bins.items():
                logging.info(f"  Field '{field_name}': type={type(field_value)}, value={field_value}")
                if field_name in ['geo', 'geo_val']:
                    logging.info(f"  Checking GeoJSON for field '{field_name}': {is_geojson(field_value)}")

        # Determine whether to use JSON or native types
        use_json = False
        if has_json_support and args.use_json:
            complex_nested_fields = count_complex_nested_fields(bins)
            use_json = complex_nested_fields >= args.json_threshold

        # Migrate using appropriate method
        success = False
        if use_json:
            success = migrate_record_json(redis_key, bins, ttl, record_id)
            if not success:
                # Fall back to native method if JSON fails
                logging.warning(f"Falling back to native migration for {redis_key}")
                success = migrate_record_native(redis_key, bins, ttl, record_id)
        else:
            success = migrate_record_native(redis_key, bins, ttl, record_id)

        if success:
            record_count += 1
            
            # Count records with multiple keys
            if record_id in record_to_keys_map and len(record_to_keys_map[record_id]) > 1:
                multi_key_records += 1
            else:
                single_key_records += 1
                
            if record_count % 100 == 0:
                logging.info(f"Migrated {record_count} records... (JSON: {json_count}, Standard: {standard_count}, Geospatial: {geospatial_count}, HyperLogLog: {hyperloglog_count})")

    except Exception as e:
        logging.error(f"Error processing record: {e}")

# Gather namespaces
namespaces = [args.namespace] if args.namespace else []
if not namespaces:
    ns_info = aero_client.info_all('namespaces')
    for _, val in ns_info.items():
        if isinstance(val, tuple): val = val[1]
        namespaces.extend(ns.strip() for ns in val.split(';') if ns.strip())

# Loop through all namespaces and sets
for ns in namespaces:
    sets = [args.set] if args.set else []
    if not sets:
        set_info = aero_client.info_all(f'sets/{ns}')
        for _, info in set_info.items():
            if isinstance(info, tuple): info = info[1]
            for entry in info.split(';'):
                if 'set=' in entry:
                    parts = dict(part.split('=') for part in entry.split(':') if '=' in part)
                    if 'set' in parts:
                        sets.append(parts['set'])

    for set_name in sets:
        prefix = args.key_prefix if args.key_prefix else f"{ns}.{set_name}"
        logging.info(f"Scanning {ns}.{set_name} with Redis key prefix '{prefix}'")
        
        try:
            # Use the partition filter syntax suggested
            if args.start_partition != 0 or args.end_partition != 4095:
                logging.info(f"Using partition filter: begin={args.start_partition}, count={args.end_partition - args.start_partition + 1}")
                
                policy = {
                    "timeout": 10000,
                    "partition_filter": {
                        "begin": args.start_partition,
                        "count": args.end_partition - args.start_partition + 1
                    }
                }
                
                # Callback function for partition filtering (receives 2 arguments)
                def partition_callback(partition_id, record):
                    # When using partition filter, callback gets partition_id and record separately
                    migrate_record(record, prefix)
                
                aero_client.scan(ns, set_name).foreach(partition_callback, policy)
            else:
                logging.info("Processing all records (no filtering)")
                policy = {"timeout": 10000}
                
                # Regular callback function (receives 1 argument)
                def regular_callback(record):
                    migrate_record(record, prefix)
                
                aero_client.scan(ns, set_name).foreach(regular_callback, policy)
                
        except Exception as e:
            logging.error(f"Error scanning {ns}.{set_name}: {e}")
            logging.error(f"Policy used: {policy}")
            import traceback
            traceback.print_exc()

logging.info(f"Completed: {record_count} records migrated.")
logging.info(f"  - {json_count} records using native JSON")
logging.info(f"  - {standard_count} records using standard Redis types")
logging.info(f"  - {geospatial_count} fields using native geospatial types")
logging.info(f"  - {hyperloglog_count} fields using native HyperLogLog types")

# Generate summary of record to key mapping
total_valkey_keys = sum(len(keys) for keys in record_to_keys_map.values())
logging.info(f"\nKey Mapping Summary:")
logging.info(f"  - {record_count} Aerospike records migrated to {total_valkey_keys} Valkey keys")
logging.info(f"  - {single_key_records} records mapped to a single Valkey key")
logging.info(f"  - {multi_key_records} records mapped to multiple Valkey keys")

# Show records with the most keys
if multi_key_records > 0:
    logging.info("\nRecords with multiple Valkey keys:")
    # Sort records by number of keys (descending)
    multi_key_examples = sorted(
        [(record_id, keys) for record_id, keys in record_to_keys_map.items() if len(keys) > 1],
        key=lambda x: len(x[1]),
        reverse=True
    )
    
    # Show up to 5 examples
    for record_id, keys in multi_key_examples[:5]:
        logging.info(f"  - Record {record_id}: mapped to {len(keys)} keys")
        for key in keys[:3]:  # Show first 3 keys
            logging.info(f"    - {key}")
        if len(keys) > 3:
            logging.info(f"    - ... and {len(keys) - 3} more")

# Calculate and log timing information
end_time = time.time()
end_datetime = datetime.now()
duration = end_time - start_time

logging.info("")
logging.info("=" * 50)
logging.info("MIGRATION TIMING SUMMARY")
logging.info("=" * 50)
logging.info(f"Start time: {start_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")  # Show milliseconds
logging.info(f"End time: {end_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")    # Show milliseconds

# Format duration with appropriate precision
if duration >= 1.0:
    logging.info(f"Total duration: {duration:.3f} seconds ({duration/60:.2f} minutes)")
elif duration >= 0.001:
    logging.info(f"Total duration: {duration*1000:.2f} milliseconds ({duration:.6f} seconds)")
else:
    logging.info(f"Total duration: {duration*1000000:.0f} microseconds ({duration:.6f} seconds)")

if record_count > 0:
    rate = record_count / duration
    logging.info(f"Migration rate: {rate:.2f} records/second")
    
    # Show partition info if using partition filtering
    if hasattr(args, 'start_partition') and (args.start_partition != 0 or args.end_partition != 4095):
        partition_count = args.end_partition - args.start_partition + 1
        logging.info(f"Partitions processed: {partition_count} (partitions {args.start_partition}-{args.end_partition})")

logging.info("=" * 50)

# Report safety-policy outcomes (threat T6)
if args.dry_run:
    logging.info(f"DRY-RUN: {valkey_client.skipped_dry_run} write operations were simulated and skipped.")
if not args.overwrite and valkey_client.skipped_existing:
    logging.info(f"Overwrite protection: {valkey_client.skipped_existing} writes to existing keys were skipped "
                 f"(re-run with --overwrite to replace).")

audit(
    "migration_complete",
    records_migrated=record_count,
    valkey_keys=total_valkey_keys,
    json_count=json_count,
    standard_count=standard_count,
    geospatial_count=geospatial_count,
    hyperloglog_count=hyperloglog_count,
    skipped_existing=getattr(valkey_client, 'skipped_existing', 0),
    skipped_dry_run=getattr(valkey_client, 'skipped_dry_run', 0),
    duration_seconds=round(duration, 6),
)

aero_client.close()
try:
    _real_valkey_client.close()
except Exception:
    pass
