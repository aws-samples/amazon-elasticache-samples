#!/usr/bin/env bash
#
# Prove per-tenant isolation (data plane).
#
# Connects to the CMD cluster on the EC2 client as each RBAC tenant and asserts,
# for every tenant N (1..TENANT_COUNT):
#   * tenant N can use its own database N (SET/GET succeed),
#   * tenant N is denied (NOPERM) on database 0,
#   * tenant N is denied (NOPERM) on a neighbour's database,
#   * tenant 1 cannot run FLUSHALL (it lacks alldbs).
#
# All redis-cli commands run on the EC2 client through SSM. Each tenant's
# password is fetched from Secrets Manager ON THE CLIENT (via its instance
# role), so no password transits the SSM command text. The script exits
# non-zero if any expectation is not met.
#
# Usage: ./scripts/verify-isolation.sh
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

step "Verifying per-tenant isolation on the CMD cluster"
info "CMD endpoint: $CMD_ENDPOINT    tenants: $TENANT_COUNT"

# Build the per-tenant portion of the remote test matrix. For each tenant we
# emit shell that resolves the password on the client, then runs the checks.
# A neighbour database is "N+1" wrapping back to 1 so every tenant has one.
tenant_block() {
  local n="$1" uname db neighbour secret_name
  uname="$(tenant_user_name "$n")"
  db="$(tenant_db "$n")"
  neighbour=$(( n % TENANT_COUNT + 1 ))   # next tenant, wrapping to 1
  secret_name="$(tenant_secret_name "$n")"
  cat <<BLOCK
U="$uname"
P="\$(aws secretsmanager get-secret-value --secret-id '$secret_name' --query SecretString --output text --region '$AWS_REGION')"

echo "=== $uname: write + read its own database $db ==="
as "\$U" "\$P" $db SET $uname:secret "only $uname can see this" >/dev/null
out="\$(as "\$U" "\$P" $db GET $uname:secret)"
echo "$uname GET db$db -> \$out"
assert_contains ${uname}_can_use_own_db "only $uname can see this" "\$out"

echo "=== $uname: denied on database 0 ==="
out="\$(as "\$U" "\$P" 0 GET anything)"
echo "$uname GET db0 -> \$out"
assert_contains ${uname}_denied_db0 "NOPERM" "\$out"

echo "=== $uname: denied on neighbour database $neighbour ==="
out="\$(as "\$U" "\$P" $neighbour GET anything)"
echo "$uname GET db$neighbour -> \$out"
assert_contains ${uname}_denied_neighbour "NOPERM" "\$out"
BLOCK
}

# Expected assertion labels (must match tenant_block output).
expected=()
tenant_blocks=""
for n in $(seq 1 "$TENANT_COUNT"); do
  uname="$(tenant_user_name "$n")"
  expected+=("${uname}_can_use_own_db" "${uname}_denied_db0" "${uname}_denied_neighbour")
  tenant_blocks+="$(tenant_block "$n")"$'\n'
done
expected+=("tenant1_flushall_blocked")

# One remote script runs the whole matrix and emits machine-checkable ASSERT
# lines. 'read -r -d "" ... <<REMOTE' loads the heredoc into the variable
# without the local shell parsing its body; 'read' returns non-zero at EOF even
# on success, so it is guarded with '|| true'.
read -r -d '' remote_cmd <<REMOTE || true
ENDPOINT="$CMD_ENDPOINT"

# Run a redis-cli command as a given user against a given db; print the result.
as() { # user pass db args...
  local user="\$1" pass="\$2" db="\$3"; shift 3
  redis-cli --no-raw --tls -h "\$ENDPOINT" -p 6379 --user "\$user" --pass "\$pass" -n "\$db" "\$@" 2>&1
}

# assert_contains <label> <needle> <haystack>
assert_contains() {
  if printf '%s' "\$3" | grep -qF -- "\$2"; then
    echo "ASSERT PASS \$1"
  else
    echo "ASSERT FAIL \$1"
  fi
}

$tenant_blocks

echo "=== tenant1: FLUSHALL is blocked (needs alldbs) ==="
T1P="\$(aws secretsmanager get-secret-value --secret-id '$(tenant_secret_name 1)' --query SecretString --output text --region '$AWS_REGION')"
out="\$(as tenant1 "\$T1P" 1 FLUSHALL)"
echo "tenant1 FLUSHALL -> \$out"
assert_contains tenant1_flushall_blocked "NOPERM" "\$out"

echo "VERIFY_DONE"
REMOTE

info "Running the isolation test matrix on the EC2 client..."
output="$(run_on_client "$remote_cmd")" || true
printf '%s\n' "$output"

step "Results"
failures=0
for check in "${expected[@]}"; do
  if printf '%s\n' "$output" | grep -q "ASSERT PASS $check"; then
    ok "$check"
  else
    err "$check"
    failures=$((failures + 1))
  fi
done

if [ "$failures" -eq 0 ]; then
  ok "All isolation checks passed. Tenant isolation is enforced by the engine."
  exit 0
else
  err "$failures isolation check(s) failed. Review the output above."
  exit 1
fi
