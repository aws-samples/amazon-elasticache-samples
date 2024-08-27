## Requirements

* AWC CLI installed on the host and AWS credentials configured for more information  http://aws.amazon.com/cli/
* Python 3.x preferably 3.8

## Limitations

This script uses the default AWS CLI profile.

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

# parameters

mandatory parameters:
--region AWS region name
--cluster ElastiCache cluster name

optional parameters: 
--day-range: default value of 1. The number of day to calculate estimated AWS ElastiCache cost in one hour increments.
--output: default value cost_estimate_cluster_name_"%H:%M_%d_%m_%Y".csv. The name of the output file in CSV format.