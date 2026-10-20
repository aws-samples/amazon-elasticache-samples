#!/usr/bin/env python3
import argparse
import json
import sys
from redis.cluster import RedisCluster, ClusterNode
from redis import Redis
from redis.commands.json.path import Path

# Set up command line argument parsing
parser = argparse.ArgumentParser(
    description="Validate/dump migrated Valkey data. This tool can expose all "
                "migrated production data, so it requires explicit confirmation."
)
parser.add_argument('--valkey-host', required=True)
parser.add_argument('--valkey-port', type=int, default=6379)
parser.add_argument('--tls', action='store_true')
parser.add_argument('--tls-ca-cert',
                    help="Path to CA certificate bundle to verify the server certificate. "
                         "Required with --tls unless --allow-insecure-tls is set.")
parser.add_argument('--allow-insecure-tls', action='store_true',
                    help="DANGEROUS: disable TLS certificate validation (MITM risk).")
parser.add_argument('--allow-plaintext', action='store_true',
                    help="DANGEROUS: permit an unencrypted connection. Required to run "
                         "without --tls; this tool reads every migrated record, so "
                         "without TLS all of it crosses the network in the clear "
                         "(threat T1).")
# T7: dumping all keys/values is a full-disclosure operation. Require opt-in
# and prefer writing to a restricted file over printing to stdout.
parser.add_argument('--confirm-data-dump', action='store_true',
                    help="Required acknowledgement that this will read and reveal "
                         "potentially sensitive migrated data.")
parser.add_argument('--output-file',
                    help="Write dump output to this file (created with 0600 permissions) "
                         "instead of stdout.")
parser.add_argument('--keys-only', action='store_true',
                    help="Only list key names and types; do not read/print values.")
args = parser.parse_args()

if not args.confirm_data_dump:
    print("Refusing to run: this tool can disclose sensitive migrated data.",
          file=sys.stderr)
    print("Re-run with --confirm-data-dump to acknowledge, and consider "
          "--keys-only and/or --output-file <path>.", file=sys.stderr)
    raise SystemExit(2)

# Resolve TLS certificate-validation policy (threat T2: MITM).
if args.tls:
    if args.allow_insecure_tls:
        print("WARNING: TLS certificate validation disabled - connection is "
              "vulnerable to man-in-the-middle attacks.", file=sys.stderr)
        _ssl_kwargs = {"ssl": True, "ssl_cert_reqs": "none"}
    else:
        if not args.tls_ca_cert:
            print("Error: --tls requires --tls-ca-cert (or explicit "
                  "--allow-insecure-tls).", file=sys.stderr)
            raise SystemExit(2)
        _ssl_kwargs = {"ssl": True, "ssl_cert_reqs": "required",
                       "ssl_ca_certs": args.tls_ca_cert}
else:
    # T1: fail closed on plaintext. This tool reads back every migrated record,
    # so an unencrypted connection exposes the entire dataset on the wire.
    if not args.allow_plaintext:
        print("Refusing to run without TLS: this tool reads every migrated record, "
              "and an unencrypted connection would expose all of it on the network.",
              file=sys.stderr)
        print("Pass --tls --tls-ca-cert <path>, or --allow-plaintext to explicitly "
              "accept unencrypted transport (never for production data).",
              file=sys.stderr)
        raise SystemExit(2)
    print("WARNING: connecting WITHOUT TLS - all data read by this tool crosses "
          "the network in plaintext.", file=sys.stderr)
    _ssl_kwargs = {"ssl": False}

# Route output either to a restricted file or stdout.
import os
if args.output_file:
    _fd = os.open(args.output_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    _out = os.fdopen(_fd, 'w')
else:
    _out = sys.stdout


def emit(*a, **kw):
    kw.setdefault('file', _out)
    print(*a, **kw)


# Create a connection to the Redis cluster
client = RedisCluster(
    startup_nodes=[ClusterNode(args.valkey_host, args.valkey_port)],
    decode_responses=True,  # Tell Redis to return strings instead of bytes
    **_ssl_kwargs
)

emit("Scanning all nodes in Valkey cluster...\n")

# Initialize counters and tracking sets
visited_keys = set()       # Keep track of keys we've already seen
record_count = 0          # Count total records processed
node_count = 0            # Count how many nodes we've scanned
data_types = {}           # Track count of each data type

try:
    # Get list of primary nodes (ignore replica nodes)
    nodes = [node for node in client.get_nodes() if node.server_type == "primary"]
    emit(f"Found {len(nodes)} primary nodes")
    
    # Loop through each primary node in the cluster
    for node in nodes:
        node_count += 1
        emit(f"\nScanning node {node_count}/{len(nodes)}: {node.host}:{node.port}")
        
        # Create a direct connection to this specific node
        node_client = Redis(
            host=node.host,
            port=node.port,
            decode_responses=True,
            **_ssl_kwargs
        )

        cursor = "0"           # Starting point for scanning
        node_key_count = 0     # Count keys found in this node
        
        try:
            # Keep scanning until we've seen all keys in this node
            while cursor != 0:
                # Get a batch of keys from Redis
                cursor, keys = node_client.scan(cursor=cursor, count=100)
                node_key_count += len(keys)
                
                # Process each key we found
                for key in keys:
                    try:
                        key_str = str(key)
                    except Exception as e:
                        emit(f"Error decoding key: {key} — {e}")
                        continue
                    
                    # Skip keys we've already processed
                    if key_str in visited_keys:
                        continue
                    
                    # Add to our tracking and increment counter
                    visited_keys.add(key_str)
                    record_count += 1
                    emit(f"Record #{record_count}: {key_str}")

                    try:
                        # Find out what type of data this key holds
                        key_type = client.type(key)
                        
                        # Special check for HyperLogLog - Redis doesn't have a specific type for HLL
                        # HyperLogLog is implemented as a string type with special encoding
                        if key_type == 'string':
                            try:
                                # Try to run PFCOUNT - if it succeeds, it's a HyperLogLog
                                client.execute_command('PFCOUNT', key)
                                key_type = 'hyperloglog'
                            except Exception:
                                # Not a HyperLogLog, keep as string
                                pass
                                
                        emit(f"{key} ({key_type})")
                        
                        # Track data type counts
                        if key_type not in data_types:
                            data_types[key_type] = 0
                        data_types[key_type] += 1

                        # In keys-only mode, never read or print the actual values.
                        if args.keys_only:
                            continue

                        # Handle different types of Redis data structures
                        if key_type == 'string':
                            try:
                                # Get the string value
                                raw = client.get(key)
                                # First try to parse as JSON
                                try:
                                    parsed_json = json.loads(raw)
                                    value = json.dumps(parsed_json, indent=2)
                                except (json.JSONDecodeError, TypeError):
                                    # If not JSON, handle as regular string
                                    value = str(raw)
                            except Exception as e:
                                value = f"[GET failed: {e}]"

                        elif key_type == 'hash':
                            try:
                                # Get all fields and values from the hash
                                raw = client.hgetall(key)
                                # Try to parse each value as JSON
                                value = {}
                                for k, v in raw.items():
                                    try:
                                        value[k] = json.loads(v)
                                    except (json.JSONDecodeError, TypeError):
                                        value[k] = str(v)
                                value = json.dumps(value, indent=2)
                            except Exception as e:
                                value = f"[HGETALL failed: {e}]"

                        elif key_type == 'list':
                            try:
                                # Get all elements in the list
                                raw = client.lrange(key, 0, -1)
                                # Try to parse each element as JSON
                                value = []
                                for item in raw:
                                    try:
                                        value.append(json.loads(item))
                                    except (json.JSONDecodeError, TypeError):
                                        value.append(str(item))
                                value = json.dumps(value, indent=2)
                            except Exception as e:
                                value = f"[LRANGE failed: {e}]"

                        elif key_type == 'set':
                            try:
                                # Get all members of the set
                                raw = client.smembers(key)
                                # Try to parse each member as JSON
                                value = []
                                for item in raw:
                                    try:
                                        value.append(json.loads(item))
                                    except (json.JSONDecodeError, TypeError):
                                        value.append(str(item))
                                value = json.dumps(value, indent=2)
                            except Exception as e:
                                value = f"[SMEMBERS failed: {e}]"

                        elif key_type == 'zset':
                            try:
                                # First check if this might be a geospatial index
                                is_geo = False
                                try:
                                    # Try a GEOPOS command on the first member
                                    first_member = client.zrange(key, 0, 0)
                                    if first_member:
                                        geo_pos = client.geopos(key, first_member[0])
                                        if geo_pos and geo_pos[0]:
                                            is_geo = True
                                except Exception:
                                    pass
                                    
                                if is_geo:
                                    # Handle as geospatial data
                                    members = client.zrange(key, 0, -1)
                                    geo_data = []
                                    for member in members:
                                        pos = client.geopos(key, member)
                                        if pos and pos[0]:
                                            lon, lat = pos[0]
                                            geo_data.append({
                                                "member": member,
                                                "type": "Point",
                                                "coordinates": [lon, lat]
                                            })
                                    value = json.dumps(geo_data, indent=2)
                                    emit(f"   (Geospatial data with {len(geo_data)} points)")
                                else:
                                    # Handle as regular zset
                                    raw = client.zrange(key, 0, -1, withscores=True)
                                    value = []
                                    for member, score in raw:
                                        try:
                                            parsed_member = json.loads(member)
                                        except (json.JSONDecodeError, TypeError):
                                            parsed_member = str(member)
                                        value.append({
                                            'member': parsed_member,
                                            'score': score
                                        })
                                    value = json.dumps(value, indent=2)
                            except Exception as e:
                                value = f"[ZRANGE failed: {e}]"

                        elif key_type == 'hyperloglog':
                            try:
                                # Get the cardinality estimate for the HyperLogLog
                                cardinality = client.execute_command('PFCOUNT', key)
                                value = f"HyperLogLog with estimated cardinality: {cardinality}"
                            except Exception as e:
                                value = f"[PFCOUNT failed: {e}]"
                                
                        elif key_type == 'stream':
                            try:
                                # Get all entries from the stream
                                raw = client.xrange(key, '-', '+')
                                value = []
                                for entry_id, fields in raw:
                                    parsed_fields = {}
                                    for k, v in fields.items():
                                        k_str = k.decode() if isinstance(k, bytes) else str(k)
                                        v_str = v.decode() if isinstance(v, bytes) else str(v)
                                        try:
                                            parsed_fields[k_str] = json.loads(v_str)
                                        except (json.JSONDecodeError, TypeError):
                                            parsed_fields[k_str] = v_str
                                    value.append({
                                        'id': entry_id,
                                        'fields': parsed_fields
                                    })
                                value = json.dumps(value, indent=2)
                            except Exception as e:
                                value = f"[XRANGE failed: {e}]"

                        elif key_type in ['RedisJSON', 'JSON', 'ReJSON-RL']:  # Include ReJSON-RL type
                            try:
                                # Try multiple approaches to get JSON data
                                json_value = None
                                error_msg = None
                                
                                # Approach 1: Using json() method if available
                                try:
                                    if hasattr(client, 'json'):
                                        json_value = client.json().get(key)
                                except Exception as e:
                                    error_msg = f"json() method failed: {e}"
                                
                                # Approach 2: Using execute_command with JSON.GET
                                if json_value is None:
                                    try:
                                        raw = client.execute_command('JSON.GET', key)
                                        if isinstance(raw, bytes):
                                            raw = raw.decode('utf-8')
                                        json_value = json.loads(raw)
                                    except Exception as e:
                                        if error_msg:
                                            error_msg += f"; JSON.GET command failed: {e}"
                                        else:
                                            error_msg = f"JSON.GET command failed: {e}"
                                
                                # Approach 3: Fallback to DUMP command
                                if json_value is None:
                                    try:
                                        client.execute_command('DUMP', key)
                                        json_value = {"_note": "JSON data exists but requires RedisJSON commands to view properly"}
                                    except Exception as e:
                                        if error_msg:
                                            error_msg += f"; DUMP command failed: {e}"
                                        else:
                                            error_msg = f"DUMP command failed: {e}"
                                
                                # Format the result
                                if json_value is not None:
                                    value = json.dumps(json_value, indent=2)
                                else:
                                    value = f"[JSON access failed: {error_msg}]"
                                    
                            except Exception as e:
                                value = f"[JSON processing failed: {e}]"

                        else:
                            value = f"[Unsupported type: {key_type}]"

                        emit(f"   {value}\n")

                    except Exception as e:
                        emit(f"Failed to process key {key}: {e}")

            emit(f"Completed scanning node {node.host}:{node.port}. Found {node_key_count} keys")
            # Clean up the connection to this node
            node_client.close()

        except Exception as e:
            emit(f"Error scanning node {node.host}:{node.port}: {e}")

    # Print final summary
    emit(f"\nScan complete!")
    emit(f"Total nodes scanned: {node_count}")
    emit(f"Total unique records processed: {record_count}")
    emit(f"Total unique keys found: {len(visited_keys)}")
    
    # Print data type summary
    emit("\nData Type Summary:")
    for data_type, count in sorted(data_types.items(), key=lambda x: x[1], reverse=True):
        emit(f"  - {data_type}: {count} keys")

except Exception as e:
    emit(f"Fatal error occurred: {e}")
finally:
    # Close the output file if we opened one.
    if args.output_file:
        try:
            _out.close()
        except Exception:
            pass
    # Always close the main cluster connection when done
    client.close()
