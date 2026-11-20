"""ECDL pipeline monitoring collectors.

Backs the "Pipeline Monitor" page: one card per component, in flow order

    source Aurora -> Debezium Server -> broker stream -> ECS sink -> target Valkey

Flask is the only tier that can reach Aurora, the broker, and the target, so all
polling happens here and the browser makes ONE request every couple of seconds
to /api/monitor/dashboard.

Design rules kept deliberately simple:
  * Every collector is wrapped so a single unreachable component degrades ONE
    card ({"ok": false, "error": ...}) instead of blanking the page.
  * Per-second rates are derived SERVER-SIDE by diffing the previous sample of a
    monotonic counter, so they stay correct no matter how the browser refreshes.
  * CloudWatch answers are cached (60s metrics, so polling them every 2s would
    make many unnecessary API calls) and are always labelled as lagging in the payload.
  * Read-only, apart from two POST actions that need the link named in the
    request: the lag probe (stamps one column of one source row and restores
    it) and the demo run queue (writes a job to that link's broker). GET never
    writes. Nothing here writes to the target.

Defaults below are the values this repo already uses (see
data-plane/debezium-server/application.properties for the offset/schema keys,
infra/ecdl-cdc-ecs.yaml for the service names, log groups and CONSUMER_GROUP).
For a deployed link they are derived per link (link_config). Everything is
overridable by environment variable.
"""

import collections
import copy
import decimal
import json
import math
import os
import random
import re
import contextlib
import functools
import threading
import time

from flask import Blueprint, jsonify, request

import repo_path  # noqa: F401  (puts ecdl_link on sys.path)
import alarm_topic
from ecdl_link import sqlident
from ecdl_link.provision import progress_key, progress_key_base

monitor_bp = Blueprint('monitor', __name__, url_prefix='/api/monitor')

# ---------------------------------------------------------------------------
# Optional dependencies. Import failures must not take down the whole app, so
# they are recorded and reported as an error on the affected card instead.
# ---------------------------------------------------------------------------
try:                                          # Valkey client (falls back to
    import valkey as _vk                      # redis-py, which is API-compatible)
    from valkey.cluster import ValkeyCluster as _VkCluster
    _VK_LIB = 'valkey'
except ImportError:                           # pragma: no cover - env dependent
    try:
        import redis as _vk
        from redis.cluster import RedisCluster as _VkCluster
        _VK_LIB = 'redis'
    except ImportError:
        _vk, _VkCluster, _VK_LIB = None, None, None

try:
    import pymysql
except ImportError:                           # pragma: no cover - env dependent
    pymysql = None

try:
    import boto3
except ImportError:                           # pragma: no cover - env dependent
    boto3 = None

try:
    import requests as _http
except ImportError:                           # pragma: no cover - env dependent
    _http = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _env(key, default=''):
    v = os.getenv(key)
    return v if v not in (None, '') else default


def _env_int(key, default):
    try:
        return int(_env(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key, default):
    return _env(key, 'true' if default else 'false').strip().lower() in ('1', 'true', 'yes', 'on')


def _default_region():
    """The region to call AWS in.

    DEMO_AWS_REGION first, for an explicit override. Then the standard AWS
    variables: the control plane passes AWS_DEFAULT_REGION to this container,
    and reading only DEMO_AWS_REGION meant a control plane outside us-east-2
    looked for its stacks, services and metrics in the wrong region. Then
    whatever region boto3 itself would use (~/.aws/config). us-east-2 is the
    last resort only, kept for the demo pipeline it was written for.
    """
    for key in ('DEMO_AWS_REGION', 'AWS_REGION', 'AWS_DEFAULT_REGION'):
        value = _env(key).strip()
        if value:
            return value
    try:
        region = boto3.session.Session().region_name if boto3 else None
    except Exception:                                     # noqa: BLE001
        region = None
    return region or 'us-east-2'


_DEFAULTS = {
    'region': _default_region(),

    # The link this page shows when no ?link= is requested. Set to a deployed
    # link name and the default view resolves through link_config() -- the same
    # path "Watch this pipeline" uses -- instead of the hardcoded env endpoints
    # below, which describe whatever pipeline this control plane was installed
    # against and can remain after it is deleted. Empty = use the individual DEMO_* endpoints.
    'default_link': _env('DEMO_DEFAULT_LINK', ''),

    # Source Aurora (MySQL)
    'db_host': _env('DEMO_DB_HOST',
                    'ecdl-demo-source-dbcluster-xxxxxxxxxxxx.cluster-clusterxxxxxx'
                    '.us-east-2.rds.amazonaws.com'),
    'db_port': _env_int('DEMO_DB_PORT', 3306),
    'db_user': _env('DEMO_DB_USER', 'admin'),
    'db_name': _env('DEMO_DB_NAME', 'Inventory'),
    'db_secret_id': _env('DEMO_DB_SECRET_ID', 'ecdl-demo-db-password'),
    'table': _env('DEMO_TABLE', 'products'),
    'rds_cluster_id': _env('DEMO_RDS_CLUSTER_ID', 'ecdl-demo-source-dbcluster-xxxxxxxxxxxx'),

    # Broker Valkey (cluster-mode OFF) — carries the CDC stream plus Debezium's
    # offset and schema-history keys. Brokers created by
    # ecdl-broker-quickstart.yaml now have TLS and a generated auth token; the
    # defaults below match the demo broker, which predates that.
    'broker_host': _env('DEMO_BROKER_HOST',
                        'ecdl-demo-broker-cache.xxxxxx.ng.0001.use2.cache.amazonaws.com'),
    'broker_port': _env_int('DEMO_BROKER_PORT', 6379),
    'broker_tls': _env_bool('DEMO_BROKER_TLS', False),
    # Usernames are not secret and are useful on the health page. The matching
    # passwords are deliberately NOT in CFG -- /health echoes CFG, so a credential
    # placed here would be published. They are read via cache_password() and
    # broker_password() instead, the same way db_password() already works.
    'broker_username': _env('DEMO_BROKER_USERNAME', ''),
    'broker_secret_id': _env('DEMO_BROKER_SECRET_ID', ''),
    # A link routes EACH TABLE to its own broker stream, so one stream name can
    # only ever describe one table of it. DEMO_STREAMS takes the same
    # comma-separated "stream=table,stream" form the Go sink's STREAMS variable
    # uses (tools/go-sink/streams.go), so the two sides are configured from one
    # string and cannot disagree about which streams exist.
    #
    # stream_key is kept as the FIRST stream. Every collector still reports its
    # familiar top-level scalars as a roll-up across tables, and the per-table
    # detail arrives alongside — dropping those scalars would silently blank
    # about forty fields on the page, which renders as em-dashes rather than an
    # error.
    'streams': _env('DEMO_STREAMS', ''),
    'stream_key': _env('DEMO_STREAM_KEY', 'data:ecdl.Inventory.products'),
    'consumer_group': _env('DEMO_CONSUMER_GROUP', 'ecdl-sink'),
    'offset_key': _env('DEMO_OFFSET_KEY', 'ecdl:metadata:offsets'),
    'schema_history_key': _env('DEMO_SCHEMA_HISTORY_KEY', 'ecdl:metadata:schema_history'),

    # Target Valkey (cluster-mode ON, TLS)
    'cache_host': _env('DEMO_CACHE_HOST',
                       'clustercfg.ecdl-demo-valkey.xxxxxx.use2.cache.amazonaws.com'),
    'cache_port': _env_int('DEMO_CACHE_PORT', 6379),
    'cache_tls': _env_bool('DEMO_CACHE_TLS', True),
    # Topology is a property of the replication group, resolved from its ARN, not
    # a preference. Both topologies have to work: a cluster client cannot talk to a
    # single node, and a plain client aimed at a sharded cluster connects and then
    # returns MOVED for most keys.
    'cache_cluster_mode': _env_bool('DEMO_CACHE_CLUSTER_MODE', True),
    # Set only when the cache has an auth token (password alone) or RBAC
    # (username and password) enabled. The password itself is not held here --
    # see the note on broker_username above.
    'cache_username': _env('DEMO_CACHE_USERNAME', ''),
    'cache_secret_id': _env('DEMO_CACHE_SECRET_ID', ''),
    'key_prefix': _env('DEMO_KEY_PREFIX', 'products:'),
    'cache_cluster_id': _env('DEMO_CACHE_CLUSTER_ID', 'ecdl-demo-valkey-0001-001'),

    # The transform applied to the monitored table, so the data-flow reconcile
    # can compare an Aurora column against the cache field it is actually written to.
    # A rename ({source: cache_field}) moves a value to a different field, and a
    # column selection drops some entirely; without these the panel compares a
    # source name against a cache field that no longer exists and reports a false
    # "differs". Empty rename + None columns = identity mapping, all columns kept
    # (the correct default for the demo pipeline and any link with no transform).
    'transform_rename': {},
    'transform_columns': None,
    # The full per-table transforms map {bare_table: {rename, columns, computed}},
    # so the multi-table data-flow panel can compare EACH table against the fields
    # its own rules produce. The single transform_rename/columns above stay for the
    # first table and the collectors that still describe one.
    'transforms': {},

    # ECS services
    'ecs_cluster': _env('DEMO_ECS_CLUSTER', 'ecdl-demo-cluster'),
    'debezium_service': _env('DEMO_DEBEZIUM_SERVICE', 'ecdl-demo-cdc-debezium'),
    'sink_service': _env('DEMO_SINK_SERVICE', 'ecdl-demo-cdc-sink'),
    # Log groups are named after the STACK that declares them, which is not the
    # ECS cluster name: '/ecs/<cdc-stack>/debezium'. The loader's group has no
    # sub-path at all ('/ecs/<loader-stack>') because its template declares it
    # that way. link_config() derives all three per link.
    'debezium_log_group': _env('DEMO_DEBEZIUM_LOG_GROUP', '/ecs/ecdl-demo-cdc/debezium'),
    'sink_log_group': _env('DEMO_SINK_LOG_GROUP', '/ecs/ecdl-demo-cdc/sink'),
    'loader_log_group': _env('DEMO_LOADER_LOG_GROUP', '/ecs/ecdl-demo-loader'),

    # Optional enrichment. Both are skipped cleanly when unset:
    #   demo_app_url          -> an application's /metrics (DB vs cache read latency)
    #   debezium_metrics_url   -> Debezium Server /q/metrics
    #   sink_metrics_url       -> Go sink health server (HEALTH_PORT, default 8081).
    # Fargate task IPs rotate and the security group generally will not allow
    # 8081 from this host, so the sink's own counters stay opt-in; applied/s is
    # derived from consumer-group progress instead.
    'demo_app_url': _env('DEMO_APP_URL', ''),
    'debezium_metrics_url': _env('DEMO_DEBEZIUM_METRICS_URL', ''),
    'sink_metrics_url': _env('DEMO_SINK_METRICS_URL', ''),

    # Lag probe. The sink ACKs then XDELs each entry, so a healthy pipeline holds
    # NO resident stream entries to read a timestamp from — passive measurement
    # only works while the sink is behind, which is the opposite of what is needed. The probe instead
    # stamps one dedicated heartbeat row in Aurora and times how long that value
    # takes to appear in the cache: a true end-to-end number, on demand.
    # It writes to the source, so it is POST-only, runs only for a link named
    # in the request, touches one pre-existing row and puts it back, and can be
    # turned off entirely.
    'probe_enabled': _env_bool('DEMO_PROBE_ENABLED', True),
    'probe_timeout_ms': _env_int('DEMO_PROBE_TIMEOUT_MS', 8000),
    'probe_min_interval_sec': _env_int('DEMO_PROBE_MIN_INTERVAL_SEC', 2),
    # Capture latency is read from the newest event still resident in a stream
    # (arrived - committed). That is only a CURRENT reading if the event arrived
    # recently: the sink leaves a few entries un-XDEL'd, and such a leftover
    # carries the capture lag fixed at the value from when it was written (observed: a
    # leftover from a load burst reported ~130s indefinitely, skewing the in-flight
    # lag). If the newest resident entry arrived longer ago than this, treat it as
    # "no current measurement" rather than a live number.
    'capture_fresh_sec': _env_int('DEMO_CAPTURE_FRESH_SEC', 60),
    # A fresh event captured longer than this after its commit is a replay from
    # an older position (after a load, a restart from an older offset, a broker
    # restore): the sink is "catching up", and cached rows can show older values
    # until it finishes (Jira story F043). Same default as the sink's CATCHING_UP_SECONDS.
    'catching_up_sec': _env_int('DEMO_CATCHING_UP_SEC', 60),
    # Past this age a probe result is flagged stale, so an old number is never
    # read as the current state of the pipeline.
    'probe_stale_sec': _env_int('DEMO_PROBE_STALE_SEC', 300),
    # The history sampler can run a self-probe this often (seconds) so the
    # CDC-lag TREND has a real end-to-end number even at idle -- a healthy
    # drained pipeline has no in-flight lag to sample. OFF by default: every
    # probe writes to a row of the customer's source table, so it runs only for
    # the one link named in DEMO_PROBE_AUTO_LINK, and only when both are set.
    'probe_auto_sec': _env_int('DEMO_PROBE_AUTO_SEC', 0),
    'probe_auto_link': _env('DEMO_PROBE_AUTO_LINK', ''),

    # Delivery integrity. Defaults mirror the Go sink's reliability layer
    # (tools/go-sink/reliability.go): failed writes stay unacked in the pending
    # list, are reclaimed after RETRY_IDLE_MS, and are dead-lettered to
    # DLQ_STREAM_PREFIX + stream once MAX_DELIVERIES is exceeded.
    'dlq_prefix': _env('DEMO_DLQ_PREFIX', 'dlq:'),
    'max_deliveries': _env_int('DEMO_MAX_DELIVERIES', 5),
    'broker_mem_warn_pct': _env_int('DEMO_BROKER_MEM_WARN_PCT', 75),
    'consumer_active_idle_ms': _env_int('DEMO_CONSUMER_ACTIVE_IDLE_MS', 60000),
    # A stream is 'stalled' only when its UNDELIVERED count has not gone down
    # for this long while nothing drains. Judged on one poll, a single unacked
    # straggler (or the few leftover entries the sink did not XDEL) read as
    # "nothing is consuming" before the sink's reclaim had a chance to act.
    'stall_window_sec': _env_int('DEMO_STALL_WINDOW_SEC', 60),
    # A dead letter counts as current loss while the DLQ is growing or its
    # newest entry is younger than this. Older ones are reported as earlier
    # dead letters (warn), so one entry from last month does not keep the sink red.
    'dlq_recent_sec': _env_int('DEMO_DLQ_RECENT_SEC', 3600),
    # In-flight CDC lag is capture + the age of the oldest not-yet-applied entry.
    # That age climbs with wall-clock time for a single stuck/orphaned straggler
    # (one entry left in a dead consumer's pending list), so a caught-up pipeline
    # would show an incorrect, steadily growing figure as its main lag value. A real draining
    # backlog is many entries deep and moving; a straggler is one or two entries
    # sitting still. Only show in-flight lag as the main figure when the queue is at least this
    # deep — below it, fall through to the probe, and let the straggler surface
    # separately as a "stuck" warning on the sink hop.
    'inflight_min_backlog': _env_int('DEMO_INFLIGHT_MIN_BACKLOG', 50),
    # Largest OFFSET used when picking a random row. Each sample is an index-only
    # scan of at most this many primary-key entries, so it stays cheap; raise it
    # to spread samples wider across a bigger table.
    'random_sample_stride': _env_int('DEMO_RANDOM_SAMPLE_STRIDE', 400000),

    # Trend history. A browser-side ring buffer is lost on reload and only covers the
    # time the page happened to be open, which is useless for comparing a load
    # phase that has already finished. This samples in the background regardless
    # of whether anyone is watching, so the "before" is still there afterwards.
    # Where the bulk loader publishes its progress, and how long after its last
    # update we still call a run "active".
    # A loader run by hand publishes to ecdl:bulkload:<key prefix> (see
    # progress_key_for in data-plane/bulk-loader/bulk_loader.py).
    'bulkload_key': _env('DEMO_BULKLOAD_KEY', 'ecdl:bulkload:{}'.format(
        _env('DEMO_KEY_PREFIX', 'products:').rstrip(':'))),
    'bulkload_active_ms': _env_int('DEMO_BULKLOAD_ACTIVE_MS', 15000),
    # A run that has not completed and has published nothing for this long is
    # shown as stalled (or stopped), not as merely inactive.
    'bulkload_stall_ms': _env_int('DEMO_BULKLOAD_STALL_MS', 600000),
    # Writes per second at any hop before the write path counts as "flowing".
    'writeflow_moving_per_sec': _env_int('DEMO_WRITEFLOW_MOVING_PER_SEC', 10),

    # Exact COUNT(*) is a full covering-index scan: ~6.2M rows read every time.
    # Once a minute that was both real load on the database whose CPU this page
    # exists to measure, and a multi-million spike in aurora rows_read/s. Rare
    # enough now to be background noise, and the samples taken during it are marked
    # so they can be excluded from the row-throughput aggregates.
    'row_count_ttl': _env_int('DEMO_ROW_COUNT_TTL', 900),
    'coverage_samples': _env_int('DEMO_COVERAGE_SAMPLES', 40),
    'coverage_ttl': _env_int('DEMO_COVERAGE_TTL', 900),
    # Both default OFF: a monitor should not scan the table whose row throughput
    # and CPU it is reporting. Turn either on when the exact figure matters more
    # than a clean measurement, or use POST /reconcile which is explicit.
    'exact_row_count': _env_bool('DEMO_EXACT_ROW_COUNT', False),
    'coverage_enabled': _env_bool('DEMO_COVERAGE_ENABLED', False),

    'history_enabled': _env_bool('DEMO_HISTORY_ENABLED', True),
    'history_interval_sec': _env_int('DEMO_HISTORY_INTERVAL_SEC', 5),
    # 3 hours, so a 120-minute view always has the full window behind it.
    'history_minutes': _env_int('DEMO_HISTORY_MINUTES', 180),
    # Persisted next to the app under ./data, which is a mounted volume, so a
    # redeploy mid-session does not delete the comparison data. '' disables it.
    'history_path': _env('DEMO_HISTORY_PATH', os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'data', 'monitor_history.jsonl')),

    # Timeouts, kept short so one slow component cannot stall a 2s poll.
    'connect_timeout': _env_int('DEMO_CONNECT_TIMEOUT', 3),
    'socket_timeout': _env_int('DEMO_SOCKET_TIMEOUT', 3),
    'http_timeout': _env_int('DEMO_HTTP_TIMEOUT', 3),
    'cloudwatch_ttl': _env_int('DEMO_CLOUDWATCH_TTL', 60),
    # How long a secret read from Secrets Manager is reused. Without a limit a
    # rotated password kept failing and a revoked one kept working until restart.
    'credential_ttl_sec': _env_int('DEMO_SECRET_TTL_SEC', 300),
}


# ---------------------------------------------------------------------------
# Watching one link at a time
# ---------------------------------------------------------------------------
#
# The defaults above describe ONE pipeline. That was fine while there was one, and
# actively misleading once the control plane could deploy more: someone deployed a
# link, opened this page, and read a healthy dashboard about a completely different
# pipeline. A healthy status for the wrong pipeline is more misleading than no status.
#
# Every collector reads CFG['...'] directly, in about forty places. Rather than
# thread a config object through all of them -- forty chances to miss one, and the
# one missed would silently report the default pipeline -- CFG consults a
# thread-local overlay first. A collector cannot tell the difference, and two
# concurrent requests watching different links cannot see each other's.


class _Config(dict):
    """Configuration with a per-request overlay.

    Subclasses dict so `CFG['x']`, `CFG.get('x')` and `CFG.items()` all keep
    working unchanged. `items()` is overridden as well because /health publishes it
    for diagnosis, and publishing the defaults while serving a different link would
    describe a pipeline that is not being viewed.
    """

    _local = threading.local()

    def _overlay(self):
        return getattr(self._local, 'overlay', None)

    def __getitem__(self, key):
        overlay = self._overlay()
        if overlay is not None and key in overlay:
            return overlay[key]
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def items(self):
        overlay = self._overlay()
        if overlay is None:
            return dict.items(self)
        merged = dict(self)
        merged.update(overlay)
        return merged.items()

    def watched_link(self):
        overlay = self._overlay() or {}
        return overlay.get('link_name')


CFG = _Config(_DEFAULTS)


def current_overlay():
    """The overlay in force on this thread, for handing to a worker thread.

    Needed because the dashboard collects in a ThreadPoolExecutor, and thread-local
    state is not inherited by the pool's threads. Without carrying it across, every
    collector ran against the defaults while the request believed it was watching a
    chosen link -- the page showed one pipeline's numbers under another's name,
    which is the exact failure this mechanism was added to remove.
    """
    return getattr(_Config._local, 'overlay', None)


def in_overlay(overlay, fn):
    """Run `fn` on another thread under `overlay`.

    Used instead of submitting the bare callable, so a collector behaves the same
    whether it runs inline or in a pool.
    """
    if overlay is None:
        return fn
    def run(*args, **kwargs):
        with watching(overlay):
            return fn(*args, **kwargs)
    functools.update_wrapper(run, fn)
    return run


@contextlib.contextmanager
def watching(overlay):
    """Serve one request against a different link.

    Thread-local rather than a swap-and-restore on the shared dict: the dev server
    is threaded, and a global swap would let a request for link A be answered with
    link B's endpoints halfway through.
    """
    previous = getattr(_Config._local, 'overlay', None)
    _Config._local.overlay = overlay or None
    try:
        yield
    finally:
        _Config._local.overlay = previous


def explicit_link():
    """The link this request NAMED (?link= or a JSON body), else None.

    A view can also be watching the configured DEMO_DEFAULT_LINK without the
    request naming it. Reading is fine then; writing is not, because the person
    pressing the button may be looking at a different pipeline.
    """
    if CFG.get('link_implicit'):
        return None
    return CFG.watched_link()


def needs_explicit_link(view):
    """Refuse a writing POST unless the request names its link."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not explicit_link():
            return jsonify({'ok': False, 'refused': True,
                            'error': WRITE_NEEDS_LINK}), 400
        return view(*args, **kwargs)
    return wrapper


WRITE_NEEDS_LINK = ('this action writes, so the request must name the link it is '
                    'for (?link=<name> or {"link": "<name>"}); it never runs '
                    'against the configured default pipeline')
PROBE_NEEDS_LINK = ('the probe writes to a source table, so it runs only for a '
                    'named link; ' + WRITE_NEEDS_LINK.split('; ', 1)[1])


def per_link(view):
    """Serve this endpoint against ?link=, when given.

    Applied to the read endpoints rather than only /dashboard: a selector that
    changed some panels and left others describing the default pipeline would be
    harder to understand than having no selector.
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        try:
            overlay = _requested_overlay()
        except LinkUnavailable as exc:
            # Temporary: say so, rather than "not deployed" (404), which makes the
            # page drop the selection.
            return jsonify({'ok': False, 'error': str(exc), 'retry': True}), 503
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 404
        if overlay is None:
            return view(*args, **kwargs)
        with watching(overlay):
            return view(*args, **kwargs)
    # Marked so a test can assert which routes are scoped. Without a marker the
    # only way to check was reading the source, and an unscoped route is exactly
    # the bug that made the page answer for the wrong pipeline.
    wrapper._per_link = True
    return wrapper



# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_samples = {}      # counter name -> (value, monotonic timestamp)
_rate_last = {}    # counter name -> last rate actually computed
# When the monitor last ran its own full-table COUNT(*). That scan reads the
# whole table, so any rows/s sample covering it is about the monitor, not the
# workload, and gets excluded from the row-throughput aggregates.
_last_row_scan_ms = 0
# Shortest gap that yields a trustworthy rate. Below this the divisor is small
# enough that a counter which advances in bursts reports a meaningless spike.
MIN_RATE_INTERVAL_SEC = 1.0
_cache = {}        # cache key    -> (value, monotonic expiry)
_secret_cache = {}  # secret id    -> (value, monotonic expiry); see secret_value()


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Which streams, and which table each carries
# ---------------------------------------------------------------------------
def table_from_stream(stream):
    """Recover the table name from a broker stream name.

    Debezium composes a topic as <prefix>.<...>.<table> and the RegexRouter
    rewrites it to data:<topic>, so the table is the LAST dot-separated segment.
    Reading the last segment rather than counting from the front is deliberate:
    the segment COUNT differs per engine (MySQL 3, PostgreSQL 3, SQL Server 4,
    Oracle 2), so any fixed position would be wrong for three of the four.

    This is the same rule as tableFromStream in tools/go-sink/streams.go and
    stream_key() in ecdl_link/resolve.py. Three implementations of one rule is
    more than is ideal, but the alternative is the monitor guessing at names
    the sink chose -- and a mismatch here would report a table that does not
    exist while missing the one that does.
    """
    text = (stream or '').strip()
    if '.' in text:
        text = text.rsplit('.', 1)[1]
    elif ':' in text:
        # A bare "data:orders" name, as the benchmark harnesses use.
        text = text.rsplit(':', 1)[1]
    return text.strip()


def parse_streams(raw, fallback_stream=None, fallback_table=None):
    """"s1=table1,s2,s3=table3" -> [{'stream': ..., 'table': ...}].

    An entry without "=" has its table derived. A duplicate stream is dropped
    rather than raising: this is a monitor, and refusing to report anything
    because one entry is repeated would be worse than reporting the rest.
    """
    specs, seen = [], set()
    for item in (raw or '').split(','):
        item = item.strip()
        if not item:
            continue
        stream, _, table = item.partition('=')
        stream, table = stream.strip(), table.strip()
        if not stream or stream in seen:
            continue
        seen.add(stream)
        specs.append({'stream': stream, 'table': table or table_from_stream(stream)})
    if not specs and fallback_stream:
        specs.append({'stream': fallback_stream,
                      'table': fallback_table or table_from_stream(fallback_stream)})
    return specs


def stream_specs():
    """The streams this monitor watches, and the table each one carries."""
    return parse_streams(CFG['streams'], CFG['stream_key'], CFG['table'])


def key_prefix_for(table):
    """How the sink names that table's keys.

    The sink writes `evt.Source.Table + ":" + pk`, so the prefix is the table
    name. CFG['key_prefix'] stays authoritative for the configured single table
    so an explicitly overridden prefix is still honoured.
    """
    if table and table == CFG['table'] and CFG['key_prefix']:
        return CFG['key_prefix']
    # The link's advanced.key_prefix (CDC stack parameter KeyPrefix), which the
    # sink puts in front of every table's keys.
    link_prefix = (CFG.get('link_key_prefix') or '').strip().rstrip(':')
    lead = link_prefix + ':' if link_prefix else ''
    return '{}{}:'.format(lead, table) if table else CFG['key_prefix']


def transform_for(table):
    """(rename_map, selected_columns_or_None) for one table's transform.

    Mirrors how the link overlay derives the FIRST table's transform, but for any
    table, so the multi-table data-flow panel compares each table against the
    fields its own rules produce (a `rename` sends a column to a different cache
    field; a `columns` list drops the rest). Falls back to the single-table CFG
    values when the table is the configured one, else identity (all columns
    cached, no rename).
    """
    tspec = (CFG.get('transforms') or {}).get(table)
    if not isinstance(tspec, dict):
        if table and table == CFG.get('table'):
            return (CFG.get('transform_rename') or {}), CFG.get('transform_columns')
        return {}, None
    rename = tspec.get('rename')
    rename = {str(k): str(v) for k, v in rename.items()} if isinstance(rename, dict) else {}
    cols = tspec.get('columns')
    if isinstance(cols, list) and cols:
        selected = [str(c) for c in cols]
        computed = tspec.get('computed')
        if isinstance(computed, list):
            selected += [str(c.get('name')) for c in computed
                         if isinstance(c, dict) and c.get('name')]
        return rename, selected
    return rename, None


def undelivered_of(group):
    """Entries added but not yet delivered to the consumer group.

    XINFO GROUPS 'lag' is the intended source, but Valkey/Redis returns it as
    NULL once entries have been XDEL'd from the stream -- and the Go sink acks
    then XDELs every entry, so 'lag' is null in normal operation. When that
    happens the backlog silently drops to "unacked only" and under-reports:
    observed live as backlog jumping from ~600K to a few thousand mid-drain while
    the queue was still deep.

    entries_added - entries_read is the same quantity and does NOT go null (the
    group's read counter stays populated even when lag does), so it is preferred.
    'lag'/'group_lag' is only a fallback for when the counters are unavailable.
    """
    group = group or {}
    added = group.get('entries_added')
    read = group.get('entries_read')
    if added is not None and read is not None:
        return max(0, int(added) - int(read))
    lag = group.get('group_lag')
    if lag is None:
        lag = group.get('lag')
    return int(lag or 0)


def backlog_of(group):
    """Entries outstanding for one consumer group: undelivered + unacked.

    NOT length + lag. An undelivered entry is counted in BOTH stream length and
    group lag, so adding those double-counts -- observed live as 4,000 for 2,000
    updates. This existed in three separate places (the broker card, the
    integrity block and the history sampler); three copies of one formula is how
    two of them end up disagreeing after an edit.

    Undelivered comes from undelivered_of (robust against Valkey's null 'lag'
    after XDELs); unacked is the group's pending count.
    """
    group = group or {}
    return undelivered_of(group) + int(group.get('pending') or 0)


def outstanding_backlog(length, last_delivered_id, last_generated_id, pending, group):
    """Entries still needing work, robust under load: the primary signal is the
    stream's RESIDENT LENGTH, not the group counters.

    Both counter signals go unreliable under heavy XDEL churn: XINFO 'lag'
    returns NULL, and so does the group's entries-read counter. Observed live: the
    counter backlog dropped to "pending only" (~10K) while the real queue was
    ~1M. The sink XDELs an entry only after it has acked it, so whatever is still
    resident is either unprocessed (undelivered + unacked) or a small pool of
    leftover entries the sink failed to delete. Resident length tracked the true
    backlog exactly in the load test, so it is used first; the counter form is a lower bound for
    the rare moment length is unavailable.

    The inaccuracy in length comes from those leftovers (~155 observed), which inflate it at
    idle. So when the group is genuinely caught up -- delivered up to the head AND
    nothing unacked -- report 0 and let the leftovers surface only as
    resident_length. While work is outstanding the leftover count is negligible.
    """
    caught_up = (last_delivered_id is not None and last_generated_id is not None
                 and last_delivered_id == last_generated_id
                 and int(pending or 0) == 0)
    if caught_up:
        return 0
    counter = undelivered_of(group) + int(pending or 0)
    return max(int(length or 0), counter)


def safe(fn, *args, **kwargs):
    """Run a collector and never raise. Returns its dict, or an error dict."""
    started = time.monotonic()
    try:
        out = fn(*args, **kwargs) or {}
        out.setdefault('ok', True)
    except Exception as exc:                              # noqa: BLE001
        out = {'ok': False, 'error': '{}: {}'.format(type(exc).__name__, exc)}
    out['collect_ms'] = round((time.monotonic() - started) * 1000, 1)
    return out


def scoped(name):
    """`name` as a key of this module's sample state, for the watched link.

    Rate baselines and the last capture reading were global, so the history
    sampler (following one link) and a page watching another wrote the same
    keys: each rate was computed from a mix of two pipelines' counters. The
    unwatched (configured) pipeline keeps the bare name.
    """
    link = CFG.watched_link()
    return '{}|{}'.format(link, name) if link else name


def rate(name, value, now=None):
    """Per-second rate of a monotonic counter, from the previous sample.

    Returns None on the first sample, when the counter went backwards (process
    restart / counter reset), or when no time has elapsed — all cases where a
    number would be misleading rather than a measurement. Baselines are kept
    per watched link (see scoped).
    """
    if value is None:
        return None
    name = scoped(name)
    now = time.monotonic() if now is None else now
    with _lock:
        prev = _samples.get(name)
        if prev is not None and now - prev[1] < MIN_RATE_INTERVAL_SEC:
            # Two callers sampling almost together (the 2s dashboard poll and the
            # 5s history sampler share this state) would divide a counter jump by
            # a few milliseconds. Counters like Innodb_rows_read advance in bulk
            # when a big statement finishes, so that produced peaks of 11M
            # rows/s. Hold the previous baseline and reuse the last rate instead
            # of reporting a false spike.
            return _rate_last.get(name)
        _samples[name] = (value, now)
    if prev is None:
        return None
    prev_value, prev_now = prev
    elapsed = now - prev_now
    if elapsed <= 0 or value < prev_value:
        return None
    computed = round((value - prev_value) / elapsed, 1)
    _rate_last[name] = computed
    return computed


def cached(key, ttl, producer):
    """Memoise an expensive lookup (CloudWatch) for ttl seconds."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
    value = producer()
    with _lock:
        _cache[key] = (value, now + ttl)
    return value


def reset_state():
    """Drop rate/cache state. Used by tests."""
    with _lock:
        _samples.clear()
        _rate_last.clear()
        _cache.clear()
        _secret_cache.clear()
        _undelivered_seen.clear()
        _dlq_seen.clear()
    _LINK_CACHE.clear()
    _auto_probe.update(last=None, stopped=None, logged=set())


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
def secret_value(secret_id):
    """A secret's password, from Secrets Manager, reused for secret_ttl_sec.

    The secret may be JSON with a "password" field (an RBAC user, a database
    secret) or a plain token. Never logged. The TTL is what lets a rotation be
    picked up and a revocation take effect without restarting the control plane;
    a credential the server rejects is dropped sooner, by forget_secret().
    """
    now = time.monotonic()
    with _lock:
        hit = _secret_cache.get(secret_id)
    if hit and hit[1] > now:
        return hit[0]
    if not boto3:
        raise RuntimeError('boto3 is not installed')
    client = boto3.client('secretsmanager', region_name=CFG['region'])
    raw = client.get_secret_value(SecretId=secret_id)['SecretString']
    try:
        value = json.loads(raw).get('password', '')
    except (ValueError, AttributeError):
        value = raw
    with _lock:
        _secret_cache[secret_id] = (value, now + max(0, CFG['credential_ttl_sec']))
    return value


def cached_secret(secret_id):
    """The cached value for secret_id while it is still fresh, else None."""
    with _lock:
        hit = _secret_cache.get(secret_id)
    return hit[0] if hit and hit[1] > time.monotonic() else None


def forget_secret(secret_id):
    """Drop a cached secret, so the next use reads Secrets Manager again."""
    if secret_id:
        with _lock:
            _secret_cache.pop(secret_id, None)


def _env_password(env_var):
    """A DEMO_*_PASSWORD value, but only when no link is being watched.

    Those variables belong to the endpoints configured on this host (the
    DEMO_* hosts). Sending them to a watched link's endpoints offered one
    pipeline's password to another pipeline's database or cache.
    """
    if CFG.watched_link():
        return ''
    return os.getenv(env_var) or ''


def db_password():
    """The DB password: DEMO_DB_PASSWORD with no link, else Secrets Manager."""
    direct = _env_password('DEMO_DB_PASSWORD')
    if direct:
        return direct
    secret_id = CFG['db_secret_id']
    if not secret_id or not boto3:
        return ''
    return secret_value(secret_id)


def _cache_credential(env_var, secret_key):
    """A Valkey credential: the env var with no link, else Secrets Manager.

    Mirrors db_password(). Deliberately not a CFG entry: /health echoes CFG, so a
    credential stored there would be served to anyone who can reach the page.
    """
    direct = _env_password(env_var)
    if direct:
        return direct
    secret_id = CFG[secret_key]
    if not secret_id or not boto3:
        return ''
    return secret_value(secret_id)


# Words a server uses when it rejects a credential. MySQL 1045 is "Access
# denied"; Valkey answers WRONGPASS or NOAUTH, and the client raises
# AuthenticationError.
_AUTH_FAILURE_WORDS = ('wrongpass', 'noauth', 'invalid password', 'invalid username',
                       'authentication', 'access denied', 'auth failed')


def is_auth_failure(exc):
    """Whether `exc` says the server refused our credential."""
    if pymysql is not None and isinstance(exc, pymysql.err.OperationalError):
        if exc.args and exc.args[0] in (1044, 1045, 1698):
            return True
    text = '{} {}'.format(type(exc).__name__, exc).lower()
    return any(word in text for word in _AUTH_FAILURE_WORDS)


def broker_password():
    """Broker auth token, generated by the broker stack. Never logged."""
    return _cache_credential('DEMO_BROKER_PASSWORD', 'broker_secret_id')


def cache_password():
    """Target cache credential, when the cache has auth enabled. Never logged."""
    return _cache_credential('DEMO_CACHE_PASSWORD', 'cache_secret_id')


def mysql_conn():
    if not pymysql:
        raise RuntimeError('pymysql is not installed')
    try:
        return pymysql.connect(
            host=CFG['db_host'], port=CFG['db_port'],
            user=CFG['db_user'], password=db_password(), database=CFG['db_name'],
            connect_timeout=CFG['connect_timeout'],
            read_timeout=CFG['socket_timeout'], write_timeout=CFG['socket_timeout'],
            autocommit=True,
        )
    except Exception as exc:
        # A rejected password is most likely a rotated one: read it again next time.
        if is_auth_failure(exc):
            forget_secret(CFG['db_secret_id'])
        raise


def broker_client():
    """Broker Valkey: single node, cluster-mode OFF.

    Cluster mode off is a requirement rather than a setting: Debezium's Redis sink
    is not cluster aware, and one stream lives in one hash slot regardless.

    Brokers created by ecdl-broker-quickstart.yaml now have in-transit encryption
    and a generated auth token, so this needs the credential to read stream and
    consumer-group state. Without it the dashboard's broker and sink panels would
    fail with NOAUTH.
    """
    if not _vk:
        raise RuntimeError('no valkey/redis client installed (pip install valkey)')
    kwargs = dict(
        host=CFG['broker_host'], port=CFG['broker_port'],
        socket_connect_timeout=CFG['connect_timeout'],
        socket_timeout=CFG['socket_timeout'],
        decode_responses=True,
    )
    if CFG['broker_tls']:
        # Verified: ElastiCache's Amazon-issued certificate covers its endpoints.
        kwargs.update(ssl=True, ssl_cert_reqs='required', ssl_check_hostname=True)
    secret = broker_password()
    if secret:
        kwargs['password'] = secret
        if CFG['broker_username']:
            kwargs['username'] = CFG['broker_username']
    return _vk.Valkey(**kwargs) if _VK_LIB == 'valkey' else _vk.Redis(**kwargs)


def target_client():
    """Target Valkey, using whichever client its topology needs.

    A replication group is cluster-mode enabled or disabled, and the two need
    different clients: a cluster client issues CLUSTER SLOTS and fails against a
    single node. This was hardcoded to the cluster client, so the monitor could
    only see a sharded target.

    `cache_cluster_mode` defaults to true, which is what every target this has
    been pointed at so far. The fan-out helpers below (`cluster_call`,
    `_sum_values`, `node_roles`) already tolerate a single-node client, so the
    collectors need no change.
    """
    kwargs = dict(
        host=CFG['cache_host'], port=CFG['cache_port'],
        socket_connect_timeout=CFG['connect_timeout'],
        socket_timeout=CFG['socket_timeout'],
        decode_responses=True,
    )
    if CFG['cache_tls']:
        # Verified: ElastiCache's Amazon-issued certificate covers its endpoints.
        kwargs.update(ssl=True, ssl_cert_reqs='required', ssl_check_hostname=True)
    secret = cache_password()
    if secret:
        kwargs['password'] = secret
        # A username without a password cannot authenticate, so it is only sent
        # alongside one -- otherwise the driver reports a confusing error rather
        # than the accurate "no credential configured".
        if CFG['cache_username']:
            kwargs['username'] = CFG['cache_username']

    if CFG['cache_cluster_mode']:
        if not _VkCluster:
            raise RuntimeError(
                'no valkey/redis cluster client installed (pip install valkey)')
        return _VkCluster(**kwargs)
    if not _vk:
        raise RuntimeError('no valkey/redis client installed (pip install valkey)')
    return _vk.Valkey(**kwargs) if _VK_LIB == 'valkey' else _vk.Redis(**kwargs)


def aws(service):
    if not boto3:
        raise RuntimeError('boto3 is not installed')
    return boto3.client(service, region_name=CFG['region'])


def http_json(url):
    if not _http:
        raise RuntimeError('requests is not installed')
    resp = _http.get(url, timeout=CFG['http_timeout'])  # nosec B113 - timeout is an int (DEMO_HTTP_TIMEOUT, default 3s)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Health / discovery
# ---------------------------------------------------------------------------
def _ping_aurora():
    conn = mysql_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT VERSION()')
        version = cur.fetchone()[0]
        return {'endpoint': '{}:{}'.format(CFG['db_host'], CFG['db_port']),
                'database': CFG['db_name'], 'version': version}
    finally:
        conn.close()


def _ping_broker():
    """Ping the broker and report what is actually there.

    This doubles as discovery: whether the configured stream key and Debezium
    offset key exist, and which consumer groups are attached.
    """
    client = broker_client()
    try:
        client.ping()
        stream_key, offset_key = CFG['stream_key'], CFG['offset_key']
        out = {
            'endpoint': '{}:{}'.format(CFG['broker_host'], CFG['broker_port']),
            'client_lib': _VK_LIB,
            'stream_key': stream_key,
            'stream_key_type': client.type(stream_key) or 'none',
            'offset_key': offset_key,
            'offset_key_type': client.type(offset_key) or 'none',
        }
        out['stream_key_found'] = out['stream_key_type'] == 'stream'
        out['offset_key_found'] = out['offset_key_type'] == 'hash'
        if out['stream_key_found']:
            groups = client.xinfo_groups(stream_key)
            out['groups'] = [g.get('name') for g in groups]
            out['consumer_group'] = CFG['consumer_group']
            out['consumer_group_found'] = CFG['consumer_group'] in out['groups']
        else:
            # Configured key is missing — surface candidates so the operator can
            # see what the real key is called instead of guessing.
            out['candidate_stream_keys'] = [
                k for k in client.scan_iter(match='data:*', count=200)
            ][:10]
        return out
    finally:
        _close(client)


def _ping_target():
    client = target_client()
    try:
        client.ping()
        return {'endpoint': '{}:{}'.format(CFG['cache_host'], CFG['cache_port']),
                'tls': CFG['cache_tls'], 'keys': _sum_values(client.dbsize())}
    finally:
        _close(client)


def _ping_ecs():
    resp = aws('ecs').describe_services(
        cluster=CFG['ecs_cluster'],
        services=[CFG['debezium_service'], CFG['sink_service']])
    services = {
        s['serviceName']: {'desired': s.get('desiredCount'),
                           'running': s.get('runningCount'),
                           'status': s.get('status')}
        for s in resp.get('services', [])
    }
    missing = [n for n in (CFG['debezium_service'], CFG['sink_service'])
               if n not in services]
    out = {'cluster': CFG['ecs_cluster'], 'services': services}
    if missing:
        out['missing'] = missing
    return out


def _ping_cloudwatch():
    """Cheapest possible permissions probe: one small GetMetricStatistics."""
    end = int(time.time())
    resp = aws('cloudwatch').get_metric_statistics(
        Namespace='AWS/RDS', MetricName='CPUUtilization',
        Dimensions=[{'Name': 'DBClusterIdentifier', 'Value': CFG['rds_cluster_id']}],
        StartTime=end - 600, EndTime=end, Period=300, Statistics=['Average'])
    return {'datapoints': len(resp.get('Datapoints', [])),
            'rds_cluster_id': CFG['rds_cluster_id'],
            'cache_cluster_id': CFG['cache_cluster_id']}


def _probe_http(url, label):
    if not url:
        return {'ok': True, 'configured': False,
                'note': '{} not configured (optional)'.format(label)}
    resp = _http.get(url, timeout=CFG['http_timeout'])  # nosec B113 - timeout is an int (DEMO_HTTP_TIMEOUT, default 3s)
    return {'ok': resp.ok, 'configured': True, 'url': url,
            'status_code': resp.status_code, 'bytes': len(resp.content)}


def _close(client):
    try:
        client.close()
    except Exception:                                     # noqa: BLE001
        pass


def _sum_values(value):
    """Cluster clients answer per-node dicts; single-node clients answer ints."""
    if isinstance(value, dict):
        return sum(int(v or 0) for v in value.values())
    return int(value or 0)


@monitor_bp.route('/health', methods=['GET'])
@per_link
def health():
    """Per-component reachability plus the discovered stream/offset/group names."""
    components = {
        'aurora': safe(_ping_aurora),
        'broker': safe(_ping_broker),
        'target': safe(_ping_target),
        'ecs': safe(_ping_ecs),
        'cloudwatch': safe(_ping_cloudwatch),
        'demo_app': safe(_probe_http,
                         CFG['demo_app_url'] and CFG['demo_app_url'].rstrip('/') + '/healthz',
                         'DEMO_APP_URL'),
        'debezium_metrics': safe(_probe_http, CFG['debezium_metrics_url'],
                                 'DEMO_DEBEZIUM_METRICS_URL'),
        'sink_metrics': safe(_probe_http, CFG['sink_metrics_url'],
                             'DEMO_SINK_METRICS_URL'),
    }
    required = ('aurora', 'broker', 'target', 'ecs')
    return jsonify({
        'ok': all(components[k].get('ok') for k in required),
        'collected_at': now_ms(),
        'region': CFG['region'],
        'components': components,
        'config': {k: v for k, v in CFG.items()
                   if k not in ('db_secret_id',) and not k.endswith('timeout')},
    })


# ---------------------------------------------------------------------------
# CloudWatch (60s metrics, so always cached and always labelled as lagging)
# ---------------------------------------------------------------------------
CW_NOTE = '60s average from CloudWatch, lags the live counters'


def soft(fn, *args, **kwargs):
    """safe() for a nested sub-section: returns its dict or {'error': ...}."""
    try:
        return fn(*args, **kwargs) or {}
    except Exception as exc:                              # noqa: BLE001
        return {'error': '{}: {}'.format(type(exc).__name__, exc)}


def pick_latest(datapoints, stat='Average'):
    """Newest value out of a GetMetricStatistics datapoint list."""
    if not datapoints:
        return None
    newest = max(datapoints, key=lambda d: d['Timestamp'])
    value = newest.get(stat)
    return None if value is None else round(float(value), 2)


def cw_latest(namespace, metric, dimensions, stat='Average', period=60, window=900):
    """Latest datapoint for one metric, memoised for cloudwatch_ttl seconds."""
    key = 'cw:{}:{}:{}:{}'.format(namespace, metric, sorted(dimensions.items()), stat)

    def produce():
        end = int(time.time())
        resp = aws('cloudwatch').get_metric_statistics(
            Namespace=namespace, MetricName=metric,
            Dimensions=[{'Name': k, 'Value': v} for k, v in dimensions.items()],
            StartTime=end - window, EndTime=end, Period=period, Statistics=[stat])
        points = resp.get('Datapoints', [])
        newest = max(points, key=lambda d: d['Timestamp']) if points else None
        return (pick_latest(points, stat),
                newest['Timestamp'].timestamp() if newest else None)

    value, timestamp = cached(key, CFG['cloudwatch_ttl'], produce)
    if timestamp:
        _CW_TS[(namespace, metric, _dims_key(dimensions))] = timestamp
    return value


def _dims_key(dimensions):
    """A hashable, order-free form of a CloudWatch dimensions dict."""
    return tuple(sorted((dimensions or {}).items()))


# Timestamp of the newest datapoint seen per (namespace, metric, dimensions).
# Dimensions are part of the key: without them one cluster's datapoint age was
# reported for another's metrics. CloudWatch
# publishes one-minute aggregates a minute or so after the fact, so a value here
# can legitimately disagree with the live counters beside it: "76% CPU" next to
# "4 reads/s" is a stale average, not a broken collector. The UI needs the age
# in order to say so.
_CW_TS = {}


def cw_age(namespace, *metrics, **kwargs):
    """Seconds since the newest datapoint for these metrics, or None.

    dimensions= names the resource, the same dict passed to cw_latest.
    """
    dims = _dims_key(kwargs.get('dimensions'))
    ages = [time.time() - _CW_TS[(namespace, m, dims)]
            for m in metrics if _CW_TS.get((namespace, m, dims))]
    return int(max(ages)) if ages else None


# ---------------------------------------------------------------------------
# Source Aurora
# ---------------------------------------------------------------------------
# Counters used for live rates. Reads come from Com_select; writes are the sum
# of the mutating Com_* counters (the demo's checkout is an UPDATE).
READ_VARS = ('Com_select',)
WRITE_VARS = ('Com_insert', 'Com_update', 'Com_delete', 'Com_replace')
GAUGE_VARS = ('Threads_running', 'Threads_connected', 'Uptime', 'Queries')
# Com_select counts STATEMENTS, which understates read work by orders of
# magnitude for anything that scans. The bulk load is 8 SELECTs covering 5.7M
# rows: a statement rate near zero while Aurora reads 34K rows/s. Innodb_rows_read
# is the row-level counter, so both are reported and labelled distinctly.
ROW_READ_VARS = ('Innodb_rows_read',)
ROW_WRITE_VARS = ('Innodb_rows_inserted', 'Innodb_rows_updated', 'Innodb_rows_deleted')


def global_status(cur):
    """SHOW GLOBAL STATUS as a dict, ints coerced where possible.

    One round trip for the whole table (a few hundred rows) is cheaper and more
    portable than several LIKE queries.
    """
    cur.execute('SHOW GLOBAL STATUS')
    out = {}
    for name, value in cur.fetchall():
        try:
            out[name] = int(value)
        except (TypeError, ValueError):
            out[name] = value
    return out


def binlog_status(cur):
    """Current binlog coordinate — the position Debezium is reading towards.

    SHOW MASTER STATUS is the MySQL 8.0 spelling; 8.4 renamed it to SHOW BINARY
    LOG STATUS. Try both so this keeps working across an engine upgrade.
    """
    for stmt in ('SHOW MASTER STATUS', 'SHOW BINARY LOG STATUS'):
        try:
            cur.execute(stmt)
            row = cur.fetchone()
        except Exception:                                 # noqa: BLE001
            continue
        if row:
            return {'binlog_file': row[0], 'binlog_pos': int(row[1])}
    return {'binlog_file': None, 'binlog_pos': None,
            'binlog_note': 'binlog status unavailable (needs REPLICATION CLIENT)'}


def sum_vars(status, names):
    """Sum a group of counters, ignoring any the server does not expose."""
    total, found = 0, False
    for name in names:
        value = status.get(name)
        if isinstance(value, int):
            total += value
            found = True
    return total if found else None


def max_connections(cur):
    """The connection ceiling, so a connection count can be shown as usage.

    "401 connections" means nothing on its own: against a limit of 2,000 it is
    20%, against 410 it is nearly exhausted.
    """
    try:
        cur.execute("SHOW VARIABLES LIKE 'max_connections'")
        row = cur.fetchone()
        return int(row[1]) if row else None
    except Exception:                                      # noqa: BLE001
        return None


def collect_aurora():
    conn = mysql_conn()
    try:
        cur = conn.cursor()
        status = global_status(cur)
        out = dict(binlog_status(cur))
        out['max_connections'] = max_connections(cur)
    finally:
        conn.close()

    out.update({
        'endpoint': '{}:{}'.format(CFG['db_host'], CFG['db_port']),
        'database': CFG['db_name'],
        'table': CFG['table'],
        # Statement rates (SELECT / INSERT+UPDATE+DELETE per second).
        'reads_per_sec': rate('aurora.reads', sum_vars(status, READ_VARS)),
        'writes_per_sec': rate('aurora.writes', sum_vars(status, WRITE_VARS)),
        # Row rates. A few big statements can move millions of rows, so this is
        # the accurate measure of how hard Aurora is working during a bulk load.
        'rows_read_per_sec': rate('aurora.rowsread', sum_vars(status, ROW_READ_VARS)),
        'rows_written_per_sec': rate('aurora.rowswritten',
                                     sum_vars(status, ROW_WRITE_VARS)),
        'queries_per_sec': rate('aurora.queries', status.get('Queries')),
        # New connections per second: the rate of the cumulative Connections
        # counter. Charted alongside the live open-connection count.
        'new_conn_per_sec': rate('aurora.newconn', status.get('Connections')),
        'threads_running': status.get('Threads_running'),
        'threads_connected': status.get('Threads_connected'),
        'uptime_sec': status.get('Uptime'),
    })
    if out.get('binlog_file') and out.get('binlog_pos') is not None:
        out['binlog'] = '{}:{}'.format(out['binlog_file'], format(out['binlog_pos'], ','))

    out['state'], out['label'] = 'ok', 'up'

    dim = {'DBClusterIdentifier': CFG['rds_cluster_id']}
    out['cloudwatch'] = soft(lambda: {
        'cpu_pct': cw_latest('AWS/RDS', 'CPUUtilization', dim),
        # The Maximum statistic, not the max of the Averages. A 60-second average
        # flattens a short burst: during the bulk load the averages sat at
        # 1.8-2.3% while the per-minute Maximum was 8-12.6%. Reporting a "peak"
        # taken from averaged samples understates every spike on the page.
        'cpu_max_pct': cw_latest('AWS/RDS', 'CPUUtilization', dim, stat='Maximum'),
        # Not shown as a metric — ACU was deliberately taken off this page because
        # it scales down slowly and gives misleading results when phases are compared one after another.
        # It is collected only because CPUUtilization is a percentage OF this
        # capacity, so without it a CPU figure cannot be compared between phases:
        # after a heavy phase leaves capacity at a high level, the next phase's real
        # work shows up as a couple of percent. Used to caption CPU, nothing else.
        'capacity_acu': cw_latest('AWS/RDS', 'ServerlessDatabaseCapacity', dim),
        # Open database connections, as CloudWatch reports them.
        'connections': cw_latest('AWS/RDS', 'DatabaseConnections', dim),
        # Freeable memory (bytes). The trend charts show this in place of read/
        # write latency; less freeable means more of the instance's memory is in
        # use. It is the memory metric CloudWatch exposes for RDS (there is no
        # direct "used" gauge).
        'freeable_memory_bytes': cw_latest('AWS/RDS', 'FreeableMemory', dim),
        # Aurora's own statement latency, in milliseconds. SelectLatency is the
        # read number; DML covers insert/update/delete and Commit is the durable
        # write cost, which is why writes look slower than reads.
        'select_latency_ms': cw_latest('AWS/RDS', 'SelectLatency', dim),
        'dml_latency_ms': cw_latest('AWS/RDS', 'DMLLatency', dim),
        'commit_latency_ms': cw_latest('AWS/RDS', 'CommitLatency', dim),
        # Storage-layer read latency in SECONDS; 0 means everything is being
        # served from the buffer pool rather than storage.
        'storage_read_latency_ms': _sec_to_ms(cw_latest('AWS/RDS', 'ReadLatency', dim)),
        'storage_write_latency_ms': _sec_to_ms(cw_latest('AWS/RDS', 'WriteLatency', dim)),
        'cluster_id': CFG['rds_cluster_id'],
        # How old these values are. Everything else on the Aurora card is read
        # live from SHOW GLOBAL STATUS; these lag by a minute or two.
        'age_sec': cw_age('AWS/RDS', 'CPUUtilization', 'SelectLatency',
                          'DMLLatency', 'CommitLatency', dimensions=dim),
        'note': CW_NOTE,
    })
    return out


def _sec_to_ms(value):
    """RDS ReadLatency/WriteLatency are in seconds; everything else here is ms."""
    return None if value is None else round(value * 1000.0, 3)


@monitor_bp.route('/aurora', methods=['GET'])
@per_link
def aurora():
    return jsonify(safe(collect_aurora))


# ---------------------------------------------------------------------------
# Shared long-lived clients
# ---------------------------------------------------------------------------
# Valkey clients pool internally and are thread-safe, so reuse them across polls
# instead of reconnecting every 2 seconds. A failure drops the cached client so
# the next poll reconnects.
_shared = {}


def _endpoint_identity(host_key, port_key, tls_key, cluster_key=None):
    """A pool key that changes when the endpoint does.

    Keyed on WHAT is being connected to, not on the word 'target'. The pool was
    keyed by the bare name, so the first client created -- for whichever pipeline
    was configured -- was handed to every later request including ones watching a
    different link. The effect was specific and misleading: the reported endpoint came
    from the config and was right, while the numbers came from the pooled
    connection and belonged to another pipeline. A dashboard naming one cache and
    counting another's keys.
    """
    parts = [CFG[host_key], str(CFG[port_key]), str(bool(CFG[tls_key]))]
    if cluster_key:
        parts.append(str(bool(CFG[cluster_key])))
    return '|'.join(parts)


_shared_creating = {}   # pool key -> lock held while that client is created


def _get_shared(name, factory):
    """The pooled client for `name`, creating it once.

    Two polls that both found no client used to create one each; the one
    stored second replaced the first, which was never closed -- a leaked
    connection pool per race. Creation is now serialised per key (not
    globally, so a slow connect to one endpoint does not hold up the others),
    and the second caller takes the client the first one made.
    """
    with _lock:
        client = _shared.get(name)
        if client is not None:
            return client
        creating = _shared_creating.setdefault(name, threading.Lock())
    with creating:
        with _lock:
            client = _shared.get(name)
        if client is None:
            client = factory()
            with _lock:
                _shared[name] = client
    return client


def _drop_shared(name):
    with _lock:
        client = _shared.pop(name, None)
    if client is not None:
        _close(client)


def get_broker():
    return _get_shared(
        'broker:' + _endpoint_identity('broker_host', 'broker_port', 'broker_tls'),
        broker_client)


def get_target():
    return _get_shared(
        'target:' + _endpoint_identity('cache_host', 'cache_port', 'cache_tls',
                                       'cache_cluster_mode'),
        target_client)


def _drop_broker():
    _drop_shared('broker:' + _endpoint_identity(
        'broker_host', 'broker_port', 'broker_tls'))


def _drop_target():
    _drop_shared('target:' + _endpoint_identity(
        'cache_host', 'cache_port', 'cache_tls', 'cache_cluster_mode'))


def with_broker(fn):
    """Run fn(broker_client); on failure drop the cached client and re-raise.

    An authentication failure also drops the cached secret, so a rotated token
    is read again on the next poll instead of failing until the TTL runs out.
    """
    try:
        return fn(get_broker())
    except Exception as exc:
        _drop_broker()
        if is_auth_failure(exc):
            forget_secret(CFG['broker_secret_id'])
        raise


def with_target(fn):
    try:
        return fn(get_target())
    except Exception as exc:
        _drop_target()
        if is_auth_failure(exc):
            forget_secret(CFG['cache_secret_id'])
        raise


# ---------------------------------------------------------------------------
# Pure helpers for CDC bookkeeping
# ---------------------------------------------------------------------------
def stream_id_ms(entry_id):
    """Valkey stream IDs are '<millis>-<seq>', so the ID itself is a timestamp."""
    if not entry_id:
        return None
    try:
        return int(str(entry_id).split('-')[0])
    except (ValueError, IndexError):
        return None


def parse_offset_hash(raw):
    """Newest Debezium offset out of the RedisOffsetBackingStore hash.

    The store keeps field -> JSON, where the value carries the MySQL coordinate:
    {"file": "mysql-bin-changelog.001482", "pos": 68092104, "ts_sec": 1755...}.
    Multiple fields can exist (one per partition), so take the freshest.
    """
    best = None
    for value in (raw or {}).values():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict) or 'pos' not in parsed:
            continue
        candidate = {
            'file': parsed.get('file'),
            'pos': int(parsed['pos']),
            'ts_sec': parsed.get('ts_sec'),
        }
        if best is None or (candidate['ts_sec'] or 0) > (best['ts_sec'] or 0):
            best = candidate
    return best


def binlog_gap(source, committed):
    """Byte gap between Aurora's binlog head and Debezium's committed offset.

    IMPORTANT: this is NOT "how far behind Debezium is". The binlog carries
    traffic Debezium does not track — other tables, RDS internal writes,
    heartbeats — so the gap sits at a non-zero baseline even when Debezium is
    fully caught up. Observed live: stayed at exactly 6,083,104 bytes through
    30+ seconds of complete idle.

    What is meaningful is the TREND (see binlog_gap_trend): a flat gap means
    Debezium is keeping pace whatever its absolute value; a growing gap means it
    is falling behind. For actual latency use capture_lag_ms, which comes from
    the event timestamps themselves.
    """
    if not source or not committed:
        return {}
    s_file, s_pos = source.get('binlog_file'), source.get('binlog_pos')
    c_file, c_pos = committed.get('file'), committed.get('pos')
    if not s_file or not c_file or s_pos is None or c_pos is None:
        return {}
    if s_file != c_file:
        return {'binlog_gap_bytes': None,
                'binlog_gap_note': 'binlog rotated ({} -> {}); gap not comparable '
                                   'across files'.format(c_file, s_file)}
    gap = max(0, s_pos - c_pos)
    return {
        'binlog_gap_bytes': gap,
        'binlog_gap_trend_bytes_per_sec': rate('debezium.binlog_gap', gap),
        'binlog_gap_note': 'baseline is non-zero because the binlog includes '
                           'activity Debezium does not capture; watch the trend, '
                           'not the value (use capture lag for latency)',
    }


def remember_capture(lag_ms, now=None):
    """Cache the newest capture-lag measurement.

    The sink ACKs then XDELs each entry, so at low event rates the stream drains
    to empty within milliseconds and there is nothing left to measure. Rather
    than showing a blank headline whenever the pipeline is briefly idle, keep the
    last real measurement and report it with its age. Measuring afresh would
    require writing a heartbeat row to Aurora, which this page deliberately
    does not do.
    """
    with _lock:
        _samples[scoped('capture.last')] = (lag_ms, now if now is not None else time.time())


def last_capture(now=None):
    with _lock:
        entry = _samples.get(scoped('capture.last'))
    if not entry:
        return {}
    lag_ms, at = entry
    now = time.time() if now is None else now
    return {'capture_lag_ms_last': lag_ms,
            'capture_measured_age_sec': round(max(0.0, now - at), 1)}


def clock_skew_note(skew_ms):
    return ('clock skew: the source clock is {:,} ms ahead of the broker\u2019s, '
            'so capture lag reads 0 and is under-reported by up to that '
            'much'.format(int(skew_ms)))


def capture_lag_from_entry(entry_id, fields, now=None):
    """Aurora commit -> broker latency for the newest CDC event.

    The stream entry ID gives the millisecond the broker accepted the event; the
    Debezium envelope's source.ts_ms gives the moment the row was committed in
    Aurora. The difference is Debezium's capture latency, measured with no
    writes of our own.
    """
    arrived = stream_id_ms(entry_id)
    if arrived is None:
        return {'capture_lag_ms': None, 'capture_note': 'unreadable stream entry id'}
    raw = (fields or {}).get('value')
    try:
        event = json.loads(raw)
    except (TypeError, ValueError):
        return {'capture_lag_ms': None,
                'capture_note': 'stream entry has no JSON value field'}
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else event
    source = payload.get('source') if isinstance(payload.get('source'), dict) else {}
    committed = source.get('ts_ms') or payload.get('ts_ms')
    if not committed:
        return {'capture_lag_ms': None,
                'capture_note': 'event carries no source.ts_ms'}
    raw = arrived - int(committed)
    out = {'capture_lag_ms': max(0, raw), 'last_event_ms': arrived}
    if raw < 0:
        # The broker stored the event before the source says it was committed,
        # which only happens when the two clocks disagree. 0 is still shown as
        # the lag, but it is a floor rather than a measurement, and the skew is
        # reported so a "0 ms" is not read as a perfect pipeline.
        out['clock_skew_ms'] = -raw
        out['capture_note'] = clock_skew_note(-raw)
    if now:
        out['last_event_age_ms'] = max(0, now - arrived)
    return out


def catching_up_tables(per_table, limit_sec=None):
    """Tables whose newest fresh event was captured long after its commit.

    Debezium is then re-reading the source log from an older position (after a
    load, a restart from an older offset, a broker restore). The sink applies
    those changes in order, so until the replay drains a cached row can show an
    older value than it held before (Jira story F043). A dead letter being replayed keeps
    its original version and is not counted; neither is a stale leftover.
    """
    limit_ms = 1000 * (CFG['catching_up_sec'] if limit_sec is None else limit_sec)
    out = []
    for t in per_table or []:
        lag = t.get('capture_lag_ms')
        if lag is None or t.get('stale') or t.get('dead_letter_replay') or lag <= limit_ms:
            continue
        out.append({'table': t.get('table'), 'behind_ms': lag})
    return sorted(out, key=lambda t: -t['behind_ms'])


def ecs_state(desired, running, pending=0):
    """Map ECS counts to a card state. Returns (state, label).

    desiredCount == 0 is somebody's decision, not a failure: it is how the
    snapshot-then-stream handoff keeps CDC stopped while the initial load runs.
    A service that has actually failed still has desiredCount > 0 with
    nothing running, which the next case handles. Reporting the planned stop as
    'bad' turned a scripted demo step into an alarm.
    """
    desired = desired or 0
    running = running or 0
    if desired == 0:
        return 'paused', 'paused'
    if running == 0:
        # desired > 0 with nothing running is either a task still coming up or a
        # service that has failed, and the counts alone cannot tell them
        # apart. Debezium's own health check allows 90s to start, so treat this
        # as starting for a bounded window and only then call it down. Without
        # the window, turning CDC back on reads as an outage for two minutes.
        if (pending or 0) > 0:
            return 'starting', 'starting'
        return 'bad', 'down'
    if running < desired:
        return 'starting', 'starting'
    return 'ok', 'up'


# ---------------------------------------------------------------------------
# ECS + CloudWatch Logs lookups
# ---------------------------------------------------------------------------
def ecs_service(service):
    """Desired/running/pending counts and uptime of the oldest running task."""
    def produce():
        client = aws('ecs')
        resp = client.describe_services(cluster=CFG['ecs_cluster'], services=[service])
        services = resp.get('services') or []
        if not services:
            raise RuntimeError('service {} not found in cluster {}'.format(
                service, CFG['ecs_cluster']))
        svc = services[0]
        out = {
            'service': service,
            'desired': svc.get('desiredCount'),
            'running': svc.get('runningCount'),
            'pending': svc.get('pendingCount'),
            'service_status': svc.get('status'),
        }
        arns = client.list_tasks(cluster=CFG['ecs_cluster'], serviceName=service,
                                desiredStatus='RUNNING').get('taskArns') or []
        if arns:
            tasks = client.describe_tasks(cluster=CFG['ecs_cluster'],
                                          tasks=arns[:10]).get('tasks') or []
            started = [t['startedAt'].timestamp() for t in tasks if t.get('startedAt')]
            if started:
                out['uptime_sec'] = int(time.time() - min(started))
        return out

    # ECS APIs are throttled and the counts do not change every 2s.
    return cached('ecs:' + service, 10, produce)


def ecs_restarts(service, minutes=120):
    """Recent restarts of a service's tasks, from ECS's own stopped-task records.

    A container that restarts repeatedly keeps a "running" desired/running count while
    silently dying and being replaced, so the task-count health check alone reports
    healthy. ECS keeps the stopped tasks with a stoppedReason, which is the accurate
    record of a restart AND why ("Essential container in task exited",
    "OutOfMemoryError", a health-check failure), so this is read rather than
    guessed from log wording.

    Bounded to the 100 most recent stopped tasks ECS retains, filtered to the
    window. restarts=0 is a real measurement -- ECS has no stopped task -- not a
    suppressed error.
    """
    def produce():
        client = aws('ecs')
        arns = client.list_tasks(cluster=CFG['ecs_cluster'], serviceName=service,
                                 desiredStatus='STOPPED').get('taskArns') or []
        if not arns:
            return {'restarts': 0, 'window_min': minutes}
        cutoff = time.time() - minutes * 60
        tasks = client.describe_tasks(cluster=CFG['ecs_cluster'],
                                      tasks=arns[:100]).get('tasks') or []
        recent = []
        for task in tasks:
            stopped_at = task.get('stoppedAt')
            when = stopped_at.timestamp() if stopped_at else None
            if when is not None and when < cutoff:
                continue
            recent.append({
                'stopped_at': when,
                'age_sec': round(time.time() - when, 1) if when else None,
                'reason': (task.get('stoppedReason') or '').strip()[:300],
                'exit_code': _container_exit(task),
            })
        recent.sort(key=lambda item: item.get('stopped_at') or 0, reverse=True)
        out = {'restarts': len(recent), 'window_min': minutes}
        if recent:
            newest = recent[0]
            out['last_restart_age_sec'] = newest.get('age_sec')
            out['last_restart_reason'] = newest.get('reason') or None
            out['last_restart_exit_code'] = newest.get('exit_code')
            out['restart_sample'] = recent[:5]
        return out

    return cached('ecs_restarts:' + service, 30, produce)


def _container_exit(task):
    """The essential container's exit code, when ECS recorded one."""
    for container in task.get('containers') or []:
        code = container.get('exitCode')
        if code is not None:
            return code
    return None


def log_restart_count(component, minutes=120):
    """A CROSS-CHECK for ecs_restarts, counted from the container's own log.

    ECS's stopped-task record is authoritative for "did it restart and why". This
    is a second, independent signal: the number of times the process announced a
    (re)start in its log (the `restarted` needles in LOG_SIGNALS -- "Connector
    started", "Starting DebeziumServer", ...). The two are kept as SEPARATE
    labelled numbers and never merged, because they legitimately differ: a start
    line with no stopped task is the very first start (not a restart), and a
    stopped task with no start line is a crash before the process logged readiness.
    Disagreement is itself informative, so it must stay visible.

    Cheap on purpose: a server-side filtered read capped at 50, cached 30s, exactly
    like log_error_count -- NOT the 500-event scan log_activity does -- so attaching
    it to the 2s dashboard poll costs one throttled CloudWatch call per 30s.
    """
    needles = []
    for name, _desc, group_needles in LOG_SIGNALS.get(component, []):
        if name == 'restarted':
            needles = list(group_needles)
            break
    if not needles:
        return {'log_starts': None}
    try:
        group = log_group_for(component)
    except ValueError:
        return {'log_starts': None}

    def produce():
        limit = 50
        # '?"a" ?"b"' means match a OR b, as literal quoted substrings.
        pattern = ' '.join('?"{}"'.format(n.replace('"', '')) for n in needles)
        try:
            resp = aws('logs').filter_log_events(
                logGroupName=group,
                startTime=int((time.time() - minutes * 60) * 1000),
                filterPattern=pattern, limit=limit)
        except Exception as exc:                          # noqa: BLE001
            # Absent/denied group: report the state, never a fabricated 0.
            return {'log_starts': None, 'log_starts_state': log_state_from_error(exc)}
        events = resp.get('events') or []
        out = {'log_starts': len(events), 'log_starts_window_min': minutes}
        if len(events) >= limit:
            out['log_starts_capped'] = True
        if events:
            out['log_starts_newest'] = (events[-1].get('message') or '').strip()[:200]
        return out

    return cached('logstarts:' + group, 30, produce)


# The same filter as the DebeziumErrors and SinkErrors metric filters in
# infra/ecdl-cdc-ecs.yaml, so the count on the page and the alarm agree. The
# page used to match 'Error' and 'Exception' as well, so it showed errors for
# log lines the alarm (rightly) ignores, such as a logged exception that was retried.
LOG_ERROR_PATTERN = '?"ERROR" ?"FATAL"'


def log_error_count(log_group, minutes=5):
    """Recent ERROR/FATAL lines — a task that keeps restarting is easy to see."""
    def produce():
        limit = 50
        resp = aws('logs').filter_log_events(
            logGroupName=log_group,
            startTime=int((time.time() - minutes * 60) * 1000),
            filterPattern=LOG_ERROR_PATTERN,
            limit=limit)
        events = resp.get('events') or []
        out = {'log_errors': len(events), 'log_window_min': minutes}
        if len(events) >= limit:
            out['log_errors_capped'] = True
        if events:
            out['last_log_error'] = (events[-1].get('message') or '').strip()[:200]
        return out

    return cached('logs:' + log_group, 30, produce)


# ---------------------------------------------------------------------------
# Reading the components' logs
# ---------------------------------------------------------------------------
#
# The count above answers "is something wrong". It cannot answer "what". These
# endpoints return the lines themselves, because for two of the three components
# the log IS the only telemetry there is:
#
#   * Debezium Server publishes no metrics. There is no micrometer or JMX
#     extension in any application*.properties, only /q/health, so retries,
#     reconnects, snapshot progress and event counts exist solely as log lines.
#   * The Go sink DOES count writes/deletes/cycles/dlq/retried and serves them on
#     HEALTH_PORT, but its tasks run in awsvpc with rotating private addresses and
#     no load balancer, so there is no stable URL for this process to scrape. Its
#     periodic stats lines, shipped by awslogs, are reachable; its /metrics is not.
#
# Callers name a COMPONENT, never a log group. Accepting a group name would turn
# a read-only monitor route into "read any log group in the account", which is a
# real escalation given the task role this runs under.

LOG_COMPONENTS = {
    'debezium': 'debezium_log_group',
    'sink': 'sink_log_group',
    'loader': 'loader_log_group',
}

# The broker is an ElastiCache replication group, not a container: there is no
# log stream to read. Saying so is better than an empty pane that looks broken.
LOG_COMPONENT_NOTES = {
    'broker': 'the broker is an ElastiCache replication group, not a container, '
              'so it has no log stream. Its health is reported from the stream '
              'itself: backlog, consumer group lag and memory.',
}

# A restart more recent than this changes Debezium's card to "restarted". Long
# enough that a single deploy-time start does not linger as a warning, short
# enough that repeated restarts are caught while it is still happening.
RESTART_WARN_SEC = _env_int('DEMO_RESTART_WARN_SEC', 600)

LOG_LIMIT_MAX = 500
LOG_LIMIT_DEFAULT = 200
LOG_MINUTES_MAX = 10080         # 7 days. The groups retain 14, but a task that
                                # last logged 29h ago was unreachable behind a
                                # 24h ceiling, which reads as 'no logs' for a
                                # component that last ran yesterday.
LOG_MINUTES_DEFAULT = 30


def log_group_for(component):
    """The configured log group for a named component.

    Raises ValueError for anything not in the allowlist, so an unknown or
    manually written component is refused rather than passed to CloudWatch.
    """
    key = LOG_COMPONENTS.get((component or '').strip().lower())
    if not key:
        raise ValueError('unknown component {!r}; expected one of {}'.format(
            component, ', '.join(sorted(LOG_COMPONENTS))))
    group = CFG.get(key)
    if not group:
        raise ValueError('no log group configured for {}'.format(component))
    return group


def aws_error_code(exc):
    """The AWS error code on a boto exception, or None.

    Read defensively off the response dict rather than by importing botocore:
    this module already treats boto3 as optional, and the import would make it
    required just to classify an error.
    """
    response = getattr(exc, 'response', None)
    if isinstance(response, dict):
        code = (response.get('Error') or {}).get('Code')
        if code:
            return str(code)
    return None


def log_state_from_error(exc):
    """Map a failure to a state the UI can word correctly.

    The distinction is useful: "the group does not exist because no task has
    ever started" and "I am not allowed to read it" and "there are no errors" are
    three different situations that all look like an empty list.
    """
    code = aws_error_code(exc) or ''
    if code == 'ResourceNotFoundException':
        return 'absent'
    if code in ('AccessDeniedException', 'AccessDenied',
                'UnauthorizedOperation'):
        return 'denied'
    if code in ('ThrottlingException', 'LimitExceededException',
                'TooManyRequestsException'):
        return 'throttled'
    return 'error'


def task_id_from_stream(stream):
    """The ECS task id out of an awslogs stream name.

    awslogs composes '<stream-prefix>/<container-name>/<task-id>', so the task id
    is the last segment: 'debezium/debezium/abc123...' -> 'abc123...'.
    """
    text = (stream or '').strip()
    return text.rsplit('/', 1)[-1] if '/' in text else text


def classify_line(message):
    """Severity of one log line, for highlighting.

    Deliberately simple and case-sensitive on the level tokens containers actually
    emit. It is a display hint, never a count anyone reasons about.
    """
    text = message or ''
    for token in ('ERROR', 'FATAL', 'SEVERE', 'Exception', 'error:'):
        if token in text:
            return 'error'
    for token in ('WARN', 'Warning'):
        if token in text:
            return 'warn'
    return 'info'


def quote_filter(text):
    """Turn free text into a CloudWatch filter pattern matching it literally.

    A bare string is NOT a literal in CloudWatch's filter syntax: '?' means OR,
    '-' negates, and braces and brackets start JSON and space-delimited matchers.
    Typing a filter with any of those either changes the meaning silently or
    fails with InvalidParameterException. Wrapping it in double quotes makes it an
    exact substring match, which is what a filter box is expected to do.
    """
    cleaned = (text or '').strip()
    if not cleaned:
        return None
    # Escape the two characters that are special INSIDE a quoted term.
    cleaned = cleaned.replace('\\', '\\\\').replace('"', '\\"')
    return '"{}"'.format(cleaned)


def log_streams(component, limit=25):
    """The component's log streams, newest activity first."""
    group = log_group_for(component)
    out = {'component': component, 'log_group': group}
    try:
        resp = aws('logs').describe_log_streams(
            logGroupName=group, orderBy='LastEventTime', descending=True,
            limit=max(1, min(int(limit or 25), 50)))
    except Exception as exc:                              # noqa: BLE001
        out['state'] = log_state_from_error(exc)
        out['error'] = '{}: {}'.format(type(exc).__name__, exc)
        out['streams'] = []
        return out

    now = now_ms()
    streams = []
    for item in resp.get('logStreams') or []:
        last = item.get('lastEventTimestamp') or item.get('lastIngestionTime')
        streams.append({
            'stream': item.get('logStreamName'),
            'task_id': task_id_from_stream(item.get('logStreamName')),
            'first_event_ms': item.get('firstEventTimestamp'),
            'last_event_ms': last,
            'last_event_age_sec': round((now - last) / 1000.0, 1) if last else None,
            'stored_bytes': item.get('storedBytes'),
        })
    out['streams'] = streams
    out['state'] = 'ok' if streams else 'empty'
    if not streams:
        out['note'] = ('the log group exists but has no streams yet, so no task '
                       'has started')
    return out


def log_events(component, minutes=None, filter_text=None, limit=None,
               next_token=None, stream=None):
    """Log lines for one component, newest last.

    Ordering is left as CloudWatch returns it (oldest first within the window) so
    a multi-line Java stack trace stays in the order it was written. Reversing for
    display would split exception traces.
    """
    group = log_group_for(component)
    minutes = max(1, min(int(minutes or LOG_MINUTES_DEFAULT), LOG_MINUTES_MAX))
    limit = max(1, min(int(limit or LOG_LIMIT_DEFAULT), LOG_LIMIT_MAX))
    pattern = quote_filter(filter_text)

    out = {
        'component': component,
        'log_group': group,
        'window_min': minutes,
        'limit': limit,
        'filter': (filter_text or '').strip() or None,
    }

    params = {
        'logGroupName': group,
        'startTime': int((time.time() - minutes * 60) * 1000),
        'limit': limit,
    }
    if pattern:
        params['filterPattern'] = pattern
    if stream:
        # Scoped to one task. filter_log_events takes a list, and naming a stream
        # that does not exist is an error rather than an empty result, so this is
        # only ever set from a stream the caller got from /logs/streams.
        params['logStreamNames'] = [stream]
    if next_token:
        params['nextToken'] = next_token

    # filter_log_events scans the window in chunks and MAY RETURN AN EMPTY PAGE
    # WITH A nextToken while matching events still exist further on. Treating the
    # first empty page as "no logs" made a wide window on a sparse group report
    # nothing: a 60-minute window found the line because it was in the first
    # chunk, while a 7-day window over the same group returned zero -- and
    # get_log_events on the very same stream returned the events. So follow the
    # token while pages come back empty, bounded so a quiet 7-day group cannot
    # turn one request into an unbounded scan.
    MAX_EMPTY_PAGES = 12
    try:
        client = aws('logs')
        resp = client.filter_log_events(**params)
        pages = 0
        while (not resp.get('events')) and resp.get('nextToken') \
                and pages < MAX_EMPTY_PAGES:
            pages += 1
            resp = client.filter_log_events(
                **dict(params, nextToken=resp['nextToken']))
        if pages:
            out['scanned_pages'] = pages + 1
    except Exception as exc:                              # noqa: BLE001
        out['state'] = log_state_from_error(exc)
        out['error'] = '{}: {}'.format(type(exc).__name__, exc)
        out['events'] = []
        if out['state'] == 'absent':
            out['note'] = ('no log group {} yet — the task has not started, so '
                           'nothing has been logged'.format(group))
        elif out['state'] == 'denied':
            out['note'] = ('this control plane may not read {} — it needs '
                           'logs:FilterLogEvents on that group'.format(group))
        return out

    now = now_ms()
    events = []
    for item in resp.get('events') or []:
        ts = item.get('timestamp')
        message = (item.get('message') or '').rstrip('\n')
        events.append({
            'ts': ts,
            'age_sec': round((now - ts) / 1000.0, 1) if ts else None,
            'stream': item.get('logStreamName'),
            'task_id': task_id_from_stream(item.get('logStreamName')),
            'message': message,
            'severity': classify_line(message),
        })

    out['events'] = events
    out['count'] = len(events)
    out['next'] = resp.get('nextToken')
    out['state'] = 'ok' if events else 'empty'
    if not events:
        out['note'] = ('nothing logged in the last {} minute(s){}'.format(
            minutes, ' matching that filter' if pattern else ''))
        if resp.get('nextToken'):
            # Gave up before the end of the window, so "empty" here means "not in
            # the part I scanned" rather than "not logged". Said plainly, because
            # the two are different answers.
            out['note'] += (' — searched {} page(s) of that window and stopped; '
                            'narrow the range to search it fully'.format(
                                out.get('scanned_pages', 1)))
            out['partial'] = True
    return out


def log_components(link=None):
    """Which components have logs, for building the tabs.

    Reports each one's group and whether it is readable, so the UI can label an
    absent loader ("no load has run") differently from a denied one ("missing
    permission") without a second round trip per tab.
    """
    items = []
    for name in ('debezium', 'sink', 'loader'):
        entry = {'component': name}
        try:
            entry['log_group'] = log_group_for(name)
        except ValueError as exc:
            entry.update({'state': 'error', 'error': str(exc)})
            items.append(entry)
            continue
        probe = log_streams(name, limit=1)
        entry['state'] = probe.get('state')
        if probe.get('error'):
            entry['error'] = probe['error']
        if probe.get('note'):
            entry['note'] = probe['note']
        newest = (probe.get('streams') or [None])[0]
        if newest:
            entry['last_event_age_sec'] = newest.get('last_event_age_sec')
            entry['task_id'] = newest.get('task_id')
        if name == 'loader' and entry['state'] == 'absent':
            entry['note'] = ('no loader has run for this link — it is either a '
                             'cdc-only link or the load step has not started')
        items.append(entry)

    return {
        'link': link or CFG.watched_link(),
        'components': items,
        'without_logs': [{'component': key, 'note': note}
                         for key, note in sorted(LOG_COMPONENT_NOTES.items())],
    }


# ---------------------------------------------------------------------------
# Activity derived from the log lines
# ---------------------------------------------------------------------------
#
# What can reliably be read out of a log, and what cannot:
#
#   * LEVEL COUNTS are trustworthy. Both containers emit a level token, so "0
#     errors in the last 30 minutes" is a real measurement.
#   * THE SINK'S OWN COUNTERS are trustworthy when present. It prints one
#     periodic line (tools/go-sink/main.go:273) carrying writes, deletes and
#     rate. The ticker only prints when it has completed a cycle, so on an idle
#     link the line is ABSENT -- which is reported as absent, never as zeros.
#   * SINK RETRIES AND DEAD-LETTERS ARE NOT IN THE LOG AT ALL.
#     tools/go-sink/reliability.go has no print statements: retries and DLQ sends
#     only increment counters served on the unreachable /metrics port. They are
#     already measured properly from the broker by collect_integrity (DLQ stream
#     length, and pending entries whose times_delivered > 1), so this parser does
#     not invent them -- it points at that instead.
#   * EVERYTHING ELSE IS A BEST-EFFORT SUBSTRING. Debezium's message wording is
#     not a stable interface, so each signal reports its own match count and the
#     number of lines scanned. A signal that matches nothing is reported as
#     matches=0 alongside lines_scanned, so "nothing happened" can be told apart
#     from "I scanned nothing".

# Debezium's own throughput line, from io.debezium.connector.common.BaseSourceTask.
# Verified against two live pipelines rather than assumed:
#
#   2026-09-13 19:21:08,769 INFO [io.deb.con.com.BaseSourceTask] (pool-8-thread-1)
#   150 records sent during previous 01:59:22.329, last recorded offset of
#   {server=uitest} partition is {ts_sec=1789271961,
#   file=mysql-bin-changelog.000305, pos=67775737, server_id=1547354676}
#
# This is the ONLY place Debezium reports how much it has actually captured --
# the server publishes no metrics endpoint. The count is per interval, not
# cumulative, and the interval is however long it had been since the last such
# line, so the pair gives an accurate average rate for that window and nothing
# more. For "right now", the broker's produced_per_sec is the better signal.
DEBEZIUM_SENT_RE = re.compile(
    r'(?P<records>\d+)\s+records sent during previous\s+'
    r'(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2}(?:\.\d+)?)')
DEBEZIUM_OFFSET_RE = re.compile(
    r'file=(?P<file>[^,}\s]+),\s*pos=(?P<pos>\d+)')

# tools/go-sink/main.go:273 -- the only periodic stats line the sink emits.
# Confirmed against a live task's log:
#   [410s] writes=1 deletes=1 rate=0/sec | per-cycle: read=406.737383ms
#   parse=73.366us write=955.122us ack=2.156106ms batch=1
SINK_STATS_RE = re.compile(
    r'\[(?P<elapsed>[\d.]+)s\]\s+writes=(?P<writes>\d+)\s+deletes=(?P<deletes>\d+)'
    r'\s+rate=(?P<rate>[\d.]+)/sec')

# The bulk loader's progress line. Confirmed against a live task's log:
#   [ 1/16 chunks done]           55 rows |    0.5s |        102 rows/sec
#   [16/16 chunks done]        1,000 rows |    0.7s |      1,420 rows/sec
# Note the thousands separators, hence the [\d,]+ groups.
LOADER_PROGRESS_RE = re.compile(
    r'\[\s*(?P<done>\d+)\s*/\s*(?P<total>\d+)\s+chunks done\]\s+'
    r'(?P<rows>[\d,]+)\s+rows\s*\|\s*(?P<secs>[\d.]+)s\s*\|\s*'
    r'(?P<rate>[\d,]+)\s+rows/sec')
SINK_CYCLE_RE = re.compile(
    r'per-cycle:\s+read=(?P<read>\S+)\s+parse=(?P<parse>\S+)\s+'
    r'write=(?P<write>\S+)\s+ack=(?P<ack>\S+)\s+batch=(?P<batch>\d+)')

# Best-effort signals, by component. Each entry is (name, description, needles).
# Matching is case-sensitive on tokens the containers actually emit.
LOG_SIGNALS = {
    'debezium': [
        ('connect_trouble', 'could not reach the source database',
         ('Communications link failure', 'Connection refused', 'Cannot connect',
          'Could not connect', 'Connection reset', 'Access denied for user')),
        ('retrying', 'retrying an operation',
         ('Retrying', 'retrying', 'will retry', 'Attempting to reconnect')),
        ('restarted', 'the connector or task (re)started',
         ('Connector started', 'Starting DebeziumServer',
          'Starting connector', 'Connected to')),
        ('snapshot', 'snapshot activity', ('Snapshot', 'snapshot')),
        ('offset_commit', 'committed a source offset',
         ('last recorded offset', 'Committed offset', 'offset commit')),
        ('binlog_missing', 'the source log it needs has been purged',
         ('Could not find first log file', 'binlog has been purged',
          'Client requested master to start replication from position')),
    ],
    'sink': [
        ('connect_trouble', 'could not reach the broker or the target cache',
         ('connection refused', 'i/o timeout', 'NOAUTH', 'MOVED', 'CLUSTERDOWN',
          'context deadline exceeded')),
        ('restarted', 'the sink process started',
         ('Connected to', 'Health server on', 'Workers:')),
        ('fatal', 'the sink exited on a fatal error', ('FATAL',)),
        ('transforms', 'transform rules were loaded or rejected',
         ('Transforms:', 'transform spec')),
    ],
    'loader': [
        # 'chunks done' rather than a completion word: the loader has no "DONE"
        # line. It reports per-chunk progress and then stops, so completion is
        # done == total, which parse_loader_progress() derives instead.
        ('progress', 'the loader reported chunk progress', ('chunks done',)),
        ('range_split', 'the primary-key range was split into chunks',
         ('PK range', 'split into')),
        ('connect_trouble', 'could not reach the source or the target',
         ('connection refused', 'timeout', 'Access denied', 'NOAUTH')),
    ],
}

# Added after reading a live sink log, which states its retry/DLQ settings on
# startup: "Reliability: retry+DLQ ON (max_deliveries=5, retry_idle=30s, dlq=...)".
LOG_SIGNALS['sink'].append(
    ('reliability', 'the retry and dead-letter settings it started with',
     ('Reliability:',)))


def parse_sink_stats(lines):
    """The newest sink stats line, or absence.

    Returns {'observed': False} rather than zeros when the sink has not printed a
    stats line in the window. It only prints after completing a cycle, so an idle
    link legitimately has none, and reporting writes=0 would read as "the sink is
    doing nothing" when the truth is "the sink had nothing to do".
    """
    newest = None
    for line in lines:
        found = SINK_STATS_RE.search(line)
        if found:
            newest = (found, line)
    if not newest:
        return {'observed': False,
                'note': 'the sink prints its counters only after completing a '
                        'batch cycle, and has not in this window'}
    found, line = newest
    out = {
        'observed': True,
        'writes': int(found.group('writes')),
        'deletes': int(found.group('deletes')),
        'rate_per_sec': float(found.group('rate')),
        'uptime_sec': float(found.group('elapsed')),
        'source': 'the sink\u2019s own periodic stats line',
    }
    cycle = SINK_CYCLE_RE.search(line)
    if cycle:
        out['per_cycle'] = {
            'read': cycle.group('read'), 'parse': cycle.group('parse'),
            'write': cycle.group('write'), 'ack': cycle.group('ack'),
            'batch': int(cycle.group('batch')),
        }
    return out


def parse_debezium_records(lines):
    """Records Debezium reported sending, summed over the scanned window.

    Debezium Server exposes no metrics endpoint, so its periodic progress line is
    the ONLY place its own throughput is stated. An earlier version of this module
    asserted the figure was unavailable and pointed at the broker's entries_added
    instead; a real task log disproved that.

    Both figures are still worth having and they answer different questions: this
    is what Debezium claims to have SENT, entries_added is what actually ARRIVED
    in the broker, and a gap between them is precisely what you want to notice.

    Each line covers one interval rather than being cumulative, so the counts are
    SUMMED. Taking the newest line alone would report a busy window as whatever
    the last interval happened to contain.

    Absence is reported rather than zero: the line only appears once the connector
    has something to report, so an idle link legitimately has none, and "0
    records" would read as a broken connector.
    """
    total, intervals, newest = 0, 0, None
    for line in lines:
        found = DEBEZIUM_SENT_RE.search(line)
        if not found:
            continue
        total += int(found.group('records'))
        intervals += 1
        newest = (found, line)

    if not newest:
        return {'observed': False,
                'note': 'Debezium reports throughput only when it has sent '
                        'records, and has not in this window'}

    found, line = newest
    out = {
        'observed': True,
        'records_sent': total,
        'intervals': intervals,
        'records_sent_last_interval': int(found.group('records')),
        'source': 'summed from Debezium\u2019s own "records sent during '
                  'previous" lines',
    }

    # The interval the newest line covered. This matters for how the number may
    # be presented: on a quiet link the interval can be HOURS, so records/interval
    # is a long-run average and must never be shown as a current rate. For "right
    # now", the broker's produced_per_sec is the accurate signal.
    seconds = (int(found.group('h')) * 3600 + int(found.group('m')) * 60
               + float(found.group('s')))
    out['reporting_window'] = '{}:{}:{}'.format(
        found.group('h'), found.group('m'), found.group('s'))
    out['newest_interval_sec'] = round(seconds, 1)
    if seconds > 0:
        out['newest_avg_per_sec'] = round(
            int(found.group('records')) / seconds, 3)

    # The same line states the coordinate the connector had committed. Present
    # only for engines whose offset carries file/pos (MySQL); absent elsewhere,
    # which is why it is optional rather than assumed.
    offset = DEBEZIUM_OFFSET_RE.search(line)
    if offset:
        out['offset'] = '{}:{}'.format(offset.group('file'),
                                       format(int(offset.group('pos')), ','))
    return out


def parse_loader_progress(lines):
    """The bulk loader's newest progress line, and whether it finished.

    Completion is done == total rather than a keyword, because the loader prints
    no completion message -- it reports the last chunk and exits. Looking for a
    word like DONE would report an in-progress load as finished the moment the
    wording changed, or never report completion at all.
    """
    newest = None
    for line in lines:
        found = LOADER_PROGRESS_RE.search(line)
        if found:
            newest = found
    if not newest:
        return {'observed': False,
                'note': 'no chunk-progress line yet, so the load has not started '
                        'reporting'}
    done = int(newest.group('done'))
    total = int(newest.group('total'))
    return {
        'observed': True,
        'chunks_done': done,
        'chunks_total': total,
        'rows': int(newest.group('rows').replace(',', '')),
        'elapsed_sec': float(newest.group('secs')),
        'rows_per_sec': int(newest.group('rate').replace(',', '')),
        'finished': done >= total,
        'source': 'the loader\u2019s own chunk-progress line',
    }


def count_signals(component, lines):
    """Best-effort signal counts, each carrying a newest example."""
    out = []
    for name, description, needles in LOG_SIGNALS.get(component, []):
        matches, sample = 0, None
        for line in lines:
            if any(needle in line for needle in needles):
                matches += 1
                sample = line.strip()[:300]
        entry = {'signal': name, 'means': description, 'matches': matches}
        if sample:
            entry['newest'] = sample
        out.append(entry)
    return out


def log_activity(component, minutes=None):
    """Level counts, signals and (for the sink) real counters, from the log.

    Every figure is labelled with where it came from, because these are not the
    same class of measurement: level counts are exact, the sink's counters are its
    own, and the signals are substring matches against wording that can change
    between versions.
    """
    minutes = max(1, min(int(minutes or LOG_MINUTES_DEFAULT), LOG_MINUTES_MAX))
    # Unfiltered, because the levels and signals are counted here rather than by
    # CloudWatch: one pass over the window serves all of them.
    read = log_events(component, minutes=minutes, limit=LOG_LIMIT_MAX)
    out = {
        'component': component,
        'log_group': read.get('log_group'),
        'window_min': minutes,
        'state': read.get('state'),
    }
    for key in ('error', 'note'):
        if read.get(key):
            out[key] = read[key]
    if read.get('state') in ('absent', 'denied', 'throttled', 'error'):
        return out

    events = read.get('events') or []
    lines = [e.get('message') or '' for e in events]
    out['lines_scanned'] = len(lines)
    out['truncated'] = bool(read.get('next')) and len(lines) >= LOG_LIMIT_MAX

    levels = collections.Counter(e.get('severity') for e in events)
    out['levels'] = {'error': levels.get('error', 0),
                     'warn': levels.get('warn', 0),
                     'info': levels.get('info', 0)}
    newest_error = None
    for event in events:
        if event.get('severity') == 'error':
            newest_error = event
    if newest_error:
        out['newest_error'] = {
            'message': newest_error['message'][:400],
            'age_sec': newest_error.get('age_sec'),
            'task_id': newest_error.get('task_id'),
        }

    out['signals'] = count_signals(component, lines)
    if component == 'sink':
        out['counters'] = parse_sink_stats(lines)
        # Named explicitly so readers know these figures are not measured here.
        out['not_in_logs'] = {
            'retried': 'not logged by the sink; measured from the broker\u2019s '
                       'pending entries (see integrity.retrying)',
            'dead_lettered': 'not logged by the sink; measured as the length of '
                             'the DLQ stream (see integrity.dead_lettered)',
        }
    if component == 'debezium':
        out['counters'] = parse_debezium_records(lines)
        out['not_in_logs'] = {
            'events_processed': 'the count below is what the connector itself '
                                'reported over this window; the authoritative '
                                'live rate is the broker stream (see '
                                'broker.entries_added and produced_per_sec)',
        }
    if component == 'loader':
        # Progress and completion, from the loader's own chunk lines (see
        # parse_loader_progress for how completion is detected).
        out['counters'] = parse_loader_progress(lines)
    return out


@monitor_bp.route('/logs/activity', methods=['GET'])
@per_link
def logs_activity():
    """Counters and signals derived from one component's recent log lines."""
    component = request.args.get('component') or 'debezium'
    try:
        log_group_for(component)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    return jsonify(safe(log_activity, component,
                        minutes=request.args.get('minutes', type=int)))


@monitor_bp.route('/logs/components', methods=['GET'])
@per_link
def logs_components():
    """The components that have logs, and whether each is readable."""
    return jsonify(safe(log_components))


@monitor_bp.route('/logs/streams', methods=['GET'])
@per_link
def logs_streams():
    """One component's log streams — effectively its task history."""
    component = request.args.get('component') or 'debezium'
    try:
        return jsonify(safe(log_streams, component,
                            request.args.get('limit', type=int)))
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@monitor_bp.route('/logs', methods=['GET'])
@per_link
def logs():
    """One component's log lines, filtered and paginated."""
    component = request.args.get('component') or 'debezium'
    try:
        log_group_for(component)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    return jsonify(safe(
        log_events, component,
        minutes=request.args.get('minutes', type=int),
        filter_text=request.args.get('filter'),
        limit=request.args.get('limit', type=int),
        next_token=request.args.get('next'),
        stream=request.args.get('stream')))


# ---------------------------------------------------------------------------
# Debezium Server
# ---------------------------------------------------------------------------
def collect_debezium(source_binlog=None):
    out = {'service': CFG['debezium_service']}

    svc = soft(ecs_service, CFG['debezium_service'])
    out.update(svc)
    state, label = ecs_state(svc.get('desired'), svc.get('running'), svc.get('pending'))
    if 'error' in svc:
        state, label = 'bad', 'unknown'

    # Restarts: the task count alone does not show a Debezium task that restarts
    # repeatedly (see ecs_restarts). Surface ECS's own record
    # of recent restarts and, when it just happened, warn on it: one binlog
    # reader flapping is a real problem even when the task count looks fine.
    # Nested under its own key so it does not collide with the ECS count fields
    # and reads as one thing on the card.
    restarts = soft(ecs_restarts, CFG['debezium_service'])
    # The log-derived "Connector started" count is an independent cross-check on
    # ECS's stopped-task record: they should broadly agree, and a divergence is
    # itself worth seeing.
    restarts.update(soft(log_restart_count, 'debezium'))
    out['restarts_2h'] = restarts
    if restarts.get('restarts') and state == 'ok':
        last = restarts.get('last_restart_age_sec')
        if last is not None and last < RESTART_WARN_SEC:
            state, label = 'warn', 'restarted'

    out['state'], out['label'] = state, label

    # Committed offset lives in the broker (Option B: no SQLite/EFS).
    def read_offset(broker):
        committed = parse_offset_hash(broker.hgetall(CFG['offset_key']))
        if not committed:
            return {'offset_note': 'no offset at {} yet'.format(CFG['offset_key'])}
        info = {
            'offset_key': CFG['offset_key'],
            'offset_binlog_file': committed['file'],
            'offset_binlog_pos': committed['pos'],
        }
        if committed['file'] and committed['pos'] is not None:
            info['offset_binlog'] = '{}:{}'.format(
                committed['file'], format(committed['pos'], ','))
        if committed.get('ts_sec'):
            info['offset_age_sec'] = round(max(0.0, time.time() - float(committed['ts_sec'])), 1)
        info.update(binlog_gap(source_binlog, committed))
        return info

    out.update(soft(with_broker, read_offset))

    # Capture latency from the newest event still resident in any stream.
    #
    # Across tables the WORST is the accurate main figure: one Debezium task reads one
    # binlog for all of them, so if any table's newest captured event is 8s old
    # that is the capture latency of the link, and taking the best or a mean
    # would hide it. Per-table figures are reported alongside.
    def read_capture(broker):
        per_table, best = [], {}
        for spec in stream_specs():
            try:
                newest = broker.xrevrange(spec['stream'], '+', '-', count=1)
            except Exception as exc:                      # noqa: BLE001
                per_table.append({'table': spec['table'], 'stream': spec['stream'],
                                  'error': '{}: {}'.format(type(exc).__name__, exc)})
                continue
            if not newest:
                per_table.append({'table': spec['table'], 'stream': spec['stream'],
                                  'capture_lag_ms': None, 'drained': True})
                continue
            entry_id, fields = newest[0]
            measured = capture_lag_from_entry(entry_id, fields, now=now_ms())
            # Reject a stale leftover: the reading is only "current" if the event
            # arrived within capture_fresh_sec. A never-XDEL'd leftover from an
            # old burst arrived hours ago and carries a capture lag that no longer changes; using
            # it makes the pipeline look permanently 100s+ behind and skews the
            # in-flight CDC lag. When it is stale, the pipeline is effectively idle
            # (no new events), so it is treated like a drained stream.
            age = measured.get('last_event_age_ms')
            stale = age is not None and age > CFG['capture_fresh_sec'] * 1000
            per_table.append(dict(measured, table=spec['table'],
                                  stream=spec['stream'], drained=False, stale=stale,
                                  dead_letter_replay=bool((fields or {}).get('ecdl_v'))))
            value = measured.get('capture_lag_ms')
            if value is not None and not stale and value >= (best.get('capture_lag_ms') or -1):
                best = measured

        out = {'capture_by_table': per_table,
               'catching_up': catching_up_tables(per_table)}
        if best.get('capture_lag_ms') is not None:
            remember_capture(best['capture_lag_ms'])
            out.update(best)
            # Skew on ANY fresh table, not just the one chosen as the worst:
            # one binlog reader, one source clock.
            skews = [r['clock_skew_ms'] for r in per_table
                     if r.get('clock_skew_ms') and not r.get('stale')]
            if skews:
                out['clock_skew_ms'] = max(skews)
                out['capture_note'] = clock_skew_note(out['clock_skew_ms'])
            return out
        # Nothing fresh to time: either every stream drained (sink caught up) or
        # the only resident entries are stale leftovers. Either way there is no
        # current capture measurement, so report None (which keeps it out of the
        # in-flight lag) and show the last real measurement with its age.
        out.update({'capture_lag_ms': None,
                    'capture_note': 'no fresh event to time (sink caught up or '
                                    'only stale leftovers resident); showing the '
                                    'last measurement'})
        out.update(last_capture())
        return out

    out.update(soft(with_broker, read_capture))

    if CFG['debezium_metrics_url']:
        out['metrics_endpoint'] = soft(http_json, CFG['debezium_metrics_url'])
    out.update(soft(log_error_count, CFG['debezium_log_group']))
    # (restarts_2h is set earlier, merging the ECS stopped-task record with the
    # log-derived "Connector started" cross-check; do not re-assign it here.)
    if state == 'paused':
        # The growing binlog gap and ageing offset are expected while CDC is
        # stopped, so they are labelled rather than left looking like a fault.
        # Kept short: the banner already says ECDL is off.
        out['paused_note'] = 'ECDL off \u00b7 gap is the backlog waiting to replay'
    out['ok'] = state != 'bad'
    return out


@monitor_bp.route('/debezium', methods=['GET'])
@per_link
def debezium():
    """Standalone card. Fetches the source binlog coordinate itself so the
    "behind source" figure still works outside the aggregate endpoint."""
    source = soft(lambda: _source_binlog_only())
    return jsonify(safe(collect_debezium, source if 'error' not in source else None))


def _source_binlog_only():
    conn = mysql_conn()
    try:
        return binlog_status(conn.cursor())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ECS sink workers
# ---------------------------------------------------------------------------
def group_info(broker, stream=None):
    """The sink's consumer group on one stream: consumers, backlog, progress.

    One sink service consumes every stream under ONE group name, so the group is
    global but its lag and pending counts are per stream -- a link can be caught
    up on one table and hours behind on another.
    """
    stream = stream or CFG['stream_key']
    for group in broker.xinfo_groups(stream):
        if group.get('name') != CFG['consumer_group']:
            continue
        return {
            'group': group.get('name'),
            'consumers': group.get('consumers'),
            'pending': group.get('pending'),
            'group_lag': group.get('lag'),
            'entries_read': group.get('entries-read'),
            'last_delivered_id': group.get('last-delivered-id'),
        }
    return {'group': CFG['consumer_group'],
            'group_note': 'consumer group not attached to {}'.format(stream)}


def oldest_pending(broker, stream=None):
    """Age of the oldest entry delivered but not yet acked — the sink-side wait."""
    stream = stream or CFG['stream_key']
    summary = broker.xpending(stream, CFG['consumer_group'])
    if isinstance(summary, dict):
        count, min_id = summary.get('pending'), summary.get('min')
    elif isinstance(summary, (list, tuple)) and len(summary) >= 2:
        count, min_id = summary[0], summary[1]
    else:
        return {}
    out = {'pending': count}
    started = stream_id_ms(min_id)
    if count and started:
        out['oldest_pending_id'] = min_id
        out['oldest_pending_age_ms'] = max(0, now_ms() - started)
    return out


def collect_sink():
    out = {'service': CFG['sink_service']}

    svc = soft(ecs_service, CFG['sink_service'])
    out.update(svc)
    state, label = ecs_state(svc.get('desired'), svc.get('running'), svc.get('pending'))
    if 'error' in svc:
        state, label = 'bad', 'unknown'

    # Sink restarts are less alarming than Debezium's — a scale-in stops a task
    # by design — so they are surfaced but do not change the card to warn on their
    # own. A task that died on an error still shows its reason and exit code.
    sink_restarts = soft(ecs_restarts, CFG['sink_service'])
    sink_restarts.update(soft(log_restart_count, 'sink'))
    out['restarts_2h'] = sink_restarts

    specs = stream_specs()

    def read_group(broker):
        # One task drains every stream, so the ECS half above is shared and only
        # the consumer-group half is per table.
        rows = []
        for spec in specs:
            row = {'stream': spec['stream'], 'table': spec['table']}
            try:
                row.update(group_info(broker, spec['stream']))
                row.update(oldest_pending(broker, spec['stream']))
            except Exception as exc:                      # noqa: BLE001
                row.update({'ok': False,
                            'error': '{}: {}'.format(type(exc).__name__, exc)})
                rows.append(row)
                continue
            row['backlog'] = backlog_of(row)
            row['applied_per_sec'] = rate('sink.entries_read:' + spec['stream'],
                                          row.get('entries_read'))
            row['ok'] = True
            rows.append(row)
        return {'tables': rows}

    group = soft(with_broker, read_group)
    rows = group.get('tables') or []
    healthy = [r for r in rows if r.get('ok')]
    out['tables'] = rows
    out['table_count'] = len(rows)

    # Roll-up, so every field the page already reads keeps working.
    out['group'] = healthy[0].get('group') if healthy else CFG['consumer_group']
    out['consumers'] = _max_present(healthy, 'consumers')
    out['pending'] = _sum_present(healthy, 'pending')
    out['group_lag'] = _sum_present(healthy, 'group_lag')
    out['entries_read'] = _sum_present(healthy, 'entries_read')
    out['applied_per_sec'] = _sum_present(healthy, 'applied_per_sec')
    out['oldest_pending_age_ms'] = _max_present(healthy, 'oldest_pending_age_ms')
    if group.get('error'):
        out['error'] = group['error']
    notes = [r['group_note'] for r in healthy if r.get('group_note')]
    if notes:
        out['group_note'] = '; '.join(notes)

    # No consumers attached means nothing is draining, whatever ECS reports.
    if out.get('consumers') == 0 and state == 'ok':
        state, label = 'warn', 'idle'

    # An entry stuck past the reclaim window: tasks may be "running" and the
    # backlog tiny, yet nothing is progressing. Size alone would miss this.
    # Named per table, because "something is stuck" is not actionable when the
    # link has six of them.
    stuck_ms = out.get('oldest_pending_age_ms')
    if stuck_ms is not None and stuck_ms > STUCK_WARN_MS and state == 'ok':
        state, label = 'warn', 'stuck'
        worst = [r for r in healthy
                 if (r.get('oldest_pending_age_ms') or 0) > STUCK_WARN_MS]
        out['stuck_note'] = (
            'oldest entry unacked for {} on {} — past the sink reclaim window, so '
            'it is not being retried'.format(
                human_age(stuck_ms / 1000.0),
                ', '.join(r.get('table') or r.get('stream') for r in worst)
                or 'the stream'))

    out['state'], out['label'] = state, label
    out['ok'] = state != 'bad'
    if CFG['sink_metrics_url']:
        out['metrics_endpoint'] = soft(http_json, CFG['sink_metrics_url'])
    out.update(soft(log_error_count, CFG['sink_log_group']))
    # (restarts_2h is set earlier from the ECS record + log cross-check.)
    return out


@monitor_bp.route('/sink', methods=['GET'])
@per_link
def sink():
    return jsonify(safe(collect_sink))


# ---------------------------------------------------------------------------
# INFO helpers (a cluster client answers per-node dicts, a single node one dict)
# ---------------------------------------------------------------------------
# Backlog above this many entries changes the broker card to "backlog". The demo
# pipeline drains ~18.5K events/s, so a few thousand in flight is normal.
BACKLOG_WARN = _env_int('DEMO_BACKLOG_WARN', 10000)

# A SMALL backlog that is not moving is worse than a large one that is. When a
# sink task is replaced, its in-flight entries are left in the dead
# consumer's pending list until another task reclaims them; the Go sink retries
# entries idle longer than RETRY_IDLE_MS (30s) on a RECLAIM_INTERVAL_MS (15s)
# tick, so up to ~45-60s of waiting is normal recovery. Past that, reclaim is not
# working and someone needs to know — observed live: one entry sat stuck for
# ~100s while every hop still rendered green.
STUCK_WARN_MS = _env_int('DEMO_STUCK_WARN_MS', 60000)

PRIMARIES = 'primaries'


def info_nodes(raw):
    """Normalise an INFO reply to a list of per-node dicts."""
    if not raw:
        return []
    if isinstance(raw, dict) and raw and all(isinstance(v, dict) for v in raw.values()):
        return list(raw.values())
    return [raw] if isinstance(raw, dict) else []


def info_sum(nodes, field):
    total, found = 0, False
    for node in nodes:
        value = node.get(field)
        if isinstance(value, (int, float)):
            total += value
            found = True
        elif isinstance(value, str) and value.isdigit():
            total += int(value)
            found = True
    return total if found else None


def info_first(nodes, field):
    for node in nodes:
        if node.get(field) is not None:
            return node[field]
    return None


def human_bytes(value):
    if not isinstance(value, (int, float)) or value < 0:
        return None
    for unit in ('B', 'K', 'M', 'G', 'T'):
        if value < 1024 or unit == 'T':
            return '{:.2f} {}'.format(value, unit) if unit != 'B' else '{:.0f} B'.format(value)
        value /= 1024.0
    return None


# Commands that count as reads and writes on the target. INFO commandstats keys
# them as cmdstat_<name>. Deliberately excludes housekeeping commands this
# collector itself issues (dbsize, ping, info) so they cannot skew the figure.
READ_CMDS = ('hgetall', 'hget', 'hmget')
WRITE_CMDS = ('hset', 'hdel', 'del')


def delta_latency(name, calls, usec):
    """Per-call latency for the interval between polls, in microseconds.

    INFO commandstats reports a LIFETIME usec_per_call — on a server that has
    served 124 million HGETALLs that average is effectively fixed and says nothing
    about latency right now. Diffing successive samples gives the live figure.
    """
    if calls is None or usec is None:
        return None
    key = scoped('lat:' + name)
    with _lock:
        prev = _samples.get(key)
        _samples[key] = (calls, usec)
    if prev is None:
        return None
    prev_calls, prev_usec = prev
    d_calls, d_usec = calls - prev_calls, usec - prev_usec
    if d_calls <= 0 or d_usec < 0:          # idle interval, or counters reset
        return None
    return round(d_usec / d_calls, 2)


def command_latency(info, commands, label):
    """Live server-side latency for a group of commands, from commandstats.

    Returns the call-weighted latency for the group plus a per-command
    breakdown, both in microseconds.
    """
    total_calls = total_usec = 0
    per_command = {}
    for cmd in commands:
        entry = info.get('cmdstat_' + cmd)
        if not isinstance(entry, dict):
            continue
        calls = int(entry.get('calls') or 0)
        usec = int(entry.get('usec') or 0)
        total_calls += calls
        total_usec += usec
        live = delta_latency('{}:{}'.format(label, cmd), calls, usec)
        if live is not None:
            per_command[cmd] = live
    group = delta_latency(label + ':__all__', total_calls, total_usec)
    return {'us': group, 'per_command': per_command, 'calls_total': total_calls or None}


def hit_rate_pct(hits, misses):
    if hits is None or misses is None:
        return None
    total = hits + misses
    return None if total <= 0 else round(hits * 100.0 / total, 2)


def cluster_call(client, method, *args, **kwargs):
    """Call a cluster method against all primaries, tolerating a single-node client."""
    fn = getattr(client, method)
    try:
        return fn(*args, target_nodes=PRIMARIES, **kwargs)
    except TypeError:
        return fn(*args, **kwargs)


def node_roles(client):
    """Node names and roles, using the client's own inclusive accessors."""
    nodes = []
    try:
        for node in client.get_primaries():
            nodes.append({'name': node.name, 'role': 'primary'})
        for node in client.get_replicas():
            nodes.append({'name': node.name, 'role': 'replica'})
    except Exception:                                     # noqa: BLE001
        return []
    return nodes


# ---------------------------------------------------------------------------
# Broker stream
# ---------------------------------------------------------------------------
def read_one_stream(broker, spec):
    """Everything stream-scoped for one table. Never raises for one bad stream.

    A missing stream is reported against that table rather than failing the whole
    card: with several tables, one that has not been created yet must not hide
    the health of the others.
    """
    stream, table = spec['stream'], spec['table']
    out = {'stream': stream, 'table': table,
           'key_prefix': key_prefix_for(table)}
    try:
        info = broker.xinfo_stream(stream)
    except Exception as exc:                              # noqa: BLE001
        return dict(out, ok=False,
                    error='{}: {}'.format(type(exc).__name__, exc),
                    stream_note='stream does not exist yet, or is unreadable')
    entries_added = info.get('entries-added')
    out.update({
        'length': info.get('length'),
        'entries_added': entries_added,
        # Rate keys MUST be per stream. They are flat strings in one shared dict,
        # so a single 'broker.entries_added' would have every table overwriting
        # the others' baseline and all of them reporting incorrect values.
        'produced_per_sec': rate('broker.entries_added:' + stream, entries_added),
        'last_generated_id': info.get('last-generated-id'),
    })
    newest = stream_id_ms(info.get('last-generated-id'))
    if newest:
        out['last_event_age_ms'] = max(0, now_ms() - newest)

    group = group_info(broker, stream)
    out.update(group)
    out.update(oldest_pending(broker, stream))
    # Resident length is the robust backlog signal under load (see
    # outstanding_backlog). Keep the raw resident count too, as the leftover/leak
    # signal, so it is never reported as lag.
    out['resident_length'] = out.get('length')
    out['backlog'] = outstanding_backlog(
        out.get('length'), out.get('last_delivered_id'),
        out.get('last_generated_id'), out.get('pending'), out)
    out['applied_per_sec'] = rate('sink.entries_read:' + stream,
                                  out.get('entries_read'))
    # "Oldest entry" = the oldest entry still WAITING, not the oldest entry
    # physically resident. The sink acks then XDELs, but leaves a few entries
    # un-deleted (observed: 155, oldest ~7h), and first-entry pointed at one of
    # those -- so the card read "oldest entry 7h" next to "backlog 0", and ~8.8h
    # even under a real backlog. The leftovers all have ids <= last-delivered-id
    # (they were delivered and acked), so the oldest entry AFTER last-delivered is
    # the oldest genuinely-undelivered entry, orphan-free. If nothing is
    # undelivered but entries are unacked, the oldest wait is the pending one.
    # Caught up -> nothing waiting -> None.
    out['oldest_entry_age_ms'] = None
    if out['backlog'] > 0:
        oldest_ms = None
        ld = out.get('last_delivered_id')
        if ld:
            try:
                after = broker.xrange(stream, min='(' + str(ld), max='+', count=1)
            except Exception:                             # noqa: BLE001
                after = []
            if after:
                ts = stream_id_ms(after[0][0])
                if ts:
                    oldest_ms = max(0, now_ms() - ts)
        if oldest_ms is None:
            oldest_ms = out.get('oldest_pending_age_ms')
        out['oldest_entry_age_ms'] = oldest_ms
    out['ok'] = True
    if out.get('group_note'):
        out['state'], out['label'] = 'warn', 'no consumer group'
    elif out['backlog'] > BACKLOG_WARN:
        out['state'], out['label'] = 'warn', 'backlog'
    else:
        out['state'], out['label'] = 'ok', 'up'
    return out


def _sum_present(rows, field):
    """Sum a field across tables, or None when no table reported it.

    None and 0 are different answers: a rate that has not been sampled yet is
    unknown, and summing it as zero would make the banner claim the pipeline is
    idle on its first poll.
    """
    values = [r.get(field) for r in rows if r.get(field) is not None]
    return round(sum(values), 1) if values else None


def _max_present(rows, field):
    values = [r.get(field) for r in rows if r.get(field) is not None]
    return max(values) if values else None


def collect_broker():
    specs = stream_specs()

    def read(broker):
        out = {'endpoint': '{}:{}'.format(CFG['broker_host'], CFG['broker_port'])}
        out['streams'] = [read_one_stream(broker, spec) for spec in specs]
        info = info_nodes(broker.info())
        out.update({
            'used_memory': human_bytes(info_sum(info, 'used_memory')),
            'used_memory_bytes': info_sum(info, 'used_memory'),
            'ops_per_sec': info_sum(info, 'instantaneous_ops_per_sec'),
            'connected_clients': info_sum(info, 'connected_clients'),
        })
        return out

    out = with_broker(read)
    rows = out.get('streams') or []
    healthy = [r for r in rows if r.get('ok')]

    # The roll-up. Every one of these keys is read by the page, so they stay at
    # the top level and mean the same thing they always did -- for a single-table
    # link the numbers are identical, and for several they are the link total.
    out['stream'] = rows[0]['stream'] if rows else CFG['stream_key']
    out['tables'] = [r.get('table') for r in rows]
    out['table_count'] = len(rows)
    out['length'] = _sum_present(healthy, 'length')
    out['entries_added'] = _sum_present(healthy, 'entries_added')
    out['produced_per_sec'] = _sum_present(healthy, 'produced_per_sec')
    out['pending'] = _sum_present(healthy, 'pending')
    out['group_lag'] = _sum_present(healthy, 'group_lag')
    out['entries_read'] = _sum_present(healthy, 'entries_read')
    out['backlog'] = int(sum(r.get('backlog') or 0 for r in healthy))
    out['group'] = healthy[0].get('group') if healthy else CFG['consumer_group']
    out['consumers'] = _max_present(healthy, 'consumers')
    # Worst case, not an average: the oldest waiting entry anywhere is what the
    # lag figure has to describe, and averaging would hide one stalled table
    # behind several healthy ones.
    out['oldest_entry_age_ms'] = _max_present(healthy, 'oldest_entry_age_ms')
    out['oldest_pending_age_ms'] = _max_present(healthy, 'oldest_pending_age_ms')
    out['last_event_age_ms'] = _min_present(healthy, 'last_event_age_ms')

    missing = [r for r in rows if not r.get('ok')]
    no_group = [r for r in healthy if r.get('group_note')]
    if missing:
        out['state'], out['label'] = 'warn', 'stream missing'
        out['group_note'] = '{} of {} streams unreadable: {}'.format(
            len(missing), len(rows), ', '.join(r['stream'] for r in missing))
    elif no_group:
        out['state'], out['label'] = 'warn', 'no consumer group'
        out['group_note'] = 'consumer group not attached to {}'.format(
            ', '.join(r['stream'] for r in no_group))
    elif out['backlog'] > BACKLOG_WARN:
        out['state'], out['label'] = 'warn', 'backlog'
    else:
        out['state'], out['label'] = 'ok', 'up'
    return out


def _min_present(rows, field):
    """Smallest value across tables. Used for "time since the last event
    anywhere", where the most recent event is the freshest number."""
    values = [r.get(field) for r in rows if r.get(field) is not None]
    return min(values) if values else None


@monitor_bp.route('/broker', methods=['GET'])
@per_link
def broker():
    return jsonify(safe(collect_broker))


# ---------------------------------------------------------------------------
# Target Valkey
# ---------------------------------------------------------------------------
def pct_from_buckets(bounds_us, counts, pct):
    """Percentile out of raw histogram bucket counts.

    Same bucket walk the app does, but applied to a *difference* of two
    snapshots so the answer covers only the interval between them.
    """
    total = sum(counts)
    if not total or not bounds_us:
        return None
    target = pct / 100.0 * total
    cum = 0
    for i, count in enumerate(counts):
        cum += count
        if cum >= target:
            return round(bounds_us[min(i, len(bounds_us) - 1)] / 1000.0, 3)
    return round(bounds_us[-1] / 1000.0, 3)


def demo_app_metrics():
    """Cached wrapper: the bucket diff must happen once per window, not once per
    caller. The dashboard polls every 2s and the history sampler every 5s; if
    both diffed, each would subtract the other's snapshot and both would be
    wrong. One shared result per interval keeps the window accurate.
    """
    return cached('appmetrics', max(2, CFG['history_interval_sec']),
                  _demo_app_metrics)


def _demo_app_metrics():
    """Aurora-read vs Valkey-read latency percentiles, measured in the app.

    This is the fair comparison: the same process, the same
    request shape, timed either side of one backend call. The app's own
    percentiles are cumulative since it started, which hides whatever is
    happening now, so the buckets are diffed between polls to get a rolling
    window instead. Cumulative values are kept alongside, labelled.
    """
    data = http_json(CFG['demo_app_url'].rstrip('/') + '/metrics?buckets=1')
    out = {}
    sections = {'db': 'aurora_read', 'cache': 'valkey_read', 'write': 'aurora_write'}
    windowed = 0
    for section, label in sections.items():
        stats = data.get(section) or {}
        for pct in (50, 90, 99):
            value = stats.get('p{}_ms'.format(pct))
            if value is not None:
                out['{}_p{}_cumulative'.format(label, pct)] = value
        if stats.get('count') is not None:
            out['{}_count'.format(label)] = stats['count']

        bounds = stats.get('bounds_us')
        counts = stats.get('buckets')
        if not bounds or not counts:
            continue
        prev = _shared.get('appbuckets:' + section)
        _shared['appbuckets:' + section] = counts
        if not prev or len(prev) != len(counts):
            continue
        delta = [max(0, now - was) for now, was in zip(counts, prev)]
        n = sum(delta)
        out['{}_samples'.format(label)] = n
        if n:
            windowed += 1
            for pct in (50, 90, 99):
                out['{}_p{}'.format(label, pct)] = pct_from_buckets(bounds, delta, pct)
    for counter in ('cache_hits', 'cache_misses', 'requests', 'errors'):
        if data.get(counter) is not None:
            out[counter] = data[counter]
    out['note'] = ('p50/p90/p99 are for the interval since the previous poll; '
                   '*_cumulative are since the app started'
                   if windowed else
                   'no traffic since the previous poll, so there is no window to '
                   'report; *_cumulative are since the app started')
    return out


def collect_target():
    def read(target):
        info = info_nodes(cluster_call(target, 'info'))
        # Server-side per-command latency. commandstats is a flat cmdstat_<cmd>
        # dict, NOT keyed by node, so it is read separately from the main INFO.
        stats = soft(lambda: cluster_call(target, 'info', 'commandstats')) or {}
        read_lat = command_latency(stats, READ_CMDS, 'target.read')
        write_lat = command_latency(stats, WRITE_CMDS, 'target.write')
        hits, misses = info_sum(info, 'keyspace_hits'), info_sum(info, 'keyspace_misses')
        used, maxmem = info_sum(info, 'used_memory'), info_sum(info, 'maxmemory')
        out = {
            'endpoint': '{}:{}'.format(CFG['cache_host'], CFG['cache_port']),
            'tls': CFG['cache_tls'],
            'keys': _sum_values(cluster_call(target, 'dbsize')),
            'ops_per_sec': info_sum(info, 'instantaneous_ops_per_sec'),
            'connected_clients': info_sum(info, 'connected_clients'),
            'hit_rate_pct': hit_rate_pct(hits, misses),
            'keyspace_hits': hits,
            'keyspace_misses': misses,
            'used_memory': human_bytes(used),
            'maxmemory': human_bytes(maxmem),
            'used_pct': round(used * 100.0 / maxmem, 1) if used and maxmem else None,
            'version': info_first(info, 'redis_version') or info_first(info, 'valkey_version'),
            'nodes': node_roles(target),
            # Live server-side latency, measured between polls (microseconds).
            'read_latency_us': read_lat['us'],
            'write_latency_us': write_lat['us'],
            'read_latency_by_cmd': read_lat['per_command'],
            'write_latency_by_cmd': write_lat['per_command'],
            'latency_note': 'server-side execution time per command, diffed '
                            'between polls (excludes network round trip)',
            # Raw call counters, turned into rates below. Splitting ops/s into
            # reads and writes matters here: reads are the application being
            # served, writes are the sink applying CDC. One number hides which.
            '_read_calls': read_lat['calls_total'],
            '_write_calls': write_lat['calls_total'],
            # Cumulative connections received, turned into a new-connections/s
            # rate below (charted next to the live connected-clients count).
            '_total_conns': info_sum(info, 'total_connections_received'),
        }
        out['shards'] = len([n for n in out['nodes'] if n['role'] == 'primary']) or None
        return out

    out = with_target(read)
    # Key-count growth is what makes new rows arriving visible (the signal
    # act2c_cdc_monitor.sh watches). Updates to existing keys do not move it.
    out['keys_added_per_sec'] = rate('target.keys', out.get('keys'))
    # Reads = the application. Writes = the sink applying CDC. During a
    # "DB only" phase reads here fall to zero while writes keep flowing,
    # because writes always go to Aurora and CDC still carries them across.
    out['reads_per_sec'] = rate('target.readcalls', out.pop('_read_calls', None))
    out['writes_per_sec'] = rate('target.writecalls', out.pop('_write_calls', None))
    out['new_conn_per_sec'] = rate('target.newconn', out.pop('_total_conns', None))

    cache_dim = {'CacheClusterId': CFG['cache_cluster_id']}
    out['cloudwatch'] = soft(lambda: {
        'engine_cpu_pct': cw_latest('AWS/ElastiCache', 'EngineCPUUtilization', cache_dim),
        # See the Aurora note: peaks need the Maximum statistic.
        'engine_cpu_max_pct': cw_latest('AWS/ElastiCache', 'EngineCPUUtilization',
                                        cache_dim, stat='Maximum'),
        'cpu_pct': cw_latest('AWS/ElastiCache', 'CPUUtilization', cache_dim),
        # End-to-end request latency as ElastiCache sees it, microseconds. Higher
        # than commandstats because it includes the server's request handling,
        # not just command execution.
        'read_latency_us': cw_latest('AWS/ElastiCache',
                                     'SuccessfulReadRequestLatency', cache_dim),
        'write_latency_us': cw_latest('AWS/ElastiCache',
                                      'SuccessfulWriteRequestLatency', cache_dim),
        # Per-command-family latency as ElastiCache reports it, microseconds.
        'get_cmds_latency_us': cw_latest('AWS/ElastiCache', 'GetTypeCmdsLatency', cache_dim),
        'set_cmds_latency_us': cw_latest('AWS/ElastiCache', 'SetTypeCmdsLatency', cache_dim),
        'hash_cmds_latency_us': cw_latest('AWS/ElastiCache', 'HashBasedCmdsLatency', cache_dim),
        'cluster_id': CFG['cache_cluster_id'],
        'age_sec': cw_age('AWS/ElastiCache', 'EngineCPUUtilization', 'CPUUtilization',
                          'SuccessfulReadRequestLatency', dimensions=cache_dim),
        'note': CW_NOTE,
    })
    if CFG['demo_app_url']:
        out['demo_app'] = soft(demo_app_metrics)
    out['state'], out['label'] = 'ok', 'up'
    return out


@monitor_bp.route('/target', methods=['GET'])
@per_link
def target():
    return jsonify(safe(collect_target))


# ---------------------------------------------------------------------------
# Delivery integrity — "did we lose anything, and is anything being retried?"
# ---------------------------------------------------------------------------
# There are exactly three ways an event can fail to reach the cache, and each is
# reported explicitly rather than inferred:
#
#   1. Dead-lettered  - the sink retried MAX_DELIVERIES times and gave up, moving
#                       the entry to the DLQ stream. Real, permanent loss from the
#                       cache's point of view until someone replays it.
#   2. Evicted        - the broker hit maxmemory and dropped data. NOTE on this
#                       deployment: policy is volatile-lru and stream entries have
#                       no TTL, so there is nothing evictable — a full broker
#                       returns OOM to Debezium instead of silently dropping.
#                       That is the safe failure mode, but it stalls ingestion,
#                       so headroom is reported too.
#   3. Stuck          - delivered but never acked and past the reclaim window.
#                       Recoverable, but not progressing on its own.
#
# Retries in progress are surfaced separately: an entry with times_delivered > 1
# is being retried right now, and how close it is to MAX_DELIVERIES shows how many
# attempts remain before it is dead-lettered.
def integrity_verdict(dlq_len, evicted, retrying, stuck_ms, mem_pct,
                      backlog=0, applied_per_sec=None, still_sec=None,
                      dlq_recent=True):
    """Combine the signals into one verdict. Loss takes priority over everything else.

    `stalled` matters as much as `stuck`: entries sitting UNDELIVERED because
    nothing is consuming are not retrying and not dead-lettered, so every other
    signal shows no problem while the backlog grows toward a full broker.
    `backlog` here is the undelivered count, and `still_sec` how long it has gone
    without shrinking; stalled needs that to reach stall_window_sec (None skips
    the window, for a caller with a single reading).

    Dead letters are 'loss' only while `dlq_recent`; older ones are
    'dead_letters', still reported, but not as loss happening now.
    """
    if ((dlq_len or 0) > 0 and dlq_recent) or (evicted or 0) > 0:
        return 'loss'
    if ((backlog or 0) > 0 and applied_per_sec is not None and applied_per_sec <= 0
            and (still_sec is None or still_sec >= CFG['stall_window_sec'])):
        return 'stalled'
    if (dlq_len or 0) > 0:
        return 'dead_letters'
    if (stuck_ms or 0) > STUCK_WARN_MS:
        return 'stuck'
    if mem_pct is not None and mem_pct >= CFG['broker_mem_warn_pct']:
        return 'pressure'
    if (retrying or 0) > 0:
        return 'retrying'
    return 'ok'


INTEGRITY_HEADLINE = {
    'loss': 'ENTRIES LOST — dead-lettered or evicted, see below',
    'stalled': 'BACKLOG NOT DRAINING — nothing is consuming; entries are queued, '
               'not lost, but the broker will fill',
    'stuck': 'entries stuck past the retry window — not progressing on their own',
    'pressure': 'broker memory pressure — ingestion stalls if it fills',
    'retrying': 'retries in progress — recovering automatically, nothing lost yet',
    'dead_letters': 'earlier dead letters, none new recently — review and replay '
                    'them, see below',
    'ok': 'no loss: nothing dead-lettered, nothing evicted, nothing stuck',
    'unknown': 'integrity unknown — a stream could not be read, so loss cannot '
               'be ruled out',
}


def integrity_for_stream(broker, spec):
    """The delivery-integrity signals for ONE table's stream.

    Everything here is per stream: its DLQ, its backlog, its retries, its
    consumers. Eviction and broker memory are instance-wide and are added once by
    the caller, because a broker that evicted keys cannot say which table's
    entries went.
    """
    stream, table = spec['stream'], spec['table']
    group = CFG['consumer_group']
    dlq_key = CFG['dlq_prefix'] + stream
    out = {'stream': stream, 'table': table, 'dlq_key': dlq_key}

    try:
        st = broker.xinfo_stream(stream)
    except Exception as exc:                              # noqa: BLE001
        return dict(out, ok=False,
                    error='{}: {}'.format(type(exc).__name__, exc))

    entries_added = int(st.get('entries-added') or 0)
    length = int(st.get('length') or 0)
    grp = [g for g in broker.xinfo_groups(stream) if g.get('name') == group]
    g0 = grp[0] if grp else {}
    unacked = int((g0.get('pending')) or 0)
    # Same robust backlog the broker card uses: resident length leads, because
    # under load both XINFO 'lag' and the group's entries-read counter go NULL and
    # the counter form drops to "unacked only" (~10K while the real queue was
    # ~1M). Integrity's stalled/pressure verdict depends on this being real.
    backlog = outstanding_backlog(
        length, g0.get('last-delivered-id'), st.get('last-generated-id'), unacked,
        {'entries_added': entries_added, 'entries_read': g0.get('entries-read'),
         'lag': g0.get('lag')})
    # Delivered up to the newest entry means nothing is undelivered, whatever the
    # resident length says (it includes the few entries the sink did not XDEL).
    last_delivered = g0.get('last-delivered-id')
    if last_delivered is not None and last_delivered == st.get('last-generated-id'):
        undelivered = 0
    else:
        undelivered = max(0, backlog - unacked)
    still = undelivered_still_sec(stream, undelivered)
    # Entries that have LEFT the stream: the sink acks then XDELs, so this is a
    # reliable drain signal (the group's entries-read counter is not).
    applied = rate('integrity.drained:' + stream, entries_added - length)

    dlq_len = broker.xlen(dlq_key) if broker.exists(dlq_key) else 0
    dlq_sample = []
    if dlq_len:
        for entry_id, fields in broker.xrevrange(dlq_key, '+', '-', count=5):
            dlq_sample.append({
                'id': entry_id,
                'reason': fields.get('reason') or fields.get('dlq_reason'),
                'deliveries': fields.get('deliveries') or fields.get('retry_count'),
                'age_ms': (now_ms() - stream_id_ms(entry_id)
                           if stream_id_ms(entry_id) else None),
            })

    newest_dlq_ms = dlq_sample[0]['age_ms'] if dlq_sample else None
    dlq_recent = dlq_is_recent(stream, dlq_len, newest_dlq_ms)

    retrying, max_seen, oldest_idle = 0, 0, 0
    samples = []
    try:
        pending = broker.xpending_range(stream, group, min='-', max='+', count=100)
    except Exception:                                     # noqa: BLE001
        pending = []
    for p in pending:
        delivered = int(p.get('times_delivered') or 1)
        idle = int(p.get('time_since_delivered') or 0)
        max_seen = max(max_seen, delivered)
        oldest_idle = max(oldest_idle, idle)
        if delivered > 1:
            retrying += 1
            if len(samples) < 5:
                samples.append({'id': p.get('message_id'),
                                'consumer': p.get('consumer'),
                                'attempt': delivered,
                                'of_max': CFG['max_deliveries'],
                                'idle_ms': idle})

    try:
        consumers = broker.xinfo_consumers(stream, group)
    except Exception:                                     # noqa: BLE001
        consumers = []
    active = [c for c in consumers
              if int(c.get('idle') or 0) < CFG['consumer_active_idle_ms']]

    out.update({
        'ok': True,
        'backlog': backlog,
        'undelivered': undelivered,
        'undelivered_still_sec': round(still, 1),
        'unacked': unacked,
        'stream_length': length,
        'drained_per_sec': applied,
        'dead_lettered': dlq_len,
        'dlq_newest_age_ms': newest_dlq_ms,
        'dlq_recent': dlq_recent,
        'dlq_sample': dlq_sample,
        'retrying': retrying,
        'max_attempt_seen': max_seen,
        'max_deliveries': CFG['max_deliveries'],
        'retry_sample': samples,
        'oldest_unacked_ms': oldest_idle or None,
        'consumers_registered': len(consumers),
        'consumers_active': len(active),
    })
    return out


_undelivered_seen = {}  # scoped stream -> (lowest undelivered since, monotonic since)
_dlq_seen = {}          # scoped stream -> DLQ length at the previous reading


def undelivered_still_sec(stream, undelivered, now=None):
    """How long `stream`'s undelivered count has gone without going down.

    0 while it shrinks or is empty. Growth does not restart the clock: a queue
    that only grows is not draining either.
    """
    now = time.monotonic() if now is None else now
    key = scoped('undelivered:' + stream)
    with _lock:
        if not undelivered:
            _undelivered_seen.pop(key, None)
            return 0.0
        seen = _undelivered_seen.get(key)
        if seen is None or undelivered < seen[0]:
            _undelivered_seen[key] = (undelivered, now)
            return 0.0
        return max(0.0, now - seen[1])


def dlq_is_recent(stream, dlq_len, newest_age_ms):
    """Whether `stream`'s dead letters are current loss: the DLQ grew since the
    last reading, or its newest entry is younger than dlq_recent_sec. An entry
    whose age cannot be read counts as recent, so loss is never hidden."""
    key = scoped('dlq:' + stream)
    with _lock:
        previous = _dlq_seen.get(key)
        _dlq_seen[key] = dlq_len or 0
    if not dlq_len:
        return False
    if previous is not None and dlq_len > previous:
        return True
    if newest_age_ms is None:
        return True
    return newest_age_ms <= CFG['dlq_recent_sec'] * 1000


def collect_integrity():
    specs = stream_specs()

    def read(broker):
        rows = [integrity_for_stream(broker, spec) for spec in specs]
        try:
            committed = parse_offset_hash(broker.hgetall(CFG['offset_key']))
        except Exception:                                 # noqa: BLE001
            committed = None
        mem = broker.info('memory')
        stats = broker.info('stats')
        used = int(mem.get('used_memory') or 0)
        maxmem = int(mem.get('maxmemory') or 0)
        out = {
            'tables': rows,
            # Instance-wide, so measured once. Eviction in particular CANNOT be
            # attributed to a table: evicted_keys is a server counter, so it is a
            # link-wide loss signal that applies to every table.
            'evicted_keys': int(stats.get('evicted_keys') or 0),
            'broker_used_pct': round(used * 100.0 / maxmem, 1) if maxmem else None,
            'broker_used_memory': human_bytes(used),
            'broker_maxmemory': human_bytes(maxmem),
            'maxmemory_policy': mem.get('maxmemory_policy'),
            '_offset_ts_sec': (committed or {}).get('ts_sec'),
        }
        policy = out['maxmemory_policy']
        if policy and policy.startswith('volatile'):
            out['eviction_note'] = (
                'policy {} evicts only keys with a TTL; CDC stream entries have '
                'none, so a full broker returns OOM to Debezium rather than '
                'dropping events'.format(policy))
        elif policy and policy != 'noeviction':
            out['eviction_note'] = (
                'policy {} CAN evict stream entries under memory pressure — that '
                'would be silent loss'.format(policy))
        return out

    out = with_broker(read)
    rows = out.get('tables') or []
    healthy = [r for r in rows if r.get('ok')]

    # Per-table verdict FIRST, using the same pure function, so a table cannot be
    # reported clean because the others are. The memory and eviction signals are
    # link-wide, so every table sees them.
    for row in healthy:
        row['verdict'] = integrity_verdict(
            row.get('dead_lettered'), out.get('evicted_keys'), row.get('retrying'),
            row.get('oldest_unacked_ms'), out.get('broker_used_pct'),
            backlog=row.get('undelivered'), applied_per_sec=row.get('drained_per_sec'),
            still_sec=row.get('undelivered_still_sec'),
            dlq_recent=row.get('dlq_recent', True))
        row['headline'] = INTEGRITY_HEADLINE[row['verdict']]
        row['state'] = VERDICT_STATE[row['verdict']]
        row['label'] = row['verdict']

    # Roll-up. Keys the page already reads keep their meaning: counts sum, the
    # longest wait is used, and the verdict is the WORST table's -- averaging a
    # correctness signal would let one table losing data read as "mostly fine".
    out['backlog'] = int(sum(r.get('backlog') or 0 for r in healthy))
    out['undelivered'] = int(sum(r.get('undelivered') or 0 for r in healthy))
    out['unacked'] = int(sum(r.get('unacked') or 0 for r in healthy))
    out['stream_length'] = int(sum(r.get('stream_length') or 0 for r in healthy))
    out['drained_per_sec'] = _sum_present(healthy, 'drained_per_sec')
    out['dead_lettered'] = int(sum(r.get('dead_lettered') or 0 for r in healthy))
    out['retrying'] = int(sum(r.get('retrying') or 0 for r in healthy))
    out['max_attempt_seen'] = _max_present(healthy, 'max_attempt_seen') or 0
    out['max_deliveries'] = CFG['max_deliveries']
    out['oldest_unacked_ms'] = _max_present(healthy, 'oldest_unacked_ms')
    out['consumers_registered'] = int(sum(r.get('consumers_registered') or 0
                                          for r in healthy))
    out['consumers_active'] = _max_present(healthy, 'consumers_active') or 0
    out['table_count'] = len(rows)
    # Samples are already capped per table; take the first few so the panel shows
    # something from whichever table actually has a problem.
    out['dlq_sample'] = [dict(s, table=r.get('table'))
                         for r in healthy for s in (r.get('dlq_sample') or [])][:5]
    out['retry_sample'] = [dict(s, table=r.get('table'))
                           for r in healthy for s in (r.get('retry_sample') or [])][:5]
    out['dlq_key'] = (healthy[0].get('dlq_key') if healthy
                      else CFG['dlq_prefix'] + CFG['stream_key'])

    unreadable = [r for r in rows if not r.get('ok')]
    # A stream that could not be read proves nothing either way, so it is
    # 'unknown', never 'ok'. Without this the roll-up was taken over the
    # readable streams only, and a link whose every stream failed to read
    # reported "no loss".
    for row in unreadable:
        row['verdict'] = 'unknown'
        row['headline'] = INTEGRITY_HEADLINE['unknown']
        row['state'] = VERDICT_STATE['unknown']
        row['label'] = 'unknown'
    verdicts = [r['verdict'] for r in rows if r.get('verdict')]
    out['verdict'] = worst_verdict(verdicts)
    out['headline'] = INTEGRITY_HEADLINE[out['verdict']]
    out['state'] = VERDICT_STATE[out['verdict']]
    out['label'] = out['verdict']
    if len(rows) > 1:
        # Name the tables that are not clean. "loss" on a six-table link is not
        # actionable without knowing which one.
        bad = [r.get('table') or r.get('stream') for r in rows
               if r.get('verdict') not in (None, 'ok')]
        if bad:
            out['affected_tables'] = bad
            out['headline'] = '{} ({})'.format(out['headline'], ', '.join(bad))
    if unreadable:
        out['unreadable_streams'] = [r['stream'] for r in unreadable]
        out['stream_note'] = (
            '{} of {} streams could not be read, so their integrity is unknown: '
            '{}'.format(len(unreadable), len(rows),
                        ', '.join(r['stream'] for r in unreadable)))
    elif not rows:
        out['stream_note'] = ('this link has no streams configured, so nothing '
                              'was checked')
    offset_ts = out.pop('_offset_ts_sec', None)
    try:
        offset_age = time.time() - float(offset_ts) if offset_ts else None
    except (TypeError, ValueError):
        offset_age = None
    out.update(soft(binlog_retention_headroom, offset_age_sec=offset_age))
    return out


VERDICT_STATE = {'loss': 'bad', 'stalled': 'bad', 'unknown': 'warn',
                 'dead_letters': 'warn', 'stuck': 'warn', 'pressure': 'warn',
                 'retrying': 'warn', 'ok': 'ok'}

# Worst first. The roll-up takes the worst table's verdict, because integrity is
# a correctness question: one table losing entries is not offset by five that
# are fine. 'unknown' (a stream we could not read) ranks below the two verdicts
# we KNOW are bad and above the rest: the unread stream may be losing entries,
# and 'retrying' or 'ok' would both claim nothing has been lost.
# 'dead_letters' (loss that happened earlier, nothing new) ranks just below it.
VERDICT_ORDER = ('loss', 'stalled', 'unknown', 'dead_letters', 'stuck', 'pressure',
                 'retrying', 'ok')


def integrity_or_unknown(block):
    """`block` (a safe()-wrapped collect_integrity) with a verdict always set.

    When the broker cannot be reached at all the collector raises and safe()
    returns only an error, with no verdict. That must read as 'unknown' rather
    than leave the page to fill the gap with "nothing lost".
    """
    if not block.get('verdict'):
        block['verdict'] = 'unknown'
        block['headline'] = INTEGRITY_HEADLINE['unknown']
        block['state'] = VERDICT_STATE['unknown']
        block['label'] = 'unknown'
        block.setdefault('stream_note', 'integrity could not be collected{}'.format(
            ': ' + block['error'] if block.get('error') else ''))
    return block


def worst_verdict(verdicts):
    """The worst verdict in `verdicts`; 'unknown' when there is none to rank.

    No verdicts means nothing was read, which is not evidence of "no loss".
    """
    for verdict in VERDICT_ORDER:
        if verdict in verdicts:
            return verdict
    return 'unknown'


def collect_tables(broker=None, sink=None, integrity=None):
    """One row per table: is THIS table flowing, behind, or losing entries?

    The three cards each carry their own per-table list, which is right for each
    card but wrong for the question a reader actually has -- "which table is the
    problem". This joins them on the stream name so one row holds everything about
    one table.

    Accepts already-collected blocks so the dashboard does not pay for a second
    round of broker calls; collects them itself when called standalone.
    """
    broker = broker if broker is not None else safe(collect_broker)
    sink = sink if sink is not None else safe(collect_sink)
    integrity = integrity_or_unknown(
        integrity if integrity is not None else safe(collect_integrity))

    by_stream = {}
    for row in (broker.get('streams') or []):
        by_stream.setdefault(row['stream'], {}).update({
            'stream': row['stream'],
            'table': row.get('table'),
            'key_prefix': row.get('key_prefix'),
            'length': row.get('length'),
            'entries_added': row.get('entries_added'),
            'produced_per_sec': row.get('produced_per_sec'),
            'last_event_age_ms': row.get('last_event_age_ms'),
            'oldest_entry_age_ms': row.get('oldest_entry_age_ms'),
            'stream_ok': row.get('ok'),
            'stream_error': row.get('error'),
        })
    for row in (sink.get('tables') or []):
        by_stream.setdefault(row['stream'], {}).update({
            'stream': row['stream'],
            'table': row.get('table'),
            'consumers': row.get('consumers'),
            'pending': row.get('pending'),
            'group_lag': row.get('group_lag'),
            'applied_per_sec': row.get('applied_per_sec'),
            'oldest_pending_age_ms': row.get('oldest_pending_age_ms'),
            'group_note': row.get('group_note'),
        })
    for row in (integrity.get('tables') or []):
        by_stream.setdefault(row['stream'], {}).update({
            'stream': row['stream'],
            'table': row.get('table'),
            'backlog': row.get('backlog'),
            'undelivered': row.get('undelivered'),
            'unacked': row.get('unacked'),
            'drained_per_sec': row.get('drained_per_sec'),
            'dead_lettered': row.get('dead_lettered'),
            'retrying': row.get('retrying'),
            'oldest_unacked_ms': row.get('oldest_unacked_ms'),
            'verdict': row.get('verdict'),
            'state': row.get('state'),
            'headline': row.get('headline'),
        })

    rows = list(by_stream.values())
    for row in rows:
        # Per-table CDC lag, from the same two parts the banner uses. Reusing
        # broker_wait_ms keeps one definition of "waiting" rather than a second
        # one that drifts.
        wait = broker_wait_ms({'ok': row.get('stream_ok') is not False,
                               'group_lag': row.get('group_lag'),
                               'pending': row.get('pending'),
                               'oldest_entry_age_ms': row.get('oldest_entry_age_ms')},
                              {'oldest_pending_age_ms': row.get('oldest_pending_age_ms')})
        row['broker_wait_ms'] = wait
        # No integrity row for this stream means integrity did not read it, which
        # is 'unknown', not a blank that sorts and rolls up as harmless.
        if not row.get('verdict'):
            row['verdict'] = 'unknown'
    rows.sort(key=lambda r: (VERDICT_ORDER.index(r['verdict'])
                             if r.get('verdict') in VERDICT_ORDER else 99,
                             r.get('table') or ''))
    return {
        'tables': rows,
        'count': len(rows),
        'worst_verdict': worst_verdict([r.get('verdict') for r in rows]),
        'note': 'one row per table in this link. Each table has its own broker '
                'stream, so a link can be caught up on one and behind on '
                'another; the cards above show the link total.',
    }


@monitor_bp.route('/tables', methods=['GET'])
@per_link
def tables():
    return jsonify(safe(collect_tables))


# Warn once Debezium's position is this old, as a share of the binlog retention.
# Half leaves the other half to find and fix whatever is holding capture back.
RETENTION_WARN_PCT = 50


def binlog_retention_headroom(offset_age_sec=None):
    """The most serious loss risk: Debezium falling behind Aurora's binlog retention.

    If a binlog file is purged before Debezium reads it, those changes are lost
    permanently — no retry can recover them. RDS exposes the retention setting via
    mysql.rds_show_configuration.

    `offset_age_sec` is how old Debezium's committed position is (its offset's
    ts_sec; heartbeats keep it current while capture is caught up). Compared
    with the retention it gives the headroom: this function used to report the
    retention alone, so the slow approach to that loss had no signal at all.
    """
    conn = mysql_conn()
    try:
        cur = conn.cursor()
        cur.execute('CALL mysql.rds_show_configuration')
        hours = None
        for row in cur.fetchall() or []:
            if row and str(row[0]).strip() == 'binlog retention hours':
                hours = None if row[1] in (None, 'NULL') else int(row[1])
        out = {'binlog_retention_hours': hours}
        if hours is None:
            out['binlog_retention_note'] = (
                'binlog retention not configured on this cluster — if Debezium '
                'ever falls behind a purge, those changes are unrecoverable')
        elif hours > 0 and offset_age_sec is not None:
            retention = hours * 3600.0
            age = max(0.0, float(offset_age_sec))
            used = round(age * 100.0 / retention, 1)
            out.update({'offset_age_sec': round(age, 1),
                        'retention_used_pct': used,
                        'retention_headroom_sec': int(max(0.0, retention - age))})
            if used >= RETENTION_WARN_PCT:
                out['retention_warn'] = True
                out['binlog_retention_note'] = (
                    'Debezium\u2019s position is {} old, {:.0f}% of the {}h binlog '
                    'retention. Changes still unread when they pass {}h are purged '
                    'and lost for good: find out why capture is behind'.format(
                        human_age(age), used, hours, hours))
        return out
    finally:
        conn.close()


@monitor_bp.route('/integrity', methods=['GET'])
@per_link
def integrity():
    return jsonify(integrity_or_unknown(safe(collect_integrity)))


# ---------------------------------------------------------------------------
# Flow summary (the banner: five hop states + one headline CDC lag)
# ---------------------------------------------------------------------------
def human_age(seconds):
    """'42s' / '7m' / '2h' — a raw 5348.1s is hard to read."""
    if seconds is None:
        return 'unknown'
    if seconds < 90:
        return '{:.0f}s'.format(seconds)
    if seconds < 3600:
        return '{:.0f}m'.format(seconds / 60.0)
    return '{:.1f}h'.format(seconds / 3600.0)


def hop_state(component):
    """A component's state for the banner.

    'unreachable' is deliberately NOT 'bad'. They are different facts and they
    need different words:

      bad          I read this component and it is unhealthy -- tasks are not
                   running, the group has no consumers, the queue is stuck.
      unreachable  I could not read it at all. The component may be perfectly
                   fine; what is broken is my view of it.

    Conflating them made one visibility gap mark a healthy pipeline as failed. The common
    case is not even a fault: a target cache belongs to the customer, so its
    security group need not admit the control plane, and ECDL must not edit it.
    Reporting "target down" there is wrong.

    The discriminator is `error`. It is set only when a read failed -- safe() and
    soft() attach it when they catch an exception. A component that is genuinely
    down is read SUCCESSFULLY and reports state 'bad' with no error, so checking
    `ok` alone cannot tell the two apart.
    """
    if not component:
        return 'unreachable'
    if component.get('error'):
        return 'unreachable'
    if component.get('ok') is False:
        return 'bad'
    return component.get('state') or 'ok'


def broker_wait_ms(broker, sink):
    """How long the oldest not-yet-applied event has been waiting.

    Delivered-but-unacked entries are the normal case, so prefer the consumer
    group's oldest pending entry. If entries exist that the sink has not even
    read yet, fall back to the age of the oldest entry still in the stream.
    Caught up (nothing pending, no lag) is a real zero, not a missing value.
    """
    if broker.get('ok') is False:
        return None                       # unreachable broker: unknown, not zero
    pending = sink.get('oldest_pending_age_ms')
    if pending is not None:
        return pending
    if (broker.get('group_lag') or 0) > 0:
        return broker.get('oldest_entry_age_ms')
    if (broker.get('pending') or 0) == 0 and (broker.get('group_lag') or 0) == 0:
        return 0
    return None


def visibility_gap_remedy(component):
    """Where an unreadable component lives, and what would make it readable.

    "not visible from the control plane" is true but not useful on its own. This
    names the endpoint that timed out and the concrete change that would fix it,
    so the operator does not need to open the console to work out which of five
    security groups is involved.

    The distinction that matters is ownership. Aurora and the target cache belong
    to the customer and may legitimately refuse this host. The broker does not:
    ECDL creates it, and the CDC stack grants it ingress from the pipeline TASK
    security group only -- so the monitor cannot reach the one component ECDL
    is entirely responsible for. That is a gap in the deployer, not a customer
    choice, and it is worth saying so plainly.
    """
    if component == 'broker':
        return {
            'endpoint': '{}:{}'.format(CFG['broker_host'], CFG['broker_port']),
            'owner': 'ecdl',
            'fix': 'the broker\u2019s security group admits the pipeline tasks '
                   'but not this host. Allow TCP {} from the control plane\u2019s '
                   'security group to read stream depth, consumer-group lag and '
                   'delivery integrity.'.format(CFG['broker_port']),
        }
    if component == 'target':
        return {
            'endpoint': '{}:{}'.format(CFG['cache_host'], CFG['cache_port']),
            'owner': 'customer',
            'fix': 'the target cache is customer-owned and need not admit this '
                   'host. Allow TCP {} from the control plane only if you want '
                   'key counts and cache latency here.'.format(CFG['cache_port']),
        }
    if component == 'aurora':
        return {
            'endpoint': '{}:{}'.format(CFG['db_host'], CFG['db_port']),
            'owner': 'customer',
            'fix': 'the source database is customer-owned. Allow TCP {} from the '
                   'control plane only if you want source-side throughput and '
                   'binlog position here.'.format(CFG['db_port']),
        }
    if component in ('debezium', 'sink'):
        # These are read two ways: ECS gives task counts, the broker gives
        # progress. Losing the broker loses the second half, which is why they
        # lose visibility alongside it rather than on their own.
        return {
            'owner': 'ecdl',
            'fix': 'task counts come from ECS and progress comes from the '
                   'broker, so this loses visibility whenever the broker does. '
                   'Component logs still work \u2014 they are read from '
                   'CloudWatch, which needs no network path to the pipeline.',
        }
    return {}


def build_flow(aurora, debezium, broker, sink, target, probe=None):
    """Assemble the banner: five hop states plus one headline CDC lag.

    Lag is reported from the best source available, and which source it came
    from is always stated so the number can never be mistaken for something it
    is not:

      probe      - measured end to end (heartbeat row -> cache). Most accurate,
                   and the only source that works when the pipeline is healthy
                   and idle, because a caught-up sink leaves nothing to observe.
      in-flight  - capture + broker wait, observed from an entry still resident
                   in the stream. Only available while the sink is behind.
      none       - nothing measurable yet; say so rather than invent a zero.
    """
    hops = {
        'aurora': hop_state(aurora),
        'debezium': hop_state(debezium),
        'broker': hop_state(broker),
        'sink': hop_state(sink),
        'target': hop_state(target),
    }
    capture = debezium.get('capture_lag_ms')
    wait = broker_wait_ms(broker, sink)
    flow = {'hops': hops, 'capture_ms': capture, 'broker_wait_ms': wait}

    probe = probe or {}
    probe_lag = probe.get('lag_ms') if probe.get('ok') else None

    # Is CDC actually in the path right now? Two cases where a lag figure is
    # misleading rather than informative:
    #
    #  1. A direct bulk load. The loader reads Aurora and writes the target
    #     itself, deliberately bypassing the broker, so the cache is being
    #     populated at high rate while CDC carries nothing. Any lag number shown
    #     during that belongs to whatever small amount of traffic came before it.
    #  2. A write burst the sink is still draining. The lag is real, but it is a
    #     queue being processed, not the steady-state staleness the number
    #     looks like. Naming it "backlog" stops a transient reading as a defect.
    produced = broker.get('produced_per_sec')
    applied = sink.get('applied_per_sec')
    target_writes = target.get('writes_per_sec')
    # True queue depth = undelivered + unacked entries. Prefer the collector's
    # own 'backlog' but fall back to computing it from group_lag + pending, so
    # this is right whether the broker dict carries the derived field or the raw
    # counts. This is the signal that separates a real draining backlog from a
    # single stuck straggler (see the in-flight lag gate below).
    backlog_now = max(broker.get('backlog') or 0, backlog_of(broker))
    bypassed = ((target_writes or 0) > 500 and (produced or 0) < 50)
    if bypassed:
        flow['cdc_in_path'] = False
        flow['cdc_context'] = 'bypassed'
        flow['cdc_context_note'] = (
            'direct load in progress: {:,.0f} writes/s straight into the cache '
            'while the broker carries {:,.0f} events/s \u2014 CDC is not in this '
            'path, so a lag figure would not describe it'.format(
                target_writes or 0, produced or 0))
    else:
        flow['cdc_in_path'] = True
        flow['cdc_context'] = 'draining' if backlog_now > 0 else 'steady'
        flow['cdc_context_note'] = None
    catching = (debezium or {}).get('catching_up') or []
    if catching and not bypassed:
        worst = catching[0]
        flow['cdc_context'] = 'catching_up'
        flow['catching_up'] = catching
        flow['cdc_context_note'] = (
            'catching up: replaying changes committed up to {} before they were '
            'captured ({}). Cached rows of {} can show older values until the '
            'replay has drained'.format(
                human_age(worst['behind_ms'] / 1000.0),
                ', '.join(t['table'] for t in catching),
                'this table' if len(catching) == 1 else 'these tables'))

    if bypassed:
        # Do not show as the main figure a lag that describes traffic not flowing
        # through CDC. The measurement is still available on request.
        flow['cdc_lag_ms'] = None
        flow['lag_source'] = None
        flow['lag_note'] = flow['cdc_context_note']
    elif (capture is not None and wait is not None
          and backlog_now >= CFG['inflight_min_backlog']):
        # A genuine backlog is in flight right now: many entries queued and
        # moving, so this is a live observation of real queue depth that is more current than a
        # probe taken moments ago. The depth gate matters — `wait` is the age of
        # the oldest unacked entry, which climbs without bound for a single stuck
        # straggler in a caught-up pipeline. Requiring a real queue keeps that
        # lone-entry case out of the headline (it falls through to the probe and
        # is reported as a "stuck" warning on the sink hop instead).
        flow['cdc_lag_ms'] = capture + wait
        flow['lag_source'] = 'in-flight'
        flow['lag_note'] = (
            'backlog draining: {:,} entries queued behind the current write '
            'burst. This is queue depth, not steady-state staleness \u2014 it '
            'falls back to a few ms once the burst ends.'.format(backlog_now))
    elif probe_lag is not None:
        age = probe.get('age_sec')
        flow['cdc_lag_ms'] = probe_lag
        flow['lag_source'] = 'probe'
        flow['lag_measured_age_sec'] = age
        stale = age is not None and age > CFG['probe_stale_sec']
        flow['lag_stale'] = stale
        if stale:
            flow['lag_note'] = ('STALE: measured {} ago \u2014 press Measure now '
                                'for the current figure'.format(human_age(age)))
        else:
            flow['lag_note'] = 'measured end to end {}s ago'.format(age)
    else:
        flow['cdc_lag_ms'] = None
        flow['lag_source'] = None
        flow['lag_note'] = (
            probe.get('note') or probe.get('error')
            or 'no lag measurement yet — the sink is caught up, so there is '
               'nothing in flight to observe. Run a probe to measure.')

    flow['flowing'] = bool((produced or 0) > 0 or (applied or 0) > 0)

    down = [name for name, state in hops.items() if state == 'bad']
    degraded = [name for name, state in hops.items() if state == 'warn']
    paused = [name for name, state in hops.items() if state == 'paused']
    starting = [name for name, state in hops.items() if state == 'starting']
    unseen = sorted(name for name, state in hops.items() if state == 'unreachable')
    backlog = broker.get('backlog') or 0

    # Published so the page can mark exactly which panels are estimates, and say
    # why, instead of leaving a reader to wonder whether a blank card means
    # "healthy and quiet" or "no data".
    if unseen:
        sources = {'aurora': aurora, 'debezium': debezium, 'broker': broker,
                   'sink': sink, 'target': target}
        flow['visibility_gaps'] = [
            dict({'component': name,
                  'error': (sources[name] or {}).get('error')},
                 **visibility_gap_remedy(name))
            for name in unseen
        ]
        # Whose problem it is differs by component, and that distinction is the
        # actionable part. Aurora and the target cache are CUSTOMER-owned, so
        # their security groups need not admit this host and having no view of them
        # is a legitimate configuration. The BROKER is different: ECDL creates it,
        # so if the control plane cannot reach it that is caused by ECDL's own configuration.
        ours = [name for name in unseen if name == 'broker']
        flow['visibility_note'] = (
            '{} not visible from the control plane, so {} panel(s) below are '
            'unknown rather than healthy. The component itself may be fine.'
            .format(', '.join(unseen), len(unseen))
            + (' The broker is created by ECDL, so unlike a customer-owned '
               'source or target this one is ours to fix: its security group '
               'admits the pipeline tasks but not this host.' if ours else
               ' A source database and a target cache are customer-owned, so '
               'their security groups need not admit this host.'))
    if starting and not down:
        # Tasks coming back up after the handoff. Named as the product action so
        # it reads as a step in progress, not a fault.
        flow['starting'] = starting
        flow['message'] = ('Turning on ECDL{}'.format(
            ' \u00b7 backlog {:,} to replay'.format(backlog) if backlog else ''))
    elif paused and not down:
        # CDC stopped deliberately (desiredCount 0) during a snapshot handoff.
        # Stated plainly as a product state rather than narrated: the audience
        # wants to know ECDL is off, not a paragraph about the binlog.
        flow['paused'] = paused
        flow['message'] = ('ECDL off{}'.format(
            ' \u00b7 backlog {:,} draining'.format(backlog) if backlog else ''))
    elif len(unseen) == len(hops):
        # Everything unreadable at once is a connectivity or config problem on
        # this host, not five simultaneous pipeline failures.
        flow['message'] = ('no component reachable from this host \u2014 check the '
                           'endpoints and security groups')
    elif down:
        flow['message'] = '{} down{}{}'.format(
            ', '.join(down),
            ' \u00b7 backlog {:,} entries'.format(backlog) if backlog else '',
            ' \u00b7 {} not visible'.format(', '.join(unseen)) if unseen else '')
    elif unseen:
        # Nothing is reported down, but part of the pipeline cannot be seen. The
        # accurate headline is what is unknown, NOT "all hops flowing" -- that would
        # claim knowledge the page does not have. Equally it is not "down".
        flow['message'] = '{} not visible from the control plane{}'.format(
            ', '.join(unseen),
            ' \u00b7 the rest is flowing' if flow['flowing'] else '')
    elif degraded:
        flow['message'] = '{} degraded \u00b7 backlog {:,} entries'.format(
            ', '.join(degraded), backlog)
    elif produced is None and applied is None:
        # First poll after a restart: rates need two samples. Claiming "idle"
        # here would be a guess, and it would be wrong under live load.
        flow['message'] = 'all components up \u00b7 measuring throughput\u2026'
    elif not flow['flowing']:
        # Not a fault: nothing is writing to Aurora, so there is nothing to
        # replicate. Say that, in units a human reads (78m, not 4707s).
        idle = broker.get('last_event_age_ms')
        flow['message'] = (
            'all components up \u00b7 nothing to replicate'
            + (' \u2014 last change captured {} ago'.format(human_age(idle / 1000.0))
               if idle else ''))
    else:
        flow['message'] = 'all hops flowing'
    if flow.get('cdc_context') == 'catching_up':
        # The lag figure is real but says nothing about rows going back in
        # time, so the note says it. It is the headline only when nothing more
        # urgent (down, paused, degraded, unseen) is.
        flow['lag_note'] = flow['cdc_context_note']
        if (flow['message'] == 'all hops flowing'
                or flow['message'].startswith('all components up')):
            flow['message'] = flow['cdc_context_note']
    return flow


# ---------------------------------------------------------------------------
# Aggregate endpoint — the page makes exactly one request against this
# ---------------------------------------------------------------------------
def collect_dashboard():
    from concurrent.futures import ThreadPoolExecutor

    jobs = {
        'aurora': collect_aurora,
        'debezium': collect_debezium,
        'broker': collect_broker,
        'sink': collect_sink,
        'target': collect_target,
        'dataflow': collect_dataflow,
        'integrity': collect_integrity,
        # A bulk load's own progress. writeflow is built after these finish,
        # because it reuses their rates rather than collecting them twice.
        'bulkload': collect_bulkload,
    }
    # The overlay is thread-local and pool threads do NOT inherit it, so it is
    # carried across explicitly (see current_overlay).
    overlay = current_overlay()
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(in_overlay(overlay, safe), fn)
                   for name, fn in jobs.items()}
        out = {name: future.result() for name, future in futures.items()}

    # Debezium runs in parallel with Aurora, so it cannot know the source binlog
    # position at collection time. Fill the gap in afterwards from Aurora's.
    out['debezium'].update(binlog_gap(
        out['aurora'],
        {'file': out['debezium'].get('offset_binlog_file'),
         'pos': out['debezium'].get('offset_binlog_pos')}))

    # Built here so it can reuse the rates the collectors above already fetched.
    out['integrity'] = integrity_or_unknown(out['integrity'])
    out['writeflow'] = safe(collect_writeflow, aurora=out['aurora'],
                            broker_stats=out['broker'], sink=out['sink'],
                            target=out['target'])
    # Per-table join. Reuses the three blocks rather than re-reading the broker:
    # a second read would also re-sample every rate inside MIN_RATE_INTERVAL_SEC
    # and get the previous value back for all of them.
    out['tables'] = safe(collect_tables, broker=out['broker'], sink=out['sink'],
                         integrity=out['integrity'])
    # Read-only: shows the last probe result, never triggers one.
    out['probe'] = last_probe()
    # Percentiles for both stores measured in the same process. Promoted to the
    # top level because it is a comparison, not a property of either store — it
    # should not be inside the Valkey section.
    app_latency = (out.get('target') or {}).pop('demo_app', None)
    if app_latency:
        out['app_latency'] = app_latency
    out['flow'] = build_flow(out['aurora'], out['debezium'], out['broker'],
                             out['sink'], out['target'], probe=out['probe'])

    # Lost or dead-lettered entries are a correctness failure, not a performance
    # one: escalate them into the banner so they cannot be scrolled past.
    integrity_block = out.get('integrity') or {}
    if integrity_block.get('verdict') == 'loss':
        out['flow']['integrity_alert'] = integrity_block.get('headline')
        out['flow']['hops']['sink'] = 'bad'
    elif integrity_block.get('verdict') == 'stalled':
        # Not loss yet, but the pipeline is not moving: as prominent as loss.
        out['flow']['integrity_alert'] = integrity_block.get('headline')
        out['flow']['hops']['sink'] = 'bad'
    elif integrity_block.get('verdict') in ('unknown', 'dead_letters', 'stuck',
                                            'retrying', 'pressure'):
        out['flow']['integrity_alert'] = integrity_block.get('headline')
    elif integrity_block.get('retention_warn'):
        # Nothing lost yet, but the one loss no retry can undo is approaching.
        out['flow']['integrity_alert'] = integrity_block.get('binlog_retention_note')
    # Jira story F105: alarms that reach nobody (see alarm_topic.py). A separate field,
    # so the integrity alert above keeps its meaning.
    topic = alarm_topic.cached_topic_status(CFG.watched_link(), aws)
    if topic is not None:
        out['alarm_topic'] = topic
        if topic.get('alert'):
            out['flow']['alarm_alert'] = topic['alert']
    out['collected_at'] = now_ms()
    out['region'] = CFG['region']
    return out


@monitor_bp.route('/dashboard', methods=['GET'])
@per_link
def dashboard():
    return jsonify(collect_dashboard())


# ---------------------------------------------------------------------------
# Source-vs-target data flow ("is my write in the cache yet?")
# ---------------------------------------------------------------------------
# Fallback only, for a table whose schema could not be read. These are the demo
# table's mutable columns; every other table gets its own, read from
# information_schema by table_shape() below.
COMPARE_FIELDS = ('price', 'stock_count')


def _sql(template, **identifiers):
    """SQL text for the (MySQL) source with its table/column names quoted.

    Names cannot be bound as parameters, so they are validated and backtick-
    quoted by sqlident.render, which raises on anything that is not a plain
    identifier. Values stay as %s and are bound by the driver.
    """
    return sqlident.render('mysql', template, **identifiers)

# Columns worth showing beside the key. Anything wider than this is a blob or a
# description, which tells a reader nothing about whether CDC is keeping up.
_COMPARE_TYPES = ('int', 'bigint', 'smallint', 'mediumint', 'tinyint',
                  'decimal', 'numeric', 'float', 'double',
                  'varchar', 'char', 'timestamp', 'datetime', 'date')
_MAX_COMPARE = 3


def table_shape(cur, table):
    """The primary key and a few comparable columns for `table`.

    Read from information_schema rather than assumed. This used to be hardcoded
    to the demo table's `id, sku, price, stock_count`, which meant the data-flow
    panel raised 1054 "Unknown column 'sku'" for every link whose table was
    anything else -- i.e. the panel worked only for the pipeline it was written
    against, and reported a broken source for all the others.

    Cached per table: a schema does not change between polls, and this runs on
    the customer's database.
    """
    def produce():
        cur.execute(
            'SELECT column_name, data_type, column_key FROM information_schema.columns '
            'WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position',
            schema_and_table(table))
        # MySQL 8 answers with uppercase column names in information_schema, so
        # index positionally rather than by key.
        cols = [(str(r[0]), str(r[1]).lower(), str(r[2] or '')) for r in cur.fetchall()]
        # Column names come from the customer's schema and are put into SQL
        # text later, so a name that is not a plain identifier (spaces, quotes,
        # non-ASCII) is left out of the panel rather than quoted around.
        pk_cols = [name for name, _t, key in cols if key == 'PRI']
        if pk_cols and not sqlident.is_safe(pk_cols[0]):
            raise sqlident.UnsafeIdentifier(
                'the primary key of {} ({!r}) is not a plain identifier, so this '
                'panel cannot query it'.format(table, pk_cols[0]))
        skipped = [name for name, _t, _k in cols if not sqlident.is_safe(name)]
        cols = [c for c in cols if sqlident.is_safe(c[0])]
        if not cols:
            return {'pk': 'id', 'label': None, 'compare': list(COMPARE_FIELDS),
                    'note': 'could not read the schema of {}; assuming the '
                            'default shape'.format(table)}
        pk = next((name for name, _t, key in cols if key == 'PRI'), cols[0][0])
        rest = [(name, dtype) for name, dtype, _k in cols if name != pk]
        # A short text column next to the key helps a human recognise the row.
        label = next((name for name, dtype in rest
                      if dtype in ('varchar', 'char')), None)
        compare = [name for name, dtype in rest
                   if dtype in _COMPARE_TYPES and name != label][:_MAX_COMPARE]
        if not compare:
            # Every non-key column was unshowable; compare the label instead of
            # nothing, so the panel still answers "did this row arrive".
            compare = [label] if label else []
        shape = {'pk': pk, 'label': label, 'compare': compare,
                 'columns': [name for name, _t, _k in cols]}
        if skipped:
            shape['note'] = ('not shown (not a plain identifier): {}'
                             .format(', '.join(repr(n) for n in skipped)))
        return shape

    # Keyed by the database's endpoint too: two links whose sources share a
    # database and table name are still different tables.
    return cached('shape:{}'.format(_source_scope(table)), 300, produce)


def source_table(table):
    """The name to query `table` by in the source: 'schema.table' when the link
    names the table's schema, else the bare name (resolved in db_name).

    The link's Tables parameter is schema-qualified ('Inventory.products'),
    but the monitor kept only the last part, so a table outside SourceDBName
    was read, counted and probed in SourceDBName -- a different table, or
    none. Key prefixes and transforms still use the bare name, as the sink does.
    """
    return (CFG.get('source_tables') or {}).get(table) or table


def schema_and_table(table):
    """(schema, table) for information_schema lookups of `table`."""
    parts = source_table(table).split('.')
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return CFG['db_name'], parts[-1]


def _source_scope(table):
    """host:port/schema/table, the identity of a source table for caching."""
    schema, name = schema_and_table(table)
    return '{}:{}/{}/{}'.format(CFG['db_host'], CFG['db_port'], schema, name)


def shape_select(shape):
    """The SELECT list for a shape: key, then label, then compared columns."""
    names = [shape['pk']]
    if shape.get('label'):
        names.append(shape['label'])
    names.extend(shape.get('compare') or [])
    return ', '.join(names), names


def _shape_row(row, shape):
    """A DB tuple -> the dict the comparison and the payload expect.

    Numbers go through as_number so 412.5 and '412.50' compare equal; the label
    stays text.
    """
    _select, names = shape_select(shape)
    out = {'id': row[0]}
    for index, name in enumerate(names[1:], start=1):
        value = row[index] if index < len(row) else None
        out[name] = value if name == shape.get('label') else as_number(value)
    # Kept so the existing table header, which shows a recognisable name beside
    # the key, keeps working whatever that column is actually called.
    out['label'] = out.get(shape['label']) if shape.get('label') else None
    return out


def safe_identifier(name, fallback):
    """A table or column name to put into SQL, or `fallback` when none is set.

    Names cannot be bound as SQL parameters. One that is set but is not a plain
    identifier RAISES: substituting the fallback would quietly query a different
    table than the one configured. Quoting happens later, in _sql().
    """
    if not name:
        return fallback
    return sqlident.validate_qualified(name, 'identifier')


def _count_rows(cur, table):
    """Exact row count. Note the cost: this is a full covering-index scan, so it
    reads every row in the table and Aurora's Innodb_rows_read counter jumps by
    the whole table at once. The timestamp lets the row-throughput aggregates
    drop whichever sample absorbed that jump, so the monitor's own scan is not
    reported as workload."""
    global _last_row_scan_ms
    cur.execute(_sql('SELECT COUNT(*) FROM {t}', t=table))
    row = cur.fetchone()
    _last_row_scan_ms = now_ms()
    return row[0] if row else None


def sample_ids(cur, table, n=40, row_count=None):
    """Real primary keys from a randomly chosen region, for a cache spot check.

    Sampling by id VALUE does not work here and the random row sampler above
    already says why: the id space is sparse, so points that fall in the gaps all
    resolve to the same row at the start of the dense block. Spreading 40 probe
    points across 1 .. 1,006,740,913 returned 2 distinct ids.

    So pick the position instead of the value: one OFFSET on the primary key
    can reach any position in the table, then take the next n consecutive ids.

    That is NOT cheap, and an earlier version of this docstring claimed it was.
    `LIMIT n OFFSET k` makes InnoDB walk k+n index entries to reach the offset,
    so a random offset into 6.2M rows reads about 3M of them on average. It is
    uniform row sampling, which is what a coverage check needs, but it appears as a
    multi-million spike in Innodb_rows_read — so the scan is timestamped and the
    affected sample is excluded from the row-throughput aggregates, and it runs
    rarely. `/reconcile` is the exhaustive check when an exact answer is wanted.
    """
    global _last_row_scan_ms
    if not row_count:
        row_count = _count_rows(cur, table)
    if not row_count:
        return []
    anchor_offset = random.randint(0, max(0, int(row_count) - n - 1))
    cur.execute(_sql('SELECT {pk} FROM {t} ORDER BY {pk} LIMIT %s OFFSET %s',
                     t=table, pk='id'), (n, anchor_offset))
    out = sorted({r[0] for r in cur.fetchall()})
    _last_row_scan_ms = now_ms()
    return out


def coverage(target, ids):
    """How many of those ids have a key in the cache. Pipelined, so ~one round trip."""
    if not ids:
        return None
    pipe = target.pipeline(transaction=False)
    for key_id in ids:
        pipe.exists('{}{}'.format(CFG['key_prefix'], key_id))
    results = pipe.execute()
    present = sum(1 for r in results if r)
    missing = [key_id for key_id, r in zip(ids, results) if not r]
    return {'checked': len(ids), 'present': present,
            'missing': missing[:10], 'missing_count': len(missing)}


def as_number(value):
    """MySQL DECIMAL arrives as decimal.Decimal, which Flask would emit as a
    JSON *string*. Send a real number so both sides of the comparison match."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


def _norm(value):
    """Compare DB and cache values as text: 412.5 and '412.50' are the same price."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    try:
        return '{:.2f}'.format(float(value))
    except (TypeError, ValueError):
        return str(value).strip()


def reconcile_rows(db_rows, cache_rows, fields=COMPARE_FIELDS, cache_field_map=None):
    """Pair the newest Aurora rows with their cache hashes.

    synced  - present in the cache with matching values
    pending - not in the cache. Whether that is 'in flight' or 'excluded by a
              transform filter' is NOT decided here: the counters already answer
              it (see the panel). When a table is caught up -- rows read ==
              rows applied, backlog 0 -- nothing is in flight, so an absent row
              is absent BY DESIGN. When it is behind, an absent row is in flight.
              The panel decides this from the per-table backlog rather than
              re-implementing the sink's filter here, which would duplicate it
              for every table and every operator.
    differs - present but stale, i.e. the update has not been applied yet

    `cache_field_map` maps a SOURCE column to the cache field its value is written to,
    so a renamed field (transform `rename`) is compared against the right cache
    field instead of one that no longer exists -- the bug that made every row
    read "differs" after a rename even though the value was correctly cached.
    Default is identity: the cache field has the same name as the source column.
    """
    cfm = cache_field_map or {}
    out = []
    for row in db_rows:
        cached_row = cache_rows.get(row['id']) or cache_rows.get(str(row['id'])) or {}
        # 'label' is whatever recognisable text column this table has; 'sku' is
        # kept alongside it only so an older page still renders.
        label = row.get('label') if row.get('label') is not None else row.get('sku')
        entry = {'id': row['id'], 'label': label, 'sku': label}
        for field in fields:
            entry['db_' + field] = row.get(field)
            entry['cache_' + field] = cached_row.get(cfm.get(field, field))
        if not cached_row:
            entry['state'] = 'pending'
        elif all(_norm(row.get(f)) == _norm(cached_row.get(cfm.get(f, f)))
                 for f in fields):
            entry['state'] = 'synced'
        else:
            entry['state'] = 'differs'
        out.append(entry)
    return out


def collect_dataflow(limit=10, sample='newest'):
    # One block per table in the link, not just the configured one. Each table
    # has its own stream, its own primary key and shape, its own key prefix and
    # its own transform, so the panel can show every table's rows source-vs-target
    # rather than only the first. Counters (rows read/applied) come from the
    # broker streams on the dashboard; this supplies the sample rows.
    specs = stream_specs()
    conn = mysql_conn()
    sampled = []          # per-table: shape + db_rows, before the cache lookup
    try:
        cur = conn.cursor()
        for spec in specs:
            table = spec.get('table') or 'products'
            try:
                # Inside the try: a table name that is not a plain identifier
                # fails its own block, not the whole panel.
                table = safe_identifier(table, 'products')
                sql_table = safe_identifier(source_table(table), 'products')
                shape = table_shape(cur, table)
                pk = safe_identifier(shape['pk'], 'id')
                _select, names = shape_select(shape)
                if sample == 'random':
                    # Walk the index by row position, not by id value: a random
                    # OFFSET on the primary key is an index-only scan and can
                    # reach any position in the table even when the id space is sparse.
                    cur.execute(_sql('SELECT MIN({pk}), MAX({pk}) FROM {t}',
                                     pk=pk, t=sql_table))
                    min_id, max_id = cur.fetchone()
                    db_rows, seen = [], set()
                    lo = int(min_id or 0)
                    cursor_id = lo - 1
                    for _ in range(limit):
                        step = random.randint(1, max(1, CFG['random_sample_stride']))
                        cur.execute(_sql('SELECT {cols} FROM {t} WHERE {pk} > %s '
                                         'ORDER BY {pk} LIMIT 1 OFFSET %s',
                                         cols=names, t=sql_table, pk=pk),
                                    (cursor_id, step))
                        row = cur.fetchone()
                        if row is None:
                            cursor_id = lo - 1
                            continue
                        cursor_id = row[0]
                        if row[0] in seen:
                            continue
                        seen.add(row[0])
                        db_rows.append(_shape_row(row, shape))
                else:
                    # Newest rows by primary key: a descending index scan, not a
                    # table scan, and no COUNT(*) (which would scan the whole table
                    # and show up as load on the database this page measures).
                    cur.execute(_sql('SELECT {cols} FROM {t} ORDER BY {pk} DESC LIMIT %s',
                                     cols=names, t=sql_table, pk=pk), (limit,))
                    db_rows = [_shape_row(r, shape) for r in cur.fetchall()]
                    cur.execute(_sql('SELECT MAX({pk}) FROM {t}', pk=pk, t=sql_table))
                    mrow = cur.fetchone()
                    max_id = mrow[0] if mrow else None
                sampled.append({'table': table, 'ok': True, 'shape': shape,
                                'pk': shape['pk'], 'key_prefix': key_prefix_for(table),
                                'aurora_max_id': max_id, 'db_rows': db_rows})
            except Exception as exc:                      # noqa: BLE001
                # One bad table must not blank the whole panel: report it against
                # its own block and keep the others.
                sampled.append({'table': table, 'ok': False,
                                'error': '{}: {}'.format(type(exc).__name__, exc),
                                'db_rows': []})
    finally:
        conn.close()

    # All tables' cache lookups over one target connection.
    def read_cache(target):
        found = {}
        for t in sampled:
            if not t.get('ok'):
                continue
            rows = {}
            for row in t['db_rows']:
                hashed = target.hgetall('{}{}'.format(t['key_prefix'], row['id']))
                if hashed:
                    rows[row['id']] = hashed
            found[t['table']] = rows
        return found

    cache_by_table = soft(with_target, read_cache)
    cache_error = (cache_by_table.pop('error', None)
                   if isinstance(cache_by_table, dict) else None)
    if not isinstance(cache_by_table, dict):
        cache_by_table = {}

    tables_out = []
    for t in sampled:
        if not t.get('ok'):
            tables_out.append({'table': t['table'], 'ok': False, 'error': t.get('error'),
                               'rows': [], 'synced': 0, 'pending': 0, 'differs': 0})
            continue
        # Compare against the fields THIS table's transform produces: drop columns
        # the transform does not select, and look each kept column up under its
        # renamed cache field, so a rename or a dropped column is not a false
        # "differs".
        rename, selected = transform_for(t['table'])
        rename = rename or {}
        compare = list(t['shape']['compare'])
        if selected is not None:
            kept = [f for f in compare if f in selected]
            compare = kept or compare
        cache_map = {f: rename.get(f, f) for f in compare}
        rows = reconcile_rows(t['db_rows'], cache_by_table.get(t['table'], {}),
                              fields=compare, cache_field_map=cache_map)
        tables_out.append({
            'table': t['table'], 'ok': True, 'key_prefix': t['key_prefix'],
            'pk': t['pk'], 'aurora_max_id': t['aurora_max_id'], 'rows': rows,
            'synced': sum(1 for r in rows if r['state'] == 'synced'),
            'pending': sum(1 for r in rows if r['state'] == 'pending'),
            'differs': sum(1 for r in rows if r['state'] == 'differs'),
        })

    first = tables_out[0] if tables_out else {}
    out = {
        'sample': sample,
        'tables': tables_out,
        # Back-compat single-table fields (the first table), for the reconcile
        # key prefix and any reader that still expects one table.
        'table': first.get('table'),
        'key_prefix': first.get('key_prefix') or CFG['key_prefix'],
        'rows': first.get('rows', []),
        'synced': first.get('synced', 0),
        'pending': first.get('pending', 0),
        'differs': first.get('differs', 0),
        'note': ('rows read/applied on the counters are CDC change-events, not '
                 'total rows. Use POST /reconcile for an exact full-table vs '
                 'cache-key count of the primary table.'),
    }
    if cache_error:
        out['cache_error'] = cache_error
    return out


@monitor_bp.route('/dataflow', methods=['GET'])
@per_link
def dataflow():
    sample = 'random' if request.args.get('sample') == 'random' else 'newest'
    try:
        limit = max(1, min(50, int(request.args.get('limit', 10))))
    except (TypeError, ValueError):
        limit = 10
    return jsonify(safe(collect_dataflow, limit=limit, sample=sample))


# ---------------------------------------------------------------------------
# Writes in flight, read off the broker stream
# ---------------------------------------------------------------------------
# collect_dataflow watches the newest rows by primary key, which is the wrong
# place to look while a load is running: the load updates *random* ids out of a
# 300k set, so the highest ids almost never move and the table appears unchanged.
# The stream is where the writes actually are. Each entry is one committed row
# change, so the newest few entries ARE the writes currently flowing to Valkey.
def parse_event(entry_id, fields):
    """One CDC stream entry -> id, operation and the values being applied."""
    out = {'entry_id': entry_id, 'arrived_ms': stream_id_ms(entry_id)}
    try:
        event = json.loads((fields or {}).get('value'))
    except (TypeError, ValueError):
        return None
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else event
    source = payload.get('source') if isinstance(payload.get('source'), dict) else {}
    # Debezium's unwrapped ("extended") shape puts the new row at the top level;
    # the enveloped shape nests it under after/before.
    after = payload.get('after') if isinstance(payload.get('after'), dict) else payload
    out['op'] = payload.get('op') or payload.get('__op')
    out['committed_ms'] = source.get('ts_ms') or payload.get('ts_ms')
    if out['arrived_ms'] and out['committed_ms']:
        raw = out['arrived_ms'] - int(out['committed_ms'])
        out['capture_ms'] = max(0, raw)
        if raw < 0:
            out['clock_skew_ms'] = -raw           # see capture_lag_from_entry
    row_id = after.get('id')
    if row_id is None:
        try:
            row_id = json.loads(fields.get('key') or '{}').get('id')
        except (TypeError, ValueError):
            row_id = None
    out['id'] = row_id
    # Record the columns this event actually carried, and compare those. Using a
    # fixed list meant that on any table without them both sides read None,
    # None == None, and the write was reported as "applied" having compared
    # nothing at all -- a false confirmation, which is more misleading than no answer.
    event_fields = [name for name in after.keys() if name != 'id'][:4]
    out['event_fields'] = event_fields
    for field in event_fields:
        out['event_' + field] = after.get(field)
    return out


OP_WORDS = {'c': 'insert', 'u': 'update', 'd': 'delete', 'r': 'snapshot'}


def source_values_allowed():
    """Whether this view may return the source's row values.

    The opt-in is MonitorSourceSecretArns: the control plane's role can read a
    link's source secret only when the operator listed it there, and the docs
    promise that without it the page shows no rows from that database. The
    write-flow list reads row values off the BROKER, which needs no source
    secret, so it returned them regardless. So the test is the same one the
    source panels pass: can this view read the source credential? (Or, with no
    link watched, has the operator set DEMO_DB_PASSWORD for the host's own
    source.) Cached for a minute, because a denied read is not cached by
    secret_value and this runs on every poll.
    """
    if _env_password('DEMO_DB_PASSWORD'):
        return True
    secret_id = CFG['db_secret_id']
    if not secret_id or not boto3:
        return False

    def produce():
        try:
            secret_value(secret_id)
            return True
        except Exception:                                 # noqa: BLE001
            return False
    return cached('source-optin:' + secret_id, 60, produce)


# What a write-flow event keeps when source values are withheld: its timing,
# operation, table and outcome, and the NAMES of the columns it carried.
_EVENT_METADATA = ('entry_id', 'arrived_ms', 'committed_ms', 'capture_ms',
                   'clock_skew_ms', 'op', 'op_word', 'table', 'age_ms',
                   'event_fields', 'applied', 'state')


def withhold_values(event):
    """`event` without its row values (the key and every column value)."""
    return dict((k, event[k]) for k in _EVENT_METADATA if k in event)


def collect_writeflow(limit=6, aurora=None, broker_stats=None, sink=None, target=None):
    """Writes moving Aurora -> CDC -> Valkey.

    Two views, because one of them is only occasionally available:

    stages   - the rate at each hop. Always works, and it is the real load's
               writes, not ours. This is the reliable answer to "are writes
               flowing", because all four numbers move together or they do not.
    events   - the actual rows in transit, read off the stream. USUALLY EMPTY:
               the sink acks and deletes each entry as it applies it, so under a
               healthy pipeline there is nothing resident to read. Measured at
               ~3,200 writes/s the tail was empty on every sample. Kept because
               when the sink DOES fall behind, this is exactly what you want to
               see, and its emptiness is itself the signal that nothing is stuck.
    probes   - our own heartbeat writes with their measured end-to-end time, as
               the row-level proof that remains after the sink deletes entries.
    """
    now = now_ms()

    def read_stream(broker):
        # XREVRANGE reads without consuming, so this cannot take events away
        # from the sink.
        # Newest entries across every table's stream, so a multi-table link does
        # not show only whichever table happens to be listed first. Interleaved
        # by arrival, because "what is in flight right now" has no per-table
        # meaning to a reader watching one list.
        collected = []
        for spec in stream_specs():
            try:
                for entry_id, fields in broker.xrevrange(spec['stream'], '+', '-',
                                                         count=limit):
                    collected.append((entry_id, fields, spec['table']))
            except Exception:                             # noqa: BLE001
                continue                                  # one unreadable stream must not fail the whole list
        collected.sort(key=lambda item: stream_id_ms(item[0]) or 0, reverse=True)
        return collected[:limit]

    entries = soft(with_broker, read_stream)
    if isinstance(entries, dict) and entries.get('error'):
        return {'error': entries['error'], 'events': []}

    events = []
    for item in (entries or []):
        # Tolerates both shapes: (id, fields) from a single-stream fake and
        # (id, fields, table) from the multi-stream read above.
        entry_id, fields = item[0], item[1]
        table = item[2] if len(item) > 2 else CFG['table']
        parsed = parse_event(entry_id, fields)
        if parsed:
            parsed['table'] = table
            parsed['op_word'] = OP_WORDS.get(parsed.get('op'), parsed.get('op') or 'change')
            parsed['age_ms'] = (max(0, now - parsed['arrived_ms'])
                                if parsed.get('arrived_ms') else None)
            events.append(parsed)

    # Has the sink applied each one yet? Compare the event's values with the
    # cache. This is the part that makes it a flow and not just a log.
    ids = [e['id'] for e in events if e.get('id') is not None]
    if ids:
        def read_cache(target):
            found = {}
            for event in events:
                row_id = event.get('id')
                if row_id is None:
                    continue
                # Each table has its own key prefix, so a multi-table link cannot
                # look every id up under one table's namespace -- that would
                # report every other table's events as "in flight" indefinitely.
                key = '{}{}'.format(key_prefix_for(event.get('table')), row_id)
                hashed = target.hgetall(key)
                if hashed:
                    found[key] = hashed
            return found
        cached = soft(with_target, read_cache)
        cache_error = cached.pop('error', None) if isinstance(cached, dict) else None
        for event in events:
            hashed = (cached or {}).get('{}{}'.format(
                key_prefix_for(event.get('table')), event.get('id')))
            if event.get('op') == 'd':
                event['applied'] = not hashed
                event['state'] = 'applied' if not hashed else 'in flight'
                continue
            if not hashed:
                event['applied'] = False
                event['state'] = 'in flight'
                continue
            # Only the columns present on BOTH sides can be compared. If none
            # overlap, the key is in the cache but nothing was verified, and that
            # is reported as such rather than as a successful apply.
            checkable = [f for f in (event.get('event_fields') or [])
                         if f in hashed]
            for field in checkable:
                event['cache_' + field] = hashed.get(field)
            if not checkable:
                event['applied'] = None
                event['state'] = 'in cache, values not comparable'
                continue
            match = all(_norm(event.get('event_' + f)) == _norm(hashed.get(f))
                        for f in checkable)
            event['applied'] = match
            event['state'] = 'applied' if match else 'in flight'
    else:
        cache_error = None

    # The stage rates. Collected elsewhere already, so they are passed in when
    # the dashboard builds them all together and fetched only if called alone.
    aurora = aurora if aurora is not None else safe(collect_aurora)
    broker_stats = broker_stats if broker_stats is not None else safe(collect_broker)
    sink = sink if sink is not None else safe(collect_sink)
    target = target if target is not None else safe(collect_target)
    stages = [
        {'name': 'Aurora', 'detail': 'UPDATE committed',
         'rate': aurora.get('writes_per_sec'), 'unit': '/s'},
        {'name': 'Debezium', 'detail': 'read from binlog, put on the stream',
         'rate': broker_stats.get('produced_per_sec'), 'unit': '/s'},
        {'name': 'Sink', 'detail': 'consumed and applied',
         'rate': sink.get('applied_per_sec'), 'unit': '/s'},
        {'name': 'Valkey', 'detail': 'HSET applied',
         'rate': target.get('writes_per_sec'), 'unit': '/s'},
    ]
    rates = [s['rate'] for s in stages if s['rate'] is not None]
    # An idle system still performs a write or two a second from health
    # checks and the monitor's own probes. Calling that "flowing" would make the
    # indicator meaningless, so require a rate that reflects actual traffic.
    moving = bool(rates) and max(rates) >= CFG['writeflow_moving_per_sec']

    if events and not source_values_allowed():
        # Compared above, so `applied` still says whether each write arrived;
        # the values themselves are not sent (see source_values_allowed).
        events = [withhold_values(e) for e in events]
        values_withheld = True
    else:
        values_withheld = False
    out = {
        'stream': CFG['stream_key'],
        'stages': stages,
        'moving': moving,
        'events': events,
        'applied': sum(1 for e in events if e.get('applied')),
        'in_flight': sum(1 for e in events if e.get('applied') is False),
        'probes': probe_history(),
    }
    if values_withheld:
        out['values_withheld'] = True
        out['values_note'] = ('row values are not shown: this link\u2019s source '
                              'secret is not in MonitorSourceSecretArns')
    if cache_error:
        out['cache_error'] = cache_error
    if not events:
        # Empty is the normal healthy resting state: the sink deletes each entry
        # as it applies it. Do not let that read as "broken" or as "idle".
        out['events_note'] = (
            'Empty is healthy: the sink deletes each entry as it applies it, so '
            'the stream only holds rows when the sink is behind.')
    if not moving:
        out['note'] = 'no writes in the last few seconds'
    return out


@monitor_bp.route('/writeflow', methods=['GET'])
@per_link
def writeflow():
    return jsonify(safe(collect_writeflow))


# ---------------------------------------------------------------------------
# Bulk load progress, published by the loader itself
# ---------------------------------------------------------------------------
# A bulk load that rewrites keys that already exist changes no key count and no
# value, so from the outside it is invisible. The loader publishes its own
# progress to a hash on the target; this reads it back.
def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def collect_bulkload():
    # One hash per table of the link (bulkload_keys). A link loads its tables
    # one at a time, so the hash updated last is the load to show.
    keys = CFG.get('bulkload_keys') or [CFG['bulkload_key']]

    def read(target):
        return [(key, target.hgetall(key)) for key in keys]

    found = soft(with_target, read)
    if isinstance(found, dict) and found.get('error'):
        return {'active': False, 'error': found['error']}
    found = [(key, raw) for key, raw in (found or []) if raw]
    if not found:
        return {'active': False,
                'note': 'no bulk load has reported in; the loader publishes here '
                        'while it runs'}
    progress_key, raw = max(found, key=lambda kr: _as_int(kr[1].get('updated_at_ms')) or 0)

    def as_int(name):
        return _as_int(raw.get(name))

    updated = as_int('updated_at_ms')
    started = as_int('started_at_ms')
    now = now_ms()
    age_ms = (now - updated) if updated else None
    workers = []
    for key, value in raw.items():
        if not key.startswith('w') or not key.endswith('_rows'):
            continue
        idx = key[1:-5]
        try:
            rows = int(value)
        except (TypeError, ValueError):
            continue
        workers.append({
            'worker': int(idx) if idx.isdigit() else idx,
            'rows': rows,
            'last_pk': raw.get('w{}_last_pk'.format(idx)),
            'done': raw.get('w{}_done'.format(idx)) == '1',
        })
    workers.sort(key=lambda w: (str(w['worker'])))
    total = sum(w['rows'] for w in workers)
    elapsed_sec = ((updated or now) - started) / 1000.0 if started else None
    expected = as_int('expected_rows') or None

    out = {
        # Stale progress is a finished (or dead) run, not a live one.
        'active': age_ms is not None and age_ms < CFG['bulkload_active_ms'],
        'rows_loaded': total,
        'expected_rows': expected,
        'pct': round(100.0 * total / expected, 1) if expected else None,
        'workers': workers,
        'worker_count': as_int('workers') or len(workers),
        'workers_done': sum(1 for w in workers if w['done']),
        'elapsed_sec': round(elapsed_sec, 1) if elapsed_sec else None,
        'rows_per_sec': (round(total / elapsed_sec) if elapsed_sec and elapsed_sec > 0
                         else None),
        'age_ms': age_ms,
        'table': raw.get('table'),
        'pk_range': ('{}..{}'.format(raw.get('pk_lo'), raw.get('pk_hi'))
                     if raw.get('pk_lo') else None),
        'note': 'written straight to Valkey - this path does not touch the broker',
        'progress_key': progress_key,
    }
    if not out['active'] and age_ms is not None:
        out['finished_note'] = 'last run finished {}s ago'.format(int(age_ms / 1000))
    out.update(_bulkload_state(raw, age_ms))
    return out


def _bulkload_state(raw, age_ms):
    """Jira story F104: completed, running, or stalled. A run without completed_at_ms
    that has published nothing for bulkload_stall_ms is stalled: its task is
    stuck, or stopped without finishing (a failed load leaves no mark of its
    own in the hash)."""
    if raw.get('completed_at_ms'):
        return {'state': 'completed', 'completed': True, 'stalled': False}
    if age_ms is not None and age_ms >= CFG['bulkload_stall_ms']:
        return {'state': 'stalled', 'completed': False, 'stalled': True,
                'stalled_note': 'no progress for {} min and not completed: the load '
                                'is stuck, or stopped without finishing; check the '
                                'loader log'.format(int(age_ms / 60000))}
    return {'state': 'running', 'completed': False, 'stalled': False}


@monitor_bp.route('/bulkload', methods=['GET'])
@per_link
def bulkload():
    return jsonify(safe(collect_bulkload))


# ---------------------------------------------------------------------------
# Lag probe — the only writing code path in this module
# ---------------------------------------------------------------------------
# Why this exists: measured on the live pipeline, `applied_per_sec` tracks
# `produced_per_sec` almost exactly, so stream length sits at 0 even at 7,500
# events/s. Passive measurement needs an entry resident in the stream, which
# only happens when the sink is BEHIND. A metric that appears only during
# failure is useless as a headline, so lag is measured actively instead.
#
# It runs only for a link named in the request (or DEMO_PROBE_AUTO_LINK): it
# stamps one column of an existing row of that link's source table and puts it
# back (see _probe_reuse). There used to be a second path for the configured
# pipeline that INSERTed a heartbeat row with MAX(id)+1 into DEMO_TABLE, run by
# a POST that named no link; it was removed so no write can land on a pipeline
# the request did not name.
_probe_state = {}


# One probe at a time, in this process, whoever starts it ("Measure now" or the
# background sampler). The interval throttle below only spaced out the STARTS,
# and a probe can last probe_timeout_ms: a second probe that began while the
# first was still waiting read the first one's stamp as the "original" value
# and restored it last, leaving the stamp in the customer's row for good.
_probe_run_lock = threading.Lock()


def run_probe():
    """Stamp one column of a cached row of the watched link, time its arrival in
    the cache, and restore it. Refused when no link is watched."""
    if not CFG['probe_enabled']:
        # Two different reasons, and saying the wrong one leads someone to search
        # through env vars for a setting that is not the cause.
        watched = CFG.watched_link()
        if watched:
            return {
                'ok': False, 'refused': True, 'link': watched,
                'error': 'the probe writes a heartbeat row to the source table, '
                         'so it is not run against a deployed link',
                'note': 'CDC lag for {} is still reported from events in flight '
                        'when the sink is behind (flow.lag_source = in-flight)'
                        .format(watched),
            }
        return {'ok': False, 'error': 'probe disabled (DEMO_PROBE_ENABLED=false)'}
    if not CFG.watched_link():
        return {'ok': False, 'refused': True, 'error': PROBE_NEEDS_LINK}

    if not _probe_run_lock.acquire(blocking=False):
        return {'ok': False, 'throttled': True,
                'error': 'a probe is already running; retry when it finishes'}
    try:
        return _run_probe_locked()
    finally:
        _probe_run_lock.release()


def _run_probe_locked():
    now = time.monotonic()
    with _lock:
        last = _probe_state.get('last_run_monotonic')
        if last is not None and now - last < CFG['probe_min_interval_sec']:
            wait = round(CFG['probe_min_interval_sec'] - (now - last), 1)
            return {'ok': False, 'throttled': True,
                    'error': 'probe throttled, retry in {}s'.format(wait)}
        _probe_state['last_run_monotonic'] = now

    return _finish_probe(_probe_reuse())


def _finish_probe(result):
    """Record a probe result as the last measurement and into the short history."""
    with _lock:
        _probe_state['last_result'] = result
        _probe_state['last_result_wall'] = time.time()
        # Keep a short history. One measurement is a single data point; a series of them
        # is the row-level evidence that writes keep arriving, which cannot be
        # read off the stream because the sink deletes entries as it applies them.
        _probe_state.setdefault('history', collections.deque(maxlen=12)).appendleft({
            'at': time.time(),
            'lag_ms': result.get('lag_ms'),
            'row_id': result.get('row_id'),
            'value': result.get('written_value') or result.get('value'),
            'ok': bool(result.get('ok')),
            'note': result.get('note') or result.get('error'),
        })
    return result


# The probe overwrites one column of a customer row for a moment. Only a long
# text column can take the string nonce unchanged ('ecdlprobe-' + 9 digits is
# 19 characters): a number, date or short column would reject it, truncate it
# or coerce it, and the cache would never show the value being polled for.
_STAMP_TYPES = ('varchar', 'char', 'tinytext', 'text', 'mediumtext', 'longtext')
_STAMP_MIN_LENGTH = 32
_IDENT_IN_EXPR = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def _stamp_columns(cur, table):
    """Every column of `table` with what the stamp choice needs to know:
    [{'name', 'type', 'length', 'key', 'extra'}], in table order. Cached like
    table_shape, since this runs against the customer's database."""
    def produce():
        cur.execute(
            'SELECT column_name, data_type, character_maximum_length, column_key, '
            'extra FROM information_schema.columns '
            'WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position',
            schema_and_table(table))
        out = []
        for row in cur.fetchall():
            row = tuple(row) + (None,) * (5 - len(row))
            try:
                length = int(row[2]) if row[2] is not None else None
            except (TypeError, ValueError):
                length = None
            out.append({'name': str(row[0]), 'type': str(row[1] or '').lower(),
                        'length': length, 'key': str(row[3] or ''),
                        'extra': str(row[4] or '').lower()})
        return out
    return cached('stampcols:{}'.format(_source_scope(table)), 300, produce)


def _transform_used_columns(table):
    """Column names a transform of this link reads or writes for `table`.

    Stamping one of these would change more than the column: a filter column can
    move the row out of the cache (the sink DELETES a row that stops matching),
    a computed field or a parent embedded in a child record would change too,
    and a rename source or target means the field we poll is not this column.
    """
    used = set()
    transforms = CFG.get('transforms') or {}
    tspec = transforms.get(table) if isinstance(transforms, dict) else None
    tspec = tspec if isinstance(tspec, dict) else {}
    rename, _selected = transform_for(table)
    for source, output in (rename or {}).items():
        used.update((str(source), str(output)))
    filt = tspec.get('filter')
    if isinstance(filt, dict) and filt.get('field'):
        used.add(str(filt['field']))
    for comp in tspec.get('computed') or []:
        if isinstance(comp, dict):
            if comp.get('name'):
                used.add(str(comp['name']))
            used.update(_IDENT_IN_EXPR.findall(str(comp.get('expr') or '')))
    for den in tspec.get('denormalize') or []:
        if isinstance(den, dict) and den.get('join_field'):
            used.add(str(den['join_field']))
    # This table as the PARENT of another table's denormalize: its embedded
    # fields are copied into every child record.
    for other in (transforms.values() if isinstance(transforms, dict) else []):
        for den in (other.get('denormalize') or [] if isinstance(other, dict) else []):
            if isinstance(den, dict) and str(den.get('parent_table') or '') == table:
                used.update(str(f) for f in (den.get('fields') or []))
    return used


def _choose_stamp_column(columns, cached_hash, used=(), prefer=None):
    """The column the probe may stamp, and why every other one was refused.

    Returns (name or None, {name: reason}). A column qualifies only if it is
    varchar/char/text of at least _STAMP_MIN_LENGTH characters, is not part of
    the primary key, is not used by any filter or transform of this link, and
    is a field of the cached row under its own name (so the transform writes
    it back and we poll the right field). There is no fallback to another
    column type: when nothing qualifies, the probe is refused.
    """
    cached_hash = cached_hash or {}
    used = set(used or ())
    refused, ok = {}, []
    for col in columns or []:
        name = col['name']
        if col['key'] == 'PRI':
            refused[name] = 'part of the primary key'
        elif col['type'] not in _STAMP_TYPES:
            refused[name] = '{} is not a text column'.format(col['type'] or 'unknown type')
        elif col['length'] is None or col['length'] < _STAMP_MIN_LENGTH:
            refused[name] = '{}({}) is shorter than {} characters'.format(
                col['type'], col['length'], _STAMP_MIN_LENGTH)
        elif 'generated' in col.get('extra', ''):
            refused[name] = 'a generated column cannot be written'
        elif name in used:
            refused[name] = 'used by a filter or transform of this link'
        elif not sqlident.is_safe(name):
            refused[name] = 'not a plain identifier'
        elif name not in cached_hash:
            refused[name] = 'not in the cached row under its own name'
        else:
            ok.append(name)
    if prefer in ok:
        return prefer, refused
    return (ok[0] if ok else None), refused


def _on_update_columns(columns):
    """Columns the probe's UPDATE changes on its own (ON UPDATE CURRENT_TIMESTAMP)."""
    return [c['name'] for c in columns or [] if 'on update' in c.get('extra', '')]


# Said in every reuse-probe result, because it is not undone: the restore puts
# the stamped column back, but the two UPDATEs it takes are real writes.
PROBE_SIDE_EFFECTS = ('the probe UPDATE and its restore are real writes: '
                      'ON UPDATE timestamp columns and UPDATE triggers on the '
                      'table fire for both, and are not undone')


def _find_cached_row(cur, table, pk, target, prefer=None):
    """A row whose key is IN the cache -- so it passes the filter and can be polled.

    Tries a remembered choice first, then the lowest ids. Returns (id, hash), or
    (None, None) when nothing in the first window is cached (an empty table, or
    one every row of which the transform filters out).
    """
    candidates = []
    if prefer is not None:
        candidates.append(prefer)
    cur.execute(_sql('SELECT {pk} FROM {t} ORDER BY {pk} LIMIT 200', pk=pk, t=table))
    candidates += [row[0] for row in cur.fetchall()]
    seen = set()
    for rid in candidates:
        if rid in seen:
            continue
        seen.add(rid)
        try:
            hashed = target.hgetall('{}{}'.format(CFG['key_prefix'], rid))
        except Exception:                                 # noqa: BLE001
            _drop_target()
            raise
        if hashed:
            return rid, hashed
    return None, None


def _restore_probe_value(table, stamp, pk, row_id, original, nonce):
    """Put the stamped column back, only if it still holds this probe's stamp.

    Returns 'restored', 'kept' (the column no longer holds the stamp, so an
    application wrote the row meanwhile and its value is left in place), or
    'failed: <reason>'.

    The condition is the point. The restore used to write the old value back
    unconditionally, so an application update made while the probe waited for
    the cache was silently reverted. The restore is itself a change, so it flows
    through CDC and the cache ends holding the real value again.
    """
    try:
        conn = mysql_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                _sql('UPDATE {t} SET {col} = %s WHERE {pk} = %s AND {col} = %s',
                     t=table, col=stamp, pk=pk),
                (original, row_id, nonce))
            changed = cur.rowcount
        finally:
            conn.close()
    except Exception as exc:                              # noqa: BLE001
        # Not the value itself: it is customer data. The API result carries it
        # so whoever ran the probe can put it back.
        print('[monitor] ERROR probe restore failed: {} {}={!r} column {} still '
              'holds {!r}: {}'.format(table, pk, row_id, stamp, nonce, exc))
        return 'failed: {}'.format(exc)
    if changed == 1:
        return 'restored'
    print('[monitor] probe restore skipped: {} {}={!r} column {} no longer holds '
          'the stamp (written meanwhile); left as is'.format(table, pk, row_id, stamp))
    return 'kept'


def _probe_reuse():
    """CDC-lag probe for a deployed link, WITHOUT creating a row.

    Reuses an existing cached row, stamps one text column with a nonce, times how
    long that value takes to appear in the cache, then restores the original
    value. The least-invasive active probe: no INSERT, no table growth, one
    column briefly changed and then restored.
    """
    link = CFG.watched_link() or CFG['table']
    table = safe_identifier(CFG['table'], 'products')
    sql_table = safe_identifier(source_table(CFG['table']), 'products')
    base = {'link': link,
            'method': 'reuse an existing cached row: stamp one text column, time '
                      'its arrival in the cache, then restore the value (no row '
                      'created)'}
    # The target first: it is pooled, so a failure here leaves nothing open.
    # Opened after the database, a target error leaked the MySQL connection.
    target = get_target()
    try:
        conn = mysql_conn()
    except Exception as exc:                              # noqa: BLE001
        return dict(base, ok=False,
                    error='could not connect to the source: {}'.format(exc))

    with _lock:
        prefer = (_probe_state.get('reuse_row') or {}).get(link)

    try:
        cur = conn.cursor()
        shape = table_shape(cur, table)
        pk = safe_identifier(shape.get('pk') or 'id', 'id')
        row_id, cached = _find_cached_row(cur, sql_table, pk, target, prefer=prefer)
        if row_id is None:
            return dict(base, ok=False,
                        error='no cached row found to probe -- the table is empty '
                              'or every row is filtered out of the cache')
        columns = _stamp_columns(cur, table)
        stamp, refused = _choose_stamp_column(
            columns, cached, used=_transform_used_columns(CFG['table']),
            prefer=shape.get('label'))
        if not stamp:
            return dict(base, ok=False, refused=True, row_id=row_id,
                        refused_columns=refused,
                        error='no column is safe to stamp, so the probe was not run. '
                              'It needs a varchar, char or text column of at least '
                              '{} characters that is cached under its own name, is '
                              'not in the primary key and is not used by a filter or '
                              'transform of this link. Refused: {}'.format(
                                  _STAMP_MIN_LENGTH,
                                  '; '.join('{} ({})'.format(k, v)
                                            for k, v in refused.items()) or 'no columns'))
        # The column name came from the schema; validate it properly (this
        # used to be safe_identifier(stamp, stamp), which falls back to itself).
        stamp = sqlident.validate(stamp, 'column')
        cur.execute(_sql('SELECT {col} FROM {t} WHERE {pk} = %s',
                         col=stamp, t=sql_table, pk=pk), (row_id,))
        got = cur.fetchone()
        original = None if got is None else got[0]
        nonce = 'ecdlprobe-{}'.format(int(time.time() * 1000) % 1000000000)
        cur.execute(_sql('UPDATE {t} SET {col} = %s WHERE {pk} = %s',
                         t=sql_table, col=stamp, pk=pk), (nonce, row_id))
        t0 = time.time()
    finally:
        conn.close()

    FINE_INTERVAL, COARSE_INTERVAL, FINE_WINDOW = 0.002, 0.02, 0.5
    key = '{}{}'.format(CFG['key_prefix'], row_id)
    deadline = t0 + CFG['probe_timeout_ms'] / 1000.0
    seen_at = None
    polls = 0
    try:
        while time.time() < deadline:
            polls += 1
            value = target.hget(key, stamp)
            if isinstance(value, bytes):
                value = value.decode('utf-8', 'replace')
            if value is not None and str(value) == nonce:
                seen_at = time.time()
                break
            elapsed = time.time() - t0
            time.sleep(FINE_INTERVAL if elapsed < FINE_WINDOW else COARSE_INTERVAL)
    finally:
        # Always restore, even if polling raised, so a failed probe never leaves
        # the stamped nonce in the customer's row.
        restore = _restore_probe_value(sql_table, stamp, pk, row_id, original, nonce)

    with _lock:
        _probe_state.setdefault('reuse_row', {})[link] = row_id

    result = dict(base, row_id=row_id, key=key, stamp_field=stamp, polls=polls,
                  measured_at_ms=int(t0 * 1000), ok=seen_at is not None,
                  restore=restore, side_effects=PROBE_SIDE_EFFECTS)
    also_set = _on_update_columns(columns)
    if also_set:
        result['also_changed'] = also_set
    if restore.startswith('failed'):
        # Said loudly: the customer's row still holds the stamp, and this is the
        # only record of what it held before.
        result['source_row_left_changed'] = True
        result['original_value'] = original
        result['restore_note'] = (
            'RESTORE FAILED: {} {}={!r} column {} still holds {!r}. Set it back to '
            'original_value.'.format(table, pk, row_id, stamp, nonce))
    elif restore == 'kept':
        result['restore_note'] = ('the row was written by another client during the '
                                  'probe, so its new value was kept')
    if seen_at is None:
        result['timeout_ms'] = CFG['probe_timeout_ms']
        result['error'] = ('the stamped value had not reached the cache after '
                           '{} ms'.format(CFG['probe_timeout_ms']))
        result['note'] = ('a single miss is a slow hop, not data loss; ' +
                          ('the source row still holds the stamp, see restore_note'
                           if restore.startswith('failed') else
                           'the source row holds its own value again'))
    else:
        result['lag_ms'] = round((seen_at - t0) * 1000, 1)
        result['resolution_ms'] = FINE_INTERVAL * 1000
    return result


def probe_history():
    with _lock:
        return list(_probe_state.get('history') or [])


def last_probe(now=None):
    """The most recent probe result, with its age. Never writes."""
    with _lock:
        result = _probe_state.get('last_result')
        wall = _probe_state.get('last_result_wall')
    if not result:
        return {'ok': False,
                'note': 'no probe run yet (POST /api/monitor/probe to measure)'}
    out = dict(result)
    out['age_sec'] = round(max(0.0, (time.time() if now is None else now) - (wall or 0)), 1)
    return out


@monitor_bp.route('/probe', methods=['GET'])
@per_link
def probe_get():
    """Last probe result. Read-only — GET never writes."""
    return jsonify(last_probe())


@monitor_bp.route('/probe', methods=['POST'])
@per_link
@needs_explicit_link
def probe_post():
    """Run a probe now. The single writing endpoint in this blueprint.

    Scoped like everything else, which for a selected link means REFUSED: the
    overlay sets probe_enabled False because the probe stamps a row in the
    source table, and doing that to a customer's table because someone opened a
    dashboard is not acceptable. Unscoped it was worse than refusing -- it wrote to the
    default pipeline's database while the page showed another link's name.
    """
    return jsonify(safe(run_probe))


# ---------------------------------------------------------------------------
# Trend history — so a finished load phase can still be compared afterwards
# ---------------------------------------------------------------------------
# Sampled by a background thread, not by page views: the interesting comparison
# is "reads on Aurora" versus "reads on Valkey", and by the time you want to look
# at the first phase it is already over. Rates and latencies here are the same
# ones the cards show, kept as a compact series.
_history = collections.deque(maxlen=max(60, (CFG['history_minutes'] * 60)
                                        // max(1, CFG['history_interval_sec'])))
_history_lock = threading.Lock()
_marks = collections.deque(maxlen=50)


HISTORY_SERIES = (
    'aurora_cpu_pct', 'aurora_cpu_max_pct', 'aurora_capacity_acu',
    'aurora_read_ms', 'aurora_write_ms', 'aurora_commit_ms',
    'aurora_reads_per_sec', 'aurora_writes_per_sec', 'aurora_active_queries',
    # Row-level throughput. Statement rates go to ~0 during a bulk load even
    # while Aurora is reading tens of thousands of rows per second.
    'aurora_rows_read_per_sec', 'aurora_rows_written_per_sec',
    # Memory + connections, charted alongside read/write latency.
    'aurora_freeable_mb', 'aurora_connections', 'aurora_new_conn_per_sec',
    'valkey_engine_cpu_pct', 'valkey_engine_cpu_max_pct',
    'valkey_read_us', 'valkey_write_us',
    'valkey_ops_per_sec', 'valkey_reads_per_sec', 'valkey_writes_per_sec',
    'valkey_keys', 'valkey_memory_pct', 'valkey_connections',
    'valkey_new_conn_per_sec',
    'cdc_backlog', 'cdc_produced_per_sec', 'cdc_lag_ms',
    # Percentiles per store, measured in the app, so they can be read over time
    # rather than off a terminal after the run has finished.
    #
    # There is no valkey_write_* percentile set on purpose: the application never
    # writes to the cache. Writes go to Aurora and the sink applies them, so the
    # only Valkey writer is the sink, which publishes no latency histogram.
    # valkey_write_us above is the server-side AVERAGE from commandstats.
    'aurora_read_p50', 'aurora_read_p90', 'aurora_read_p99',
    'aurora_write_p50', 'aurora_write_p90', 'aurora_write_p99',
    'valkey_read_p50', 'valkey_read_p90', 'valkey_read_p99',
    # How many operations each percentile was computed from. Needed to average
    # percentiles across a run: an interval that served 12 reads must not carry
    # the same weight as one that served 300,000.
    'aurora_read_samples', 'aurora_write_samples', 'valkey_read_samples',
)


def history_sample(previous_sample_ms=None):
    """One compact datapoint. Never raises; missing pieces are recorded as None.

    previous_sample_ms is the wall clock of the preceding sample, used only to
    decide whether the monitor's own table scan fell inside this interval.
    """
    point = {'t': int(time.time())}
    for key in HISTORY_SERIES:
        point[key] = None

    # Collected in parallel, like the dashboard does. Run serially, one sample
    # costs the SUM of six collectors' round trips (MySQL, Valkey, two ECS
    # describes, CloudWatch) — measured at 26-38s apiece, so a "5s" history was
    # really recording a point every ~32s and the interval shown to the user was
    # wrong. In parallel a sample costs the slowest collector, not the total.
    from concurrent.futures import ThreadPoolExecutor
    jobs = {'aurora': collect_aurora, 'target': collect_target,
            'broker': collect_broker, 'sink': collect_sink,
            'debezium': collect_debezium}
    # The overlay is thread-local and pool threads do NOT inherit it, so it is
    # carried across explicitly (see current_overlay).
    overlay = current_overlay()
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(in_overlay(overlay, safe), fn)
                   for name, fn in jobs.items()}
        got = {name: future.result() for name, future in futures.items()}

    aurora = got['aurora']
    cw = aurora.get('cloudwatch') or {}
    point.update({
        'aurora_cpu_pct': cw.get('cpu_pct'),
        'aurora_cpu_max_pct': cw.get('cpu_max_pct'),
        'aurora_capacity_acu': cw.get('capacity_acu'),
        'aurora_read_ms': cw.get('select_latency_ms'),
        'aurora_write_ms': cw.get('dml_latency_ms'),
        'aurora_commit_ms': cw.get('commit_latency_ms'),
        'aurora_reads_per_sec': aurora.get('reads_per_sec'),
        'aurora_writes_per_sec': aurora.get('writes_per_sec'),
        'aurora_rows_read_per_sec': aurora.get('rows_read_per_sec'),
        'aurora_rows_written_per_sec': aurora.get('rows_written_per_sec'),
        # Memory + connections replace the read/write-latency trend charts.
        'aurora_freeable_mb': (round(cw['freeable_memory_bytes'] / 1048576.0, 1)
                               if cw.get('freeable_memory_bytes') is not None
                               else None),
        'aurora_connections': aurora.get('threads_connected'),
        'aurora_new_conn_per_sec': aurora.get('new_conn_per_sec'),
        # True when the monitor's own COUNT(*) scan fell inside this sample's
        # interval, which inflates rows_read by the whole table.
        'row_scan': bool(_last_row_scan_ms and previous_sample_ms
                         and _last_row_scan_ms >= previous_sample_ms),
        'aurora_active_queries': aurora.get('threads_running'),
    })

    target = got['target']
    tcw = target.get('cloudwatch') or {}
    point.update({
        'valkey_engine_cpu_pct': tcw.get('engine_cpu_pct'),
        'valkey_engine_cpu_max_pct': tcw.get('engine_cpu_max_pct'),
        'valkey_read_us': target.get('read_latency_us'),
        'valkey_write_us': target.get('write_latency_us'),
        'valkey_ops_per_sec': target.get('ops_per_sec'),
        'valkey_reads_per_sec': target.get('reads_per_sec'),
        'valkey_writes_per_sec': target.get('writes_per_sec'),
        'valkey_keys': target.get('keys'),
        'valkey_memory_pct': target.get('used_pct'),
        'valkey_connections': target.get('connected_clients'),
        'valkey_new_conn_per_sec': target.get('new_conn_per_sec'),
    })

    broker, sink = got['broker'], got['sink']
    point.update({
        # collect_broker already sums this across tables, and one formula in one
        # place is the point of backlog_of -- this was a third separately written copy.
        'cdc_backlog': broker.get('backlog', backlog_of(broker)),
        'cdc_produced_per_sec': broker.get('produced_per_sec'),
    })
    flow = build_flow(aurora, got['debezium'], broker, sink, target,
                      probe=last_probe())
    # Only record a lag that describes *now*. A probe result stays readable for
    # probe_stale_sec so the card is not blank, but recording a stale one every
    # 5s repeats a single old reading across the whole phase and turns a ~20ms
    # pipeline into a "5s average". In-flight is live, a fresh probe is live, a
    # stale probe is not a measurement of this moment.
    point['cdc_lag_ms'] = None if flow.get('lag_stale') else flow.get('cdc_lag_ms')

    # collect_target embeds the app metrics; the window is the gap between these
    # samples, which is exactly the history interval.
    app = target.get('demo_app') or {}
    for series in ('aurora_read', 'aurora_write', 'valkey_read'):
        for pct in (50, 90, 99):
            point['{}_p{}'.format(series, pct)] = app.get('{}_p{}'.format(series, pct))
        point['{}_samples'.format(series)] = app.get('{}_samples'.format(series))
    return point


# Persistence. An in-memory buffer is empty for the first two hours after any
# restart, and a restart is exactly what happens when the backend is redeployed
# mid-session — so "keep 120 minutes" only holds if the samples are on disk.
# One JSON object per line: appending is cheap, a truncated final line from an
# unexpected kill costs one sample, and it needs no schema migration when a series
# is added.
_history_appends = 0


def _history_write(point):
    """Append one sample. Never raises: monitoring must keep running when the disk is full."""
    path = CFG['history_path']
    if not path:
        return
    global _history_appends
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with open(path, 'a') as handle:
            handle.write(json.dumps(point) + '\n')
        _history_appends += 1
        # Rewrite occasionally so the file cannot grow without bound.
        if _history_appends % 240 == 0:
            _history_trim()
    except Exception as exc:                              # noqa: BLE001
        if _history_appends == 0:
            print('[monitor] history not persisted ({}): trends will reset on '
                  'restart'.format(exc))
            _history_appends = -1


def _history_trim():
    """Drop anything older than the retention window from the file.

    Filtered from the FILE, never from the in-memory buffer. Rewriting from
    memory means any process whose buffer is partial - a fresh worker, a debug
    reloader child, a temporary import - silently truncates the durable record
    to whatever it happens to hold. That is how a 180-minute retention ended up
    keeping 23 minutes, and it deletes finished runs as well.
    """
    path = CFG['history_path']
    cutoff = time.time() - CFG['history_minutes'] * 60
    try:
        keep = []
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    point = json.loads(line)
                except ValueError:
                    continue
                if isinstance(point, dict) and point.get('t', 0) >= cutoff:
                    keep.append(point)
        keep.sort(key=lambda p: p['t'])
        # Deduplicate by timestamp: two writers appending to one file is not
        # worth a lock, but it should not double-count either.
        deduped, seen = [], set()
        for point in keep:
            if point['t'] in seen:
                continue
            seen.add(point['t'])
            deduped.append(point)
        with open(path + '.tmp', 'w') as handle:
            for point in deduped:
                handle.write(json.dumps(point) + '\n')
        os.replace(path + '.tmp', path)
    except Exception:                                     # noqa: BLE001
        pass


def _history_restore():
    """Reload the retention window from disk into the buffer on startup."""
    path = CFG['history_path']
    if not path or not os.path.exists(path):
        return 0
    cutoff = time.time() - CFG['history_minutes'] * 60
    restored = []
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    point = json.loads(line)
                except ValueError:
                    continue          # truncated tail from an abrupt shutdown
                if isinstance(point, dict) and point.get('t', 0) >= cutoff:
                    restored.append(point)
    except Exception as exc:                              # noqa: BLE001
        print('[monitor] could not restore history: {}'.format(exc))
        return 0
    restored.sort(key=lambda p: p['t'])
    with _history_lock:
        _history.extend(restored[-_history.maxlen:])
    return len(restored)


def _history_loop():
    interval = max(2, CFG['history_interval_sec'])
    previous_ms = None
    # Sample the SAME pipeline the dashboard shows by default. The sampler runs on
    # a background thread with no request, so without this it has no overlay and
    # every collector reads the base DEMO_* endpoints. For a control plane whose
    # default is a deployed link, that meant the whole Target/Valkey trend was
    # null (the base cache endpoint is empty or points at a retired cluster),
    # while Aurora happened to populate because the base config points at it. Bind
    # a link's overlay so the trend follows the same pipeline the cards do.
    #
    # The link is chosen by _effective_default_link(): the configured default
    # when one is set, otherwise the newest deployed link discovered from
    # CloudFormation. The fallback is what makes a fresh install work -- a
    # control plane deployed with no DEMO_DEFAULT_LINK used to sample the static
    # placeholder endpoints indefinitely and the whole trend read 'no data'.
    #
    # Resolved lazily INSIDE the loop, not once up front, and retried every tick
    # until it binds: this thread can start before the module finishes importing
    # (link_config/discover_links are defined further down), so an eager call
    # raced the import and failed. Retrying also recovers from a transient AWS error
    # and picks up a link that is deployed after the control plane started.
    overlay = None
    while True:
        if overlay is None:
            # Guarded: on the first tick the function may not be defined yet
            # (the import race above), and an exception here used to stop the
            # sampler thread permanently, so the trend never recorded anything.
            try:
                name = _effective_default_link()
            except Exception:  # noqa: BLE001
                name = None
            if name:
                try:
                    overlay = link_config(name)
                    print('[monitor] history: sampling link {!r}'.format(name))
                except Exception:                         # noqa: BLE001
                    overlay = None                        # try again next tick
        # The opt-in self-probe (see auto_probe_tick). Guarded so a failure in
        # it can never stop the sampler thread.
        try:
            auto_probe_tick()
        except Exception as exc:                          # noqa: BLE001
            print('[monitor] auto-probe failed: {}'.format(exc))
        try:
            with watching(overlay):
                point = history_sample(previous_sample_ms=previous_ms)
            previous_ms = point['t'] * 1000
            with _history_lock:
                _history.append(point)
            _history_write(point)
        except Exception as exc:                          # noqa: BLE001
            print('[monitor] history sample failed: {}'.format(exc))
        time.sleep(interval)


# The background self-probe. It stamps a row in the customer's source table, so
# it is opt-in for one named link (DEMO_PROBE_AUTO_LINK) and never follows
# "the newest link". It re-resolves that link every tick, because a link can be
# deleted or have its credential revoked while the control plane keeps running,
# and stops for good when either happens: writing to a database we are no
# longer authorised for is the failure this exists to prevent. A restart is
# needed to start it again.
_auto_probe = {'last': None, 'stopped': None, 'logged': set()}


def _auto_probe_log(key, message):
    """Print `message` once per `key`, so a stopped or retrying probe does not
    repeat itself every tick."""
    if key not in _auto_probe['logged']:
        _auto_probe['logged'].add(key)
        print('[monitor] auto-probe: {}'.format(message))


def _auto_probe_stop(reason):
    _auto_probe['stopped'] = reason
    _auto_probe_log('stopped', 'stopped: {}'.format(reason))
    return 'stopped'


def _read_secret_fresh(secret_id):
    """Read a secret from Secrets Manager now, bypassing the cache.

    The fresh value replaces the cached one, so a rotation is picked up.
    """
    forget_secret(secret_id)
    return secret_value(secret_id)


# The credentials a probe uses. The probe always runs for a watched link, so
# the DEMO_*_PASSWORD variables never apply to it (see _env_password).
_PROBE_SECRETS = ('db_secret_id', 'cache_secret_id')


def auto_probe_tick(now=None):
    """Run the background probe if it is due. Returns what happened, for tests:
    'off', 'stopped', 'waiting', 'skipped' or 'probed'."""
    auto = CFG.get('probe_auto_sec') or 0
    link = (CFG.get('probe_auto_link') or '').strip()
    if auto <= 0 or not link:
        if auto > 0 or link:
            _auto_probe_log('half', 'off: set both DEMO_PROBE_AUTO_SEC (> 0) and '
                            'DEMO_PROBE_AUTO_LINK to turn it on')
        return 'off'
    if _auto_probe['stopped']:
        return 'stopped'
    now = time.monotonic() if now is None else now
    if _auto_probe['last'] is not None and now - _auto_probe['last'] < auto:
        return 'waiting'
    _auto_probe['last'] = now
    try:
        overlay = link_config(link)
    except ValueError as exc:
        text = str(exc)
        if 'no deployed link' in text or 'not a valid link name' in text:
            return _auto_probe_stop(text)
        # Any other failure to read the stack (throttling, a network error) is
        # not evidence the link is gone; try again next interval.
        _auto_probe_log('resolve:' + text, 'skipped, will retry: {}'.format(text))
        return 'skipped'
    with watching(overlay):
        if not CFG['probe_enabled']:
            _auto_probe_log('disabled', 'off: DEMO_PROBE_ENABLED=false')
            return 'off'
        for key in _PROBE_SECRETS:
            secret_id = CFG[key]
            if not secret_id:
                continue
            try:
                _read_secret_fresh(secret_id)
            except Exception as exc:                      # noqa: BLE001
                # Forget the cached copy too, so nothing else in this process
                # keeps using a credential that has been withdrawn.
                forget_secret(secret_id)
                return _auto_probe_stop('the {} secret for link {!r} cannot be read '
                                        '({})'.format(key, link, type(exc).__name__))
        safe(run_probe)
    return 'probed'


def start_history():
    if not CFG['history_enabled']:
        return
    restored = _history_restore()
    thread = threading.Thread(target=_history_loop, daemon=True,
                              name='monitor-history')
    thread.start()
    print('[monitor] trend history: every {}s, keeping {} min{}'.format(
        CFG['history_interval_sec'], CFG['history_minutes'],
        ', restored {} samples from disk'.format(restored) if restored else ''))


def downsample(points, max_points):
    """Average consecutive samples into at most max_points buckets.

    Two hours at a five-second interval is 1,440 samples per series; sending all
    of them every poll is wasteful and draws more detail than a chart a few
    hundred pixels wide can show. Averaging keeps the shape while cutting the
    payload. None is preserved: a bucket with no readings stays None instead of
    becoming a zero, so "this store served nothing" cannot be mistaken for
    "this store was fast".
    """
    if not points or max_points <= 0 or len(points) <= max_points:
        return points
    size = math.ceil(len(points) / max_points)
    out = []
    for start in range(0, len(points), size):
        bucket = points[start:start + size]
        merged = {'t': bucket[len(bucket) // 2]['t'], 'n': len(bucket)}
        for key in HISTORY_SERIES:
            values = [p[key] for p in bucket
                      if p.get(key) is not None]
            merged[key] = round(sum(values) / len(values), 3) if values else None
        out.append(merged)
    return out


@monitor_bp.route('/history', methods=['GET'])
@per_link
def history():
    """Trend series for the last N minutes (default 30).

    `marks` are operator-placed labels (POST /api/monitor/mark) so a phase can be
    named on the chart instead of guessed at from the shape.
    """
    def arg_int(name, default):
        try:
            return int(request.args.get(name, default))
        except (TypeError, ValueError):
            return default

    minutes = max(1, min(arg_int('minutes', 30), CFG['history_minutes']))
    max_points = max(0, arg_int('max_points', 360))
    cutoff = time.time() - minutes * 60
    with _history_lock:
        points = [p for p in _history if p['t'] >= cutoff]
        marks = [m for m in _marks if m['t'] >= cutoff]
    raw = len(points)
    points = downsample(points, max_points)
    return jsonify({
        'minutes': minutes,
        'interval_sec': CFG['history_interval_sec'],
        'retention_minutes': CFG['history_minutes'],
        'series': list(HISTORY_SERIES),
        'count': len(points),
        'samples': raw,
        'downsampled': raw != len(points),
        'points': points,
        'marks': marks,
        'persisted': bool(CFG['history_path']) and _history_appends >= 0,
        # WHOSE history this is. The sampler is a single background thread with no
        # request and therefore no overlay, so it samples the pipeline this control
        # plane was configured with -- always, regardless of what the page is
        # watching. Returning these points under a chosen link's name without
        # saying so would put one pipeline's trends in another's dashboard, which is
        # the exact confusion the link selector was added to remove.
        #
        # Said rather than silently fixed: sampling every deployed link in the
        # background is a real change in what this process does to a customer's
        # infrastructure, and is not something to start doing because someone
        # opened a page.
        'scope': 'default',
        'scope_link': None,
        'watching': CFG.watched_link(),
        'scope_note': (
            'these trends are for the pipeline this control plane was configured '
            'with, not for the link selected above. Background sampling runs '
            'outside any request, so it follows the configured pipeline only.'
            if CFG.watched_link() else None),
        'note': 'sampled server-side in the background and written to disk, so '
                'it survives page reloads and backend restarts and covers '
                'phases that have already finished',
    })


# /reconcile lists at most this many other key groups, and tracks at most
# RECONCILE_FAMILY_TRACK while scanning (the rest are counted as one group).
RECONCILE_FAMILY_MAX = 20
RECONCILE_FAMILY_TRACK = 1000
RECONCILE_OTHER = '(other groups)'


@monitor_bp.route('/reconcile', methods=['POST'])
@per_link
def reconcile():
    """Definitive row-count vs key-count, counted on the same basis.

    Aurora rows are counted exactly; cache keys are counted by walking every key
    and grouping by prefix. The comparison people actually want is rows against
    the `products:` family alone — total DBSIZE includes unrelated key families
    and will always look larger.

    On demand only: the scan visits every key (~18s for 6.5M) and that is cache
    CPU you do not want to spend inside a 2-second poll loop.
    """
    started = time.time()

    def count_rows():
        conn = mysql_conn()
        try:
            with conn.cursor() as cur:
                return _count_rows(cur, safe_identifier(source_table(CFG['table']),
                                                        'products'))
        finally:
            conn.close()

    prefix_family = CFG['key_prefix'] if CFG['key_prefix'].endswith(':') \
        else CFG['key_prefix'] + ':'

    def scan_families(target):
        # The table's keys are counted by their FULL prefix. Grouping only by the
        # first segment could never match a link with a KeyPrefix
        # ('shop:products:' falls in family 'shop:'), so such a link always
        # reported 0 keys. Everything else is grouped by first segment, with a
        # bounded number of groups: keys with unique first segments made one
        # group per key and returned the whole keyspace.
        families = collections.Counter()
        scanned = 0
        for key in target.scan_iter(count=5000):
            scanned += 1
            text = key.decode() if isinstance(key, bytes) else key
            if text.startswith(prefix_family):
                family = prefix_family
            else:
                family = text.split(':', 1)[0] + ':' if ':' in text else '(no prefix)'
                if family not in families and len(families) >= RECONCILE_FAMILY_TRACK:
                    family = RECONCILE_OTHER
            families[family] += 1
        return families, scanned

    # NOT safe(): that helper wraps a collector's *dict* result, so handing it a
    # function returning an int makes it fail and report no rows at all.
    rows, row_error = None, None
    try:
        rows = count_rows()
    except Exception as exc:                                  # noqa: BLE001
        row_error = '{}: {}'.format(type(exc).__name__, exc)

    scan = soft(with_target, scan_families)
    if isinstance(scan, dict) and scan.get('error'):
        return jsonify({'ok': False, 'aurora_rows': rows,
                        'error': scan['error'], 'row_error': row_error})

    families, scanned = scan
    product_keys = families.get(prefix_family, 0)
    other = sorted(((name, count) for name, count in families.items()
                    if name != prefix_family), key=lambda kv: (-kv[1], kv[0]))
    shown = other[:RECONCILE_FAMILY_MAX]
    extra = {}
    if len(other) > len(shown) or RECONCILE_OTHER in families:
        extra['other_families_truncated'] = True
        extra['other_families_note'] = (
            'the {} largest other key groups are listed; the rest are counted in '
            'other_total'.format(len(shown)))
    return jsonify(dict(extra, **{
        'ok': True,
        'aurora_rows': rows,
        'product_keys': product_keys,
        'matched': rows is not None and rows == product_keys,
        'difference': None if rows is None else product_keys - rows,
        'row_error': row_error,
        'total_keys': scanned,
        'other_families': dict(shown),
        'other_total': scanned - product_keys,
        'scan_sec': round(time.time() - started, 1),
        'note': 'matched compares Aurora rows against {}* keys only. Other '
                'families are unrelated data in the same cache and are listed '
                'separately.'.format(prefix_family),
    }))


def add_mark(label):
    """Label the current moment on the trend charts. Shared by the HTTP route and
    by the demo controller, so a step names its own phase automatically."""
    entry = {'t': int(time.time()), 'label': str(label)[:60]}
    with _history_lock:
        _marks.append(entry)
    return entry


@monitor_bp.route('/mark', methods=['POST'])
def mark():
    """Label the current moment on the trend charts (e.g. 'step 1: reads on DB')."""
    label = (request.args.get('label')
             or (request.get_json(silent=True) or {}).get('label')
             or 'mark')
    return jsonify({'ok': True, 'mark': add_mark(label)})


start_history()


# ---------------------------------------------------------------------------
# Demo runs — three named phases, driven from the page
# ---------------------------------------------------------------------------
# The load generator lives on a different host that this backend cannot reach on
# any port, and the reverse is also blocked. Rather than open ports or give this
# container permission to execute shell commands on another instance, jobs go
# through a list on the broker, which both sides already talk to. The agent on
# the loadgen host pops jobs and reports back into the same keyspace.
#
# Each run stores its own window, so "compare phase 1 with phase 3" becomes a
# table of finished runs rather than an exercise in reading a chart.
JOB_QUEUE = 'ecdl:monitor:jobs'
RUN_KEY = 'ecdl:monitor:run:{}'
RUN_INDEX = 'ecdl:monitor:runs'
CANCEL_KEY = 'ecdl:monitor:cancel'
RUN_INDEX_MAX = 40

# What each button does. Each step is one phase of the initial-load handoff
# that the demo harness on the loadgen host runs:
#   1 -> the DB-only load
#   2 -> the parallel bulk snapshot, straight into Valkey, no broker
#   3 -> the same load as step 1, with reads served from the cache
# The pipeline stays up throughout. Nothing here stops or reconfigures Debezium,
# the broker or the sink: writes replicate as they always do, so step 3 reads
# current data with no cutover to perform.
RUN_PHASES = {
    # 'cdc' says whether cache freshness is part of what this phase demonstrates.
    # It gates the CDC lag readout in the UI. Writes replicate through CDC in
    # every phase, but in phases 1 and 2 nothing is being served from the cache,
    # so a lag figure is not relevant to the phase - and a healthy sink
    # drains to empty, so it renders as an empty panel with a prompt to run a probe.
    '1': {
        'steps': '1',
        'title': 'DB load only',
        'detail': 'Reads and writes both go to Aurora. The cache is not in the '
                  'request path at all.',
        'watch': 'aurora',
        'cdc': False,
    },
    '2': {
        'steps': '2',
        'title': 'Bulk insert into cache',
        'detail': 'Reads Aurora directly and writes Valkey directly. Bypasses '
                  'Debezium and the broker entirely - no CDC involved.',
        'watch': 'valkey',
        'cdc': False,
    },
    '3': {
        'steps': '3',
        'title': 'DB + cache',
        'detail': 'The same workload as phase 1, with reads served from Valkey '
                  'and writes still going to Aurora - so the two are directly '
                  'comparable.',
        'watch': 'both',
        # The app reads the cache here, so freshness matters in this phase.
        'cdc': True,
    },
}

# Phases that generate application traffic. The others are pipeline operations,
# and asking them for read percentiles produces empty columns, not insight.
for _phase in RUN_PHASES.values():
    _phase.setdefault('load', True)

# Aggregates worth keeping per run. avg over the window, plus peak where a peak
# is the interesting part (CPU) and delta where growth is (keys).
SUMMARY_SPEC = (
    # avg from the Average statistic, peak from the Maximum one. Taking the peak
    # from averaged samples hid every short burst.
    ('aurora_cpu_pct', 'avg'), ('aurora_cpu_max_pct', 'max'),
    ('aurora_capacity_acu', 'avg'),
    ('aurora_reads_per_sec', 'avg'), ('aurora_writes_per_sec', 'avg'),
    # Row throughput, kept alongside the statement rates because they differ
    # greatly. The bulk load reads 5.7M rows from Aurora using 8 SELECTs: the
    # statement rate rounds to zero while rows/s is in the tens of thousands.
    # Showing only statements made the bulk-load phase look like Aurora was idle.
    ('aurora_rows_read_per_sec', 'avg'), ('aurora_rows_read_per_sec', 'max'),
    ('aurora_rows_written_per_sec', 'avg'),
    ('aurora_active_queries', 'avg'),
    # Percentile averages are WEIGHTED by the operation count behind each
    # sample. Unweighted, a ramp-up interval serving a handful of slow reads
    # counts as much as a steady-state interval serving hundreds of thousands,
    # which pushes the reported p50 well above what the load generator saw.
    ('aurora_read_p50', 'avg', 'aurora_read_samples'),
    ('aurora_read_p90', 'avg', 'aurora_read_samples'),
    ('aurora_read_p99', 'max'),
    ('aurora_write_p50', 'avg', 'aurora_write_samples'),
    ('aurora_write_p90', 'avg', 'aurora_write_samples'),
    ('aurora_write_p99', 'max'),
    ('valkey_engine_cpu_pct', 'avg'), ('valkey_engine_cpu_max_pct', 'max'),
    # Split, not just the total. In the DB-only phase the application reads
    # nothing from the cache, yet total ops/s sits in the thousands because the
    # sink is applying the Aurora write stream. Reporting only the total reads as
    # "the cache was busy serving traffic", which is the wrong conclusion, so the
    # read and write rates are carried separately and the total is derived.
    ('valkey_reads_per_sec', 'avg'), ('valkey_writes_per_sec', 'avg'),
    ('valkey_ops_per_sec', 'avg'), ('valkey_memory_pct', 'max'),
    ('valkey_read_p50', 'avg', 'valkey_read_samples'),
    ('valkey_read_p90', 'avg', 'valkey_read_samples'),
    ('valkey_read_p99', 'max'),
    ('valkey_write_us', 'avg'),
    ('valkey_keys', 'delta'),
    ('cdc_lag_ms', 'avg'), ('cdc_lag_ms', 'max'), ('cdc_backlog', 'max'),
)


def summarise_window(start_ms, end_ms):
    """Aggregate the history samples inside a run's window.

    Skips the first two samples: rates need a previous sample to exist and CPU
    has not responded yet, so including them lowers every average.
    """
    lo, hi = start_ms / 1000.0, (end_ms or now_ms()) / 1000.0
    with _history_lock:
        points = [p for p in _history if lo <= p['t'] <= hi]
    if len(points) > 4:
        points = points[2:]
    window_sec = int(hi - lo)
    out = {'samples': len(points), 'window_sec': window_sec}
    if not points:
        return out
    # Samples that absorbed the monitor's own COUNT(*) scan report the whole
    # table as rows read. That is the monitor measuring itself, so those samples
    # are dropped from the row-throughput fields only — everything else in them
    # is still valid.
    clean_rows = [p for p in points if not p.get('row_scan')]
    ROW_FIELDS = ('aurora_rows_read_per_sec', 'aurora_rows_written_per_sec')

    for spec in SUMMARY_SPEC:
        field, how = spec[0], spec[1]
        weight_field = spec[2] if len(spec) > 2 else None
        source = clean_rows if field in ROW_FIELDS else points
        pairs = [(p[field], p.get(weight_field) or 0 if weight_field else 1)
                 for p in source if p.get(field) is not None]
        if not pairs:
            continue
        values = [v for v, _ in pairs]
        # Coverage. A metric that is only measurable some of the time (CDC lag is
        # the main one) would otherwise report an average over an unrepresentative
        # subset with nothing on screen to say so.
        out['{}_n'.format(field)] = len(values)
        name = '{}_{}'.format(field, how)
        if how == 'avg':
            total_weight = sum(w for _, w in pairs)
            if weight_field and total_weight > 0:
                out[name] = round(
                    sum(v * w for v, w in pairs) / total_weight, 3)
                out['{}_ops'.format(field)] = int(total_weight)
            else:
                out[name] = round(sum(values) / len(values), 3)
        elif how == 'max':
            out[name] = round(max(values), 3)
        elif how == 'delta':
            out[name] = round(values[-1] - values[0], 3)

    # CDC lag is only observable while the sink is behind: when it has drained,
    # there is nothing queued to measure. So the samples that DO carry a value
    # are the moments the pipeline was behind, and averaging only those reports
    # the worst moments as if they were typical. Say that outright rather than
    # letting a number stand that the backlog figure contradicts.
    # Aurora CPU is a percentage OF the currently provisioned capacity, and
    # Serverless v2 scales down over many minutes. So a phase that follows a
    # heavy one inherits capacity it does not need, and its real work shows up as
    # a couple of percent. Caption it with the capacity it was measured against,
    # and with what the same reading is worth at the lowest capacity this monitor
    # has actually observed — an observed baseline, not an assumed floor.
    capacity = out.get('aurora_capacity_acu_avg')
    cpu_avg = out.get('aurora_cpu_pct_avg')
    if capacity and cpu_avg is not None:
        with _history_lock:
            seen = [p['aurora_capacity_acu'] for p in _history
                    if p.get('aurora_capacity_acu')]
        baseline = min(seen) if seen else None
        note = ('CPU is a share of provisioned capacity, which averaged '
                '{:.0f} ACU during this phase'.format(capacity))
        if baseline and capacity >= baseline * 1.5:
            note += ('. Capacity had not scaled back down yet, so the same work '
                     'at the lowest capacity seen ({:.0f} ACU) would read about '
                     '{:.0f}%'.format(baseline, cpu_avg * capacity / baseline))
        out['aurora_cpu_note'] = note + '.'

    lag_n = out.get('cdc_lag_ms_n', 0)
    if lag_n and lag_n < len(points) * 0.5:
        out['cdc_lag_note'] = (
            'measured in only {} of {} samples: lag is observable while events '
            'are still queued, so these are the moments the pipeline was behind, '
            'not an average over the run'.format(lag_n, len(points)))
    return out


def _run_read(client, run_id):
    raw = client.hgetall(RUN_KEY.format(run_id))
    if not raw:
        return None
    out = {}
    for key, value in raw.items():
        key = key.decode() if isinstance(key, bytes) else key
        value = value.decode() if isinstance(value, bytes) else value
        out[key] = value
    for field in ('started_ms', 'ended_ms', 'exit_code', 'conc', 'dur', 'write_pct'):
        if out.get(field) not in (None, '', 'None'):
            try:
                out[field] = int(out[field])
            except ValueError:
                pass
    return out


@monitor_bp.route('/phases', methods=['GET'])
def phases():
    """What the buttons are, so the page does not hardcode the wording."""
    return jsonify({'phases': [dict(id=key, **value)
                               for key, value in sorted(RUN_PHASES.items())]})


@monitor_bp.route('/run', methods=['POST'])
@per_link
@needs_explicit_link
def run_phase():
    """Queue one phase for the agent on the loadgen host, on the named link's
    broker. Refused without a named link: it writes a job to a broker."""
    body = request.get_json(silent=True) or {}
    phase = str(body.get('phase') or request.args.get('phase') or '')
    if phase not in RUN_PHASES:
        return jsonify({'ok': False,
                        'error': 'phase must be one of {}'.format(
                            sorted(RUN_PHASES))}), 400
    spec = RUN_PHASES[phase]

    def bounded(name, default, low, high):
        try:
            value = int(body.get(name, request.args.get(name, default)))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    # Bounded on purpose: this endpoint starts real load on shared infrastructure.
    conc = bounded('conc', 1000, 1, 4000)
    dur = bounded('dur', 240, 10, 1800)
    write_pct = bounded('write_pct', 5, 0, 100)

    run_id = '{}-{}'.format(int(time.time()), phase)
    job = {'run_id': run_id, 'phase': phase, 'steps': spec['steps'],
           'conc': conc, 'dur': dur, 'write_pct': write_pct}
    # 'load' travels with the run so the summary knows whether to expect request
    # latency at all. A catch-up phase generates no traffic, and reporting empty
    # percentiles for it reads as a broken collector rather than as "not
    # applicable".
    record = dict(job, title=spec['title'], watch=spec['watch'],
                  load='1' if spec.get('load', True) else '0',
                  status='queued', queued_ms=now_ms())

    def push(client):
        client.hset(RUN_KEY.format(run_id),
                    mapping={k: str(v) for k, v in record.items()})
        client.expire(RUN_KEY.format(run_id), 7 * 24 * 3600)
        client.lpush(RUN_INDEX, run_id)
        client.ltrim(RUN_INDEX, 0, RUN_INDEX_MAX - 1)
        client.delete(CANCEL_KEY)
        client.lpush(JOB_QUEUE, json.dumps(job))
        client.expire(JOB_QUEUE, 3600)
        return client.llen(JOB_QUEUE)

    try:
        queued = with_broker(push)
    except Exception as exc:                                  # noqa: BLE001
        return jsonify({'ok': False,
                        'error': 'could not queue on the broker: {}'.format(exc)}), 502
    return jsonify({'ok': True, 'run': record, 'queue_depth': queued,
                    'note': 'the agent on the loadgen host picks this up; if '
                            'nothing starts, the agent is not running'})


@monitor_bp.route('/run/cancel', methods=['POST'])
@per_link
@needs_explicit_link
def cancel_run():
    """Ask the agent to stop the current run. It kills its child process.
    Writes to the named link's broker, so it needs a named link too."""
    body = request.get_json(silent=True) or {}
    run_id = body.get('run_id') or request.args.get('run_id') or 'current'

    def do(client):
        client.setex(CANCEL_KEY, 300, str(run_id))
        # Drop anything still queued so a cancel does not just start the next one.
        client.delete(JOB_QUEUE)
        return True

    try:
        with_broker(do)
    except Exception as exc:                                  # noqa: BLE001
        return jsonify({'ok': False, 'error': str(exc)}), 502
    return jsonify({'ok': True, 'cancelling': run_id})


# Commentary that used to be generated and has since been dropped. Summaries
# frozen during earlier sessions still carry it, so it has to be stripped on read
# or retired text keeps appearing under finished phases indefinitely.
RETIRED_NOTE_KEYS = ('cpu_note',)
RETIRED_NOTE_PHRASES = ('kept the stream drained', 'has not caught up',
                        'served no application reads')


def strip_retired_notes(summary):
    """Drop retired commentary from a summary loaded out of storage."""
    if not isinstance(summary, dict):
        return summary
    for key in RETIRED_NOTE_KEYS:
        summary.pop(key, None)
    note = summary.get('cdc_lag_note')
    if note and any(phrase in note for phrase in RETIRED_NOTE_PHRASES):
        summary.pop('cdc_lag_note', None)
    return summary


# Summaries of finished runs, frozen in this process the first time they are
# computed. They used to be written back to the broker by GET /runs -- a GET
# that wrote, every five seconds, to whichever broker was configured.
_frozen_summaries = {}


@monitor_bp.route('/runs', methods=['GET'])
@per_link
def runs():
    """Finished and in-flight runs, each with the metrics for its own window.
    Read-only: GET never writes."""
    try:
        limit = max(1, min(int(request.args.get('limit', 12)), RUN_INDEX_MAX))
    except (TypeError, ValueError):
        limit = 12

    def read(client):
        ids = client.lrange(RUN_INDEX, 0, limit - 1)
        agent_seen = client.get('ecdl:monitor:agent')
        depth = client.llen(JOB_QUEUE)
        out = []
        for raw in ids:
            run_id = raw.decode() if isinstance(raw, bytes) else raw
            record = _run_read(client, run_id)
            if record:
                out.append(record)
        return out, agent_seen, depth

    try:
        records, agent_seen, depth = with_broker(read)
    except Exception as exc:                                  # noqa: BLE001
        return jsonify({'runs': [], 'error': str(exc)}), 502

    if isinstance(agent_seen, bytes):
        agent_seen = agent_seen.decode()
    # A finished run's numbers are frozen into the run record the first time they
    # can be computed. Recomputing them on every read means they disappear as
    # soon as the history window moves past the run - which is why phases from an
    # hour ago were all reading "samples 0". A run is a historical fact; once
    # measured, its values should not change.
    for record in records:
        if not record.get('started_ms'):
            continue
        stored = record.get('summary_json')
        if stored:
            try:
                record['summary'] = strip_retired_notes(json.loads(stored))
                record.pop('summary_json', None)
                continue
            except ValueError:
                pass                      # corrupt, fall through and recompute
        record.pop('summary_json', None)
        frozen_key = (CFG.watched_link(), record.get('run_id'))
        with _lock:
            summary = _frozen_summaries.get(frozen_key)
        if summary is None:
            summary = summarise_window(record['started_ms'], record.get('ended_ms'))
            # Only freeze once the run is over and there is something to freeze;
            # an in-flight run should keep updating.
            if (record.get('status') in ('done', 'failed', 'cancelled')
                    and summary.get('samples') and record.get('run_id')):
                with _lock:
                    _frozen_summaries[frozen_key] = summary
        record['summary'] = summary
    agent_age = None
    if agent_seen:
        try:
            agent_age = int(time.time() - float(agent_seen))
        except (TypeError, ValueError):
            agent_age = None
    return jsonify({
        'runs': records,
        'queue_depth': depth,
        'agent_last_seen_sec': agent_age,
        # Without this the page cannot tell "nothing queued" from "no agent is
        # listening", which are very different problems mid-demo.
        'agent_ok': agent_age is not None and agent_age < 30,
    })


# ---------------------------------------------------------------------------
# Which links can be watched
# ---------------------------------------------------------------------------
#
# Derived from CloudFormation rather than remembered. A deployment record is held
# in memory and is lost on restart, and the control plane may not be the thing
# that deployed the link at all -- `ecdl apply` from a laptop produces the same
# stacks. The stacks are the durable record, so they are what gets read.
#
# A link is monitorable when its `-cdc` stack exists. That stack's parameters name
# the streams, the target and the services; its `-broker` sibling's outputs name
# the broker and its credential. Between them there is enough to watch a pipeline
# without anyone typing an endpoint.

_LINK_CACHE = {}
_LINK_CACHE_TTL = 30


def _cfn():
    # `aws()` is this module's client factory; it applies the configured region.
    return aws('cloudformation')


def discover_links():
    """Every link with a deployed CDC stack, newest first."""
    cached = _LINK_CACHE.get('links')
    if cached and time.time() - cached[0] < _LINK_CACHE_TTL:
        return cached[1]

    found = []
    try:
        marker = None
        while True:
            params = {'StackStatusFilter': [
                'CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE',
                'IMPORT_COMPLETE']}
            if marker:
                params['NextToken'] = marker
            page = _cfn().list_stacks(**params)
            for summary in page.get('StackSummaries') or []:
                name = summary.get('StackName') or ''
                if name.startswith('ecdl-') and name.endswith('-cdc'):
                    found.append({
                        'stack': name,
                        # ecdl-<link>-cdc -> <link>
                        'link': name[len('ecdl-'):-len('-cdc')],
                        'updated': str(summary.get('LastUpdatedTime')
                                       or summary.get('CreationTime') or ''),
                    })
            marker = page.get('NextToken')
            if not marker:
                break
    except Exception as exc:                              # noqa: BLE001
        return [{'link': None, 'error': 'could not list stacks: {}'.format(exc)}]

    found.sort(key=lambda item: item['updated'], reverse=True)
    _LINK_CACHE['links'] = (time.time(), found)
    return found


def _effective_default_link():
    """The link name the default (unselected) view and trend sampler follow.

    Prefers the explicitly configured default link. When none is configured --
    the state of a freshly installed control plane -- it falls back to the
    newest deployed link discovered from CloudFormation, so the trend binds to
    the pipeline that is actually there instead of the static DEMO_* placeholder
    endpoints (which read as 'no data'). Returns None only when nothing is
    configured and nothing is deployed.

    discover_links() is cached for a short while and never raises, so calling
    this each sampler tick is cheap and self-healing.
    """
    configured = (CFG.get('default_link') or '').strip()
    if configured:
        return configured
    deployed = [item['link'] for item in discover_links() if item.get('link')]
    return deployed[0] if deployed else None             # newest first


def _stack_parameters(stack):
    described = _cfn().describe_stacks(StackName=stack)['Stacks'][0]
    return {p['ParameterKey']: p['ParameterValue']
            for p in described.get('Parameters') or []}


def _stack_outputs(stack):
    described = _cfn().describe_stacks(StackName=stack)['Stacks'][0]
    return {o['OutputKey']: o['OutputValue']
            for o in described.get('Outputs') or []}


# Same as "name" in ecdl_link/link.schema.json.
_LINK_NAME = re.compile(r'[a-z](?:[a-z0-9-]{0,26}[a-z0-9])?\Z')


class LinkUnavailable(ValueError):
    """A link's stacks could not be read just now (throttling, a network error).

    A ValueError, so callers that refuse an unknown link still refuse this, but a
    separate class: "not deployed" is a 404, while this is a 503 that the next
    poll may not hit. Answering it as "not deployed" made the page drop the
    selection and show the configured default pipeline instead.
    """


def _missing_stack(exc):
    return 'does not exist' in str(exc)


def link_config(link_name):
    """A CFG overlay that points every collector at one deployed link.

    Raises ValueError when the link has no CDC stack, so a mistyped name is
    answered plainly rather than by quietly monitoring the default pipeline --
    which is the failure this whole mechanism exists to remove -- and
    LinkUnavailable when the stacks could not be read.

    Cached for _LINK_CACHE_TTL seconds: building it costs three DescribeStacks
    calls, and every poll of every open page asked for it, which is how the
    account's CloudFormation rate limit was reached. Only a complete overlay is
    cached; each caller gets its own copy.
    """
    # The name arrives from ?link= and is spliced into stack, log-group and key
    # names below. Validate it against the link schema's own pattern before any of that.
    if not isinstance(link_name, str) or not _LINK_NAME.match(link_name):
        raise ValueError('{!r} is not a valid link name (lower-case letters, '
                         'digits and hyphens, up to 28 characters)'.format(link_name))
    key = ('overlay', link_name)
    hit = _LINK_CACHE.get(key)
    if hit and time.time() - hit[0] < _LINK_CACHE_TTL:
        return copy.deepcopy(hit[1])
    overlay = _build_link_config(link_name)
    _LINK_CACHE[key] = (time.time(), overlay)
    return copy.deepcopy(overlay)


def _build_link_config(link_name):
    cdc_stack = 'ecdl-{}-cdc'.format(link_name)
    try:
        params = _stack_parameters(cdc_stack)
    except Exception as exc:                              # noqa: BLE001
        if _missing_stack(exc):
            raise ValueError(
                'no deployed link called {!r} (looked for stack {})'.format(
                    link_name, cdc_stack))
        raise LinkUnavailable('could not read {}: {}'.format(cdc_stack, exc))

    overlay = {'link_name': link_name}

    def carry(param, key, cast=None):
        value = params.get(param)
        if value in (None, ''):
            return
        overlay[key] = cast(value) if cast else value

    def truthy(value):
        return str(value).lower() in ('1', 'true', 'yes', 'on', 'enabled')

    carry('SourceDBHost', 'db_host')
    carry('SourceDBPort', 'db_port', int)
    carry('SourceDBUser', 'db_user')
    carry('SourceDBName', 'db_name')
    # Credentials, or the live panels connect to the link's endpoints holding the
    # default pipeline's password. That happens to work when two links share a
    # source and fails confusingly when they do not.
    carry('DBSecretArn', 'db_secret_id')
    carry('TargetSecretArn', 'cache_secret_id')
    carry('BrokerHost', 'broker_host')
    carry('BrokerPort', 'broker_port', int)
    carry('BrokerSSL', 'broker_tls', truthy)
    carry('TargetHost', 'cache_host')
    carry('TargetPort', 'cache_port', int)
    carry('TargetSSL', 'cache_tls', truthy)
    carry('TargetCluster', 'cache_cluster_mode', truthy)
    carry('Streams', 'streams')
    carry('OffsetKey', 'offset_key')
    carry('SchemaHistoryKey', 'schema_history_key')
    carry('ClusterName', 'ecs_cluster')

    # The first table, for the collectors that still describe a single one.
    tables = [t.strip() for t in (params.get('Tables') or '').split(',') if t.strip()]
    # bare name -> the name as the link gives it ('Inventory.products'), so the
    # source is queried in the table's own schema (see source_table).
    overlay['source_tables'] = {t.split('.')[-1]: t for t in tables if '.' in t}
    if tables:
        # 'Inventory.products' -> 'products'; the key prefix follows the sink,
        # which names keys after the table as the source reports it.
        bare = tables[0].split('.')[-1]
        overlay['table'] = bare
        link_prefix = (params.get('KeyPrefix') or '').strip().rstrip(':')
        overlay['link_key_prefix'] = link_prefix
        overlay['key_prefix'] = ('{}:'.format(link_prefix) if link_prefix else '') + bare + ':'

        # The monitored table's transform, so the data-flow panel compares each
        # Aurora column against the cache field it is actually written to. A rename
        # sends a value to a different field; a `columns` list drops the rest.
        # Keyed by bare table name in TransformsJson.
        try:
            tspec = (json.loads(params.get('TransformsJson') or '{}')
                     .get('tables', {}).get(bare, {})) or {}
        except Exception:                                # noqa: BLE001
            tspec = {}
        rename = tspec.get('rename')
        overlay['transform_rename'] = ({str(k): str(v) for k, v in rename.items()}
                                       if isinstance(rename, dict) else {})
        cols = tspec.get('columns')
        if isinstance(cols, list) and cols:
            selected = [str(c) for c in cols]
            computed = tspec.get('computed')
            if isinstance(computed, list):
                selected += [str(c.get('name')) for c in computed
                             if isinstance(c, dict) and c.get('name')]
            overlay['transform_columns'] = selected
        else:
            overlay['transform_columns'] = None          # no restriction: all cached

        # The full per-table map, so the data-flow panel compares EVERY table in
        # the link (not just the first) against its own rules.
        try:
            all_tspecs = (json.loads(params.get('TransformsJson') or '{}')
                          .get('tables', {})) or {}
        except Exception:                                # noqa: BLE001
            all_tspecs = {}
        overlay['transforms'] = all_tspecs if isinstance(all_tspecs, dict) else {}

    # Streams is authoritative for the stream key when present.
    if overlay.get('streams'):
        first = overlay['streams'].split(',')[0].split('=')[0].strip()
        if first:
            overlay['stream_key'] = first

    # Service names follow the stack, not the parameters.
    overlay['debezium_service'] = '{}-cdc-debezium'.format(
        overlay.get('ecs_cluster') or 'ecdl-{}'.format(link_name))
    overlay['sink_service'] = '{}-cdc-sink'.format(
        overlay.get('ecs_cluster') or 'ecdl-{}'.format(link_name))

    # The broker's credential lives in its own stack's outputs. Without it the
    # broker collector cannot authenticate, and a hardened broker refuses
    # unauthenticated reads -- which would look like a broker that is down.
    try:
        broker_outputs = _stack_outputs('ecdl-{}-broker'.format(link_name))
        if broker_outputs.get('AuthSecretArn'):
            overlay['broker_secret_id'] = broker_outputs['AuthSecretArn']
        if broker_outputs.get('PrimaryEndpoint'):
            overlay['broker_host'] = broker_outputs['PrimaryEndpoint']
        if broker_outputs.get('TransitEncryption'):
            overlay['broker_tls'] = truthy(broker_outputs['TransitEncryption'])
    except Exception as exc:                              # noqa: BLE001
        # An initial_load-only link has no broker at all, which is not an error.
        # Any other failure is: without the broker's credential every broker
        # read fails NOAUTH and looks like a broker that is down.
        if not _missing_stack(exc):
            raise LinkUnavailable('could not read ecdl-{}-broker: {}'.format(
                link_name, exc))

    # ── Log groups ──────────────────────────────────────────────────────────
    # Named after the STACK, not the ECS cluster. The two differ by a suffix --
    # cluster_name() is 'ecdl-<link>' while the CDC stack is 'ecdl-<link>-cdc' --
    # and infra/ecdl-cdc-ecs.yaml declares '/ecs/${AWS::StackName}/debezium'. This
    # was previously derived from the cluster, so every per-link lookup asked for
    # '/ecs/ecdl-<link>/debezium', a group that does not exist. filter_log_events
    # raises ResourceNotFound, soft() ignores it, and the card renders a dash --
    # i.e. "no errors" and "I cannot find your logs" looked identical. The module
    # default was right ('/ecs/ecdl-demo-cdc/...', stack ecdl-demo-cdc), which is
    # why this bug went unnoticed: it only ever broke for a *selected* link.
    overlay['debezium_log_group'] = '/ecs/{}/debezium'.format(cdc_stack)
    overlay['sink_log_group'] = '/ecs/{}/sink'.format(cdc_stack)

    # The loader is a one-off task in its own stack, and its group carries NO
    # sub-path: infra/ecdl-loader-ecs.yaml uses '/ecs/${AWS::StackName}' bare.
    # Prefer the stack's own LogGroup output when the stack is there, so a
    # renamed group is still found; fall back to the documented shape for a
    # cdc-only link, whose loader stack never existed.
    loader_stack = 'ecdl-{}-loader'.format(link_name)
    overlay['loader_log_group'] = '/ecs/{}'.format(loader_stack)
    try:
        loader_outputs = _stack_outputs(loader_stack)
        if loader_outputs.get('LogGroup'):
            overlay['loader_log_group'] = loader_outputs['LogGroup']
    except Exception as exc:                              # noqa: BLE001
        # No loader stack: a 'cdc' mode link never had one. The derived name is
        # kept so the UI can say "no loader has run" rather than nothing at all.
        if not _missing_stack(exc):
            raise LinkUnavailable('could not read {}: {}'.format(loader_stack, exc))

    # ── CloudWatch identifiers ──────────────────────────────────────────────
    # These are separate from the endpoints, and forgetting them is silent: the
    # Aurora and Valkey panels kept reporting the DEFAULT clusters' CPU, capacity
    # and memory while every other panel followed the chosen link. Two pipelines
    # sharing a source hid it completely.
    #
    # Derived from the endpoints because CloudFormation does not carry the ids:
    #   shop.cluster-abc.us-east-2.rds.amazonaws.com          -> shop
    #   clustercfg.my-cache.abc.use2.cache.amazonaws.com      -> my-cache
    #   master.my-broker.abc.ng.0001.use2.cache.amazonaws.com -> my-broker
    if overlay.get('db_host'):
        overlay['rds_cluster_id'] = overlay['db_host'].split('.')[0]
    if overlay.get('cache_host'):
        labels = overlay['cache_host'].split('.')
        # Drop the endpoint-kind prefix ElastiCache puts in front of the group id.
        group = labels[1] if labels[0] in ('clustercfg', 'master', 'replica') \
            else labels[0]
        overlay['cache_replication_group'] = group
        # Node-level metrics (EngineCPUUtilization, memory) are per cache cluster,
        # and the first node of the first shard is the primary the sink writes to.
        overlay['cache_cluster_id'] = '{}-0001-001'.format(group)

    # Per link and table, the hashes the loader publishes to (provision.
    # progress_key), so one link's or table's bulk-load progress is not read
    # as another's.
    load_prefix = (params.get('KeyPrefix') or '').strip().rstrip(':')
    overlay['bulkload_keys'] = [
        progress_key(link_name, t, load_prefix)
        for t in (params.get('Tables') or '').split(',') if t.strip()]
    overlay['bulkload_key'] = (overlay['bulkload_keys'][0] if overlay['bulkload_keys']
                               else progress_key_base(link_name))
    overlay['dlq_prefix'] = CFG['dlq_prefix']

    # Offsets and schema history: taken from the stack when it names them, and
    # otherwise derived the same way provision.py does. A fallback to the module
    # default would read the DEFAULT pipeline's Debezium offset and report it as
    # this link's capture position -- which is more misleading than reporting nothing,
    # because an offset is how "is capture keeping up" gets answered.
    overlay.setdefault('offset_key', 'ecdl:{}:offsets'.format(link_name))
    overlay.setdefault('schema_history_key',
                       'ecdl:{}:schema_history'.format(link_name))

    # ── things that belong to this HOST, not to a link ──────────────────────
    # The app-latency panel scrapes a demo application that has nothing to do with
    # any particular link, so leaving it on would show one pipeline's read
    # latencies inside another's dashboard.
    overlay['demo_app_url'] = ''
    overlay['debezium_metrics_url'] = ''
    overlay['sink_metrics_url'] = ''

    # The active CDC-lag probe IS enabled for a deployed link. It does not create
    # a row in the customer's table (the original objection): for a per-link
    # config run_probe() dispatches to the reuse-and-restore path, which stamps
    # one text column of an existing cached row with a nonce, times its arrival,
    # and restores the value. On-demand, throttled, and switchable off with
    # DEMO_PROBE_ENABLED=false.
    overlay['probe_enabled'] = _env_bool('DEMO_PROBE_ENABLED', True)

    return overlay


def _requested_overlay():
    """The overlay for the requested link, or None to use the configured default.

    Reads the query string first and then a JSON body, because the actions are
    POSTs. Accepting only ?link= meant /probe and /reconcile ignored the page's
    selection entirely and acted on the DEFAULT pipeline -- and since the probe
    writes a heartbeat row to the source table, that wrote to a different
    customer's database than the one on screen.
    """
    name = (request.args.get('link') or '').strip()
    if not name and request.method in ('POST', 'PUT', 'PATCH'):
        # silent: a POST with no body at all is normal for these actions, and
        # get_json() would raise on it.
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            name = str(body.get('link') or '').strip()
    if not name:
        # No explicit selection: use the configured default link, if one is
        # set, through the same link_config() path, so the default view is a
        # real deployed pipeline rather than the static env endpoints.
        #
        # A failure is reported, not papered over. It used to fall back to the
        # env-configured pipeline, so one throttled DescribeStacks call showed
        # a different pipeline's numbers under the default view.
        default_link = (CFG.get('default_link') or '').strip()
        if default_link:
            try:
                # Marked, so a write cannot treat it as the request's choice.
                return dict(link_config(default_link), link_implicit=True)
            except LinkUnavailable:
                raise
            except ValueError as exc:
                raise ValueError('the configured default link (DEMO_DEFAULT_LINK) '
                                 'cannot be shown: {}'.format(exc))
        return None
    return link_config(name)


def configured_default_exists():
    """Whether the pipeline named in the configuration is still there.

    The configured default describes ONE pipeline, set from environment when this
    control plane was installed. It can remain after the thing it names is deleted -- the demo
    pipeline it pointed at was deleted while its stacks' names stayed in the
    config -- and then the page opens on a pipeline whose every component is
    unreachable. That looks like a broken product rather than a stale default, so
    the answer has to be published and not assumed.

    Checked via the ECS services, because that is cheap, needs no credential and
    fails fast; the broker and target would each need a connection.
    """
    try:
        resp = aws('ecs').describe_services(
            cluster=CFG['ecs_cluster'],
            services=[CFG['debezium_service'], CFG['sink_service']])
        live = [s for s in resp.get('services') or []
                if s.get('status') == 'ACTIVE']
        return bool(live)
    except Exception:                                     # noqa: BLE001
        return False


@monitor_bp.route('/links', methods=['GET'])
def monitor_links():
    """Which pipelines this page can watch, and which one it should show first."""
    links = discover_links()
    problems = [item['error'] for item in links if item.get('error')]
    deployed = [item for item in links if item.get('link')]

    default_alive = configured_default_exists()
    default = {
        'link': None,
        'stream': CFG['stream_key'],
        'sink_service': CFG['sink_service'],
        'available': default_alive,
        'note': 'the pipeline this control plane was configured with, before '
                'any link was selected',
    }
    if not default_alive:
        default['note'] = ('the pipeline this control plane was configured with '
                           'no longer exists — its services are gone, so every '
                           'component would read as unreachable')

    # What the page should open on. A deployed link is preferred over a configured default
    # that no longer exists, because the alternative is a page of unreachable components
    # next to a dropdown the newcomer has no reason to touch.
    prefer = None
    if deployed and not default_alive:
        prefer = deployed[0]['link']          # discover_links sorts newest first

    return jsonify({
        'links': deployed,
        'default': default,
        'prefer': prefer,
        'problems': problems,
    })
