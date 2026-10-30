#!/usr/bin/env bash
#
# Numbered databases.
#
# Demonstrates, on the Cluster Mode Disabled (CMD) cluster, that:
#   * new connections start in database 0; SELECT <id> switches databases,
#   * keys are isolated per numbered database,
#   * FLUSHDB clears only the current database while FLUSHALL clears all of them.
#
# Optionally (RUN_CME_DEMO=true) repeats the numbered-database check on the
# Cluster Mode Enabled (CME) cluster, which is new in Valkey 9.0.
#
# All redis-cli commands run on the EC2 client through SSM, because the clusters
# are private to the VPC.
#
# Usage: ./scripts/demo-numbered-databases.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

require_aws

CMD_ENDPOINT="$(stack_output CmdPrimaryEndpoint)"
if [ -z "$CMD_ENDPOINT" ] || [ "$CMD_ENDPOINT" = "None" ]; then
  err "CmdPrimaryEndpoint not found. Deploy the stack first with ./scripts/deploy.sh"
  exit 1
fi

step "Numbered databases on the CMD cluster"
info "CMD endpoint: $CMD_ENDPOINT"

# Build a single remote script that drives redis-cli non-interactively. Using
# -n/-x style piping keeps everything in one connection where it matters, but
# redis-cli runs each invocation fresh, so we pass a sequence of commands via a
# here-doc piped into one redis-cli session.
#
# redis-cli reads commands from stdin when not given a command on the args line,
# so we pipe a command list into a single authenticated session.

remote_cmd_cmd=$(cat <<REMOTE
set -e
ENDPOINT="$CMD_ENDPOINT"
$(remote_fetch_token_snippet)
rc() { redis-cli --no-raw --tls -h "\$ENDPOINT" -p 6379 -a "\$TOKEN" "\$@" 2>/dev/null; }

echo "--- who am I (expect: default) ---"
rc ACL WHOAMI

echo "--- write tenant1:greeting into db 0 ---"
rc -n 0 SET tenant1:greeting "hello from database 0"
echo "--- write tenant2:greeting into db 1 ---"
rc -n 1 SET tenant2:greeting "hello from database 1"

echo "--- db 1 cannot see db 0's key (expect nil) ---"
rc -n 1 GET tenant1:greeting
echo "--- keys in db 1 (expect only tenant2:greeting) ---"
rc -n 1 KEYS '*'
echo "--- db 0 cannot see db 1's key (expect nil) ---"
rc -n 0 GET tenant2:greeting
echo "--- keys in db 0 (expect only tenant1:greeting) ---"
rc -n 0 KEYS '*'

echo "--- load a small data set per database ---"
rc -n 1 MSET tenant2:a 1 tenant2:b 2 tenant2:c 3
echo "db 1 size:"; rc -n 1 DBSIZE
rc -n 0 MSET tenant1:a 1 tenant1:b 2
echo "db 0 size:"; rc -n 0 DBSIZE

echo "--- FLUSHDB clears only db 1 ---"
rc -n 1 FLUSHDB
echo "db 1 size after FLUSHDB (expect 0):"; rc -n 1 DBSIZE
echo "db 0 size (expect 3, untouched):";    rc -n 0 DBSIZE

echo "--- FLUSHALL clears every database ---"
rc -n 1 MSET tenant2:a 1 tenant2:b 2
rc -n 1 FLUSHALL
echo "db 1 size after FLUSHALL (expect 0):"; rc -n 1 DBSIZE
echo "db 0 size after FLUSHALL (expect 0):"; rc -n 0 DBSIZE

echo "NUMBERED_DB_CMD_DONE"
REMOTE
)

info "Running the numbered-database sequence on the EC2 client..."
run_on_client "$remote_cmd_cmd"
ok "Numbered-database demo (CMD) complete."

if [ "$RUN_CME_DEMO" = "true" ]; then
  step "Optional: numbered databases on the CME cluster"
  CME_RG="$(cme_replication_group_id)"
  CME_ENDPOINT="$(aws elasticache describe-replication-groups \
    --replication-group-id "$CME_RG" \
    --query "ReplicationGroups[0].[ConfigurationEndpoint.Address,NodeGroups[0].PrimaryEndpoint.Address]" \
    --output text | awk '{print $1 == "None" ? $2 : $1}')"
  info "CME endpoint: $CME_ENDPOINT"

  remote_cmd_cme=$(cat <<REMOTE
set -e
ENDPOINT="$CME_ENDPOINT"
$(remote_fetch_token_snippet)
# -c enables cluster mode for the CME cluster.
rc() { redis-cli -c --no-raw --tls -h "\$ENDPOINT" -p 6379 -a "\$TOKEN" "\$@" 2>/dev/null; }

echo "--- write into db 1 on CME ---"
rc -n 1 SET tenant2:cme "hello from cluster mode database 1"
echo "--- db 0 cannot see db 1's key on CME (expect nil) ---"
rc -n 0 GET tenant2:cme
echo "NUMBERED_DB_CME_DONE"
REMOTE
)
  run_on_client "$remote_cmd_cme"
  ok "Numbered-database demo (CME) complete. Numbered databases work in cluster mode (Valkey 9.0+)."
fi

cat <<EOF

Takeaways:
  * New connections start in database 0; SELECT/-n <id> switches databases.
  * Keys are isolated per numbered database.
  * FLUSHDB clears only the current database; FLUSHALL clears every database.

EOF
