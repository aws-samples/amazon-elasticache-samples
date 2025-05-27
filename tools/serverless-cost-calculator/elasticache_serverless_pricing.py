"""
ElastiCache Serverless pricing dictionary by AWS region.
Contains pricing for ECPU (per 1M ECPUs) and Storage (per GB per hour).
Pricing as of the creation date and may change. Refer to AWS pricing page for latest information.
"""

ELASTICACHE_SERVERLESS_PRICING = {
    # US Regions
    "us-east-1": {  # US East (N. Virginia)
        "ecpu_per_million": 2.30,
        "storage_per_gb": 0.084
    },
    "us-east-2": {  # US East (Ohio)
        "ecpu_per_million": 2.30,
        "storage_per_gb": 0.084
    },
    "us-west-1": {  # US West (N. California)
        "ecpu_per_million": 2.76,
        "storage_per_gb": 0.101
    },
    "us-west-2": {  # US West (Oregon)
        "ecpu_per_million": 2.30,
        "storage_per_gb": 0.084
    },
    
    # Canada Region
    "ca-central-1": {  # Canada (Central)
        "ecpu_per_million": 2.53,
        "storage_per_gb": 0.092
    },
    
    # South America Region
    "sa-east-1": {  # South America (São Paulo)
        "ecpu_per_million": 3.45,
        "storage_per_gb": 0.126
    },
    
    # Europe Regions
    "eu-central-1": {  # Europe (Frankfurt)
        "ecpu_per_million": 2.53,
        "storage_per_gb": 0.092
    },
    "eu-west-1": {  # Europe (Ireland)
        "ecpu_per_million": 2.30,
        "storage_per_gb": 0.084
    },
    "eu-west-2": {  # Europe (London)
        "ecpu_per_million": 2.42,
        "storage_per_gb": 0.088
    },
    "eu-west-3": {  # Europe (Paris)
        "ecpu_per_million": 2.42,
        "storage_per_gb": 0.088
    },
    "eu-north-1": {  # Europe (Stockholm)
        "ecpu_per_million": 2.19,
        "storage_per_gb": 0.080
    },
    "eu-south-1": {  # Europe (Milan)
        "ecpu_per_million": 2.53,
        "storage_per_gb": 0.092
    },
    
    # Asia Pacific Regions
    "ap-east-1": {  # Asia Pacific (Hong Kong)
        "ecpu_per_million": 3.00,
        "storage_per_gb": 0.109
    },
    "ap-south-1": {  # Asia Pacific (Mumbai)
        "ecpu_per_million": 2.53,
        "storage_per_gb": 0.092
    },
    "ap-northeast-1": {  # Asia Pacific (Tokyo)
        "ecpu_per_million": 2.76,
        "storage_per_gb": 0.101
    },
    "ap-northeast-2": {  # Asia Pacific (Seoul)
        "ecpu_per_million": 2.53,
        "storage_per_gb": 0.092
    },
    "ap-northeast-3": {  # Asia Pacific (Osaka)
        "ecpu_per_million": 2.76,
        "storage_per_gb": 0.101
    },
    "ap-southeast-1": {  # Asia Pacific (Singapore)
        "ecpu_per_million": 2.76,
        "storage_per_gb": 0.101
    },
    "ap-southeast-2": {  # Asia Pacific (Sydney)
        "ecpu_per_million": 2.76,
        "storage_per_gb": 0.101
    },
    
    # Middle East Regions
    "me-south-1": {  # Middle East (Bahrain)
        "ecpu_per_million": 2.76,
        "storage_per_gb": 0.101
    },
    
    # Africa Regions
    "af-south-1": {  # Africa (Cape Town)
        "ecpu_per_million": 2.76,
        "storage_per_gb": 0.101
    }
}

def get_pricing(region):
    """
    Get ElastiCache Serverless pricing for a specific region.
    
    Args:
        region (str): AWS region code (e.g., 'us-east-1')
        
    Returns:
        dict: Pricing information for the region or None if region not found
    """
    return ELASTICACHE_SERVERLESS_PRICING.get(region.lower())

def calculate_cost(region, ecpu_usage_millions, storage_gb_hours):
    """
    Calculate ElastiCache Serverless cost for given usage.
    
    Args:
        region (str): AWS region code (e.g., 'us-east-1')
        ecpu_usage_millions (float): ECPU usage in millions
        storage_gb_hours (float): Storage usage in GB-hours
        
    Returns:
        dict: Cost breakdown or None if region not found
    """
    pricing = get_pricing(region)
    if not pricing:
        return None
        
    ecpu_cost = ecpu_usage_millions * pricing["ecpu_per_million"]
    storage_cost = storage_gb_hours * pricing["storage_per_gb"]
    
    return {
        "ecpu_cost": round(ecpu_cost, 3),
        "storage_cost": round(storage_cost, 3),
        "total_cost": round(ecpu_cost + storage_cost, 3)
    }