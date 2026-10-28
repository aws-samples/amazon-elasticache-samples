#!/usr/bin/env bash
# Install vLLM + LMCache + Valkey on one GPU host for measure_knobs.py.
# Usage: setup_stack.sh install PATCH_DIR   # venv, vLLM, LMCache, LMCache patches, model
#        setup_stack.sh valkey              # start Valkey on 127.0.0.1:6379 (needs VALKEY_PASSWORD)
#        setup_stack.sh lmcache-yaml FILE   # write the LMCache config (needs VALKEY_PASSWORD)
#        setup_stack.sh tc on|off           # cap Valkey loopback traffic: 10 Gbit/s per direction, 0.2 ms RTT (sudo)
#        setup_stack.sh cache-node          # ON A SEPARATE CACHE NODE, as root: Valkey on its private IP + 127.0.0.1
# PATCH_DIR holds the LMCache 0.5.5 *.patch files to apply (not shipped here).
set -euo pipefail

VENV=${VENV:-$HOME/knobs-venv}
VLLM_VERSION=${VLLM_VERSION:-0.30.0}
LMCACHE_VERSION=${LMCACHE_VERSION:-0.5.5}
PY_VER=${PY_VER:-3.12}
MODEL=${MODEL:-Qwen/Qwen3-8B}
VALKEY_IMAGE=${VALKEY_IMAGE:-valkey/valkey:9.1.2}
TC_RATE=${TC_RATE:-10gbit}
TC_DELAY=${TC_DELAY:-0.1ms}   # one way; RTT adds 2x
export HF_HOME=${HF_HOME:-$HOME/hf}

need_password() {
  [ -n "${VALKEY_PASSWORD:-}" ] || { echo "set VALKEY_PASSWORD" >&2; exit 2; }
  # It is written inside double quotes in the Valkey config and the LMCache yaml.
  case "$VALKEY_PASSWORD" in *[\"\\]*|*[[:cntrl:]]*) echo "VALKEY_PASSWORD must not contain \", \\ or control characters" >&2; exit 2 ;; esac
}

install() {
  local patch_dir=$1
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  [ -x "$VENV/bin/python" ] || uv venv --python "$PY_VER" --seed "$VENV"
  uv pip install --python "$VENV/bin/python" "vllm==$VLLM_VERSION" ninja
  local torch_v constraints
  torch_v=$("$VENV/bin/python" -c 'import torch;print(torch.__version__.split("+")[0])')
  constraints=$(mktemp /tmp/knobs-constraints-XXXX.txt)
  printf 'torch==%s\nvllm==%s\n' "$torch_v" "$VLLM_VERSION" > "$constraints"
  uv pip install --python "$VENV/bin/python" -c "$constraints" \
    "lmcache==$LMCACHE_VERSION" "valkey-glide-sync>=2.3" hf_xet
  rm -f "$constraints"
  local site
  site=$("$VENV/bin/python" -c 'import lmcache,os;print(os.path.dirname(os.path.dirname(lmcache.__file__)))')
  for p in "$patch_dir"/lmcache-"$LMCACHE_VERSION"-*.patch; do
    if (cd "$site" && patch -p1 --forward --dry-run -s < "$p" >/dev/null 2>&1); then
      (cd "$site" && patch -p1 --forward -s < "$p") && echo "applied $(basename "$p")"
    else
      echo "skipped $(basename "$p") (already applied or does not apply)"
    fi
  done
  # LMCache 0.5.5 passes (username, password) positionally to GLIDE's ServerCredentials(password, username),
  # so requirepass never authenticates. Pass them by keyword.
  "$VENV/bin/python" - "$site/lmcache/v1/storage_backend/valkey/worker_pool.py" <<'PY'
import sys
path = sys.argv[1]
src = open(path).read()
old = "glide_sync.ServerCredentials(self._username, self._password)"
new = "glide_sync.ServerCredentials(password=self._password, username=self._username or None)"
if old in src:
    open(path, "w").write(src.replace(old, new))
    print("applied ServerCredentials keyword fix")
else:
    print("ServerCredentials keyword fix: already applied or not needed")
PY
  "$VENV/bin/hf" download "$MODEL" > /dev/null
  "$VENV/bin/python" -c 'import lmcache, vllm, torch; print("lmcache", lmcache.__version__, "vllm", vllm.__version__, "torch", torch.__version__)'
}

valkey() {
  need_password
  docker rm -f knobs-valkey >/dev/null 2>&1 || true
  # The password reaches the container through its environment and valkey-server through a 0600 config
  # file written by the shell builtin printf, so it is in no process argv.
  docker run -d --name knobs-valkey --network host -e VALKEY_PASSWORD "$VALKEY_IMAGE" \
    sh -c 'umask 077; printf "requirepass \"%s\"\n" "$VALKEY_PASSWORD" > /tmp/auth.conf
      exec valkey-server /tmp/auth.conf --bind 127.0.0.1 --port 6379 --protected-mode yes --save "" \
      --appendonly no --maxmemory 24gb --maxmemory-policy allkeys-lru' >/dev/null
  sleep 2
  docker exec knobs-valkey sh -c 'REDISCLI_AUTH="$VALKEY_PASSWORD" exec valkey-cli -h 127.0.0.1 INFO server' \
    | grep valkey_version
}

lmcache_yaml() {
  if [ "${LMCACHE_REMOTE:-1}" = 0 ]; then
    cat > "$1" <<EOF
chunk_size: 256
local_cpu: ${LMCACHE_LOCAL_CPU:-true}
max_local_cpu_size: ${LMCACHE_CPU_GB:-5}
use_layerwise: false
save_decode_cache: false
save_unfull_chunk: false
pre_caching_hash_algorithm: sha256_cbor
blocking_timeout_secs: 10
EOF
    api_yaml >> "$1"
    return
  fi
  need_password
  umask 077
  cat > "$1" <<EOF
chunk_size: 256
local_cpu: ${LMCACHE_LOCAL_CPU:-false}
max_local_cpu_size: ${LMCACHE_CPU_GB:-5}
remote_url: "${VALKEY_URL:-valkey://127.0.0.1:6379}"
remote_serde: "naive"
use_layerwise: false
save_decode_cache: false
save_unfull_chunk: false
pre_caching_hash_algorithm: sha256_cbor
blocking_timeout_secs: 10
extra_config:
  valkey_mode: "standalone"
  valkey_num_workers: 8
  save_chunk_meta: false
  valkey_password: "$VALKEY_PASSWORD"
EOF
  api_yaml >> "$1"
}

api_yaml() {
  # LMCache internal API (DELETE /cache/clear empties the CPU tier between cases), loopback only.
  [ -n "${LMCACHE_API_PORT:-}" ] || return 0
  printf 'internal_api_server_enabled: true\ninternal_api_server_host: "127.0.0.1"\ninternal_api_server_port_start: %d\n' \
    "$LMCACHE_API_PORT"
}

tc_lo() {
  # Only Valkey traffic is shaped: band 4 = to port 6379 (writes), band 5 = from port 6379
  # (reads), each its own rate-limited queue with TC_DELAY one way. Other loopback traffic
  # (HTTP, ZMQ events) keeps the default bands.
  case "$1" in
    on)
      sudo tc qdisc del dev lo root 2>/dev/null || true
      sudo tc qdisc add dev lo root handle 1: prio bands 5
      sudo tc qdisc add dev lo parent 1:4 handle 40: netem delay "$TC_DELAY" rate "$TC_RATE" limit 200000
      sudo tc qdisc add dev lo parent 1:5 handle 50: netem delay "$TC_DELAY" rate "$TC_RATE" limit 200000
      sudo tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip dport 6379 0xffff flowid 1:4
      sudo tc filter add dev lo parent 1: protocol ip prio 2 u32 match ip sport 6379 0xffff flowid 1:5
      tc qdisc show dev lo ;;
    off)
      sudo -n tc qdisc del dev lo root 2>/dev/null || true
      tc qdisc show dev lo ;;
    *) echo "tc on|off" >&2; exit 2 ;;
  esac
}

cache_node() {
  # Off-box Valkey for the TCP baseline. Run as root on the cache node, never on the GPU host. Binds the
  # node's private IP and 127.0.0.1 only, protected-mode on, plain TCP (no TLS, as in the benchmark).
  # The password is made once into the root-only /etc/valkey/pass and bind-mounted read-only; the
  # container writes it into a 0600 config file, so it is in no argv, no environment and not in the
  # container's stored config. PRIV overrides the bind address (default: IMDSv2 local-ipv4).
  # Restrict tcp/6379 to the GPU host's security group. Copy the password to the GPU host over ssh
  # (ssh cache sudo cat /etc/valkey/pass | ssh gpu 'umask 077; cat > ~/.valkey-pass').
  [ "$(id -u)" = 0 ] || { echo "cache-node: run as root" >&2; exit 2; }
  local priv=${PRIV:-} token mem_kb
  if [ -z "$priv" ]; then
    token=$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60") \
      || { echo "cache-node: IMDSv2 token failed (set PRIV)" >&2; exit 1; }
    priv=$(curl -sf -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/local-ipv4) \
      || { echo "cache-node: IMDSv2 local-ipv4 failed (set PRIV)" >&2; exit 1; }
  fi
  case "$priv" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) ;;
    *) echo "cache-node: '$priv' is not a private IPv4 address (set PRIV)" >&2; exit 1 ;;
  esac
  ip -4 -o addr show | grep -qw "$priv" || { echo "cache-node: $priv is not on this host" >&2; exit 1; }
  mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
  local gb=$((mem_kb * 7 / 10 / 1024 / 1024))   # about 70% of RAM; 0 would mean no limit
  [ "$gb" -ge 1 ] || gb=1
  local maxmem=${VALKEY_MAXMEMORY:-${gb}gb}
  if [ ! -s /etc/valkey/pass ]; then
    install -d -m 700 /etc/valkey
    (umask 077; openssl rand -hex 32 > /etc/valkey/pass)
  fi
  chown root:root /etc/valkey/pass; chmod 600 /etc/valkey/pass
  docker rm -f kvt-valkey >/dev/null 2>&1 || true
  # Kit settings (io-threads 4, lazyfree, no output buffer limit), standalone, no AOF or RDB.
  PRIV=$priv MAXMEM=$maxmem IO_THREADS=${IO_THREADS:-4} \
    docker run -d --name kvt-valkey --network host -e PRIV -e MAXMEM -e IO_THREADS \
    -v /etc/valkey/pass:/run/valkey-pass:ro --ulimit nofile=65536:65536 "$VALKEY_IMAGE" sh -c '
    umask 077; printf "requirepass \"%s\"\n" "$(cat /run/valkey-pass)" > /tmp/auth.conf; chown valkey /tmp/auth.conf
    exec setpriv --reuid=valkey --regid=valkey --clear-groups valkey-server /tmp/auth.conf \
      --bind "$PRIV 127.0.0.1" --port 6379 --protected-mode yes --save "" --appendonly no \
      --maxmemory "$MAXMEM" --maxmemory-policy allkeys-lru --io-threads "$IO_THREADS" \
      --lazyfree-lazy-eviction yes --lazyfree-lazy-user-flush yes --tcp-backlog 65535 --maxclients 60000 \
      --client-output-buffer-limit "normal 0 0 0" --latency-tracking yes --dir /tmp' >/dev/null
  sleep 2
  ss -ltnp | grep ':6379 ' || { docker logs kvt-valkey >&2; exit 1; }
}

case "${1:-}" in
  install) install "${2:?PATCH_DIR}" ;;
  valkey) valkey ;;
  lmcache-yaml) lmcache_yaml "${2:?FILE}" ;;
  tc) tc_lo "${2:?on|off}" ;;
  cache-node) cache_node ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac
