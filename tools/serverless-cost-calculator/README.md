# ElastiCache Serverless Valkey Cost Estimator

The ElastiCache Serverless Valkey Cost Estimator is a Python script designed to help AWS users estimate the potential costs of migrating their existing ElastiCache clusters to the serverless Valkey engine. This tool analyzes the current usage patterns of your ElastiCache clusters and projects what those workloads would cost if run on the Valkey serverless architecture.

### Key Features:

- **Serverless Cost Projection**: Uses metrics from your current ElastiCache clusters to estimate costs for the Valkey serverless engine.
- **CloudWatch Integration**: Leverages AWS CloudWatch metrics to gather accurate usage data from your existing clusters.
- **Flexible Analysis Period**: Allows cost estimation over custom time ranges, from a single day to multiple weeks.
- **Comprehensive Metric Analysis**: Processes various ElastiCache metrics including memory usage, network I/O, and command executions to provide a holistic cost estimate.
- **Detailed Excel Report**: Generates comprehensive Excel reports with hourly breakdowns and cost comparisons between node-based and serverless deployments.
- **Advanced Metrics Analysis**: Calculates eCPU usage based on command latency, network I/O, and replication metrics.
- **Multi-Node Support**: Analyzes both primary and reader nodes for complete cluster cost assessment.
- **Versatile Deployment**: Can be run in various environments including local machines, AWS CloudShell, or EC2 instances.

Whether you're a cloud architect, DevOps engineer, or finance analyst, this tool provides the data you need to make informed decisions about your ElastiCache deployments. By understanding your usage patterns and associated costs, you can optimize your cache strategy, improve performance, and control expenses.

Get started with the ElastiCache Cost Calculator to gain deeper insights into your AWS ElastiCache usage and costs!

## Requirements

* [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) installed on the host and AWS credentials configured for more information  http://aws.amazon.com/cli/
* Python 3.x preferably 3.8

## Limitations

This script uses the default AWS CLI profile to connect to a cluster in the default profile.
In order to speed up calculations the script assumes even key distribution among all shards and even workload distribution. Uneven key distribution and or a hot spot will effect calculation accuracy. The costs calculated are published rates at the time of publishing.

## Cost Calculation

The calculator performs comprehensive cost analysis considering multiple factors:

1. **Storage Costs**: Based on actual memory usage (BytesUsedForCache) with a minimum of 0.1GB
2. **Compute (eCPU) Costs**: Calculated from:
   - Command evaluation costs (EvalBasedCmds × EvalBasedCmdsLatency)
   - Network I/O costs for both primary and reader nodes
   - Replication costs between nodes
3. **Node-based vs Serverless Comparison**:
   - Hourly and monthly cost projections for both deployment types
   - Detailed breakdowns in Excel format with separate sheets for:
     - Hourly metrics and costs
     - Overall cost comparison summary

The calculator retrieves real-time pricing information from AWS Price List API to ensure accuracy.

## Execution Environment

This script is flexible and can be run in various environments, provided they have access to valid AWS credentials. Suitable execution environments include:

1. Local Machine: Run the script on your personal computer or workstation.
2. [AWS CloudShell](https://aws.amazon.com/cloudshell): Execute directly from the AWS Management Console using CloudShell.
3. [EC2 Instance](https://aws.amazon.com/ec2): Deploy and run on an Amazon EC2 instance within your AWS environment.

## Installation

Clone the [repository](https://github.com/aws-samples/amazon-elasticache-samples) and change to `tools/serverless-cost-calculator` directory then execute the following steps

* Create a virtual python environment and install the requirements

```bash
python3.8 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## How to run it

```bash
python ./cost-calculator.py --region us-east-1 --cluster cluster-name [--day-range 1] [--output elasticache_cost_report.xlsx]
```

## Mandatory parameters
`--region`: AWS region name

`--cluster`: ElastiCache cluster name

### Optional parameters 
`--day-range`: default value of 1. The number of day to calculate estimated AWS ElastiCache cost in one hour increments.

`--output`: default value elasticache_cost_cluster_name_"%Y%m%d_%H%M".xlsx. The name of the output Excel file containing detailed metrics and cost comparisons.

## Authors and acknowledgment

Author: Steven Hancz shancz@amazon.com 
Contributors: Lakshmi Peri lvperi@amazon.com, Yann Richard yannrich@amazon.com, Luis Morales lluim@amazon.com

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
