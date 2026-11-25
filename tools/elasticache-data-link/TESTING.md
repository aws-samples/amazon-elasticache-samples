# Testing

The **automated suites** need no AWS account. To run ECDL against your own
database, follow [docs/getting-started.md](docs/getting-started.md).

## Automated suites

Run everything with `make test`. Each component keeps its tests next to its code.
The suites need no secrets and no AWS account.

The integration suites (real Valkey: replication waits, truncates, dead
letters, the denormalize races, the offset store, the schema history) skip
themselves without servers. `tools/ci/start-valkey.sh` starts the set they
need (a primary with a replica, a cluster, two spares) and writes the
variables to `valkey.env`; `data-plane/debezium-server/broker-writer/ci-test.sh`
runs the broker writer's unit and integration tests without building the image.

```bash
sh tools/ci/start-valkey.sh && . ./valkey.env                        # Linux x86-64; VALKEY_BIN=<dir> elsewhere
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r backend/requirements.lock  # what the API image installs
python3 -m unittest discover -s ecdl_link/tests -t .                  # link engine, planner, deployer
(cd data-plane/bulk-loader && python3 -m unittest discover -s tests -t .)   # bulk loader
python3 test_docs.py                                                  # docs match the code
cd backend
python3 tests/test_auth.py
python3 tests/test_links.py
python3 tests/test_links_lifecycle.py
DEMO_HISTORY_ENABLED=false python3 tests/test_monitor.py
python3 tests/test_monitor_links.py
python3 tests/test_legacy_removed.py
python3 tests/test_app_hardening.py
cd ..
(cd frontend && npm ci && npx vitest run && npx vite build)       # UI: tests and build
(cd tools/go-sink && go test ./...)                                   # sink
cfn-lint infra/*.yaml                                                 # templates
```

The sink's version-guard tests also run against a real Valkey when one is given:

```bash
docker run -d -p 127.0.0.1:6399:6379 public.ecr.aws/valkey/valkey:8
(cd tools/go-sink && ECDL_TEST_VALKEY=127.0.0.1:6399 go test -run Integration ./...)
```
The teardown's count of what the broker still holds uses the same variable:
`ECDL_TEST_VALKEY=127.0.0.1:6399 python3 -m unittest ecdl_link.tests.test_teardown_safety`.
The replica-wait tests also need a replica of that server on the same host
(`ECDL_TEST_VALKEY_REPLICA=127.0.0.1:<port>`; they pause it with SIGSTOP), and
the cluster tests a cluster created with `--cluster-replicas 1`
(`ECDL_TEST_VALKEY_CLUSTER=127.0.0.1:<port>`).

`test_docs.py` validates the YAML link examples in the docs, checks that every
`ecdl` command and flag mentioned exists, and resolves every relative link.

Logs, metrics and alarms are described in [docs/operations.md](docs/operations.md).
Benchmark results, and the configuration behind each: [docs/benchmark-results.md](docs/benchmark-results.md).
