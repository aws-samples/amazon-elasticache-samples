import argparse
import boto3
import csv
from datetime import datetime, timedelta
import sys
import string, random

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
    args.output = 'cost_estimate_' + args.cluster + '_' + datetime.now().strftime("%H:%M_%d_%m_%Y") + '.csv'

# Initialize AWS clients
try:

    elasticache = boto3.client('elasticache', region_name=args.region)
    cloudwatch = boto3.client('cloudwatch', region_name=args.region)

except Exception as e:
        print(f"Error initializing AWS client, credential are probably missing: {e}")
        sys.exit(1)


# Function to get the all nodes of a cluster
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
        #  Unit='Megabytes',

    for datapoint in response['Datapoints']:
            timestamp = datapoint['Timestamp'].strftime('%Y-%m-%d %H:%M:%S')
            if timestamp not in aggregated_data:
                aggregated_data[timestamp] = datapoint[stat]
            else:
                # print('datapoint in aggreage accumulate data')
                aggregated_data[timestamp] += datapoint[stat]
    return aggregated_data


def collect_and_write_metrics(cluster_id, start_time, end_time, filename):
    """Main function that collects, displays, and saves metrics data to a  csv file."""

    primary_nodes = []
    reader_nodes = []

    all_nodes = get_nodes(cluster_id)

    # Identify the primary and read replica nodes
    try:
      # Generate a list of current primary and read replica nodes
      # based on the role each cluster node played in the last minute
      l_start_time = end_time - timedelta(minutes=1)
      for node in all_nodes:
          aggregated_data = get_metric_data('IsMaster', node, l_start_time, end_time, 60, 'Sum')
          #print(list(aggregated_data.values())[0])

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
    num_replicas = int(num_readers/num_shards)

    print("Number of primaries: " + str(num_shards))
    print("Number of replicas: " + str(num_replicas))

    # Prepare data structure for CSV writing
    collected_data = {}

    # For a primary node collect the following metrics
    for metric in ['NetworkBytesOut', 'BytesUsedForCache', 'EvalBasedCmds', 'EvalBasedCmdsLatency', 'GetTypeCmds', 'NetworkBytesIn', 'NetworkBytesOut', 'ReplicationBytes', 'SetTypeCmds']:

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
    # and are stored in special Reader<metricName> fields
    if reader_node is not None:
        for metric in ['GetTypeCmds', 'NetworkBytesOut']:
            aggregated_data = get_metric_data(metric, reader_node, start_time, end_time, stat='Sum')
            reader_metric = 'Reader' + metric
            for timestamp, value in aggregated_data.items():
                collected_data[timestamp][reader_metric] = value

    dataKeys = list(collected_data.keys())
    dataKeys.sort()
    sorted_collected_data = {i: collected_data[i] for i in dataKeys}

    # At this point we have all the data sorted and ready to calculate

    import numpy as np
    import pandas as pd

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

    columns = ['BytesUsedForCache', 'EvalBasedCmds', 'EvalBasedCmdsLatency', 'GetTypeCmds', 'ReaderGetTypeCmds', 'NetworkBytesIn', 'NetworkBytesOut', 'ReaderNetworkBytesOut', 'ReplicationBytes', 'SetTypeCmds']
    df = df[columns]
    df['TotalSizeMB'] = df['BytesUsedForCache'].div(1000*1000).mul(num_shards).round(2).apply(lambda x : "{:,}".format(x))
    df['EvaleCPU'] = (df['EvalBasedCmds'].mul(num_shards) * df['EvalBasedCmdsLatency'].div(2)).astype(int)
    df['EvalBasedCmds'] = df['EvalBasedCmds'].mul(num_shards)
    # Prevent division by 0
    df['AVGInSize'] = np.where(df['SetTypeCmds'] == 0, 0,
                            df['NetworkBytesIn'].div(1000)/df['SetTypeCmds'].astype(int))
    # If avg inbound size is >0< 1 KB round to 1 by using the number of SetTypeCmds
    df['PrimaryIneCPU'] = np.where((df['AVGInSize'] > 0) & (df['AVGInSize'] < 1), df['SetTypeCmds'] * num_shards,
                            df['SetTypeCmds'] * df['AVGInSize'] * num_shards)

    df['GetTypeCmds'] = df['GetTypeCmds'].astype(int)
    df['ReaderGetTypeCmds'] = df['ReaderGetTypeCmds'].astype(int)

    # Prevent division by 0
    # Remove replication bytes from primary node only as that does not count
    df['AVGOutSize'] = np.where(df['GetTypeCmds'] == 0, 0,
                               ((df['NetworkBytesOut'].div(1000))-(df['ReplicationBytes'].div(1000).mul(num_readers)))/df['GetTypeCmds'].astype(int))
    # Not removing replication  df['NetworkBytesOut'].div(1000)/df['GetTypeCmds'].astype(int))

    # Prevent division by 0
    df['ReaderAVGOutSize'] = np.where(df['ReaderGetTypeCmds'] == 0, 0,
                             df['ReaderNetworkBytesOut'].div(1000)/df['ReaderGetTypeCmds'].astype(int))

    # If avg outbound size is >0 and <1 KB round to 1 by using the number of SetTypeCmds
    df['PrimaryOuteCPU'] = np.where((df['AVGOutSize'] > 0) & (df['AVGOutSize'] < 1), df['GetTypeCmds'] * num_shards,
                              df['GetTypeCmds'] * df['AVGOutSize'] * num_shards)

    # Do the same for the reader but multiply by the number of readers
    df['ReaderOuteCPU'] = np.where((df['ReaderAVGOutSize'] > 0) & (df['ReaderAVGOutSize'] <= 1), df['ReaderGetTypeCmds'].astype(int) * num_readers,
                            df['ReaderGetTypeCmds'].astype(int) * df['ReaderAVGOutSize'] * num_readers)

    df['SetTypeCmds'] = df['SetTypeCmds'].astype(int)

    # Minimum storage cost is for 100MB
    df['StorageCost'] = np.where(df['BytesUsedForCache'].div(1000*1000).mul(num_shards).round(4) <= 100, (0.00825), \
                                 df['BytesUsedForCache'].div(1000*1000*1000).mul(num_shards).mul(0.08125).round(2))

    df['eCPUCost'] = df['EvaleCPU'].mul(0.0000000034).apply(lambda x: round(x, 4)) + \
                     df['PrimaryIneCPU'].mul(0.0000000034).apply(lambda x: round(x, 4)) + \
                     df['PrimaryOuteCPU'].mul(0.0000000034).apply(lambda x: round(x, 4)) + \
                     df['ReaderOuteCPU'].mul(0.0000000034).apply(lambda x: round(x, 4))
    df['TotalCost'] = (df['StorageCost'] + df['eCPUCost']).round(3)

    # df.index.name = 'Date Time'
    print("")
    # print(df[['TotalSizeMB', 'GetTypeCmds', 'NetworkBytesOut', 'ReaderGetTypeCmds', 'ReaderNetworkBytesOut', 'ReaderAVGOutSize', 'SetTypeCmds', 'EvalBasedCmds', 'StorageCost', 'eCPUCost', 'TotalCost']])
    print(df[['TotalSizeMB', 'EvaleCPU', 'PrimaryIneCPU', 'PrimaryOuteCPU', 'ReaderOuteCPU', 'StorageCost', 'eCPUCost', 'TotalCost']])

    with open(filename, 'w', newline='') as csvfile:
        df.to_csv(csvfile, index=True)

if __name__ == '__main__':
    end_time = datetime.utcnow()
    end_time = end_time.replace(minute=0, second=0)
    start_time = end_time - timedelta(days=args.day_range)
    print('Start time: ' + str(start_time))
    print('End time: ' + str(end_time))
    collect_and_write_metrics(args.cluster, start_time, end_time, args.output)