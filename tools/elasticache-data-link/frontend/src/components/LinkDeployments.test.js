import { vi } from 'vitest';
/*
 * Render tests for the deployments page, focused on the join between deploying a
 * link and watching it run.
 *
 * The gap these cover: after a deploy the next question is always "is it actually
 * working", and the answer lived on a different page with its own link dropdown.
 * The stack outputs had the same shape of problem -- they rendered only the KEY
 * names, so the endpoint and secret ARN a deploy produced were invisible.
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import axios from 'axios';
import LinkDeployments, { groupByPipeline, ServiceChip, StatusChip } from './LinkDeployments';

vi.mock('axios');

const LIST = {
  deployments: [
    { id: 'deploy-shop-abc123', link: 'shop', mode: 'initial_load_and_cdc',
      status: 'succeeded', steps: ['broker', 'cdc', 'loader'],
      completed: ['broker', 'cdc', 'loader'] },
  ],
};

const DETAIL = {
  id: 'deploy-shop-abc123',
  link: 'shop',
  mode: 'initial_load_and_cdc',
  status: 'succeeded',
  steps: ['broker', 'cdc', 'loader'],
  completed: ['broker', 'cdc', 'loader'],
  outputs: {
    broker: {
      PrimaryEndpoint: 'ecdl-shop-broker.abc.ng.0001.use2.cache.amazonaws.com',
      Port: '6379',
      AuthSecretArn: 'arn:aws:secretsmanager:us-east-2:1234:secret:ecdl-shop-broker-xY',
    },
    cdc: { DebeziumService: 'ecdl-shop-cdc-debezium', SinkService: 'ecdl-shop-cdc-sink' },
  },
  events: [{ event: 'plan_started' }],
};

let container;
let root;

function mockApi() {
  axios.get.mockImplementation((url) => {
    if (url.includes('/api/links/deployments/')) return Promise.resolve({ data: DETAIL });
    if (url.includes('/api/links/deployments')) return Promise.resolve({ data: LIST });
    // The log panel mounts only when opened; return an empty response either way.
    return Promise.resolve({ data: { components: [], without_logs: [], events: [] } });
  });
}

async function renderPage(props = {}) {
  container = document.createElement('div');
  document.body.appendChild(container);
  await act(async () => {
    root = createRoot(container);
    root.render(<LinkDeployments {...props} />);
  });
  return container;
}

async function openDeployment(props = {}) {
  const el = await renderPage(props);
  const row = el.querySelector('.lk-deprow');
  await act(async () => { row.click(); });
  return el;
}

afterEach(() => {
  if (root) act(() => root.unmount());
  if (container && container.parentNode) container.parentNode.removeChild(container);
  root = null;
  container = null;
  vi.clearAllMocks();
});

test('a selected deployment offers to watch that pipeline', async () => {
  mockApi();
  const seen = [];
  const el = await openDeployment({ onWatch: (l) => seen.push(l) });
  const watch = [...el.querySelectorAll('button')]
    .find((b) => /watch this pipeline/i.test(b.textContent));
  expect(watch).not.toBeNull();
  await act(async () => { watch.click(); });
  // It has to carry the link, or the monitor opens on whatever was last chosen.
  expect(seen).toEqual(['shop']);
});

test('without a watch handler the button is not offered', async () => {
  mockApi();
  const el = await openDeployment();
  const watch = [...el.querySelectorAll('button')]
    .find((b) => /watch this pipeline/i.test(b.textContent));
  expect(watch).toBeUndefined();
});

test('stack outputs show their values, not just the key names', async () => {
  mockApi();
  const el = await openDeployment();
  expect(el.textContent).toContain('PrimaryEndpoint');
  // The value is the reason to look at this section at all.
  expect(el.textContent).toContain('ecdl-shop-broker.abc.ng.0001.use2.cache.amazonaws.com');
  expect(el.textContent).toContain('ecdl-shop-cdc-debezium');
  expect(el.textContent).toContain('arn:aws:secretsmanager');
});

test('component logs are one click away, not open by default', async () => {
  mockApi();
  const el = await openDeployment();
  // Closed initially: most visits are about a deploy that worked, and each open
  // costs a CloudWatch call.
  expect(el.querySelector('.cl')).toBeNull();
  const toggle = [...el.querySelectorAll('button')]
    .find((b) => /show component logs/i.test(b.textContent));
  expect(toggle).not.toBeNull();
  await act(async () => { toggle.click(); });
  expect(el.querySelector('.cl')).not.toBeNull();
});

test('the log panel opens scoped to the deployed link', async () => {
  mockApi();
  const el = await openDeployment();
  const toggle = [...el.querySelectorAll('button')]
    .find((b) => /show component logs/i.test(b.textContent));
  await act(async () => { toggle.click(); });
  const logCalls = axios.get.mock.calls
    .map((c) => c[0]).filter((u) => u.includes('/api/monitor/logs'));
  expect(logCalls.length).toBeGreaterThan(0);
  logCalls.forEach((u) => expect(u).toContain('link=shop'));
});

test('a failed deployment still names the step it failed on', async () => {
  const failed = {
    ...DETAIL,
    status: 'failed',
    completed: ['broker'],
    failed_step: 'cdc',
    error: 'stack ecdl-shop-cdc ended in CREATE_FAILED',
    hint: 'Delete it and deploy again.',
  };
  axios.get.mockImplementation((url) => {
    if (url.includes('/api/links/deployments/')) return Promise.resolve({ data: failed });
    if (url.includes('/api/links/deployments')) {
      return Promise.resolve({ data: { deployments: [{ ...LIST.deployments[0],
        status: 'failed', completed: ['broker'] }] } });
    }
    return Promise.resolve({ data: { components: [], without_logs: [], events: [] } });
  });
  const el = await openDeployment({ onWatch: () => {} });
  expect(el.textContent).toContain('cdc');
  expect(el.textContent).toContain('CREATE_FAILED');
  expect(el.textContent).toContain('Delete it and deploy again.');
  // Logs are the point of this page for a failure, so the toggle must be there.
  expect([...el.querySelectorAll('button')]
    .find((b) => /show component logs/i.test(b.textContent))).not.toBeNull();
});

// ── Discovered deployments (read from CloudFormation) ──────────────────────
// These have no tracked run, so no mode and no events -- but they DO carry the
// wiring, which is the main purpose of this view: what is this link connected to, and is it
// running. Before this the row said only "3 of 3 steps" and clicking it returned a 404.
const DISCOVERED_COMPONENTS = {
  source: {
    engine: 'mysql', host: 'aurora-writer.example.rds.amazonaws.com',
    port: '3306', database: 'Inventory', user: 'admin', tables: 'Inventory.users',
  },
  broker: {
    host: 'master.ecdl-uitest-broker.example.cache.amazonaws.com', port: '6379',
    tls: 'true', offset_key: 'ecdl:uitest:offsets',
    schema_history_key: 'ecdl:uitest:schema_history',
  },
  target: {
    host: 'ecdl-uitest-target.example.cache.amazonaws.com', port: '6379',
    tls: 'false', cluster_mode: 'true',
  },
  streams: [{ stream: 'data:uitest.Inventory.users',
              dlq: 'dlq:data:uitest.Inventory.users' }],
  topic_prefix: 'uitest',
  runtime: {
    cluster: 'ecdl-uitest',
    debezium_service: 'ecdl-uitest-cdc-debezium',
    sink_service: 'ecdl-uitest-cdc-sink',
    sink_scaling: 'min=1 desired=2 max=10, target CPU 60%, 8 workers/task',
    services: [
      { name: 'ecdl-uitest-cdc-debezium', status: 'ACTIVE', desired: 1, running: 1 },
      { name: 'ecdl-uitest-cdc-sink', status: 'ACTIVE', desired: 1, running: 1 },
    ],
  },
};

const DISCOVERED_DETAIL = {
  id: 'stacks-uitest', link: 'uitest', status: 'succeeded',
  source: 'cloudformation', steps: ['broker', 'cdc', 'loader'],
  completed: ['broker', 'cdc', 'loader'],
  note: 'read from CloudFormation, not from a tracked run in this process',
  components: DISCOVERED_COMPONENTS,
};

const DISCOVERED_LIST = {
  deployments: [{
    id: 'stacks-uitest', link: 'uitest', status: 'succeeded',
    source: 'cloudformation', steps: ['broker', 'cdc', 'loader'],
    completed: ['broker', 'cdc', 'loader'], components: DISCOVERED_COMPONENTS,
  }],
};

function mockDiscovered() {
  axios.get.mockImplementation((url) => {
    if (url.includes('/api/links/deployments/')) {
      return Promise.resolve({ data: DISCOVERED_DETAIL });
    }
    if (url.includes('/api/links/deployments')) {
      return Promise.resolve({ data: DISCOVERED_LIST });
    }
    return Promise.resolve({ data: { components: [], without_logs: [], events: [] } });
  });
}

test('a discovered deployment shows what it is wired to', async () => {
  mockDiscovered();
  const el = await openDeployment();
  const text = el.textContent;
  // source, broker and target endpoints -- the thing you otherwise open the CLI for
  expect(text).toContain('aurora-writer.example.rds.amazonaws.com');
  expect(text).toContain('master.ecdl-uitest-broker.example.cache.amazonaws.com');
  expect(text).toContain('ecdl-uitest-target.example.cache.amazonaws.com');
  // the stream and its dead-letter queue
  expect(text).toContain('data:uitest.Inventory.users');
  expect(text).toContain('dlq:data:uitest.Inventory.users');
  // the offset key that lives in the broker
  expect(text).toContain('ecdl:uitest:offsets');
});

test('a discovered deployment shows live running/desired for its services', async () => {
  mockDiscovered();
  const el = await openDeployment();
  expect(el.textContent).toContain('ecdl-uitest-cdc-debezium');
  expect(el.textContent).toContain('ecdl-uitest-cdc-sink');
  // Both services fully up: two green running/desired chips reading 1/1.
  const chips = [...el.querySelectorAll('.lk-chip-succeeded')]
    .filter((c) => c.textContent === '1/1');
  expect(chips.length).toBe(2);
});

test('the list row summarises the topology without a click', async () => {
  mockDiscovered();
  const el = await renderPage();
  const row = el.querySelector('.lk-deprow');
  expect(row.textContent).toContain('mysql');
  expect(row.textContent).toContain('1 stream');
  expect(row.textContent).toContain('2/2 services up');
});

test('a discovered deployment has no mode row and does not 404 on open', async () => {
  mockDiscovered();
  const el = await openDeployment();
  // The detail rendered (wiring present) rather than staying on the empty prompt.
  expect(el.textContent).toContain('components');
  expect(el.textContent).not.toContain('Select a deployment');
  // No tracked run, so no mode line in the summary table.
  expect(el.querySelector('.lk-detailhead')).not.toBeNull();
});

// --- one row per pipeline -------------------------------------------------
// The page used to list every RUN, so a pipeline deployed twice read as two
// pipelines, and a deleted pipeline sat among the live ones.

test('runs are grouped per pipeline, latest run first', () => {
  const groups = groupByPipeline([
    { id: 'deploy-cxtest-1', link: 'cxtest', status: 'succeeded', started_at: 100 },
    { id: 'deploy-demo-1', link: 'demo', status: 'failed', started_at: 150, gone: true },
    { id: 'deploy-cxtest-2', link: 'cxtest', status: 'succeeded', started_at: 200 },
  ]);
  expect(groups.map((g) => g.latest.id)).toEqual(['deploy-cxtest-2', 'deploy-demo-1']);
  expect(groups[0].runs.map((r) => r.id)).toEqual(['deploy-cxtest-2', 'deploy-cxtest-1']);
});

test('a discovered pipeline with no tracked run is its own group', () => {
  const groups = groupByPipeline([
    { id: 'stacks-shop', link: 'shop', status: 'succeeded' },
    { id: 'deploy-cxtest-1', link: 'cxtest', status: 'running', started_at: 5 },
  ]);
  expect(groups.map((g) => g.latest.link).sort()).toEqual(['cxtest', 'shop']);
});

const THREE_RUNS = {
  deployments: [
    { id: 'deploy-cxtest-2', link: 'cxtest', status: 'succeeded', started_at: 300 },
    { id: 'deploy-demo-1', link: 'demo', status: 'failed', started_at: 200, gone: true },
    { id: 'deploy-cxtest-1', link: 'cxtest', status: 'succeeded', started_at: 100 },
  ],
};

test('two runs of one pipeline and a deleted one show as one row plus a toggle', async () => {
  axios.get.mockImplementation((url) => {
    if (url.includes('/api/links/deployments/')) {
      return Promise.resolve({ data: { ...DETAIL, id: 'deploy-cxtest-2', link: 'cxtest' } });
    }
    if (url.includes('/api/links/deployments')) return Promise.resolve({ data: THREE_RUNS });
    return Promise.resolve({ data: { components: [], without_logs: [], events: [] } });
  });
  const el = await renderPage();
  expect(el.querySelectorAll('.lk-deprow').length).toBe(1);
  expect(el.textContent).toContain('1 pipeline(s)');
  expect(el.textContent).toContain('2 runs (latest shown)');

  const toggle = el.querySelector('.lk-showdeleted');
  expect(toggle.textContent).toBe('Show 1 deleted pipeline(s)');
  await act(async () => { toggle.click(); });
  expect(el.querySelectorAll('.lk-deprow').length).toBe(2);
  expect(el.querySelector('.lk-showdeleted').textContent).toBe('Hide deleted pipelines');
});

// Jira story F059: a load that did not finish is marked on the loader stack; the page says so.
test('an incomplete initial load is shown on the row and the detail', async () => {
  const note = 'initial load incomplete: the last bulk load did not finish, so the '
    + 'cache may be missing rows. Deploy again with a reload to run it.';
  axios.get.mockImplementation((url) => {
    if (url.includes('/api/links/deployments/')) {
      return Promise.resolve({ data: { ...DETAIL, initial_load: 'incomplete',
        initial_load_incomplete: true, initial_load_note: note } });
    }
    if (url.includes('/api/links/deployments')) {
      return Promise.resolve({ data: { deployments: [{ ...LIST.deployments[0],
        initial_load: 'incomplete', initial_load_incomplete: true }] } });
    }
    return Promise.resolve({ data: { components: [], without_logs: [], events: [] } });
  });
  const el = await openDeployment();
  expect(el.querySelector('.lk-deprow').textContent).toContain('initial load incomplete');
  expect(el.textContent).toContain('Deploy again with a reload');
});

test('a complete or unmarked load shows nothing extra', async () => {
  mockApi();
  const el = await openDeployment();
  expect(el.textContent).not.toContain('initial load incomplete');
});

// --- the detail pane follows the selection (Jira story F191) --------------------------
// Selecting another pipeline left the previous detail on screen until the new
// one arrived, and a slow answer for the earlier selection could land after the
// new one and replace it.
describe('the detail pane follows the selection', () => {
  const TWO = { deployments: [
    { id: 'deploy-alpha-1', link: 'alpha', status: 'succeeded', started_at: 200,
      steps: ['broker'], completed: ['broker'] },
    { id: 'deploy-beta-1', link: 'beta', status: 'succeeded', started_at: 100,
      steps: ['broker'], completed: ['broker'] },
  ] };
  let pending;

  function detailOf(id) {
    const link = id.split('-')[1];
    return { id, link, status: 'succeeded', steps: ['broker'], completed: ['broker'],
             outputs: { broker: { Marker: `marker-${link}` } } };
  }

  // Every detail request waits until the test answers it, so the order of the
  // answers is the test's choice.
  beforeEach(() => {
    pending = [];
    axios.get.mockImplementation((url) => {
      const m = url.match(/\/api\/links\/deployments\/(.+)$/);
      if (m) return new Promise((resolve, reject) => pending.push({ id: m[1], resolve, reject }));
      if (url.includes('/api/links/deployments')) return Promise.resolve({ data: TWO });
      return Promise.resolve({ data: { components: [], without_logs: [], events: [] } });
    });
  });

  const rowOf = (el, link) => [...el.querySelectorAll('.lk-deprow')]
    .find((r) => r.querySelector('strong').textContent === link);
  const answer = async (p, data) => { await act(async () => { p.resolve({ data }); }); };
  const pane = (el) => el.querySelector('.lk-results').textContent;

  it('clears the old detail as soon as another pipeline is selected', async () => {
    const el = await renderPage();
    await act(async () => { rowOf(el, 'alpha').click(); });
    await answer(pending[0], detailOf('deploy-alpha-1'));
    expect(pane(el)).toContain('marker-alpha');
    await act(async () => { rowOf(el, 'beta').click(); });
    // beta has not answered yet: nothing of alpha is left in the pane.
    expect(pane(el)).not.toContain('marker-alpha');
    expect(pane(el)).toContain('Loading');
    await answer(pending.find((p) => p.id === 'deploy-beta-1'), detailOf('deploy-beta-1'));
    expect(pane(el)).toContain('marker-beta');
  });

  it('a late answer for the previous selection is dropped', async () => {
    const el = await renderPage();
    await act(async () => { rowOf(el, 'alpha').click(); });
    await act(async () => { rowOf(el, 'beta').click(); });
    const alpha = pending.find((p) => p.id === 'deploy-alpha-1');
    const beta = pending.find((p) => p.id === 'deploy-beta-1');
    await answer(beta, detailOf('deploy-beta-1'));
    await answer(alpha, detailOf('deploy-alpha-1'));
    expect(pane(el)).toContain('marker-beta');
    expect(pane(el)).not.toContain('marker-alpha');
    expect(el.querySelector('.lk-detailhead h2').textContent).toBe('beta');
  });

  it('an older poll of the same pipeline cannot replace a newer one', async () => {
    const el = await renderPage();
    await act(async () => { rowOf(el, 'alpha').click(); });
    // Wait for the second poll, so two requests for alpha are in flight.
    await act(async () => { await new Promise((r) => setTimeout(r, 4200)); });
    const polls = pending.filter((p) => p.id === 'deploy-alpha-1');
    expect(polls.length).toBeGreaterThanOrEqual(2);
    await answer(polls[1], { ...detailOf('deploy-alpha-1'), status: 'failed',
                             outputs: { broker: { Marker: 'newer' } } });
    await answer(polls[0], { ...detailOf('deploy-alpha-1'),
                             outputs: { broker: { Marker: 'older' } } });
    expect(pane(el)).toContain('newer');
    expect(pane(el)).not.toContain('older');
  }, 15000);

  it('clicking the selected row again keeps its detail', async () => {
    const el = await renderPage();
    await act(async () => { rowOf(el, 'alpha').click(); });
    await answer(pending[0], detailOf('deploy-alpha-1'));
    await act(async () => { rowOf(el, 'alpha').click(); });
    expect(pane(el)).toContain('marker-alpha');
  });

  it('a failed first load says so rather than loading forever', async () => {
    const el = await renderPage();
    await act(async () => { rowOf(el, 'beta').click(); });
    await act(async () => { pending[0].reject(new Error('Network Error')); });
    expect(pane(el)).toContain('Could not load deploy-beta-1: Network Error');
  });
});

// --- the list when the API fails, and "services up" (Jira story F192) ------------------
describe('the pipeline list', () => {
  const ONE = { deployments: [
    { id: 'deploy-alpha-1', link: 'alpha', status: 'succeeded', started_at: 200,
      steps: ['broker'], completed: ['broker'] }] };
  let listReplies;

  function fail(message, data) {
    const err = new Error(message);
    if (data) err.response = { status: 503, data };
    return err;
  }

  beforeEach(() => {
    listReplies = [];
    axios.get.mockImplementation((url) => {
      if (url.includes('/api/links/deployments/')) return Promise.resolve({ data: DETAIL });
      if (url.includes('/api/links/deployments')) {
        const next = listReplies.length > 1 ? listReplies.shift() : listReplies[0];
        if (typeof next === 'function') return next();
        return next instanceof Error ? Promise.reject(next) : Promise.resolve({ data: next });
      }
      return Promise.resolve({ data: {} });
    });
  });

  const refresh = async (el) => {
    const btn = [...el.querySelectorAll('button')].find((b) => b.textContent === 'Refresh');
    await act(async () => { btn.click(); });
  };

  it('a failed first load is not shown as "No pipelines"', async () => {
    listReplies = [fail('x', { error: 'list failed', reason: 'AWS credentials expired' })];
    const el = await renderPage();
    expect(el.textContent).not.toContain('No pipelines');
    expect(el.textContent).toContain('could not load the list');
    expect(el.querySelector('.lk-apierror').textContent)
      .toContain('list failed — AWS credentials expired');
  });

  it('a failed refresh keeps the last list, marked as stale', async () => {
    listReplies = [ONE, fail('Network Error')];
    const el = await renderPage();
    expect(el.querySelectorAll('.lk-deprow')).toHaveLength(1);
    expect(el.querySelector('.lk-stalelist')).toBeNull();
    await refresh(el);
    expect(el.querySelectorAll('.lk-deprow')).toHaveLength(1);
    expect(el.textContent).not.toContain('No pipelines');
    expect(el.querySelector('.lk-stalelist').textContent).toContain('last successful read');
    expect(el.querySelector('.lk-apierror').textContent).toContain('Network Error');
  });

  it('a successful refresh clears the stale mark', async () => {
    listReplies = [ONE, fail('Network Error'), ONE];
    const el = await renderPage();
    await refresh(el);
    expect(el.querySelector('.lk-stalelist')).toBeTruthy();
    await refresh(el);
    expect(el.querySelector('.lk-stalelist')).toBeNull();
    expect(el.querySelector('.lk-apierror')).toBeNull();
  });

  it('an empty list that loaded says "No pipelines"', async () => {
    listReplies = [{ deployments: [] }];
    const el = await renderPage();
    expect(el.textContent).toContain('No pipelines');
    expect(el.textContent).toContain('0 pipeline(s)');
  });

  it('an empty list that could not be refreshed does not claim "No pipelines"', async () => {
    listReplies = [{ deployments: [] }, fail('Network Error')];
    const el = await renderPage();
    expect(el.textContent).toContain('No pipelines');
    await refresh(el);
    expect(el.textContent).not.toContain('No pipelines');
    expect(el.querySelector('.lk-stalelist')).toBeTruthy();
  });

  it('an older list answer cannot replace a newer one', async () => {
    const held = [];
    listReplies = [() => new Promise((resolve) => held.push(resolve))];
    const el = await renderPage();
    await refresh(el);                       // a second load, also held
    expect(held).toHaveLength(2);
    const TWO = { deployments: ONE.deployments.concat([
      { id: 'deploy-beta-1', link: 'beta', status: 'running', started_at: 300,
        steps: ['broker'], completed: [] }]) };
    await act(async () => { held[1]({ data: TWO }); });
    await act(async () => { held[0]({ data: { deployments: [] } }); });
    expect(el.querySelectorAll('.lk-deprow')).toHaveLength(2);
    expect(el.textContent).not.toContain('No pipelines');
  });

  it('an older failure cannot mark a newer list as stale', async () => {
    const held = [];
    listReplies = [() => new Promise((resolve, reject) => held.push({ resolve, reject }))];
    const el = await renderPage();
    await refresh(el);
    await act(async () => { held[1].resolve({ data: ONE }); });
    await act(async () => { held[0].reject(fail('timeout')); });
    expect(el.querySelector('.lk-stalelist')).toBeNull();
    expect(el.querySelector('.lk-apierror')).toBeNull();
  });

  it('"services up" counts only services that are fully up', async () => {
    listReplies = [{ deployments: [{
      id: 'deploy-alpha-1', link: 'alpha', status: 'succeeded', started_at: 1,
      steps: [], completed: [],
      components: { source: { engine: 'mysql' }, streams: [], runtime: { services: [
        { name: 'dbz', running: 1, desired: 3 },     // partly up
        { name: 'sink', running: 2, desired: 2 },    // up
        { name: 'loader', running: 0, desired: 0 },  // scaled to zero, not up
        { name: 'extra', running: 2, desired: 1 },   // more than asked, up
      ] } } }] }];
    const el = await renderPage();
    expect(el.querySelector('.lk-deprow').textContent).toContain('2/4 services up');
  });
});

// ── Jira story F135: Debezium held at desired count 0 is paused ───────────────────────
// The backend lists a link with healthy stacks and Debezium at 0 as `paused`
// (and a tracked run of it as paused, with run_status succeeded). It used to
// render as a green "succeeded", which reads as "capturing" when nothing is.
describe('a paused pipeline (Jira story F135)', () => {
  const PAUSED_NOTE = 'capture is paused: the Debezium service desired count is 0.';
  const pausedComponents = {
    ...DISCOVERED_COMPONENTS,
    runtime: {
      ...DISCOVERED_COMPONENTS.runtime,
      services: [
        { name: 'ecdl-uitest-cdc-debezium', status: 'ACTIVE', desired: 0, running: 0 },
        { name: 'ecdl-uitest-cdc-sink', status: 'ACTIVE', desired: 1, running: 1 },
      ],
    },
  };
  function mockPaused({ listItem, detail }) {
    axios.get.mockImplementation((url) => {
      if (url.includes('/api/links/deployments/')) return Promise.resolve({ data: detail });
      if (url.includes('/api/links/deployments')) {
        return Promise.resolve({ data: { deployments: [listItem] } });
      }
      return Promise.resolve({ data: { components: [], without_logs: [], events: [] } });
    });
  }

  test('the list row shows a paused chip in its own style, not succeeded', async () => {
    const item = { ...DISCOVERED_LIST.deployments[0], status: 'paused',
                   capture_paused: true, capture_paused_note: PAUSED_NOTE,
                   components: pausedComponents };
    mockPaused({ listItem: item, detail: { ...DISCOVERED_DETAIL, ...item } });
    const el = await renderPage();
    const chip = el.querySelector('.lk-deprow .lk-chip');
    expect(chip.textContent).toBe('paused');
    expect(chip.className).toContain('lk-chip-paused');
    expect(chip.className).not.toContain('lk-chip-succeeded');
    expect(chip.className).not.toContain('lk-chip-unknown');
  });

  test('a discovered paused link says why in its detail', async () => {
    const item = { ...DISCOVERED_LIST.deployments[0], status: 'paused',
                   capture_paused: true, capture_paused_note: PAUSED_NOTE,
                   components: pausedComponents };
    mockPaused({ listItem: item, detail: { ...DISCOVERED_DETAIL, ...item } });
    const el = await openDeployment();
    const statusChip = [...el.querySelectorAll('.lk-results .lk-table tr')]
      .find((tr) => tr.querySelector('th') && tr.querySelector('th').textContent === 'status')
      .querySelector('.lk-chip');
    expect(statusChip.textContent).toBe('paused');
    expect(el.querySelector('.lk-capturepaused').textContent).toContain(PAUSED_NOTE);
    // Debezium at 0/0 is paused, not a red outage; the sink is still green.
    const dbz = [...el.querySelectorAll('.lk-chip')].find((c) => c.textContent === '0/0');
    expect(dbz.className).toContain('lk-chip-paused');
    expect(dbz.className).not.toContain('lk-chip-failed');
    expect([...el.querySelectorAll('.lk-chip-succeeded')]
      .some((c) => c.textContent === '1/1')).toBe(true);
  });

  test('a tracked run reads paused like its row, and keeps what the run did', async () => {
    // The detail endpoint of a tracked run still says succeeded; the list
    // says paused. The detail used to show the run's status alone, so the row
    // and the detail of the same pipeline disagreed.
    const listItem = { ...LIST.deployments[0], status: 'paused', run_status: 'succeeded',
                       capture_paused_note: PAUSED_NOTE };
    mockPaused({ listItem, detail: { ...DETAIL, components: pausedComponents } });
    const el = await openDeployment();
    const rows = [...el.querySelectorAll('.lk-results .lk-table tr')];
    const cell = (name) => rows.find((tr) => tr.querySelector('th')
      && tr.querySelector('th').textContent === name);
    expect(cell('status').querySelector('.lk-chip').textContent).toBe('paused');
    expect(cell('this run').querySelector('.lk-chip').textContent).toBe('succeeded');
    expect(el.querySelector('.lk-capturepaused').textContent).toContain(PAUSED_NOTE);
  });

  test('a running link shows no paused state', async () => {
    mockDiscovered();
    const el = await openDeployment();
    expect(el.querySelector('.lk-chip-paused')).toBeNull();
    expect(el.querySelector('.lk-capturepaused')).toBeNull();
    expect(el.textContent).not.toContain('this run');
  });

  test('a run that failed is not relabelled paused by the detail', async () => {
    // Only a succeeded run is turned into paused by the backend; a failed one
    // keeps failed on both the row and the detail.
    const listItem = { ...LIST.deployments[0], status: 'failed' };
    mockPaused({ listItem, detail: { ...DETAIL, status: 'failed', error: 'boom',
                                     components: pausedComponents } });
    const el = await openDeployment();
    const rows = [...el.querySelectorAll('.lk-results .lk-table tr')];
    const status = rows.find((tr) => tr.querySelector('th')
      && tr.querySelector('th').textContent === 'status');
    expect(status.querySelector('.lk-chip').textContent).toBe('failed');
    expect(el.querySelector('.lk-capturepaused')).toBeNull();
  });
});

describe('the chips', () => {
  let chipRoot;
  let chipBox;
  beforeEach(() => {
    chipBox = document.createElement('div');
    document.body.appendChild(chipBox);
    chipRoot = createRoot(chipBox);
  });
  afterEach(() => {
    act(() => chipRoot.unmount());
    chipBox.remove();
  });
  async function draw(element) {
    await act(async () => { chipRoot.render(element); });
    return chipBox.querySelector('.lk-chip');
  }

  test.each([
    ['running', 'lk-chip-running'], ['succeeded', 'lk-chip-succeeded'],
    ['failed', 'lk-chip-failed'], ['paused', 'lk-chip-paused'],
    ['interrupted', 'lk-chip-unknown'], [undefined, 'lk-chip-unknown'],
  ])('status %s is drawn as %s', async (status, cls) => {
    const chip = await draw(<StatusChip status={status} />);
    expect(chip.className).toBe(`lk-chip ${cls}`);
    expect(chip.textContent).toBe(status || 'unknown');
  });

  test.each([
    [{ desired: 1, running: 1 }, 'lk-chip-succeeded'],
    [{ desired: 2, running: 1 }, 'lk-chip-running'],
    [{ desired: 1, running: 0 }, 'lk-chip-failed'],
    // Desired 0 is a deliberate stop, so it is paused, not an outage.
    [{ desired: 0, running: 0 }, 'lk-chip-paused'],
    [{ desired: '0', running: '0' }, 'lk-chip-paused'],
    // Still draining after being set to 0: tasks are up, so it is running.
    [{ desired: 0, running: 1 }, 'lk-chip-running'],
  ])('service %j is drawn as %s', async (svc, cls) => {
    const chip = await draw(<ServiceChip svc={svc} />);
    expect(chip.className).toBe(`lk-chip ${cls}`);
  });
});
