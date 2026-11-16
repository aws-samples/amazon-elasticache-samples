#!/usr/bin/env python3
"""Unit tests for the pipeline monitor collectors (backend/monitor.py).

Pure-logic only: no Aurora, no Valkey, no AWS. Anything that needs a live
component is exercised by calling /api/monitor/health on the deployed host.

Run: python3 test_monitor.py   (exit 0 = all pass)
"""
import collections
import decimal
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, _BACKEND)                   # backend/  (import monitor)
sys.path.insert(0, os.path.dirname(_BACKEND))  # repo root (backend modules import ecdl_link)

import monitor  # noqa: E402

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append('{}: got {!r}, want {!r}'.format(name, got, want))


def check_true(name, cond):
    if not cond:
        FAILURES.append('{}: expected true'.format(name))


_CLIENT = []


def monitor_client():
    """Flask test client for the routes that need a request context.

    Built once and reused: a blueprint can only be registered on an app once.
    """
    if not _CLIENT:
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(monitor.monitor_bp)
        _CLIENT.append(app.test_client())
    return _CLIENT[0]


# --- safe(): collectors must never raise ------------------------------------
def test_safe_catches():
    def boom():
        raise ValueError('broker unreachable')

    out = monitor.safe(boom)
    check('safe.ok', out['ok'], False)
    check('safe.error', out['error'], 'ValueError: broker unreachable')
    check_true('safe.collect_ms', 'collect_ms' in out)


def test_safe_passes_through():
    out = monitor.safe(lambda: {'keys': 5})
    check('safe.value', out['keys'], 5)
    check('safe.default_ok', out['ok'], True)


def test_safe_respects_explicit_not_ok():
    out = monitor.safe(lambda: {'ok': False, 'error': 'not configured'})
    check('safe.explicit_ok', out['ok'], False)


# --- rate(): per-second rates from monotonic counters -----------------------
def test_rate_first_sample_is_none():
    monitor.reset_state()
    check('rate.first', monitor.rate('t.first', 100, now=10.0), None)


def test_rate_computes_per_second():
    monitor.reset_state()
    monitor.rate('t.ok', 1000, now=100.0)
    check('rate.value', monitor.rate('t.ok', 1600, now=102.0), 300.0)


def test_rate_counter_reset_returns_none():
    """A restarted component resets its counters; report nothing, not a negative."""
    monitor.reset_state()
    monitor.rate('t.reset', 5000, now=100.0)
    check('rate.reset', monitor.rate('t.reset', 12, now=101.0), None)


def test_rate_zero_elapsed_returns_none():
    monitor.reset_state()
    monitor.rate('t.zero', 100, now=100.0)
    check('rate.zero_elapsed', monitor.rate('t.zero', 200, now=100.0), None)


def test_rate_none_value_returns_none():
    monitor.reset_state()
    check('rate.none', monitor.rate('t.none', None, now=1.0), None)


def test_rate_tracks_series_independently():
    monitor.reset_state()
    monitor.rate('a', 0, now=0.0)
    monitor.rate('b', 0, now=0.0)
    check('rate.series_a', monitor.rate('a', 10, now=1.0), 10.0)
    check('rate.series_b', monitor.rate('b', 40, now=1.0), 40.0)


# --- cached(): CloudWatch memoisation ---------------------------------------
def test_cached_reuses_within_ttl():
    monitor.reset_state()
    calls = []

    def produce():
        calls.append(1)
        return len(calls)

    check('cached.first', monitor.cached('k', 60, produce), 1)
    check('cached.second', monitor.cached('k', 60, produce), 1)
    check('cached.call_count', len(calls), 1)


def test_cached_expires():
    monitor.reset_state()
    calls = []

    def produce():
        calls.append(1)
        return len(calls)

    monitor.cached('k', 0, produce)
    monitor.cached('k', 0, produce)
    check('cached.expired_calls', len(calls), 2)


# --- helpers ----------------------------------------------------------------
def test_sum_values_handles_cluster_and_single():
    check('sum.cluster', monitor._sum_values({'n1': 10, 'n2': '20'}), 30)
    check('sum.single', monitor._sum_values(7), 7)
    check('sum.none', monitor._sum_values(None), 0)


def test_unconfigured_optional_probe_is_ok_not_error():
    out = monitor.safe(monitor._probe_http, '', 'DEMO_APP_URL')
    check('probe.ok', out['ok'], True)
    check('probe.configured', out['configured'], False)


def test_config_defaults_match_repo():
    """These names are what the deployed pipeline actually uses."""
    check('cfg.stream', monitor.CFG['stream_key'], 'data:ecdl.Inventory.products')
    check('cfg.group', monitor.CFG['consumer_group'], 'ecdl-sink')
    check('cfg.offset', monitor.CFG['offset_key'], 'ecdl:metadata:offsets')
    check('cfg.region', monitor.CFG['region'], 'us-east-2')
    check('cfg.debezium_svc', monitor.CFG['debezium_service'], 'ecdl-demo-cdc-debezium')
    check('cfg.sink_svc', monitor.CFG['sink_service'], 'ecdl-demo-cdc-sink')


def test_health_payload_never_leaks_password():
    """The /health config echo must not expose credentials.

    /health serves `{k: v for k, v in CFG.items() ...}`, so any credential placed
    in CFG is published to whoever can reach the page. Credentials are resolved by
    db_password() / broker_password() / cache_password() instead.
    """
    blob = repr(monitor.CFG)
    check_true('cfg.no_password_key', 'db_password' not in monitor.CFG)
    check_true('cfg.no_pw_value', 'DEMO_DB_PASSWORD' not in blob)
    for key in ('broker_password', 'cache_password', 'auth_token'):
        check_true('cfg.no_{}'.format(key), key not in monitor.CFG)


def test_no_cfg_key_looks_like_a_credential():
    """A guard for whatever gets added next, not just today's keys."""
    suspicious = [k for k in monitor.CFG
                  if ('password' in k or 'token' in k or 'secret' in k)
                  and not k.endswith('_secret_id')]
    check('cfg.credential_keys', suspicious, [])


def test_credential_helpers_prefer_env_and_return_empty_when_unset():
    saved = {k: os.environ.get(k) for k in
             ('DEMO_BROKER_PASSWORD', 'DEMO_CACHE_PASSWORD')}
    try:
        os.environ['DEMO_BROKER_PASSWORD'] = 'brk-token'  # nosec B105 - fake test credential
        os.environ['DEMO_CACHE_PASSWORD'] = 'cache-token'  # nosec B105 - fake test credential
        check('cred.broker_env', monitor.broker_password(), 'brk-token')
        check('cred.cache_env', monitor.cache_password(), 'cache-token')
        os.environ.pop('DEMO_BROKER_PASSWORD')
        os.environ.pop('DEMO_CACHE_PASSWORD')
        # No env and no secret id configured -> empty, not an exception, so an
        # unauthenticated broker keeps working.
        check('cred.broker_unset', monitor.broker_password(), '')
        check('cred.cache_unset', monitor.cache_password(), '')
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --- target topology --------------------------------------------------------
# A replication group is cluster-mode enabled or disabled, and the two need
# different clients. target_client() was hardcoded to the cluster client, so the
# monitor could only see a sharded target.

def _target_client_with(cache_password='', **overrides):  # nosec B107 - fake test credential
    """Build a target client with CFG temporarily overridden, capturing the class.

    The password comes from cache_password(), not CFG, so it is injected through
    the environment the same way the real deployment does.
    """
    captured = {}

    class FakeCluster:
        def __init__(self, **kwargs):
            captured['kind'] = 'cluster'
            captured['kwargs'] = kwargs

    class FakeSingle:
        def __init__(self, **kwargs):
            captured['kind'] = 'single'
            captured['kwargs'] = kwargs

    saved_cfg = {k: monitor.CFG[k] for k in overrides}
    saved_cluster, saved_vk, saved_lib = (
        monitor._VkCluster, monitor._vk, monitor._VK_LIB)
    saved_pw = os.environ.get('DEMO_CACHE_PASSWORD')
    fake_vk = type('vk', (), {'Valkey': FakeSingle})
    try:
        monitor.CFG.update(overrides)
        if cache_password:
            os.environ['DEMO_CACHE_PASSWORD'] = cache_password
        else:
            os.environ.pop('DEMO_CACHE_PASSWORD', None)
        monitor._VkCluster = FakeCluster
        monitor._vk = fake_vk
        monitor._VK_LIB = 'valkey'
        monitor.target_client()
    finally:
        monitor.CFG.update(saved_cfg)
        monitor._VkCluster, monitor._vk, monitor._VK_LIB = (
            saved_cluster, saved_vk, saved_lib)
        if saved_pw is None:
            os.environ.pop('DEMO_CACHE_PASSWORD', None)
        else:
            os.environ['DEMO_CACHE_PASSWORD'] = saved_pw
    return captured


def test_target_client_uses_cluster_client_when_cluster_mode():
    got = _target_client_with(cache_cluster_mode=True)
    check('target.cluster_kind', got['kind'], 'cluster')


def test_target_client_uses_plain_client_when_not_cluster_mode():
    got = _target_client_with(cache_cluster_mode=False)
    check('target.single_kind', got['kind'], 'single')


def test_target_client_defaults_to_cluster_mode():
    """Every target this has been pointed at so far is cluster-mode enabled."""
    check('cfg.cluster_mode_default', monitor.CFG['cache_cluster_mode'], True)


def test_target_client_omits_auth_when_none_configured():
    got = _target_client_with(cache_cluster_mode=True, cache_username='')
    check_true('target.no_password', 'password' not in got['kwargs'])
    check_true('target.no_username', 'username' not in got['kwargs'])


def test_target_client_sends_password_only_for_an_auth_token():
    got = _target_client_with(cache_cluster_mode=True, cache_username='',
                              cache_password='tok')  # nosec B106 - fake test credential
    check('target.token_password', got['kwargs']['password'], 'tok')
    check_true('target.token_no_username', 'username' not in got['kwargs'])


def test_target_client_sends_both_for_rbac():
    got = _target_client_with(cache_cluster_mode=False, cache_username='u',
                              cache_password='p')  # nosec B106 - fake test credential
    check('target.rbac_user', got['kwargs']['username'], 'u')
    check('target.rbac_pass', got['kwargs']['password'], 'p')


def test_target_client_ignores_a_username_with_no_password():
    got = _target_client_with(cache_cluster_mode=True, cache_username='u')
    check_true('target.lone_username_dropped', 'username' not in got['kwargs'])


def test_target_client_auth_works_on_both_topologies():
    for clustered, kind in ((True, 'cluster'), (False, 'single')):
        got = _target_client_with(cache_cluster_mode=clustered,
                                  cache_username='u', cache_password='p')  # nosec B106 - fake test credential
        check('target.matrix_kind_{}'.format(kind), got['kind'], kind)
        check('target.matrix_user_{}'.format(kind),
              got['kwargs']['username'], 'u')


# --- Aurora collector -------------------------------------------------------
class FakeCursor:
    """Minimal DB-API cursor. `plan` maps an SQL prefix to rows, or to an
    Exception instance to simulate an unsupported statement."""

    def __init__(self, plan):
        self.plan = plan
        self.executed = []
        self._rows = []

    def execute(self, sql):
        self.executed.append(sql)
        for prefix, result in self.plan.items():
            if sql.upper().startswith(prefix.upper()):
                if isinstance(result, Exception):
                    raise result
                self._rows = list(result)
                return
        self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_global_status_coerces_ints_and_keeps_strings():
    cur = FakeCursor({'SHOW GLOBAL STATUS': [
        ('Com_select', '12345'), ('Threads_running', '7'),
        ('Innodb_buffer_pool_dump_status', 'not started'),
    ]})
    status = monitor.global_status(cur)
    check('status.int', status['Com_select'], 12345)
    check('status.gauge', status['Threads_running'], 7)
    check('status.str', status['Innodb_buffer_pool_dump_status'], 'not started')


def test_sum_vars_adds_write_counters():
    status = {'Com_insert': 10, 'Com_update': 5, 'Com_delete': 1}
    check('sum_vars.total', monitor.sum_vars(status, monitor.WRITE_VARS), 16)


def test_sum_vars_none_when_no_counter_present():
    check('sum_vars.absent', monitor.sum_vars({}, monitor.WRITE_VARS), None)


def test_sum_vars_ignores_non_numeric():
    status = {'Com_insert': 4, 'Com_update': 'n/a'}
    check('sum_vars.mixed', monitor.sum_vars(status, monitor.WRITE_VARS), 4)


def test_binlog_status_prefers_show_master_status():
    cur = FakeCursor({'SHOW MASTER STATUS': [('mysql-bin-changelog.001482', 69341552)]})
    out = monitor.binlog_status(cur)
    check('binlog.file', out['binlog_file'], 'mysql-bin-changelog.001482')
    check('binlog.pos', out['binlog_pos'], 69341552)


def test_binlog_status_falls_back_on_newer_mysql():
    """MySQL 8.4 renamed the statement; the collector must not stop."""
    cur = FakeCursor({
        'SHOW MASTER STATUS': RuntimeError('You have an error in your SQL syntax'),
        'SHOW BINARY LOG STATUS': [('mysql-bin-changelog.002000', 12)],
    })
    out = monitor.binlog_status(cur)
    check('binlog.fallback_file', out['binlog_file'], 'mysql-bin-changelog.002000')
    check('binlog.fallback_pos', out['binlog_pos'], 12)


def test_binlog_status_missing_privilege_is_reported_not_raised():
    cur = FakeCursor({
        'SHOW MASTER STATUS': RuntimeError('Access denied'),
        'SHOW BINARY LOG STATUS': RuntimeError('Access denied'),
    })
    out = monitor.binlog_status(cur)
    check('binlog.none', out['binlog_pos'], None)
    check_true('binlog.note', 'REPLICATION CLIENT' in out['binlog_note'])


# --- CloudWatch helpers -----------------------------------------------------
# --- read/write latency ------------------------------------------------------
def test_delta_latency_uses_the_interval_not_the_lifetime_average():
    """commandstats usec_per_call is a lifetime average — after 124M calls it is
    frozen and says nothing about latency now. The delta is the live figure."""
    monitor.reset_state()
    # lifetime average would be 373952204/124261903 = 3.01 us
    check('lat.first', monitor.delta_latency('t', 124261903, 373952204), None)
    # next poll: 100 calls took 2,000 us -> 20 us/call right now
    check('lat.delta', monitor.delta_latency('t', 124262003, 373954204), 20.0)


def test_delta_latency_none_on_idle_interval():
    monitor.reset_state()
    monitor.delta_latency('idle', 100, 1000)
    check('lat.idle', monitor.delta_latency('idle', 100, 1000), None)


def test_delta_latency_none_on_counter_reset():
    monitor.reset_state()
    monitor.delta_latency('reset', 5000, 50000)
    check('lat.reset', monitor.delta_latency('reset', 10, 20), None)


def test_command_latency_groups_reads_and_writes():
    monitor.reset_state()
    before = {
        'cmdstat_hgetall': {'calls': 1000, 'usec': 3000},
        'cmdstat_hget': {'calls': 500, 'usec': 1000},
        'cmdstat_hset': {'calls': 2000, 'usec': 4000},
    }
    monitor.command_latency(before, monitor.READ_CMDS, 'r')
    after = {
        'cmdstat_hgetall': {'calls': 1100, 'usec': 3400},   # 100 calls, 400us -> 4.0
        'cmdstat_hget': {'calls': 600, 'usec': 1200},       # 100 calls, 200us -> 2.0
        'cmdstat_hset': {'calls': 2000, 'usec': 4000},
    }
    out = monitor.command_latency(after, monitor.READ_CMDS, 'r')
    check('cmdlat.hgetall', out['per_command']['hgetall'], 4.0)
    check('cmdlat.hget', out['per_command']['hget'], 2.0)
    check('cmdlat.group', out['us'], 3.0)         # 200 calls, 600us, call-weighted


def test_command_latency_ignores_housekeeping_commands():
    """dbsize/ping/info are this collector's own traffic and must not skew it."""
    check_true('cmdlat.no_dbsize', 'dbsize' not in monitor.READ_CMDS)
    check_true('cmdlat.no_ping', 'ping' not in monitor.READ_CMDS)
    check_true('cmdlat.reads', 'hgetall' in monitor.READ_CMDS)
    check_true('cmdlat.writes', 'hset' in monitor.WRITE_CMDS)


def test_command_latency_tolerates_missing_commands():
    monitor.reset_state()
    out = monitor.command_latency({}, monitor.READ_CMDS, 'empty')
    check('cmdlat.absent', out['us'], None)
    check('cmdlat.absent_per_cmd', out['per_command'], {})


def test_rds_seconds_converted_to_ms():
    """RDS ReadLatency/WriteLatency are seconds; every other latency here is ms."""
    check('sec2ms.value', monitor._sec_to_ms(0.0008930375849842178), 0.893)
    check('sec2ms.none', monitor._sec_to_ms(None), None)


def test_pick_latest_returns_newest_datapoint():
    points = [{'Timestamp': 100, 'Average': 41.6},
              {'Timestamp': 300, 'Average': 68.8},
              {'Timestamp': 200, 'Average': 55.0}]
    check('cw.latest', monitor.pick_latest(points), 68.8)


def test_pick_latest_empty_and_missing_stat():
    check('cw.empty', monitor.pick_latest([]), None)
    check('cw.missing_stat', monitor.pick_latest([{'Timestamp': 1, 'Maximum': 3}]), None)


def test_soft_isolates_a_failing_subsection():
    """A CloudWatch outage must degrade one field, not the whole card."""
    out = monitor.soft(lambda: (_ for _ in ()).throw(RuntimeError('AccessDenied')))
    check('soft.error', out['error'], 'RuntimeError: AccessDenied')


# --- CDC bookkeeping: stream ids, offsets, lag ------------------------------
def test_stream_id_ms_extracts_timestamp():
    check('id.ms', monitor.stream_id_ms('1755791234567-0'), 1755791234567)
    check('id.none', monitor.stream_id_ms(None), None)
    check('id.malformed', monitor.stream_id_ms('not-an-id'), None)


def test_parse_offset_hash_picks_freshest_partition():
    raw = {
        '{"server":"ecdl"}': '{"file":"mysql-bin-changelog.001480","pos":11,"ts_sec":1000}',
        '{"server":"ecdl2"}': '{"file":"mysql-bin-changelog.001482","pos":68092104,"ts_sec":2000}',
    }
    out = monitor.parse_offset_hash(raw)
    check('offset.file', out['file'], 'mysql-bin-changelog.001482')
    check('offset.pos', out['pos'], 68092104)
    check('offset.ts', out['ts_sec'], 2000)


def test_parse_offset_hash_tolerates_invalid_and_empty():
    check('offset.empty', monitor.parse_offset_hash({}), None)
    check('offset.none', monitor.parse_offset_hash(None), None)
    check('offset.invalid', monitor.parse_offset_hash({'f': 'not json'}), None)
    check('offset.no_pos', monitor.parse_offset_hash({'f': '{"file":"x"}'}), None)


def test_binlog_gap_same_file():
    monitor.reset_state()
    source = {'binlog_file': 'bin.001482', 'binlog_pos': 69341552}
    committed = {'file': 'bin.001482', 'pos': 68092104}
    check('gap.bytes', monitor.binlog_gap(source, committed)['binlog_gap_bytes'], 1249448)


def test_binlog_gap_is_labelled_as_trend_not_absolute():
    """It stayed at 6,083,104 through 30s of idle: the value alone is misleading."""
    monitor.reset_state()
    out = monitor.binlog_gap({'binlog_file': 'b', 'binlog_pos': 10},
                             {'file': 'b', 'pos': 1})
    check_true('gap.note_trend', 'watch the trend' in out['binlog_gap_note'])
    check_true('gap.note_reason', 'does not capture' in out['binlog_gap_note'])
    check_true('gap.trend_key', 'binlog_gap_trend_bytes_per_sec' in out)


def test_binlog_gap_trend_flat_when_keeping_pace():
    """Same gap two polls apart = the sink keeps up with the source, whatever the absolute value."""
    monitor.reset_state()
    monitor.binlog_gap({'binlog_file': 'b', 'binlog_pos': 6083104},
                       {'file': 'b', 'pos': 0})
    import time as _t
    # Must exceed MIN_RATE_INTERVAL_SEC: below that the rate helper deliberately
    # refuses to divide by a near-zero interval. Real polls are 2s and 5s apart.
    _t.sleep(monitor.MIN_RATE_INTERVAL_SEC + 0.05)
    out = monitor.binlog_gap({'binlog_file': 'b', 'binlog_pos': 6083104},
                             {'file': 'b', 'pos': 0})
    check('gap.trend_flat', out['binlog_gap_trend_bytes_per_sec'], 0.0)


def test_binlog_gap_never_negative():
    """Offset can briefly read ahead of our own SHOW MASTER STATUS sample."""
    monitor.reset_state()
    source = {'binlog_file': 'bin.001482', 'binlog_pos': 100}
    committed = {'file': 'bin.001482', 'pos': 140}
    check('gap.clamped', monitor.binlog_gap(source, committed)['binlog_gap_bytes'], 0)


def test_binlog_gap_across_rotation_reports_note_not_number():
    source = {'binlog_file': 'bin.001483', 'binlog_pos': 100}
    committed = {'file': 'bin.001482', 'pos': 900}
    out = monitor.binlog_gap(source, committed)
    check('gap.rotated_value', out['binlog_gap_bytes'], None)
    check_true('gap.rotated_note', 'rotated' in out['binlog_gap_note'])


def test_binlog_gap_missing_inputs():
    check('gap.no_source', monitor.binlog_gap(None, {'file': 'a', 'pos': 1}), {})
    check('gap.no_committed', monitor.binlog_gap({'binlog_file': 'a', 'binlog_pos': 1}, None), {})


def test_capture_lag_from_debezium_envelope():
    """Entry id millis minus source.ts_ms = Aurora commit -> broker latency."""
    fields = {'value': json.dumps({
        'payload': {'after': {'id': 1}, 'source': {'ts_ms': 1755791234545}, 'ts_ms': 1755791234560}
    })}
    out = monitor.capture_lag_from_entry('1755791234567-0', fields, now=1755791234600)
    check('capture.lag', out['capture_lag_ms'], 22)
    check('capture.age', out['last_event_age_ms'], 33)


def test_capture_lag_handles_unwrapped_payload():
    fields = {'value': json.dumps({'after': {'id': 1}, 'source': {'ts_ms': 1000}})}
    check('capture.unwrapped', monitor.capture_lag_from_entry('1050-0', fields)['capture_lag_ms'], 50)


def test_capture_lag_reports_note_when_timestamp_absent():
    """No source.ts_ms means no lag figure. It must NOT silently invent one
    (a heartbeat write would need explicit sign-off)."""
    fields = {'value': json.dumps({'after': {'id': 1}})}
    out = monitor.capture_lag_from_entry('1050-0', fields)
    check('capture.absent', out['capture_lag_ms'], None)
    check_true('capture.absent_note', 'ts_ms' in out['capture_note'])


def test_capture_lag_handles_non_json_and_missing_value():
    check('capture.nonjson',
          monitor.capture_lag_from_entry('1-0', {'value': '<xml/>'})['capture_lag_ms'], None)
    check('capture.novalue',
          monitor.capture_lag_from_entry('1-0', {})['capture_lag_ms'], None)
    check('capture.badid',
          monitor.capture_lag_from_entry('zz', {'value': '{}'})['capture_lag_ms'], None)


def test_capture_lag_never_negative():
    fields = {'value': json.dumps({'source': {'ts_ms': 2000}})}
    check('capture.clamped', monitor.capture_lag_from_entry('1000-0', fields)['capture_lag_ms'], 0)


# --- ECS status mapping -----------------------------------------------------
def test_stuck_threshold_sits_above_the_sink_reclaim_window():
    """The Go sink retries entries idle >30s on a 15s tick, so ~45-60s of waiting
    is normal recovery after a task is replaced. Warning earlier would be a false alarm."""
    check_true('stuck.threshold', monitor.STUCK_WARN_MS >= 45000)


def test_ecs_state_healthy():
    check('ecs.ok', monitor.ecs_state(2, 2, 0), ('ok', 'up'))


def test_ecs_state_partial_is_starting():
    check('ecs.partial', monitor.ecs_state(2, 1, 1), ('starting', 'starting'))


def test_ecs_state_all_tasks_gone_is_down():
    check('ecs.down', monitor.ecs_state(2, 0, 0), ('bad', 'down'))


def test_ecs_state_scaled_to_zero_is_paused():
    # Was ('bad', 'stopped'). A service at desiredCount 0 was put there by
    # somebody — the handoff does exactly that — so it is not a fault.
    check('ecs.stopped', monitor.ecs_state(0, 0, 0), ('paused', 'paused'))


def test_ecs_state_handles_none_counts():
    check('ecs.none', monitor.ecs_state(None, None), ('paused', 'paused'))


# --- INFO parsing / aggregation across cluster nodes ------------------------
def test_info_nodes_single_node():
    raw = {'used_memory': 100, 'instantaneous_ops_per_sec': 5}
    check('info.single', monitor.info_nodes(raw), [raw])


def test_info_nodes_cluster_shape():
    raw = {'n1:6379': {'used_memory': 100}, 'n2:6379': {'used_memory': 50}}
    check('info.cluster_len', len(monitor.info_nodes(raw)), 2)


def test_info_nodes_empty():
    check('info.none', monitor.info_nodes(None), [])
    check('info.empty', monitor.info_nodes({}), [])


def test_info_sum_across_nodes_and_string_digits():
    nodes = [{'used_memory': 100}, {'used_memory': '50'}, {'other': 1}]
    check('info.sum', monitor.info_sum(nodes, 'used_memory'), 150)
    check('info.sum_absent', monitor.info_sum(nodes, 'nope'), None)


def test_info_first_skips_missing():
    nodes = [{'redis_version': None}, {'redis_version': '9.0.0'}]
    check('info.first', monitor.info_first(nodes, 'redis_version'), '9.0.0')


def test_human_bytes():
    check('bytes.b', monitor.human_bytes(512), '512 B')
    check('bytes.g', monitor.human_bytes(1363148800), '1.27 G')
    check('bytes.invalid', monitor.human_bytes(None), None)
    check('bytes.negative', monitor.human_bytes(-5), None)


def test_hit_rate():
    check('hit.pct', monitor.hit_rate_pct(994, 6), 99.4)
    check('hit.zero_total', monitor.hit_rate_pct(0, 0), None)
    check('hit.missing', monitor.hit_rate_pct(None, 5), None)


def test_cluster_call_falls_back_for_single_node_client():
    """A non-cluster client has no target_nodes kwarg; the call must still work."""
    class Single:
        def info(self):
            return {'used_memory': 1}

    check('cluster_call.fallback', monitor.cluster_call(Single(), 'info'), {'used_memory': 1})


def test_cluster_call_uses_primaries_when_supported():
    seen = {}

    class Cluster:
        def info(self, target_nodes=None):
            seen['target'] = target_nodes
            return {'n1': {'used_memory': 1}}

    monitor.cluster_call(Cluster(), 'info')
    check('cluster_call.primaries', seen['target'], monitor.PRIMARIES)


def test_node_roles_uses_inclusive_accessors():
    class Node:
        def __init__(self, name):
            self.name = name

    class Cluster:
        def get_primaries(self):
            return [Node('a:6379')]

        def get_replicas(self):
            return [Node('b:6379')]

    check('nodes.roles', monitor.node_roles(Cluster()),
          [{'name': 'a:6379', 'role': 'primary'}, {'name': 'b:6379', 'role': 'replica'}])


def test_node_roles_tolerates_client_without_accessors():
    check('nodes.absent', monitor.node_roles(object()), [])


# --- flow banner: hop states and the headline CDC lag -----------------------
OK_AURORA = {'ok': True, 'state': 'ok'}
OK_TARGET = {'ok': True, 'state': 'ok'}


def test_hop_state_unreachable_beats_reported_state():
    check('hop.error', monitor.hop_state({'ok': False, 'state': 'ok'}), 'bad')
    check('hop.warn', monitor.hop_state({'ok': True, 'state': 'warn'}), 'warn')
    check('hop.ok', monitor.hop_state({'ok': True}), 'ok')


def test_hop_state_no_data_is_never_green():
    """Nothing collected must never render as healthy.

    'unreachable' rather than 'bad': with nothing collected there is no evidence
    the component is unhealthy, only that it could not be read.
    """
    check('hop.empty', monitor.hop_state({}), 'unreachable')
    check('hop.none', monitor.hop_state(None), 'unreachable')
    check_true('hop.not_ok', monitor.hop_state({}) != 'ok')


def test_hop_state_separates_unreachable_from_down():
    """The distinction the banner depends on.

    A read failure carries an error; a component that is genuinely down was read
    successfully and reports state 'bad'. Conflating them let one visibility gap --
    most often a customer-owned target whose security group does not admit this
    host -- mark a healthy pipeline as failed.
    """
    check('hop.unreachable', monitor.hop_state(
        {'ok': False, 'error': 'ConnectionError: timed out'}), 'unreachable')
    check('hop.down', monitor.hop_state(
        {'ok': False, 'state': 'bad', 'desired': 2, 'running': 0}), 'bad')
    # An error alongside a healthy-looking state is still a read failure.
    check('hop.error_wins', monitor.hop_state(
        {'ok': True, 'state': 'ok', 'error': 'AccessDenied'}), 'unreachable')


def test_broker_wait_prefers_oldest_pending():
    check('wait.pending',
          monitor.broker_wait_ms({'group_lag': 0}, {'oldest_pending_age_ms': 31}), 31)


def test_broker_wait_falls_back_to_undelivered_entries():
    """Entries the sink has not read yet still count as lag."""
    check('wait.undelivered',
          monitor.broker_wait_ms({'group_lag': 500, 'oldest_entry_age_ms': 900}, {}), 900)


def test_broker_wait_caught_up_is_zero_not_none():
    check('wait.caught_up',
          monitor.broker_wait_ms({'group_lag': 0, 'pending': 0}, {}), 0)


def test_broker_wait_unknown_when_broker_unreachable():
    """An unreachable broker means the wait is unknown, not zero."""
    check('wait.unreachable',
          monitor.broker_wait_ms({'ok': False, 'error': 'ConnectionError'}, {}), None)


def test_flow_total_outage_reads_as_connectivity_problem():
    # What safe() actually returns when it cannot reach a component: ok False
    # WITH an error. The error is the signal that the read failed rather than the
    # component being unhealthy.
    dead = {'ok': False, 'error': 'ConnectionError: timed out'}
    flow = monitor.build_flow(dead, dead, dead, dead, dead)
    check('flow.outage_wait', flow['broker_wait_ms'], None)
    check('flow.outage_lag', flow['cdc_lag_ms'], None)
    check_true('flow.outage_msg', 'no component reachable' in flow['message'])


def test_one_visibility_gap_does_not_condemn_the_pipeline():
    """A single unreadable component must not read as an outage.

    The live case: the target cache belongs to the customer, so its security
    group need not admit the control plane, and ECDL must not edit it. The old
    behaviour announced "target down" for a pipeline that was working.
    """
    up = {'ok': True, 'state': 'ok'}
    broker = dict(up, produced_per_sec=6200, entries_added=100)
    sink = dict(up, applied_per_sec=6190, oldest_pending_age_ms=30)
    unreadable = {'ok': False, 'error': 'ConnectionError: timed out'}
    flow = monitor.build_flow(up, up, broker, sink, unreadable)

    check('gap.hop', flow['hops']['target'], 'unreachable')
    check('gap.others_ok', flow['hops']['broker'], 'ok')
    check_true('gap.not_down', 'down' not in flow['message'])
    check_true('gap.names_it', 'target' in flow['message'])
    check_true('gap.says_not_visible', 'not visible' in flow['message'])
    check_true('gap.rest_flowing', 'the rest is flowing' in flow['message'])
    # The page needs the reason to word the panel, not just the state.
    check('gap.spots', [s['component'] for s in flow['visibility_gaps']], ['target'])
    check_true('gap.spot_error', 'ConnectionError' in flow['visibility_gaps'][0]['error'])
    check_true('gap.note_explains_ownership',
               'customer-owned' in flow['visibility_note'])


def test_a_real_outage_still_says_down_and_names_the_visibility_gap():
    up = {'ok': True, 'state': 'ok'}
    down = {'ok': False, 'state': 'bad', 'desired': 2, 'running': 0}
    unreadable = {'ok': False, 'error': 'ConnectionError'}
    flow = monitor.build_flow(up, up, up, down, unreadable)
    check_true('mixed.says_down', 'sink down' in flow['message'])
    check_true('mixed.also_unseen', 'target not visible' in flow['message'])


def test_no_visibility_gaps_means_no_gap_fields():
    up = {'ok': True, 'state': 'ok'}
    flow = monitor.build_flow(up, up, dict(up, produced_per_sec=10),
                              dict(up, applied_per_sec=10), up)
    check_true('clean.no_spots', 'visibility_gaps' not in flow)
    check_true('clean.no_note', 'visibility_note' not in flow)


def test_flow_lag_is_capture_plus_wait():
    flow = monitor.build_flow(
        OK_AURORA,
        {'ok': True, 'state': 'ok', 'capture_lag_ms': 22},
        {'ok': True, 'state': 'ok', 'produced_per_sec': 6210, 'group_lag': 0, 'pending': 1842},
        {'ok': True, 'state': 'ok', 'oldest_pending_age_ms': 31, 'applied_per_sec': 6190},
        OK_TARGET)
    check('flow.lag', flow['cdc_lag_ms'], 53)
    check('flow.lag_source', flow['lag_source'], 'in-flight')
    check('flow.flowing', flow['flowing'], True)
    check('flow.message', flow['message'], 'all hops flowing')
    check('flow.hops', flow['hops']['sink'], 'ok')


def test_capture_measurement_is_remembered_with_its_age():
    """The sink XDELs acked entries, so at idle there is nothing left to sample."""
    monitor.reset_state()
    check('capture.remember_empty', monitor.last_capture(), {})
    monitor.remember_capture(22, now=1000.0)
    out = monitor.last_capture(now=1003.5)
    check('capture.remembered', out['capture_lag_ms_last'], 22)
    check('capture.age', out['capture_measured_age_sec'], 3.5)


def test_flow_uses_probe_when_nothing_is_in_flight():
    """A caught-up sink leaves nothing to observe, so the probe carries the number."""
    flow = monitor.build_flow(
        OK_AURORA,
        {'ok': True, 'state': 'ok', 'capture_lag_ms': None},
        {'ok': True, 'state': 'ok', 'group_lag': 0, 'pending': 0},
        {'ok': True, 'state': 'ok'},
        OK_TARGET,
        probe={'ok': True, 'lag_ms': 64.0, 'age_sec': 8.0})
    check('flow.probe_lag', flow['cdc_lag_ms'], 64.0)
    check('flow.probe_source', flow['lag_source'], 'probe')
    check('flow.probe_age', flow['lag_measured_age_sec'], 8.0)
    check_true('flow.probe_note', 'measured end to end 8.0s ago' in flow['lag_note'])


def test_flow_prefers_live_in_flight_observation_over_older_probe():
    """Entries in flight are happening now; a probe is a moment ago."""
    flow = monitor.build_flow(
        OK_AURORA,
        {'ok': True, 'state': 'ok', 'capture_lag_ms': 24},
        {'ok': True, 'state': 'warn', 'group_lag': 5000, 'pending': 5000},
        {'ok': True, 'state': 'ok', 'oldest_pending_age_ms': 4000},
        OK_TARGET,
        probe={'ok': True, 'lag_ms': 64.0, 'age_sec': 30.0})
    check('flow.inflight_lag', flow['cdc_lag_ms'], 4024)
    check('flow.inflight_source', flow['lag_source'], 'in-flight')


def test_flow_explains_itself_when_no_measurement_exists():
    flow = monitor.build_flow(
        OK_AURORA, {'ok': True, 'state': 'ok', 'capture_lag_ms': None},
        {'ok': True, 'state': 'ok', 'group_lag': 0, 'pending': 0},
        {'ok': True, 'state': 'ok'}, OK_TARGET,
        probe={'ok': False, 'note': 'no probe run yet (POST /api/monitor/probe to measure)'})
    check('flow.none_lag', flow['cdc_lag_ms'], None)
    check('flow.none_source', flow['lag_source'], None)
    check_true('flow.none_note', 'no probe run yet' in flow['lag_note'])


def test_single_stuck_straggler_does_not_headline_climbing_lag():
    """One orphaned entry in a dead consumer's pending list must not read as lag.

    The bug: `wait` is the age of the oldest unacked entry, which climbs with
    wall-clock time. A single stuck straggler in an otherwise caught-up pipeline
    made the banner headline an incorrect, ever-growing figure (~700s and rising)
    labelled 'in-flight'. A real backlog is many entries deep; a straggler is
    one. Below the depth threshold the number must fall through to the probe.
    """
    monitor.reset_state()
    flow = monitor.build_flow(
        OK_AURORA,
        {'ok': True, 'state': 'ok', 'capture_lag_ms': 15},
        # One entry stuck: backlog of 1, nothing being produced.
        {'ok': True, 'state': 'ok', 'produced_per_sec': 0,
         'group_lag': 0, 'pending': 1, 'backlog': 1},
        # ...and it has been unacked for ~700 seconds (the climbing value).
        {'ok': True, 'state': 'ok', 'applied_per_sec': 0,
         'oldest_pending_age_ms': 700000},
        OK_TARGET,
        probe={'ok': True, 'lag_ms': 18.0, 'age_sec': 4.0})
    # Not the 700015ms straggler age, and not labelled in-flight.
    check('straggler.not_inflight', flow['lag_source'], 'probe')
    check('straggler.uses_probe', flow['cdc_lag_ms'], 18.0)
    # The raw observation is still available on the flow, just not headlined.
    check('straggler.raw_wait_kept', flow['broker_wait_ms'], 700000)


def test_small_stuck_backlog_with_no_probe_reports_none_not_a_climb():
    """A straggler and no probe yields an accurate 'none', never a climbing number."""
    monitor.reset_state()
    flow = monitor.build_flow(
        OK_AURORA,
        {'ok': True, 'state': 'ok', 'capture_lag_ms': 15},
        {'ok': True, 'state': 'ok', 'produced_per_sec': 0,
         'group_lag': 0, 'pending': 2, 'backlog': 2},
        {'ok': True, 'state': 'ok', 'applied_per_sec': 0,
         'oldest_pending_age_ms': 512000},
        OK_TARGET,
        probe={'ok': False, 'note': 'no probe run yet'})
    check('straggler_np.source', flow['lag_source'], None)
    check('straggler_np.lag', flow['cdc_lag_ms'], None)


def test_backlog_at_threshold_is_reported_in_flight():
    """A genuine queue at the depth threshold is real and must be headlined."""
    monitor.reset_state()
    flow = monitor.build_flow(
        OK_AURORA,
        {'ok': True, 'state': 'ok', 'capture_lag_ms': 20},
        {'ok': True, 'state': 'ok', 'produced_per_sec': 4000,
         'group_lag': 0, 'pending': 50, 'backlog': 50},
        {'ok': True, 'state': 'ok', 'applied_per_sec': 4000,
         'oldest_pending_age_ms': 80},
        OK_TARGET,
        probe={'ok': True, 'lag_ms': 64.0, 'age_sec': 30.0})
    check('threshold.source', flow['lag_source'], 'in-flight')
    check('threshold.lag', flow['cdc_lag_ms'], 100)


# --- delivery integrity: nothing may be lost silently -----------------------
def test_verdict_ok_when_clean():
    check('verdict.ok', monitor.integrity_verdict(0, 0, 0, 0, 20.0), 'ok')


def test_verdict_dead_letter_is_loss():
    """Dead-lettered entries are permanent loss from the cache's point of view."""
    check('verdict.dlq', monitor.integrity_verdict(3, 0, 0, 0, 20.0), 'loss')


def test_verdict_eviction_is_loss():
    check('verdict.evicted', monitor.integrity_verdict(0, 12, 0, 0, 20.0), 'loss')


def test_verdict_loss_outranks_every_other_signal():
    """A retrying, stuck, memory-pressured pipeline that also lost data reads LOSS."""
    check('verdict.precedence',
          monitor.integrity_verdict(1, 5, 40, 900000, 99.0), 'loss')


def test_backlog_is_undelivered_plus_unacked_not_length_plus_lag():
    """Regression: an undelivered entry appears in BOTH stream length and group
    lag, so length+lag double-counts. Observed live as 4,000 for 2,000 writes."""
    group = {'lag': 2000, 'pending': 0}
    length = 2000                      # the same 2,000 entries, still resident
    backlog = (group['lag'] or 0) + (group['pending'] or 0)
    check('backlog.no_double_count', backlog, 2000)
    check_true('backlog.not_length_plus_lag', backlog != length + group['lag'])


def test_verdict_stalled_when_backlog_is_not_draining():
    """Undelivered entries are not retrying and not dead-lettered, so every other
    signal reads clean while the backlog grows toward a full broker."""
    check('verdict.stalled',
          monitor.integrity_verdict(0, 0, 0, 0, 20.0, backlog=5000, applied_per_sec=0.0),
          'stalled')


def test_verdict_not_stalled_while_draining():
    check('verdict.draining',
          monitor.integrity_verdict(0, 0, 0, 0, 20.0, backlog=5000, applied_per_sec=1200.0),
          'ok')


def test_verdict_not_stalled_before_a_drain_rate_exists():
    """First poll has no rate yet; that must not be reported as a stall."""
    check('verdict.no_rate_yet',
          monitor.integrity_verdict(0, 0, 0, 0, 20.0, backlog=5000, applied_per_sec=None),
          'ok')


def test_verdict_loss_outranks_stalled():
    check('verdict.loss_over_stalled',
          monitor.integrity_verdict(2, 0, 0, 0, 20.0, backlog=5000, applied_per_sec=0.0),
          'loss')


def test_verdict_stuck_outranks_retrying():
    check('verdict.stuck', monitor.integrity_verdict(0, 0, 4, 120000, 20.0), 'stuck')


def test_verdict_memory_pressure_is_flagged():
    """A full broker stalls ingestion even when nothing is lost yet."""
    check('verdict.pressure', monitor.integrity_verdict(0, 0, 0, 0, 88.0), 'pressure')


def test_verdict_retrying_is_visible_but_not_loss():
    """Retries are recovery in progress: visible, but nothing is lost yet."""
    check('verdict.retrying', monitor.integrity_verdict(0, 0, 7, 1000, 20.0), 'retrying')


def test_every_verdict_has_a_plain_english_headline():
    for verdict in ('loss', 'stalled', 'stuck', 'pressure', 'retrying', 'ok'):
        check_true('headline.' + verdict, bool(monitor.INTEGRITY_HEADLINE.get(verdict)))
    check_true('headline.ok_wording',
               'no loss' in monitor.INTEGRITY_HEADLINE['ok'])
    check_true('headline.loss_wording',
               'LOST' in monitor.INTEGRITY_HEADLINE['loss'])


def test_ok_verdict_requires_all_signals_clean():
    """Guards against a future edit letting 'ok' through with a dirty signal."""
    dirty = [
        {'dlq_len': 1}, {'evicted': 1}, {'retrying': 1},
        {'stuck_ms': monitor.STUCK_WARN_MS + 1},
        {'mem_pct': monitor.CFG['broker_mem_warn_pct'] + 1},
    ]
    for case in dirty:
        args = {'dlq_len': 0, 'evicted': 0, 'retrying': 0, 'stuck_ms': 0, 'mem_pct': 10.0}
        args.update(case)
        got = monitor.integrity_verdict(args['dlq_len'], args['evicted'],
                                       args['retrying'], args['stuck_ms'], args['mem_pct'])
        if got == 'ok':
            FAILURES.append('verdict should not be ok for {}'.format(case))


# --- lag probe ---------------------------------------------------------------
def test_human_age_is_readable():
    check('age.sec', monitor.human_age(42.0), '42s')
    check('age.min', monitor.human_age(600.0), '10m')
    check('age.hour', monitor.human_age(5348.1), '1.5h')
    check('age.none', monitor.human_age(None), 'unknown')


def test_stale_probe_is_flagged_not_presented_as_current():
    """A 90-minute-old reading must never look like the live figure."""
    flow = monitor.build_flow(
        OK_AURORA, {'ok': True, 'state': 'ok', 'capture_lag_ms': None},
        {'ok': True, 'state': 'ok', 'group_lag': 0, 'pending': 0},
        {'ok': True, 'state': 'ok'}, OK_TARGET,
        probe={'ok': True, 'lag_ms': 15.0, 'age_sec': 5348.1})
    check('stale.flag', flow['lag_stale'], True)
    check_true('stale.note_prefix', flow['lag_note'].startswith('STALE:'))
    check_true('stale.note_age', '1.5h' in flow['lag_note'])
    check_true('stale.note_action', 'Measure now' in flow['lag_note'])


def test_fresh_probe_is_not_flagged_stale():
    flow = monitor.build_flow(
        OK_AURORA, {'ok': True, 'state': 'ok', 'capture_lag_ms': None},
        {'ok': True, 'state': 'ok', 'group_lag': 0, 'pending': 0},
        {'ok': True, 'state': 'ok'}, OK_TARGET,
        probe={'ok': True, 'lag_ms': 15.0, 'age_sec': 12.0})
    check('fresh.flag', flow['lag_stale'], False)
    check_true('fresh.note', 'measured end to end 12.0s ago' in flow['lag_note'])


def test_last_probe_reports_nothing_run_yet():
    monitor._probe_state.clear()
    out = monitor.last_probe()
    check('probe.empty_ok', out['ok'], False)
    check_true('probe.empty_note', 'no probe run yet' in out['note'])


def test_last_probe_returns_result_with_age_and_never_writes():
    monitor._probe_state.clear()
    monitor._probe_state['last_result'] = {'ok': True, 'lag_ms': 64.0, 'row_id': 7}
    monitor._probe_state['last_result_wall'] = 1000.0
    out = monitor.last_probe(now=1012.5)
    check('probe.lag', out['lag_ms'], 64.0)
    check('probe.age', out['age_sec'], 12.5)


def test_probe_can_be_disabled_entirely():
    monitor._probe_state.clear()
    original = monitor.CFG['probe_enabled']
    monitor.CFG['probe_enabled'] = False
    try:
        out = monitor.run_probe()
        check('probe.disabled_ok', out['ok'], False)
        check_true('probe.disabled_msg', 'DEMO_PROBE_ENABLED' in out['error'])
    finally:
        monitor.CFG['probe_enabled'] = original


def test_probe_is_throttled_so_polling_cannot_hammer_aurora():
    """Guards against the page or a script probing on every refresh."""
    monitor._probe_state.clear()
    monitor._probe_state['last_run_monotonic'] = __import__('time').monotonic()
    out = monitor.run_probe()
    check('probe.throttled', out.get('throttled'), True)
    check('probe.throttled_ok', out['ok'], False)


def test_flow_lag_none_when_nothing_measurable():
    """No fabricated number when there is nothing to measure."""
    monitor.reset_state()          # nothing ever measured
    flow = monitor.build_flow(
        OK_AURORA,
        {'ok': True, 'state': 'ok', 'capture_lag_ms': None, 'capture_note': 'stream is empty'},
        {'ok': True, 'state': 'ok', 'group_lag': 0, 'pending': 0},
        {'ok': True, 'state': 'ok'},
        OK_TARGET)
    check('flow.lag_none', flow['cdc_lag_ms'], None)
    check('flow.lag_source_none', flow['lag_source'], None)
    check_true('flow.lag_note', 'nothing in flight' in flow['lag_note'])


def test_flow_reports_sink_down_and_backlog():
    flow = monitor.build_flow(
        OK_AURORA,
        {'ok': True, 'state': 'ok', 'capture_lag_ms': 24},
        {'ok': True, 'state': 'warn', 'produced_per_sec': 6210, 'backlog': 114208,
         'group_lag': 114208, 'pending': 114208},
        {'ok': False, 'state': 'bad', 'oldest_pending_age_ms': 18376, 'applied_per_sec': 0},
        OK_TARGET)
    check('flow.down_lag', flow['cdc_lag_ms'], 18400)
    check('flow.down_hop', flow['hops']['sink'], 'bad')
    check_true('flow.down_msg_names', flow['message'].startswith('sink down'))
    check_true('flow.down_msg_backlog', '114,208' in flow['message'])
    # No comment about which side is authoritative; only the state is reported.
    check_true('flow.down_msg_terse', 'authoritative' not in flow['message'])


def test_flow_idle_says_nothing_to_replicate_not_a_fault():
    flow = monitor.build_flow(
        OK_AURORA,
        {'ok': True, 'state': 'ok', 'capture_lag_ms': 5},
        {'ok': True, 'state': 'ok', 'produced_per_sec': 0, 'group_lag': 0, 'pending': 0,
         'last_event_age_ms': 42000},
        {'ok': True, 'state': 'ok', 'applied_per_sec': 0},
        OK_TARGET)
    check('flow.idle_flowing', flow['flowing'], False)
    check_true('flow.idle_msg', 'nothing to replicate' in flow['message'])
    check_true('flow.idle_age', '42s ago' in flow['message'])


def test_flow_idle_age_is_human_readable_not_raw_seconds():
    """It read 'no new events for 4707s'. 4707 seconds is 78 minutes."""
    flow = monitor.build_flow(
        OK_AURORA,
        {'ok': True, 'state': 'ok', 'capture_lag_ms': 5},
        {'ok': True, 'state': 'ok', 'produced_per_sec': 0, 'group_lag': 0, 'pending': 0,
         'last_event_age_ms': 4707000},
        {'ok': True, 'state': 'ok', 'applied_per_sec': 0},
        OK_TARGET)
    check_true('flow.idle_human', '1.3h ago' in flow['message'])
    check_true('flow.idle_no_raw', '4707' not in flow['message'])


def test_flow_does_not_claim_idle_before_rates_exist():
    """First poll after a restart has no rates yet; 'idle' would be a guess and
    would be wrong under live load."""
    flow = monitor.build_flow(
        OK_AURORA,
        {'ok': True, 'state': 'ok', 'capture_lag_ms': 5},
        {'ok': True, 'state': 'ok', 'produced_per_sec': None, 'group_lag': 0, 'pending': 0,
         'last_event_age_ms': 1000},
        {'ok': True, 'state': 'ok', 'applied_per_sec': None},
        OK_TARGET)
    check_true('flow.measuring', 'measuring throughput' in flow['message'])
    check_true('flow.not_idle', 'nothing to replicate' not in flow['message'])


def test_flow_degraded_mentions_the_component():
    flow = monitor.build_flow(
        OK_AURORA, {'ok': True, 'state': 'ok', 'capture_lag_ms': 1},
        {'ok': True, 'state': 'warn', 'backlog': 20000, 'group_lag': 20000, 'pending': 20000,
         'produced_per_sec': 100},
        {'ok': True, 'state': 'ok', 'oldest_pending_age_ms': 5000}, OK_TARGET)
    check_true('flow.degraded', flow['message'].startswith('broker degraded'))


# --- source vs target reconciliation ----------------------------------------
DB_ROW = {'id': 1004577715, 'sku': 'live-1732-0005', 'price': 412.50, 'stock_count': 1000000}


def test_reconcile_marks_matching_row_synced():
    cache = {1004577715: {'price': '412.50', 'stock_count': '1000000'}}
    out = monitor.reconcile_rows([DB_ROW], cache)
    check('rec.synced', out[0]['state'], 'synced')
    check('rec.sku', out[0]['sku'], 'live-1732-0005')


def test_reconcile_marks_missing_key_pending():
    out = monitor.reconcile_rows([DB_ROW], {})
    check('rec.pending', out[0]['state'], 'pending')
    check('rec.cache_value', out[0]['cache_price'], None)


def test_reconcile_marks_stale_value_differs():
    """Row is in the cache but the newest update has not been applied yet."""
    cache = {1004577715: {'price': '412.50', 'stock_count': '999999'}}
    check('rec.differs', monitor.reconcile_rows([DB_ROW], cache)[0]['state'], 'differs')


def test_reconcile_ignores_decimal_formatting_differences():
    """412.5 from MySQL and '412.50' from the cache are the same price."""
    cache = {1004577715: {'price': '412.5', 'stock_count': '1000000'}}
    check('rec.format', monitor.reconcile_rows([DB_ROW], cache)[0]['state'], 'synced')


def test_reconcile_accepts_string_keyed_cache_ids():
    cache = {'1004577715': {'price': '412.50', 'stock_count': '1000000'}}
    check('rec.strkey', monitor.reconcile_rows([DB_ROW], cache)[0]['state'], 'synced')


def test_reconcile_handles_mixed_batch():
    # Copies, never a mutation of the shared fixture.
    rows = [dict(DB_ROW, id=1, sku='a'), dict(DB_ROW, id=2, sku='b'),
            dict(DB_ROW, id=3, sku='c')]
    cache = {1: {'price': '412.50', 'stock_count': '1000000'},
             3: {'price': '99.00', 'stock_count': '1000000'}}
    out = monitor.reconcile_rows(rows, cache)
    check('rec.mixed', [r['state'] for r in out], ['synced', 'pending', 'differs'])


def test_decimal_prices_become_json_numbers():
    """MySQL DECIMAL would otherwise reach the browser as a quoted string."""
    check('num.decimal', monitor.as_number(decimal.Decimal('412.50')), 412.5)
    check('num.passthrough', monitor.as_number(1000000), 1000000)
    check('num.none', monitor.as_number(None), None)


def test_reconciled_payload_is_plain_json():
    rows = [{'id': 1, 'sku': 'a', 'price': monitor.as_number(decimal.Decimal('412.50')),
             'stock_count': 1000000}]
    out = monitor.reconcile_rows(rows, {1: {'price': '412.50', 'stock_count': '1000000'}})
    json.dumps(out)          # raises TypeError if a Decimal leaked through
    check('json.state', out[0]['state'], 'synced')


def test_safe_identifier_rejects_injection():
    """The table name comes from env and cannot be a bound parameter.

    A set-but-unsafe name RAISES. It used to fall back to the default table,
    which meant a bad name quietly queried a different table."""
    check('ident.ok', monitor.safe_identifier('products', 'fallback'), 'products')
    check('ident.qualified', monitor.safe_identifier('Inventory.products', 'x'),
          'Inventory.products')
    check('ident.empty', monitor.safe_identifier('', 'fallback'), 'fallback')
    check('ident.none', monitor.safe_identifier(None, 'fallback'), 'fallback')
    for bad in ('products; DROP TABLE x', 'my table', 'a`b', 'p" OR 1=1 --',
                'x/*', 'ürün', 'products\n'):
        try:
            monitor.safe_identifier(bad, 'fallback')
            raised = False
        except monitor.sqlident.UnsafeIdentifier:
            raised = True
        check('ident.raises {!r}'.format(bad), raised, True)


class _RecordingCursor:
    """Captures SQL text and binds; answers information_schema with `columns`."""

    def __init__(self, columns):
        self.columns = columns
        self.sql = []
        self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last = sql

    def fetchall(self):
        if 'information_schema' in (self._last or ''):
            return self.columns
        return []

    def fetchone(self):
        return None


def test_monitor_sql_quotes_identifiers_and_drops_unsafe_columns():
    """Column names come from the customer's schema and are put into SQL text.

    A column whose name could break out of its quotes is left out of the panel;
    every name that is used is backtick-quoted; values stay bound."""
    cur = _RecordingCursor([
        ('id', 'int', 'PRI'),
        ('name', 'varchar', ''),
        ('price`) FROM x; DROP TABLE products; -- ', 'int', ''),
        ('stock_count', 'int', ''),
    ])
    monitor._cache.pop('shape:{}:{}'.format(monitor.CFG['db_name'], 'evil_t'), None)
    shape = monitor.table_shape(cur, 'evil_t')
    check('shape.unsafe_dropped', any('DROP' in c for c in shape['columns']), False)
    check('shape.note', 'not shown' in (shape.get('note') or ''), True)
    _sel, names = monitor.shape_select(shape)
    sql = monitor._sql('SELECT {cols} FROM {t} WHERE {pk} = %s',
                       cols=names, t='Inventory.evil_t', pk=shape['pk'])
    check('sql.quoted', sql,
          'SELECT `id`, `name`, `stock_count` FROM `Inventory`.`evil_t` WHERE `id` = %s')


def test_monitor_shape_refuses_unsafe_primary_key():
    cur = _RecordingCursor([('id`; DROP TABLE t; --', 'int', 'PRI'), ('v', 'int', '')])
    monitor._cache.pop('shape:{}:{}'.format(monitor.CFG['db_name'], 'pk_t'), None)
    try:
        monitor.table_shape(cur, 'pk_t')
        raised = False
    except monitor.sqlident.UnsafeIdentifier:
        raised = True
    check('shape.pk_refused', raised, True)


def test_link_config_rejects_bad_link_names():
    for bad in ('../x', 'Shop', 'a b', "x'; --", 'a' * 40, ''):
        try:
            monitor.link_config(bad)
            raised = False
        except ValueError as exc:
            raised = 'not a valid link name' in str(exc)
        check('link.name {!r}'.format(bad), raised, True)


def test_downsample_keeps_shape_and_preserves_gaps():
    """120 min of 5s samples is 1440 points; average them down, but never turn
    'no data' into a zero - that would read as 'this store was fast'."""
    points = []
    for i in range(100):
        points.append({
            't': 1000 + i,
            'aurora_cpu_pct': 10.0 if i < 50 else 90.0,
            # Valkey served nothing for the first half of the window.
            'valkey_read_p90': None if i < 50 else 1.0,
        })
    out = monitor.downsample(points, 10)
    check('ds.buckets', len(out), 10)
    # First bucket averages the low half, last bucket the high half.
    check('ds.first_cpu', out[0]['aurora_cpu_pct'], 10.0)
    check('ds.last_cpu', out[-1]['aurora_cpu_pct'], 90.0)
    # Gap preserved, not zeroed.
    check('ds.gap_stays_none', out[0]['valkey_read_p90'], None)
    check('ds.value_after_gap', out[-1]['valkey_read_p90'], 1.0)
    check('ds.sample_count_kept', out[0]['n'], 10)
    # Under the cap, nothing is touched.
    check('ds.noop', monitor.downsample(points, 500), points)
    check('ds.empty', monitor.downsample([], 10), [])


def test_history_series_covers_read_and_write_per_source():
    s = monitor.HISTORY_SERIES
    for src in ('aurora_read', 'aurora_write', 'valkey_read'):
        for pct in (50, 90, 99):
            name = '{}_p{}'.format(src, pct)
            assert name in s, 'missing history series ' + name
    # Deliberately absent: the app never writes to the cache, so there is no
    # client-side sample to build a percentile from.
    assert 'valkey_write_p90' not in s
    # The average is still carried, from commandstats.
    assert 'valkey_write_us' in s


def test_run_phase_rejects_unknown_phases():
    """This endpoint starts real load, so it takes an allowlist, not a command."""
    client = monitor_client()
    for bad in ('5', 'rm -rf', '', '0'):
        resp = client.post('/api/monitor/run?phase=' + bad)
        check('run.reject.' + (bad or 'empty'), resp.status_code, 400)


def test_run_phase_bounds_the_load_parameters():
    """A typo in the page must not be able to ask for 500k concurrency."""
    monitor.reset_state()
    captured = {}

    class FakeBroker:
        def hset(self, key, mapping=None):
            captured['record'] = mapping
        def expire(self, *a, **k):
            pass
        def lpush(self, key, value):
            captured.setdefault(key, []).append(value)
        def ltrim(self, *a, **k):
            pass
        def delete(self, *a, **k):
            pass
        def llen(self, key):
            return 1

    original = monitor.with_broker
    monitor.with_broker = lambda fn: fn(FakeBroker())
    try:
        client = monitor_client()
        resp = client.post('/api/monitor/run',
                           json={'phase': '1', 'conc': 999999, 'dur': -5,
                                 'write_pct': 250})
        check('run.ok', resp.status_code, 200)
        job = json.loads(captured[monitor.JOB_QUEUE][0])
        check('run.conc_capped', job['conc'], 4000)
        check('run.dur_floored', job['dur'], 10)
        check('run.write_capped', job['write_pct'], 100)
        check('run.steps', job['steps'], '1')
        # The agent is told which phase, never a command to run.
        assert 'cmd' not in job and 'command' not in job, job
    finally:
        monitor.with_broker = original


def test_run_summary_covers_only_that_runs_window():
    """Each run carries its own numbers - that is what makes runs comparable."""
    monitor.reset_state()
    monitor._history.clear()
    # Whole seconds: converting a fractional epoch to int ms and back truncates,
    # which would drop the sample sitting exactly on the window edge.
    base = int(time.time()) - 600
    # 6 samples of an idle period, then 6 of a loaded one.
    for i in range(6):
        monitor._history.append({'t': base + i * 5, 'aurora_cpu_pct': 2.0,
                                 'aurora_read_p90': 0.5, 'valkey_keys': 100})
    for i in range(6):
        monitor._history.append({'t': base + 100 + i * 5, 'aurora_cpu_pct': 70.0,
                                 'aurora_read_p90': 9.0, 'valkey_keys': 100 + i})
    quiet = monitor.summarise_window(int(base * 1000), int((base + 25) * 1000))
    loud = monitor.summarise_window(int((base + 100) * 1000),
                                    int((base + 125) * 1000))
    check('summary.quiet_cpu', quiet.get('aurora_cpu_pct_avg'), 2.0)
    check('summary.loud_cpu', loud.get('aurora_cpu_pct_avg'), 70.0)
    check('summary.loud_p90', loud.get('aurora_read_p90_avg'), 9.0)
    # keys is reported as growth over the window, not an average
    check('summary.keys_delta', loud.get('valkey_keys_delta'), 3.0)
    assert quiet['samples'] and loud['samples'], (quiet, loud)


def test_phases_match_the_migration_sequence_in_the_script():
    """Three phases, one per step of the initial-load handoff.

    This has drifted twice, both times silently, and both times the page kept
    offering a button that did something other than what it said:
      - the script was renumbered and the phase map was not, so step 3 ran the
        CDC catch-up while the tab called it the cache-read load;
      - and a fourth phase offered a cache-read run of its own, which could be
        pressed before the catch-up had converged - reading a cache that was
        knowingly behind.
    The cache-read load is the second half of step 3 for that reason. The ids
    here are the contract between the page, the backend and the script, so they
    are pinned rather than described.
    """
    client = monitor_client()
    body = client.get('/api/monitor/phases').get_json()
    ids = [p['id'] for p in body['phases']]
    check('phases.ids', ids, ['1', '2', '3'])
    watch = {p['id']: p['watch'] for p in body['phases']}
    # 1 database-only with CDC stopped, 2 snapshot load, 3 catch-up then reads
    check('phases.watch1', watch['1'], 'aurora')
    check('phases.watch2', watch['2'], 'valkey')
    check('phases.watch3', watch['3'], 'both')
    # steps must map 1:1 onto the script's own numbering, which maps 1:1 onto the
    # orchestrator's --step
    steps = {p['id']: p['steps'] for p in body['phases']}
    check('phases.steps', [steps[i] for i in ids], ['1', '2', '3'])
    # Every phase now ends up generating application traffic.
    load = {p['id']: p.get('load') for p in body['phases']}
    for pid in ids:
        check('phases.load{}'.format(pid), load[pid], True)


def test_history_trim_keeps_the_file_not_just_memory():
    """Trimming must filter the FILE.

    Rewriting from the in-memory buffer lets any process with a partial buffer
    truncate the durable record - which is how a 180 minute retention ended up
    holding 23 minutes and deleted every finished run's numbers.
    """
    import tempfile
    monitor.reset_state()
    now = int(time.time())
    path = os.path.join(tempfile.mkdtemp(), 'hist.jsonl')
    old = monitor.CFG['history_path']
    monitor.CFG['history_path'] = path
    try:
        # On disk: 40 minutes of samples, plus one genuinely expired sample.
        with open(path, 'w') as handle:
            for i in range(40):
                handle.write(json.dumps({'t': now - (40 - i) * 60,
                                         'aurora_cpu_pct': 50}) + '\n')
            handle.write(json.dumps({'t': now - 400 * 60,
                                     'aurora_cpu_pct': 1}) + '\n')
        # In memory: only the most recent sample, as a freshly started worker
        # would have.
        monitor._history.clear()
        monitor._history.append({'t': now, 'aurora_cpu_pct': 50})

        monitor._history_trim()

        with open(path) as handle:
            kept = [json.loads(line) for line in handle if line.strip()]
        # The 40 minutes on disk survive even though memory held one sample.
        check('trim.kept', len(kept), 40)
        # The expired one is gone.
        assert all(p['t'] >= now - monitor.CFG['history_minutes'] * 60
                   for p in kept), 'expired sample survived'
        # Sorted and deduplicated.
        stamps = [p['t'] for p in kept]
        check('trim.sorted', stamps, sorted(stamps))
        check('trim.deduped', len(set(stamps)), len(stamps))
    finally:
        monitor.CFG['history_path'] = old


def test_finished_run_summary_is_frozen_not_recomputed():
    """A finished phase keeps its numbers after history moves past it."""
    monitor.reset_state()
    now = int(time.time())
    frozen = {'samples': 48, 'aurora_cpu_pct_avg': 68.8, 'window_sec': 240}
    fake = {
        'run_id': 'r1', 'phase': '1', 'status': 'done',
        'started_ms': (now - 7200) * 1000, 'ended_ms': (now - 6960) * 1000,
        'summary_json': json.dumps(frozen),
    }
    original = monitor._run_read
    monitor._run_read = lambda client, run_id: dict(fake)

    class FakeBroker:
        def lrange(self, *a):
            return [b'r1']

        def get(self, *a):
            return str(time.time()).encode()

        def llen(self, *a):
            return 0

        def hset(self, *a, **k):
            raise AssertionError('a frozen summary must not be rewritten')

    orig_with = monitor.with_broker
    monitor.with_broker = lambda fn: fn(FakeBroker())
    try:
        # History is empty, so a recompute would yield samples 0.
        monitor._history.clear()
        body = monitor_client().get('/api/monitor/runs').get_json()
        run = body['runs'][0]
        check('run.frozen_samples', run['summary']['samples'], 48)
        check('run.frozen_cpu', run['summary']['aurora_cpu_pct_avg'], 68.8)
        # The raw field is not leaked to the page.
        check('run.no_raw_field', 'summary_json' in run, False)
    finally:
        monitor._run_read = original
        monitor.with_broker = orig_with


def _flow_parts(target_writes, produced, capture=None, wait_pending_id=None, backlog=0):
    aurora = {'ok': True, 'state': 'ok'}
    debezium = {'ok': True, 'state': 'ok', 'running': 1, 'desired': 1,
                'capture_lag_ms': capture}
    broker = {'ok': True, 'state': 'ok', 'produced_per_sec': produced,
              'backlog': backlog, 'oldest_entry_id': wait_pending_id}
    sink = {'ok': True, 'state': 'ok', 'running': 1, 'desired': 1,
            'applied_per_sec': 0, 'consumers': 1}
    target = {'ok': True, 'state': 'ok', 'writes_per_sec': target_writes}
    return aurora, debezium, broker, sink, target


def test_bulk_load_suppresses_cdc_lag():
    """A direct load bypasses the broker, so a CDC lag figure describes nothing.

    This is the bulk-load step: writes go directly into the cache while the
    broker carries nothing.
    """
    monitor.reset_state()
    flow = monitor.build_flow(*_flow_parts(target_writes=34000, produced=0))
    check('flow.bypass_in_path', flow.get('cdc_in_path'), False)
    check('flow.bypass_context', flow.get('cdc_context'), 'bypassed')
    check('flow.bypass_lag', flow.get('cdc_lag_ms'), None)
    check('flow.bypass_source', flow.get('lag_source'), None)
    assert 'not in this' in (flow.get('lag_note') or ''), flow.get('lag_note')


def test_cdc_lag_kept_when_cdc_is_carrying_traffic():
    """Normal CDC traffic must still be measured - suppression is only for bypass."""
    monitor.reset_state()
    flow = monitor.build_flow(*_flow_parts(target_writes=200, produced=6000))
    check('flow.normal_in_path', flow.get('cdc_in_path'), True)
    check('flow.normal_context', flow.get('cdc_context'), 'steady')


def test_write_burst_lag_is_labelled_backlog_not_staleness():
    """7.2s during a write burst is queue depth, and has to read as a transient."""
    monitor.reset_state()
    now = monitor.now_ms()
    aurora, debezium, broker, sink, target = _flow_parts(
        target_writes=100, produced=3000, capture=759, backlog=42000)
    # The sink's oldest delivered-but-unacked entry has been waiting 6.4s.
    sink['oldest_pending_age_ms'] = 6400
    sink['pending'] = 42000
    flow = monitor.build_flow(aurora, debezium, broker, sink, target)
    check('flow.burst_context', flow.get('cdc_context'), 'draining')
    check('flow.burst_source', flow.get('lag_source'), 'in-flight')
    assert flow.get('cdc_lag_ms') and flow['cdc_lag_ms'] > 6000, flow.get('cdc_lag_ms')
    assert 'backlog draining' in (flow.get('lag_note') or ''), flow.get('lag_note')
    assert 'not steady-state' in (flow.get('lag_note') or ''), flow.get('lag_note')


def test_history_series_covers_read_and_write_percentiles_per_source():
    """Each source needs p50/p90/p99 of its own, not a shared pair."""
    for key in ('aurora_read_p50', 'aurora_read_p90', 'aurora_read_p99',
                'aurora_write_p50', 'aurora_write_p90', 'aurora_write_p99',
                'valkey_read_p50', 'valkey_read_p90', 'valkey_read_p99'):
        assert key in monitor.HISTORY_SERIES, key
    # Valkey write percentiles are deliberately absent: the app never writes to
    # the cache, so there is no client-side histogram to take them from, and
    # ElastiCache does not expose server-side percentiles. The average is there.
    assert 'valkey_write_p90' not in monitor.HISTORY_SERIES
    assert 'valkey_write_us' in monitor.HISTORY_SERIES
    # 120 minutes of view must fit inside retention.
    assert monitor.CFG['history_minutes'] >= 120, monitor.CFG['history_minutes']


def _seed_history(points):
    monitor._history.clear()
    for p in points:
        full = {k: None for k in monitor.HISTORY_SERIES}
        full.update(p)
        monitor._history.append(full)


def test_percentile_average_is_weighted_by_operation_count():
    """A ramp-up interval serving 10 slow reads must not outweigh steady state.

    Unweighted, these two samples average to (40 + 1) / 2 = 20.5 ms, which is
    nothing like what the 200,000 reads actually experienced.
    """
    base = int(time.time())
    _seed_history([
        # 6 points because summarise_window drops the first two.
        {'t': base + 0, 'aurora_read_p50': 40.0, 'aurora_read_samples': 10},
        {'t': base + 1, 'aurora_read_p50': 40.0, 'aurora_read_samples': 10},
        {'t': base + 2, 'aurora_read_p50': 40.0, 'aurora_read_samples': 10},
        {'t': base + 3, 'aurora_read_p50': 1.0, 'aurora_read_samples': 100000},
        {'t': base + 4, 'aurora_read_p50': 1.0, 'aurora_read_samples': 100000},
        {'t': base + 5, 'aurora_read_p50': 1.0, 'aurora_read_samples': 100000},
    ])
    s = monitor.summarise_window((base) * 1000, (base + 5) * 1000)
    # weighted: (40*10 + 1*300000) / 300010 -> ~1.0013
    assert 1.0 <= s['aurora_read_p50_avg'] <= 1.01, s['aurora_read_p50_avg']
    check('summary.ops_reported', s.get('aurora_read_p50_ops'), 300010)
    check('summary.coverage', s.get('aurora_read_p50_n'), 4)


def test_cdc_lag_average_declares_its_coverage():
    """Averaging only the samples that had a value reports the worst moments."""
    base = int(time.time())
    pts = [{'t': base + i, 'cdc_backlog': 5} for i in range(20)]
    # Lag observable in just two samples, both large.
    pts[7]['cdc_lag_ms'] = 26700
    pts[9]['cdc_lag_ms'] = 5000
    _seed_history(pts)
    s = monitor.summarise_window(base * 1000, (base + 19) * 1000)
    check('lag.n', s.get('cdc_lag_ms_n'), 2)
    check('lag.max', s.get('cdc_lag_ms_max'), 26700)
    assert 'only 2 of' in s.get('cdc_lag_note', ''), s.get('cdc_lag_note')
    assert 'moments the pipeline was behind' in s['cdc_lag_note']


def test_cdc_lag_absent_reports_nothing_rather_than_a_note():
    """No lag samples means no average and no commentary.

    The UI already shows "not measurable this phase" in the CDC lag row, so a
    paragraph repeating it underneath added no information.
    """
    base = int(time.time())
    _seed_history([{'t': base + i, 'cdc_backlog': 0} for i in range(10)])
    s = monitor.summarise_window(base * 1000, (base + 9) * 1000)
    check('lag.absent_avg', s.get('cdc_lag_ms_avg'), None)
    check('lag.absent_note', s.get('cdc_lag_note'), None)


def test_retired_notes_are_stripped_from_frozen_summaries():
    """Summaries frozen in an earlier session still carry the retired text."""
    old = {
        'samples': 48,
        'cpu_note': 'window shorter than 3 min: CloudWatch CPU has not caught up',
        'cdc_lag_note': 'not measurable during this run: the sink kept the stream '
                        'drained, so nothing was ever in flight to time.',
        'aurora_cpu_pct_avg': 68.4,
    }
    cleaned = monitor.strip_retired_notes(dict(old))
    check('retired.cpu_note', cleaned.get('cpu_note'), None)
    check('retired.lag_note', cleaned.get('cdc_lag_note'), None)
    # Real measurements are untouched.
    check('retired.keeps_data', cleaned.get('aurora_cpu_pct_avg'), 68.4)
    check('retired.keeps_samples', cleaned.get('samples'), 48)
    # The note that was NOT retired survives.
    kept = monitor.strip_retired_notes(
        {'cdc_lag_note': 'measured in only 3 of 48 samples: lag is observable '
                         'while events are still queued'})
    assert 'measured in only 3 of 48' in kept['cdc_lag_note'], kept
    # Non-dicts pass through rather than raising.
    check('retired.none', monitor.strip_retired_notes(None), None)


def test_short_window_carries_no_cpu_caveat():
    base = int(time.time())
    _seed_history([{'t': base + i, 'aurora_cpu_pct': 40.0} for i in range(4)])
    s = monitor.summarise_window(base * 1000, (base + 3) * 1000)
    check('short.window_sec', s.get('window_sec'), 3)
    check('short.no_cpu_note', s.get('cpu_note'), None)


def test_unweighted_fields_still_average_plainly():
    base = int(time.time())
    _seed_history([{'t': base + i, 'aurora_cpu_pct': 10.0 * (i + 1)} for i in range(6)])
    s = monitor.summarise_window(base * 1000, (base + 5) * 1000)
    # points[2:] -> 30,40,50,60 -> avg 45
    check('cpu.avg', s.get('aurora_cpu_pct_avg'), 45.0)


def test_cpu_peak_comes_from_the_maximum_statistic():
    """Peak must not be the max of 60-second averages.

    A one-minute average flattens a burst: during the bulk load the averages sat
    at ~2% while CloudWatch's per-minute Maximum was 12.6%. Reporting the max of
    the averaged series as "peak" understated every spike on the page.
    """
    base = int(time.time())
    _seed_history([{'t': base + i, 'aurora_cpu_pct': 2.0,
                    'aurora_cpu_max_pct': 12.6,
                    'valkey_engine_cpu_pct': 9.6,
                    'valkey_engine_cpu_max_pct': 20.2} for i in range(6)])
    s = monitor.summarise_window(base * 1000, (base + 5) * 1000)
    check('peak.aurora_avg', s.get('aurora_cpu_pct_avg'), 2.0)
    check('peak.aurora_max', s.get('aurora_cpu_max_pct_max'), 12.6)
    check('peak.valkey_avg', s.get('valkey_engine_cpu_pct_avg'), 9.6)
    check('peak.valkey_max', s.get('valkey_engine_cpu_max_pct_max'), 20.2)
    # The averaged series must no longer be the source of a peak figure.
    check('peak.no_avg_derived_max', s.get('aurora_cpu_pct_max'), None)


def test_cpu_is_captioned_with_the_capacity_it_was_measured_against():
    """2% CPU during a bulk load is real but unreadable without the denominator.

    CPUUtilization is a share of provisioned capacity, and Serverless v2 scales
    down slowly, so a phase following a heavy one inherits capacity it does not
    need. Without the caption the reading looks like a broken collector.
    """
    monitor.reset_state()
    base = int(time.time())
    # Capacity stayed at 32 ACU after an earlier heavy phase; 4 ACU seen earlier.
    _seed_history(
        [{'t': base - 200 + i, 'aurora_cpu_pct': 1.6, 'aurora_capacity_acu': 4.0}
         for i in range(4)]
        + [{'t': base + i, 'aurora_cpu_pct': 2.0, 'aurora_capacity_acu': 32.0}
           for i in range(6)])
    s = monitor.summarise_window(base * 1000, (base + 5) * 1000)
    note = s.get('aurora_cpu_note') or ''
    assert '32 ACU' in note, note
    # 2% of 32 ACU is ~16% of 4 ACU: state that rather than leaving 2% bare.
    assert '16%' in note, note
    assert '4 ACU' in note, note

    # When capacity is already at the observed baseline there is nothing to
    # explain, so no scaling clause.
    monitor.reset_state()
    _seed_history([{'t': base + i, 'aurora_cpu_pct': 70.0, 'aurora_capacity_acu': 4.0}
                   for i in range(6)])
    s2 = monitor.summarise_window(base * 1000, (base + 5) * 1000)
    assert 'would read' not in (s2.get('aurora_cpu_note') or ''), s2.get('aurora_cpu_note')


def test_dataflow_returns_a_block_per_table():
    """The panel shows every table in the link, so dataflow returns a tables[]
    array; each block carries its own rows, key prefix and sync tally. The
    back-compat single-table fields point at the first table."""
    monitor.reset_state()

    class Cur:
        def execute(self, sql, args=None):
            self._sql = sql

        def fetchone(self):
            if 'MAX(' in self._sql:
                return (1000,)
            return (10,)

        def fetchall(self):
            if 'information_schema.columns' in self._sql:
                return [('id', 'bigint', 'PRI'), ('sku', 'varchar', ''),
                        ('price', 'decimal', ''), ('stock_count', 'int', '')]
            return [(999, 'sku-999', decimal.Decimal('1.00'), 1)]

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass

    saved = (monitor.mysql_conn, monitor.with_target)
    monitor.mysql_conn = lambda: Conn()
    # cache is grouped per table, then by id
    monitor.with_target = lambda fn: {
        monitor.CFG['table']: {999: {'price': '1.00', 'stock_count': '1'}}}
    try:
        out = monitor.collect_dataflow(limit=3)
    finally:
        (monitor.mysql_conn, monitor.with_target) = saved

    check('df.has_tables', isinstance(out.get('tables'), list), True)
    check('df.one_table', len(out['tables']), 1)
    t0 = out['tables'][0]
    check('df.table_name', t0['table'], monitor.CFG['table'])
    check('df.key_prefix', t0['key_prefix'], monitor.CFG['table'] + ':')
    check('df.row_id', t0['rows'][0]['id'], 999)
    check('df.row_synced', t0['rows'][0]['state'], 'synced')
    check('df.synced_tally', t0['synced'], 1)
    # No full-table scan: no COUNT(*), no information_schema row estimate.
    check_true('df.note_reconcile', '/reconcile' in out['note'])
    # back-compat single-table fields still present, pointing at the first table
    check('df.compat_table', out['table'], monitor.CFG['table'])
    check('df.compat_rows', out['rows'][0]['id'], 999)


def test_sample_ids_samples_by_position_not_by_id_value():
    """Sparse ids break value-based sampling; position-based sampling survives it.

    Spreading probe points across the id range returned 2 distinct ids out of 40
    on the real table, because ~99% of the range is an empty gap. This must pick
    an OFFSET into the primary key instead.
    """
    monitor.reset_state()
    executed = []

    class Cur:
        def execute(self, sql, args=None):
            executed.append((' '.join(sql.split()), args))
            self._sql = sql

        def fetchone(self):
            return (6179765,)

        def fetchall(self):
            return [(5,), (5,), (77,)]          # duplicates collapse

    ids = monitor.sample_ids(Cur(), 'products', n=3, row_count=6179765)
    check('sample.dedup', ids, [5, 77])
    sql, args = executed[-1]
    assert 'OFFSET' in sql, sql
    assert 'UNION ALL' not in sql, sql             # the broken value-based shape
    check('sample.round_trips', len(executed), 1)  # row_count supplied, so no count
    check('sample.limit', args[0], 3)
    assert 0 <= args[1] <= 6179765 - 3 - 1, args   # anchor stays in range


def test_sample_ids_handles_an_empty_table():
    class Cur:
        def execute(self, sql, args=None):
            self._sql = sql

        def fetchone(self):
            return (0,)

        def fetchall(self):
            return []

    check('sample.empty', monitor.sample_ids(Cur(), 'products', n=5), [])


def _probe_with(entries_seq, cache_returns):
    """Run run_probe() against fake Aurora / broker / cache."""
    monitor.reset_state()
    monitor._probe_state.clear()
    calls = {'entries': list(entries_seq)}

    class FakeCur:
        def execute(self, sql, args=None):
            self.sql = sql
        def fetchone(self):
            return (1004648913,)
    class FakeConn:
        def cursor(self):
            return FakeCur()
        def close(self):
            pass

    class FakeTarget:
        def hget(self, key, field):
            return cache_returns.pop(0) if cache_returns else None

    def fake_entries():
        return calls['entries'].pop(0) if calls['entries'] else None

    originals = (monitor.mysql_conn, monitor.get_target,
                 monitor.stream_entries_added, monitor.CFG['probe_timeout_ms'])
    monitor.mysql_conn = lambda: FakeConn()
    monitor.get_target = lambda: FakeTarget()
    monitor.stream_entries_added = fake_entries
    monitor.CFG['probe_timeout_ms'] = 60          # keep the test fast
    try:
        return monitor.run_probe()
    finally:
        (monitor.mysql_conn, monitor.get_target,
         monitor.stream_entries_added, monitor.CFG['probe_timeout_ms']) = originals


def test_probe_miss_blames_the_sink_when_the_broker_saw_it():
    """Captured but not applied is a slow sink, not lost data - say which."""
    # entries-added climbs (Debezium captured it); the cache never matches.
    result = _probe_with([100, 101, 101, 101, 101, 101, 101], [])
    check('probe.ok', result['ok'], False)
    check('probe.hop', result.get('stalled_hop'), 'sink')
    assert 'captured' in result['error'], result['error']
    assert result.get('captured_ms') is not None
    assert 'nothing is lost' in (result.get('note') or ''), result.get('note')


def test_probe_miss_blames_debezium_when_the_broker_saw_nothing():
    result = _probe_with([100] * 8, [])
    check('probe.ok2', result['ok'], False)
    check('probe.hop2', result.get('stalled_hop'), 'debezium')
    assert 'did not capture' in result['error'], result['error']


def test_probe_miss_says_so_when_the_broker_is_unreadable():
    """No broker reading means no attribution - do not guess a culprit."""
    result = _probe_with([None] * 8, [])
    check('probe.ok3', result['ok'], False)
    check('probe.hop3', result.get('stalled_hop'), None)
    assert 'could not read the broker' in result['error'], result['error']


def test_dataflow_does_not_scan_the_table_by_default():
    """The monitor must not add scan load to the database it is measuring.

    COUNT(*) reads the whole primary key and the coverage sampler's OFFSET walk
    reads about half the table. Left on, they put million-row spikes into
    aurora rows_read/s and real CPU onto the cluster whose offload is the point
    of the demo.
    """
    monitor.reset_state()
    executed = []

    class Cur:
        def execute(self, sql, args=None):
            executed.append(' '.join(sql.split()))
            self._sql = sql

        def fetchone(self):
            if 'MIN(`id`)' in self._sql:
                return (1, 1000)
            if 'table_rows' in self._sql:
                return (5785714,)
            return (10,)

        def fetchall(self):
            if 'information_schema.columns' in self._sql:
                return [('id', 'bigint', 'PRI'), ('sku', 'varchar', ''),
                        ('price', 'decimal', ''), ('stock_count', 'int', '')]
            return [(999, 'sku-999', decimal.Decimal('1.00'), 1)]

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass

    class Target:
        def hgetall(self, key):
            return {}

    original_conn, original_target = monitor.mysql_conn, monitor.with_target
    monitor.mysql_conn = lambda: Conn()
    monitor.with_target = lambda fn: fn(Target())
    try:
        out = monitor.collect_dataflow(limit=3)
    finally:
        monitor.mysql_conn, monitor.with_target = original_conn, original_target

    sql = ' | '.join(executed)
    check_true('df.no_count_star', 'COUNT(*)' not in sql)
    check_true('df.no_offset_walk', 'OFFSET' not in sql)
    check_true('df.no_table_rows_scan', 'table_rows' not in sql)
    # The note points at reconcile for an exact full-table count.
    check_true('df.note_points_at_reconcile', '/reconcile' in out['note'])


def test_row_throughput_excludes_the_monitors_own_table_scan():
    """The monitor's COUNT(*) reads the whole table, so a sample covering it
    reported millions of rows/s that no workload produced. Those samples are
    dropped from the row fields only, not from CPU or anything else."""
    monitor.reset_state()
    now = int(time.time())
    pts = []
    for i in range(8):
        pts.append({
            't': now - (8 - i) * 5,
            'aurora_rows_read_per_sec': 34000,
            'aurora_cpu_pct': 40.0,
            'row_scan': False,
        })
    # One sample includes the 6.18M-row scan.
    pts[5]['aurora_rows_read_per_sec'] = 6180000
    pts[5]['row_scan'] = True
    with monitor._history_lock:
        monitor._history.clear()
        monitor._history.extend(pts)
    s = monitor.summarise_window((now - 60) * 1000, now * 1000)
    # Peak reflects the workload, not the monitor
    check('rows.peak', s.get('aurora_rows_read_per_sec_max'), 34000.0)
    check('rows.avg', s.get('aurora_rows_read_per_sec_avg'), 34000.0)
    # CPU still uses every sample, including the scan one
    check('rows.cpu_unaffected_n', s.get('aurora_cpu_pct_n'), len(pts) - 2)


def test_rate_ignores_samples_taken_too_close_together():
    """A burst counter divided by a few milliseconds invents an absurd rate.

    Innodb_rows_read jumps by a whole statement's worth of rows at once. The 2s
    dashboard poll and the 5s history sampler share this state, so they sometimes
    run milliseconds apart -- which reported 11.3M rows/s during a bulk load
    that was really doing ~34K.
    """
    monitor.reset_state()
    check('rate.first', monitor.rate('rows', 1000, now=100.0), None)
    # 5s later: 50,000 rows -> 10,000/s
    check('rate.normal', monitor.rate('rows', 51000, now=105.0), 10000.0)
    # 20ms later a big statement completes. Naively that is 12.5M/s; instead the
    # previous rate stands and the baseline is not moved.
    check('rate.too_soon', monitor.rate('rows', 301000, now=105.02), 10000.0)
    # The next real interval measures from the UNMOVED baseline (105.0), so the
    # rows are still counted rather than lost: 300,000 over 5s.
    check('rate.baseline_kept', monitor.rate('rows', 351000, now=110.0), 60000.0)


def test_rate_still_reports_none_on_reset():
    monitor.reset_state()
    check('rate.r1', monitor.rate('c', 500, now=10.0), None)
    check('rate.r2', monitor.rate('c', 700, now=12.0), 100.0)
    # counter went backwards (restart): not a measurement
    check('rate.backwards', monitor.rate('c', 5, now=14.0), None)


def test_pct_from_buckets():
    bounds = [50.0, 100.0, 200.0, 400.0]
    # 10 samples: 5 in <=50us, 4 in <=100us, 1 in <=200us
    counts = [5, 4, 1, 0]
    check('pct.p50', monitor.pct_from_buckets(bounds, counts, 50), 0.05)
    check('pct.p90', monitor.pct_from_buckets(bounds, counts, 90), 0.1)
    check('pct.p99', monitor.pct_from_buckets(bounds, counts, 99), 0.2)
    check('pct.empty', monitor.pct_from_buckets(bounds, [0, 0, 0, 0], 90), None)
    check('pct.no_bounds', monitor.pct_from_buckets([], [1], 90), None)


def test_app_percentiles_are_windowed_not_cumulative():
    """The window must come from the DIFFERENCE of two snapshots.

    The app's own percentiles are lifetime figures, so a phase that just started
    would be dominated by whatever came before it.
    """
    monitor.reset_state()
    bounds = [50.0, 100.0, 200.0, 400.0, 800.0]
    calls = []

    def fake_http(url):
        calls.append(url)
        if len(calls) == 1:
            # A long history of fast Valkey reads and nothing on Aurora.
            return {'cache': {'bounds_us': bounds, 'buckets': [1000, 0, 0, 0, 0],
                              'count': 1000, 'p90_ms': 0.05},
                    'db': {'bounds_us': bounds, 'buckets': [0, 0, 0, 0, 0], 'count': 0},
                    'write': {'bounds_us': bounds, 'buckets': [0, 0, 0, 0, 0], 'count': 0}}
        # Since then: reads moved to Aurora and they are slow (800us bucket).
        return {'cache': {'bounds_us': bounds, 'buckets': [1000, 0, 0, 0, 0],
                          'count': 1000, 'p90_ms': 0.05},
                'db': {'bounds_us': bounds, 'buckets': [0, 0, 0, 0, 10],
                       'count': 10, 'p90_ms': 0.8},
                'write': {'bounds_us': bounds, 'buckets': [0, 0, 0, 0, 0], 'count': 0}}

    monitor.CFG['demo_app_url'] = 'http://app:8088'
    original = monitor.http_json
    monitor.http_json = fake_http
    try:
        first = monitor._demo_app_metrics()
        # Nothing to diff against yet: no window, and it says so.
        check('app.first_no_window', first.get('valkey_read_p90'), None)
        assert 'no traffic' in first['note'] or 'previous poll' in first['note'], first['note']

        second = monitor._demo_app_metrics()
        # Valkey did nothing in the interval -> no window for it...
        check('app.valkey_idle', second.get('valkey_read_p90'), None)
        check('app.valkey_samples', second.get('valkey_read_samples'), 0)
        # ...while Aurora's 10 new slow reads DO produce a window.
        check('app.aurora_p90_windowed', second.get('aurora_read_p90'), 0.8)
        check('app.aurora_samples', second.get('aurora_read_samples'), 10)
        # The cumulative figure is kept but plainly labelled.
        check('app.valkey_cumulative', second.get('valkey_read_p90_cumulative'), 0.05)
        assert '?buckets=1' in calls[0], calls[0]
    finally:
        monitor.http_json = original
        monitor.CFG['demo_app_url'] = ''


def test_history_sample_collects_in_parallel():
    """A sample must cost the slowest collector, not the sum of all of them.

    Run serially, six collectors at ~5s each turned a "every 5s" history into a
    point every ~32s, and the interval reported to the UI was wrong.
    """
    monitor.reset_state()
    calls = []

    def slow(name):
        def fn():
            calls.append(name)
            time.sleep(0.30)
            return {}
        return fn

    originals = {n: getattr(monitor, 'collect_' + n)
                 for n in ('aurora', 'target', 'broker', 'sink', 'debezium')}
    for name in originals:
        setattr(monitor, 'collect_' + name, slow(name))
    original_flow = monitor.build_flow
    monitor.build_flow = lambda *a, **k: {}
    try:
        started = time.time()
        point = monitor.history_sample()
        elapsed = time.time() - started
    finally:
        for name, fn in originals.items():
            setattr(monitor, 'collect_' + name, fn)
        monitor.build_flow = original_flow

    check('history.all_collectors_ran', len(calls), 5)
    # Serial would be >=1.5s; parallel is one collector's cost plus overhead.
    assert elapsed < 0.9, 'history_sample took {:.2f}s - collectors ran serially'.format(elapsed)
    # Every declared series is present, even when a collector returns nothing.
    for series in monitor.HISTORY_SERIES:
        assert series in point, 'missing series ' + series


def test_cloudwatch_age_explains_stale_values():
    """A 76% CPU average beside 4 reads/s is stale, not broken - report the age."""
    monitor.reset_state()
    monitor._CW_TS.clear()
    check('cw.age_unknown', monitor.cw_age('AWS/RDS', 'CPUUtilization'), None)
    monitor._CW_TS[('AWS/RDS', 'CPUUtilization')] = time.time() - 95
    monitor._CW_TS[('AWS/RDS', 'SelectLatency')] = time.time() - 40
    # The oldest in the group is used: it is the worst-case staleness on that card.
    age = monitor.cw_age('AWS/RDS', 'CPUUtilization', 'SelectLatency')
    assert 94 <= age <= 96, age
    # A metric from another namespace must not be picked up by name alone.
    check('cw.age_ns_isolated', monitor.cw_age('AWS/ElastiCache', 'CPUUtilization'), None)


def test_history_refuses_to_record_a_stale_lag():
    """A stale probe must not be sampled as if it described this moment.

    The probe result stays readable for probe_stale_sec so the card is not blank.
    But recording it every 5s repeats one old reading across a whole phase: a
    single 26s probe turned a ~20ms pipeline into a "CDC lag avg 5.0s" in the
    phase summary, with a backlog of 59 entries flatly contradicting it.
    """
    monitor.reset_state()
    originals = {n: getattr(monitor, 'collect_' + n)
                 for n in ('aurora', 'target', 'broker', 'sink', 'debezium')}
    for name in originals:
        setattr(monitor, 'collect_' + name, lambda: {})
    original_flow = monitor.build_flow
    try:
        # Stale probe: readable on the card, but not a measurement of now.
        monitor.build_flow = lambda *a, **k: {'cdc_lag_ms': 26700, 'lag_stale': True,
                                             'lag_source': 'probe'}
        check('history.stale_lag_dropped', monitor.history_sample()['cdc_lag_ms'], None)

        # A fresh probe is a real reading and must be kept.
        monitor.build_flow = lambda *a, **k: {'cdc_lag_ms': 18, 'lag_stale': False,
                                             'lag_source': 'probe'}
        check('history.fresh_probe_kept', monitor.history_sample()['cdc_lag_ms'], 18)

        # In-flight is observed live and carries no staleness flag at all.
        monitor.build_flow = lambda *a, **k: {'cdc_lag_ms': 53, 'lag_source': 'in-flight'}
        check('history.inflight_kept', monitor.history_sample()['cdc_lag_ms'], 53)
    finally:
        for name, fn in originals.items():
            setattr(monitor, 'collect_' + name, fn)
        monitor.build_flow = original_flow


def test_history_survives_a_restart():
    """A redeploy mid-session must not wipe the comparison.

    The buffer is memory-only, so without persistence "keep 120 minutes" is a
    requirement the collector cannot meet once the container is recreated.
    """
    import tempfile
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, 'nested', 'hist.jsonl')
    original_path, original_appends = monitor.CFG['history_path'], monitor._history_appends
    monitor.CFG['history_path'] = path
    monitor._history_appends = 0
    try:
        now = int(time.time())
        recent = {'t': now - 60, 'aurora_cpu_pct': 71.0, 'aurora_read_p90': 9.3}
        stale = {'t': now - (monitor.CFG['history_minutes'] * 60 + 600),
                 'aurora_cpu_pct': 5.0}
        # Writing creates the directory rather than silently dropping samples.
        monitor._history_write(stale)
        monitor._history_write(recent)
        assert os.path.exists(path), 'sample file was not created'

        # Simulate a restart: buffer empty, then restore from disk.
        monitor._history.clear()
        restored = monitor._history_restore()
        check('hist.restored_count', restored, 1)      # the stale one is dropped
        points = list(monitor._history)
        check('hist.restored_len', len(points), 1)
        check('hist.restored_value', points[0]['aurora_read_p90'], 9.3)

        # A truncated final line (abrupt kill) costs one sample, not the file.
        with open(path, 'a') as handle:
            handle.write('{"t": 999, "aurora_cpu')
        monitor._history.clear()
        monitor._history_restore()
        check('hist.survives_torn_line', len(list(monitor._history)), 1)
    finally:
        monitor.CFG['history_path'] = original_path
        monitor._history_appends = original_appends
        monitor._history.clear()


def test_history_persistence_failure_is_not_fatal():
    """A read-only or full disk must not stop the sampler."""
    original_path, original_appends = monitor.CFG['history_path'], monitor._history_appends
    monitor.CFG['history_path'] = '/proc/definitely-not-writable/hist.jsonl'
    monitor._history_appends = 0
    try:
        monitor._history_write({'t': int(time.time()), 'aurora_cpu_pct': 1.0})
        # Flagged as unpersisted so /history can say so, and no exception escaped.
        check('hist.marked_unpersisted', monitor._history_appends, -1)
    finally:
        monitor.CFG['history_path'] = original_path
        monitor._history_appends = original_appends


def test_downsample_keeps_gaps_as_gaps():
    """A bucket with no readings must stay None, never collapse to zero."""
    now = int(time.time())
    points = [{'t': now + i, 'aurora_read_p90': None if i < 4 else 10.0}
              for i in range(8)]
    for key in monitor.HISTORY_SERIES:
        for p in points:
            p.setdefault(key, None)
    out = monitor.downsample(points, 2)
    check('ds.buckets', len(out), 2)
    check('ds.gap_stays_none', out[0]['aurora_read_p90'], None)
    check('ds.value_averaged', out[1]['aurora_read_p90'], 10.0)


def _envelope(row_id, price, stock, committed_ms, op='u'):
    return {'key': json.dumps({'id': row_id}),
            'value': json.dumps({'payload': {
                'op': op, 'id': row_id, 'price': price, 'stock_count': stock,
                'source': {'ts_ms': committed_ms}}})}


def test_writeflow_shows_events_still_in_flight_and_applied_ones():
    """The newest stream entries ARE the writes flowing to Valkey.

    dataflow watches the highest ids, which a random-id load never touches; this
    reads the stream instead, so it moves whenever writes move.
    """
    monitor.reset_state()
    now = monitor.now_ms()

    class FakeBroker:
        def xrevrange(self, stream, hi=None, lo=None, count=None):
            return [
                # newest: cache already matches -> applied
                ('{}-0'.format(now - 40), _envelope(500, '10.50', '99', now - 60)),
                # older: cache still has the previous value -> in flight
                ('{}-0'.format(now - 90), _envelope(501, '20.00', '50', now - 120)),
                # a delete whose key is gone from the cache -> applied
                ('{}-0'.format(now - 150), _envelope(502, None, None, now - 200, op='d')),
            ]

    class FakeTarget:
        def hgetall(self, key):
            return {'products:500': {'price': '10.50', 'stock_count': '99'},
                    'products:501': {'price': '19.00', 'stock_count': '50'}}.get(key, {})

    ob, ot = monitor.with_broker, monitor.with_target
    monitor.with_broker = lambda fn: fn(FakeBroker())
    monitor.with_target = lambda fn: fn(FakeTarget())
    try:
        out = monitor.collect_writeflow(aurora={}, broker_stats={}, sink={}, target={})
        check('wf.count', len(out['events']), 3)
        check('wf.applied', out['applied'], 2)
        check('wf.in_flight', out['in_flight'], 1)
        first = out['events'][0]
        check('wf.id', first['id'], 500)
        check('wf.op_word', first['op_word'], 'update')
        check('wf.state_applied', first['state'], 'applied')
        # capture latency comes from the entry id minus the commit timestamp
        check('wf.capture', first['capture_ms'], 20)
        stale = out['events'][1]
        check('wf.state_inflight', stale['state'], 'in flight')
        check('wf.cache_value_shown', stale['cache_price'], '19.00')
        check('wf.event_value_shown', stale['event_price'], '20.00')
        # a delete counts as applied precisely when the key is gone
        check('wf.delete_applied', out['events'][2]['state'], 'applied')
    finally:
        monitor.with_broker, monitor.with_target = ob, ot


def test_writeflow_explains_an_empty_stream_instead_of_a_blank_table():
    """Empty is the healthy resting state AND what a bulk load looks like."""
    monitor.reset_state()

    class FakeBroker:
        def xrevrange(self, stream, hi=None, lo=None, count=None):
            return []

    ob = monitor.with_broker
    monitor.with_broker = lambda fn: fn(FakeBroker())
    try:
        out = monitor.collect_writeflow(aurora={}, broker_stats={}, sink={}, target={})
        check('wf.empty', out['events'], [])
        # An empty tail must not read as broken or idle: say it is healthy and
        # say why. Kept short - the long version was too long to be useful.
        note = out['events_note']
        assert 'healthy' in note, note
        assert 'behind' in note, note
        assert len(note) < 200, 'note is too long again: %d chars' % len(note)
    finally:
        monitor.with_broker = ob


def test_writeflow_stage_rates_are_the_reliable_signal():
    """The stream tail is usually empty, so the rates show that writes are flowing.

    Measured live: at ~3,200 writes/s the tail was empty on every sample because
    the sink deletes entries as it applies them. These four numbers move together
    or the pipeline is not moving.
    """
    monitor.reset_state()

    class FakeBroker:
        def xrevrange(self, stream, hi=None, lo=None, count=None):
            return []

    ob = monitor.with_broker
    monitor.with_broker = lambda fn: fn(FakeBroker())
    try:
        out = monitor.collect_writeflow(
            aurora={'writes_per_sec': 3200.0},
            broker_stats={'produced_per_sec': 3190.0},
            sink={'applied_per_sec': 3185.0},
            target={'writes_per_sec': 3201.0})
        names = [s['name'] for s in out['stages']]
        check('wf.stages', names, ['Aurora', 'Debezium', 'Sink', 'Valkey'])
        check('wf.stage_rate', out['stages'][0]['rate'], 3200.0)
        check('wf.moving', out['moving'], True)

        # Idle noise (health checks, our own probe) must not read as "flowing".
        idle = monitor.collect_writeflow(
            aurora={'writes_per_sec': 1.4}, broker_stats={'produced_per_sec': 0.0},
            sink={'applied_per_sec': 0.0}, target={'writes_per_sec': None})
        check('wf.idle', idle['moving'], False)
        check('wf.idle_note', idle['note'], 'no writes in the last few seconds')
    finally:
        monitor.with_broker = ob


def test_probe_history_accumulates_newest_first():
    """Row-level proof that survives the sink deleting stream entries."""
    monitor.reset_state()
    monitor._probe_state.clear()
    check('probe.hist_empty', monitor.probe_history(), [])
    for i in range(3):
        with monitor._lock:
            monitor._probe_state.setdefault(
                'history', collections.deque(maxlen=12)).appendleft(
                    {'at': time.time(), 'lag_ms': 10 + i, 'row_id': 42, 'ok': True})
    hist = monitor.probe_history()
    check('probe.hist_len', len(hist), 3)
    check('probe.newest_first', hist[0]['lag_ms'], 12)


def test_bulkload_progress_is_live_then_goes_stale():
    """A rewrite-in-place load is invisible from outside, so the loader reports."""
    monitor.reset_state()
    now = monitor.now_ms()
    state = {'updated': now - 2000}

    class FakeTarget:
        def hgetall(self, key):
            return {'started_at_ms': str(now - 10000),
                    'updated_at_ms': str(state['updated']),
                    'workers': '2', 'expected_rows': '1000',
                    'pk_lo': '1000000000', 'pk_hi': '1006740913', 'table': 'products',
                    'w0_rows': '300', 'w0_last_pk': '1000300000',
                    'w1_rows': '200', 'w1_last_pk': '1003200000', 'w1_done': '1'}

    ot = monitor.with_target
    monitor.with_target = lambda fn: fn(FakeTarget())
    try:
        out = monitor.collect_bulkload()
        check('bl.active', out['active'], True)
        check('bl.rows', out['rows_loaded'], 500)
        check('bl.pct', out['pct'], 50.0)
        check('bl.workers', len(out['workers']), 2)
        check('bl.workers_done', out['workers_done'], 1)
        check('bl.last_pk', out['workers'][0]['last_pk'], '1000300000')
        check('bl.rate', out['rows_per_sec'], 62)   # 500 rows / 8s elapsed, banker's rounding
        check('bl.range', out['pk_range'], '1000000000..1006740913')
        assert 'does not touch the broker' in out['note'], out['note']

        # An old run must not be presented as one in progress.
        state['updated'] = now - 120000
        stale = monitor.collect_bulkload()
        check('bl.stale_inactive', stale['active'], False)
        assert 'finished' in stale.get('finished_note', ''), stale
    finally:
        monitor.with_target = ot


def test_bulkload_absent_says_so():
    monitor.reset_state()

    class FakeTarget:
        def hgetall(self, key):
            return {}

    ot = monitor.with_target
    monitor.with_target = lambda fn: fn(FakeTarget())
    try:
        out = monitor.collect_bulkload()
        check('bl.none_active', out['active'], False)
        assert 'no bulk load' in out['note'], out['note']
    finally:
        monitor.with_target = ot


def test_dataflow_random_sample_seeks_and_dedupes():
    """Random ids must come from index seeks, not ORDER BY RAND().

    RAND() would scan every row of a 6M-row table, which is exactly the kind of
    query this page must never issue. Seeking forward from random points can
    select the same row twice, so duplicates are collapsed.
    """
    monitor.reset_state()
    executed = []

    seq = iter([(500, 'sku-500', decimal.Decimal('10.00'), 5),
                (500, 'sku-500', decimal.Decimal('10.00'), 5),   # repeat -> dropped
                (900, 'sku-900', decimal.Decimal('20.00'), 7)])

    class Cur:
        def execute(self, sql, args=None):
            executed.append((' '.join(sql.split()), args))
            self._sql = sql
        def fetchone(self):
            if 'MIN(`id`)' in self._sql:
                return (1, 1006740913)
            if 'table_rows' in self._sql:
                return (6179765,)
            return next(seq, None)
        def fetchall(self):
            if 'information_schema.columns' in self._sql:
                return [('id', 'bigint', 'PRI'), ('sku', 'varchar', ''),
                        ('price', 'decimal', ''), ('stock_count', 'int', '')]
            return []

    class Conn:
        def cursor(self):
            return Cur()
        def close(self):
            pass

    original_conn, original_target = monitor.mysql_conn, monitor.with_target
    monitor.mysql_conn = lambda: Conn()
    monitor.with_target = lambda fn: {monitor.CFG['table']: {500: {'price': '10.00', 'stock_count': '5'}}}
    try:
        out = monitor.collect_dataflow(limit=3, sample='random')
    finally:
        monitor.mysql_conn, monitor.with_target = original_conn, original_target

    sql_text = ' '.join(s for s, _ in executed)
    assert 'RAND()' not in sql_text.upper(), 'must not use ORDER BY RAND()'
    # Random must walk by ROW POSITION. Seeking from a random id value collapses
    # onto cluster boundaries when the id space is sparse.
    assert 'LIMIT 1 OFFSET' in sql_text, sql_text
    offsets = [a[1] for s, a in executed if a and 'OFFSET' in s]
    assert offsets and all(1 <= o <= monitor.CFG['random_sample_stride']
                           for o in offsets), offsets
    ids = [r['id'] for r in out['rows']]
    check('dataflow.random_dedup', ids, [500, 900])
    check('dataflow.random_synced', out['synced'], 1)
    check('dataflow.random_pending', out['pending'], 1)


def test_dataflow_newest_sample_is_still_the_default():
    monitor.reset_state()
    executed = []

    class Cur:
        def execute(self, sql, args=None):
            executed.append(' '.join(sql.split()))
            self._sql = sql
        def fetchone(self):
            return (1, 999) if 'MIN(`id`)' in self._sql else (10,)
        def fetchall(self):
            if 'information_schema.columns' in self._sql:
                return [('id', 'bigint', 'PRI'), ('sku', 'varchar', ''),
                        ('price', 'decimal', ''), ('stock_count', 'int', '')]
            return [(999, 'sku-999', decimal.Decimal('1.00'), 1)]

    class Conn:
        def cursor(self):
            return Cur()
        def close(self):
            pass

    original_conn, original_target = monitor.mysql_conn, monitor.with_target
    monitor.mysql_conn = lambda: Conn()
    monitor.with_target = lambda fn: {}
    try:
        monitor.collect_dataflow(limit=5)
    finally:
        monitor.mysql_conn, monitor.with_target = original_conn, original_target
    joined = ' '.join(executed)
    assert 'ORDER BY `id` DESC' in joined, joined
    # The random ROW sample walks the table with OFFSET on the full row; the
    # default must not. (Coverage sampling also uses OFFSET, but selects only id.)
    row_walk = [s for s in executed if 'OFFSET' in s and 'price' in s]
    check('df.no_random_row_walk', row_walk, [])


def test_phases_declare_whether_cdc_lag_is_relevant():
    """CDC lag is only meaningful where something is read from the cache.

    Writes replicate through CDC in every phase, but in "DB load only" and
    "Bulk insert" nothing is served from the cache, so a lag figure has no
    use there - and a healthy sink drains to empty, so it renders
    as an empty panel asking you to run a probe.
    """
    phases = monitor.RUN_PHASES
    check('phase1.cdc', phases['1'].get('cdc'), False)   # DB load only
    check('phase2.cdc', phases['2'].get('cdc'), False)   # bulk insert, no broker
    check('phase3.cdc', phases['3'].get('cdc'), True)    # catch-up, then reads
    # The flag has to reach the UI, which is what actually gates the readout.
    response = monitor_client().get('/api/monitor/phases')
    check('phases.http', response.status_code, 200)
    by_id = {p['id']: p for p in response.get_json()['phases']}
    check('phases.count', len(by_id), 3)
    for pid, expected in (('1', False), ('2', False), ('3', True)):
        check('phases.api.{}.cdc'.format(pid), by_id[pid].get('cdc'), expected)
        assert by_id[pid].get('title'), pid


def test_scaled_to_zero_is_paused_not_broken():
    """desiredCount 0 is an operator decision; desired>0 with nothing up is a fault.

    The snapshot handoff stops Debezium on purpose. Calling that 'down' turned a
    scripted demo step into what looked like an outage.
    """
    check('ecs.paused', monitor.ecs_state(0, 0), ('paused', 'paused'))
    check('ecs.crashed', monitor.ecs_state(1, 0), ('bad', 'down'))
    check('ecs.starting', monitor.ecs_state(2, 1), ('starting', 'starting'))
    check('ecs.up', monitor.ecs_state(2, 2), ('ok', 'up'))
    # Turning CDC back on: desired 1, nothing running yet, one task provisioning.
    # Must not read as an outage for the ~2 min Debezium takes to boot.
    check('ecs.coming_up', monitor.ecs_state(1, 0, 1), ('starting', 'starting'))
    # paused must not be reported as unreachable
    check('ecs.paused_hop', monitor.hop_state({'state': 'paused', 'ok': True}), 'paused')


def test_paused_cdc_reads_as_planned_not_as_an_outage():
    aurora = {'state': 'ok'}
    debezium = {'state': 'paused', 'label': 'paused', 'ok': True}
    broker = {'state': 'ok', 'produced_per_sec': 0, 'backlog': 0}
    sink = {'state': 'ok', 'applied_per_sec': 0}
    target = {'state': 'ok'}
    flow = monitor.build_flow(aurora, debezium, broker, sink, target, probe={})
    check('flow.paused_hop', flow['hops']['debezium'], 'paused')
    check('flow.paused_list', flow.get('paused'), ['debezium'])
    msg = flow['message']
    # Stated as a product state, not narrated. The audience wants "ECDL off",
    # not an explanation of binlog accumulation.
    check('flow.paused_msg', msg, 'ECDL off')
    assert 'down' not in msg, msg
    assert 'authoritative' not in msg, msg
    assert 'binlog' not in msg, msg

    # A real failure still reads as a failure.
    broken = monitor.build_flow(aurora, {'state': 'bad', 'ok': False}, broker, sink,
                               target, probe={})
    assert 'down' in broken['message'], broken['message']
    assert 'authoritative' not in broken['message'], broken['message']

    # Paused while a backlog is still draining says both things.
    draining = monitor.build_flow(
        aurora, debezium, {'state': 'ok', 'produced_per_sec': 0, 'backlog': 24000},
        sink, target, probe={})
    assert 'ECDL off' in draining['message'], draining['message']
    assert '24,000' in draining['message'], draining['message']


def test_turning_cdc_back_on_is_its_own_state():
    """Between 'ECDL off' and 'flowing' there is a boot window.

    Debezium's task takes a couple of minutes to provision and start streaming.
    Reporting that as 'debezium down' mid-demo reads as a fault, so it gets its
    own state and its own wording.
    """
    aurora = {'state': 'ok'}
    debezium = {'state': 'starting', 'label': 'starting', 'ok': True}
    broker = {'state': 'ok', 'produced_per_sec': 0, 'backlog': 0}
    sink = {'state': 'ok', 'applied_per_sec': 0}
    target = {'state': 'ok'}
    flow = monitor.build_flow(aurora, debezium, broker, sink, target, probe={})
    check('flow.starting_msg', flow['message'], 'Turning on ECDL')
    check('flow.starting_list', flow.get('starting'), ['debezium'])
    assert 'down' not in flow['message'], flow['message']

    # With a delta waiting to replay, say how much.
    catching_up = monitor.build_flow(
        aurora, debezium, {'state': 'ok', 'produced_per_sec': 0, 'backlog': 812000},
        sink, target, probe={})
    assert 'Turning on ECDL' in catching_up['message'], catching_up['message']
    assert '812,000' in catching_up['message'], catching_up['message']

    # A genuinely dead component still takes precedence over a starting one.
    with_failure = monitor.build_flow(
        aurora, debezium, broker, {'state': 'bad', 'ok': False}, target, probe={})
    assert 'down' in with_failure['message'], with_failure['message']


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as exc:                           # noqa: BLE001
            FAILURES.append('{}: raised {}: {}'.format(t.__name__, type(exc).__name__, exc))
    if FAILURES:
        print('FAIL ({} problem(s)):'.format(len(FAILURES)))
        for f in FAILURES:
            print('  -', f)
        return 1
    print('ok: {} monitor tests passed'.format(len(tests)))
    return 0

# --- per-table breakdown ----------------------------------------------------
# A link routes each table to its own broker stream. The monitor used to hold one
# stream_key, so a three-table link reported one table and the other two were
# invisible -- including their backlog, their DLQ and their retries.
def test_table_from_stream_matches_the_go_sink_rule():
    """Same last-dot-segment rule as tableFromStream in tools/go-sink/streams.go.

    The segment COUNT differs per engine, which is why the rule reads the LAST
    segment: any fixed position would be wrong for three of the four.
    """
    cases = [
        ('data:checkout.Inventory.orders', 'orders'),          # MySQL, 3 parts
        ('data:checkout.public.orders', 'orders'),              # PostgreSQL, 3
        ('data:checkout.Inventory.dbo.orders', 'orders'),       # SQL Server, 4
        ('data:checkout.ADMIN.ORDERS3W', 'ORDERS3W'),           # Oracle, 2 + upper
        ('data:orders', 'orders'),                              # bare harness name
    ]
    for stream, want in cases:
        check('table_from.' + stream, monitor.table_from_stream(stream), want)


def test_parse_streams_accepts_the_sinks_own_format():
    """One string configures both sides, so they cannot disagree."""
    got = monitor.parse_streams('data:l.Inventory.orders=orders,'
                                'data:l.Inventory.customers')
    check('parse.count', len(got), 2)
    check('parse.explicit', got[0]['table'], 'orders')
    check('parse.derived', got[1]['table'], 'customers')


def test_parse_streams_falls_back_to_the_single_stream_config():
    got = monitor.parse_streams('', 'data:l.Inventory.products', 'products')
    check('parse.fallback', [s['table'] for s in got], ['products'])


def test_parse_streams_drops_a_duplicate_rather_than_failing():
    """A monitor that reports nothing because one entry repeats is worse than one
    that reports the rest."""
    got = monitor.parse_streams('data:a.b.orders,data:a.b.orders')
    check('parse.dedupe', len(got), 1)


def test_key_prefix_is_per_table():
    """The sink writes evt.Source.Table + ':' + pk, so each table has its own
    namespace. Looking every id up under one prefix would report every other
    table's rows as missing."""
    check('prefix.other', monitor.key_prefix_for('orders'), 'orders:')
    # The configured table keeps its explicit override.
    check('prefix.configured', monitor.key_prefix_for(monitor.CFG['table']),
          monitor.CFG['key_prefix'])


def test_backlog_of_is_one_formula_in_one_place():
    """This lived in three places (broker card, integrity block, history
    sampler). Three copies of one formula is how two of them end up disagreeing.
    """
    check('backlog_of.both', monitor.backlog_of({'group_lag': 2000, 'pending': 30}), 2030)
    check('backlog_of.raw_lag', monitor.backlog_of({'lag': 5, 'pending': 1}), 6)
    check('backlog_of.empty', monitor.backlog_of({}), 0)
    check('backlog_of.none', monitor.backlog_of(None), 0)


def test_undelivered_uses_read_counter_when_lag_goes_null():
    """Valkey returns XINFO 'lag' as NULL once entries are XDEL'd, which the sink
    does after every ack. Deriving undelivered from that null silently collapses
    the backlog to "unacked only" mid-drain. entries_added - entries_read is the
    same number and stays populated, so it must be used."""
    # lag is null, but the counters say 500,000 are still undelivered.
    g = {'entries_added': 600000, 'entries_read': 100000, 'group_lag': None,
         'pending': 2000}
    check('undelivered.from_counters', monitor.undelivered_of(g), 500000)
    check('backlog.from_counters', monitor.backlog_of(g), 502000)
    # Caught up: added == read, null lag -> 0, not a fabricated number.
    caught = {'entries_added': 93597716, 'entries_read': 93597716,
              'group_lag': None, 'pending': 0}
    check('undelivered.caught_up', monitor.undelivered_of(caught), 0)
    check('backlog.caught_up', monitor.backlog_of(caught), 0)
    # No counters at all -> fall back to the lag field.
    check('undelivered.fallback', monitor.undelivered_of({'lag': 42}), 42)


class _OrphanBroker:
    """A fully caught-up group whose sink left entries un-XDEL'd: the group has
    delivered and acked everything (added == read, pending 0, delivered == head)
    yet 155 old entries remain resident, so first-entry is ~7h old. The card
    must not read that leftover as a 7h backlog."""

    S = 'data:l.Inventory.catalog'

    def xinfo_stream(self, stream):
        return {'entries-added': 93597716, 'length': 155,
                'last-generated-id': '1790352496827-0',
                'first-entry': ('1765000000000-0', {'x': '1'})}

    def xinfo_groups(self, stream):
        return [{'name': monitor.CFG['consumer_group'], 'consumers': 1,
                 'pending': 0, 'lag': None, 'entries-read': 93597716,
                 'last-delivered-id': '1790352496827-0'}]

    def xpending(self, stream, group):
        return {'pending': 0, 'min': None}


def test_oldest_entry_not_reported_when_the_stream_is_caught_up():
    """Regression: 155 un-deleted leftovers made 'oldest entry' read ~7h next to
    'backlog 0'. When nothing is outstanding, there is nothing waiting, so oldest
    entry is None (renders as a dash), while the raw resident count is kept."""
    monitor.reset_state()
    b = _OrphanBroker()
    row = monitor.read_one_stream(b, {'stream': _OrphanBroker.S, 'table': 'catalog'})
    check('orphan.backlog_zero', row['backlog'], 0)
    check('orphan.oldest_suppressed', row.get('oldest_entry_age_ms'), None)
    check('orphan.resident_kept', row.get('resident_length'), 155)
    check('orphan.length_still_true', row.get('length'), 155)


def test_outstanding_backlog_uses_resident_length_when_counters_go_null():
    """The load test proved the counter form is not enough: under heavy XDEL churn
    BOTH 'lag' and entries-read return NULL, so the counter backlog collapsed to
    ~10K while the real queue (resident length) was ~1M. Resident length must be used.
    """
    # 1M resident, group behind the head, both counters null -> use length.
    big = monitor.outstanding_backlog(
        1026308, '100-0', '999-0', 14000,
        {'entries_added': 5000000, 'entries_read': None, 'lag': None})
    check('outstanding.resident_wins', big, 1026308)
    # Caught up (delivered to head, nothing unacked) -> 0, ignoring ~155 leftovers.
    caught = monitor.outstanding_backlog(
        155, '9-0', '9-0', 0,
        {'entries_added': 100, 'entries_read': 100, 'lag': None})
    check('outstanding.caught_up_zero', caught, 0)
    # Counters act as a floor when length is momentarily unavailable/smaller.
    floored = monitor.outstanding_backlog(
        0, '1-0', '9-0', 50, {'entries_added': 500, 'entries_read': 350})
    check('outstanding.counter_floor', floored, 200)


class _LoadBroker:
    """Under load: the group is behind the head, both XINFO counters are NULL,
    ~1M entries are resident, and there are ancient acked leftovers whose ids sit
    at/below last-delivered. The oldest UNDELIVERED entry is ~5s old."""

    S = 'data:l.Inventory.catalog'

    def xinfo_stream(self, stream):
        return {'entries-added': 5000000, 'length': 1000000,
                'last-generated-id': '1790352000000-0'}

    def xinfo_groups(self, stream):
        return [{'name': monitor.CFG['consumer_group'], 'consumers': 1,
                 'pending': 14000, 'lag': None, 'entries-read': None,
                 'last-delivered-id': '1790351000000-0'}]

    def xpending(self, stream, group):
        return {'pending': 14000, 'min': '1790351000001-0'}

    def xrange(self, name, min=None, max=None, count=None):
        # Oldest entry after last-delivered = the oldest genuinely undelivered
        # entry, ~5s old. Orphans (older, id <= last-delivered) are excluded.
        return [('{}-0'.format(monitor.now_ms() - 5000), {'x': '1'})]


def test_backlog_and_oldest_entry_are_right_under_load():
    """End-to-end of the load fix: backlog reflects the ~1M resident queue (not
    the null-counter ~14K), and oldest entry is the oldest undelivered (~5s), not
    a 7h orphan."""
    monitor.reset_state()
    row = monitor.read_one_stream(_LoadBroker(), {'stream': _LoadBroker.S,
                                                  'table': 'catalog'})
    check('load.backlog_is_resident', row['backlog'], 1000000)
    age = row.get('oldest_entry_age_ms')
    check_true('load.oldest_is_recent', age is not None and 3000 <= age <= 15000)


def test_worst_verdict_takes_the_worst_table_not_an_average():
    """Integrity is a correctness question: one table losing entries is not
    offset by five that are fine."""
    check('worst.loss', monitor.worst_verdict(['ok', 'ok', 'loss']), 'loss')
    check('worst.stalled', monitor.worst_verdict(['retrying', 'stalled']), 'stalled')
    check('worst.all_ok', monitor.worst_verdict(['ok', 'ok']), 'ok')
    check('worst.empty', monitor.worst_verdict([]), 'ok')


def test_worst_verdict_order_matches_the_single_table_precedence():
    """The roll-up must not invent a different ranking from integrity_verdict."""
    check('worst.order', list(monitor.VERDICT_ORDER),
          ['loss', 'stalled', 'stuck', 'pressure', 'retrying', 'ok'])


class _MultiBroker:
    """A broker carrying two streams: orders is healthy, customers is backed up
    and has dead-lettered entries."""

    STREAMS = {
        'data:l.Inventory.orders': {'entries-added': 1000, 'length': 0,
                                    'last-generated-id': '1-0'},
        'data:l.Inventory.customers': {'entries-added': 500, 'length': 200,
                                       'last-generated-id': '2-0'},
    }
    GROUPS = {
        'data:l.Inventory.orders': [{'name': 'ecdl-sink', 'consumers': 2,
                                     'pending': 0, 'lag': 0,
                                     'entries-read': 1000,
                                     'last-delivered-id': '1-0'}],
        # entries-read is set so entries_added - entries_read == lag (500-350=150).
        # Valkey maintains that invariant; the backlog helper now derives
        # undelivered from the counters (lag goes NULL after XDELs), so the
        # fixture must honour it or it is testing an impossible stream.
        'data:l.Inventory.customers': [{'name': 'ecdl-sink', 'consumers': 2,
                                        'pending': 50, 'lag': 150,
                                        'entries-read': 350,
                                        'last-delivered-id': '2-0'}],
    }

    def xinfo_stream(self, stream):
        if stream not in self.STREAMS:
            raise RuntimeError('no such key')
        return self.STREAMS[stream]

    def xinfo_groups(self, stream):
        return self.GROUPS.get(stream, [])

    def xpending(self, stream, group):
        return {'pending': self.GROUPS[stream][0]['pending'], 'min': None}

    def xpending_range(self, stream, group, min=None, max=None, count=None):
        return []

    def xinfo_consumers(self, stream, group):
        return [{'name': 'c1', 'idle': 10}]

    def exists(self, key):
        return key == 'dlq:data:l.Inventory.customers'

    def xlen(self, key):
        return 3 if key == 'dlq:data:l.Inventory.customers' else 0

    def xrevrange(self, key, hi=None, lo=None, count=None):
        if key.startswith('dlq:'):
            return [('9-0', {'reason': 'target refused', 'deliveries': '5'})]
        return []

    def info(self, section=None):
        if section == 'memory':
            return {'used_memory': 1000, 'maxmemory': 10000,
                    'maxmemory_policy': 'noeviction'}
        if section == 'stats':
            return {'evicted_keys': 0}
        return {'used_memory': 1000, 'instantaneous_ops_per_sec': 50,
                'connected_clients': 3}


def _with_two_streams(fn):
    """Run fn with CFG pointing at the two-stream fixture."""
    monitor.reset_state()
    saved = (monitor.CFG['streams'], monitor.with_broker)
    monitor.CFG['streams'] = ('data:l.Inventory.orders=orders,'
                              'data:l.Inventory.customers=customers')
    monitor.with_broker = lambda f: f(_MultiBroker())
    try:
        return fn()
    finally:
        monitor.CFG['streams'], monitor.with_broker = saved


def test_broker_reports_every_stream_not_just_the_first():
    """The bug: two of three tables were invisible, backlog included."""
    out = _with_two_streams(monitor.collect_broker)
    check('broker.stream_count', len(out['streams']), 2)
    check('broker.tables', sorted(out['tables']), ['customers', 'orders'])


def test_broker_backlog_is_the_sum_across_tables():
    """The page reads broker.backlog; with several tables it has to be the link
    total or a backed-up table is hidden."""
    out = _with_two_streams(monitor.collect_broker)
    # customers: lag 150 + pending 50 = 200. orders: 0.
    check('broker.backlog_total', out['backlog'], 200)


def test_broker_keeps_every_field_the_page_reads():
    """A per-table refactor that drops a scalar renders as an em-dash, not an
    error, so it would look fine and show nothing."""
    out = _with_two_streams(monitor.collect_broker)
    for field in ('stream', 'length', 'entries_added', 'produced_per_sec',
                  'backlog', 'group_lag', 'pending', 'group', 'used_memory',
                  'oldest_entry_age_ms', 'state', 'label'):
        check_true('broker.has.' + field, field in out)


def test_broker_worst_wait_wins_not_an_average():
    """One stalled table must not be hidden behind healthy ones."""
    out = _with_two_streams(monitor.collect_broker)
    rows = {r['table']: r for r in out['streams']}
    check('broker.per_table_backlog', rows['customers']['backlog'], 200)
    check('broker.healthy_table', rows['orders']['backlog'], 0)


def test_a_missing_stream_is_reported_against_its_table_only():
    """With several tables, one that does not exist yet must not hide the health
    of the others."""
    monitor.reset_state()
    saved = (monitor.CFG['streams'], monitor.with_broker)
    monitor.CFG['streams'] = ('data:l.Inventory.orders=orders,'
                              'data:l.Inventory.missing=missing')
    monitor.with_broker = lambda f: f(_MultiBroker())
    try:
        out = monitor.collect_broker()
    finally:
        monitor.CFG['streams'], monitor.with_broker = saved
    rows = {r['table']: r for r in out['streams']}
    check('broker.missing_ok', rows['missing']['ok'], False)
    check('broker.other_still_ok', rows['orders']['ok'], True)
    check('broker.state_warn', out['state'], 'warn')
    check_true('broker.names_it', 'missing' in (out.get('group_note') or ''))


def test_integrity_verdict_is_per_table_and_names_the_bad_one():
    """"loss" on a six-table link is not actionable without knowing which."""
    out = _with_two_streams(monitor.collect_integrity)
    rows = {r['table']: r for r in out['tables']}
    check('integ.bad_table', rows['customers']['verdict'], 'loss')
    check('integ.good_table', rows['orders']['verdict'], 'ok')
    check('integ.rollup', out['verdict'], 'loss')
    check('integ.affected', out.get('affected_tables'), ['customers'])
    check_true('integ.headline_names_it', 'customers' in out['headline'])


def test_integrity_totals_sum_across_tables():
    out = _with_two_streams(monitor.collect_integrity)
    check('integ.dlq_total', out['dead_lettered'], 3)
    check('integ.backlog_total', out['backlog'], 200)
    check('integ.undelivered', out['undelivered'], 150)
    check('integ.unacked', out['unacked'], 50)


def test_integrity_keeps_every_field_the_page_reads():
    out = _with_two_streams(monitor.collect_integrity)
    for field in ('verdict', 'state', 'label', 'headline', 'dead_lettered',
                  'evicted_keys', 'backlog', 'drained_per_sec', 'retrying',
                  'max_attempt_seen', 'max_deliveries', 'oldest_unacked_ms',
                  'broker_used_pct', 'broker_used_memory', 'broker_maxmemory',
                  'maxmemory_policy', 'consumers_active', 'consumers_registered',
                  'dlq_key', 'dlq_sample', 'retry_sample'):
        check_true('integ.has.' + field, field in out)


def test_dlq_sample_says_which_table_the_entry_came_from():
    out = _with_two_streams(monitor.collect_integrity)
    sample = out['dlq_sample']
    check_true('integ.sample_present', bool(sample))
    check('integ.sample_table', sample[0].get('table'), 'customers')


def test_eviction_is_link_wide_not_attributed_to_a_table():
    """evicted_keys is a server counter, so it cannot say whose entries went --
    it is reported for every table."""
    out = _with_two_streams(monitor.collect_integrity)
    check_true('integ.evicted_toplevel', 'evicted_keys' in out)
    for row in out['tables']:
        check_true('integ.no_per_table_evicted', 'evicted_keys' not in row)


def test_per_table_rates_do_not_clobber_each_other():
    """Rate keys are flat strings in one shared dict. A single
    'broker.entries_added' would have every table overwriting the others'
    baseline and all of them reporting incorrect values."""
    monitor.reset_state()
    _with_two_streams(monitor.collect_broker)
    keys = [k for k in monitor._samples if k.startswith('broker.entries_added')]
    check('rate.per_stream_keys', len(keys), 2)
    check_true('rate.key_named', any('orders' in k for k in keys))


def test_tables_endpoint_joins_the_three_cards_into_one_row_per_table():
    """The three cards each carry a per-table list, which is right for each card
    and wrong for "which table is the problem"."""
    def run():
        broker = monitor.safe(monitor.collect_broker)
        sink = {'tables': [
            {'stream': 'data:l.Inventory.orders', 'table': 'orders',
             'ok': True, 'pending': 0, 'group_lag': 0, 'applied_per_sec': 10.0},
            {'stream': 'data:l.Inventory.customers', 'table': 'customers',
             'ok': True, 'pending': 50, 'group_lag': 150,
             'oldest_pending_age_ms': 9000},
        ]}
        integrity = monitor.safe(monitor.collect_integrity)
        return monitor.collect_tables(broker=broker, sink=sink,
                                      integrity=integrity)

    out = _with_two_streams(run)
    check('tables.count', out['count'], 2)
    # Sorted worst first, so the problem table is the first thing read.
    check('tables.worst_first', out['tables'][0]['table'], 'customers')
    check('tables.worst_verdict', out['worst_verdict'], 'loss')
    row = out['tables'][0]
    for field in ('table', 'stream', 'backlog', 'dead_lettered',
                  'produced_per_sec', 'applied_per_sec', 'verdict',
                  'broker_wait_ms'):
        check_true('tables.has.' + field, field in row)
    check('tables.wait', row['broker_wait_ms'], 9000)


def test_single_table_link_still_reports_exactly_what_it_used_to():
    """The common case must not change shape just because the plural one works."""
    monitor.reset_state()
    saved = (monitor.CFG['streams'], monitor.with_broker)
    monitor.CFG['streams'] = 'data:l.Inventory.orders=orders'
    monitor.with_broker = lambda f: f(_MultiBroker())
    try:
        out = monitor.collect_broker()
    finally:
        monitor.CFG['streams'], monitor.with_broker = saved
    check('single.count', len(out['streams']), 1)
    check('single.stream', out['stream'], 'data:l.Inventory.orders')
    check('single.backlog', out['backlog'], 0)
    check('single.state', out['state'], 'ok')


def test_capture_lag_takes_the_worst_table():
    """One Debezium task reads one binlog for every table, so if any table's
    newest captured event is old, that is the link's capture latency."""
    monitor.reset_state()
    now = monitor.now_ms()

    class Broker:
        def xrevrange(self, stream, hi=None, lo=None, count=None):
            # orders captured 20ms ago, customers 5s ago.
            age = 20 if 'orders' in stream else 5000
            return [('{}-0'.format(now), {'value': json.dumps(
                {'payload': {'source': {'ts_ms': now - age}, 'after': {'id': 1}}})})]
        def hgetall(self, key):
            return {}
        def info(self, section=None):
            return {}

    saved = (monitor.CFG['streams'], monitor.with_broker, monitor.ecs_service,
             monitor.log_error_count)
    monitor.CFG['streams'] = ('data:l.Inventory.orders=orders,'
                              'data:l.Inventory.customers=customers')
    monitor.with_broker = lambda f: f(Broker())
    monitor.ecs_service = lambda name: {'desired': 1, 'running': 1, 'pending': 0}
    monitor.log_error_count = lambda group: {}
    try:
        out = monitor.collect_debezium()
    finally:
        (monitor.CFG['streams'], monitor.with_broker, monitor.ecs_service,
         monitor.log_error_count) = saved
    check('capture.worst', out['capture_lag_ms'], 5000)
    check('capture.per_table_count', len(out['capture_by_table']), 2)


def test_stale_leftover_is_not_reported_as_current_capture_lag():
    """A never-XDEL'd leftover arrived hours ago carrying a frozen capture lag.
    It must NOT be reported as the live figure -- that made the pipeline look
    ~130s behind at idle and distorted the in-flight CDC lag (the ~150s peak)."""
    monitor.reset_state()
    now = monitor.now_ms()
    old_arrived = now - 2 * 3600 * 1000        # arrived 2 hours ago

    class Broker:
        def xrevrange(self, stream, hi=None, lo=None, count=None):
            # committed 130s before it arrived -> stored capture_lag 130000,
            # but the entry itself arrived 2h ago, so it is not current.
            return [('{}-0'.format(old_arrived), {'value': json.dumps(
                {'payload': {'source': {'ts_ms': old_arrived - 130000},
                             'after': {'id': 1}}})})]
        def hgetall(self, key):
            return {}
        def info(self, section=None):
            return {}

    saved = (monitor.CFG['streams'], monitor.with_broker, monitor.ecs_service,
             monitor.log_error_count)
    monitor.CFG['streams'] = 'data:l.Inventory.catalog=catalog'
    monitor.with_broker = lambda f: f(Broker())
    monitor.ecs_service = lambda name: {'desired': 1, 'running': 1, 'pending': 0}
    monitor.log_error_count = lambda group: {}
    try:
        out = monitor.collect_debezium()
    finally:
        (monitor.CFG['streams'], monitor.with_broker, monitor.ecs_service,
         monitor.log_error_count) = saved
    check('capture.stale_not_current', out.get('capture_lag_ms'), None)
    check_true('capture.stale_flagged', out['capture_by_table'][0].get('stale') is True)
    check_true('capture.stale_note', 'stale' in (out.get('capture_note') or ''))


# --- per-link log groups ----------------------------------------------------
# These assert the STACK-based names. The bug they exist to catch was silent:
# link_config() built the groups from the ECS cluster ('ecdl-<link>') while the
# template declares them from the stack ('ecdl-<link>-cdc'), so every per-link
# log read asked for a group that does not exist, filter_log_events raised
# ResourceNotFound, soft() ignored it, and the card rendered a dash -- making
# "no errors" and "wrong log group" indistinguishable.
def _link_config_with(params, outputs_by_stack):
    saved = (monitor._stack_parameters, monitor._stack_outputs)
    monitor._stack_parameters = lambda stack: params

    def outputs(stack):
        if stack in outputs_by_stack:
            return outputs_by_stack[stack]
        raise RuntimeError('Stack with id {} does not exist'.format(stack))

    monitor._stack_outputs = outputs
    try:
        return monitor.link_config('shop')
    finally:
        (monitor._stack_parameters, monitor._stack_outputs) = saved


def test_link_log_groups_follow_the_stack_not_the_cluster():
    overlay = _link_config_with({'ClusterName': 'ecdl-shop'}, {})
    check('logs.debezium', overlay['debezium_log_group'],
          '/ecs/ecdl-shop-cdc/debezium')
    check('logs.sink', overlay['sink_log_group'], '/ecs/ecdl-shop-cdc/sink')


def test_loader_log_group_has_no_subpath():
    # infra/ecdl-loader-ecs.yaml declares '/ecs/${AWS::StackName}' bare, unlike
    # the CDC groups which carry a per-container sub-path.
    overlay = _link_config_with({'ClusterName': 'ecdl-shop'}, {})
    check('logs.loader', overlay['loader_log_group'], '/ecs/ecdl-shop-loader')


def test_loader_log_group_prefers_the_stack_output():
    overlay = _link_config_with(
        {'ClusterName': 'ecdl-shop'},
        {'ecdl-shop-loader': {'LogGroup': '/ecs/renamed-loader-group'}})
    check('logs.loader_from_output', overlay['loader_log_group'],
          '/ecs/renamed-loader-group')


def test_missing_loader_stack_still_yields_a_loader_group():
    # A 'cdc' mode link never had a loader stack. The derived name is kept so the
    # UI can say "no loader has run for this link" instead of showing nothing.
    overlay = _link_config_with({'ClusterName': 'ecdl-shop'}, {})
    check_true('logs.loader_present', bool(overlay.get('loader_log_group')))


# --- reading component logs -------------------------------------------------
class _FakeLogs(object):
    """Stands in for the CloudWatch Logs client."""

    def __init__(self, events=None, streams=None, raise_code=None,
                 empty_pages=0):
        self._events = events or []
        self._streams = streams or []
        self._raise_code = raise_code
        self.empty_pages = empty_pages
        self.calls = []

    def _maybe_raise(self):
        if not self._raise_code:
            return
        exc = RuntimeError('denied')
        exc.response = {'Error': {'Code': self._raise_code}}
        raise exc

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        self._maybe_raise()
        # CloudWatch can answer with an EMPTY page plus a nextToken while
        # matching events still exist further into the window. empty_pages
        # models that so the follow-the-token path is covered.
        if self.empty_pages > 0:
            self.empty_pages -= 1
            return {'events': [], 'nextToken': 'tok-more'}
        return {'events': self._events, 'nextToken': 'tok-2'}

    def describe_log_streams(self, **kwargs):
        self.calls.append(kwargs)
        self._maybe_raise()
        return {'logStreams': self._streams}


def _with_logs(fake, fn, *args, **kwargs):
    saved = monitor.aws
    monitor.aws = lambda service: fake
    try:
        return fn(*args, **kwargs)
    finally:
        monitor.aws = saved


def test_log_group_for_rejects_an_unknown_component():
    # The allowlist is the purpose of this check: accepting a caller-supplied group name
    # would make this route "read any log group in the account".
    try:
        monitor.log_group_for('../../etc/passwd')
        FAILURES.append('log_group_for: expected ValueError')
    except ValueError as exc:
        check_true('log_group_for.names_valid', 'debezium' in str(exc))


def test_log_group_for_maps_each_component():
    check('log_group.debezium', monitor.log_group_for('debezium'),
          monitor.CFG['debezium_log_group'])
    check('log_group.sink', monitor.log_group_for('sink'),
          monitor.CFG['sink_log_group'])
    check('log_group.loader', monitor.log_group_for('loader'),
          monitor.CFG['loader_log_group'])


def test_quote_filter_makes_free_text_literal():
    # Unquoted, '?' is OR and '-' negates, so a filter typed by a human either
    # changes meaning silently or fails with InvalidParameterException.
    check('filter.plain', monitor.quote_filter('connection refused'),
          '"connection refused"')
    check('filter.special', monitor.quote_filter('? -x'), '"? -x"')
    check('filter.escapes_quote', monitor.quote_filter('say "hi"'),
          '"say \\"hi\\""')
    check('filter.blank_is_none', monitor.quote_filter('   '), None)


def test_classify_line_severity():
    check('sev.error', monitor.classify_line('2026-01-01 ERROR could not connect'),
          'error')
    check('sev.exception', monitor.classify_line('java.lang.Exception: boom'),
          'error')
    check('sev.warn', monitor.classify_line('WARN retrying in 5s'), 'warn')
    check('sev.info', monitor.classify_line('started consumer'), 'info')


def test_task_id_from_stream():
    check('task_id.awslogs', monitor.task_id_from_stream('debezium/debezium/abc123'),
          'abc123')
    check('task_id.bare', monitor.task_id_from_stream('plain'), 'plain')


def test_log_state_from_error_distinguishes_absent_from_denied():
    def err(code):
        exc = RuntimeError('x')
        exc.response = {'Error': {'Code': code}}
        return exc

    check('state.absent', monitor.log_state_from_error(err('ResourceNotFoundException')),
          'absent')
    check('state.denied', monitor.log_state_from_error(err('AccessDeniedException')),
          'denied')
    check('state.throttled', monitor.log_state_from_error(err('ThrottlingException')),
          'throttled')
    check('state.other', monitor.log_state_from_error(RuntimeError('nope')), 'error')


def test_log_events_returns_lines_with_severity_and_task():
    now = monitor.now_ms()
    fake = _FakeLogs(events=[
        {'timestamp': now - 1000, 'message': 'started\n',
         'logStreamName': 'sink/sink/task-a'},
        {'timestamp': now - 500, 'message': 'ERROR write failed',
         'logStreamName': 'sink/sink/task-a'},
    ])
    out = _with_logs(fake, monitor.log_events, 'sink', minutes=5)
    check('logs.state', out['state'], 'ok')
    check('logs.count', out['count'], 2)
    check('logs.severity', [e['severity'] for e in out['events']],
          ['info', 'error'])
    check('logs.task', out['events'][0]['task_id'], 'task-a')
    check('logs.next', out['next'], 'tok-2')
    # Trailing newline stripped, so the UI does not render a blank line per entry.
    check('logs.message', out['events'][0]['message'], 'started')


def test_log_events_preserves_cloudwatch_order():
    # Oldest first, so a multi-line stack trace stays readable.
    now = monitor.now_ms()
    fake = _FakeLogs(events=[
        {'timestamp': now - 3000, 'message': 'Exception: boom', 'logStreamName': 's'},
        {'timestamp': now - 2999, 'message': '  at Foo.bar()', 'logStreamName': 's'},
    ])
    out = _with_logs(fake, monitor.log_events, 'debezium')
    check('logs.order', [e['message'] for e in out['events']],
          ['Exception: boom', '  at Foo.bar()'])


def test_log_events_absent_group_is_not_an_error_state():
    fake = _FakeLogs(raise_code='ResourceNotFoundException')
    out = _with_logs(fake, monitor.log_events, 'loader')
    check('logs.absent_state', out['state'], 'absent')
    check('logs.absent_events', out['events'], [])
    check_true('logs.absent_note', 'has not started' in (out.get('note') or ''))


def test_log_events_denied_says_which_permission():
    fake = _FakeLogs(raise_code='AccessDeniedException')
    out = _with_logs(fake, monitor.log_events, 'sink')
    check('logs.denied_state', out['state'], 'denied')
    check_true('logs.denied_note',
               'logs:FilterLogEvents' in (out.get('note') or ''))


def test_log_events_empty_is_distinct_from_absent():
    out = _with_logs(_FakeLogs(events=[]), monitor.log_events, 'sink', minutes=7)
    check('logs.empty_state', out['state'], 'empty')
    check_true('logs.empty_note', '7 minute' in (out.get('note') or ''))


def test_log_events_clamps_limit_and_window():
    fake = _FakeLogs(events=[])
    out = _with_logs(fake, monitor.log_events, 'sink', minutes=99999, limit=99999)
    check('logs.window_clamped', out['window_min'], monitor.LOG_MINUTES_MAX)
    check('logs.limit_clamped', out['limit'], monitor.LOG_LIMIT_MAX)
    check('logs.api_limit', fake.calls[0]['limit'], monitor.LOG_LIMIT_MAX)


def test_log_events_only_sends_a_filter_when_given_one():
    fake = _FakeLogs(events=[])
    _with_logs(fake, monitor.log_events, 'sink', filter_text='')
    check_true('logs.no_empty_filter', 'filterPattern' not in fake.calls[0])
    fake2 = _FakeLogs(events=[])
    _with_logs(fake2, monitor.log_events, 'sink', filter_text='boom')
    check('logs.filter_quoted', fake2.calls[0]['filterPattern'], '"boom"')


def test_log_streams_reports_age_and_task():
    now = monitor.now_ms()
    fake = _FakeLogs(streams=[
        {'logStreamName': 'debezium/debezium/t1', 'lastEventTimestamp': now - 4000,
         'firstEventTimestamp': now - 90000, 'storedBytes': 1234},
    ])
    out = _with_logs(fake, monitor.log_streams, 'debezium')
    check('streams.state', out['state'], 'ok')
    check('streams.task', out['streams'][0]['task_id'], 't1')
    check('streams.age', out['streams'][0]['last_event_age_sec'], 4.0)
    check('streams.newest_first', fake.calls[0]['descending'], True)


def test_log_streams_empty_group_says_no_task_started():
    out = _with_logs(_FakeLogs(streams=[]), monitor.log_streams, 'debezium')
    check('streams.empty_state', out['state'], 'empty')
    check_true('streams.empty_note', 'no task has started' in (out.get('note') or ''))


def test_log_components_explains_the_broker_has_no_logs():
    out = _with_logs(_FakeLogs(streams=[]), monitor.log_components)
    names = [c['component'] for c in out['components']]
    check('components.names', names, ['debezium', 'sink', 'loader'])
    without = {c['component']: c['note'] for c in out['without_logs']}
    check_true('components.broker_explained', 'broker' in without)
    check_true('components.broker_reason',
               'ElastiCache' in without.get('broker', ''))


SINK_STATS_LINE = ('  [30s] writes=125000 deletes=12 rate=4166/sec | per-cycle: '
                   'read=1.2ms parse=300\u00b5s write=2.1ms ack=800\u00b5s batch=2000')


def test_parse_sink_stats_reads_the_real_line_format():
    # Built from the format string at tools/go-sink/main.go:273.
    out = monitor.parse_sink_stats([SINK_STATS_LINE])
    check('sink_stats.observed', out['observed'], True)
    check('sink_stats.writes', out['writes'], 125000)
    check('sink_stats.deletes', out['deletes'], 12)
    check('sink_stats.rate', out['rate_per_sec'], 4166.0)
    check('sink_stats.batch', out['per_cycle']['batch'], 2000)


def test_parse_sink_stats_takes_the_newest_line():
    older = SINK_STATS_LINE
    newer = older.replace('writes=125000', 'writes=126000')
    out = monitor.parse_sink_stats([older, newer])
    check('sink_stats.newest', out['writes'], 126000)


def test_parse_sink_stats_absent_is_not_zero():
    # The sink prints only after a completed cycle, so an idle link has no line.
    # Reporting writes=0 would read as "the sink is broken".
    out = monitor.parse_sink_stats(['  Connected to broker', '  Workers: 8'])
    check('sink_stats.absent', out['observed'], False)
    check_true('sink_stats.no_writes_key', 'writes' not in out)


def test_count_signals_reports_matches_and_a_sample():
    lines = ['2026-01-01 ERROR Communications link failure to source',
             '2026-01-01 INFO  Connector started',
             '2026-01-01 WARN  Retrying in 5s']
    signals = {s['signal']: s for s in monitor.count_signals('debezium', lines)}
    check('signals.connect', signals['connect_trouble']['matches'], 1)
    check('signals.retry', signals['retrying']['matches'], 1)
    check('signals.restart', signals['restarted']['matches'], 1)
    check('signals.snapshot_zero', signals['snapshot']['matches'], 0)
    check_true('signals.sample', 'Communications link failure'
               in signals['connect_trouble']['newest'])


def test_log_activity_counts_levels_and_keeps_newest_error():
    now = monitor.now_ms()
    fake = _FakeLogs(events=[
        {'timestamp': now - 5000, 'message': 'INFO started', 'logStreamName': 's/s/t1'},
        {'timestamp': now - 4000, 'message': 'WARN slow', 'logStreamName': 's/s/t1'},
        {'timestamp': now - 3000, 'message': 'ERROR first bad', 'logStreamName': 's/s/t1'},
        {'timestamp': now - 1000, 'message': 'ERROR second bad', 'logStreamName': 's/s/t1'},
    ])
    out = _with_logs(fake, monitor.log_activity, 'debezium', minutes=10)
    check('activity.errors', out['levels']['error'], 2)
    check('activity.warns', out['levels']['warn'], 1)
    check('activity.scanned', out['lines_scanned'], 4)
    check('activity.newest_error', out['newest_error']['message'], 'ERROR second bad')
    check('activity.task', out['newest_error']['task_id'], 't1')


def test_log_activity_for_sink_includes_counters_and_names_what_is_absent():
    now = monitor.now_ms()
    fake = _FakeLogs(events=[
        {'timestamp': now - 1000, 'message': SINK_STATS_LINE,
         'logStreamName': 'sink/sink/t9'},
    ])
    out = _with_logs(fake, monitor.log_activity, 'sink')
    check('activity.sink_writes', out['counters']['writes'], 125000)
    # Retries and dead-letters are NOT logged by the sink (reliability.go has no
    # print statements), so the payload must point at where they really come from
    # rather than reporting zero.
    check_true('activity.retried_explained', 'retried' in out['not_in_logs'])
    check_true('activity.dlq_explained', 'dead_lettered' in out['not_in_logs'])
    check_true('activity.points_at_integrity',
               'integrity' in out['not_in_logs']['retried'])


def test_log_activity_for_debezium_points_throughput_at_the_broker():
    # Debezium does log its own throughput, so the count is reported -- but the
    # broker is still named as the authoritative live rate, because the log line
    # only appears once per interval and lags whatever is happening now.
    fake = _FakeLogs(events=[])
    out = _with_logs(fake, monitor.log_activity, 'debezium')
    check_true('activity.dbz_points_at_broker',
               'broker' in out['not_in_logs']['events_processed'])


def test_log_activity_propagates_denied_without_inventing_counts():
    fake = _FakeLogs(raise_code='AccessDeniedException')
    out = _with_logs(fake, monitor.log_activity, 'sink')
    check('activity.denied_state', out['state'], 'denied')
    check_true('activity.denied_no_levels', 'levels' not in out)
    check_true('activity.denied_no_counters', 'counters' not in out)


def test_log_activity_route_rejects_bad_component():
    client = monitor_client()
    resp = client.get('/api/monitor/logs/activity?component=whatever')
    check('activity_route.status', resp.status_code, 400)


# --- restarts from ECS stopped-task records + log cross-check ----------------
class _FakeECSRestarts(object):
    """Stands in for the ECS client for ecs_restarts()."""

    def __init__(self, stopped=None):
        self._stopped = stopped or []

    def list_tasks(self, **kwargs):
        return {'taskArns': ['arn-{}'.format(i) for i in range(len(self._stopped))]}

    def describe_tasks(self, **kwargs):
        return {'tasks': self._stopped}


def _stopped_task(age_sec, reason, exit_code):
    import datetime
    when = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=age_sec))
    return {'stoppedAt': when, 'stoppedReason': reason,
            'containers': [{'exitCode': exit_code}]}


def _with_ecs(fake, fn, *args, **kwargs):
    monitor.reset_state()          # ecs_restarts is cached; clear between cases
    saved = monitor.aws
    monitor.aws = lambda service: fake
    try:
        return fn(*args, **kwargs)
    finally:
        monitor.aws = saved


def test_ecs_restarts_counts_recent_and_surfaces_newest_reason():
    fake = _FakeECSRestarts(stopped=[
        _stopped_task(30, 'Essential container in task exited', 1),
        _stopped_task(600, 'OutOfMemoryError', 137),
    ])
    out = _with_ecs(fake, monitor.ecs_restarts, 'svc', minutes=120)
    check('restarts.count', out['restarts'], 2)
    # Newest first, so the most recent reason/exit is what the card shows.
    check('restarts.reason', out['last_restart_reason'],
          'Essential container in task exited')
    check('restarts.exit', out['last_restart_exit_code'], 1)
    check_true('restarts.sample', len(out['restart_sample']) == 2)


def test_ecs_restarts_excludes_out_of_window():
    fake = _FakeECSRestarts(stopped=[
        _stopped_task(30, 'recent', 1),
        _stopped_task(3 * 3600, 'too old', 1),   # 3h ago, window is 2h
    ])
    out = _with_ecs(fake, monitor.ecs_restarts, 'svc', minutes=120)
    check('restarts.window', out['restarts'], 1)


def test_ecs_restarts_none_stopped_is_zero_not_error():
    # A healthy service genuinely has no stopped task. That is restarts=0, a real
    # measurement, NOT an ignored failure.
    out = _with_ecs(_FakeECSRestarts(stopped=[]), monitor.ecs_restarts, 'svc')
    check('restarts.zero', out['restarts'], 0)
    check_true('restarts.no_error', 'error' not in out)


def test_log_restart_count_is_a_separate_labelled_crosscheck():
    now = monitor.now_ms()
    fake = _FakeLogs(events=[
        {'timestamp': now - 5000, 'message': 'INFO Connector started',
         'logStreamName': 'debezium/debezium/t1'},
        {'timestamp': now - 1000, 'message': 'INFO Connector started',
         'logStreamName': 'debezium/debezium/t2'},
    ])
    out = _with_logs(fake, monitor.log_restart_count, 'debezium')
    check('log_starts.count', out['log_starts'], 2)
    check_true('log_starts.newest', 'Connector started' in (out.get('log_starts_newest') or ''))
    # It must NOT be reported as the ECS restart number.
    check_true('log_starts.not_restarts', 'restarts' not in out)


def test_log_restart_count_absent_group_reports_state_not_zero():
    fake = _FakeLogs(raise_code='ResourceNotFoundException')
    out = _with_logs(fake, monitor.log_restart_count, 'sink')
    check('log_starts.absent', out['log_starts'], None)
    check('log_starts.absent_state', out.get('log_starts_state'), 'absent')


def test_collect_debezium_merges_both_restart_signals():
    # The card carries the ECS count AND the log cross-check, side by side.
    now = monitor.now_ms()
    class Broker:
        def xrevrange(self, *a, **k): return []
        def hgetall(self, *a, **k): return {}
        def info(self, *a, **k): return {}
    saved = (monitor.with_broker, monitor.ecs_service, monitor.log_error_count,
             monitor.ecs_restarts, monitor.log_restart_count, monitor.CFG['streams'])
    monitor.CFG['streams'] = 'data:l.Inventory.products=products'
    monitor.with_broker = lambda f: f(Broker())
    monitor.ecs_service = lambda name: {'desired': 1, 'running': 1, 'pending': 0}
    monitor.log_error_count = lambda group: {}
    monitor.ecs_restarts = lambda svc: {'restarts': 3, 'window_min': 120}
    monitor.log_restart_count = lambda comp: {'log_starts': 2}
    try:
        out = monitor.collect_debezium()
    finally:
        (monitor.with_broker, monitor.ecs_service, monitor.log_error_count,
         monitor.ecs_restarts, monitor.log_restart_count,
         monitor.CFG['streams']) = saved
    check('dbz.restarts', out['restarts_2h']['restarts'], 3)
    check('dbz.log_starts', out['restarts_2h']['log_starts'], 2)


class _FakeEcs(object):
    """Stands in for the ECS client for restart detection."""

    def __init__(self, stopped):
        self._stopped = stopped

    def list_tasks(self, **kwargs):
        if kwargs.get('desiredStatus') == 'STOPPED':
            return {'taskArns': ['arn:%d' % i for i in range(len(self._stopped))]}
        return {'taskArns': []}

    def describe_tasks(self, **kwargs):
        return {'tasks': self._stopped}


def _with_ecs(fake, fn, *args, **kwargs):
    saved = monitor.aws
    monitor.aws = lambda service: fake
    monitor.reset_state()  # ecs_restarts is cached; clear between cases
    try:
        return fn(*args, **kwargs)
    finally:
        monitor.aws = saved


def test_ecs_restarts_none_is_a_real_zero():
    out = _with_ecs(_FakeEcs([]), monitor.ecs_restarts, 'svc')
    check('restarts.zero', out['restarts'], 0)
    check_true('restarts.no_sample', 'restart_sample' not in out)


def test_ecs_restarts_reports_reason_exit_and_age():
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    stopped = [{
        'stoppedAt': now - datetime.timedelta(seconds=45),
        'stoppedReason': 'Essential container in task exited',
        'containers': [{'exitCode': 1}],
    }]
    out = _with_ecs(_FakeEcs(stopped), monitor.ecs_restarts, 'svc')
    check('restarts.count', out['restarts'], 1)
    check('restarts.reason', out['last_restart_reason'],
          'Essential container in task exited')
    check('restarts.exit', out['last_restart_exit_code'], 1)
    check_true('restarts.age', out['last_restart_age_sec'] < 120)
    check('restarts.sample_len', len(out['restart_sample']), 1)


def test_ecs_restarts_excludes_tasks_older_than_the_window():
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    stopped = [{
        'stoppedAt': now - datetime.timedelta(hours=5),  # window default 120m
        'stoppedReason': 'Scaling activity',
        'containers': [{'exitCode': 0}],
    }]
    out = _with_ecs(_FakeEcs(stopped), monitor.ecs_restarts, 'svc')
    check('restarts.windowed_out', out['restarts'], 0)


def test_ecs_restarts_newest_first():
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    stopped = [
        {'stoppedAt': now - datetime.timedelta(seconds=300),
         'stoppedReason': 'older', 'containers': [{'exitCode': 1}]},
        {'stoppedAt': now - datetime.timedelta(seconds=30),
         'stoppedReason': 'newer', 'containers': [{'exitCode': 1}]},
    ]
    out = _with_ecs(_FakeEcs(stopped), monitor.ecs_restarts, 'svc')
    check('restarts.newest', out['last_restart_reason'], 'newer')


class _FakeEcs(object):
    """Stands in for the ECS client for ecs_restarts()."""

    def __init__(self, stopped):
        self._stopped = stopped

    def list_tasks(self, **kwargs):
        if kwargs.get('desiredStatus') == 'STOPPED':
            return {'taskArns': ['arn:t{}'.format(i) for i in range(len(self._stopped))]}
        return {'taskArns': []}

    def describe_tasks(self, **kwargs):
        return {'tasks': self._stopped}


def _stopped_task(age_sec, reason='Essential container in task exited', exit_code=1):
    import datetime
    when = datetime.datetime.fromtimestamp(time.time() - age_sec)
    return {'stoppedAt': when, 'stoppedReason': reason,
            'containers': [{'exitCode': exit_code}]}


def _with_ecs(fake, fn, *args, **kwargs):
    saved = monitor.aws
    monitor.aws = lambda service: fake
    monitor.reset_state()  # ecs_restarts is cached; clear between cases
    try:
        return fn(*args, **kwargs)
    finally:
        monitor.aws = saved


def test_ecs_restarts_none_is_a_real_zero():
    out = _with_ecs(_FakeEcs([]), monitor.ecs_restarts, 'svc')
    check('restarts.zero', out['restarts'], 0)
    check_true('restarts.no_sample', 'restart_sample' not in out)


def test_ecs_restarts_reports_count_reason_and_recency():
    fake = _FakeEcs([
        _stopped_task(30, 'OutOfMemoryError: container killed', 137),
        _stopped_task(400, 'Essential container in task exited', 1),
    ])
    out = _with_ecs(fake, monitor.ecs_restarts, 'svc', 120)
    check('restarts.count', out['restarts'], 2)
    # Newest first: the 30s-ago crash is the last restart.
    check_true('restarts.recent', out['last_restart_age_sec'] < 60)
    check_true('restarts.reason', 'OutOfMemory' in out['last_restart_reason'])
    check('restarts.exit', out['last_restart_exit_code'], 137)
    check('restarts.sample_len', len(out['restart_sample']), 2)


def test_ecs_restarts_excludes_tasks_older_than_the_window():
    fake = _FakeEcs([
        _stopped_task(30, 'recent crash', 1),
        _stopped_task(9999, 'ancient crash', 1),
    ])
    out = _with_ecs(fake, monitor.ecs_restarts, 'svc', 5)  # 5-minute window
    check('restarts.windowed', out['restarts'], 1)
    check_true('restarts.kept_recent', 'recent crash' in out['last_restart_reason'])


def test_logs_route_rejects_a_bad_component_with_400():
    client = monitor_client()
    resp = client.get('/api/monitor/logs?component=nope')
    check('logs_route.status', resp.status_code, 400)
    check_true('logs_route.error', 'unknown component' in resp.get_json()['error'])


# --- data flow against a table that is not the demo table ---------------------
# The failure this prevents was seen live: selecting a link whose table had no
# 'sku' column made the panel report
#   OperationalError: (1054, "Unknown column 'sku' in 'field list'")
# because the query was written for the demo schema. A monitor must work
# against any table, not only one.
def _dataflow_against(columns, rows, cache=None):
    monitor.reset_state()
    executed = []

    class Cur:
        def execute(self, sql, args=None):
            executed.append(' '.join(sql.split()))
            self._sql = sql

        def fetchone(self):
            if 'MIN(' in self._sql:
                return (1, 99)
            if 'table_rows' in self._sql:
                return (2,)
            return None

        def fetchall(self):
            if 'information_schema.columns' in self._sql:
                return columns
            return rows

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass

    saved = (monitor.mysql_conn, monitor.with_target)
    monitor.mysql_conn = lambda: Conn()
    # collect_dataflow now groups cache lookups per table ({table: {id: hash}}),
    # so wrap the flat fixture cache under the configured table name.
    monitor.with_target = lambda fn: ({monitor.CFG['table']: cache} if cache else {})
    try:
        out = monitor.collect_dataflow(limit=3)
    finally:
        (monitor.mysql_conn, monitor.with_target) = saved
    return out, ' '.join(executed)


def test_dataflow_reads_the_links_own_columns_not_the_demo_ones():
    columns = [('order_id', 'bigint', 'PRI'), ('customer_ref', 'varchar', ''),
               ('amount', 'decimal', ''), ('qty', 'int', '')]
    rows = [(7, 'CUST-7', decimal.Decimal('12.50'), 2)]
    out, sql = _dataflow_against(columns, rows)
    check_true('df.selects_real_pk', 'order_id' in sql)
    check_true('df.no_hardcoded_sku', 'sku' not in sql)
    check('df.row_id', out['rows'][0]['id'], 7)
    # The recognisable text column is carried as 'label' whatever it is called.
    check('df.label', out['rows'][0]['label'], 'CUST-7')


def test_dataflow_compares_the_columns_that_exist():
    columns = [('order_id', 'bigint', 'PRI'), ('customer_ref', 'varchar', ''),
               ('amount', 'decimal', ''), ('qty', 'int', '')]
    rows = [(7, 'CUST-7', decimal.Decimal('12.50'), 2)]
    cache = {7: {'amount': '12.50', 'qty': '2'}}
    out, _sql = _dataflow_against(columns, rows, cache=cache)
    check('df.synced', out['rows'][0]['state'], 'synced')


def test_dataflow_spots_a_stale_row_on_a_non_demo_table():
    columns = [('order_id', 'bigint', 'PRI'), ('customer_ref', 'varchar', ''),
               ('amount', 'decimal', '')]
    rows = [(7, 'CUST-7', decimal.Decimal('99.99'))]
    cache = {7: {'amount': '12.50'}}
    out, _sql = _dataflow_against(columns, rows, cache=cache)
    check('df.differs', out['rows'][0]['state'], 'differs')


def test_table_shape_falls_back_when_the_schema_cannot_be_read():
    class Cur:
        def execute(self, sql, args=None):
            self._sql = sql

        def fetchall(self):
            return []

    shape = monitor.table_shape(Cur(), 'mystery')
    check('shape.pk', shape['pk'], 'id')
    check_true('shape.note', 'could not read the schema' in (shape.get('note') or ''))


def test_table_shape_picks_the_primary_key_not_the_first_column():
    class Cur:
        def execute(self, sql, args=None):
            self._sql = sql

        def fetchall(self):
            return [('created_at', 'timestamp', ''), ('pk_col', 'bigint', 'PRI'),
                    ('name', 'varchar', '')]

    monitor.reset_state()
    shape = monitor.table_shape(Cur(), 'weird')
    check('shape.real_pk', shape['pk'], 'pk_col')
    check('shape.label', shape['label'], 'name')


DBZ_RECORDS_LINE = ('2026-09-14 09:45:56,909 INFO  [io.deb.con.com.BaseSourceTask] '
                    '(pool-8-thread-1) 150 records sent during previous 00:00:30.0, '
                    'last recorded offset: {"file":"mysql-bin.000123","pos":456}')


def test_debezium_records_line_is_parsed():
    # Format confirmed against a live task, not assumed.
    out = monitor.parse_debezium_records([DBZ_RECORDS_LINE])
    check('dbz.observed', out['observed'], True)
    check('dbz.records', out['records_sent'], 150)
    check('dbz.intervals', out['intervals'], 1)


def test_debezium_records_are_summed_not_replaced():
    # Each line covers its own interval, so the window total is the sum.
    second = DBZ_RECORDS_LINE.replace('150 records', '75 records')
    out = monitor.parse_debezium_records([DBZ_RECORDS_LINE, second])
    check('dbz.summed', out['records_sent'], 225)
    check('dbz.two_intervals', out['intervals'], 2)


def test_debezium_idle_reports_absence_not_zero_throughput():
    out = monitor.parse_debezium_records(['INFO Connector started'])
    check('dbz.absent', out['observed'], False)
    check_true('dbz.no_records_key', 'records_sent' not in out)


def test_log_activity_surfaces_debezium_records():
    now = monitor.now_ms()
    fake = _FakeLogs(events=[
        {'timestamp': now - 1000, 'message': DBZ_RECORDS_LINE,
         'logStreamName': 'debezium/debezium/t1'},
    ])
    out = _with_logs(fake, monitor.log_activity, 'debezium')
    check('activity.dbz_records', out['counters']['records_sent'], 150)


def test_log_events_follows_an_empty_page_that_carries_a_token():
    """CloudWatch scans the window in chunks and can hand back an empty page with
    a nextToken while events still exist further on. Observed live: a 60-minute
    window returned the line, a 7-day window over the SAME group returned zero,
    and get_log_events on the same stream returned the events."""
    now = monitor.now_ms()
    fake = _FakeLogs(empty_pages=3, events=[
        {'timestamp': now - 1000, 'message': 'INFO found me',
         'logStreamName': 'sink/sink/t1'},
    ])
    out = _with_logs(fake, monitor.log_events, 'sink', minutes=10080)
    check('logs.followed_state', out['state'], 'ok')
    check('logs.followed_count', out['count'], 1)
    check('logs.pages', out['scanned_pages'], 4)
    # Every follow-up call must carry the token and keep the original window.
    check('logs.token_sent', fake.calls[1]['nextToken'], 'tok-more')


def test_log_events_stops_following_and_says_the_search_was_partial():
    # A genuinely quiet 7-day group must not become an unbounded scan, and the
    # answer has to distinguish "not logged" from "not in the part I searched".
    fake = _FakeLogs(empty_pages=999, events=[])
    out = _with_logs(fake, monitor.log_events, 'sink', minutes=10080)
    check('logs.partial_state', out['state'], 'empty')
    check('logs.partial_flag', out.get('partial'), True)
    check_true('logs.partial_note', 'stopped' in (out.get('note') or ''))
    check_true('logs.bounded', len(fake.calls) <= 14)


# --- every action must follow the selected link ------------------------------
# The bug these guard: /probe and /reconcile were unscoped, so with a link
# selected they acted on the CONFIGURED pipeline instead. For the probe that
# meant writing a heartbeat row into a different customer's database than the
# one named on screen.
#
# Intentionally unscoped, with the reason:
#   /links            lists every link, so it cannot be scoped to one
#   /phases /run      the load-phase harness belongs to this host, not a link
#   /run/cancel /runs   (same)
#   /mark             marks the host-wide history series
UNSCOPED_ON_PURPOSE = {
    '/links', '/phases', '/run', '/run/cancel', '/runs', '/mark',
}


def test_every_monitor_route_follows_the_selected_link():
    client = monitor_client()
    app = client.application
    unscoped = []
    for rule in app.url_map.iter_rules():
        path = rule.rule.replace('/api/monitor', '') or '/'
        if path in UNSCOPED_ON_PURPOSE or rule.endpoint == 'static':
            continue
        view = app.view_functions[rule.endpoint]
        if not getattr(view, '_per_link', False):
            unscoped.append(path)
    check('routes.all_scoped', sorted(set(unscoped)), [])


def test_a_post_can_carry_the_link_in_its_body():
    # The actions are POSTs, and the page sends the link in the body rather than
    # the query string. Reading only request.args ignored it completely.
    client = monitor_client()
    captured = []
    saved = monitor.link_config
    monitor.link_config = lambda name: captured.append(name) or {'link_name': name}
    try:
        client.post('/api/monitor/reconcile', json={'link': 'shop'})
    finally:
        monitor.link_config = saved
    check('routes.body_link', captured, ['shop'])


def test_probe_refuses_a_watched_link_and_says_the_real_reason():
    # probe_enabled is False for any watched link because the probe WRITES to the
    # source table. Reporting the env-var reason would lead someone to look for a
    # setting that is not the cause.
    with monitor.watching({'link_name': 'shop', 'probe_enabled': False}):
        out = monitor.run_probe()
    check('probe.refused', out['ok'], False)
    check('probe.link', out['link'], 'shop')
    check_true('probe.reason_is_the_write',
               'writes a heartbeat row' in out['error'])
    check_true('probe.not_the_env_var', 'DEMO_PROBE_ENABLED' not in out['error'])


def test_probe_still_reports_the_env_reason_when_not_watching_a_link():
    saved = monitor.CFG['probe_enabled']
    monitor.CFG['probe_enabled'] = False
    try:
        out = monitor.run_probe()
    finally:
        monitor.CFG['probe_enabled'] = saved
    check_true('probe.env_reason', 'DEMO_PROBE_ENABLED' in out['error'])


# --- parsers validated against REAL captured log lines --------------------
# These strings are verbatim from a live uitest pipeline's CloudWatch groups, so
# they are the contract. Earlier versions of this parser were written from the
# source format string alone; reading a real log corrected two of them.
REAL_SINK_LINE = ('  [410s] writes=1 deletes=1 rate=0/sec | per-cycle: '
                  'read=406.737383ms parse=73.366\u00b5s write=955.122\u00b5s '
                  'ack=2.156106ms batch=1')
REAL_DBZ_LINE = ('2026-09-14 05:21:08,867 INFO  [io.deb.con.com.BaseSourceTask] '
                 '(pool-8-thread-1) 150 records sent during previous '
                 '02:00:00.016, last recorded offset of {server=uitest} '
                 'partition is {ts_sec=1}')
REAL_LOADER_LINES = [
    '  [ 1/16 chunks done]           55 rows |    0.5s |        102 rows/sec',
    '  [16/16 chunks done]        1,000 rows |    0.7s |      1,420 rows/sec',
]


def test_sink_stats_parses_a_real_line_with_a_zero_rate():
    out = monitor.parse_sink_stats([REAL_SINK_LINE])
    check('real_sink.writes', out['writes'], 1)
    check('real_sink.rate', out['rate_per_sec'], 0.0)
    check('real_sink.batch', out['per_cycle']['batch'], 1)





def test_loader_progress_handles_thousands_separators():
    out = monitor.parse_loader_progress(REAL_LOADER_LINES)
    check('real_loader.rows', out['rows'], 1000)
    check('real_loader.rate', out['rows_per_sec'], 1420)
    check('real_loader.chunks', (out['chunks_done'], out['chunks_total']), (16, 16))


def test_loader_completion_is_derived_from_chunks_not_a_keyword():
    # The loader prints no "DONE" line; it reports the last chunk and exits.
    done = monitor.parse_loader_progress(REAL_LOADER_LINES)
    check('real_loader.finished', done['finished'], True)
    partial = monitor.parse_loader_progress([REAL_LOADER_LINES[0]])
    check('real_loader.unfinished', partial['finished'], False)


def test_loader_signals_match_the_real_wording():
    signals = {s['signal']: s['matches']
               for s in monitor.count_signals('loader', REAL_LOADER_LINES
                                              + ['PK range [1..1000] split into 16 chunks'])}
    check('real_loader.progress_sig', signals['progress'], 2)
    check('real_loader.split_sig', signals['range_split'], 1)


def test_sink_signals_match_the_real_startup_lines():
    signals = {s['signal']: s['matches'] for s in monitor.count_signals('sink', [
        '  Workers: 8, Batch: 2000',
        '  Connected to broker master.ecdl-uitest-broker.example:6379',
        '  Reliability: retry+DLQ ON (max_deliveries=5, retry_idle=30s, dlq=dlq:x)',
        '  Transforms: none (passthrough)'])}
    check('real_sink.restarted', signals['restarted'], 2)
    check('real_sink.reliability', signals['reliability'], 1)
    check('real_sink.transforms', signals['transforms'], 1)


# --- the merged debezium parser -------------------------------------------
# parse_debezium_records was once defined TWICE in monitor.py. Python keeps the
# last definition, so the richer implementation was silently dead code and the
# regex it used was unreferenced. The suite did not catch it because both
# versions returned records_sent and intervals, which was all anyone asserted.
# These tests pin the fields the surviving implementation must expose, and the
# last one fails outright if a second definition ever reappears.

# The genuine line, copied from a live task's log -- note the offset uses
# `file=..., pos=...` inside the partition map, not JSON.
DBZ_REAL_LINE = (
    '2026-09-13 19:21:08,769 INFO [io.deb.con.com.BaseSourceTask] '
    '(pool-8-thread-1) 150 records sent during previous 01:59:22.329, '
    'last recorded offset of {server=uitest} partition is {ts_sec=1789271961, '
    'file=mysql-bin-changelog.000305, pos=67775737, server_id=1547354676}')


def test_debezium_records_exposes_every_merged_field():
    out = monitor.parse_debezium_records([DBZ_REAL_LINE])
    check('dbz_merged.observed', out['observed'], True)
    check('dbz_merged.total', out['records_sent'], 150)
    check('dbz_merged.intervals', out['intervals'], 1)
    check('dbz_merged.last', out['records_sent_last_interval'], 150)
    # Reconstructed from the captured groups verbatim, so the zero-padded hour
    # the connector actually printed is preserved rather than normalised.
    check('dbz_merged.window', out['reporting_window'], '01:59:22.329')
    check('dbz_merged.interval_sec', out['newest_interval_sec'], 7162.3)


def test_debezium_records_extracts_the_binlog_offset_from_a_real_line():
    # Exercises DEBEZIUM_OFFSET_RE against the shape it was written for. The
    # older sample line used a JSON offset, so this path had no coverage.
    out = monitor.parse_debezium_records([DBZ_REAL_LINE])
    check('dbz_merged.offset', out['offset'],
          'mysql-bin-changelog.000305:67,775,737')


def test_debezium_average_is_over_the_reported_interval_not_a_live_rate():
    # 150 records across nearly two hours is ~0.021/sec. The interval is reported
    # alongside so this can never be shown as current throughput; for that the
    # broker's produced_per_sec is the accurate signal.
    out = monitor.parse_debezium_records([DBZ_REAL_LINE])
    check('dbz_merged.avg', out['newest_avg_per_sec'], 0.021)
    check_true('dbz_merged.interval_present',
               out['newest_interval_sec'] > 7000)


def test_debezium_offset_absent_when_the_engine_does_not_report_one():
    # PostgreSQL and Oracle offsets carry no file/pos, so the key must simply be
    # missing rather than present and wrong.
    line = ('2026-09-14 09:45:56,909 INFO [io.deb.con.com.BaseSourceTask] '
            '(pool-8-thread-1) 7 records sent during previous 00:00:30.0, '
            'last recorded offset of {server=pg} partition is {lsn=12345}')
    out = monitor.parse_debezium_records([line])
    check('dbz_merged.no_offset', 'offset' in out, False)
    check('dbz_merged.pg_total', out['records_sent'], 7)


def test_parse_debezium_records_is_defined_exactly_once():
    # The regression guard for the original defect: a second def would shadow the
    # first and every assertion above would still pass.
    import inspect
    source = inspect.getsource(monitor)
    check('dbz_merged.one_def',
          source.count('\ndef parse_debezium_records('), 1)


# --- visibility gaps name the endpoint and whose problem it is ------------------
# Observed live: two deployed links, one whose broker the control plane could
# reach and one it could not. The CDC stack grants the broker ingress from the
# pipeline TASK security group only, so the monitor cannot reach the single
# component ECDL creates itself. "not visible from the control plane" was true
# but did not tell the reader what to do next.
def test_unseen_broker_is_owned_by_ecdl_and_names_the_fix():
    remedy = monitor.visibility_gap_remedy('broker')
    check('gap.broker_owner', remedy['owner'], 'ecdl')
    check_true('gap.broker_endpoint',
               remedy['endpoint'].startswith(monitor.CFG['broker_host']))
    check_true('gap.broker_fix_mentions_sg',
               'security group' in remedy['fix'])


def test_unseen_customer_components_are_marked_customer_owned():
    for name in ('target', 'aurora'):
        remedy = monitor.visibility_gap_remedy(name)
        check('gap.%s_owner' % name, remedy['owner'], 'customer')
        check_true('gap.%s_endpoint' % name, bool(remedy.get('endpoint')))


def test_unseen_debezium_points_out_that_logs_still_work():
    # The useful fact when the broker is unreachable: CloudWatch Logs needs no
    # network path to the pipeline, so the log panel is unaffected.
    remedy = monitor.visibility_gap_remedy('debezium')
    check('gap.dbz_owner', remedy['owner'], 'ecdl')
    check_true('gap.dbz_logs_note', 'logs still work' in remedy['fix'])


def test_visibility_note_blames_ecdl_when_the_broker_is_the_unseen_one():
    dead = {'ok': False, 'error': 'TimeoutError: Timeout connecting to server'}
    alive = {'ok': True, 'state': 'ok'}
    flow = monitor.build_flow(alive, alive, dead, alive, alive)
    check_true('gap.note_ours', 'ours to fix' in flow['visibility_note'])
    spots = {s['component']: s for s in flow['visibility_gaps']}
    check_true('gap.spot_has_fix', bool(spots['broker'].get('fix')))
    check('gap.spot_owner', spots['broker']['owner'], 'ecdl')


def test_visibility_note_does_not_blame_ecdl_for_a_customer_target():
    dead = {'ok': False, 'error': 'TimeoutError: Timeout connecting to server'}
    alive = {'ok': True, 'state': 'ok'}
    flow = monitor.build_flow(alive, alive, alive, alive, dead)
    check_true('gap.note_customer', 'customer-owned' in flow['visibility_note'])
    check_true('gap.note_not_ours', 'ours to fix' not in flow['visibility_note'])


# --- restart detection (ECS stopped-task reasons) --------------------------
class _FakeEcs(object):
    """Stands in for the ECS client for restart tests."""

    def __init__(self, stopped=None):
        self._stopped = stopped or []

    def list_tasks(self, **kwargs):
        if kwargs.get('desiredStatus') == 'STOPPED':
            return {'taskArns': ['arn:t:%d' % i for i in range(len(self._stopped))]}
        return {'taskArns': []}

    def describe_tasks(self, **kwargs):
        return {'tasks': self._stopped}


class _Stamp(object):
    """A stand-in for a boto datetime with .timestamp()."""

    def __init__(self, ts):
        self._ts = ts

    def timestamp(self):
        return self._ts


def _with_ecs(fake, fn, *args, **kwargs):
    saved = monitor.aws
    monitor.aws = lambda service: fake
    monitor.reset_state()  # ecs_restarts is cached; clear between cases
    try:
        return fn(*args, **kwargs)
    finally:
        monitor.aws = saved


def test_ecs_restarts_none_is_zero_not_an_error():
    out = _with_ecs(_FakeEcs([]), monitor.ecs_restarts, 'svc')
    check('restarts.none', out['restarts'], 0)
    check_true('restarts.no_sample', 'restart_sample' not in out)


def test_ecs_restarts_reports_reason_and_exit_code():
    now = time.time()
    stopped = [
        {'stoppedAt': _Stamp(now - 30),
         'stoppedReason': 'Essential container in task exited',
         'containers': [{'exitCode': 137}]},
        {'stoppedAt': _Stamp(now - 600),
         'stoppedReason': 'Scaling activity',
         'containers': [{'exitCode': 0}]},
    ]
    out = _with_ecs(_FakeEcs(stopped), monitor.ecs_restarts, 'svc', minutes=120)
    check('restarts.count', out['restarts'], 2)
    # Newest first, so the last restart is the one shown.
    check('restarts.last_reason', out['last_restart_reason'],
          'Essential container in task exited')
    check('restarts.last_exit', out['last_restart_exit_code'], 137)
    check('restarts.last_age', out['last_restart_age_sec'], 30.0)


def test_ecs_restarts_excludes_tasks_older_than_the_window():
    now = time.time()
    stopped = [
        {'stoppedAt': _Stamp(now - 30), 'stoppedReason': 'recent',
         'containers': [{'exitCode': 1}]},
        {'stoppedAt': _Stamp(now - 5 * 3600), 'stoppedReason': 'old',
         'containers': [{'exitCode': 1}]},
    ]
    out = _with_ecs(_FakeEcs(stopped), monitor.ecs_restarts, 'svc', minutes=120)
    check('restarts.windowed', out['restarts'], 1)
    check('restarts.windowed_reason', out['last_restart_reason'], 'recent')


def test_reconcile_maps_source_column_to_its_renamed_cache_field():
    # Transform renamed name->title: Aurora 'name' must be compared against the
    # cache field 'title', not a cache 'name' that no longer exists.
    db = [{'id': 1, 'label': 'x', 'name': 'Widget', 'price': 9.99}]
    cache = {1: {'title': 'Widget', 'price': '9.99'}}
    rows = monitor.reconcile_rows(db, cache, fields=['name', 'price'],
                                  cache_field_map={'name': 'title'})
    check('recon.rename.state', rows[0]['state'], 'synced')
    check('recon.rename.cache_name', rows[0]['cache_name'], 'Widget')


def test_reconcile_without_the_map_flags_a_renamed_field_as_differs():
    # This is the bug the map fixes: without it, a renamed field reads as stale.
    db = [{'id': 1, 'name': 'Widget', 'price': 9.99}]
    cache = {1: {'title': 'Widget', 'price': '9.99'}}
    rows = monitor.reconcile_rows(db, cache, fields=['name', 'price'])
    check('recon.norename.state', rows[0]['state'], 'differs')


def test_reconcile_pending_when_the_key_is_not_cached():
    rows = monitor.reconcile_rows([{'id': 1, 'price': 1.0}], {}, fields=['price'])
    check('recon.pending', rows[0]['state'], 'pending')


def test_choose_stamp_column_prefers_the_label_when_it_reaches_the_cache():
    shape = {'pk': 'id', 'label': 'sku', 'compare': ['name', 'price']}
    cached = {'id': '1', 'sku': 'ABC', 'name': 'x', 'price': '1.0'}
    check('stamp.label', monitor._choose_stamp_column(shape, cached, 'id'), 'sku')


def test_choose_stamp_column_falls_back_when_label_is_renamed_or_absent():
    # The label is not a field in the cache (renamed away or not selected), so a
    # compare column that IS present is used instead.
    shape = {'pk': 'id', 'label': 'name', 'compare': ['category', 'price']}
    cached = {'id': '1', 'title': 'x', 'category': 'demo', 'price': '1.0'}
    check('stamp.fallback',
          monitor._choose_stamp_column(shape, cached, 'id'), 'category')


def test_choose_stamp_column_is_none_when_nothing_reaches_the_cache():
    shape = {'pk': 'id', 'label': 'name', 'compare': ['amount']}
    cached = {'id': '1', 'title': 'x'}  # neither name nor amount present
    check('stamp.none', monitor._choose_stamp_column(shape, cached, 'id'), None)


def test_choose_stamp_column_never_returns_the_primary_key():
    shape = {'pk': 'id', 'label': 'id', 'compare': ['id']}
    cached = {'id': '1'}
    check('stamp.notpk', monitor._choose_stamp_column(shape, cached, 'id'), None)


if __name__ == '__main__':
    sys.exit(main())
