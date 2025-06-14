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
    'reader': [
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

    def get_cluster_nodes(self) -> Dict[str, str]:
        """Get primary and reader nodes for the cluster."""
        try:
            nodes = self.elasticache.describe_replication_groups(
                ReplicationGroupId=self.cluster_id
            )['ReplicationGroups'][0]['MemberClusters']
            
            roles = {'primary': None, 'reader': None}
            check_time = datetime.now(timezone.utc) - timedelta(minutes=1)
            
            for node in nodes:
                metric = self.get_metric_data('IsMaster', node, check_time, 
                                            datetime.now(timezone.utc), 60, 'Sum')
                if metric and list(metric.values())[0] == 1.0:
                    roles['primary'] = node
                else:
                    roles['reader'] = node
            
            return roles
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
        expected_columns.extend([f'Reader{metric}' for metric, _ in METRICS['reader']])
        
        # Collect primary node metrics
        for metric, stat in METRICS['primary']:
            data = self.get_metric_data(metric, nodes['primary'], start, end, DEFAULT_PERIOD, stat)
            for t, v in data.items():
                collected.setdefault(t, {metric: 0 for metric in expected_columns})[metric] = v

        # Collect reader node metrics if available
        if nodes['reader']:
            for metric, stat in METRICS['reader']:
                data = self.get_metric_data(metric, nodes['reader'], start, end, DEFAULT_PERIOD, stat)
                for t, v in data.items():
                    if t not in collected:
                        collected[t] = {metric: 0 for metric in expected_columns}
                    collected[t][f'Reader{metric}'] = v

        df = pd.DataFrame.from_dict(collected, orient='index').fillna(0)
        
        # Ensure all expected columns exist
        for col in expected_columns:
            if col not in df.columns:
                df[col] = 0
                
        return df

    def calculate_costs(self, df: pd.DataFrame, storage_price: float, 
                       ecpu_price: float, node_price: float) -> pd.DataFrame:
        """Calculate costs based on collected metrics."""
        # Calculate eCPU usage
        df['EvaleCPU'] = (df['EvalBasedCmds'] * df['EvalBasedCmdsLatency'] / 2).fillna(0).astype(int)
        df['PrimaryIn'] = df['SetTypeCmds'] * (df['NetworkBytesIn']/1000 / df['SetTypeCmds'].replace(0,1))
        df['PrimaryOut'] = df['GetTypeCmds'] * (df['NetworkBytesOut']/1000 - df['ReplicationBytes']/1000) / df['GetTypeCmds'].replace(0,1)
        df['ReaderOut'] = df['ReaderGetTypeCmds'] * (df['ReaderNetworkBytesOut']/1000) / df['ReaderGetTypeCmds'].replace(0,1)

        # Set minimum values
        df['BytesUsedForCache'] = df['BytesUsedForCache'].replace(0, 1024)
        df['EvaleCPU'] = df['EvaleCPU'].replace(0, 1)

        # Calculate costs
        df['StorageCost'] = (np.maximum(df['BytesUsedForCache'] / BYTES_TO_GB, MIN_STORAGE_GB) * storage_price).round(4)
        df['eCPUCost'] = ((df['EvaleCPU'] + df['PrimaryIn'] + df['PrimaryOut'] + df['ReaderOut']) * ecpu_price).round(4)
        df['eCPUCost'] = df['eCPUCost'].where(df['EvaleCPU'] + df['PrimaryIn'] + df['PrimaryOut'] + df['ReaderOut'] > 0, 0.0)
        df['TotalCost'] = df['StorageCost'] + df['eCPUCost']

        # Add node-based costs for comparison
        df['NodeBasedCost'] = node_price
        df['NodeMonthly'] = node_price * HOUR_IN_MONTH
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

            # Generate summary
            summary = pd.DataFrame({
                'Total Hours': [len(df)],
                'NodeBased/hr': [node_price],
                'NodeBased/month': [node_price * HOUR_IN_MONTH],
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
