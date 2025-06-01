import argparse
import boto3
from datetime import datetime, timedelta, timezone
import sys
from decimal import Decimal
import numpy as np
import pandas as pd
import requests

# Parse command line arguments
parser = argparse.ArgumentParser(description='Collect AWS ElastiCache metrics with specific aggregation rules.')
parser.add_argument('-r', '--region', required=True, help='AWS region for the ElastiCache cluster')
parser.add_argument('-c', '--cluster', required=True, help='ElastiCache cluster ID')
parser.add_argument('-dr', '--day-range', type=int, default=1, help='Day range for hourly metrics collection <1>')
parser.add_argument('-o', '--output', required=False, help='Output CSV file name <output.csv>')
args = parser.parse_args()

if not args.region:
    print("ERROR: Missing region parameter. Please pass in the region name [US-EAST-1, US-EAST-2, and so on]")
    exit(1)

print(f"Region Name: {args.region}")
if not args.cluster:
    print("ERROR: Missing cluster name parameter. Please pass in the cluster name <example-elasti-cache>")
    exit(1)

print(f"Cluster Name: {args.cluster}")

if not args.output:
    args.output = 'cost_estimate_' + args.cluster + '_' + datetime.now(timezone.utc).strftime("%H:%M_%d_%m_%Y") + '.csv'

# Initialize AWS clients
try:
    elasticache = boto3.client('elasticache', region_name=args.region)
    cloudwatch = boto3.client('cloudwatch', region_name=args.region)
    pricing = boto3.client('pricing', region_name='us-east-1')  # Pricing API is only available in us-east-1
except Exception as e:
    print(f"Error initializing AWS client, credential are probably missing: {e}")
    sys.exit(1)

def get_price_list(region_code, currency_code='USD'):
    """
    Fetches ElastiCache pricing data through a 3-step process:
    1. Get price list ARN
    2. Get download URL for price list
    3. Download and return the JSON price data
    """
    try:
        # Step 1: Get Price List ARN
        resp = pricing.list_price_lists(
            ServiceCode='AmazonElastiCache',
            CurrencyCode=currency_code,
            EffectiveDate=datetime.now(timezone.utc),
            RegionCode=region_code,
            MaxResults=1
        )
        
        if not resp['PriceLists']:
            print('No price lists found for the specified parameters.')
            return None
        
        price_list_arn = resp['PriceLists'][0]['PriceListArn']
        
        # Step 2: Get Download URL for Price List File
        url_resp = pricing.get_price_list_file_url(
            PriceListArn=price_list_arn,
            FileFormat='json'
        )
        download_url = url_resp['Url']
        
        # Step 3: Download the Price List JSON
        price_list_json = requests.get(download_url).json()
        return price_list_json
    except Exception as e:
        print(f"Error fetching price list: {e}")
        return None

def get_node_price(price_data, node_type, engine=None):
    """
    Fetch hourly on-demand pricing for a given ElastiCache node type
    
    Args:
        price_data (dict): ElastiCache price list data
        node_type (str): ElastiCache node type (e.g., 'cache.t3.medium')
        engine (str, optional): Cache engine ('redis' or 'valkey')
    
    Returns:
        float: Hourly on-demand price in USD, or None if not found
    """
    try:
        if not price_data:
            return None
            
        # Find the SKU for the specified node type and engine
        node_sku = None
        for sku, product in price_data.get('products', {}).items():
            attributes = product.get('attributes', {})
            
            # Match node type
            if attributes.get('instanceType') != node_type:
                continue
                
            # Match engine if specified
            if engine and engine.lower() in ['redis', 'valkey']:
                product_engine = attributes.get('cacheEngine', '').lower()
                if engine.lower() not in product_engine:
                    continue
            
            node_sku = sku
            break
        
        if not node_sku:
            return None
        
        # Find the on-demand pricing for the SKU
        on_demand_terms = price_data.get('terms', {}).get('OnDemand', {}).get(node_sku, {})
        
        # Get the first offer term (there's typically only one for on-demand)
        if not on_demand_terms:
            return None
            
        offer_term = list(on_demand_terms.values())[0]
        
        # Get the price dimensions
        price_dimensions = offer_term.get('priceDimensions', {})
        if not price_dimensions:
            return None
            
        # Get the first price dimension (there's typically only one for hourly pricing)
        price_dimension = list(price_dimensions.values())[0]
        
        # Extract the price per unit in USD
        price_per_unit = price_dimension.get('pricePerUnit', {}).get('USD')
        
        return float(price_per_unit) if price_per_unit else None
        
    except Exception as e:
        print(f"Error getting node price: {e}")
        return None

def get_serverless_price(price_list_data, engine):
    """
    Extract serverless pricing for a given engine from the price list data
    
    Args:
        price_list_data (dict): ElastiCache price list data
        engine (str): Cache engine ('Redis', 'Valkey', or 'Memcached')
    
    Returns:
        tuple: (cached_data_price, ecpu_price) - Hourly prices for data storage (per GB) and ECPU
    """
    if not price_list_data:
        return None, None
    
    # Initialize prices
    cached_data_price = None
    ecpu_price = None
    
    # Normalize engine name for comparison
    engine = engine.lower()
    
    # Find the SKUs for serverless pricing
    for sku, product in price_list_data.get('products', {}).items():
        if product.get('productFamily') != 'ElastiCache Serverless':
            continue
        
        attributes = product.get('attributes', {})
        product_engine = attributes.get('cacheEngine', '').lower()
        
        # Skip if engine doesn't match
        if engine not in product_engine:
            continue
        
        # Check for CachedData (storage) pricing
        if 'CachedData' in attributes.get('usagetype', ''):
            # Get the on-demand pricing for this SKU
            on_demand_terms = price_list_data.get('terms', {}).get('OnDemand', {}).get(sku, {})
            if on_demand_terms:
                offer_term = list(on_demand_terms.values())[0]
                price_dimensions = offer_term.get('priceDimensions', {})
                if price_dimensions:
                    price_dimension = list(price_dimensions.values())[0]
                    cached_data_price = float(price_dimension.get('pricePerUnit', {}).get('USD', 0))
        
        # Check for ElastiCacheProcessingUnits pricing
        if 'ElastiCacheProcessingUnits' in attributes.get('usagetype', ''):
            # Get the on-demand pricing for this SKU
            on_demand_terms = price_list_data.get('terms', {}).get('OnDemand', {}).get(sku, {})
            if on_demand_terms:
                offer_term = list(on_demand_terms.values())[0]
                price_dimensions = offer_term.get('priceDimensions', {})
                if price_dimensions:
                    price_dimension = list(price_dimensions.values())[0]
                    ecpu_price = float(price_dimension.get('pricePerUnit', {}).get('USD', 0))
    
    return cached_data_price, ecpu_price

def calculate_total_costs(df):
    """Calculate total costs across the date range"""
    total_storage_cost = Decimal(str(df['StorageCost'].sum()))
    total_cpu_cost = Decimal(str(df['eCPUCost'].sum()))
    total_cost = Decimal(str(df['TotalCost'].sum()))
    
    return {
        'storage_cost': total_storage_cost,
        'cpu_cost': total_cpu_cost,
        'total_cost': total_cost,
        'hours_analyzed': len(df)
    }

def get_nodes(cluster_id):
    """Retrieve primary nodes considering cluster mode enabled scenarios."""
    try:
        response = elasticache.describe_replication_groups(ReplicationGroupId=cluster_id)
        all_nodes = response['ReplicationGroups'][0].get('MemberClusters', [])
        return all_nodes
    except Exception as e:
        print(f"Error retrieving cluster node details: {e}")
        sys.exit(1)

def get_metric_data(metric_name, node_id, start_time, end_time, period=3600, stat='Average'):
    """Aggregate metric data for given node ID over the specified time range."""
    aggregated_data = {}
    
    response = cloudwatch.get_metric_statistics(
        Namespace='AWS/ElastiCache',
        MetricName=metric_name,
        Dimensions=[{'Name': 'CacheClusterId', 'Value': node_id}],
        StartTime=start_time,
        EndTime=end_time,
        Period=period,  # default 3600 seconds or 1 hour
        Statistics=[stat],
    )

    for datapoint in response['Datapoints']:
        timestamp = datapoint['Timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        if timestamp not in aggregated_data:
            aggregated_data[timestamp] = datapoint[stat]
        else:
            aggregated_data[timestamp] += datapoint[stat]
    return aggregated_data

def collect_and_write_metrics(cluster_id, start_time, end_time, filename):
    """
    Main processing function that:
    1. Retrieves cluster configuration and pricing
    2. Collects metrics for primary and reader nodes
    3. Calculates costs using serverless pricing model
    4. Generates CSV report with metrics and cost analysis
    """
    primary_nodes = []
    reader_nodes = []
    
    # Get cluster details to determine node type and engine
    try:
        cluster_info = elasticache.describe_replication_groups(ReplicationGroupId=cluster_id)
        if cluster_info['ReplicationGroups']:
            cache_node_type = cluster_info['ReplicationGroups'][0].get('CacheNodeType')
            engine = cluster_info['ReplicationGroups'][0].get('Engine', 'redis')
            print(f"Cache Node Type: {cache_node_type}")
            print(f"Engine: {engine}")
            
            # Get pricing information
            price_list = get_price_list(args.region) #implementation to fetch elasticache pricing info from Pricing API
            node_price = get_node_price(price_list, cache_node_type, engine) # get node type hourly cost in given region
            serverless_data_stored_price, serverless_ecpu_price = get_serverless_price(price_list, engine)# get serverless prucing in the given region. 
            if node_price:
                print(f"Hourly node price: ${node_price}")
            else:
                print("Could not determine node pricing. Using calculated costs only.")
        else:
            print("Could not retrieve cluster details. Using calculated costs only.")
            cache_node_type = None
            engine = None
            node_price = None
    except Exception as e:
        print(f"Error retrieving cluster details: {e}")
        cache_node_type = None
        engine = None
        node_price = None
    
    all_nodes = get_nodes(cluster_id)

    # Identify the primary and read replica nodes
    try:
        # Generate a list of current primary and read replica nodes
        # based on the role each cluster node played in the last minute
        l_start_time = end_time - timedelta(minutes=1)
        for node in all_nodes:
            aggregated_data = get_metric_data('IsMaster', node, l_start_time, end_time, 60, 'Sum')
            if next(iter(aggregated_data.values())) == 1.0:
                primary_nodes.append(node)
            else:
                reader_nodes.append(node)
    except Exception as e:
        print(f"No metrics exist for cluster: {cluster_id}")
        sys.exit(1)

    primary_node = primary_nodes[0]
    print("Primary node used: " + primary_node)

    if len(reader_nodes) > 1:
        reader_node = reader_nodes[0]
        print("Reader node used: " + reader_node)
    else:
        reader_node = None

    num_shards = len(primary_nodes)
    num_readers = len(reader_nodes)
    num_replicas_per_shard = int(num_readers/num_shards)
    total_nodes=num_shards+num_readers
    node_based_cost=node_price*(total_nodes)

    print("Number of primaries: " + str(num_shards))
    print("Number of replicas: " + str(num_readers))
    print("total nodes: " +str(total_nodes))
    print(f"Current node based cluster hourly cost: ${node_based_cost:.2f}")
    print(f"Current node based cluster daily cost: ${(node_based_cost*24):.2f}")

    # Prepare data structure for CSV writing
    collected_data = {}

    # For a primary node collect the following metrics
    for metric in ['BytesUsedForCache', 'EvalBasedCmds', 'EvalBasedCmdsLatency', 'GetTypeCmds', 
                   'NetworkBytesIn', 'NetworkBytesOut', 'ReplicationBytes', 'SetTypeCmds']:
        # Retrieve the average for the below metrics
        if metric in ['BytesUsedForCache', 'EvalBasedCmdsLatency']:
            aggregated_data = get_metric_data(metric, primary_node, start_time, end_time, stat='Average')
        # The sum for the rest of the metrics
        else:
            aggregated_data = get_metric_data(metric, primary_node, start_time, end_time, stat='Sum')

        for timestamp, value in aggregated_data.items():
            if timestamp not in collected_data:
                collected_data[timestamp] = {}
            collected_data[timestamp][metric] = value

    # For a read replica node only the GetTypeCmds and NetworkBytesOut metrics are needed
    if reader_node is not None:
        for metric in ['GetTypeCmds', 'NetworkBytesOut']:
            aggregated_data = get_metric_data(metric, reader_node, start_time, end_time, stat='Sum')
            reader_metric = 'Reader' + metric
            for timestamp, value in aggregated_data.items():
                collected_data[timestamp][reader_metric] = value

    dataKeys = list(collected_data.keys())
    dataKeys.sort()
    sorted_collected_data = {i: collected_data[i] for i in dataKeys}

    pd.set_option('display.max_rows', None)
    df = pd.DataFrame(sorted_collected_data)
    df = df.transpose()

    # Since certain fields might not be populated, for lack of data, set them to 0
    df['GetTypeCmds'] = df.get('GetTypeCmds', 0)
    df['SetTypeCmds'] = df.get('SetTypeCmds', 0)
    df['EvalBasedCmds'] = df.get('EvalBasedCmds', 0)
    df['EvalBasedCmdsLatency'] = df.get('EvalBasedCmdsLatency', 0)
    df['ReaderGetTypeCmds'] = df.get('ReaderGetTypeCmds', 0)
    df['ReaderNetworkBytesOut'] = df.get('ReaderNetworkBytesOut', 0)
    df['ReplicationBytes'] = df.get('ReplicationBytes', 0)
    df = df.fillna(0)

    columns = ['BytesUsedForCache', 'EvalBasedCmds', 'EvalBasedCmdsLatency', 'GetTypeCmds', 
               'ReaderGetTypeCmds', 'NetworkBytesIn', 'NetworkBytesOut', 'ReaderNetworkBytesOut', 
               'ReplicationBytes', 'SetTypeCmds']
    df = df[columns]
    
    df['TotalSizeMB'] = df['BytesUsedForCache'].div(1000*1000).mul(num_shards).round(2).apply(lambda x : "{:,}".format(x))
    df['EvaleCPU'] = (df['EvalBasedCmds'].mul(num_shards) * df['EvalBasedCmdsLatency'].div(2)).astype(int)
    df['EvalBasedCmds'] = df['EvalBasedCmds'].mul(num_shards)
    
    # Prevent division by 0
    df['AVGInSize'] = np.where(df['SetTypeCmds'] == 0, 0,
                              df['NetworkBytesIn'].div(1000)/df['SetTypeCmds'].astype(int))
    
    df['PrimaryIneCPU'] = np.where((df['AVGInSize'] > 0) & (df['AVGInSize'] < 1), 
                                  df['SetTypeCmds'] * num_shards,
                                  df['SetTypeCmds'] * df['AVGInSize'] * num_shards)

    df['GetTypeCmds'] = df['GetTypeCmds'].astype(int)
    df['ReaderGetTypeCmds'] = df['ReaderGetTypeCmds'].astype(int)

    df['AVGOutSize'] = np.where(df['GetTypeCmds'] == 0, 0,
                               ((df['NetworkBytesOut'].div(1000))-
                                (df['ReplicationBytes'].div(1000).mul(num_replicas_per_shard)))/   
                               df['GetTypeCmds'].astype(int))  # calculating per shard data so using num_replicas_per_shard (changed)

    df['ReaderAVGOutSize'] = np.where(df['ReaderGetTypeCmds'] == 0, 0,
                                     df['ReaderNetworkBytesOut'].div(1000)/
                                     df['ReaderGetTypeCmds'].astype(int))

    df['PrimaryOuteCPU'] = np.where((df['AVGOutSize'] > 0) & (df['AVGOutSize'] < 1), 
                                   df['GetTypeCmds'] * num_shards,
                                   df['GetTypeCmds'] * df['AVGOutSize'] * num_shards)

    df['ReaderOuteCPU'] = np.where((df['ReaderAVGOutSize'] > 0) & (df['ReaderAVGOutSize'] <= 1), 
                                  df['ReaderGetTypeCmds'].astype(int) * num_replicas_per_shard,
                                  df['ReaderGetTypeCmds'].astype(int) * df['ReaderAVGOutSize'] * num_replicas_per_shard)

    df['SetTypeCmds'] = df['SetTypeCmds'].astype(int)

    # Minimum storage cost is for 100MB (changed)
    df['StorageCost'] = np.where(df['BytesUsedForCache'].div(1000*1000).mul(num_shards).round(4) <= 100, 
                                (0.1*serverless_data_stored_price),
                                df['BytesUsedForCache'].div(1000*1000*1000).mul(num_shards).mul(serverless_data_stored_price).round(2))

    df['eCPUCost'] = (df['EvaleCPU'].mul(serverless_ecpu_price).apply(lambda x: round(x, 4)) + 
                      df['PrimaryIneCPU'].mul(serverless_ecpu_price).apply(lambda x: round(x, 4)) + 
                      df['PrimaryOuteCPU'].mul(serverless_ecpu_price).apply(lambda x: round(x, 4)) + 
                      df['ReaderOuteCPU'].mul(serverless_ecpu_price).apply(lambda x: round(x, 4)))
    
    print(f"serverless storage pricing is ${serverless_data_stored_price}"+" "+"GB-Hour")
    print(f"serverless ecpu pricing is ${serverless_ecpu_price}")
    
    df['TotalCost'] = (df['StorageCost'] + df['eCPUCost']).round(3)

    print("")
    print(df[['TotalSizeMB', 'EvaleCPU', 'PrimaryIneCPU', 'PrimaryOuteCPU', 'ReaderOuteCPU', 
              'StorageCost', 'eCPUCost', 'TotalCost']])

    # Calculate total costs across the date range
    total_costs = calculate_total_costs(df)
    
    print("\nSummary for the entire period:")
    print(f"Total Hours Analyzed: {total_costs['hours_analyzed']}")
    print(f"Total Cost: ${total_costs['total_cost']:.3f}")
    
    # Add summary rows to DataFrame
    summary_rows = [
        pd.Series({
            'TotalSizeMB': '',
            'StorageCost': '',
            'eCPUCost': '',
            'TotalCost': ''
        }, name=''),
        pd.Series({
            'TotalSizeMB': 'SUMMARY',
            'StorageCost': '',
            'eCPUCost': '',
            'TotalCost': ''
        }, name='Summary Statistics'),
        pd.Series({
            'TotalSizeMB': f'Total Hours Analyzed: {total_costs["hours_analyzed"]}',
            'StorageCost': '',
            'eCPUCost': '',
            'TotalCost': ''
        }, name=''),
        pd.Series({
            'TotalSizeMB': f'Total Cost: ${total_costs["total_cost"]:.3f}',
            'StorageCost': '',
            'eCPUCost': '',
            'TotalCost': ''
        }, name='')
    ]

    # Concatenate the original DataFrame with the summary rows
    df = pd.concat([df] + [pd.DataFrame([row]) for row in summary_rows])

    # Write to CSV
    with open(filename, 'w', newline='') as csvfile:
        df.to_csv(csvfile, index=True)

if __name__ == '__main__':
    end_time = datetime.now(timezone.utc)
    end_time = end_time.replace(minute=0, second=0)
    start_time = end_time - timedelta(days=args.day_range)
    print('Start time: ' + str(start_time))
    print('End time: ' + str(end_time))
    print('Region: ' + args.region)
    collect_and_write_metrics(args.cluster, start_time, end_time, args.output)