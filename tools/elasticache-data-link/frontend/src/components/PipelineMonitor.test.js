import { vi } from 'vitest';
/*
 * Render tests for the Pipeline Monitor page. Uses react-dom directly (no extra
 * test dependencies) with axios mocked, so nothing touches the network.
 *
 * Run: npm test -- --watchAll=false
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import axios from 'axios';

import PipelineMonitor from './PipelineMonitor';

vi.mock('axios');

const HEALTHY = {
  collected_at: 1755791234567,
  region: 'us-east-2',
  flow: {
    cdc_lag_ms: 53, capture_ms: 22, broker_wait_ms: 31, flowing: true,
    lag_source: 'in-flight',           // 1,842 entries pending, so observable live
    message: 'all hops flowing',
    hops: { aurora: 'ok', debezium: 'ok', broker: 'ok', sink: 'ok', target: 'ok' },
  },
  aurora: {
    ok: true, state: 'ok', label: 'up', endpoint: 'aurora.example:3306',
    reads_per_sec: 620.4, writes_per_sec: 6180.2, threads_running: 12,
    threads_connected: 401, max_connections: 2000, binlog: 'mysql-bin-changelog.001482:69,341,552',
    cloudwatch: { cpu_pct: 41.6, select_latency_ms: 5.23,
                  dml_latency_ms: 12.27, commit_latency_ms: 11.73 },
  },
  debezium: {
    ok: true, state: 'ok', label: 'up', service: 'ecdl-demo-cdc-debezium',
    desired: 1, running: 1, uptime_sec: 15120, capture_lag_ms: 22,
    offset_age_sec: 0.8, binlog_gap_bytes: 1249448, binlog_gap_trend_bytes_per_sec: 0,
    offset_binlog: 'mysql-bin-changelog.001482:68,092,104', log_errors: 0,
  },
  broker: {
    ok: true, state: 'ok', label: 'up', stream: 'data:ecdl.Inventory.products',
    length: 1842, entries_added: 12904551, produced_per_sec: 6210, group_lag: 0,
    pending: 1842, backlog: 1842, oldest_entry_age_ms: 31, used_memory: '148.18 M',
    streams: [
      { table: 'products', stream: 'data:ecdl.Inventory.products', key_prefix: 'products:',
        entries_added: 12904551, produced_per_sec: 6210, applied_per_sec: 6190,
        backlog: 1842, resident_length: 1842 },
    ],
  },
  sink: {
    ok: true, state: 'ok', label: 'up', service: 'ecdl-demo-cdc-sink',
    desired: 2, running: 2, consumers: 16, group: 'ecdl-sink', pending: 1842,
    oldest_pending_age_ms: 31, applied_per_sec: 6190, uptime_sec: 15100, log_errors: 0,
  },
  target: {
    ok: true, state: 'ok', label: 'up', endpoint: 'clustercfg.example:6379', tls: true,
    read_latency_us: 3.01, write_latency_us: 2.09,
    read_latency_by_cmd: { hgetall: 3.01 }, write_latency_by_cmd: { hset: 2.09 },
    keys: 5426981, ops_per_sec: 41300,
    // Split so a DB-only phase can be told apart: reads are the application,
    // writes are the sink applying CDC.
    reads_per_sec: 41300, writes_per_sec: 6180,
    hit_rate_pct: 99.4, used_memory: '1.27 G',
    maxmemory: '4.79 G', used_pct: 26.5, connected_clients: 1504, version: '9.0.0',
    shards: 1, nodes: [{ name: 'ecdl-demo-valkey-0001-001:6379', role: 'primary' }],
    cloudwatch: { engine_cpu_pct: 71.0, read_latency_us: 17.77, write_latency_us: 235.85,
                  get_cmds_latency_us: 3.88, set_cmds_latency_us: 0.43 },
  },
  // Percentiles for both stores, measured in the app, windowed to one poll.
  app_latency: {
    aurora_read_p50: 0.94, aurora_read_p90: 2.58, aurora_read_p99: 28.35,
    aurora_read_samples: 13508,
    valkey_read_p50: 0.79, valkey_read_p90: 1.06, valkey_read_p99: 2.11,
    valkey_read_samples: 12750,
    aurora_write_p50: 5.08, aurora_write_p90: 16.97, aurora_write_p99: 39.72,
    aurora_write_samples: 711,
  },
  dataflow: {
    ok: true, table: 'products', aurora_max_id: 1004577715, aurora_rows_approx: 5040562,
    synced: 1, pending: 1, differs: 1,
    rows: [
      { id: 1004577715, sku: 'live-1732-0005', db_price: 412.5, cache_price: '412.50',
        db_stock_count: 1000000, cache_stock_count: '1000000', state: 'synced' },
      { id: 1004577714, sku: 'live-1732-0004', db_price: 118.99, cache_price: null,
        db_stock_count: 1000000, cache_stock_count: null, state: 'pending' },
      { id: 1004577713, sku: 'live-1732-0003', db_price: 906.1, cache_price: '906.10',
        db_stock_count: 999998, cache_stock_count: '999999', state: 'differs' },
    ],
  },
};

const SINK_DOWN = {
  ...HEALTHY,
  flow: {
    cdc_lag_ms: 18400, capture_ms: 24, broker_wait_ms: 18376, flowing: false,
    message: 'sink down \u00b7 broker backlog 114,208 entries \u2014 Aurora is still authoritative',
    hops: { aurora: 'ok', debezium: 'ok', broker: 'warn', sink: 'bad', target: 'ok' },
  },
  broker: { ...HEALTHY.broker, state: 'warn', label: 'backlog', backlog: 114208, group_lag: 114208 },
  sink: {
    ...HEALTHY.sink, ok: false, state: 'bad', label: 'down', running: 0,
    consumers: 0, applied_per_sec: 0, oldest_pending_age_ms: 18376, log_errors: 3,
    error: 'ConnectionError: broker unreachable',
  },
};

let container;
let root;

async function renderPage() {
  container = document.createElement('div');
  document.body.appendChild(container);
  await act(async () => {
    root = createRoot(container);
    root.render(<PipelineMonitor />);
  });
  return container;
}

/* The page shows four things by default: CDC lag, component health, the two
 * resource cards, and history. Everything diagnostic sits behind a toggle, so
 * tests that assert on internals have to open it first. */
/* CDC lag is only displayed on the phase that serves reads from the cache, so
 * every test about the lag readout has to be on that phase. */
async function renderOnCachePhase() {
  const el = await renderPage();
  const tabs = el.querySelectorAll('.pm-tab');
  if (tabs.length >= 3) {
    await act(async () => { tabs[2].click(); });
  }
  return el;
}

async function renderExpanded() {
  const el = await renderPage();
  const btn = [...el.querySelectorAll('.pm-details-btn')]
    .find((b) => /show detail/i.test(b.textContent));
  if (btn) {
    await act(async () => { btn.click(); });
  }
  return el;
}

afterEach(async () => {
  if (root) await act(async () => root.unmount());
  if (container) container.remove();
  root = null;
  container = null;
  vi.clearAllMocks();
});

test('default view shows lag, component health, and one card per store', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderPage();
  const text = el.textContent;
  // 1. the lag headline
  expect(text).toContain('CDC lag');
  // 2. every component reduced to a health chip
  expect(el.querySelectorAll('.pm-chip').length).toBe(4);
  ['Debezium', 'Broker', 'Sink', 'Delivery integrity']
    .forEach((name) => expect(text).toContain(name));
  // 3. all five stages get a resource card, always visible and in flow order:
  //    the two stores plus the three pipeline components between them.
  expect(el.querySelectorAll('.pm-res-card').length).toBe(5);
  expect(text).toContain('Source \u00b7 Aurora');
  expect(text).toContain('Capture \u00b7 Debezium');
  expect(text).toContain('Broker \u00b7 Valkey Streams');
  expect(text).toContain('Sink \u00b7 ECS');
  expect(text).toContain('Target \u00b7 Valkey');
  // the component summary cards carry the headline numbers without expanding
  expect(text).toContain('1/1');   // Debezium tasks running/desired
  expect(text).toContain('2/2');   // Sink tasks running/desired
  // 4. the Aurora-vs-Valkey row check is part of the default view: it is the
  // correctness proof the demo relies on, not a diagnostic
  expect(text).toContain('Data flowing');
  // the DEEPER per-component internals stay hidden until requested
  expect(text).not.toContain('Debezium Server');
  expect(el.querySelectorAll('.pm-section').length).toBe(0);
});

test('detail is one click away and carries the per-component internals', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderExpanded();
  const text = el.textContent;
  ['Debezium Server', 'Broker Stream', 'Sink Workers', 'Delivery integrity', 'Data flowing']
    .forEach((title) => expect(text).toContain(title));
  expect(el.querySelectorAll('.pm-grid-3 .pm-card').length).toBe(3);
});

test('resource cards show utilisation as a bar with a ceiling', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderPage();
  const bars = el.querySelectorAll('.pm-res-card .pm-bar-fill');
  // Aurora: CPU + connections. Valkey: engine CPU only — memory, hit rate and
  // client count were moved to the detail section to keep the card to the
  // metrics that matter for the comparison.
  expect(bars.length).toBe(3);
  // 71% engine CPU is past the warn threshold, 41.6% CPU is not
  expect(el.querySelectorAll('.pm-bar-warn').length).toBeGreaterThanOrEqual(1);
  // connections are expressed against max_connections, not as a bare count
  expect(el.textContent).toContain('401 of 2,000 max');
});

test('shows the CDC lag headline with its capture and wait breakdown', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderOnCachePhase();
  expect(el.textContent).toContain('CDC lag');
  expect(el.textContent).toContain('53.0 ms');
  expect(el.textContent).toContain('capture 22.0 ms');
  expect(el.textContent).toContain('broker wait 31.0 ms');
});

test('sub-millisecond latencies keep their decimals', async () => {
  // Rounding these to whole milliseconds would erase the Aurora-vs-Valkey gap.
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderPage();   // percentiles are on the store cards now
  expect(el.textContent).toContain('2.58 ms');
  expect(el.textContent).toContain('1.06 ms');
});

test('renders live component numbers, not placeholders', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderExpanded();
  const text = el.textContent;
  expect(text).toContain('6,180');                      // writes/s, shown in full
  expect(text).toContain('41.3K');                      // reads/s, abbreviated above 10K
  expect(text).toContain('data:ecdl.Inventory.products');
  expect(text).toContain('ecdl-sink');
  expect(text).toContain('mysql-bin-changelog.001482:69,341,552');
  expect(text).toContain('1.2 MB');                     // binlog gap, humanised
  expect(text).toContain('flat');                       // trend, not the raw value
  expect(text).toContain('trend, not value');           // the label that stops misreading
  expect(text).toContain('99.4%');                      // cache hit rate
  expect(text).toContain('5.4M');                       // keys stored (compact on the card)
});

test('each pipeline component gets a health chip with its state', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderPage();
  // Debezium, Broker, Sink, Delivery integrity - the stores have their own cards
  expect(el.querySelectorAll('.pm-chip').length).toBe(4);
  expect(el.querySelectorAll('.pm-chip .pm-dot.pm-s-ok').length).toBe(3);
  // one number each, so the strip stays scannable
  expect(el.textContent).toContain('1/1 tasks');
  expect(el.textContent).toContain('6,210 ev/s');
  expect(el.textContent).toContain('6,190 applied/s');
});

test('a failure opens the detail section by itself', async () => {
  // A collapsed section must never be what hides a broken pipeline.
  axios.get.mockResolvedValue({ data: SINK_DOWN });
  const el = await renderPage();          // note: no click
  expect(el.textContent).toContain('Sink Workers');
  expect([...el.querySelectorAll('.pm-details-btn')][0].textContent)
    .toMatch(/hide detail/i);
});

test('degraded payload surfaces the failing card, its error, and a warning banner', async () => {
  axios.get.mockResolvedValue({ data: SINK_DOWN });
  const el = await renderExpanded();
  const text = el.textContent;

  const sinkChip = [...el.querySelectorAll('.pm-chip')]
    .find((c) => c.textContent.startsWith('Sink'));
  expect(sinkChip.className).toContain('pm-s-bad');
  expect(el.querySelectorAll('.pm-chip.pm-s-warn').length).toBeGreaterThanOrEqual(1);
  // This payload's sink carries an `error`, meaning the collector could not READ
  // it. The card is therefore marked unreadable rather than failed, and the error
  // is still shown verbatim -- the pipeline may be fine and only our view broke.
  // The banner still says "sink down" because the payload's own flow.hops does.
  expect(el.querySelector('.pm-unknown')).not.toBeNull();
  expect(el.querySelector('.pm-unknown').textContent)
    .toContain('broker unreachable');
  expect(el.querySelector('.pm-unknown').textContent)
    .toContain('unknown, not zero');
  expect(text).toContain('18.4 s');                     // lag rolled up to seconds
  expect(text).toContain('114,208');                    // backlog visible
  // the reassurance that matters while the sink is down
  expect(text).toContain('sink down');
  expect(text).toContain('Aurora is still authoritative');
});

test('a healthy pipeline shows no status banner', async () => {
  // The old banner restated the dots and got it wrong when idle. No banner unless
  // something is actually degraded.
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderPage();
  expect(el.querySelectorAll('.pm-flowmsg').length).toBe(0);
  expect(el.textContent).not.toContain('all hops flowing');
});

test('data-flow strip pairs Aurora rows with their cache keys', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderExpanded();
  const text = el.textContent;

  expect(text).toContain('Data flowing');
  // DMS-style per-table counters: rows read vs rows applied (both CDC).
  expect(text).toContain('rows read (CDC)');
  expect(text).toContain('rows applied (CDC)');
  expect(text).toContain('12,904,551');                // rows read = entries_added
  // Only rows that ARE in the cache are shown (synced + differs). The absent
  // (pending) row is not displayed -- completeness is a counters job, not a
  // sample-row job.
  expect(el.querySelectorAll('.pm-flow-rows tbody tr').length).toBe(2);
  expect(text).toContain('1,004,577,715');             // newest PK (synced) is shown
  expect(el.querySelectorAll('.pm-dot-synced').length).toBe(1);
  expect(el.querySelectorAll('.pm-dot-pending').length).toBe(0);   // absent row not shown
  expect(el.querySelectorAll('.pm-dot-differs').length).toBe(1);
  // footnote: the sample proves source==target for cached rows; completeness is
  // the counters' job
  expect(text).toContain('the sink applied every captured change');
  expect(text).toContain('are not shown here');
});

test('the value cell joins the link\'s own fields, not a fixed price/stock pair', async () => {
  // A catalog link has name/category/price and no stock column; the source and
  // target value cells must join those, not a phantom "stock" from another shape.
  axios.get.mockResolvedValue({ data: { ...HEALTHY,
    broker: { ...HEALTHY.broker,
      streams: [{ table: 'catalog', stream: 'data:l.Inventory.catalog', key_prefix: 'catalog:',
        entries_added: 100, produced_per_sec: 0, applied_per_sec: 0, backlog: 0, resident_length: 0 }] },
    dataflow: { ...HEALTHY.dataflow, table: 'catalog', key_prefix: 'catalog:',
      synced: 1, pending: 0, differs: 0,
      rows: [
        { id: 42, sku: 'SUS42', db_name: 'Sustained 42', cache_name: 'Sustained 42',
          db_category: 'demo', cache_category: 'demo',
          db_price: 8.99, cache_price: '8.99', state: 'synced' },
      ] } } });
  const el = await renderExpanded();
  const text = el.textContent;
  expect(text).toContain('value at source');
  expect(text).toContain('value at target');
  expect(text).not.toContain('stock');               // no phantom field for catalog
  const srcCell = el.querySelector('.pm-flow-rows tbody tr .pm-flowval');
  expect(srcCell.textContent).toContain('Sustained 42');
  expect(srcCell.textContent).toContain('demo');
  expect(srcCell.textContent).toContain('8.99');
  expect(el.querySelectorAll('.pm-dot-synced').length).toBe(1);
});

test('a multi-table link shows a data-flowing block per table with its own rows', async () => {
  axios.get.mockResolvedValue({ data: { ...HEALTHY,
    broker: { ...HEALTHY.broker, streams: [
      { table: 'catalog', key_prefix: 'catalog:', entries_added: 500,
        produced_per_sec: 10, applied_per_sec: 10, backlog: 0 },
      { table: 'customers', key_prefix: 'customers:', entries_added: 84203,
        produced_per_sec: 0, applied_per_sec: 0, backlog: 12 },
    ] },
    dataflow: { ok: true, sample: 'newest', tables: [
      { table: 'catalog', ok: true, key_prefix: 'catalog:', synced: 1, pending: 0, differs: 0,
        rows: [{ id: 42, label: 'SUS42', sku: 'SUS42', db_name: 'Sustained 42',
          cache_name: 'Sustained 42', db_price: 8.99, cache_price: '8.99', state: 'synced' }] },
      { table: 'customers', ok: true, key_prefix: 'customers:', synced: 1, pending: 0, differs: 0,
        rows: [{ id: 7, label: 'Ada', sku: 'Ada', db_name: 'Ada Lovelace',
          cache_name: 'Ada Lovelace', state: 'synced' }] },
    ] } } });
  const el = await renderExpanded();
  const text = el.textContent;
  expect(el.querySelectorAll('.pm-flowtable').length).toBe(2);   // one block per table
  expect(el.querySelectorAll('.pm-flow-rows').length).toBe(2);   // each has its own rows
  expect(text).toContain('caught up');   // catalog: backlog 0
  expect(text).toContain('12 behind');   // customers: backlog 12
  expect(text).toContain('Sustained 42');   // catalog's own row value
  expect(text).toContain('Ada Lovelace');    // customers' own row value
  expect(text).toContain('84,203');          // customers rows read (CDC)
});

test('applied = read - backlog, and a table behind reads "N behind"', async () => {
  // HEALTHY has entries_added 12,904,551 and backlog 1,842, so applied is
  // 12,902,709 and the table is not yet caught up.
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderExpanded();
  expect(el.textContent).toContain('12,904,551');   // rows read (CDC)
  expect(el.textContent).toContain('12,902,709');   // rows applied = read - 1,842
  expect(el.textContent).toContain('1,842 behind');
  expect(el.querySelector('.pm-recon-btn').textContent).toContain('Reconcile exactly');
});

test('a table with no backlog reads as caught up', async () => {
  axios.get.mockResolvedValue({ data: { ...HEALTHY,
    broker: { ...HEALTHY.broker, backlog: 0,
      streams: [{ ...HEALTHY.broker.streams[0], backlog: 0,
        produced_per_sec: 0, applied_per_sec: 0 }] } } });
  const el = await renderExpanded();
  expect(el.textContent).toContain('caught up');
  // read and applied are equal when nothing is outstanding
  const nums = Array.from(el.querySelectorAll('.pm-flowctr-num')).map((n) => n.textContent);
  expect(nums[0]).toBe(nums[1]);
});

test('reconcile counts like for like and explains the leftover keys', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  axios.post.mockResolvedValue({ data: {
    ok: true, aurora_rows: 6179765, product_keys: 6179765, matched: true,
    difference: 0, total_keys: 6569194, other_total: 389429,
    other_families: { 'catalog:': 378900, 'sku:': 10520 }, scan_sec: 15.3 } });
  const el = await renderExpanded();
  const btn = el.querySelector('.pm-recon-btn');
  await act(async () => { btn.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

  expect(axios.post).toHaveBeenCalledWith(
    expect.stringContaining('/api/monitor/reconcile'), {}, expect.any(Object));
  expect(el.querySelectorAll('.pm-recon-ok').length).toBe(1);
  expect(el.textContent).toContain('exact match');
  expect(el.textContent).toContain('389,429');
  expect(el.textContent).toContain('catalog: 378.9K');
});

test('a real row-to-key mismatch is reported as a problem', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  axios.post.mockResolvedValue({ data: {
    ok: true, aurora_rows: 6179765, product_keys: 6179700, matched: false,
    difference: -65, total_keys: 6569194, other_total: 389429,
    other_families: {}, scan_sec: 15.1 } });
  const el = await renderExpanded();
  await act(async () => {
    el.querySelector('.pm-recon-btn')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  expect(el.querySelectorAll('.pm-recon-bad').length).toBe(1);
  expect(el.textContent).toContain('off by -65');
});

test('a row absent from the cache is NOT shown in the sample', async () => {
  // HEALTHY has three sampled rows: synced, pending (absent), differs. The
  // absent one must not appear -- the sample only compares rows that are in the
  // cache; completeness is the counters' job, not a sample-row job.
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderExpanded();
  // the pending PK (1,004,577,714) is dropped entirely
  expect(el.querySelector('tr.pm-row-pending')).toBeNull();
  expect(el.textContent).not.toContain('1,004,577,714');
  expect(el.textContent).not.toContain('not in cache');
  expect(el.textContent).not.toContain('not yet in cache');
  // the two cached rows (synced + differs) ARE shown
  expect(el.querySelectorAll('.pm-flow-rows tbody tr').length).toBe(2);
});

test('lag is labelled with the source it came from', async () => {
  axios.get.mockResolvedValue({
    data: {
      ...HEALTHY,
      flow: { ...HEALTHY.flow, cdc_lag_ms: 64, lag_source: 'probe',
              lag_measured_age_sec: 8, lag_note: 'measured end to end 8s ago' },
    },
  });
  const el = await renderOnCachePhase();
  expect(el.querySelector('.pm-src').textContent).toBe('probe');
  expect(el.textContent).toContain('64.0 ms');
  expect(el.textContent).toContain('measured end to end 8s ago');
});

test('in-flight lag shows the capture and wait breakdown', async () => {
  axios.get.mockResolvedValue({
    data: { ...HEALTHY, flow: { ...HEALTHY.flow, lag_source: 'in-flight' } },
  });
  const el = await renderOnCachePhase();
  expect(el.querySelector('.pm-src').textContent).toBe('in-flight');
  expect(el.textContent).toContain('capture 22.0 ms');
  expect(el.textContent).toContain('broker wait 31.0 ms');
});

test('with nothing measurable the page explains why instead of showing a zero', async () => {
  axios.get.mockResolvedValue({
    data: {
      ...HEALTHY,
      flow: { ...HEALTHY.flow, cdc_lag_ms: null, lag_source: null,
              lag_note: 'no probe run yet (POST /api/monitor/probe to measure)' },
    },
  });
  const el = await renderOnCachePhase();
  expect(el.textContent).toContain('no probe run yet');
  expect(el.querySelector('.pm-src')).toBeNull();
  // the lag figure itself must be a dash, not a fabricated zero
  expect(el.querySelector('.pm-lagbox .pm-v').textContent.trim()).toBe('\u2014');
});

test('Measure now posts a probe and refreshes, and never fires on its own', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  axios.post.mockResolvedValue({ data: { ok: true, lag_ms: 64.0 } });
  const el = await renderOnCachePhase();

  // Polling alone must not write.
  expect(axios.post).not.toHaveBeenCalled();

  const btn = el.querySelector('.pm-probe-btn');
  expect(btn.textContent).toContain('Measure now');
  await act(async () => { btn.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

  expect(axios.post).toHaveBeenCalledTimes(1);
  expect(axios.post).toHaveBeenCalledWith(
    expect.stringContaining('/api/monitor/probe'), {}, expect.any(Object));
});

test('a stale probe reading is visibly struck through, not shown as current', async () => {
  axios.get.mockResolvedValue({
    data: {
      ...HEALTHY,
      flow: { ...HEALTHY.flow, cdc_lag_ms: 15, lag_source: 'probe', lag_stale: true,
              lag_note: 'STALE: measured 1.5h ago \u2014 press Measure now for the current figure' },
    },
  });
  const el = await renderOnCachePhase();
  expect(el.querySelector('.pm-v.pm-stale-lag')).not.toBeNull();
  expect(el.querySelector('.pm-d-stale')).not.toBeNull();
  expect(el.textContent).toContain('STALE');
});

test('a throttled or failed probe surfaces its reason', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  axios.post.mockResolvedValue({ data: { ok: false, throttled: true,
                                         error: 'probe throttled, retry in 1.4s' } });
  const el = await renderOnCachePhase();
  await act(async () => {
    el.querySelector('.pm-probe-btn').dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  expect(el.querySelector('.pm-probe-err').textContent).toContain('probe throttled');
});

test('a refused probe shows the server\u2019s reason, not the status code', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const refusal = new Error('Request failed with status code 400');
  refusal.response = { status: 400, data: { ok: false, refused: true,
    error: 'the probe writes to a source table, so it runs only for a named link' } };
  axios.post.mockRejectedValue(refusal);
  const el = await renderOnCachePhase();
  await act(async () => {
    el.querySelector('.pm-probe-btn').dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  const text = el.querySelector('.pm-probe-err').textContent;
  expect(text).toContain('runs only for a named link');
  expect(text).not.toContain('status code');
});

const CLEAN_INTEGRITY = {
  ok: true, state: 'ok', label: 'ok', verdict: 'ok',
  headline: 'no loss: nothing dead-lettered, nothing evicted, nothing stuck',
  dead_lettered: 0, evicted_keys: 0, retrying: 0, max_attempt_seen: 0, max_deliveries: 5,
  oldest_unacked_ms: null, broker_used_pct: 2.0, broker_used_memory: '21.40 M',
  broker_maxmemory: '1.03 G', maxmemory_policy: 'volatile-lru',
  consumers_registered: 116, consumers_active: 8, retry_sample: [], dlq_sample: [],
};

test('integrity panel shows a healthy status when nothing is lost', async () => {
  axios.get.mockResolvedValue({ data: { ...HEALTHY, integrity: CLEAN_INTEGRITY } });
  const el = await renderExpanded();
  expect(el.textContent).toContain('Delivery integrity');
  expect(el.textContent).toContain('no loss');
  expect(el.querySelector('.pm-integ .pm-pill').textContent).toBe('no loss');
  expect(el.textContent).toContain('dead-lettered');
  expect(el.textContent).toContain('none, ever');
  // consumer count must not overstate: 8 active of 116 registered
  expect(el.textContent).toContain('8');
  expect(el.textContent).toContain('116 registered');
  expect(el.querySelector('.pm-integ-loss')).toBeNull();
});

test('retries in progress are visible with attempt counts', async () => {
  axios.get.mockResolvedValue({
    data: {
      ...HEALTHY,
      integrity: {
        ...CLEAN_INTEGRITY, verdict: 'retrying', state: 'warn', label: 'retrying',
        headline: 'retries in progress — recovering automatically, nothing lost yet',
        retrying: 2, max_attempt_seen: 3,
        retry_sample: [
          { id: '1755-0', consumer: 'ip-10-0-0-1-w3', attempt: 3, of_max: 5, idle_ms: 31000 },
          { id: '1756-0', consumer: 'ip-10-0-0-1-w7', attempt: 2, of_max: 5, idle_ms: 12000 },
        ],
      },
      flow: { ...HEALTHY.flow,
              integrity_alert: 'retries in progress — recovering automatically, nothing lost yet' },
    },
  });
  const el = await renderExpanded();
  expect(el.textContent).toContain('retrying now');
  expect(el.textContent).toContain('worst attempt 3 of 5');
  expect(el.textContent).toContain('ip-10-0-0-1-w3');
  expect(el.textContent).toContain('3 of 5');
  expect(el.textContent).toContain('nothing lost yet');
  expect(el.querySelector('.pm-integ-alert')).not.toBeNull();
});

test('lost entries are unmissable and say where they went', async () => {
  axios.get.mockResolvedValue({
    data: {
      ...HEALTHY,
      integrity: {
        ...CLEAN_INTEGRITY, verdict: 'loss', state: 'bad', label: 'loss',
        headline: 'ENTRIES LOST — dead-lettered or evicted, see below',
        dead_lettered: 3, dlq_key: 'dlq:data:ecdl.Inventory.products',
        dlq_sample: [{ id: '1750-0', reason: 'max_retries', deliveries: 6, age_ms: 45000 }],
      },
      flow: { ...HEALTHY.flow, hops: { ...HEALTHY.flow.hops, sink: 'bad' },
              integrity_alert: 'ENTRIES LOST — dead-lettered or evicted, see below' },
    },
  });
  const el = await renderExpanded();
  expect(el.querySelector('.pm-integ-loss')).not.toBeNull();
  expect(el.querySelector('.pm-integ-alert-loss')).not.toBeNull();
  expect(el.textContent).toContain('ENTRIES LOST');
  expect(el.querySelector('.pm-integ .pm-pill').textContent).toBe('entries lost');
  expect(el.textContent).toContain('max_retries');
  expect(el.textContent).toContain('dlq:data:ecdl.Inventory.products');
  expect(el.textContent).toContain('Aurora remains authoritative');
  // the failure must also mark the sink chip as bad
  const chip = [...el.querySelectorAll('.pm-chip')]
    .find((c) => c.textContent.startsWith('Sink'));
  expect(chip.className).toContain('pm-s-bad');
});

test('old dead letters are named, without the loss alarm', async () => {
  axios.get.mockResolvedValue({
    data: {
      ...HEALTHY,
      integrity: {
        ...CLEAN_INTEGRITY, verdict: 'dead_letters', state: 'warn', label: 'dead_letters',
        headline: 'earlier dead letters, none new recently — review and replay them, see below',
        dead_lettered: 2, dlq_key: 'dlq:data:ecdl.Inventory.products',
        dlq_sample: [{ id: '1750-0', reason: 'max_retries', deliveries: 6, age_ms: 7200000 }],
      },
      flow: { ...HEALTHY.flow,
              integrity_alert: 'earlier dead letters, none new recently — review and replay them, see below' },
    },
  });
  const el = await renderExpanded();
  expect(el.querySelector('.pm-integ .pm-pill').textContent).toBe('earlier dead letters');
  expect(el.querySelector('.pm-integ-alert-loss')).toBeNull();
  expect(el.textContent).toContain('dlq:data:ecdl.Inventory.products');
  const chip = [...el.querySelectorAll('.pm-chip')]
    .find((c) => c.textContent.startsWith('Sink'));
  expect(chip.className).not.toContain('pm-s-bad');
});

test('a stalled backlog is reported as queued-not-lost, and prominently', async () => {
  axios.get.mockResolvedValue({
    data: {
      ...HEALTHY,
      integrity: {
        ...CLEAN_INTEGRITY, verdict: 'stalled', state: 'bad', label: 'stalled',
        headline: 'BACKLOG NOT DRAINING — nothing is consuming; entries are queued, '
                  + 'not lost, but the broker will fill',
        backlog: 48000, drained_per_sec: 0, consumers_active: 0,
        binlog_retention_hours: 24,
      },
      flow: { ...HEALTHY.flow, integrity_alert: 'BACKLOG NOT DRAINING' },
    },
  });
  const el = await renderPage();
  expect(el.querySelector('.pm-integ-stalled')).not.toBeNull();
  expect(el.textContent).toContain('queued, not yet applied');
  expect(el.textContent).toContain('48,000');
  expect(el.textContent).toContain('not lost');
  expect(el.textContent).toContain('binlog retention');
  expect(el.textContent).toContain('24h');
});

test('broker memory pressure is surfaced before anything is lost', async () => {
  axios.get.mockResolvedValue({
    data: {
      ...HEALTHY,
      integrity: {
        ...CLEAN_INTEGRITY, verdict: 'pressure', state: 'warn', label: 'pressure',
        headline: 'broker memory pressure — ingestion stalls if it fills',
        broker_used_pct: 88.0,
      },
      flow: { ...HEALTHY.flow,
              integrity_alert: 'broker memory pressure — ingestion stalls if it fills' },
    },
  });
  const el = await renderExpanded();
  expect(el.textContent).toContain('88%');
  expect(el.textContent).toContain('ingestion stalls if it fills');
});

test('Aurora shows read, write and commit latency', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderExpanded();
  const text = el.textContent;
  expect(text).toContain('commit latency');
  expect(text).toContain('11.7 ms');      // CommitLatency, distinct from DML
  // Aurora's own averages are diagnostic, so they sit in the detail strip
  expect(text).toContain('Aurora SELECT latency');
  expect(text).toContain('5.23 ms');      // SelectLatency
  expect(text).toContain('Aurora DML latency');
  expect(text).toContain('12.3 ms');      // DMLLatency (1dp above 10ms)
});

test('the Aurora card shows read and write latency rows like the target does', async () => {
  // Regression: the source card previously carried only commit latency, so read
  // and write latency were missing from it even though CloudWatch supplies both
  // (SelectLatency / DMLLatency). They must appear on the card, not only in the
  // detail footer.
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderPage();
  const aurora = el.querySelectorAll('.pm-res-card')[0].textContent;
  expect(aurora).toContain('read latency');
  expect(aurora).toContain('write latency');
  expect(aurora).toContain('commit latency');
  expect(aurora).toContain('5.23 ms');     // SelectLatency, on the card now
  expect(aurora).toContain('12.3 ms');     // DMLLatency, on the card now
});

test('each store card carries its own read and write percentiles', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderPage();
  const cards = el.querySelectorAll('.pm-res-card');
  const aurora = cards[0].textContent;
  const valkey = cards[1].textContent;
  // Aurora: read AND write percentiles
  expect(aurora).toContain('0.94 ms');    // read p50
  expect(aurora).toContain('2.58 ms');    // read p90
  expect(aurora).toContain('28.4 ms');    // read p99
  expect(aurora).toContain('5.08 ms');    // write p50
  expect(aurora).toContain('17.0 ms');    // write p90
  expect(aurora).toContain('39.7 ms');    // write p99
  // Valkey: read percentiles. There is no write histogram to report, so the
  // write row carries the server-side average and is labelled as an average.
  expect(valkey).toContain('1.06 ms');    // read p90
  expect(valkey).toContain('2.09');       // write average
  expect(valkey).toContain('live avg');
  // the two stores are NOT mixed into one table
  expect(aurora).not.toContain('1.06 ms');
});

test('Valkey latency is shown in microseconds, not rounded to 0 ms', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderExpanded();
  const text = el.textContent;
  expect(text).toContain('3.01 \u00b5s');   // live server-side read
  expect(text).toContain('2.09 \u00b5s');   // live server-side write
  expect(text).toContain('3.88 \u00b5s');   // CloudWatch GET cmds
  expect(text).toContain('0.43 \u00b5s');   // CloudWatch SET cmds
  expect(text).toContain('GET cmds');
  expect(text).toContain('SET cmds');
  expect(text).not.toContain('0.00 ms');
});

test('a store that served nothing says so instead of showing a misleading zero', async () => {
  axios.get.mockResolvedValue({ data: { ...HEALTHY, app_latency: {
    valkey_read_p50: 0.8, valkey_read_p90: 1.02, valkey_read_p99: 2.11,
    valkey_read_samples: 61234,
    aurora_read_p50: 0, aurora_read_p90: 0, aurora_read_p99: 0,
    aurora_read_samples: 0,
    aurora_write_p50: 4.9, aurora_write_p90: 6.2, aurora_write_p99: 9.5,
    aurora_write_samples: 3100,
  } } });
  const el = await renderPage();
  const aurora = el.querySelectorAll('.pm-res-card')[0].textContent;
  expect(aurora).toContain('no reads served by Aurora this interval');
  // ...but its writes still report, because writes always go to Aurora
  expect(aurora).toContain('6.20 ms');
  expect(el.querySelectorAll('.pm-lat-idle').length).toBe(1);
  expect(el.querySelectorAll('.pm-res-card')[1].textContent).toContain('1.02 ms');
});

test('percentiles are absent, not invented, when no app is wired up', async () => {
  const noApp = { ...HEALTHY };
  delete noApp.app_latency;
  axios.get.mockResolvedValue({ data: noApp });
  const el = await renderPage();
  // all five stage cards still render (two stores + three components)
  expect(el.querySelectorAll('.pm-res-card').length).toBe(5);
  // and the percentile rows read as idle rather than 0.00 ms
  expect(el.textContent).toContain('no reads served by Aurora this interval');
  expect(el.textContent).not.toContain('0.00 ms');
});

test('a running bulk load shows per-worker ids climbing', async () => {
  axios.get.mockResolvedValue({ data: { ...HEALTHY, bulkload: {
    active: true, rows_loaded: 1250000, rows_per_sec: 34338, pct: 21.9,
    worker_count: 8, workers_done: 1, pk_range: '1000000000..1006740913',
    note: 'written straight to Valkey - this path does not touch the broker',
    workers: [
      { worker: 0, rows: 300000, last_pk: 1000300000, done: false },
      { worker: 1, rows: 200000, last_pk: 1001200000, done: true },
    ],
  } } });
  const el = await renderPage();
  expect(el.textContent).toContain('Bulk load into Valkey');
  expect(el.textContent).toContain('loading');
  expect(el.textContent).toContain('1.3M');            // rows written, compacted
  expect(el.textContent).toContain('34.3K');           // rows/s
  expect(el.textContent).toContain('1/8');             // workers finished
  expect(el.textContent).toContain('1,000,300,000');   // last id written
  expect(el.textContent).toContain('does not touch the broker');
});

test('with no bulk load running the panel says so and shows nothing else', async () => {
  axios.get.mockResolvedValue({ data: { ...HEALTHY, bulkload: {
    active: false, note: 'no bulk load has reported in',
  } } });
  const el = await renderPage();
  expect(el.textContent).toContain('idle');
  expect(el.textContent).toContain('no bulk load has reported in');
  // no worker table when nothing is running
  expect(el.textContent).not.toContain('last id written');
});

test('stale CloudWatch values are labelled with their age', async () => {
  axios.get.mockResolvedValue({ data: { ...HEALTHY,
    aurora: { ...HEALTHY.aurora, reads_per_sec: 4, writes_per_sec: 1,
              cloudwatch: { ...HEALTHY.aurora.cloudwatch, cpu_pct: 76.5, age_sec: 95 } } } });
  const el = await renderPage();
  // 76% CPU next to 4 reads/s is a stale average; the age has to be on screen
  expect(el.textContent).toContain('95s old');
  expect(el.textContent).toContain('76.5%');
});

test('a failed poll shows an error instead of a blank page', async () => {
  axios.get.mockRejectedValue(new Error('Network Error'));
  const el = await renderPage();
  expect(el.textContent).toContain('Network Error');
});

test('missing values render as a dash rather than undefined or NaN', async () => {
  axios.get.mockResolvedValue({
    data: {
      flow: { hops: {}, cdc_lag_ms: null, lag_note: 'stream is empty', message: 'idle' },
      aurora: { ok: true }, debezium: { ok: true }, broker: { ok: true },
      sink: { ok: true }, target: { ok: true },
    },
  });
  const el = await renderOnCachePhase();
  expect(el.textContent).toContain('\u2014');
  expect(el.textContent).not.toContain('undefined');
  expect(el.textContent).not.toContain('NaN');
  expect(el.textContent).toContain('stream is empty');
});

// ── phase tabs ──────────────────────────────────────────────────────────────
// One tab per demo phase. The tab starts that phase's load and scopes the
// history to that phase's own window, which is what makes switching between tabs
// a comparison rather than three views of the same trailing minutes.

// cdc marks the phases where something is actually read from the cache, which
// is the only situation where its staleness matters.
// Mirrors the backend, which mirrors the three steps of the initial-load
// handoff: stop CDC and record the position, bulk
// snapshot, then seed the position back and let CDC catch up before reads move
// to the cache. Three phases, not four - the cache-read load is the second half
// of step 3, because it only means anything once the catch-up has converged.
const PHASES = [
  { id: '1', title: 'DB load only', detail: 'Reads and writes both go to Aurora.', cdc: false, load: true },
  { id: '2', title: 'Bulk insert into cache', detail: 'Bypasses Debezium and the broker.', cdc: false, load: true },
  { id: '3', title: 'CDC on: catch up, then cache reads', detail: 'Replays every change made while it was off, then reads from Valkey.', cdc: true, load: true },
];

function mockWith({ runsPayload, historyPayload }) {
  axios.get.mockImplementation((url) => {
    if (url.includes('/api/monitor/phases')) {
      return Promise.resolve({ data: { phases: PHASES } });
    }
    if (url.includes('/api/monitor/runs')) {
      return Promise.resolve({ data: runsPayload || { runs: [], agent_ok: true } });
    }
    if (url.includes('/api/monitor/history')) {
      return Promise.resolve({ data: historyPayload
        || { minutes: 30, interval_sec: 5, points: [], marks: [] } });
    }
    return Promise.resolve({ data: HEALTHY });
  });
}

test('no CDC lag readout on the DB-load and bulk-insert phases', async () => {
  mockWith({});
  const el = await renderPage();
  // Phase 1 is selected by default: nothing is read from the cache, so a lag
  // figure answers nothing - and with a healthy sink there is nothing in flight
  // to measure, which is what produced an empty panel asking for a probe.
  expect(el.querySelectorAll('.pm-lagbox').length).toBe(0);
  expect(el.textContent).not.toContain('no probe run yet');
  expect(el.textContent).toContain('nothing is read from the cache in this phase');
  // Component health is still on screen - that was never the complaint.
  expect(el.textContent).toContain('Debezium');
  expect(el.textContent).toContain('Broker');
  expect(el.textContent).toContain('Sink');
  // Phase 2 likewise.
  await act(async () => { el.querySelectorAll('.pm-tab')[1].click(); });
  expect(el.querySelectorAll('.pm-lagbox').length).toBe(0);
});

test('CDC lag returns on the phase that serves reads from the cache', async () => {
  mockWith({});
  const el = await renderPage();
  await act(async () => { el.querySelectorAll('.pm-tab')[2].click(); });
  expect(el.querySelectorAll('.pm-lagbox').length).toBe(1);
  expect(el.textContent).toContain('CDC lag');
  // and the explanation of why it matters here
  expect(el.textContent).toContain('freshness of what the app just read');
});

test('the probe stays reachable on phases where the lag panel is hidden', async () => {
  mockWith({});
  const el = await renderPage();
  await act(async () => { el.querySelector('.pm-details-btn').click(); });
  // Scoped to the CDC block: the tracked-writes panel reuses the same button class.
  const buttons = [...el.querySelectorAll('.pm-cdc-ondemand .pm-probe-btn')];
  expect(buttons.length).toBe(1);
  expect(buttons[0].textContent).toContain('Measure now');
  expect(el.textContent).toContain('not part of this phase, measure on demand');
});

test('one tab per phase of the migration sequence, titled from the backend', async () => {
  mockWith({});
  const el = await renderPage();
  const tabs = el.querySelectorAll('.pm-tab');
  // Three, one per handoff step. A fourth tab meant the page was offering a
  // cache-read run that had not been through the catch-up.
  expect(tabs.length).toBe(3);
  expect(el.textContent).toContain('DB load only');
  expect(el.textContent).toContain('Bulk insert into cache');
  expect(el.textContent).toContain('CDC on: catch up, then cache reads');
  // first tab selected by default, and it explains itself
  expect(tabs[0].getAttribute('aria-selected')).toBe('true');
  expect(el.textContent).toContain('Reads and writes both go to Aurora.');
  // nothing has run yet, and it says so per tab rather than implying success
  expect(el.textContent).toContain('not run yet');
});

test('selecting a tab switches the phase and its run button', async () => {
  mockWith({});
  const el = await renderPage();
  await act(async () => {
    el.querySelectorAll('.pm-tab')[2].click();
  });
  expect(el.querySelectorAll('.pm-tab')[2].getAttribute('aria-selected')).toBe('true');
  expect(el.textContent).toContain('then reads from Valkey.');
  expect(el.textContent).toContain('Run: CDC on: catch up, then cache reads');
});

/* Jira story F165. POST /run and /run/cancel write to a broker, so the server refuses
 * them (400) unless the request names its link, and the harness is not offered
 * for a named link. On the configured pipeline the buttons used to stay
 * enabled and every press failed with that 400; they are now disabled and say
 * why, and nothing is posted. These replace the tests that pressed Run there. */
test('on the configured pipeline Run and Stop are disabled and say why', async () => {
  mockWith({});
  axios.post.mockResolvedValue({ data: { ok: true } });
  const el = await renderPage();
  await act(async () => {
    el.querySelectorAll('.pm-tab')[1].click();
  });
  const run = el.querySelector('.pm-run-btn');
  expect(run.disabled).toBe(true);
  expect(el.querySelector('.pm-run-stop').disabled).toBe(true);
  expect(el.querySelector('.pm-run-off').textContent)
    .toContain('accepts it only for a named link');
  await act(async () => { run.click(); });
  expect(axios.post).not.toHaveBeenCalled();
});

test('a run in progress is still shown, with Stop disabled too', async () => {
  mockWith({ runsPayload: { agent_ok: true, runs: [
    { run_id: 'r1', phase: '1', title: 'DB load only', status: 'running',
      started_ms: Date.now() - 30000, output_tail: 'throughput=58860 ops/s' },
  ] } });
  const el = await renderPage();
  expect(el.querySelector('.pm-run-btn').disabled).toBe(true);
  expect(el.textContent).toContain('a run is in progress');
  // a cancel would be refused the same way, so it is not offered either
  expect(el.querySelector('.pm-run-stop').disabled).toBe(true);
  // the running phase is marked on its tab, and its output is visible
  expect(el.querySelectorAll('.pm-tab-running').length).toBe(1);
  expect(el.textContent).toContain('throughput=58860 ops/s');
});

test('a missing loadgen agent is not reported while the buttons are off anyway', async () => {
  // "start the agent or the buttons do nothing" would send someone to start
  // an agent for buttons the server refuses whatever the agent does.
  mockWith({ runsPayload: { agent_ok: false, runs: [], agent_last_seen_sec: null } });
  const el = await renderPage();
  expect(el.textContent).not.toContain('agent on the loadgen host is not reporting in');
  expect(el.querySelector('.pm-run-btn').disabled).toBe(true);
});

test('a failing GET /runs is quiet: no banner, the tabs still render', async () => {
  axios.get.mockImplementation((url) => {
    if (url.includes('/api/monitor/phases')) return Promise.resolve({ data: { phases: PHASES } });
    if (url.includes('/api/monitor/runs')) {
      const refusal = new Error('Request failed with status code 502');
      refusal.response = { status: 502, data: { runs: [], error: 'broker unreachable' } };
      return Promise.reject(refusal);
    }
    return Promise.resolve({ data: HEALTHY });
  });
  const el = await renderPage();
  expect(el.querySelectorAll('.pm-tab')).toHaveLength(3);
  expect(el.querySelector('.pm-banner-error')).toBeNull();
  expect(el.textContent).not.toContain('broker unreachable');
  expect(el.textContent).not.toContain('status code 502');
});

test('a store that served no reads loses its read percentiles instead of showing dashes', async () => {
  // DB-only phase: Aurora serves every read. Valkey serves none, but ECDL is
  // still applying the write stream to it, so it is not idle.
  mockWith({ runsPayload: { agent_ok: true, runs: [
    { run_id: 'r1', phase: '1', title: 'DB load only', status: 'finished',
      started_ms: Date.now() - 300000, ended_ms: Date.now() - 10000,
      summary: {
        samples: 48, window_sec: 240,
        aurora_cpu_pct_avg: 68.4, aurora_cpu_pct_max: 76.5, aurora_cpu_max_pct_max: 83.6,
        aurora_reads_per_sec_avg: 53157, aurora_writes_per_sec_avg: 2942,
        aurora_read_p50_avg: 0.94, aurora_read_p90_avg: 9.31, aurora_read_p99_max: 31.3,
        aurora_write_p50_avg: 5.28, aurora_write_p90_avg: 20.2, aurora_write_p99_max: 57.5,
        valkey_engine_cpu_pct_avg: 2.3, valkey_engine_cpu_pct_max: 3.8, valkey_engine_cpu_max_pct_max: 6.1,
        // the cache serves no reads, but the sink is writing to it the whole time
        valkey_reads_per_sec_avg: 0, valkey_writes_per_sec_avg: 3119,
        valkey_ops_per_sec_avg: 3119, valkey_memory_pct_max: 35.1,
        valkey_keys_delta: 0, valkey_write_us_avg: 2.14,
        valkey_read_p50_avg: null, valkey_read_p90_avg: null, valkey_read_p99_max: null,
      } },
  ] } });
  const el = await renderPage();
  expect(el.textContent).toContain('Valkey reads via the app');
  expect(el.textContent).toContain('none this phase');
  expect(el.textContent).not.toContain('Valkey read p50 / p90');
  expect(el.textContent).not.toContain('Valkey read p99 worst');
  // Aurora did serve reads, so its percentiles remain shown
  expect(el.textContent).toContain('Aurora read p50 / p90');
  // Read and write rates are reported separately: a single combined ops/s next
  // to "no reads served" is what made this look like a broken collector.
  expect(el.textContent).toContain('Valkey reads/s avg');
  // The row label carries this, so no explanatory paragraph is needed under it.
  expect(el.textContent).toContain('ECDL applying');
  expect(el.textContent).not.toContain('Valkey ops/s avg');
  expect(el.textContent).not.toContain('applying the Aurora write stream');
  expect(el.textContent).not.toContain('served no application reads');
});

test('a CDC lag average declares how few samples it came from', async () => {
  // 26.7s lag next to a 58-entry backlog is not physically possible: lag is only
  // observable while events are queued, so the few samples that carry a value
  // are the moments when the pipeline was behind. The coverage has to be on screen with the number.
  mockWith({ runsPayload: { agent_ok: true, runs: [
    { run_id: 'r1', phase: '1', title: 'DB load only', status: 'finished',
      started_ms: Date.now() - 300000, ended_ms: Date.now() - 10000,
      summary: {
        samples: 48, window_sec: 268,
        cdc_lag_ms_avg: 5000, cdc_lag_ms_max: 26700, cdc_lag_ms_n: 3,
        cdc_backlog_max: 58,
        cdc_lag_note: 'measured in only 3 of 48 samples: lag is observable while '
          + 'events are still queued, so these are the moments the pipeline was '
          + 'behind, not an average over the run',
      } },
  ] } });
  const el = await renderPage();
  expect(el.textContent).toContain('CDC lag avg (3 of 48)');
  expect(el.textContent).toContain('measured in only 3 of 48 samples');
  expect(el.textContent).toContain('moments the pipeline was behind');
});

test('a phase where lag was never measurable says so instead of showing a dash', async () => {
  mockWith({ runsPayload: { agent_ok: true, runs: [
    { run_id: 'r1', phase: '1', title: 'DB load only', status: 'finished',
      started_ms: Date.now() - 300000, ended_ms: Date.now() - 10000,
      summary: { samples: 48, window_sec: 240, cdc_backlog_max: 0 } },
  ] } });
  const el = await renderPage();
  // The row itself carries the message; no explanatory paragraph underneath.
  expect(el.textContent).toContain('not measurable this phase');
  expect(el.textContent).not.toContain('CDC lag avg');
  expect(el.textContent).not.toContain('kept the stream drained');
});

test('a run record that generated no traffic reads as a pipeline operation', async () => {
  /* Kept for run records carrying load=0. Phases all generate traffic now, but
   * runs saved by earlier sessions still have the flag, and the failure this
   * test prevents is serious: a healthy catch-up - Valkey taking 4.8K writes/s from the
   * sink, replaying a long backlog - rendered as "no traffic" with empty
   * percentile columns, which reads as a broken phase rather than as a phase
   * that was never going to have request latency. */
  mockWith({ runsPayload: { agent_ok: true, runs: [
    { run_id: 'r3', phase: '3', load: '0', title: 'CDC on: catch up',
      status: 'finished', started_ms: Date.now() - 90000, ended_ms: Date.now() - 3000,
      summary: {
        samples: 15, window_sec: 87,
        // no app traffic at all: this phase generates none
        aurora_read_p50_avg: null, aurora_read_p90_avg: null, aurora_read_p99_max: null,
        aurora_write_p50_avg: null, aurora_write_p90_avg: null, aurora_write_p99_max: null,
        valkey_read_p50_avg: null, valkey_read_p90_avg: null, valkey_read_p99_max: null,
        // the sink applying the replay is the real signal here
        valkey_writes_per_sec_avg: 4795, cdc_lag_ms_avg: 2215300, cdc_lag_ms_n: 2,
        cdc_backlog_max: 215,
      } },
  ] } });
  const el = await renderPage();
  await act(async () => { el.querySelectorAll('.pm-tab')[2].click(); });
  // says what it is, so "no app traffic" is read as the design
  expect(el.textContent).toContain('pipeline catch-up');
  // and does NOT print empty latency rows that look like a broken collector
  expect(el.textContent).not.toContain('Aurora reads via the app');
  expect(el.textContent).not.toContain('Valkey reads via the app');
  expect(el.textContent).not.toContain('Aurora write p50 / p90');
  // the numbers that DO matter for a catch-up are still there
  expect(el.textContent).toContain('4,795');
});

test('read percentiles follow whichever store actually served the reads', async () => {
  mockWith({ runsPayload: { agent_ok: true, runs: [
    // Phase 3 catches CDC up and THEN runs the cache-read load, so a finished
    // phase 3 does carry request latency - served by Valkey, not Aurora.
    { run_id: 'r3', phase: '3', load: '1',
      title: 'CDC on: catch up, then cache reads', status: 'finished',
      started_ms: Date.now() - 300000, ended_ms: Date.now() - 10000,
      summary: {
        samples: 48, window_sec: 240,
        aurora_read_p50_avg: null, aurora_read_p90_avg: null, aurora_read_p99_max: null,
        aurora_write_p50_avg: 4.96, aurora_write_p90_avg: 6.26, aurora_write_p99_max: 9.56,
        valkey_ops_per_sec_avg: 61395,
        valkey_read_p50_avg: 0.79, valkey_read_p90_avg: 1.02, valkey_read_p99_max: 2.11,
      } },
  ] } });
  const el = await renderPage();
  const tabs = el.querySelectorAll('.pm-tab');
  await act(async () => { tabs[tabs.length - 1].click(); });
  expect(el.textContent).toContain('Valkey read p50 / p90');
  expect(el.textContent).not.toContain('applying the Aurora write stream');
  // symmetric: Aurora served no reads this phase, so its read rows go too
  expect(el.textContent).not.toContain('Aurora read p50 / p90');
  expect(el.textContent).toContain('Aurora reads via the app');
});

test('the bulk load shows Aurora reading rows, not an idle Aurora', async () => {
  // The bulk loader pulls 5.7M rows out of Aurora with 8 SELECTs. Com_select
  // therefore rounds to nothing while Aurora reads ~34K rows/s. Reporting only
  // the statement rate made this phase look like the cache filled with no visible source.
  mockWith({ runsPayload: { agent_ok: true, runs: [
    { run_id: 'r2', phase: '2', title: 'Bulk insert into cache', status: 'finished',
      started_ms: Date.now() - 200000, ended_ms: Date.now() - 10000,
      summary: {
        samples: 31, window_sec: 171,
        aurora_cpu_pct_avg: 2.8, aurora_cpu_pct_max: 3.2, aurora_cpu_max_pct_max: 12.6,
        aurora_reads_per_sec_avg: 23,              // statements: ~nothing
        aurora_rows_read_per_sec_avg: 34338,       // rows: the real work
        aurora_rows_read_per_sec_max: 41200,
        aurora_writes_per_sec_avg: 0,
        aurora_active_queries_avg: 9.8,
        valkey_engine_cpu_pct_avg: 7.4, valkey_engine_cpu_pct_max: 20.2, valkey_engine_cpu_max_pct_max: 24.9,
        valkey_reads_per_sec_avg: 18, valkey_writes_per_sec_avg: 35400,
        valkey_memory_pct_max: 35.1, valkey_keys_delta: 0,
        valkey_write_us_avg: 1.61,
      } },
  ] } });
  const el = await renderPage();
  const tabs = el.querySelectorAll('.pm-tab');
  await act(async () => { tabs[1].click(); });
  // The row throughput is present and labelled as rows
  expect(el.textContent).toContain('Aurora rows read/s avg');
  expect(el.textContent).toContain('34.3K');
  expect(el.textContent).toContain('Aurora rows read/s peak');
  // and the statement rate is still there, but labelled as statements so that
  // "23" cannot be mistaken for the amount of read work Aurora did
  expect(el.textContent).toContain('Aurora SELECTs/s avg');
  expect(el.textContent).toContain('statements');
  // In THIS phase the loader writes the cache directly - ECDL is not involved,
  // so claiming "ECDL applying" would be wrong.
  expect(el.textContent).toContain('bulk loader, direct');
  expect(el.textContent).not.toContain('ECDL applying');
});

test('a phase with no run says so rather than showing another phase numbers', async () => {
  mockWith({ runsPayload: { agent_ok: true, runs: [
    { run_id: 'r1', phase: '1', title: 'DB load only', status: 'finished',
      started_ms: Date.now() - 60000, ended_ms: Date.now() - 10000,
      summary: { samples: 8, aurora_cpu_max_pct_max: 76.5 } },
  ] } });
  const el = await renderPage();
  expect(el.textContent).toContain('76.5%');
  await act(async () => {
    el.querySelectorAll('.pm-tab')[2].click();   // phase 3 never ran
  });
  expect(el.textContent).toContain('No run recorded for this phase yet');
  expect(el.textContent).not.toContain('76.5%');
});

test('a low CPU reading is captioned with the capacity it was measured against', async () => {
  // The bulk load really did sit at ~2% Aurora CPU, because capacity was still
  // at 32 ACU from the previous phase. Without the caption that reads as
  // a broken collector.
  mockWith({ runsPayload: { agent_ok: true, runs: [
    { run_id: 'r2', phase: '2', title: 'Bulk insert into cache', status: 'finished',
      started_ms: Date.now() - 200000, ended_ms: Date.now() - 40000,
      summary: { samples: 32,
        aurora_cpu_pct_avg: 2.4, aurora_cpu_max_pct_max: 12.6,
        aurora_rows_read_per_sec_avg: 34600,
        aurora_cpu_note: 'CPU is a share of provisioned capacity, which averaged '
          + '32 ACU during this phase. Capacity had not scaled back down yet, so '
          + 'the same work at the lowest capacity seen (4 ACU) would read about 19%.' } },
  ] } });
  const el = await renderPage();
  await act(async () => { el.querySelectorAll('.pm-tab')[1].click(); });
  expect(el.textContent).toContain('2.4%');
  expect(el.textContent).toContain('12.6%');              // true max, not 3.9%
  expect(el.textContent).toContain('32 ACU during this phase');
  expect(el.textContent).toContain('would read about 19%');
});

/* Mock everything, with the dashboard payload overridable. */
function mockFull({ dashboard, historyPayload }) {
  const urls = [];
  axios.get.mockImplementation((url) => {
    urls.push(url);
    if (url.includes('/api/monitor/phases')) {
      return Promise.resolve({ data: { phases: PHASES } });
    }
    if (url.includes('/api/monitor/runs')) {
      return Promise.resolve({ data: { runs: [], agent_ok: true } });
    }
    if (url.includes('/api/monitor/history')) {
      return Promise.resolve({ data: historyPayload
        || { minutes: 120, interval_sec: 5, points: [], marks: [] } });
    }
    return Promise.resolve({ data: dashboard || HEALTHY });
  });
  return urls;
}

test('history keeps each source on its own charts and asks for 120 minutes', async () => {
  const now = Math.floor(Date.now() / 1000);
  const points = [];
  for (let i = 0; i < 20; i += 1) {
    points.push({
      t: now - (20 - i) * 30,
      aurora_cpu_pct: 70, aurora_reads_per_sec: 50000, aurora_writes_per_sec: 3000,
      aurora_read_p50: 0.9, aurora_read_p90: 9.3, aurora_read_p99: 31.3,
      aurora_write_p50: 5.2, aurora_write_p90: 20.2, aurora_write_p99: 57.5,
      valkey_engine_cpu_pct: 5, valkey_reads_per_sec: 10, valkey_writes_per_sec: 3000,
      // Valkey served no reads in this window
      valkey_read_p50: null, valkey_read_p90: null, valkey_read_p99: null,
      valkey_write_us: 2.1, cdc_lag_ms: 40, cdc_backlog: 0,
    });
  }
  const urls = mockFull({ historyPayload: {
    minutes: 120, interval_sec: 5, retention_minutes: 180, samples: 1440,
    count: points.length, points, persisted: true,
    marks: [{ t: now - 300, label: 'step 1: reads on Aurora' }],
  } });

  const el = await renderPage();
  expect(urls.some((u) => u.includes('/api/monitor/history?minutes=120'))).toBe(true);
  expect(el.textContent).toContain('keeps 180 min');
  expect(el.textContent).toContain('on disk');
  // one column per source, never mixed on one axis
  expect(el.querySelectorAll('.pm-hist-col').length).toBe(2);
  expect(el.textContent).toContain('Source \u00b7 Aurora');
  expect(el.textContent).toContain('Target \u00b7 Valkey');
  // read AND write latency, per source
  expect(el.textContent).toContain('Read latency');
  expect(el.textContent).toContain('Write latency');
  // memory + connections trend charts added alongside latency (not replacing it)
  expect(el.textContent).toContain('Memory (freeable)');   // Aurora
  expect(el.textContent).toContain('Memory used');         // Valkey
  expect(el.textContent).toContain('Connections');
  expect(el.textContent).toContain('New connections/s');
  // p50/p90/p99 legends are drawn for Aurora reads
  expect(el.textContent).toContain('9.3');
  // Valkey read percentiles are null here: say so, do not draw a zero line
  expect(el.textContent).toContain('no data in this window yet');
  // Valkey writes are an average and must be labelled as one
  expect(el.textContent).toContain('average, not percentiles');
  expect(el.textContent).toContain('step 1: reads on Aurora');
});

test('the CDC lag chart headlines p95, so one spike does not define the tail', async () => {
  // 19 samples at 10 ms and a single 145 s spike (a synthetic burst). The tail
  // headline must be the p95 (10 ms), not the max, so a one-off does not make
  // the pipeline look permanently 145 s behind.
  const now = Math.floor(Date.now() / 1000);
  const points = [];
  for (let i = 0; i < 20; i += 1) {
    points.push({ t: now - (20 - i) * 30, cdc_lag_ms: i === 10 ? 145000 : 10 });
  }
  mockFull({ historyPayload: { minutes: 120, interval_sec: 5, points, marks: [] } });
  const el = await renderPage();
  const lagChart = [...el.querySelectorAll('.pm-chart')].find((c) => {
    const title = c.querySelector('.pm-chart-title');
    return title && title.textContent.includes('CDC lag');
  });
  expect(lagChart).toBeTruthy();
  const headline = lagChart.querySelector('.pm-chart-ax').textContent;
  expect(headline).toContain('p95');
  expect(headline).toContain('10.0 ms');   // the p95
  expect(headline).not.toContain('145');    // the spike does not headline
});

test('the history range selector offers 120 minutes and refetches', async () => {
  const urls = mockFull({});
  const el = await renderPage();
  const buttons = [...el.querySelectorAll('.pm-range-btn')].map((b) => b.textContent);
  expect(buttons).toEqual(['15m', '60m', '120m']);
  await act(async () => { el.querySelectorAll('.pm-range-btn')[0].click(); });
  expect(urls.some((u) => u.includes('minutes=15'))).toBe(true);
});

test('a deliberately paused Debezium is not shown as an outage', async () => {
  // Step 1 of the handoff scales Debezium to 0 on purpose. This used to render
  // red with "debezium down - Aurora is still authoritative", mid-demo.
  axios.get.mockResolvedValue({ data: { ...HEALTHY,
    debezium: { ...HEALTHY.debezium, state: 'paused', label: 'paused',
                ok: true, desired: 0, running: 0 },
    flow: { ...HEALTHY.flow,
            hops: { ...HEALTHY.flow.hops, debezium: 'paused' },
            paused: ['debezium'],
            message: 'debezium paused on purpose \u2014 changes are accumulating in '
                     + 'the Aurora binlog and replay when it resumes' } } });
  const el = await renderPage();
  expect(el.textContent).toContain('paused on purpose');
  expect(el.textContent).toContain('replay when it resumes');
  expect(el.textContent).not.toContain('Aurora is still authoritative');
  // neutral styling, not failure styling
  expect(el.querySelectorAll('.pm-s-paused').length).toBeGreaterThan(0);
});

test('CDC lag is hidden when a direct load bypasses the pipeline', async () => {
  mockFull({ dashboard: { ...HEALTHY, flow: { ...HEALTHY.flow,
    cdc_in_path: false, cdc_context: 'bypassed', cdc_lag_ms: null, lag_source: null,
    cdc_context_note: 'direct load in progress: 34,000 writes/s straight into the cache' } } });
  const el = await renderOnCachePhase();
  expect(el.querySelectorAll('.pm-lagbox-off').length).toBe(1);
  expect(el.textContent).toContain('not in path');
  expect(el.textContent).toContain('34,000 writes/s');
  // no probe button while the measurement would be meaningless
  expect(el.querySelectorAll('.pm-probe-btn').length).toBe(0);
});

test('a draining backlog reads as queue depth, not steady staleness', async () => {
  mockFull({ dashboard: { ...HEALTHY, flow: { ...HEALTHY.flow,
    cdc_in_path: true, cdc_context: 'draining', cdc_lag_ms: 7159, lag_source: 'in-flight',
    lag_note: 'backlog draining: 42,000 entries queued behind the current write burst.' } } });
  const el = await renderOnCachePhase();
  expect(el.textContent).toContain('draining');
  expect(el.textContent).toContain('42,000 entries queued');
  expect(el.querySelectorAll('.pm-lagbox-off').length).toBe(0);
  expect(el.textContent).toContain('7.2 s');
});

test('random id spot check is fetched on demand and shows synced state', async () => {
  const randomRows = {
    table: 'products', key_prefix: 'products:',
    aurora_max_id: 1006740913, aurora_rows_approx: 6179765, synced: 2, pending: 0, differs: 0,
    rows: [
      { id: 4211, sku: 'sku-4211', db_price: 12.5, cache_price: 12.5,
        db_stock_count: 900, cache_stock_count: 900, state: 'synced' },
      { id: 918273645, sku: 'sku-918273645', db_price: 99.99, cache_price: 99.99,
        db_stock_count: 12, cache_stock_count: 12, state: 'synced' },
    ],
  };
  axios.get.mockImplementation((url) => {
    if (String(url).includes('/api/monitor/dataflow')) {
      // the random set must be requested explicitly, not inferred
      expect(String(url)).toContain('sample=random');
      return Promise.resolve({ data: randomRows });
    }
    if (String(url).includes('/api/monitor/history')) {
      return Promise.resolve({ data: { points: [], marks: [] } });
    }
    return Promise.resolve({ data: HEALTHY });
  });

  const el = await renderPage();
  // the default view costs no extra Aurora query
  expect(axios.get.mock.calls.some((c) => String(c[0]).includes('/dataflow'))).toBe(false);

  const randomBtn = [...el.querySelectorAll('.pm-sample-btn')]
    .find((b) => b.textContent === 'random');
  expect(randomBtn).toBeTruthy();
  await act(async () => { randomBtn.click(); });

  expect(el.textContent).toContain('918,273,645');   // a PK nowhere near MAX(id)
  expect(el.querySelectorAll('.pm-flow-rows tbody tr').length).toBe(2);
  expect(el.querySelectorAll('.pm-dot-synced').length).toBe(2);
  expect([...el.querySelectorAll('.pm-sample-btn')]
    .some((b) => b.textContent.includes('re-sample'))).toBe(true);
});

test('a failed random sample keeps the page usable', async () => {
  axios.get.mockImplementation((url) => {
    if (String(url).includes('/api/monitor/dataflow')) {
      return Promise.reject(new Error('timeout'));
    }
    if (String(url).includes('/api/monitor/history')) {
      return Promise.resolve({ data: { points: [], marks: [] } });
    }
    return Promise.resolve({ data: HEALTHY });
  });
  const el = await renderPage();
  const randomBtn = [...el.querySelectorAll('.pm-sample-btn')]
    .find((b) => b.textContent === 'random');
  await act(async () => { randomBtn.click(); });
  expect(el.textContent).toContain('random sample failed');
  expect(el.textContent).toContain('Pipeline Monitor');
});


// ── per-table breakdown ─────────────────────────────────────────────────────
// A link routes each table to its own broker stream, so it can be caught up on
// one and behind on another. The cards show the link TOTAL, which is the wrong
// number for "which table is the problem".
const TWO_TABLES = {
  ...HEALTHY,
  tables: {
    ok: true,
    count: 2,
    worst_verdict: 'loss',
    note: 'one row per table in this link.',
    tables: [
      {
        stream: 'data:ecdl.Inventory.customers', table: 'customers',
        produced_per_sec: 120.5, applied_per_sec: 0, backlog: 200,
        dead_lettered: 3, retrying: 1, broker_wait_ms: 9000,
        verdict: 'loss', state: 'bad',
      },
      {
        stream: 'data:ecdl.Inventory.orders', table: 'orders',
        produced_per_sec: 6210, applied_per_sec: 6190, backlog: 0,
        dead_lettered: 0, retrying: 0, broker_wait_ms: 31,
        verdict: 'ok', state: 'ok',
      },
    ],
  },
};

test('a multi-table link shows one row per table, worst first', async () => {
  axios.get.mockResolvedValue({ data: TWO_TABLES });
  const el = await renderExpanded();
  const text = el.textContent;
  expect(text).toContain('Per table');
  expect(text).toContain('customers');
  expect(text).toContain('orders');
  // The unhealthy table is listed first, so it is the first thing read.
  expect(text.indexOf('customers')).toBeLessThan(text.indexOf('data:ecdl.Inventory.orders'));
});

test('the per-table row names the failing table and its numbers', async () => {
  axios.get.mockResolvedValue({ data: TWO_TABLES });
  const el = await renderExpanded();
  const text = el.textContent;
  // Backlog and dead-letter count belong to customers, not to the link total.
  expect(text).toContain('200');
  // 9000 ms renders as '9.0 s': ms() switches unit above a second, which is the
  // readable form. Asserting the raw number would have been asserting a bug.
  expect(text).toContain('9.0 s');
  expect(text).toContain('2 tables in this link');
  // The verdict is words, not the raw enum.
  expect(text).toContain('entries lost');
});

test('a single-table link does not add a redundant per-table table', async () => {
  // Everything about one table is already on the cards above it.
  axios.get.mockResolvedValue({
    data: { ...HEALTHY, tables: { ok: true, count: 1, worst_verdict: 'ok',
      tables: [{ stream: 'data:ecdl.Inventory.products', table: 'products',
                 backlog: 0, verdict: 'ok' }] } },
  });
  const el = await renderExpanded();
  expect(el.textContent).not.toContain('Per table');
});

test('a single unhealthy table is shown even on a one-table link', async () => {
  axios.get.mockResolvedValue({
    data: { ...HEALTHY, tables: { ok: true, count: 1, worst_verdict: 'loss',
      note: 'one row per table.',
      tables: [{ stream: 'data:ecdl.Inventory.products', table: 'products',
                 backlog: 12, dead_lettered: 4, verdict: 'loss' }] } },
  });
  const el = await renderExpanded();
  expect(el.textContent).toContain('Per table');
});

test('the page still renders when the payload has no per-table block', async () => {
  // Older backend, or the collector failed: the page must still render from the roll-ups.
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderExpanded();
  expect(el.textContent).not.toContain('Per table');
  expect(el.textContent).toContain('Broker Stream');
});

/* ── opening on a specific link ───────────────────────────────────────────────
   The deployments page can navigate here about one link. That has to take precedence over the
   remembered selection, or "watch the thing I just deployed" opens whichever
   pipeline was looked at last -- the exact confusion the link selector exists to
   remove. */
test('an explicitly requested link takes precedence over the remembered one', async () => {
  sessionStorage.setItem('ecdl.monitorLink', 'previously-viewed');
  axios.get.mockResolvedValue({ data: HEALTHY });
  container = document.createElement('div');
  document.body.appendChild(container);
  await act(async () => {
    root = createRoot(container);
    root.render(<PipelineMonitor initialLink="just-deployed" />);
  });
  const asked = axios.get.mock.calls.map((c) => c[0])
    .filter((u) => u.includes('/api/monitor/dashboard'));
  expect(asked.length).toBeGreaterThan(0);
  asked.forEach((u) => expect(u).toContain('link=just-deployed'));
  asked.forEach((u) => expect(u).not.toContain('previously-viewed'));
  expect(container.querySelector('#pm-link').value).toBe('just-deployed');
  sessionStorage.removeItem('ecdl.monitorLink');
});

test('with no requested link the remembered selection is still honoured', async () => {
  sessionStorage.setItem('ecdl.monitorLink', 'previously-viewed');
  axios.get.mockResolvedValue({ data: HEALTHY });
  container = document.createElement('div');
  document.body.appendChild(container);
  await act(async () => {
    root = createRoot(container);
    root.render(<PipelineMonitor />);
  });
  expect(container.querySelector('#pm-link').value).toBe('previously-viewed');
  sessionStorage.removeItem('ecdl.monitorLink');
});

describe('a deployed link does not show the demo load harness', () => {
  // Seen live: selecting a link still showed the three load-phase tabs, and
  // pressing one failed with "Name or service not known" because it queues on
  // the CONFIGURED demo pipeline's broker, not the link's. The controls cannot
  // work for a link, so they are not offered for one.
  test('the phase tabs and run button are hidden when watching a link', async () => {
    axios.get.mockImplementation((url) => {
      if (url.includes('/api/monitor/links')) {
        return Promise.resolve({ data: { links: [{ link: 'shop' }], problems: [] } });
      }
      return Promise.resolve({ data: HEALTHY });
    });
    const el = await renderPage();
    // With no link selected the harness is present.
    expect(el.querySelectorAll('.pm-tab').length).toBeGreaterThan(0);

    const select = el.querySelector('#pm-link');
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLSelectElement.prototype, 'value').set;
    await act(async () => {
      setter.call(select, 'shop');
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });

    expect(el.querySelectorAll('.pm-tab')).toHaveLength(0);
    expect(el.querySelector('.pm-runbar')).toBeNull();
    expect(el.querySelector('.pm-demo-note')).not.toBeNull();
    expect(el.querySelector('.pm-demo-note').textContent).toContain('shop');
  });

  // Jira story F165. GET /runs names no link, so on a link it read the configured
  // pipeline's broker and the page showed that pipeline's runs under the
  // link's name. With no harness on a link there is nothing to ask for.
  test('a watched link never asks for GET /runs, on load or on the poll', async () => {
    vi.useFakeTimers();
    try {
      sessionStorage.setItem('ecdl.monitorLink', 'shop');
      axios.get.mockImplementation((url) => {
        if (url.includes('/api/monitor/runs')) {
          return Promise.resolve({ data: { agent_ok: true, runs: [
            { run_id: 'r1', phase: '1', title: 'DB load only', status: 'running',
              started_ms: Date.now() - 30000, output_tail: 'other pipeline' }] } });
        }
        return Promise.resolve({ data: HEALTHY });
      });
      const el = await renderPage();
      await act(async () => { vi.advanceTimersByTime(12000); });
      const asked = axios.get.mock.calls.map((c) => c[0]);
      expect(asked.some((u) => u.includes('/api/monitor/dashboard'))).toBe(true);
      expect(asked.some((u) => u.includes('/api/monitor/runs'))).toBe(false);
      expect(el.textContent).not.toContain('other pipeline');
      expect(axios.post).not.toHaveBeenCalled();
    } finally {
      sessionStorage.removeItem('ecdl.monitorLink');
      vi.useRealTimers();
    }
  });

  test('switching from the configured pipeline to a link stops the runs poll', async () => {
    vi.useFakeTimers();
    try {
      axios.get.mockImplementation((url) => {
        if (url.includes('/api/monitor/links')) {
          return Promise.resolve({ data: { links: [{ link: 'shop' }], problems: [] } });
        }
        if (url.includes('/api/monitor/runs')) {
          return Promise.resolve({ data: { agent_ok: true, runs: [] } });
        }
        return Promise.resolve({ data: HEALTHY });
      });
      const el = await renderPage();
      // The configured pipeline still lists its runs (read-only).
      expect(axios.get.mock.calls.some((c) => c[0].includes('/api/monitor/runs'))).toBe(true);
      const select = el.querySelector('#pm-link');
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLSelectElement.prototype, 'value').set;
      await act(async () => {
        setter.call(select, 'shop');
        select.dispatchEvent(new Event('change', { bubbles: true }));
      });
      axios.get.mockClear();
      await act(async () => { vi.advanceTimersByTime(12000); });
      const asked = axios.get.mock.calls.map((c) => c[0]);
      expect(asked.some((u) => u.includes('link=shop'))).toBe(true);
      expect(asked.some((u) => u.includes('/api/monitor/runs'))).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});

/* Jira story F105. The backend sets flow.alarm_alert while the link's alarm topic has no
 * confirmed subscription. Nothing else on the page would show it: the pipeline
 * can be entirely healthy while every alarm goes to nobody. */
describe('the alarm topic banner', () => {
  const ALERT = 'Alarms reach nobody: the alarm topic has no confirmed subscription '
    + '(1 waiting for the email to be confirmed). Subscribe an endpoint to '
    + 'arn:aws:sns:us-east-2:1234:ecdl-shop-alarms or set advanced.alarms.email.';
  test('is shown when the backend reports no confirmed subscription', async () => {
    axios.get.mockResolvedValue({ data: {
      ...HEALTHY, flow: { ...HEALTHY.flow, alarm_alert: ALERT },
      alarm_topic: { state: 'unread', confirmed: 0, pending: 1 } } });
    const el = await renderPage();
    const banner = el.querySelector('.pm-alarm-alert');
    expect(banner).not.toBeNull();
    expect(banner.textContent).toBe(ALERT);
    expect(banner.getAttribute('role')).toBe('alert');
  });

  test('is absent when the topic has a confirmed subscriber', async () => {
    axios.get.mockResolvedValue({ data: {
      ...HEALTHY, alarm_topic: { state: 'ok', confirmed: 1, pending: 0 } } });
    const el = await renderPage();
    expect(el.querySelector('.pm-alarm-alert')).toBeNull();
    expect(el.textContent).not.toContain('Alarms reach nobody');
  });

  test('is absent when alarms are off or the topic could not be read', async () => {
    // 'off' and 'unknown' carry no alert: the backend only alerts on a topic
    // it read and found without a confirmed subscriber.
    for (const topic of [{ state: 'off' }, { state: 'unknown', error: 'AccessDenied' }]) {
      axios.get.mockResolvedValue({ data: { ...HEALTHY, alarm_topic: topic } });
      const el = await renderPage();
      expect(el.querySelector('.pm-alarm-alert')).toBeNull();
      await act(async () => root.unmount());
      container.remove();
      root = null;
      container = null;
    }
  });

  test('an empty alert string shows no banner', async () => {
    axios.get.mockResolvedValue({ data: {
      ...HEALTHY, flow: { ...HEALTHY.flow, alarm_alert: '' } } });
    const el = await renderPage();
    expect(el.querySelector('.pm-alarm-alert')).toBeNull();
  });
});

/* One unreadable component must degrade its own panel, not mark the whole pipeline as failed.
 * The live case this comes from: a target cache belongs to the customer, so its
 * security group need not admit the control plane, and ECDL must not edit it.
 * The old behaviour showed that as "target down" on a working pipeline. */
const TARGET_UNREADABLE = {
  ...HEALTHY,
  flow: {
    ...HEALTHY.flow,
    hops: { aurora: 'ok', debezium: 'ok', broker: 'ok', sink: 'ok',
            target: 'unreachable' },
    message: 'target not visible from the control plane \u00b7 the rest is flowing',
    visibility_gaps: [{ component: 'target',
                    error: 'ConnectionError: timed out connecting to cache' }],
    visibility_note: 'target not visible from the control plane, so 1 panel(s) below '
                + 'are unknown rather than healthy. The component itself may be '
                + 'fine: a target cache is customer-owned, so its security group '
                + 'need not admit this host.',
  },
  target: { ok: false, error: 'ConnectionError: timed out connecting to cache' },
};

test('an unreadable component is shown grey, not red', async () => {
  axios.get.mockResolvedValue({ data: TARGET_UNREADABLE });
  const el = await renderExpanded();
  const chip = [...el.querySelectorAll('.pm-chip')]
    .find((c) => /target/i.test(c.textContent));
  if (chip) {
    expect(chip.className).toContain('pm-s-unknown');
    expect(chip.className).not.toContain('pm-s-bad');
  }
  // The banner names the visibility gap without claiming an outage.
  const msg = el.querySelector('.pm-flowmsg');
  expect(msg.className).toContain('pm-unknown-msg');
  expect(msg.className).not.toContain('pm-bad');
  expect(msg.textContent).toContain('not visible');
  expect(msg.textContent).not.toContain('down');
});

test('the visibility gap explains itself rather than leaving a blank card', async () => {
  axios.get.mockResolvedValue({ data: TARGET_UNREADABLE });
  const el = await renderExpanded();
  expect(el.querySelector('.pm-visibilitynote').textContent)
    .toContain('customer-owned');
  const unknown = el.querySelector('.pm-unknown');
  expect(unknown).not.toBeNull();
  expect(unknown.textContent).toContain('Not readable from the control plane');
  expect(unknown.textContent).toContain('timed out connecting to cache');
  // The distinction that matters: absent numbers are unknown, not zero.
  expect(unknown.textContent).toContain('unknown, not zero');
});

test('a pipeline with no visibility gaps shows no visibility-gap elements', async () => {
  axios.get.mockResolvedValue({ data: HEALTHY });
  const el = await renderExpanded();
  expect(el.querySelector('.pm-visibilitynote')).toBeNull();
  expect(el.querySelector('.pm-unknown')).toBeNull();
});

/* Jira story F092: a stream the monitor could not read is "unknown", never "caught up"
 * or "nothing lost". */
const chipText = (el, name) => [...el.querySelectorAll('.pm-chip')]
  .find((c) => c.textContent.startsWith(name)).textContent;
const TWO_STREAMS = [
  { table: 'orders', stream: 'data:l.Inventory.orders', key_prefix: 'orders:', ok: true,
    entries_added: 1000, produced_per_sec: 0, applied_per_sec: 0, backlog: 0 },
  { table: 'customers', stream: 'data:l.Inventory.customers', key_prefix: 'customers:',
    ok: true, entries_added: 500, produced_per_sec: 0, applied_per_sec: 0, backlog: 0 },
];
const unreadStream = (s) => ({ table: s.table, stream: s.stream, key_prefix: s.key_prefix,
  ok: false, error: 'ResponseError: no such key' });
const UNKNOWN_INTEGRITY = {
  ...CLEAN_INTEGRITY, verdict: 'unknown', state: 'warn', label: 'unknown',
  headline: 'integrity unknown — a stream could not be read, so loss cannot be ruled out',
};

test('Jira story F092 all streams readable: caught up and nothing lost', async () => {
  axios.get.mockResolvedValue({ data: { ...HEALTHY, integrity: CLEAN_INTEGRITY,
    broker: { ...HEALTHY.broker, backlog: 0, streams: TWO_STREAMS } } });
  const el = await renderExpanded();
  expect(el.textContent).toContain('caught up 2/2');
  expect(chipText(el, 'Delivery integrity')).toContain('nothing lost');
  expect(el.querySelector('.pm-integ-streamnote')).toBeNull();
  expect(el.textContent).not.toContain('backlog unknown');
});

test('Jira story F092 one stream unreadable: that table is unknown, not caught up', async () => {
  const note = '1 of 2 streams could not be read, so their integrity is unknown: '
    + 'data:l.Inventory.customers';
  axios.get.mockResolvedValue({ data: { ...HEALTHY,
    integrity: { ...UNKNOWN_INTEGRITY, stream_note: note,
      unreadable_streams: ['data:l.Inventory.customers'] },
    flow: { ...HEALTHY.flow, integrity_alert: UNKNOWN_INTEGRITY.headline },
    broker: { ...HEALTHY.broker, backlog: 0,
      streams: [TWO_STREAMS[0], unreadStream(TWO_STREAMS[1])] } } });
  const el = await renderExpanded();
  expect(el.textContent).toContain('caught up 1/2');
  const blocks = [...el.querySelectorAll('.pm-flowtable')];
  const customers = blocks.find((b) => b.textContent.includes('customers'));
  const orders = blocks.find((b) => b.textContent.includes('orders'));
  expect(customers.textContent).toContain('backlog unknown');
  expect(customers.textContent).not.toContain('caught up');
  expect(customers.querySelector('.pm-flowbadge-unknown')).not.toBeNull();
  expect(orders.textContent).toContain('caught up');
  // The reason is shown, and nothing claims "nothing lost".
  expect(el.querySelector('.pm-integ-streamnote').textContent).toBe(note);
  expect(el.querySelector('.pm-integ .pm-pill').textContent).toBe('unknown');
  expect(el.querySelector('.pm-integ-unknown')).not.toBeNull();
  expect(chipText(el, 'Delivery integrity')).toContain('1 stream(s) not read');
  expect(chipText(el, 'Delivery integrity')).not.toContain('nothing lost');
  expect(el.textContent).not.toContain('none, ever');
  expect(el.querySelector('.pm-integ-alert').textContent).toContain('integrity unknown');
});

test('Jira story F092 no stream readable: every table unknown, nothing lost is never shown', async () => {
  const note = '2 of 2 streams could not be read, so their integrity is unknown: '
    + 'data:l.Inventory.orders, data:l.Inventory.customers';
  axios.get.mockResolvedValue({ data: { ...HEALTHY,
    integrity: { ...UNKNOWN_INTEGRITY, stream_note: note,
      unreadable_streams: TWO_STREAMS.map((s) => s.stream) },
    broker: { ...HEALTHY.broker, backlog: 0, streams: TWO_STREAMS.map(unreadStream) } } });
  const el = await renderExpanded();
  expect(el.textContent).toContain('caught up 0/2');
  expect(el.querySelectorAll('.pm-flowbadge-unknown').length).toBe(2);
  expect(el.querySelectorAll('.pm-flowbadge-ok').length).toBe(0);
  expect(el.querySelector('.pm-integ-streamnote').textContent).toBe(note);
  expect(chipText(el, 'Delivery integrity')).toContain('2 stream(s) not read');
  expect(el.textContent).not.toContain('nothing lost');
});

test('Jira story F092 empty link or failed integrity read: unknown, with the reason', async () => {
  axios.get.mockResolvedValue({ data: { ...HEALTHY,
    integrity: { ok: false, verdict: 'unknown', state: 'warn', label: 'unknown',
      headline: UNKNOWN_INTEGRITY.headline,
      stream_note: 'this link has no streams configured, so nothing was checked' },
    broker: { ...HEALTHY.broker, streams: [] },
    dataflow: { ok: true, tables: [] } } });
  const el = await renderExpanded();
  expect(el.querySelector('.pm-integ .pm-pill').textContent).toBe('unknown');
  expect(el.querySelector('.pm-integ-streamnote').textContent)
    .toContain('no streams configured');
  expect(chipText(el, 'Delivery integrity')).toContain('not read');
  expect(el.textContent).not.toContain('nothing lost');
});

test('Jira story F092 a stream row without a backlog figure is not drawn as caught up', async () => {
  // Older or partial payloads: no ok flag and no backlog. Unknown, not 0.
  axios.get.mockResolvedValue({ data: { ...HEALTHY, integrity: CLEAN_INTEGRITY,
    broker: { ...HEALTHY.broker,
      streams: [{ table: 'products', stream: 's', entries_added: 10 }] } } });
  const el = await renderExpanded();
  expect(el.textContent).toContain('caught up 0/1');
  expect(el.textContent).toContain('backlog unknown');
});
