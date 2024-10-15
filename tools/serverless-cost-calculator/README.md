## Requirements

* AWC CLI installed on the host and AWS credentials configured for more information  http://aws.amazon.com/cli/
* Python 3.x preferably 3.8

## Limitations

This script uses the default AWS CLI profile to connect to a cluster in the default profile.
In order to speed up calculations the script assumes even key distribution among all shards and even workload distribution. Uneven key distribution and or a hot spot will effect calculation accuracy. The costs calculated are published rates at the time of publishing.

## Compute

This calculator uses the Amazon ElastiCache Valkey compute engine cost bases.

## Installation

Cone the repository and change to cost-calculator directory then execute the following steps

* Create a virtual python environment and install the requirements

```bash
python3.8 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## How to run it

```bash
python ./cost-calculator.py --region us-east-1 --cluster cluster-name <--day-range 1> <--output hourly_cost_estimate.scv>
```

## Mandatory parameters
--region AWS region name

--cluster ElastiCache cluster name

### Optional parameters 
--day-range: default value of 1. The number of day to calculate estimated AWS ElastiCache cost in one hour increments.

--output: default value cost_estimate_cluster_name_"%H:%M_%d_%m_%Y".csv. The name of the output file in CSV format.

## Authors and acknowledgment

Author: Steven Hancz shancz@amazon.com 
Contributors: Lakshmi Peri lvperi@amazon.com, Yann Richard yannrich@amazon.com

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
