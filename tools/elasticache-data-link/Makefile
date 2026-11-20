# ECDL — the main commands for testing, building and releasing.
#
# Everything here shells out to a script or a test runner that also works on its
# own. The Makefile only lists those commands in one place; nothing in this
# repo is compiled by make.

SHELL := /bin/bash
VERSION := $(shell tr -d '[:space:]' < VERSION)

.PHONY: help test test-ecdl-link test-loader test-backend test-docs lint-cfn \
        images push pin version release-check

help:
	@echo "ECDL $(VERSION)"
	@echo ""
	@echo "  make test           run every test suite"
	@echo "  make lint-cfn       cfn-lint the CloudFormation templates"
	@echo ""
	@echo "  make images         build the five container images locally (host arch)"
	@echo "  make push           build multi-arch and push to the registry"
	@echo "  make pin            set the templates' ImageTag default to VERSION"
	@echo "  make version        show version, commit and where images would go"
	@echo "  make release-check  everything that must pass before a release"
	@echo ""
	@echo "  Registry comes from ECDL_REGISTRY, or from your AWS credentials."
	@echo "  Nothing is hardcoded to one account. See release.sh."

# ── Tests ──────────────────────────────────────────────────────────────────
# Listed separately as well as together, because when one fails you want to
# re-run just that one. Each suite also runs on its own with a bare `python3`.
# The backend suites and test-docs need PyYAML/Flask/boto3 — see TESTING.md.
test: test-ecdl-link test-loader test-backend test-docs
	@echo "all suites passed"

# The link engine shared by the CLI and the backend.
test-ecdl-link:
	python3 -m unittest discover -s ecdl_link/tests -t .

# The parallel bulk loader and its transform engine.
test-loader:
	cd data-plane/bulk-loader && python3 -m unittest discover -s tests -t .

# The control-plane backend (Flask). Needs Flask, Flask-CORS, PyYAML, boto3.
test-backend:
	cd backend && python3 tests/test_auth.py
	cd backend && python3 tests/test_links.py
	cd backend && python3 tests/test_links_lifecycle.py
	cd backend && DEMO_HISTORY_ENABLED=false python3 tests/test_monitor.py
	cd backend && python3 tests/test_monitor_links.py
	cd backend && python3 tests/test_sql_safety.py
	cd backend && python3 tests/test_legacy_removed.py
	cd backend && python3 tests/test_app_hardening.py
	cd backend && python3 tests/test_alarm_topic.py

# Documentation-vs-code guardrail: runs the CLI over the README examples.
test-docs:
	python3 test_docs.py

lint-cfn:
	cfn-lint infra/*.yaml

# ── Images ─────────────────────────────────────────────────────────────────
images:
	./release.sh build

push:
	./release.sh push

pin:
	./release.sh pin

version:
	@./release.sh show

# A release changes what customers run, so the gate is the full suite plus the
# template lint plus a syntax check on the shell that gets shipped.
release-check: test lint-cfn
	bash -n release.sh
	bash -n data-plane/debezium-server/entrypoint.sh
	@echo "release checks passed for $(VERSION)"
