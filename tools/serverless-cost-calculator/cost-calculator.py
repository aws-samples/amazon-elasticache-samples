#!/usr/bin/env python3
"""
ElastiCache Cost Report Generator

This script generates cost comparison reports between node-based and serverless ElastiCache deployments.
It collects metrics from CloudWatch and pricing information to provide detailed cost analysis.
"""

import argparse
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional, Any

import boto3
import pandas as pd
import numpy as np
import requests

# Constants
HOUR_IN_MONTH = 730
MIN_STORAGE_GB = 0.1
DEFAULT_PERIOD = 3600
BYTES_TO_GB = 1024 ** 3

# Metric configurations
METRICS = {
    'primary': [
        ('BytesUsedForCache', 'Average'),
        ('EvalBasedCmds', 'Sum'),
        ('EvalBasedCmdsLatency', 'Average'),
        ('GetTypeCmds', 'Sum'),
        ('NetworkBytesIn', 'Sum'),
        ('NetworkBytesOut', 'Sum'),
        ('ReplicationBytes', 'Sum'),
        ('SetTypeCmds', 'Sum')
    ],
    'replicas_per_shard': [
        ('GetTypeCmds', 'Sum'),
        ('NetworkBytesOut', 'Sum')
    ]
}

class ElastiCacheCostCalculator:
    """Handles cost calculation for ElastiCache clusters."""

    def __init__(self, region: str, cluster_id: str):
        """Initialize the calculator with region and cluster ID."""
        self.region = region
        self.cluster_id = cluster_id
        self.elasticache = boto3.client('elasticache', region_name=region)
        self.cloudwatch = boto3.client('cloudwatch', region_name=region)
        self.pricing = boto3.client('pricing', region_name='us-east-1')
        self.logger = self._setup_logger()

    @staticmethod
    def _setup_logger() -> logging.Logger:
        """Set up logging configuration."""
        logger = logging.getLogger('ElastiCacheCostCalculator')
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)
        return logger

    def get_price_list(self) -> dict:
        """Retrieve the price list for ElastiCache in the specified region."""
        try:
            resp = self.pricing.list_price_lists(
                ServiceCode='AmazonElastiCache',
                CurrencyCode='USD',
                EffectiveDate=datetime.now(timezone.utc),
                RegionCode=self.region,
                MaxResults=1
            )
            arn = resp['PriceLists'][0]['PriceListArn']
            url = self.pricing.get_price_list_file_url(PriceListArn=arn, FileFormat='json')['Url']
            return requests.get(url).json()
        except Exception as e:
            self.logger.error(f"Failed to retrieve price list: {str(e)}")
            raise

    def get_node_price(self, data: dict, node_type: str, engine: Optional[str] = None) -> Optional[float]:
        """Get the price per hour for a specific node type."""
        for sku, product in data.get('products', {}).items():
            attrs = product.get('attributes', {})
            if (attrs.get('instanceType') == node_type and
                (not engine or engine.lower() in attrs.get('cacheEngine', '').lower())):
                term = list(data['terms']['OnDemand'][sku].values())[0]
                price_dim = list(term['priceDimensions'].values())[0]
                return float(price_dim['pricePerUnit']['USD'])
        return None

    def get_serverless_price(self, data: dict, engine: str) -> Tuple[Optional[float], Optional[float]]:
        """Get serverless pricing for storage and eCPU."""
        cached, ecpu = None, None
        for sku, product in data.get('products', {}).items():
            if (product['productFamily'] == 'ElastiCache Serverless' and
                engine.lower() in product['attributes'].get('cacheEngine', '').lower()):
                attrs = product['attributes']
                term = list(data['terms']['OnDemand'][sku].values())[0]
                price = float(list(term['priceDimensions'].values())[0]['pricePerUnit']['USD'])

                if 'CachedData' in attrs['usagetype']:
                    cached = price
                elif 'ElastiCacheProcessingUnits' in attrs['usagetype']:
                    ecpu = price
        return cached, ecpu

    def get_metric_data(self, metric: str, node_id: str, start: datetime,
                       end: datetime, period: int = DEFAULT_PERIOD, stat: str = 'Average') -> Dict[str, float]:
        """Retrieve metric data from CloudWatch."""
        try:
            response = self.cloudwatch.get_metric_statistics(
                Namespace='AWS/ElastiCache',
                MetricName=metric,
                Dimensions=[{'Name': 'CacheClusterId', 'Value': node_id}],
                StartTime=start,
                EndTime=end,
                Period=period,
                Statistics=[stat]
            )
            return {p['Timestamp'].strftime('%Y-%m-%d %H:%M:%S'): p[stat]
                   for p in response['Datapoints']}
        except Exception as e:
            self.logger.error(f"Failed to get metric data for {metric}: {str(e)}")
            return {}

    def get_cluster_nodes(self) -> Dict[str, Any]:
        """Get cluster nodes and their roles."""
        try:
            # Get all nodes in the cluster
            response = self.elasticache.describe_replication_groups(
                ReplicationGroupId=self.cluster_id
            )
            all_nodes = response['ReplicationGroups'][0].get('MemberClusters', [])

            # Initialize lists for primary and replica nodes
            primary_nodes = []
            replica_nodes = []

            # Check the role each node played in the last minute
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(minutes=1)

            for node in all_nodes:
                metric_data = self.get_metric_data('IsMaster', node, start_time, end_time, 60, 'Sum')
                if metric_data and next(iter(metric_data.values())) == 1.0:
                    primary_nodes.append(node)
                else:
                    replica_nodes.append(node)

            # Calculate cluster structure
            num_shards = len(primary_nodes)
            num_replicas = len(replica_nodes)
            num_replicas_per_shard = int(num_replicas/num_shards) if num_shards > 0 else 0
            total_nodes = num_shards + num_replicas

            self.logger.info(f"Found {num_shards} primaries and {num_replicas} replicas ({num_replicas_per_shard} per shard)")

            return {
                'primary': primary_nodes,
                'replicas_per_shard': replica_nodes,
                'num_shards': num_shards,
                'num_replicas': num_replicas,
                'num_replicas_per_shard': num_replicas_per_shard,
                'total_nodes': total_nodes
            }
        except Exception as e:
            self.logger.error(f"Failed to get cluster nodes: {str(e)}")
            raise

    def get_cluster_info(self) -> Tuple[str, str]:
        """Get cluster node type and engine."""
        try:
            info = self.elasticache.describe_replication_groups(
                ReplicationGroupId=self.cluster_id
            )['ReplicationGroups'][0]

            node_type = info['CacheNodeType']
            engine = info.get('Engine')

            if not engine:
                node_id = info.get('MemberClusters', [])[0]
                node_info = self.elasticache.describe_cache_clusters(
                    CacheClusterId=node_id,
                    ShowCacheNodeInfo=False
                )
                engine = node_info['CacheClusters'][0]['Engine']

            return node_type, engine
        except Exception as e:
            self.logger.error(f"Failed to get cluster info: {str(e)}")
            raise

    def collect_metrics(self, start: datetime, end: datetime) -> pd.DataFrame:
        """Collect all required metrics for cost calculation."""
        nodes = self.get_cluster_nodes()
        collected = {}

        # Initialize all expected columns with zeros
        expected_columns = [metric for metric, _ in METRICS['primary']]
        expected_columns.extend([f'ReplicasPerShard{metric}' for metric, _ in METRICS['replicas_per_shard']])

        # Collect primary node metrics
        for primary in nodes['primary']:
            for metric, stat in METRICS['primary']:
                data = self.get_metric_data(metric, primary, start, end, DEFAULT_PERIOD, stat)
                for t, v in data.items():
                    collected.setdefault(t, {metric: 0 for metric in expected_columns})
                    # Aggregate metrics from all primaries
                    existing = collected[t].get(metric, 0)
                    collected[t][metric] = existing + v

        # Collect replica node metrics if available
        if nodes['replicas_per_shard']:
            for replica in nodes['replicas_per_shard']:
                for metric, stat in METRICS['replicas_per_shard']:
                    data = self.get_metric_data(metric, replica, start, end, DEFAULT_PERIOD, stat)
                    for t, v in data.items():
                        if t not in collected:
                            collected[t] = {metric: 0 for metric in expected_columns}
                        # Aggregate metrics from all replicas
                        existing = collected[t].get(f'ReplicasPerShard{metric}', 0)
                        collected[t][f'ReplicasPerShard{metric}'] = existing + v

        df = pd.DataFrame.from_dict(collected, orient='index').fillna(0)

        # Ensure all expected columns exist
        for col in expected_columns:
            if col not in df.columns:
                df[col] = 0

        return df

    def calculate_costs(self, df: pd.DataFrame, storage_price: float,
                       ecpu_price: float, node_price: float) -> pd.DataFrame:
        """Calculate costs based on collected metrics."""
        nodes = self.get_cluster_nodes()
        num_shards = nodes['num_shards']
        num_replicas_per_shard = nodes['num_replicas_per_shard']

        # Calculate total size in MB per shard
        df['TotalSizeMB'] = df['BytesUsedForCache'].div(1000*1000).mul(num_shards).round(2)

        # Calculate eCPU usage
        df['EvaleCPU'] = (df['EvalBasedCmds'].mul(num_shards) * df['EvalBasedCmdsLatency'].div(2)).astype(int)
        df['EvalBasedCmds'] = df['EvalBasedCmds'].mul(num_shards)

        # Calculate average input size and primary input eCPU
        df['AVGInSize'] = np.where(df['SetTypeCmds'] == 0, 0,
                                  df['NetworkBytesIn'].div(1000)/df['SetTypeCmds'].astype(int))
        df['PrimaryIneCPU'] = np.where((df['AVGInSize'] > 0) & (df['AVGInSize'] < 1),
                                      df['SetTypeCmds'] * num_shards,
                                      df['SetTypeCmds'] * df['AVGInSize'] * num_shards)

        # Calculate average output sizes and eCPU for primary and replicas
        df['AVGOutSize'] = np.where(df['GetTypeCmds'] == 0, 0,
                                   ((df['NetworkBytesOut'].div(1000)) -
                                    (df['ReplicationBytes'].div(1000).mul(num_replicas_per_shard)))/
                                   df['GetTypeCmds'].astype(int))

        df['ReaderAVGOutSize'] = np.where(df['ReplicasPerShardGetTypeCmds'] == 0, 0,
                                         df['ReplicasPerShardNetworkBytesOut'].div(1000)/
                                         df['ReplicasPerShardGetTypeCmds'].astype(int))

        df['PrimaryOuteCPU'] = np.where((df['AVGOutSize'] > 0) & (df['AVGOutSize'] < 1),
                                       df['GetTypeCmds'] * num_shards,
                                       df['GetTypeCmds'] * df['AVGOutSize'] * num_shards)

        df['ReaderOuteCPU'] = np.where((df['ReaderAVGOutSize'] > 0) & (df['ReaderAVGOutSize'] <= 1),
                                      df['ReplicasPerShardGetTypeCmds'].astype(int) * num_replicas_per_shard,
                                      df['ReplicasPerShardGetTypeCmds'].astype(int) * df['ReaderAVGOutSize'] * num_replicas_per_shard)

        # Calculate costs
        # Minimum storage cost is for 100MB
        df['StorageCost'] = np.where(df['BytesUsedForCache'].div(1000*1000).mul(num_shards).round(4) <= 100,
                                    (0.1 * storage_price),
                                    df['BytesUsedForCache'].div(1000*1000*1000).mul(num_shards).mul(storage_price).round(2))

        df['eCPUCost'] = (df['EvaleCPU'].mul(ecpu_price).apply(lambda x: round(x, 4)) +
                         df['PrimaryIneCPU'].mul(ecpu_price).apply(lambda x: round(x, 4)) +
                         df['PrimaryOuteCPU'].mul(ecpu_price).apply(lambda x: round(x, 4)) +
                         df['ReaderOuteCPU'].mul(ecpu_price).apply(lambda x: round(x, 4)))

        df['TotalCost'] = (df['StorageCost'] + df['eCPUCost']).round(3)

        # Add node-based costs for comparison
        total_nodes = nodes['total_nodes']
        df['NodeBasedCost'] = node_price * total_nodes
        df['NodeMonthly'] = node_price * HOUR_IN_MONTH * total_nodes
        df['ServerlessMonthly'] = df['TotalCost'].mean() * HOUR_IN_MONTH

        return df

    def generate_report(self, start: datetime, end: datetime, output_file: str):
        """Generate the cost comparison report."""
        try:
            # Get pricing information
            prices = self.get_price_list()
            node_type, engine = self.get_cluster_info()
            node_price = self.get_node_price(prices, node_type, engine)
            storage_price, ecpu_price = self.get_serverless_price(prices, 'valkey')

            self.logger.info(
                f"Pricing info - Node/hr: ${node_price}, "
                f"Serverless Storage/hr/GB: ${storage_price}, "
                f"eCPU unit: ${ecpu_price} (~${ecpu_price * 1_000_000:.6f} per million)"
            )

            # Collect and process metrics
            df = self.collect_metrics(start, end)
            df = self.calculate_costs(df, storage_price, ecpu_price, node_price)

            # Get node counts
            nodes = self.get_cluster_nodes()
            num_nodes = len(nodes['primary']) + len(nodes['replicas_per_shard'])

            # Generate summary
            summary = pd.DataFrame({
                'Total Hours': [len(df)],
                'Total Nodes': [num_nodes],
                'Primary Nodes': [len(nodes['primary'])],
                'Replica Nodes': [len(nodes['replicas_per_shard'])],
                'NodeBased/hr': [node_price * num_nodes],
                'NodeBased/month': [node_price * HOUR_IN_MONTH * num_nodes],
                'Serverless/hr (avg)': [df['TotalCost'].mean()],
                'Serverless/month': [df['TotalCost'].mean() * HOUR_IN_MONTH]
            })

            # Save to Excel
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='Hourly Details')
                summary.to_excel(writer, sheet_name='Cost Comparison', index=False)

            self.logger.info(f"Excel report saved: {output_file}")

        except Exception as e:
            self.logger.error(f"Failed to generate report: {str(e)}")
            raise

def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(description='Generate ElastiCache cost comparison report')
    parser.add_argument('-r', '--region', required=True, help='AWS region')
    parser.add_argument('-c', '--cluster', required=True, help='ElastiCache cluster ID')
    parser.add_argument('-dr', '--day-range', type=int, default=1, help='Number of days to analyze')
    parser.add_argument('-o', '--output', help='Output file path')

    args = parser.parse_args()

    if not args.output:
        args.output = (f"elasticache_cost_{args.cluster}_"
                      f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.xlsx")

    end = datetime.now(timezone.utc).replace(minute=0, second=0)
    start = end - timedelta(days=args.day_range)

    calculator = ElastiCacheCostCalculator(args.region, args.cluster)
    calculator.generate_report(start, end, args.output)

if __name__ == '__main__':
    main()
