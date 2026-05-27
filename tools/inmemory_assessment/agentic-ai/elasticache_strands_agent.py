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
        'cluster_mode': cluster.get('cluster_mode'),
        'primaries': cluster.get('primaries'),
        'replicas': cluster.get('replicas'),
        'eviction_policy': cluster.get('eviction_policy_0')
    }
    
    # Parse source engine and version from engines_0 (e.g., "Redis 6.2.6")
    engine_str = cluster.get('engines_0', '')
    parts = engine_str.split(' ', 1)
    result['source_engine'] = parts[0] if parts else 'Unknown'
    result['source_version'] = parts[1] if len(parts) > 1 else 'Unknown'
    
    # Flag any missing critical fields
    missing = [k for k in ['memory_gb', 'total_ops_sec'] if result[k] is None]
    if missing:
        result['warning'] = f'Missing critical fields: {", ".join(missing)}'
    
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
                    {'Type': 'TERM_MATCH', 'Field': 'cacheEngine', 'Value': 'Redis'},
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
    
    # If instance data provided, use it; otherwise use fallback r7g values
    if instance_types_data:
        # Extract memory-optimized instances (r-family, any generation)
        instance_options = []
        for instance_name, specs in instance_types_data.items():
            # Match cache.rXg.* pattern (r7g, r8g, r9g, etc.)
            if instance_name.startswith('cache.r') and 'g.' in instance_name:
                size = instance_name.split('.')[-1]
                if size in ['xlarge', '2xlarge', '4xlarge', '8xlarge', '12xlarge', '16xlarge']:
                    instance_options.append({
                        'size': size,
                        'memory_gb': specs['memory_gb'],
                        'vcpus': specs['vcpu'],
                        'network_gbps': specs['network_gbps']
                    })
    else:
        # Fallback: r7g family values (for when API data not available)
        instance_options = [
            {'size': 'xlarge', 'memory_gb': 26, 'vcpus': 4, 'network_gbps': 10},
            {'size': '2xlarge', 'memory_gb': 52, 'vcpus': 8, 'network_gbps': 15},
            {'size': '4xlarge', 'memory_gb': 105, 'vcpus': 16, 'network_gbps': 15},
            {'size': '8xlarge', 'memory_gb': 209, 'vcpus': 32, 'network_gbps': 15},
            {'size': '12xlarge', 'memory_gb': 314, 'vcpus': 48, 'network_gbps': 20},
            {'size': '16xlarge', 'memory_gb': 419, 'vcpus': 64, 'network_gbps': 25},
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
            'instance_size': instance['size'],
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
        'recommended_instance_size': preferred['instance_size'],
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
        
        # Find base on-demand price (skip ExtendedSupport SKUs)
        price_per_hour = None
        for item in response['PriceList']:
            product = json.loads(item)
            terms = product.get('terms', {}).get('OnDemand', {})
            for term_val in terms.values():
                for dim_val in term_val.get('priceDimensions', {}).values():
                    desc = dim_val.get('description', '')
                    if 'ExtendedSupport' in desc:
                        continue
                    price_per_hour = float(dim_val['pricePerUnit'].get('USD', '0'))
                    break
                if price_per_hour is not None:
                    break
            if price_per_hour is not None:
                break
        
        if price_per_hour is None:
            return {'error': f'No pricing found for {instance_type} in {region}'}
        
        total_nodes = num_shards * (1 + replicas_per_shard)
        monthly_per_node = price_per_hour * 730
        monthly_total = monthly_per_node * total_nodes
        
        return {
            'instance_type': instance_type,
            'region': region,
            'price_per_hour_per_node': round(price_per_hour, 4),
            'monthly_per_node': round(monthly_per_node, 2),
            'total_nodes': total_nodes,
            'monthly_total': round(monthly_total, 2),
            'pricing_type': 'On-Demand',
            'note': 'On-Demand pricing shown. Reserved Instances (1-year: ~30% savings, 3-year: ~55% savings) are recommended for production workloads. Node-based costs are fixed — you pay for provisioned nodes 24/7 regardless of traffic. For accurate sizing, ensure the migration assessment was run during peak traffic hours. See Cost Optimization section.'
        }
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
   - DO NOT recommend RIOT as it is no longer maintained
3. This is for ElastiCache for Valkey - focus on Valkey recommendations without mentioning Redis negatively

TONE & MESSAGING:
- When discussing serverless: Be neutral and present it as a valid option alongside node-based
- Present BOTH deployment types with real pricing so the customer can make an informed decision
- Focus on Valkey benefits, not Redis drawbacks
- Avoid saying "Do not use Redis" - just recommend Valkey

CLIENT RECOMMENDATIONS:
- Always recommend Valkey GLIDE as the primary client library
- Mention it supports multiple languages and is optimized for Valkey

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
- Instance selection:
  * Memory-bound → R-family (r7g, r6g)
  * Network-bound → M-family (m7g) or C7gn
  * CPU-bound → 4+ vCPU with Enhanced I/O

SECURITY:
- ElastiCache for Valkey uses port 6379 for both TLS and non-TLS connections
- Do NOT mention port 6380 - that's incorrect for ElastiCache

When given a migration assessment file:
1. Get latest Valkey version using get_latest_valkey_version
2. Parse metrics using parse_migration_assessment (includes read_ops_sec and write_ops_sec)
3. Get instance types using get_elasticache_instance_types
4. Calculate sharding using calculate_shard_recommendation (pass write_ops_sec for ratio calculation)
5. Validate each proposed option using validate_recommendation — if any issues are returned, revise the configuration before including it in the report
6. MUST call estimate_cost for every node-based configuration option — include exact monthly cost in each option's table, label as On-Demand, and note Reserved Instances can save 30-55%
7. MUST call estimate_serverless_cost using estimated_ecpus_sec and memory_gb from the assessment — include the serverless cost in the Deployment Type Comparison section
8. Provide comprehensive recommendations in THIS EXACT section order:
   - Workload Summary (table format)
   - Sizing Note (MUST appear immediately after Workload Summary, BEFORE cluster options — use <div class="note">). MUST include ALL of these:
     * Write ratio calculation and BGSAVE overhead band applied
     * Total memory calculation: raw × multiplier = total needed
     * Ops/sec capacity note: ~100K RPS per vCPU for simple O(1) commands (GET, SET, HGET); complex O(N) commands (HGETALL, LRANGE, SORT) are significantly more expensive
     * Network bandwidth limits vary by instance type
     * End with: <strong>⚠️ After deployment, monitor CloudWatch metrics and adjust based on actual usage patterns. These are starting-point recommendations — always validate with real production traffic.</strong>
     * You MAY add additional relevant sizing details (bandwidth, utilization targets, etc.) beyond the required items above
     * Do NOT use eCPUs for node-based sizing — eCPUs are a serverless ElastiCache billing metric. For node-based, use ops/sec and vCPU count. For serverless, use the estimated_ecpus_sec from the assessment with estimate_serverless_cost tool.
   - Deployment Type Comparison (Node-based vs Serverless):
     * Present BOTH options with real pricing from tools
     * Show a comparison table with columns: Aspect | Node-Based | Serverless
     * Include rows for: Monthly Cost, Scaling, Management Overhead, Best For
     * For Serverless: use estimate_serverless_cost results (ECPU cost + storage cost breakdown)
     * For Node-based: use the recommended Option A cost from estimate_cost
     * Note: ECPUs are the serverless billing metric — the assessment provides estimated_ecpus_sec
     * Note: Serverless minimum data storage is 100 MB for Valkey
     * Clearly state which option is recommended for this workload and WHY (cost, scale pattern, operational preference)
     * If serverless is significantly more expensive, explain the cost driver (usually storage at scale)
     * Add: "For serverless pre-scaling options, see: https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Scaling.html#Pre-Scaling"
     * IMPORTANT — Add a <div class="note"> after the comparison table with this caveat:
       "⚠️ Cost estimates are based on the workload assessment snapshot, which reflects usage at the time the assessment was run. <strong>For best results, ensure the migration assessment was run during peak traffic hours</strong> so that sizing and cost estimates reflect your worst-case workload. For Serverless: when there is no traffic, ECPU charges drop to zero — you only pay for storage. This means Serverless can be significantly cheaper for bursty or intermittent workloads. For Node-based: costs are fixed regardless of traffic — you pay for provisioned nodes 24/7."
   - Valkey version (use the latest from get_latest_valkey_version)
   - Cluster configuration with MULTIPLE OPTIONS (MUST come BEFORE instance type justification):
     * ALWAYS recommend Cluster Mode Enabled (CME) — even for single-shard deployments. CME allows future horizontal scaling (adding shards) without downtime or re-creation. If the source has cluster mode disabled, note that the target should still use CME and advise the customer to verify their application does not use cross-slot multi-key commands without hash tags (e.g., bare MGET across unrelated keys). Note: CMD should only be used if the application is legacy and cannot move to CME at this point.
     * Show the math: raw memory × overhead multiplier = total needed
     * Option A: Balanced (mark as "Recommended Starting Point" or "✅ Recommended")
     * Option B: More shards, smaller instances
     * Option C: Fewer shards, larger instances
     * IMPORTANT: After calling estimate_cost for ALL options, compare the total monthly costs. The option with the LOWEST total monthly cost gets a "💰 Cost-Optimized" label. No other option gets this label. If Option A costs $22,942 and Option B costs $24,458, Option A gets the label — always compare the actual numbers.
     * Compare trade-offs for each option
     * Clearly indicate which option is the primary recommendation
   - Instance type justification (MUST come AFTER cluster configuration options):
     * Why this family? (R vs M vs C)
     * Why this generation? (r7g vs r6g)
     * Why not alternatives? (explain what doesn't fit)
     * Enhanced I/O benefits
   - Sharding strategy
   - Client configuration (recommend Valkey GLIDE)
   - High availability setup:
     * Default: 2 replicas per shard (for HA + read scaling)
     * Read-heavy workloads (>70% reads): Consider 3+ replicas for read distribution
     * Write-heavy workloads: 1-2 replicas sufficient (writes don't benefit from more replicas)
     * Explain replica count reasoning based on read/write ratio
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
       - Any other metrics relevant to the specific workload
     * Provide specific threshold recommendations based on instance type and workload
   - Cost optimization (MUST include ALL of these topics, not just pricing comparison):
     * Reserved Instances savings (1-year and 3-year)
     * Right-sizing review after 2-4 weeks of production monitoring
     * Read replica efficiency — are replicas actively serving reads or idle?
     * Data tiering consideration (r7gd instances with NVMe SSD for large datasets)
     * Latest Graviton generation benefits (price-performance improvements)
     * Snapshot retention cost awareness
     * Cross-AZ data transfer costs for replicas
     * Any other cost optimization tips relevant to the specific workload
   - Migration approach
   - Compatibility Notes (MUST include ALL of these):
     * Valkey API compatibility with source Redis version
     * Redis module check — Valkey natively supports JSON and vector search. However, if the source uses Redis Stack or third-party modules with proprietary APIs, verify those specific APIs are compatible with Valkey's native implementations
     * Check any other source features or commands against the target Valkey version for compatibility
     * Lua scripts and server-side functions compatibility
     * Eviction policy carryover (confirm if source policy works as-is or needs changes)

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
</ul>
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
    parser.add_argument('--file', '-f', 
                        default='migration-assessment-main/examples/output.json',
                        help='Path to migration assessment JSON file')
    parser.add_argument('--region', '-r',
                        default='us-west-2',
                        help='AWS region for Bedrock (default: us-west-2)')
    parser.add_argument('--model', '-m',
                        default='global.anthropic.claude-sonnet-4-6',
                        help='Bedrock model ID (default: Claude Sonnet 4.6)')
    parser.add_argument('--output', '-o',
                        default=None,
                        help='Output HTML file (default: elasticache-recommendations-TIMESTAMP.html)')
    
    args = parser.parse_args()
    
    # Generate unique filename if not specified
    if args.output is None:
        import datetime
        timestamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        args.output = f'elasticache-recommendations-{timestamp}.html'
    
    # Set region
    os.environ['AWS_DEFAULT_REGION'] = args.region
    
    # Create agent with user-specified model
    advisor = Agent(
        name="ElastiCache for Valkey Migration Advisor",
        model=args.model,
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
