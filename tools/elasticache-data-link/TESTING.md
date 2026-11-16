# Testing

The **automated suites** need no AWS account. To run ECDL against your own
database, follow [docs/getting-started.md](docs/getting-started.md).

## Automated suites

Run everything with `make test`. Each component keeps its tests next to its code.

```bash
python3 -m venv .venv && .venv/bin/pip install PyYAML Flask Flask-CORS boto3 requests
python3 -m unittest discover -s ecdl_link/tests -t .                  # link engine, planner, deployer
(cd data-plane/bulk-loader && python3 -m unittest discover -s tests -t .)   # bulk loader
python3 test_docs.py                                                  # docs match the code
cd backend
python3 tests/test_auth.py
python3 tests/test_links.py
DEMO_HISTORY_ENABLED=false python3 tests/test_monitor.py
python3 tests/test_monitor_links.py
python3 tests/test_sql_safety.py
cd ..
(cd frontend && CI=true npx react-scripts test --watchAll=false)     # UI
(cd tools/go-sink && go test ./...)                                   # sink
cfn-lint infra/*.yaml                                                 # templates
```

The sink's version-guard tests also run against a real Valkey when one is given:

```bash
docker run -d -p 127.0.0.1:6399:6379 public.ecr.aws/valkey/valkey:8
(cd tools/go-sink && ECDL_TEST_VALKEY=127.0.0.1:6399 go test -run Integration ./...)
```

`test_docs.py` validates the YAML link examples in the docs, checks that every
`ecdl` command and flag mentioned exists, and resolves every relative link.

Logs, metrics and alarms are described in [docs/operations.md](docs/operations.md).
Benchmark results, and the configuration behind each: [docs/benchmark-results.md](docs/benchmark-results.md).
