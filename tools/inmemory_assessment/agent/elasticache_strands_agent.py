import json
import os
from strands import Agent, tool

@tool
def get_latest_valkey_version(region: str = None) -> dict:
    """Get the latest available Valkey version from AWS ElastiCache."""
    import boto3
    
    try:
        region = region or os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
        elasticache = boto3.client('elasticache', region_name=region)
        
        response = elasticache.describe_cache_engine_versions(
            Engine='valkey',
            DefaultOnly=False
        )
        
        versions = []
        for version_info in response['CacheEngineVersions']:
            version = version_info['EngineVersion']
            versions.append({
                'version': version,
                'description': version_info.get('CacheEngineDescription', ''),
                'parameter_group_family': version_info.get('CacheParameterGroupFamily', '')
            })
        
        # Sort by version (descending) to get latest first
        versions.sort(key=lambda x: [int(n) for n in x['version'].split('.')], reverse=True)
        
        return {
            'latest_version': versions[0]['version'] if versions else 'Unknown',
            'all_versions': [v['version'] for v in versions],
            'latest_details': versions[0] if versions else {}
        }
    except Exception as e:
        return {'error': f'Failed to fetch Valkey versions: {str(e)}'}

@tool
def parse_migration_assessment(file_path: str) -> dict:
    """Parse migration assessment JSON file and extract key metrics."""
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {'error': f'File not found: {file_path}'}
    except json.JSONDecodeError:
        return {'error': f'Invalid JSON in: {file_path}'}
    
    cluster = data.get('cluster', {})
    if not cluster:
        return {'error': 'No "cluster" key found in assessment'}
    
    result = {
        'memory_gb': cluster.get('summary_metric_memory_gb'),
        'total_ops_sec': cluster.get('summary_metric_total_ops_sec'),
        'write_ops_sec': cluster.get('summary_metric_total_write_ops_sec'),
        'read_ops_sec': cluster.get('summary_metric_total_read_ops_sec'),
        'avg_bytes_per_op': cluster.get('summary_metric_avg_bytes_per_operation'),
        'total_bandwidth_gbps': cluster.get('summary_metric_total_cluster_bandwidth_gbps'),
        'estimated_ecpus_sec': cluster.get('summary_metric_estimated_ecpus_per_sec'),
        'ecpu_complexity_factor': cluster.get('summary_metric_ecpu_complexity_factor', 1.0),
        'ecpu_estimation_method': cluster.get('summary_metric_ecpu_estimation_method', 'unknown'),
        'cluster_mode': cluster.get('cluster_mode'),
        'primaries': cluster.get('primaries'),
        'replicas': cluster.get('replicas'),
        'eviction_policy': cluster.get('eviction_policy_0')
    }
    
    # Parse source engine and version
    # engines_0 is the Redis compatibility version, engines_1 may be the actual Valkey version
    engine_str = cluster.get('engines_0', '')
    engine_str_1 = cluster.get('engines_1', '')
    if 'valkey' in engine_str_1.lower():
        engine_str = engine_str_1
    parts = engine_str.split(' ', 1)
    result['source_engine'] = parts[0] if parts else 'Unknown'
    result['source_version'] = parts[1] if len(parts) > 1 else 'Unknown'
    
    # Flag any missing critical fields
    missing = [k for k in ['memory_gb', 'total_ops_sec'] if result[k] is None]
    if missing:
        result['warning'] = f'Missing critical fields: {", ".join(missing)}'

    # --- Observed command inventory -------------------------------------------------
    # The assessment records per-command call counts from INFO commandstats as
    # "__commandstats_snapshot_cmdstat_<cmd>_calls" keys on each node. Extract the
    # distinct commands the workload actually executed so compatibility analysis is
    # grounded in real usage instead of guesswork.
    observed = {}
    for node_data in data.get('nodes', {}).values():
        if not isinstance(node_data, dict):
            continue
        for key, value in node_data.items():
            if not key.startswith('__commandstats_snapshot_cmdstat_'):
                continue
            if not key.endswith('_calls'):
                continue
            # The JSON flattener emits several *_calls variants per command:
            #   ..._cmdstat_get_calls           <- what we want
            #   ..._cmdstat_get_failed_calls    <- would yield a bogus "get_failed"
            #   ..._cmdstat_get_rejected_calls  <- would yield a bogus "get_rejected"
            if key.endswith('_failed_calls') or key.endswith('_rejected_calls'):
                continue
            cmd = key[len('__commandstats_snapshot_cmdstat_'):-len('_calls')]
            if not cmd:
                continue
            try:
                calls = int(value)
            except (TypeError, ValueError):
                continue
            if calls <= 0:
                continue
            observed[cmd] = observed.get(cmd, 0) + calls

    # Commands ElastiCache restricts on ALL cache types (node-based and serverless),
    # because they require privileges a managed service does not expose.
    # Source: https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/SupportedCommands.html
    RESTRICTED_ALL = frozenset([
        'acl setuser', 'acl load', 'acl save', 'acl deluser',
        'bgrewriteaof', 'bgsave', 'save',
        'cluster addslot', 'cluster addslotsrange', 'cluster bumpepoch',
        'cluster delslot', 'cluster delslotsrange', 'cluster failover',
        'cluster flushslots', 'cluster forget', 'cluster links', 'cluster meet',
        'cluster setslot',
        'config', 'debug', 'migrate', 'psync', 'replicaof', 'slaveof',
        'shutdown', 'sync',
    ])

    # ADDITIONALLY restricted on serverless caches only. Presence of any of these
    # is a concrete reason a workload cannot move to Serverless as-is.
    RESTRICTED_SERVERLESS_ONLY = frozenset([
        'acl log',
        'client caching', 'client getredir', 'client id', 'client info',
        'client kill', 'client list', 'client no-evict', 'client pause',
        'client tracking', 'client trackinginfo', 'client unblock', 'client unpause',
        'cluster count-failure-reports',
        'commandlog', 'commandlog get', 'commandlog help', 'commandlog len',
        'commandlog reset',
        'fcall', 'fcall_ro',
        'function', 'function delete', 'function dump', 'function flush',
        'function help', 'function kill', 'function list', 'function load',
        'function restore', 'function stats',
        'keys', 'lastsave',
        'latency', 'latency doctor', 'latency graph', 'latency help',
        'latency histogram', 'latency history', 'latency latest', 'latency reset',
        'memory', 'memory doctor', 'memory help', 'memory malloc-stats',
        'memory purge', 'memory stats', 'memory usage',
        'monitor', 'move',
        'object', 'object encoding', 'object freq', 'object help',
        'object idletime', 'object refcount',
        'pfdebug', 'pfselftest',
        'psubscribe', 'pubsub numpat', 'punsubscribe',
        'script kill',
        'slowlog', 'slowlog get', 'slowlog help', 'slowlog len', 'slowlog reset',
        'swapdb', 'wait',
    ])

    # Commands issued by THIS assessment tool itself while collecting metrics.
    # They will appear in commandstats even when the customer application never
    # uses them, so they must not be reported as application-level blockers.
    ASSESSMENT_TOOL_COMMANDS = frozenset([
        'config', 'info', 'client', 'cluster', 'memory', 'object', 'slowlog',
        'command', 'ping', 'auth', 'hello', 'select', 'replconf',
    ])

    all_observed = sorted(observed.keys())

    # commandstats separates container/subcommand with "|" (e.g. "object|encoding",
    # "client|list"); the ElastiCache docs use a space. Normalise for comparison.
    def _norm(cmd):
        return cmd.replace('|', ' ').lower()

    def _container(cmd):
        return _norm(cmd).split(' ', 1)[0]

    def _match(cmd, restricted_set):
        n = _norm(cmd)
        # Match the exact command, or its container if the container itself is
        # restricted wholesale (docs list bare "config", "memory", "object", etc.)
        return n in restricted_set or _container(cmd) in restricted_set

    restricted_all_hits = sorted(
        c for c in all_observed if _match(c, RESTRICTED_ALL)
    )
    restricted_serverless_hits = sorted(
        c for c in all_observed if _match(c, RESTRICTED_SERVERLESS_ONLY)
    )

    # Split out hits that are attributable to the assessment tool's own probing.
    def _split_tool_noise(hits):
        app, tool = [], []
        for c in hits:
            (tool if _container(c) in ASSESSMENT_TOOL_COMMANDS else app).append(c)
        return app, tool

    app_restricted_all, tool_restricted_all = _split_tool_noise(restricted_all_hits)
    app_restricted_sl, tool_restricted_sl = _split_tool_noise(restricted_serverless_hits)

    # Module / search command namespaces. Matched by prefix rather than an
    # exhaustive command list so newly added commands are still detected.
    MODULE_PREFIXES = {'json.': 'JSON', 'bf.': 'Bloom filter', 'ft.': 'Search (FT)'}
    module_hits = {}
    for c in all_observed:
        n = _norm(c)
        for prefix, label in MODULE_PREFIXES.items():
            if n.startswith(prefix):
                module_hits.setdefault(label, []).append(c)

    # Minimum Valkey version that introduced each command, for commands added AFTER
    # Valkey 7.2 (the oldest engine version ElastiCache for Valkey offers). Commands
    # not listed here predate 7.2 and are therefore available on every ElastiCache
    # for Valkey version.
    #
    # Generated from the authoritative "since" field in the Valkey command
    # definitions (valkey-io/valkey, src/commands/*.json, all 426 commands scanned).
    # Sentinel commands are excluded as they do not apply to ElastiCache.
    # Regenerate when Valkey publishes a new release.
    COMMAND_MIN_VALKEY_VERSION = {
        # --- Valkey 8.0.0 ---
        'client capa': '8.0.0',
        'cluster slot-stats': '8.0.0',
        'script show': '8.0.0',
        # --- Valkey 8.1.0 ---
        'client import-source': '8.1.0',
        'commandlog': '8.1.0',
        'commandlog get': '8.1.0',
        'commandlog help': '8.1.0',
        'commandlog len': '8.1.0',
        'commandlog reset': '8.1.0',
        # --- Valkey 9.0.0 ---
        'cluster cancelslotmigrations': '9.0.0',
        'cluster flushslot': '9.0.0',
        'cluster getslotmigrations': '9.0.0',
        'cluster migrateslots': '9.0.0',
        'cluster syncslots': '9.0.0',
        'delifeq': '9.0.0',
        'hexpire': '9.0.0',
        'hexpireat': '9.0.0',
        'hexpiretime': '9.0.0',
        'hgetex': '9.0.0',
        'hpersist': '9.0.0',
        'hpexpire': '9.0.0',
        'hpexpireat': '9.0.0',
        'hpexpiretime': '9.0.0',
        'hpttl': '9.0.0',
        'hsetex': '9.0.0',
        'httl': '9.0.0',
        # --- Valkey 9.1.0 ---
        'clusterscan': '9.1.0',
        'hgetdel': '9.1.0',
        'msetex': '9.1.0',
        # --- Valkey 9.2.0 (not yet offered by ElastiCache) ---
        'config info': '9.2.0',
    }

    version_requirements = {}
    for c in all_observed:
        n = _norm(c)
        since = COMMAND_MIN_VALKEY_VERSION.get(n)
        if since is None:
            # Try the container form (e.g. "commandlog|get" -> "commandlog")
            since = COMMAND_MIN_VALKEY_VERSION.get(_container(c))
        if since:
            version_requirements[c] = since

    min_required = None
    if version_requirements:
        def _vt(s):
            try:
                return tuple(int(x) for x in str(s).split('.')[:3])
            except Exception:
                return (0,)
        min_required = max(version_requirements.values(), key=_vt)

    result['observed_commands'] = all_observed
    result['observed_command_count'] = len(all_observed)
    result['observed_command_calls'] = {
        c: observed[c] for c in sorted(observed, key=observed.get, reverse=True)
    }
    result['restricted_on_all_elasticache'] = app_restricted_all
    result['restricted_on_serverless_only'] = app_restricted_sl
    result['restricted_hits_from_assessment_tool'] = sorted(
        set(tool_restricted_all) | set(tool_restricted_sl)
    )
    result['module_commands_detected'] = module_hits
    result['command_min_valkey_version'] = version_requirements
    result['min_valkey_version_required'] = min_required
    result['version_requirement_note'] = (
        'command_min_valkey_version maps each OBSERVED command to the Valkey version that '
        'introduced it, for commands added after Valkey 7.2 (the oldest version ElastiCache '
        'for Valkey offers). Commands absent from this map predate 7.2 and run on any '
        'ElastiCache for Valkey version. min_valkey_version_required is the highest such '
        'requirement across the observed workload — the target engine version must be at '
        'least this. Cross-check it against the versions get_latest_valkey_version reports '
        'as actually available on ElastiCache: if a command requires a version ElastiCache '
        'does not yet offer, that is a hard blocker and must be called out. Source: the '
        '"since" field in the Valkey command definitions (valkey-io/valkey).'
    )
    result['command_inventory_note'] = (
        'Commands OBSERVED during the assessment measurement window only, from INFO '
        'commandstats. This is NOT necessarily the complete set the application uses — '
        'infrequent paths (nightly batch jobs, failover/admin scripts, error handlers) '
        'may not have executed during the window. Treat absence as "not observed", not '
        '"not used". '
        'restricted_on_all_elasticache and restricted_on_serverless_only are matched '
        'against the ElastiCache restricted-command lists at '
        'https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/SupportedCommands.html. '
        'restricted_hits_from_assessment_tool lists restricted commands that this '
        'assessment tool itself issues while collecting metrics (CONFIG, INFO, CLIENT, '
        'CLUSTER, MEMORY, OBJECT, SLOWLOG) — these are usually measurement artifacts, '
        'NOT application usage, and should not be reported as blockers unless the '
        'customer confirms their application uses them.'
    )
    
    return result

@tool
def get_elasticache_instance_types(region: str = None) -> dict:
    """Get available ElastiCache instance types with specs from AWS Pricing API."""
    import boto3
    import json
    import re
    
    try:
        region = region or os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
        try:
            location = get_region_location_name(region)
        except Exception:
            location = 'US East (N. Virginia)'
        
        pricing = get_pricing_client()
        
        # Fetch all results with pagination
        all_price_items = []
        next_token = None
        
        while True:
            params = {
                'ServiceCode': 'AmazonElastiCache',
                'Filters': [
                    {'Type': 'TERM_MATCH', 'Field': 'cacheEngine', 'Value': 'Valkey'},
                    {'Type': 'TERM_MATCH', 'Field': 'locationType', 'Value': 'AWS Region'},
                    {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': location}
                ],
                'MaxResults': 100
            }
            
            if next_token:
                params['NextToken'] = next_token
            
            response = pricing.get_products(**params)
            all_price_items.extend(response['PriceList'])
            
            if 'NextToken' not in response:
                break
            next_token = response['NextToken']
        
        instance_types = {}
        for price_item in all_price_items:
            product = json.loads(price_item)
            attrs = product['product']['attributes']
            
            instance_type = attrs.get('instanceType')
            if not instance_type or not instance_type.startswith('cache.'):
                continue
            
            # Skip if this SKU doesn't have memory/vcpu (extended support, etc.)
            if 'memory' not in attrs or 'vcpu' not in attrs:
                continue
            
            # Skip if we already have this instance type
            if instance_type in instance_types:
                continue
            
            # Parse memory (e.g., "3.09 GiB" -> 3.09)
            memory_str = attrs.get('memory', '0 GiB')
            try:
                memory_gb = float(memory_str.split()[0]) if memory_str else 0
            except (ValueError, IndexError):
                memory_gb = 0.0
            
            # Parse vCPU (handle "Variable" and string numbers)
            vcpu_str = attrs.get('vcpu', '0')
            try:
                vcpu = int(vcpu_str) if vcpu_str and vcpu_str != 'Variable' else 0
            except ValueError:
                vcpu = 0
            
            # Parse network performance (approximate to Gbps)
            network_perf = attrs.get('networkPerformance', 'Low')
            network_gbps = 0.5  # default
            
            # Extract number from network performance string
            if 'Gigabit' in network_perf:
                match = re.search(r'(\d+\.?\d*)\s*Gigabit', network_perf)
                if match:
                    network_gbps = float(match.group(1))
            elif 'High' in network_perf:
                network_gbps = 1.0
            elif 'Moderate' in network_perf:
                network_gbps = 0.75
            
            # Get instance family for categorization
            instance_family = attrs.get('instanceFamily', 'Unknown')
            
            # Check if current generation
            is_current_gen = attrs.get('currentGeneration', 'No') == 'Yes'
            
            # Skip previous generation instances
            if not is_current_gen:
                continue
            
            instance_types[instance_type] = {
                'vcpu': vcpu,
                'memory_gb': memory_gb,
                'network_gbps': network_gbps,
                'network_performance': network_perf,
                'instance_family': instance_family,
                'current_generation': is_current_gen
            }
        
        # Sort by instance family, then by generation (newer first), then by name
        family_order = {
            'Standard': 1,
            'Memory optimized': 2,
            'Network optimized': 3,
            'Compute optimized': 4,
            'Micro': 5,
            'Unknown': 6
        }
        
        def get_generation(instance_name):
            """Extract generation number from instance name (e.g., r7g -> 7, m6g -> 6)"""
            import re
            match = re.search(r'cache\.([a-z])(\d+)', instance_name)
            if match:
                return int(match.group(2))
            return 0
        
        return dict(sorted(
            instance_types.items(),
            key=lambda x: (
                family_order.get(x[1]['instance_family'], 6),
                -get_generation(x[0]),
                x[0]
            )
        ))
    except Exception as e:
        return {'error': f'Failed to fetch instance types: {str(e)}'}

@tool
def calculate_shard_recommendation(memory_gb: float, ops_sec: float, bandwidth_gbps: float, write_ops_sec: float = 0, instance_types_data: dict = None, ecpu_complexity_factor: float = 1.0) -> dict:
    """Calculate recommended number of shards and suggest instance sizes for optimal balance.
    
    Finds the sweet spot between horizontal (more shards) and vertical (larger instances) scaling.
    Uses actual instance data from get_elasticache_instance_types if provided.
    
    Args:
        ecpu_complexity_factor: Workload complexity from assessment JSON (1.0 = all simple GET/SET,
            higher = heavier commands like EVAL/SORT). Adjusts the 100K ops/vCPU baseline down.
    
    Returns write ratio information so the model can assess if BGSAVE overhead (1.3x-2x) is needed.
    """
    # Calculate write ratio
    write_ratio = (write_ops_sec / ops_sec * 100) if ops_sec > 0 else 0
    
    # If instance data provided, use it; otherwise use fallback values
    if instance_types_data:
        import re as _re
        # Extract memory-optimized instances (r-family, any generation).
        # When multiple generations expose the same size (e.g. cache.r7g.2xlarge
        # AND cache.r8g.2xlarge), keep the NEWEST generation for that size so we
        # never recommend an older generation when a newer one is available.
        by_size = {}
        for instance_name, specs in instance_types_data.items():
            # Match cache.rXg.* pattern (r7g, r8g, r9g, etc.)
            if instance_name.startswith('cache.r') and 'g.' in instance_name:
                size = instance_name.split('.')[-1]
                if size not in ['xlarge', '2xlarge', '4xlarge', '8xlarge', '12xlarge', '16xlarge']:
                    continue
                gen_match = _re.search(r'cache\.[a-z](\d+)', instance_name)
                generation = int(gen_match.group(1)) if gen_match else 0
                existing = by_size.get(size)
                if existing is None or generation > existing['generation']:
                    by_size[size] = {
                        'instance_type': instance_name,
                        'size': size,
                        'generation': generation,
                        'memory_gb': specs['memory_gb'],
                        'vcpus': specs['vcpu'],
                        'network_gbps': specs['network_gbps']
                    }
        instance_options = list(by_size.values())
    else:
        # Fallback: r7g family values (for when API data not available)
        instance_options = [
            {'instance_type': 'cache.r7g.xlarge',   'size': 'xlarge',   'generation': 7, 'memory_gb': 26,  'vcpus': 4,  'network_gbps': 10},
            {'instance_type': 'cache.r7g.2xlarge',  'size': '2xlarge',  'generation': 7, 'memory_gb': 52,  'vcpus': 8,  'network_gbps': 15},
            {'instance_type': 'cache.r7g.4xlarge',  'size': '4xlarge',  'generation': 7, 'memory_gb': 105, 'vcpus': 16, 'network_gbps': 15},
            {'instance_type': 'cache.r7g.8xlarge',  'size': '8xlarge',  'generation': 7, 'memory_gb': 209, 'vcpus': 32, 'network_gbps': 15},
            {'instance_type': 'cache.r7g.12xlarge', 'size': '12xlarge', 'generation': 7, 'memory_gb': 314, 'vcpus': 48, 'network_gbps': 20},
            {'instance_type': 'cache.r7g.16xlarge', 'size': '16xlarge', 'generation': 7, 'memory_gb': 419, 'vcpus': 64, 'network_gbps': 25},
        ]
    
    recommendations = []
    for instance in instance_options:
        # Calculate shards needed based on raw memory (model will decide on overhead)
        shards_needed = max(1, int(memory_gb / instance['memory_gb']) + 
                           (1 if memory_gb % instance['memory_gb'] > 0 else 0))
        
        # Calculate capacity per shard
        ops_per_shard = ops_sec / shards_needed
        bandwidth_per_shard = bandwidth_gbps / shards_needed
        
        # Estimate RPS capacity adjusted for command complexity
        # Base: ~100K per vCPU for simple GET/SET. Divided by complexity factor for heavier workloads.
        effective_ops_per_vcpu = int(100000 / max(1.0, ecpu_complexity_factor))
        estimated_rps_capacity = instance['vcpus'] * effective_ops_per_vcpu
        
        # Check if this configuration can handle the workload
        can_handle_ops = ops_per_shard <= estimated_rps_capacity
        can_handle_bandwidth = bandwidth_per_shard <= instance['network_gbps']
        
        recommendations.append({
            'instance_type': instance.get('instance_type', f"cache.r7g.{instance['size']}"),
            'instance_size': instance['size'],
            'generation': instance.get('generation', 0),
            'shards': shards_needed,
            'memory_per_shard_gb': memory_gb / shards_needed,
            'ops_per_shard': ops_per_shard,
            'bandwidth_per_shard_gbps': bandwidth_per_shard,
            'estimated_rps_capacity': estimated_rps_capacity,
            'can_handle_ops': can_handle_ops,
            'can_handle_bandwidth': can_handle_bandwidth,
            'vcpus': instance['vcpus'],
            'total_nodes': shards_needed * 3  # primary + 2 replicas
        })
    
    # Find viable options
    viable_options = [r for r in recommendations if r['can_handle_ops'] and r['can_handle_bandwidth']]
    
    # Prefer 2xlarge to 4xlarge range for good balance
    preferred = next((r for r in viable_options if r['instance_size'] in ['2xlarge', '4xlarge']), 
                     viable_options[0] if viable_options else recommendations[0])
    
    return {
        'recommended_instance_type': preferred.get('instance_type', f"cache.r7g.{preferred['instance_size']}"),
        'recommended_instance_size': preferred['instance_size'],
        'recommended_generation': preferred.get('generation', 0),
        'recommended_shards': preferred['shards'],
        'memory_per_shard_gb': preferred['memory_per_shard_gb'],
        'ops_per_shard': preferred['ops_per_shard'],
        'bandwidth_per_shard_gbps': preferred['bandwidth_per_shard_gbps'],
        'estimated_rps_capacity_per_shard': preferred['estimated_rps_capacity'],
        'total_nodes': preferred['total_nodes'],
        'write_ratio_percent': write_ratio,
        'write_ops_sec': write_ops_sec,
        'ecpu_complexity_factor': ecpu_complexity_factor,
        'effective_ops_per_vcpu': effective_ops_per_vcpu,
        'all_options': recommendations,
        'note': 'Recommendation balances horizontal scaling (more shards) with vertical scaling (larger instances). Memory sizing does NOT include BGSAVE overhead - model should assess based on write pattern: <10% writes = minimal overhead (1.1-1.2x), 10-50% = moderate (1.3x), >60% with frequent rewrites = write-heavy (1.5-2x).'
    }

@tool
def validate_recommendation(memory_needed_gb: float, instance_memory_gb: float, num_shards: int, replicas_per_shard: int, total_ops_sec: float, estimated_rps_capacity: float) -> dict:
    """Validate that a proposed ElastiCache configuration actually covers the workload requirements.
    
    Call this after generating recommendations to verify the math is correct.
    """
    issues = []

    # Memory check
    total_memory = instance_memory_gb * num_shards
    if total_memory < memory_needed_gb:
        issues.append(f"CRITICAL: Total memory {total_memory:.1f} GB < {memory_needed_gb:.1f} GB needed")

    utilization = (memory_needed_gb / total_memory * 100) if total_memory > 0 else 100
    if utilization > 75:
        issues.append(f"Memory utilization {utilization:.0f}% exceeds 75% safe threshold")

    # Ops check
    total_rps = estimated_rps_capacity * num_shards
    if total_rps < total_ops_sec:
        issues.append(f"CRITICAL: RPS capacity {total_rps:.0f} < {total_ops_sec:.0f} ops/sec needed")

    # Node count
    total_nodes = num_shards * (1 + replicas_per_shard)

    # HA check
    if replicas_per_shard < 1:
        issues.append("No replicas — no failover capability")
    if replicas_per_shard >= 1 and num_shards == 1 and replicas_per_shard < 2:
        issues.append("Single shard with 1 replica — consider 2 replicas for stronger HA")

    return {
        'valid': len(issues) == 0,
        'total_memory_gb': total_memory,
        'memory_utilization_pct': round(utilization, 1),
        'total_rps_capacity': total_rps,
        'total_nodes': total_nodes,
        'issues': issues
    }

def get_pricing_client():
    """Get Pricing API client with fallback from us-east-1 to ap-south-1."""
    import boto3
    for region in ['us-east-1', 'ap-south-1']:
        try:
            client = boto3.client('pricing', region_name=region)
            client.describe_services(ServiceCode='AmazonElastiCache', MaxResults=1)
            return client
        except Exception:
            continue
    raise ValueError('Pricing API unavailable in both us-east-1 and ap-south-1')

def get_region_location_name(region: str) -> str:
    """Get Pricing API location name from region code via SSM Parameter Store."""
    import boto3
    for ssm_region in [region, 'us-east-1']:
        try:
            ssm = boto3.client('ssm', region_name=ssm_region)
            param = ssm.get_parameter(Name=f'/aws/service/global-infrastructure/regions/{region}/longName')
            return param['Parameter']['Value']
        except Exception:
            continue
    raise ValueError(f'Could not resolve location name for region: {region}')

@tool
def estimate_cost(instance_type: str, num_shards: int, replicas_per_shard: int, region: str = None) -> dict:
    """Estimate monthly on-demand cost for an ElastiCache for Valkey configuration.
    
    Prices are ON-DEMAND only. Reserved Instances (1yr or 3yr) can reduce costs by 30-55%.
    """
    import boto3
    import json
    
    try:
        region = region or os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
        try:
            location = get_region_location_name(region)
        except Exception:
            return {'error': f'Unknown region: {region}. Could not resolve location name via SSM.'}
        
        pricing = get_pricing_client()
        
        response = pricing.get_products(
            ServiceCode='AmazonElastiCache',
            Filters=[
                {'Type': 'TERM_MATCH', 'Field': 'cacheEngine', 'Value': 'Valkey'},
                {'Type': 'TERM_MATCH', 'Field': 'instanceType', 'Value': instance_type},
                {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': location},
                {'Type': 'TERM_MATCH', 'Field': 'locationType', 'Value': 'AWS Region'},
            ],
            MaxResults=10
        )
        
        # Select the correct SKU by usagetype.
        #
        # The Pricing API returns MULTIPLE on-demand SKUs per instance type. For example,
        # cache.r8g.large returns both:
        #   USW2-NodeUsage:cache.r8g.large               -> $0.1756/hr  (the node price)
        #   USW2-SyncDurability-NodeUsage:cache.r8g.large -> $0.031608/hr (durability add-on)
        # Matching loosely (e.g. "first SKU that isn't ExtendedSupport") can pick the
        # durability surcharge and understate node cost by ~5x. So match the usagetype
        # explicitly: the base node price ends in "NodeUsage:<instance_type>" with no
        # other qualifier prefix.
        price_per_hour = None
        durability_price_per_hour = None

        for item in response['PriceList']:
            product = json.loads(item)
            usage_type = product.get('product', {}).get('attributes', {}).get('usagetype', '')
            if not usage_type.endswith(f'NodeUsage:{instance_type}'):
                continue

            # Strip the region prefix (e.g. "USW2-") to inspect any qualifier.
            qualifier = usage_type.split('-', 1)[1] if '-' in usage_type else usage_type
            is_base_node = qualifier == f'NodeUsage:{instance_type}'
            is_durability = 'Durability' in qualifier

            if 'ExtendedSupport' in qualifier:
                continue

            for term_val in product.get('terms', {}).get('OnDemand', {}).values():
                for dim_val in term_val.get('priceDimensions', {}).values():
                    if 'ExtendedSupport' in dim_val.get('description', ''):
                        continue
                    usd = float(dim_val['pricePerUnit'].get('USD', '0'))
                    if is_base_node and price_per_hour is None:
                        price_per_hour = usd
                    elif is_durability and durability_price_per_hour is None:
                        durability_price_per_hour = usd

        if price_per_hour is None:
            return {'error': f'No base NodeUsage pricing found for {instance_type} in {region}'}

        total_nodes = num_shards * (1 + replicas_per_shard)
        monthly_per_node = price_per_hour * 730
        monthly_total = monthly_per_node * total_nodes

        result = {
            'instance_type': instance_type,
            'region': region,
            'price_per_hour_per_node': round(price_per_hour, 4),
            'monthly_per_node': round(monthly_per_node, 2),
            'total_nodes': total_nodes,
            'monthly_total': round(monthly_total, 2),
            'pricing_type': 'On-Demand',
            'note': 'On-Demand pricing shown. Reserved Instances (1-year: ~30% savings, 3-year: ~55% savings) are recommended for production workloads. Node-based costs are fixed — you pay for provisioned nodes 24/7 regardless of traffic. For accurate sizing, ensure the migration assessment was run during peak traffic hours. See Cost Optimization section.'
        }

        # Durability is an optional node-based add-on billed on top of node usage.
        # Report it separately so it is never mistaken for the node price itself.
        if durability_price_per_hour is not None:
            durability_monthly = durability_price_per_hour * 730 * total_nodes
            result['durability_surcharge_per_hour_per_node'] = round(durability_price_per_hour, 6)
            result['durability_monthly_total'] = round(durability_monthly, 2)
            result['monthly_total_with_durability'] = round(monthly_total + durability_monthly, 2)
            result['durability_note'] = (
                'Durability is an OPTIONAL add-on billed IN ADDITION to node usage (node-based only; '
                'not available on Serverless). monthly_total EXCLUDES it. Include '
                'monthly_total_with_durability only if the customer enables durability.'
            )

        return result
    except Exception as e:
        return {'error': f'Failed to estimate cost: {str(e)}'}

@tool
def estimate_serverless_cost(ecpus_per_sec: float, storage_gb: float, region: str = None) -> dict:
    """Estimate monthly cost for ElastiCache Serverless for Valkey.

    Uses AWS Pricing API to fetch real ECPU and storage rates.
    ECPUs are only counted when >= 1 (integer). Minimum storage: 100 MB for Valkey.

    Args:
        ecpus_per_sec: Estimated ECPUs per second from assessment.
        storage_gb: Data storage in GB.
        region: AWS region code.

    Returns:
        Dict with cost breakdown or error.
    """
    import boto3
    import json

    try:
        region = region or os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
        try:
            location = get_region_location_name(region)
        except Exception:
            return {'error': f'Unknown region: {region}. Could not resolve location name via SSM.'}

        pricing = get_pricing_client()

        response = pricing.get_products(
            ServiceCode='AmazonElastiCache',
            Filters=[
                {'Type': 'TERM_MATCH', 'Field': 'cacheEngine', 'Value': 'Valkey'},
                {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': location},
                {'Type': 'TERM_MATCH', 'Field': 'operation', 'Value': 'CreateServerlessCache'},
            ],
            MaxResults=10
        )

        ecpu_price = None
        storage_price = None

        for item in response['PriceList']:
            product = json.loads(item)
            usage_type = product.get('product', {}).get('attributes', {}).get('usagetype', '')
            terms = product.get('terms', {}).get('OnDemand', {})
            for term_val in terms.values():
                for dim_val in term_val.get('priceDimensions', {}).values():
                    price = float(dim_val['pricePerUnit'].get('USD', '0'))
                    if 'ElastiCacheProcessingUnits' in usage_type:
                        ecpu_price = price
                    elif 'CachedData' in usage_type:
                        storage_price = price

        if ecpu_price is None or storage_price is None:
            return {'error': f'Could not find serverless Valkey pricing in {region}'}

        hours_per_month = 730

        # ECPU cost: ecpus/sec * 3600 sec/hr * 730 hr/month * price_per_ecpu
        monthly_ecpus = ecpus_per_sec * 3600 * hours_per_month
        monthly_ecpu_cost = monthly_ecpus * ecpu_price

        # Storage cost: GB-hours. Minimum 0.1 GB for Valkey serverless
        effective_storage = max(storage_gb, 0.1)
        monthly_storage_cost = effective_storage * hours_per_month * storage_price

        monthly_total = monthly_ecpu_cost + monthly_storage_cost

        return {
            'ecpus_per_sec': ecpus_per_sec,
            'storage_gb': storage_gb,
            'effective_storage_gb': effective_storage,
            'region': region,
            'ecpu_price_per_unit': ecpu_price,
            'storage_price_per_gb_hr': storage_price,
            'monthly_ecpu_cost': round(monthly_ecpu_cost, 2),
            'monthly_storage_cost': round(monthly_storage_cost, 2),
            'monthly_total': round(monthly_total, 2),
            'pricing_type': 'On-Demand (Serverless)',
            'note': 'Serverless pricing based on actual usage. ECPUs scale automatically — zero traffic means zero ECPU charges (storage charges still apply). Minimum data storage: 100 MB for Valkey. For accurate estimates, ensure the migration assessment was run during peak traffic hours.'
        }
    except Exception as e:
        return {'error': f'Failed to estimate serverless cost: {str(e)}'}

# System prompt for the agent
SYSTEM_PROMPT = """You are an AWS ElastiCache for Valkey expert advisor. Analyze Redis/Valkey migration assessments and provide detailed ElastiCache for Valkey deployment recommendations.

IMPORTANT GUIDELINES:
1. Always use get_latest_valkey_version tool FIRST to get the latest Valkey version and recommend it
2. For migration tools, ONLY recommend:
   - RedisShake (for most migrations)
   - ElastiCache Online Migration (ONLY if cluster_mode is false AND user verifies prerequisites at: https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Migration-Prepare.html)
   - RIOT is no longer maintained and should not be recommended
3. Target engine is ElastiCache for Valkey for all recommendations

DEPLOYMENT GUIDANCE:
- Present both serverless and node-based deployment types with real pricing
- Let the cost and workload characteristics drive the recommendation
- Include trade-offs so the customer can make an informed decision

CLIENT RECOMMENDATIONS:
- Recommend Valkey GLIDE as the client library (multi-language support, optimized for Valkey, official AWS support)

SIZING GUIDELINES (from AWS documentation):
- Memory is the PRIMARY sharding factor
- BGSAVE overhead depends on write pattern:
  * <10% writes (read-heavy): 1.1-1.2x memory overhead
  * 10-50% writes (balanced): 1.3x memory overhead
  * >60% writes with frequent key rewrites (write-heavy): 1.5-2x memory overhead
  * Append-only workloads (new keys): minimal overhead
  * Worst case: All data rewritten during BGSAVE = 2x memory needed
- Ops/sec capacity depends on:
  * Instance vCPUs (~100K RPS per core for simple GET/SET)
  * Command complexity - use O(1) vs O(N) notation (e.g., GET is O(1), HGETALL is O(N))
  * HGETALL, LRANGE, SORT are 50-100x more expensive than GET
  * Enhanced I/O features (4+ vCPU instances)
  * TLS enabled/disabled
- Network bandwidth varies by instance type
- Larger instances handle hot key spikes better
- LATEST GENERATION (Graviton4) — ElastiCache now supports Graviton4-based M8g, R8g, and C8gn
  node families for Valkey and Memcached. Prefer these over Graviton3 (r7g/m7g/c7gn) when available:
  * Up to 47% higher throughput and up to 43% lower P99 latency vs equivalent Graviton3 nodes
  * Up to 31% better price-performance vs Graviton3
  * Up to 20% more memory per node at the same size (factor this into memory-based sharding math)
  * C8gn delivers up to 200 Gbps network bandwidth for network-intensive workloads
- Instance selection:
  * Memory-bound → R-family — prefer R8g (Graviton4); fall back to r7g/r6g only if R8g is unavailable in the region
  * Network-bound → M-family (M8g) or network-optimized C8gn (up to 200 Gbps); fall back to m7g/C7gn if unavailable
  * CPU-bound → 4+ vCPU with Enhanced I/O
  * ALWAYS prefer the newest available generation returned by get_elasticache_instance_types. Do NOT
    default to an older generation (e.g. r7g) when a newer one (e.g. r8g) is present in the returned data —
    the tool returns live, region-specific availability.

SECURITY:
- ElastiCache for Valkey uses port 6379 for both TLS and non-TLS connections
- Do NOT mention port 6380 - that's incorrect for ElastiCache

When given a migration assessment file:
1. Get latest Valkey version using get_latest_valkey_version
2. Parse metrics using parse_migration_assessment (includes read_ops_sec and write_ops_sec)
2a. ANALYSE COMMAND COMPATIBILITY NOW, before writing any section. From the
   parse_migration_assessment result, determine two constraints and carry them through the
   whole report:
   - SERVERLESS VIABILITY: is restricted_on_serverless_only non-empty (ignoring
     restricted_hits_from_assessment_tool, which is this tool's own probing)? If yes,
     Serverless is NOT viable for this workload.
   - MINIMUM ENGINE VERSION: what is min_valkey_version_required? Compare it against the
     versions get_latest_valkey_version reports as available on ElastiCache.
   Also note restricted_on_all_elasticache (hard blockers on any cache type) and
   module_commands_detected (JSON/BF/FT usage implies a 9.x target).
   These constraints must be reflected in the Deployment Type Comparison and Valkey version
   sections even though the DETAILED analysis is written later in Compatibility Notes.
3. Get instance types using get_elasticache_instance_types
4. Calculate sharding using calculate_shard_recommendation (pass write_ops_sec for ratio calculation, and pass ecpu_complexity_factor from parse_migration_assessment so capacity is adjusted for command mix)
5. Validate each proposed option using validate_recommendation — if any issues are returned, revise the configuration before including it in the report
6. MUST call estimate_cost for every node-based configuration option — include the estimated monthly cost in each option's table (label the row "Estimated Monthly Cost (On-Demand)"), and note Reserved Instances can save 30-55%
7. MUST call estimate_serverless_cost using estimated_ecpus_sec and memory_gb from the assessment — include the serverless cost in the Deployment Type Comparison section
8. Provide comprehensive recommendations in THIS EXACT section order:
   - Workload Summary (table format)
     * Keep this to the OBSERVED workload: memory, ops/sec (read/write split), avg bytes per
       operation, bandwidth, key count, engine version, cluster topology, eviction policy.
     * Do NOT put "Estimated ECPUs/sec" or "ECPU Complexity Factor" in this table. Neither is an
       observed workload characteristic — they are derived inputs to the sizing and cost math.
       ECPUs/sec belongs in the Deployment Type Comparison (it drives the Serverless estimate);
       the complexity factor belongs in the Sizing Note (it adjusts the ops/vCPU capacity figure).
   - Sizing Note (MUST appear immediately after Workload Summary, BEFORE cluster options — use <div class="note">). MUST include ALL of these:
     * Write ratio calculation and BGSAVE overhead band applied
     * Total memory calculation: raw × multiplier = total needed
     * Ops/sec capacity note: ~100K RPS per vCPU for simple O(1) commands (GET, SET, HGET); complex O(N) commands (HGETALL, LRANGE, SORT) are significantly more expensive
     * State the ECPU Complexity Factor here and what it did to the capacity figure. It is the average
       cost of a command in the observed workload relative to a simple GET/SET, so the effective
       capacity used was 100K ÷ complexity factor per vCPU. At 1.0 the workload is all simple O(1)
       commands and the full ~100K per vCPU applies; above 1.0 the per-vCPU figure was reduced
       accordingly. Show the arithmetic so the capacity number is traceable.
     * Network bandwidth limits vary by instance type
     * End with: <strong>⚠️ After deployment, monitor CloudWatch metrics and adjust based on actual usage patterns. These are starting-point recommendations — always validate with real production traffic.</strong>
     * You MAY add additional relevant sizing details (bandwidth, utilization targets, etc.) beyond the required items above
     * Do NOT use eCPUs for node-based sizing — eCPUs are a serverless ElastiCache billing metric. For node-based, use ops/sec and vCPU count. For serverless, use the estimated_ecpus_sec from the assessment with estimate_serverless_cost tool.
   - Deployment Type Comparison (Node-based vs Serverless):
     * Present BOTH options with real pricing from tools
     * Show a comparison table with columns: Aspect | Node-Based | Serverless
     * Include rows for: Estimated Monthly Cost, Scaling, Management Overhead, Durability Support, Best For
     * For the "Best For" row, base it on TRAFFIC SHAPE and operational requirements, not on dataset
       size. Serverless is NOT limited to small workloads — do not write "small footprint", "small
       datasets" or similar. Use:
       - Serverless: bursty, variable or intermittent traffic where capacity would otherwise sit idle;
         unpredictable growth; teams that want no shard/node management.
       - Node-based: steady predictable traffic; need for node-level control and tuning; durability
         requirements; workloads where Reserved Node pricing materially lowers cost.
     * For the Durability Support row: Node-Based = "Supported (synchronous or asynchronous writes)",
       Serverless = "Not currently supported". If the workload appears to need durability (used as a
       primary data store rather than a repopulatable cache), factor this into the recommendation and
       say so explicitly.
     * For Serverless: use estimate_serverless_cost results (ECPU cost + storage cost breakdown).
       State the Estimated ECPUs/sec here, since this is the figure the Serverless cost is derived
       from — show it as the input to the calculation rather than as a standalone metric.
     * For Node-based: use the recommended Option A cost from estimate_cost
     * Note: ECPUs are the serverless billing metric — the assessment provides estimated_ecpus_sec
     * Note: Serverless minimum data storage is 100 MB for Valkey
     * COMMAND CONSTRAINT (from step 2a) — if restricted_on_serverless_only contains commands the
       application genuinely uses, Serverless is NOT viable regardless of cost. Say so here, name
       the blocking command(s) in one line, recommend node-based, and point the reader to
       Compatibility Notes for the full analysis. Do NOT restate the whole inventory here, and do
       NOT recommend Serverless and then contradict it later.
     * Clearly state which option is recommended for this workload and WHY (cost, scale pattern, operational preference)
     * After stating the recommendation, add a short forward-looking sentence telling the reader what the next
       section contains, so the report reads coherently. Node-based cluster options are ALWAYS shown, even when
       Serverless is recommended — so if Serverless wins, say something like: "Detailed node-based cluster
       configurations are provided below as a reference alternative." If Node-based wins, say the following
       section details the recommended configuration options.
     * If serverless is significantly more expensive, explain the cost driver (usually storage at scale)
     * Add: "For serverless pre-scaling options, see: https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Scaling.html#Pre-Scaling"
     * IMPORTANT — Add a <div class="note"> after the comparison table with this caveat:
       "⚠️ Cost estimates are based on the workload assessment snapshot, which reflects usage at the time the assessment was run. <strong>For best results, ensure the migration assessment was run during peak traffic hours</strong> so that sizing and cost estimates reflect your peak workload. For Serverless: when there is no traffic, ECPU charges drop to zero — you only pay for storage. This means Serverless can be significantly cheaper for bursty or intermittent workloads. For Node-based: costs are fixed regardless of traffic — you pay for provisioned nodes 24/7."
   - Valkey version (use the latest from get_latest_valkey_version)
     * COMMAND CONSTRAINT (from step 2a) — the version you recommend MUST satisfy
       min_valkey_version_required. If it is set, name the driving command(s) in one line
       (e.g. "HEXPIRE requires Valkey 9.0+") and point to Compatibility Notes for detail. If
       module commands (JSON/BF/FT) were detected, recommend 9.x. If the requirement exceeds
       every version ElastiCache offers, state that as a hard blocker here, not only later.
   - Cluster configuration with MULTIPLE OPTIONS (MUST come BEFORE instance type justification):
     * REQUIRED TRANSITION: Open this section with a one-to-two sentence bridge that connects it to the
       deployment recommendation you just made, so the reader understands why node-based configurations
       follow. Tailor it to the recommendation:
       - If NODE-BASED was recommended: state that the following options detail the recommended node-based
         deployment, and that Option A is the starting point.
       - If SERVERLESS was recommended: explicitly acknowledge that Serverless is the primary recommendation
         and that the node-based options below are provided as a reference alternative — useful if you
         need predictable fixed costs, require node-level control/tuning, have steady (non-bursty)
         traffic that erodes the Serverless advantage, or must satisfy requirements Serverless does not yet
         support. Make clear these are NOT a reversal of the recommendation.
       - If the two are CLOSE IN COST: say so, and frame the node-based options as a genuine side-by-side
         alternative you may reasonably choose on operational preference.
     * ALWAYS recommend Cluster Mode Enabled (CME) — even for single-shard deployments. CME allows future horizontal scaling (adding shards) without downtime or re-creation. If the source has cluster mode disabled, note that the target should still use CME and advise the customer to verify their application does not use cross-slot multi-key commands without hash tags (e.g., bare MGET across unrelated keys). Note: CMD should only be used if the application is legacy and cannot move to CME at this point.
     * Show the math: raw memory × overhead multiplier = total needed
     * SHARD COUNT PARITY — do NOT annotate the Shards row in any option table. Never write "(odd)",
       "(even)" or "no parity concern" next to a shard count; that is internal reasoning and is noise.
       If ALL options have an odd shard count, say nothing at all about parity.
       If ANY option lands on an EVEN shard count, add ONE note AFTER all three option tables (use
       <div class="note">), not inside them, and not repeated per option. In that note explain:
       - Which option(s) it applies to.
       - Why it matters: on a cluster without durability, the nodes decide among themselves which
         shard is authoritative if they lose contact with each other, and that decision is made by
         majority. An odd number of shards means there is always a clear majority; an even number can
         split evenly with no clear winner, which slows or complicates recovery.
       - The ways to resolve it, with the real cost of each: step to the adjacent odd shard count
         (each shard is 1 primary plus its replicas, so at 2 replicas per shard one extra shard means
         3 more nodes — give the actual dollar delta if you have it); or use a larger instance size so
         a smaller, odd number of shards covers the same memory; or enable durability, which removes
         the consideration entirely because the authoritative copy is determined at the storage layer
         rather than by majority.
       - If you recommend an adjusted configuration, confirm it still satisfies memory, ops/sec and
         bandwidth per shard.
       Keep it to a few sentences. This is a consideration to raise, not a blocker.
     * Option A: Balanced (mark as "Recommended Starting Point" or "✅ Recommended")
     * Option B: More shards, smaller instances
     * Option C: Fewer shards, larger instances
     * IMPORTANT: After calling estimate_cost for ALL options, compare the total monthly costs. The option with the LOWEST total monthly cost gets a "💰 Cost-Optimized" label. No other option gets this label. If Option A costs $22,942 and Option B costs $24,458, Option A gets the label — always compare the actual numbers.
     * Compare trade-offs for each option
     * Clearly indicate which option is the primary recommendation
   - Instance type justification (MUST come AFTER cluster configuration options):
     * Why this family? (R vs M vs C)
     * Why this generation? (justify the newest available generation, e.g. r8g vs r7g — pick the newest present in the get_elasticache_instance_types results)
     * Why not alternatives? (explain what doesn't fit)
     * Enhanced I/O benefits
   - Sharding strategy
   - Performance and data modelling practices (keep brief, point to the docs):
     * Enable SLOWLOG to a permanent destination to find long-running commands (needs Valkey 7.2+ /
       Redis OSS 6.0+ and parameter group configuration).
     * Compress large values to reduce memory and network usage.
     * Use a consistent key naming convention to avoid unintentional overwrites; be aware of command
       time complexity (O(1) vs O(N)) when modelling data.
     * Identify and remediate hot keys; connect to the correct endpoint (configuration endpoint for
       cluster mode enabled, reader endpoint for replica reads).
   - Behavioural differences to be aware of after migration (flag only if relevant to the workload):
     * FLUSHDB / FLUSHALL: on Serverless these always flush the entire cluster and cannot be used
       inside a transaction; on node-based they must be sent to every primary.
     * Pub/Sub on Serverless uses sharded pub/sub internally, so channel names are mixed.
   - Client configuration (recommend Valkey GLIDE)
     * Use connection pooling; do not open a connection per request
     * Set the client socket timeout to at least one second
     * Use pipelining for bulk ingestion
     * Send reads to replica nodes for read-heavy workloads
     * If IAM auth is used, the client must support a credentials provider so 15-minute tokens are
       refreshed automatically on long-lived connections
   - High availability setup:
     * Default: 2 replicas per shard (for HA + read scaling)
     * Read-heavy workloads (>70% reads): Consider 3+ replicas for read distribution
     * Write-heavy workloads: 1-2 replicas sufficient (writes don't benefit from more replicas)
     * Explain replica count reasoning based on read/write ratio
     * Ask the customer to document RPO/RTO. Point to scheduled backups for RPO, and Global
       Datastore for cross-Region DR if their RTO requires it.
       https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Redis-Global-Datastore.html
     * Scaling axes — state which lever applies: more read throughput → add replicas; more write
       throughput → add shards (scale out); more network throughput → network-optimized instances
       or scale up.
     * Auto Scaling — recommend for variable/spiky workloads so node count tracks demand instead of
       being provisioned for peak 24/7. Requires cluster mode enabled and a supported instance
       family/size. NOTE: not available with Global Datastore, Outposts, or Local Zones — if the
       customer needs cross-Region DR via Global Datastore, say plainly that Auto Scaling and
       Global Datastore cannot be combined and they must choose.
       https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/AutoScaling.html
     * Enable SNS notifications for cluster events so failovers and maintenance are visible.
     * Enable engine logs (Redis OSS 6.2+ / Valkey) to CloudWatch Logs or Kinesis Data Firehose.
     * Stay current: apply self-service updates promptly and run a supported engine version.
   - Durability (optional — mention it, then point to the documentation):
     * Durability is an ElastiCache-specific feature — it is not part of open-source Valkey or Redis
       OSS, and is not available in a self-managed deployment. It is something the customer gains by
       moving to ElastiCache.
     * ElastiCache for Valkey supports durability, so it can be used as a primary data store and not
       only as a cache. Synchronous writes persist the write before the client is acknowledged, which
       adds single-digit millisecond write latency. Asynchronous writes keep write latency in
       microseconds, but up to 10 seconds of writes can be lost if the primary fails. Reads stay in
       microseconds either way.
     * Node-based only — not available on Serverless. If you recommended Serverless and the customer
       needs durability, say that is a reason to choose node-based instead.
     * It is an option, not a requirement. If the data can be rebuilt from a database, they may not
       need it. If it cannot — session state, a primary data store, anything with no second copy —
       losing it means losing data. Say which applies based on the observed workload and let them
       decide. Leave the configuration detail to the documentation.
     * https://aws.amazon.com/blogs/database/announcing-durability-for-amazon-elasticache/
       https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/durability.html
     * COST: estimate_cost returns durability pricing separately when available:
       monthly_total EXCLUDES durability; durability_monthly_total is the add-on; and
       monthly_total_with_durability is the combined figure. Always quote monthly_total as the
       node cost. If you discuss enabling durability, show the surcharge as a SEPARATE line item
       (e.g. "node cost $384.56/mo + durability $69.22/mo = $453.79/mo") — never present the
       durability surcharge on its own as if it were the node price.
     * COST: estimate_cost returns durability pricing separately when available:
       monthly_total EXCLUDES durability; durability_monthly_total is the add-on; and
       monthly_total_with_durability is the combined figure. Always quote monthly_total as the
       node cost. If you discuss enabling durability, show the surcharge as a SEPARATE line item
       (e.g. "node cost $384.56/mo + durability $69.22/mo = $453.79/mo") — never present the
       durability surcharge on its own as if it were the node price.
   - Security (high level — point the customer in the right direction, do not write a full guide):
     * IAM authentication — RECOMMENDED. Requires ElastiCache for Valkey 7.2+ (or Redis OSS 7.0+)
       and in-transit encryption enabled. Short-lived tokens instead of long-lived passwords.
       https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/auth-iam.html
       Note for clients: tokens expire in 15 minutes, so long-lived connections need a client that
       supports a credentials provider to refresh them (Valkey GLIDE does).
     * Encryption in transit (TLS) — RECOMMENDED.
       https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/in-transit-encryption.html
     * Encryption at rest — RECOMMENDED.
       https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/at-rest-encryption.html
     * RBAC user groups with least-privilege access strings — recommended where IAM auth is not
       available (target engine below 7.2). Rotate auth tokens if AUTH is used.
       https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Clusters.RBAC.html
     * Network: place the cache in private subnets, restrict inbound with security groups to the
       application tier only.
     * Control plane: least-privilege IAM for ElastiCache API actions; separate accounts for
       production and non-production.
     * Reference: ElastiCache Well-Architected Lens Security Pillar
       https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/SecurityPillar.html
   - Resilience testing with AWS Fault Injection Service (FIS):
     * LEAD WITH THE BENEFIT: most applications assume the cache is always available. The failure
       mode you actually care about is what your application does when a node goes away — does it
       reconnect cleanly, does it fall back to the database, does that fallback overwhelm the
       database. You want to find that out on your own schedule in a test environment, not during a
       production incident. Testing failover before you go live is the point; verifying that
       ElastiCache itself fails over is secondary, because it does that automatically.
     * Then say what it is: AWS FIS is a managed service for running controlled fault injection
       experiments. For ElastiCache it can interrupt power to nodes in a chosen Availability Zone
       (action: aws:elasticache:replicationgroup-interrupt-az-power), which triggers a real failover
       so you can watch how your application handles it.
     * Recommend running it in a NON-PRODUCTION environment first. Requires Multi-AZ enabled, and
       targets are selected by resource tag.
     * IMPORTANT: FIS experiments are NOT supported on ElastiCache Serverless — Serverless handles
       Multi-AZ failover behind a managed proxy. Only recommend FIS for node-based deployments.
     * What to look for: connection errors should last seconds not minutes; database query rate
       rises temporarily but stays manageable; latency returns to baseline afterwards.
     * Blog walkthrough: https://aws.amazon.com/blogs/database/resilience-testing-on-amazon-elasticache-with-aws-fault-injection-service/
   - CloudWatch Metrics to Monitor Post-Migration:
     * MUST include these core metrics with recommended thresholds:
       - CPUUtilization - overall CPU usage
       - EngineCPUUtilization - Valkey engine thread CPU
       - FreeableMemory - available memory
       - SwapUsage - swap usage (should be near zero)
       - BytesUsedForCache - memory used by data
       - NetworkBytesIn / NetworkBytesOut - network throughput
       - CurrConnections - current client connections
       - NewConnections - rate of new connections
     * Add additional relevant metrics based on workload characteristics:
       - For cache workloads: CacheHits, CacheMisses, CacheHitRate
       - For replicated clusters: ReplicationLag, ReplicationBytes
       - For memory-constrained: Evictions, DatabaseMemoryUsagePercentage
       - For write-heavy: SaveInProgress
       - If RBAC or IAM auth is enabled: AuthenticationFailures, KeyAuthorizationFailures,
         CommandAuthorizationFailures (surface security events, not just performance)
       - Any other metrics relevant to the specific workload
     * Provide specific threshold recommendations based on instance type and workload
   - Cost optimization (MUST include ALL of these topics, not just pricing comparison):
     * Reserved Instances savings (1-year and 3-year)
     * Right-sizing review after 2-4 weeks of production monitoring
     * Read replica efficiency — are replicas actively serving reads or idle?
     * Data tiering consideration (r6gd instances with NVMe SSD for large datasets)
     * Latest Graviton generation benefits — Graviton4 (R8g/M8g/C8gn) offers up to 31% better price-performance, up to 47% higher throughput, up to 43% lower P99 latency, and up to 20% more memory per node vs Graviton3; migrating from r7g/m7g to r8g/m8g can reduce cost per op
     * Snapshot retention cost awareness
     * Use cost allocation tags to attribute cache spend per workload/team, and correlate CloudWatch
       utilization with Cost Explorer to spot over-provisioning
     * Cross-AZ data transfer costs for replicas
     * Any other cost optimization tips relevant to the specific workload
   - Migration approach
   - Compatibility Notes (MUST include ALL of these):
     * OBSERVED COMMANDS — parse_migration_assessment returns the commands the workload actually
       ran (observed_commands, observed_command_calls). Ground this whole section in that data
       rather than generic advice. State plainly that the list covers only what executed during
       the measurement window, so infrequent paths (batch jobs, admin scripts) may be missing.
     * RESTRICTED COMMANDS — write this as prose about the workload, not as a field-by-field readout.
       Never print a field name, and never say a list is "empty" — say what that means instead.
       Three findings to cover, in this order:
       1. Commands unavailable on ANY ElastiCache cache (read from restricted_on_all_elasticache).
          If present: "This workload uses DEBUG, which is not available on ElastiCache. The
          application must stop using it before migrating."
          If none: "No commands used by this workload are restricted on ElastiCache."
       2. Commands that work on node-based but not Serverless (read from
          restricted_on_serverless_only).
          If present: "This workload uses KEYS and FUNCTION, which are not available on ElastiCache
          Serverless." If the application genuinely relies on them, that alone rules out Serverless —
          say so and reconcile it with the Deployment Type Comparison recommendation.
          If none: "No commands used by this workload are restricted on ElastiCache Serverless."
       3. Restricted commands that came from the assessment tool's own probing (read from
          restricted_hits_from_assessment_tool — CONFIG, INFO, CLIENT, CLUSTER, MEMORY, OBJECT,
          SLOWLOG). Never report these as application blockers.
          If present: "CONFIG appeared in the command statistics, but it was issued by the assessment
          tool while collecting metrics rather than by your application. Confirm your application does
          not use it directly."
          If none: omit this point entirely — do not mention it.
       Do NOT structure this as "Restricted on all ElastiCache (restricted_on_all_elasticache):
       empty — ..." That exposes internal names and reads as a debug dump.
       - If all three are empty, say so — "no restricted commands were observed" is a useful finding.
     * MINIMUM ENGINE VERSION — command_min_valkey_version maps each observed command to the
       Valkey version that introduced it (only for commands added after 7.2);
       min_valkey_version_required is the highest such requirement. Use it as follows:
       - If min_valkey_version_required is set, the recommended target engine version MUST be
         at least that. State the driving commands explicitly, e.g. "the workload uses HEXPIRE
         and HTTL, which require Valkey 9.0+, so target 9.x — these will not work on 7.2 or 8.x."
       - Cross-check against the versions get_latest_valkey_version reports. If the requirement
         exceeds every version ElastiCache offers, that is a HARD BLOCKER: name the command, the
         version it needs, and the highest version ElastiCache currently supports.
       - If it is null, say that all observed commands are available on any ElastiCache for
         Valkey version, so version choice is driven by features and price-performance instead.
     * MODULE / SEARCH COMMANDS — module_commands_detected reports observed JSON.*, BF.* and FT.*
       usage. ElastiCache for Valkey supports these natively from the 9.x engine versions:
       - JSON (JSON.* commands) for document storage
       - Bloom filter (BF.* commands) for probabilistic membership
       - Search (FT.* commands) covering vector similarity search, full-text search, exact
         matching, and numeric filters
       If the workload uses any of these, recommend a 9.x target version and say why. If the
       source is Redis Stack or a third-party module, flag that the API surface may differ from
       Valkey's native implementation and must be validated — do not assume drop-in equivalence.
     * HASH FIELD EXPIRATION — if the source engine predates hash-field TTL and the workload
       manages per-field expiry manually (e.g. a parallel sorted set of timestamps plus a cleanup
       job, or splitting one hash into many keys just to get independent TTLs), note that
       ElastiCache for Valkey supports HEXPIRE/HPEXPIRE/HGETEX/HGETDEL/HTTL/HPERSIST and the
       application can drop that workaround. Present it as an available simplification, not a
       requirement.
     * Valkey API compatibility with the source Redis version
     * Lua scripts and server-side functions compatibility — note that FUNCTION/FCALL are
       restricted on Serverless
     * Eviction policy carryover (confirm if source policy works as-is or needs changes)
     * Reference: https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/SupportedCommands.html

Be specific with numbers and explain your reasoning.

FORMATTING REQUIREMENTS:
- Show detailed calculations: "450.75 GB × 1.3 = 586 GB total needed"
- Present multiple configuration options (A, B, C) with pros/cons
- Include comprehensive instance type justification section
- Explain why alternatives don't fit
- Use consistent formatting:
  * Workload Summary: Use TABLE format with columns: Metric | Value | Notes
  * Cluster Configuration Options: Use tables for each option (A, B, C)
  * Instance Justification: Use <ul> lists with <h3> subsections

IMPORTANT MATH CONSTRAINTS:
- When calculating shards: Use MINIMUM shards needed + 10-15% headroom maximum
- Do NOT over-provision beyond 20% headroom
- Example: If 586 GB needed and instance has 52 GB → 586/52 = 11.1 shards → use 12 shards (not 18!)
- Show the math explicitly: "586 GB ÷ 52.82 GB = 11.1 → 12 shards"
- Validate that total capacity meets requirement with reasonable headroom (10-20%)

IMPORTANT: Format your response as clean HTML content (body content only, not full document) with:
- Use <h2> for main sections
- Use <h3> for subsections
- Use <ul> and <li> for bullet points
- Use <code> tags for instance types and commands
- Use <strong> for emphasis
- Use <div class="disclaimer"> for the disclaimer section
- Use <div class="note"> for the sizing note
- Do NOT include ANY thinking process, commentary, or markdown markers
- COST LABELLING: every cost figure in the report is an ESTIMATE. Label it as such everywhere it
  appears — table headers, row labels, and prose. Use "Estimated Monthly Cost" rather than
  "Monthly Cost", "estimated at $384.56/month" rather than "costs $384.56/month". This applies to
  node costs, serverless costs, durability surcharges, and any totals or deltas you compute from
  them. Never present a figure in a way that reads as a quote or a committed price.
- VOICE: the customer is the person READING this report. Address them directly as "you" / "your".
  Never refer to them in the third person as "the customer", "the customer's application", "they" or
  "the client" — that reads like an internal document written about them rather than for them.
  Wrong: "if the customer prefers predictable fixed costs"
  Right: "if you prefer predictable fixed costs"
  Wrong: "advise the customer to verify their application does not use cross-slot commands"
  Right: "verify your application does not use cross-slot commands"
  Wrong: "the customer gains this by moving to ElastiCache"
  Right: "you gain this by moving to ElastiCache"
  Note: where these instructions say "the customer", that is describing the reader TO YOU — translate
  it to "you" in the report itself.
- NEVER print internal field names or tool names in the report. Names like
  restricted_on_serverless_only, restricted_on_all_elasticache, restricted_hits_from_assessment_tool,
  observed_commands, min_valkey_version_required, module_commands_detected, estimated_ecpus_sec,
  ecpu_complexity_factor, monthly_total, and the tool names (parse_migration_assessment,
  estimate_cost, estimate_serverless_cost, calculate_shard_recommendation,
  get_elasticache_instance_types, get_latest_valkey_version, validate_recommendation) are
  INSTRUCTIONS TO YOU, not report content. The customer has never seen them and they read as leaked
  implementation detail. Describe the finding in plain language instead.
  Wrong: "restricted_on_serverless_only is empty — no application commands block Serverless."
  Right: "No commands used by this workload are restricted on ElastiCache Serverless."
  Also wrong — do NOT put the field name in brackets after a friendly label:
  Wrong: "Restricted on Serverless only (restricted_on_serverless_only): empty — nothing rules out..."
  Right: "No commands used by this workload are restricted on ElastiCache Serverless."
  And never describe a result as "empty", "null", "none returned" or "the list is empty" — those
  describe a data structure. Say what it means for the workload instead.
  Wrong: "min_valkey_version_required is 9.0.0."
  Right: "This workload needs Valkey 9.0 or later, because it uses HEXPIRE and HTTL."
  The only identifiers that SHOULD appear are things the customer deals with directly: Valkey/Redis
  command names (GET, HEXPIRE), instance types (cache.r8g.2xlarge), CloudWatch metric names
  (CPUUtilization, FreeableMemory), and parameter names they would set themselves.
- Start IMMEDIATELY with HTML content (first line should be <h2> for Workload Summary)
- Do NOT write "Let me...", "I will...", "Now I have...", etc.
- Do NOT include a report title/header or metadata (file, region, date, model) — the HTML template already provides these

IMPORTANT: Always end your recommendations with this EXACT disclaimer text (do not paraphrase):

<div class="disclaimer">
<h2>⚠️ Disclaimer</h2>
<p>These recommendations are <strong>AI-generated</strong> and should be used as <strong>guidance only</strong>. You must:</p>
<ul>
  <li>Thoroughly <strong>test all recommendations</strong> in a non-production environment</li>
  <li>Validate that your application functions correctly with the recommended configuration</li>
  <li>Verify that all <strong>SLA, performance, and business requirements</strong> are met</li>
  <li>Conduct <strong>load testing</strong> to ensure the configuration handles your workload</li>
  <li>Have a <strong>rollback plan</strong> before implementing in production</li>
  <li>Consult AWS documentation and AWS Support <strong>if you need further assistance</strong> for production deployments</li>
  <li><strong>Independently verify all cost estimates</strong> before making budget or purchasing decisions</li>
</ul>
<p><strong>💰 Cost disclaimer:</strong> All pricing shown is <strong>estimated</strong>, based on public On-Demand rates retrieved from the AWS Pricing API at the time this report was generated, and on the workload metrics captured during the assessment window. Actual costs may differ. Estimates <strong>exclude</strong> data transfer, backup/snapshot storage, CloudWatch, and other associated service charges. Prices vary by region and change over time, and Reserved Instance or Savings Plan commitments will alter the totals. If the assessment was not run during peak traffic, sizing and cost may be understated. For authoritative figures, consult the <strong>AWS Pricing Calculator</strong>, the <a href="https://aws.amazon.com/elasticache/pricing/">ElastiCache pricing page</a>, and your AWS account team. These estimates are not a quote or a commitment from AWS.</p>
</div>

📊 SIZING NOTE: Recommendations are based on AWS best practices:
- Memory-based sharding with BGSAVE overhead applied based on write pattern (<10% writes: 1.1-1.2×, 10-50%: 1.3×, >60%: 1.5-2×)
- Ops/sec capacity: ~100K RPS per vCPU for simple O(1) commands (GET, SET, HGET); complex O(N) commands (HGETALL, LRANGE, SORT) are significantly more expensive
- Network bandwidth limits vary by instance type (check instance specs)
- Write ratio assessed to determine appropriate memory overhead multiplier
After deployment, monitor CloudWatch metrics and adjust based on actual usage patterns.<br><strong>⚠️ These are starting-point recommendations — always validate with real production traffic.</strong>"
"""

if __name__ == "__main__":
    import asyncio
    import argparse
    import os
    
    parser = argparse.ArgumentParser(description='ElastiCache for Valkey Migration Advisor using Strands')
    # Default to the bundled example assessment. Resolved relative to this script so it works
    # regardless of the current working directory (the script lives in agent/, the example in
    # ../examples/).
    _default_example = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir, 'examples', 'output.json'
    )
    parser.add_argument('--file', '-f',
                        default=os.path.normpath(_default_example),
                        help='Path to migration assessment JSON file (defaults to the bundled example)')
    parser.add_argument('--region', '-r',
                        default='us-west-2',
                        help='AWS region for Bedrock (default: us-west-2)')
    parser.add_argument('--model', '-m',
                        default='global.anthropic.claude-sonnet-5',
                        help='Bedrock model ID (default: Claude Sonnet 5)')
    parser.add_argument('--output', '-o',
                        default=None,
                        help='Output HTML file (default: elasticache-recommendations-TIMESTAMP.html)')
    parser.add_argument('--max-tokens',
                        type=int,
                        default=32000,
                        help='Max output tokens for the model (default: 32000). Raise if the '
                             'report is truncated with MaxTokensReachedException.')
    
    args = parser.parse_args()
    
    # Generate unique filename if not specified
    if args.output is None:
        import datetime
        timestamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        args.output = f'elasticache-recommendations-{timestamp}.html'
    
    # Set region
    os.environ['AWS_DEFAULT_REGION'] = args.region
    
    # Create agent with user-specified model.
    # An explicit BedrockModel is used (rather than passing the model ID string)
    # so max_tokens can be raised: the system prompt asks for a long, multi-section
    # HTML report, which overruns the provider default and raises
    # MaxTokensReachedException mid-generation.
    from strands.models.bedrock import BedrockModel

    bedrock_model = BedrockModel(
        model_id=args.model,
        region_name=args.region,
        max_tokens=args.max_tokens,
    )

    advisor = Agent(
        name="ElastiCache for Valkey Migration Advisor",
        model=bedrock_model,
        system_prompt=SYSTEM_PROMPT,
        tools=[get_latest_valkey_version, parse_migration_assessment, get_elasticache_instance_types, calculate_shard_recommendation, validate_recommendation, estimate_cost, estimate_serverless_cost]
    )
    
    print(f"🚀 Starting ElastiCache for Valkey Migration Advisor (Strands)...")
    print(f"   File: {args.file}")
    print(f"   Region: {args.region}")
    print(f"   Model: {args.model}")
    print(f"   Output: {args.output}\n")
    
    # Capture the response
    response_text = []
    
    async def main():
        response = await advisor.invoke_async(
            f"""Analyze the migration assessment at {args.file} and provide ElastiCache for Valkey recommendations in HTML format.
Use region '{args.region}' for all tool calls that accept a region parameter.

Format your response as clean HTML content (not a complete HTML document, just the body content) with:
- Use <h2> for main sections (e.g., "Valkey Version", "Instance Type", etc.)
- Use <h3> for subsections
- Use <ul> and <li> for bullet points
- Use <code> tags for instance types (e.g., cache.r7g.2xlarge)
- Use <strong> for emphasis
- Use <div class="disclaimer"> for the disclaimer section
- Use <div class="note"> for the sizing note

Remember to recommend the latest Valkey version available and evaluate if ElastiCache Online Migration prerequisites are met."""
        )
        
        # Get response text (should be HTML now)
        response_html = str(response.content) if hasattr(response, 'content') else str(response)
        
        # Strip any LLM preamble before the first HTML tag
        import re
        html_start = re.search(r'<[hH][1-6r]|<div|<table|<ul|<ol|<p[ >]', response_html)
        if html_start:
            response_html = response_html[html_start.start():]
        
        # Generate HTML report
        import datetime
        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ElastiCache for Valkey Migration Recommendations</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            line-height: 1.6;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 30px;
        }}
        .header h1 {{
            margin: 0 0 10px 0;
        }}
        .metadata {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .metadata table {{
            width: 100%;
            border-collapse: collapse;
        }}
        .metadata td {{
            padding: 8px;
            border-bottom: 1px solid #eee;
        }}
        .metadata td:first-child {{
            font-weight: bold;
            width: 200px;
        }}
        .content {{
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .content h2 {{
            color: #667eea;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
            margin-top: 30px;
        }}
        .content h3 {{
            color: #764ba2;
            margin-top: 20px;
        }}
        .content ul {{
            line-height: 1.8;
        }}
        .content code {{
            background: #f8f9fa;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
            color: #e83e8c;
        }}
        .content strong {{
            font-weight: 600;
        }}
        .content h3 {{
            color: #667eea;
            margin-top: 25px;
            margin-bottom: 10px;
        }}
        .content ul {{
            margin: 10px 0;
            padding-left: 20px;
        }}
        .content li {{
            margin: 5px 0;
        }}
        .content code {{
            background: #f8f9fa;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
            color: #e83e8c;
        }}
        .content strong {{
            color: #333;
        }}
        .content p {{
            margin: 10px 0;
        }}
        .content table {{
            width: 100%;
            border-collapse: collapse;
            margin: 15px 0;
        }}
        .content th, .content td {{
            border: 1px solid #ddd;
            padding: 8px 12px;
            text-align: left;
        }}
        .content th {{
            background: #f2f2f2;
            font-weight: 600;
        }}
        .disclaimer {{
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 15px;
            margin: 20px 0;
            border-radius: 4px;
        }}
        .note {{
            background: #d1ecf1;
            border-left: 4px solid #17a2b8;
            padding: 15px;
            margin: 20px 0;
            border-radius: 4px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🚀 ElastiCache for Valkey Migration Recommendations</h1>
        <p>AI-powered migration analysis and deployment recommendations</p>
    </div>
    
    <div class="metadata">
        <table>
            <tr>
                <td>Assessment File</td>
                <td>{args.file}</td>
            </tr>
            <tr>
                <td>Generated</td>
                <td>{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</td>
            </tr>
            <tr>
                <td>AWS Region</td>
                <td>{args.region}</td>
            </tr>
            <tr>
                <td>Model</td>
                <td>{args.model}</td>
            </tr>
        </table>
    </div>
    
    <div class="content">
{response_html}
    </div>
</body>
</html>"""
        
        with open(args.output, 'w') as f:
            f.write(html_content)
        
        print(f"\n✅ Analysis complete!")
        print(f"📄 HTML report saved to: {args.output}")
    
    asyncio.run(main())
