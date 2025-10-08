# In-Memory Assessment Tool

## 1. Purpose

This tool connects to a Valkey or Redis OSS cluster and gathers two sets of metrics from each node. Once at the beginning of the execution, and again after a user-defined waiting period. It then calculates deltas and averages, prints a human-readable summary report, and writes CSV and JSON files for deeper analysis.

**Works with**: Open-source Valkey clusters, open-source Redis OSS clusters. 

**Experimental support** ElastiCache node-based clusters.

**Does not work with**: Commercial versions from Redis, Ltd. This tool is not affiliated with Redis, Ltd.

### Caveats

There is no single right way to collect metrics against all versions of Valkey and Redis OSS. Between versions of each engine there are bugs that appear and are fixed, metrics that appear and are fixed, and metrics are sometimes calculated differently. This tool is a best effort to work around many (but not all) of these.

Consider the fact that primary nodes can serve both write requests and read requests. They can also replicate data to one or more replica nodes. Trying to map the network bandwidth against both read-based and write-based commands is difficult.

## 2. Installation

### Clone the repository and change into the assessment directory

```sh
git clone https://github.com/aws-samples/amazon-elasticache-samples.git
cd amazon-elasticache-samples/tools/inmemory_assessment
```

### Create a Python environment and install the application

```sh
python -m venv venv
source venv/bin/activate
pip install .
```

## 3. Execution

Simply run the inmemory_assessment tool now that it has been installed
with `pip`:

```sh
inmemory_assessment --help
```

### Usage

Output from running `inmemory_assessment --help`:

```
 Usage: inmemory_assessment [OPTIONS]

 A workload assessment tool for Valkey and Redis OSS clusters.


╭─ Options ────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ --host                          TEXT     Hostname or IP address [default: localhost]                                 │
│ --port                          INTEGER  Port (default: 6379) [default: 6379]                                        │
│ --user                          TEXT     Username (optional, for authenticated ElastiCache) [default: None]          │
│ --password                      TEXT     Password (default: '')                                                      │
│ --tls             --no-tls               Enable TLS/SSL [default: no-tls]                                            │
│ --output                        TEXT     CSV output filename (defaults to output/ directory)                         │
│                                          [default: output/output_20250821_224840.csv]                                │
│ --json-output                   TEXT     JSON output filename (defaults to CSV filename with .json extension)        │
│                                          [default: None]                                                             │
│ --duration                      INTEGER  Duration in seconds to collect performance metrics (default: 120 seconds)   │
│                                          [default: 120]                                                              │
│ --log-level                     TEXT     Set logging level: DEBUG (detailed info), INFO (operational details),       │
│                                          WARNING (warnings only - default), ERROR (errors only), CRITICAL (critical  │
│                                          only)                                                                       │
│                                          [default: WARNING]                                                          │
│ --quiet           --no-quiet             Suppress console output (only show final file locations)                    │
│                                          [default: no-quiet]                                                         │
│ --version                                                                                                            │
│ --legacy-units                           Use legacy traffic units (KB/sec, MB/sec) instead of default Gbps           │
│ --help                                   Show this message and exit.                                                 │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯

```

## 4. What this tool gathers

The tool connects to each Valkey / Redis OSS node and runs READ-ONLY
commands which collect:

- Server info, memory, persistence, clients, and stats sections
- `CONFIG GET` (if enabled) values for memory and AOF settings
- Loaded modules
- `INFO commandstats` and breakdown of read/write operations
- Key counts per DB and total keys
- Delta over time for memory, commands, and network I/O

### Example Console Output of a 30 second run

```
> inmemory_assessment --host your_cluster_name --duration 30

                          Cluster Summary
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Metric                          ┃ Value                         ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Cluster Mode                    │ Enabled                       │
│ Primaries                       │ 2                             │
│ Replicas                        │ 2                             │
│ Engine(s)                       │ Redis OSS 7.2.4               │
│ Total Primary Memory Configured │ 0.81 GB                       │
│ Total Primary Memory Used       │ 0.15 GB                       │
│ Total Primary Keys              │ 84,264                        │
│ Eviction Policy                 │ volatile-lru                  │
└─────────────────────────────────┴───────────────────────────────┘

Cluster workload information
    Client commands       : 2,117.0 / sec
    Avg bytes per command : 1,112 bytes
    Client bandwidth      : 0.019 Gbps
    Replication bandwidth : 0.004 Gbps
    Total bandwidth       : 0.023 Gbps
    Estimated ECPUs/sec   : 2,298

Note: 'Avg bytes per command' represents pure client traffic (replication excluded).

Per-node operation breakdown
    172.31.45.137:6379 (primary): 4 reads/sec, 250 writes/sec, 0.002 Gbps client traffic
    172.31.7.156:6379 (primary): 5 reads/sec, 246 writes/sec, 0.002 Gbps client traffic
    172.31.71.152:6379 (replica): 882 reads/sec, 0.0 writes/sec, 0.008 Gbps client traffic
    172.31.87.128:6379 (replica): 730 reads/sec, 0.0 writes/sec, 0.006 Gbps client traffic


Monitoring Tool Overhead Analysis
    Estimated diagnostic commands : ~72 total
    Estimated diagnostic traffic  : ~2,220 bytes/sec across all nodes
    Per-node diagnostic overhead   : ~0.000004 Gbps average per node
Note: Low-traffic nodes may show mostly diagnostic overhead from this monitoring tool.
JSON report written to: output/output_20251007_231314.json
CSV report written to: output/output_20251007_231314.csv

```

### Mixed workload calculation challenges

For pure workloads (reads-only from replicas, writes-only to primaries), we get accurate metrics.

For mixed workloads on the same nodes, we get reasonable approximations but not perfect precision.

This is because there is no way to assign the total outbound and inbound network traffic to read-or-write related commands.

