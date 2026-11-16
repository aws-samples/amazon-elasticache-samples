/*
 * Render tests for the per-component log panel. react-dom directly with axios
 * mocked, matching PipelineMonitor.test.js, so nothing touches the network.
 *
 * The common point in these tests: an empty list has several very different
 * causes, and the panel has to word each one differently. A denied read that
 * renders as "no logs" would let a permissions problem read as a healthy
 * pipeline, which is the failure this panel exists to prevent.
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import axios from 'axios';
import ComponentLogs from './ComponentLogs';

jest.mock('axios');

const API = 'http://control-plane.test:5006';

const COMPONENTS = {
  ok: true,
  link: 'shop',
  components: [
    { component: 'debezium', log_group: '/ecs/ecdl-shop-cdc/debezium',
      state: 'ok', last_event_age_sec: 3.2, task_id: 'task-dbz' },
    { component: 'sink', log_group: '/ecs/ecdl-shop-cdc/sink',
      state: 'ok', last_event_age_sec: 1.1, task_id: 'task-sink' },
    { component: 'loader', log_group: '/ecs/ecdl-shop-loader',
      state: 'absent',
      note: 'no loader has run for this link, either a cdc-only link or the load step has not started' },
  ],
  without_logs: [
    { component: 'broker',
      note: 'the broker is an ElastiCache replication group, not a container, so it has no log stream.' },
  ],
};

const STREAMS = {
  ok: true,
  component: 'debezium',
  state: 'ok',
  streams: [
    { stream: 'debezium/debezium/aaaaaaaaaaaa1111', task_id: 'aaaaaaaaaaaa1111',
      last_event_age_sec: 2.0 },
    { stream: 'debezium/debezium/bbbbbbbbbbbb2222', task_id: 'bbbbbbbbbbbb2222',
      last_event_age_sec: 900.0 },
  ],
};

const LINES = {
  ok: true,
  component: 'debezium',
  log_group: '/ecs/ecdl-shop-cdc/debezium',
  state: 'ok',
  window_min: 30,
  count: 3,
  next: 'tok-page-2',
  events: [
    { ts: 1755791234000, age_sec: 12, stream: 'debezium/debezium/aaaa',
      task_id: 'aaaa1111', message: 'INFO  Connector started', severity: 'info' },
    { ts: 1755791235000, age_sec: 11, stream: 'debezium/debezium/aaaa',
      task_id: 'aaaa1111', message: 'WARN  Retrying in 5s', severity: 'warn' },
    { ts: 1755791236000, age_sec: 10, stream: 'debezium/debezium/aaaa',
      task_id: 'aaaa1111',
      message: 'ERROR Communications link failure', severity: 'error' },
  ],
};

const ACTIVITY = {
  ok: true,
  component: 'debezium',
  state: 'ok',
  window_min: 30,
  lines_scanned: 42,
  levels: { error: 1, warn: 1, info: 40 },
  newest_error: { message: 'ERROR Communications link failure', age_sec: 10 },
  signals: [
    { signal: 'connect_trouble', means: 'could not reach the source database',
      matches: 2, newest: 'ERROR Communications link failure' },
    { signal: 'retrying', means: 'retrying an operation', matches: 1,
      newest: 'WARN  Retrying in 5s' },
    { signal: 'snapshot', means: 'snapshot activity', matches: 0 },
  ],
  not_in_logs: {
    events_processed: 'Debezium Server publishes no metrics endpoint; throughput is measured from the broker stream',
  },
};

const SINK_ACTIVITY = {
  ok: true,
  component: 'sink',
  state: 'ok',
  window_min: 30,
  lines_scanned: 12,
  levels: { error: 0, warn: 0, info: 12 },
  signals: [],
  counters: {
    observed: true, writes: 125000, deletes: 12, rate_per_sec: 4166,
    uptime_sec: 30, per_cycle: { read: '1.2ms', parse: '300us', write: '2.1ms',
                                 ack: '800us', batch: 2000 },
  },
  not_in_logs: {
    retried: 'not logged by the sink; measured from the broker pending entries (see integrity.retrying)',
    dead_lettered: 'not logged by the sink; measured as the length of the DLQ stream (see integrity.dead_lettered)',
  },
};

let container;
let root;

/* Routes every URL the panel can request. Overrides let one test change a
   single response without restating the others. */
function mockApi({ lines = LINES, activity = ACTIVITY, components = COMPONENTS,
                   streams = STREAMS } = {}) {
  axios.get.mockImplementation((url) => {
    if (url.includes('/logs/components')) return Promise.resolve({ data: components });
    if (url.includes('/logs/streams')) return Promise.resolve({ data: streams });
    if (url.includes('/logs/activity')) return Promise.resolve({ data: activity });
    if (url.includes('/api/monitor/logs')) return Promise.resolve({ data: lines });
    return Promise.resolve({ data: {} });
  });
}

async function renderPanel(props = {}) {
  container = document.createElement('div');
  document.body.appendChild(container);
  await act(async () => {
    root = createRoot(container);
    root.render(<ComponentLogs apiUrl={API} link="shop" {...props} />);
  });
  return container;
}

afterEach(() => {
  if (root) act(() => root.unmount());
  if (container && container.parentNode) container.parentNode.removeChild(container);
  root = null;
  container = null;
  jest.clearAllMocks();
});

function urlsFor(fragment) {
  return axios.get.mock.calls.map((c) => c[0]).filter((u) => u.includes(fragment));
}

test('renders the log lines with their time, task and severity', async () => {
  mockApi();
  const el = await renderPanel();
  expect(el.querySelectorAll('.cl-line')).toHaveLength(3);
  expect(el.textContent).toContain('Connector started');
  expect(el.textContent).toContain('Communications link failure');
  expect(el.querySelectorAll('.cl-sev-error')).toHaveLength(1);
  expect(el.querySelectorAll('.cl-sev-warn')).toHaveLength(1);
  expect(el.querySelector('.cl-task').textContent).toBe('aaaa1111');
});

test('names the log group it is reading, so the wrong group is visible', async () => {
  mockApi();
  const el = await renderPanel();
  expect(el.textContent).toContain('/ecs/ecdl-shop-cdc/debezium');
});

test('scopes every request to the selected link', async () => {
  mockApi();
  await renderPanel();
  const scoped = urlsFor('/api/monitor/logs');
  expect(scoped.length).toBeGreaterThan(0);
  scoped.forEach((u) => expect(u).toContain('link=shop'));
});

test('an absent log group reads as nothing having run, not as an error', async () => {
  mockApi({
    lines: { ok: true, component: 'loader', state: 'absent',
             log_group: '/ecs/ecdl-shop-loader', events: [],
             note: 'no log group /ecs/ecdl-shop-loader yet, the task has not started' },
    activity: { ok: true, component: 'loader', state: 'absent' },
  });
  const el = await renderPanel();
  expect(el.textContent).toContain('Nothing has run yet');
  expect(el.textContent).toContain('has not started');
  expect(el.querySelector('.cl-banner-bad')).toBeNull();
  expect(el.querySelectorAll('.cl-line')).toHaveLength(0);
});

test('a denied read says so and names the missing permission', async () => {
  mockApi({
    lines: { ok: true, component: 'sink', state: 'denied', events: [],
             note: 'this control plane may not read that group, it needs logs:FilterLogEvents' },
    activity: { ok: true, component: 'sink', state: 'denied' },
  });
  const el = await renderPanel();
  expect(el.textContent).toContain('Not allowed to read these logs');
  expect(el.textContent).toContain('logs:FilterLogEvents');
  expect(el.querySelector('.cl-banner-bad')).not.toBeNull();
  expect(el.textContent).not.toContain('Nothing has run yet');
});

test('a quiet log is worded as no matching lines, not as a failure', async () => {
  mockApi({
    lines: { ok: true, component: 'debezium', state: 'empty', events: [],
             window_min: 15, note: 'nothing logged in the last 15 minute(s)' },
    activity: { ...ACTIVITY, levels: { error: 0, warn: 0, info: 0 }, lines_scanned: 0 },
  });
  const el = await renderPanel();
  expect(el.textContent).toContain('No matching lines');
  expect(el.querySelector('.cl-banner-bad')).toBeNull();
});

test('the broker appears as a tab explaining it has no logs at all', async () => {
  mockApi();
  const el = await renderPanel();
  const off = el.querySelector('.cl-tab.is-off');
  expect(off).not.toBeNull();
  expect(off.textContent).toContain('broker');
  expect(off.getAttribute('title')).toContain('ElastiCache');
  expect(off.tagName).not.toBe('BUTTON');
});

test('switching component refetches that component', async () => {
  mockApi();
  const el = await renderPanel();
  const sinkTab = [...el.querySelectorAll('button.cl-tab')]
    .find((b) => /sink/i.test(b.textContent));
  await act(async () => { sinkTab.click(); });
  expect(urlsFor('component=sink').length).toBeGreaterThan(0);
});

test('the filter is applied on submit', async () => {
  mockApi();
  const el = await renderPanel();
  const input = el.querySelector('.cl-filter input');
  await act(async () => {
    // React tracks its own value setter, so assigning .value directly does not
    // update state. Same approach as LinkWizard.test.js.
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, 'link failure');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => {
    el.querySelector('.cl-filter').dispatchEvent(
      new Event('submit', { bubbles: true, cancelable: true }));
  });
  const filtered = urlsFor('filter=');
  expect(filtered.length).toBeGreaterThan(0);
  expect(filtered[filtered.length - 1]).toContain(encodeURIComponent('link failure'));
});

test('changing the time range refetches with the new window', async () => {
  mockApi();
  const el = await renderPanel();
  const fourHours = [...el.querySelectorAll('.cl-range')]
    .find((b) => b.textContent === '4h');
  await act(async () => { fourHours.click(); });
  expect(urlsFor('minutes=240').length).toBeGreaterThan(0);
});

test('level counts and matched signals are shown, unmatched ones hidden', async () => {
  mockApi();
  const el = await renderPanel();
  expect(el.textContent).toContain('1 error');
  expect(el.textContent).toContain('42 line(s) scanned');
  expect(el.textContent).toContain('connect trouble');
  expect(el.textContent).not.toContain('snapshot');
});

test('the sink own counters are shown when it reported them', async () => {
  mockApi({ activity: SINK_ACTIVITY });
  const el = await renderPanel();
  expect(el.textContent).toContain('125,000');
  expect(el.textContent).toContain('reported by the sink itself');
  expect(el.textContent).toContain('batch 2000');
});

test('an idle sink shows why it has no counters instead of zeros', async () => {
  mockApi({
    activity: {
      ...SINK_ACTIVITY,
      counters: { observed: false,
                  note: 'the sink prints its counters only after completing a batch cycle, and has not in this window' },
    },
  });
  const el = await renderPanel();
  expect(el.textContent).toContain('only after completing a batch cycle');
  expect(el.textContent).not.toContain('0 writes');
});

test('figures that are not in the log point at where they really come from', async () => {
  mockApi({ activity: SINK_ACTIVITY });
  const el = await renderPanel();
  const notes = el.querySelector('.cl-notinlogs').textContent;
  expect(notes).toContain('retried');
  expect(notes).toContain('dead lettered');
  expect(notes).toContain('integrity');
});

test('older pages are fetched with the next token', async () => {
  mockApi();
  const el = await renderPanel();
  const more = [...el.querySelectorAll('button')]
    .find((b) => /load older/i.test(b.textContent));
  expect(more).not.toBeNull();
  await act(async () => { more.click(); });
  expect(urlsFor('next=').length).toBeGreaterThan(0);
});

test('a task picker appears only when more than one task has logged', async () => {
  mockApi();
  const el = await renderPanel();
  expect(el.querySelector('.cl-streampick')).not.toBeNull();
});

test('follow is off by default, so the panel does not poll CloudWatch on a timer', async () => {
  jest.useFakeTimers();
  mockApi();
  const el = await renderPanel();
  const before = axios.get.mock.calls.length;
  await act(async () => { jest.advanceTimersByTime(30000); });
  expect(axios.get.mock.calls.length).toBe(before);
  expect(el.querySelector('.cl-toggle input').checked).toBe(false);
  jest.useRealTimers();
});

test('a request failure is surfaced rather than leaving an empty pane', async () => {
  axios.get.mockImplementation((url) => {
    if (url.includes('/logs/components')) return Promise.resolve({ data: COMPONENTS });
    if (url.includes('/logs/streams')) return Promise.resolve({ data: STREAMS });
    if (url.includes('/logs/activity')) return Promise.resolve({ data: ACTIVITY });
    return Promise.reject(new Error('Network Error'));
  });
  const el = await renderPanel();
  expect(el.querySelector('.cl-banner-bad').textContent).toContain('Network Error');
});

test('with no link it reads the configured pipeline and sends no link param', async () => {
  mockApi();
  const el = await renderPanel({ link: '' });
  expect(el.textContent).toContain('the configured pipeline');
  urlsFor('/api/monitor/logs').forEach((u) => expect(u).not.toContain('link='));
});

test('debezium counters render as records captured, not as sink writes', async () => {
  mockApi({
    activity: {
      ok: true, component: 'debezium', state: 'ok', window_min: 30,
      lines_scanned: 5, levels: { error: 0, warn: 0, info: 5 }, signals: [],
      counters: { observed: true, records_sent: 225, intervals: 2,
                  source: 'summed from the connector own lines' },
      not_in_logs: { events_processed: 'the broker is the authoritative live rate' },
    },
  });
  const el = await renderPanel();
  expect(el.textContent).toContain('225');
  expect(el.textContent).toContain('records captured');
  expect(el.textContent).toContain('reported by the connector itself');
  // The sink wording must not appear on the connector.
  expect(el.textContent).not.toContain('writes');
});
