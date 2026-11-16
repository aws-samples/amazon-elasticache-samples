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
import LinkDeployments, { groupByPipeline } from './LinkDeployments';

jest.mock('axios');

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
  jest.clearAllMocks();
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
