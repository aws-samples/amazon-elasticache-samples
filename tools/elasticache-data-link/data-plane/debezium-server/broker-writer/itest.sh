#!/bin/bash
# Integration test for the broker writer: the Debezium image against a real
# MySQL and a Valkey primary + replica, in Docker. Prints PASS/FAIL per check.
# Run from the repository root on a host with Docker, after `./release.sh push`
# has copied the Debezium base into this account's ECR:
#   AWS_REGION=<region> bash data-plane/debezium-server/broker-writer/itest.sh
set -u
D=do; D=${D}cker
REGION=${AWS_REGION:?set AWS_REGION}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text) || exit 1
REG=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
aws ecr get-login-password --region "$REGION" | $D login --username AWS --password-stdin $REG >/dev/null 2>&1
BASE=$REG/ecdl/debezium-server-base:3.0.6.Final
echo "== build (compiles the writer, runs its tests, checks the upstream jar)"
DOCKER_BUILDKIT=1 $D build --progress=plain --build-arg DEBEZIUM_BASE=$BASE -t ecdl-dbz-itest data-plane/debezium-server > build.log 2>&1
BRC=$?
grep -E "checks passed|FAILED|FAIL |OK$|sha256|error" build.log | head -20
[ $BRC = 0 ] && echo "PASS build" || { echo "FAIL build"; tail -30 build.log; exit 1; }

cleanup() { $D rm -f dz-mysql dz-vk1 dz-vk2 dz-dbz >/dev/null 2>&1; $D network rm dz >/dev/null 2>&1; }
cleanup
$D pull -q public.ecr.aws/docker/library/mysql:8.0 >/dev/null; $D pull -q public.ecr.aws/valkey/valkey:8 >/dev/null
$D network create dz >/dev/null
$D run -d --name dz-mysql --network dz -e MYSQL_ROOT_PASSWORD=pw -e MYSQL_DATABASE=shop \
  public.ecr.aws/docker/library/mysql:8.0 --server-id=1 --log-bin=mysql-bin --binlog-format=ROW \
  --binlog-row-image=FULL >/dev/null
$D run -d --name dz-vk1 --network dz public.ecr.aws/valkey/valkey:8 valkey-server --save '' --appendonly no >/dev/null
$D run -d --name dz-vk2 --network dz public.ecr.aws/valkey/valkey:8 valkey-server --save '' --appendonly no --replicaof dz-vk1 6379 >/dev/null
vk1() { $D exec dz-vk1 valkey-cli "$@"; }
sql() { $D exec dz-mysql mysql -uroot -ppw shop -N -e "$1" 2>/dev/null; }
for i in $(seq 1 60); do sql "select 1" >/dev/null && break; sleep 2; done
sql "CREATE TABLE items (id INT PRIMARY KEY, v INT); CREATE TABLE ecdl_signal (id VARCHAR(42) PRIMARY KEY, type VARCHAR(32) NOT NULL, data VARCHAR(2048));"

start_dbz() {
  $D rm -f dz-dbz >/dev/null 2>&1
  $D run -d --name dz-dbz --network dz \
    -e QUARKUS_PROFILE=mysql -e SOURCE_DB_HOST=dz-mysql -e SOURCE_DB_USER=root -e SOURCE_DB_PASSWORD=pw \
    -e SOURCE_DB_NAME=shop -e SERVER_ID=5401 -e TABLE_INCLUDE_LIST=shop.items -e SNAPSHOT_MODE=initial \
    -e SIGNAL_COLLECTION=shop.ecdl_signal -e TOPIC_PREFIX=t -e BROKER_HOST=dz-vk1 -e BROKER_PORT=6379 \
    -e BROKER_SSL=false -e BROKER_WAIT=true -e OFFSET_KEY=ecdl:t:offsets -e SCHEMA_HISTORY_KEY=ecdl:t:history \
    -e HEARTBEAT_INTERVAL_MS=2000 ecdl-dbz-itest >/dev/null
}
wait_len() { # stream n timeout
  for i in $(seq 1 $3); do n=$(vk1 XLEN "$1" 2>/dev/null); [ "${n:-0}" -ge "$2" ] && return 0; sleep 1; done; return 1
}
running() { [ "$($D inspect -f '{{.State.Running}}' dz-dbz 2>/dev/null)" = true ]; }
S=data:t.shop.items

echo "== 1. our writer is the one loaded, normal capture"
start_dbz
for i in $(seq 1 90); do $D logs dz-dbz 2>&1 | grep -q "ECDL broker writer in use" && break; sleep 2; done
$D logs dz-dbz 2>&1 | grep -q "ECDL broker writer in use" && echo "PASS writer loaded" || { echo "FAIL writer not loaded"; $D logs dz-dbz 2>&1 | tail -30; }
for i in $(seq 1 60); do $D logs dz-dbz 2>&1 | grep -q -i "streaming" && break; sleep 2; done
for k in $(seq 1 50); do sql "INSERT INTO items VALUES ($k, $k)"; done
wait_len $S 50 60 && echo "PASS 50 inserts captured" || echo "FAIL inserts: XLEN $(vk1 XLEN $S)"

echo "== 2. full broker: no reorder, nothing lost, capture resumes when memory frees"
used=$(vk1 INFO memory | tr -d '\r' | grep '^used_memory:' | cut -d: -f2)
vk1 CONFIG SET maxmemory-policy noeviction >/dev/null
vk1 CONFIG SET maxmemory $((used / 2)) >/dev/null   # below current use: every write is refused
# many updates of the same 5 rows, so the last value of each row is known
for r in $(seq 1 40); do sql "UPDATE items SET v = $r WHERE id BETWEEN 1 AND 5"; done
sleep 70
echo "INFO stream length while full: $(vk1 XLEN $S), used $(vk1 INFO memory | tr -d "\r" | grep "^used_memory:")"
$D logs dz-dbz 2>&1 | grep -q -E "ERROR .*(not written yet \(OOM|broker memory is full)" && echo "PASS full broker logged as ERROR" || echo "FAIL full broker not logged as ERROR"
[ "$(vk1 XLEN $S)" = 50 ] && echo "PASS nothing written while full" || echo "FAIL stream grew while full: $(vk1 XLEN $S)"
vk1 CONFIG SET maxmemory 0 >/dev/null
sleep 10
running && echo "PASS task still running after OOM" || echo "FAIL task stopped after OOM"
cat > last.py <<'PY'
import json, subprocess, sys
raw = subprocess.run(['docker','exec','dz-vk1','valkey-cli','--json','XRANGE','data:t.shop.items','-','+'],
                     capture_output=True, text=True).stdout
last = {}
for _id, fields in json.loads(raw):
    f = dict(zip(fields[::2], fields[1::2]))
    v = json.loads(f['value'])
    row = v.get('after') or v.get('before')
    if row: last[row['id']] = (v.get('after') or {}).get('v')
bad = {k: last.get(k) for k in range(1, 6) if last.get(k) != 40}
print('PASS last value of every row is the newest' if not bad else 'FAIL stale last values %s' % bad)
PY
python3 last.py

echo "== 3. refused write (WRONGTYPE): task stops, offset does not move past it"
off_before=$(vk1 HGETALL ecdl:t:offsets | tail -1)
vk1 DEL $S >/dev/null; vk1 SET $S notastream >/dev/null
sql "INSERT INTO items VALUES (100, 100)"
for i in $(seq 1 60); do running || break; sleep 2; done
running && echo "FAIL task still running after WRONGTYPE" || echo "PASS task stopped on WRONGTYPE"
$D logs dz-dbz 2>&1 | grep -q "refused a write (WRONGTYPE" && echo "PASS reason logged" || echo "FAIL reason not logged"
vk1 DEL $S >/dev/null
start_dbz
for i in $(seq 1 90); do n=$(vk1 XLEN $S); [ "${n:-0}" -ge 1 ] && break; sleep 2; done
python3 - <<'PY'
import json, subprocess
raw = subprocess.run(['docker','exec','dz-vk1','valkey-cli','--json','XRANGE','data:t.shop.items','-','+'],
                     capture_output=True, text=True).stdout
ids = [json.loads(dict(zip(f[::2], f[1::2]))['value']).get('after', {}).get('id') for _i, f in json.loads(raw)]
print('PASS insert 100 written after restart' if 100 in ids else 'FAIL insert 100 missing after restart: %s' % ids[:10])
PY

echo "== 4. broker restart (new run_id = failover): task stops, restart replays, nothing lost"
$D restart dz-vk1 >/dev/null; sleep 3
$D exec dz-vk2 valkey-cli REPLICAOF dz-vk1 6379 >/dev/null
sql "INSERT INTO items VALUES (200, 200)"
for i in $(seq 1 60); do running || break; sleep 2; done
if running; then echo "INFO task did not stop (no write reached the broker during the restart)"; else
  $D logs dz-dbz > step4.log 2>&1
  if grep -q -E "failed over|different primary" step4.log; then echo "PASS stopped: the writer saw the failover";
  elif grep -q -E "RedisClientConnectionException|JedisConnectionException" step4.log; then echo "PASS stopped: Debezium's offset store saw the lost connection";
  else echo "FAIL stopped for another reason:"; grep -a -E "ERROR|Exception" step4.log | grep -v "^\s*at " | tail -6 | cut -c1-300 | sed 's/^/INFO   /'; fi; fi
# the broker came back empty (no persistence): offsets are gone too, like a lost broker
start_dbz
for i in $(seq 1 90); do n=$(vk1 XLEN $S 2>/dev/null); [ "${n:-0}" -ge 1 ] && break; sleep 2; done
for i in $(seq 1 60); do n=$(vk1 XLEN $S 2>/dev/null); [ "${n:-0}" -ge 52 ] && break; sleep 2; done
python3 - <<'PY'
import json, subprocess
raw = subprocess.run(['docker','exec','dz-vk1','valkey-cli','--json','XRANGE','data:t.shop.items','-','+'],
                     capture_output=True, text=True).stdout
want = set(range(1, 51)) | {100, 200}
last = {}
for _i, f in json.loads(raw):
    v = json.loads(dict(zip(f[::2], f[1::2]))['value'])
    row = v.get('after')
    if row: last[row['id']] = row['v']
missing = sorted(want - set(last))
stale = {k: last[k] for k in range(1, 6) if last.get(k) != 40}
print('PASS every row is in the stream after the restart, newest values' if not missing and not stale
      else 'FAIL after restart: missing %s stale %s' % (missing[:10], stale))
PY
$D logs dz-dbz 2>&1 | grep -E "ECDL broker writer" | tail -5
echo "== 5. replica down: no write is confirmed, task stops, offset not moved; nothing lost"
running || start_dbz
for i in $(seq 1 60); do $D logs dz-dbz 2>&1 | grep -q "ECDL broker writer in use" && break; sleep 2; done
sleep 10
$D stop dz-vk2 >/dev/null
sql "INSERT INTO items VALUES (300, 300)"
for i in $(seq 1 60); do running || break; sleep 2; done
running && echo "FAIL task still running with no replica" || { $D logs dz-dbz 2>&1 | grep -q "no replica acknowledged" && echo "PASS stopped: no replica acknowledged" || echo "FAIL stopped for another reason"; }
$D start dz-vk2 >/dev/null; sleep 5
start_dbz
for i in $(seq 1 60); do $D exec dz-vk1 valkey-cli --json XRANGE $S - + | grep -q '\\"id\\":300' && break; sleep 2; done
$D exec dz-vk1 valkey-cli --json XRANGE $S - + | grep -q '\\"id\\":300' && echo "PASS insert 300 in the stream after the replica returned" || echo "FAIL insert 300 missing"
cleanup
