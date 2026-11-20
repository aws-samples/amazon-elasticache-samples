import { vi } from 'vitest';
/*
 * Tests for the Links page, focused on the one thing a wizard test cannot reach:
 * what is actually sent in the request.
 *
 * This file exists because of a bug that every component-level test missed. The
 * wizard correctly asked for a real deploy, and the page correctly had a deploy
 * function, and the request that left the browser still said dry_run: true --
 * because the handler ignored its argument and read a checkbox instead, and
 * because the two sides disagreed about whether the flag meant "real" or
 * "dryRun". The only place that shows up is the request body.
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import axios from 'axios';
import Links, { deployRunState } from './Links';

vi.mock('axios');
vi.mock('../authClient', () => ({
  getToken: () => 'test-token',
  onTokenChange: () => () => {},
}));

const SRC = 'arn:aws:rds:us-east-2:111122223333:cluster:shop';
const TGT = 'arn:aws:elasticache:us-east-2:111122223333:replicationgroup:cache';
const SEC = 'arn:aws:secretsmanager:us-east-2:111122223333:secret:shop-password';

const COMPLETE = {
  schema_version: 1,
  name: 'shop',
  mode: 'initial_load_and_cdc',
  source: { arn: SRC, database: 'Inventory', secret_arn: SEC },
  target: { arn: TGT },
  tables: ['orders'],
};

const INVENTORY = {
  discovered: true,
  sources: [{
    arn: SRC, identifier: 'shop', kind: 'cluster', engine: 'mysql',
    engine_label: 'MySQL', endpoint: 'shop.rds:3306', status: 'available',
    usable: true, blockers: [], database: 'Inventory',
  }],
  targets: [{
    arn: TGT, identifier: 'cache', endpoint: 'clustercfg.cache:6379',
    cluster_mode: true, shards: 3, node_type: 'cache.m7g.large', tls: true,
    auth_mode: 'none', needs_secret: false, status: 'available', usable: true,
    blockers: [], notes: [],
  }],
  secrets: [{ arn: SEC, name: 'shop-password', likely: true }],
  counts: { sources: 1, targets: 1, secrets: 1},
  truncated: {},
  problems: [],
};

let container;
let root;
let posts;

beforeEach(() => {
  vi.clearAllMocks();
  posts = [];
  container = window.document.createElement('div');
  window.document.body.appendChild(container);
  root = createRoot(container);

  axios.get.mockImplementation((url) => {
    if (url.includes('/api/auth/status')) {
      return Promise.resolve({ data: { enabled: false, configured: true } });
    }
    if (url.includes('/api/links/discover')) {
      return Promise.resolve({ data: INVENTORY });
    }
    return Promise.resolve({ data: {} });
  });

  axios.post.mockImplementation((url, body) => {
    posts.push({ url, body });
    if (url.includes('/api/links/parse')) {
      return Promise.resolve({
        data: { parsed: true, document: COMPLETE, valid: true,
                problems: [], normalized: COMPLETE },
      });
    }
    if (url.includes('/api/links/format')) {
      return Promise.resolve({ data: { valid: true, text: 'name: shop\n',
                                       problems: [] } });
    }
    if (url.includes('/api/links/deploy')) {
      return Promise.resolve({
        data: body.dry_run
          ? { dry_run: true, plan: { steps: [], cluster_name: 'ecdl-shop' } }
          : { deployment_id: 'dep-1', link: 'shop', status: 'running',
              steps: ['broker'], completed: [] },
      });
    }
    return Promise.resolve({ data: {} });
  });
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

async function renderPage() {
  await act(async () => { root.render(<Links />); });
  // The page debounces its parse; let it settle so `canAct` becomes true. The
  // wizard also fills defaults on load, which is formatted (350 ms) and then
  // parsed (500 ms) again before the actions are allowed.
  await settle();
}

// Clicking the Guided tab seeds the wizard from the currently parsed text, which
// is how anyone arrives at a populated form. Without it the wizard opens on an
// empty document and Next is correctly blocked on step one.
async function toReviewStep() {
  await click(buttonByText('Guided'));
  for (let i = 0; i < 5; i += 1) {
    const next = Array.from(container.querySelectorAll('.wz-nav button'))
      .find((b) => b.textContent === 'Next' || b.textContent === 'Review');
    if (!next || next.disabled) break;
    await click(next);
  }
  // The wizard fills defaults as it goes, and each change is formatted and
  // parsed again before Deploy is allowed (Jira story F186). Let that settle.
  await settle();
}
// Several short act() windows rather than one long one: React applies a state
// update made inside act() when that act() ends, so a format -> setText ->
// parse chain only advances one link per window.
async function settle(rounds = 6) {
  for (let i = 0; i < rounds; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 300)); });
  }
}

function buttonByText(text) {
  return Array.from(container.querySelectorAll('button'))
    .find((b) => b.textContent === text);
}

async function click(el) {
  await act(async () => {
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
}

function deployCalls() {
  return posts.filter((p) => p.url.includes('/api/links/deploy'));
}

describe('deploying from the guided view', () => {
  it('sends dry_run false when a real deploy is confirmed', async () => {
    await renderPage();
    await toReviewStep();
    const arm = buttonByText('Confirm and deploy');
    expect(arm).toBeTruthy();
    await click(arm);
    await click(buttonByText('Yes, deploy it'));

    const calls = deployCalls();
    expect(calls).toHaveLength(1);
    // The assertion the old code would have failed.
    expect(calls[0].body.dry_run).toBe(false);
  });

  it('sends dry_run true for the plan button, which must create nothing', async () => {
    await renderPage();
    await toReviewStep();
    await click(buttonByText('Show what it will create'));
    // Plan is its own endpoint; it must not reach deploy at all.
    expect(deployCalls()).toHaveLength(0);
    expect(posts.some((p) => p.url.includes('/api/links/plan'))).toBe(true);
  });

  it('the first click alone sends nothing', async () => {
    await renderPage();
    await toReviewStep();
    await click(buttonByText('Confirm and deploy'));
    expect(deployCalls()).toHaveLength(0);
  });

  it('shows a refusal that arrives as HTTP 200 instead of hiding it', async () => {
    // This API reports verdicts in the body, not the status, so a deploy that is
    // refused for a good reason still returns 200. The handler used to react only
    // to `dry_run` and `deployment_id`, so a reasoned refusal looked identical to
    // the button doing nothing -- on the one button that costs money.
    axios.post.mockImplementation((url, body) => {
      posts.push({ url, body });
      if (url.includes('/api/links/parse')) {
        return Promise.resolve({
          data: { parsed: true, document: COMPLETE, valid: true,
                  problems: [], normalized: COMPLETE },
        });
      }
      if (url.includes('/api/links/format')) {
        return Promise.resolve({ data: { valid: true, text: 'name: shop\n',
                                         problems: [] } });
      }
      if (url.includes('/api/links/deploy')) {
        return Promise.resolve({
          data: { deployed: false, stage: 'clients',
                  error: 'no usable AWS credentials',
                  hint: 'Refresh them.' },
        });
      }
      return Promise.resolve({ data: {} });
    });
    await renderPage();
    await toReviewStep();
    await click(buttonByText('Confirm and deploy'));
    await click(buttonByText('Yes, deploy it'));
    expect(container.textContent).toContain('no usable AWS credentials');
    expect(container.textContent).toContain('Refresh them.');
  });
});

describe('the run history', () => {
  // The pane used to hold one panel per KIND of result and always show the latest
  // of each, so a Resolve from ten minutes ago sat directly above a fresh Deploy
  // with nothing saying they described different configurations. Someone read a
  // stale credential off that screen and believed it was current.
  async function resolveThenPlan() {
    axios.post.mockImplementation((url, body) => {
      posts.push({ url, body });
      if (url.includes('/api/links/parse')) {
        return Promise.resolve({
          data: { parsed: true, document: COMPLETE, valid: true,
                  problems: [], normalized: COMPLETE },
        });
      }
      if (url.includes('/api/links/format')) {
        return Promise.resolve({ data: { valid: true, text: 'name: shop\n',
                                         problems: [] } });
      }
      if (url.includes('/api/links/resolve')) {
        return Promise.resolve({
          data: { resolved: true, resolution: {
            source: { engine_label: 'MySQL', endpoint: 'shop.rds', port: 3306,
                      username: 'admin', secret: { name: 'shop-password' } },
            target: { endpoint: 'cache:6379', port: 6379, node_type: 'm7g',
                      shards: 3, cluster_mode: true, tls: true, auth_mode: 'none' },
            network: { vpc_id: 'vpc-1', subnet_ids: ['subnet-1'] },
            tables: [{ qualified: 'Inventory.orders', stream: 'data:x',
                       key_prefix: 'orders:' }],
            warnings: [], notes: [],
          } },
        });
      }
      if (url.includes('/api/links/plan')) {
        return Promise.resolve({
          data: { planned: true, plan: { cluster_name: 'ecdl-shop', steps: [
            { id: 'broker', kind: 'stack', name: 'ecdl-shop-broker' },
            { id: 'cdc', kind: 'stack', name: 'ecdl-shop-cdc' }] } },
        });
      }
      return Promise.resolve({ data: {} });
    });
    await renderPage();
    await click(buttonByText('File'));          // the compact action row
    await click(buttonByText('Resolve'));
    await click(buttonByText('Plan'));
  }

  it('lists every run, newest first, instead of one panel per kind', async () => {
    await resolveThenPlan();
    const heads = Array.from(container.querySelectorAll('.lk-runkind'))
      .map((e) => e.textContent);
    expect(heads).toEqual(['Plan', 'Resolve']);
  });

  it('stamps each run with the link it was for', async () => {
    await resolveThenPlan();
    const links = Array.from(container.querySelectorAll('.lk-runlink'))
      .map((e) => e.textContent);
    expect(links.every((l) => l === 'shop')).toBe(true);
  });

  it('summarises the outcome without expanding', async () => {
    await resolveThenPlan();
    const status = Array.from(container.querySelectorAll('.lk-runstatus'))
      .map((e) => e.textContent);
    expect(status[0]).toContain('2 steps, nothing created');
    expect(status[1]).toContain('read the source, target and tables');
  });

  it('marks older runs as superseded when opened', async () => {
    await resolveThenPlan();
    // The newest is expanded by default; open the older one.
    const olderHead = container.querySelectorAll('.lk-runhead')[1];
    await click(olderHead);
    expect(container.querySelector('.lk-stalenote')).toBeTruthy();
    expect(container.querySelector('.lk-stalenote').textContent)
      .toContain('different configuration');
  });

  it('does not mark the newest run as superseded', async () => {
    await resolveThenPlan();
    // Newest is open already, and it is current, so no note.
    expect(container.querySelector('.lk-stalenote')).toBeNull();
  });

  it('shows the detail of the run that is open, not a merged view', async () => {
    await resolveThenPlan();
    // Plan is newest and open: its steps show, and the resolve detail does not.
    expect(container.textContent).toContain('ecdl-shop-broker');
    expect(container.textContent).not.toContain('shop.rds');
    const olderHead = container.querySelectorAll('.lk-runhead')[1];
    await click(olderHead);
    expect(container.textContent).toContain('shop.rds');
  });

  it('says nothing has run yet before anything has', async () => {
    await renderPage();
    expect(container.querySelector('.lk-empty').textContent)
      .toContain('Nothing run yet');
  });

  it('a deployment appears as a run and reports its progress', async () => {
    await renderPage();
    await toReviewStep();
    await click(buttonByText('Confirm and deploy'));
    await click(buttonByText('Yes, deploy it'));
    const kinds = Array.from(container.querySelectorAll('.lk-runkind'))
      .map((e) => e.textContent);
    expect(kinds).toContain('Deploy');
    expect(container.querySelector('.lk-runstatus-running')).toBeTruthy();
  });
});

describe('the two views', () => {
  it('opens on the guided view', async () => {
    await renderPage();
    expect(container.querySelector('.wz-steps')).toBeTruthy();
  });

  it('the file view shows the text and the compact action row', async () => {
    await renderPage();
    await click(buttonByText('File'));
    expect(container.querySelector('.lk-editor')).toBeTruthy();
    expect(container.querySelector('.lk-actions')).toBeTruthy();
  });

  it('does not list required-field problems while still filling the form in', async () => {
    // Six "is required" errors against a form on its first step looks like a failure.
    axios.post.mockImplementation((url) => {
      if (url.includes('/api/links/parse')) {
        return Promise.resolve({
          data: { parsed: true, document: {}, valid: false,
                  problems: [{ path: 'name', message: 'is required' }] },
        });
      }
      return Promise.resolve({ data: { valid: false, text: '', problems: [] } });
    });
    await renderPage();
    expect(container.querySelector('.lk-problems')).toBeNull();
    expect(container.textContent).toContain('not finished yet');
  });
});


describe('deploying a link that already exists', () => {
  // The guard: a second deploy of the same link must ask, not silently update.
  // The backend answers 409 { already_deployed } and the page turns that into a
  // confirm prompt rather than the red error banner call() would produce.
  function mockDeployAlreadyExists() {
    axios.post.mockImplementation((url, body) => {
      posts.push({ url, body });
      if (url.includes('/api/links/parse')) {
        return Promise.resolve({
          data: { parsed: true, document: COMPLETE, valid: true,
                  problems: [], normalized: COMPLETE },
        });
      }
      if (url.includes('/api/links/format')) {
        return Promise.resolve({ data: { valid: true, text: 'name: shop\n',
                                         problems: [] } });
      }
      if (url.includes('/api/links/deploy')) {
        if (body.confirm_redeploy) {
          return Promise.resolve({
            data: { deployment_id: 'dep-2', link: 'shop', status: 'running',
                    steps: ['broker'], completed: [] },
          });
        }
        const err = new Error('Conflict');
        err.response = { status: 409, data: {
          deployed: false, already_deployed: true, link: 'shop',
          existing_stacks: { 'ecdl-shop-cdc': 'CREATE_COMPLETE' },
          message: 'A pipeline for \'shop\' is already deployed '
                   + '(ecdl-shop-cdc CREATE_COMPLETE).',
          hint: 'Send confirm_redeploy true to update it.' } };
        return Promise.reject(err);
      }
      return Promise.resolve({ data: {} });
    });
  }

  async function armAndDeploy() {
    await renderPage();
    await toReviewStep();
    await click(buttonByText('Confirm and deploy'));
    await click(buttonByText('Yes, deploy it'));
  }

  it('shows a confirm prompt instead of an error when the link exists', async () => {
    mockDeployAlreadyExists();
    await armAndDeploy();
    // The prompt, not the red banner.
    expect(container.querySelector('.lk-redeploy')).toBeTruthy();
    expect(container.querySelector('.lk-apierror')).toBeNull();
    expect(container.textContent).toContain('already deployed');
    // And it must not have started a deployment yet.
    expect(deployCalls().every((c) => !c.body.confirm_redeploy)).toBe(true);
  });

  it('confirming re-sends the deploy with confirm_redeploy', async () => {
    mockDeployAlreadyExists();
    await armAndDeploy();
    await click(buttonByText('Update the existing pipeline'));
    const confirmed = deployCalls().filter((c) => c.body.confirm_redeploy);
    expect(confirmed.length).toBe(1);
    expect(confirmed[0].body.dry_run).toBe(false);
  });

  it('cancelling dismisses the prompt and starts nothing', async () => {
    mockDeployAlreadyExists();
    await armAndDeploy();
    await click(buttonByText('Cancel'));
    expect(container.querySelector('.lk-redeploy')).toBeNull();
    expect(deployCalls().some((c) => c.body.confirm_redeploy)).toBe(false);
  });
});

describe('create-only build pipeline', () => {
  it('has no edit-mode controls (editing moved to the Deployments page)', async () => {
    await renderPage();
    expect(buttonByText('Create new pipeline')).toBeUndefined();
    expect(buttonByText('Edit existing pipeline')).toBeUndefined();
    expect(container.querySelector('.lk-editexisting')).toBeNull();
    expect(container.querySelector('.lk-modetabs')).toBeNull();
  });
});

/* Jira story F186: Deploy from the Guided view sends exactly the document on the Review
 * step, never the last one the page happened to parse. These mocks round-trip
 * the document: format writes it as JSON text and parse reads it back (over
 * COMPLETE, so it is a whole link), stamping a parse counter the way the real
 * normaliser adds defaults. So "the latest parsed document" and "the reviewed
 * document" are distinguishable in the request body. */
describe('guided deploy sends the reviewed document', () => {
  // Each test waits out real debounces (format 350 ms, parse 500 ms) several times.
  vi.setConfig({ testTimeout: 30000 });
  let parseGate;          // when set, parse waits for it: { promise }
  let parseCount;
  let parseFails;         // when true, parse rejects
  let formatFails;        // when true, format rejects

  function lastFormatted() {
    const f = posts.filter((p) => p.url.includes('/api/links/format'));
    return f.length ? f[f.length - 1].body.link : null;
  }

  function parseReply(text) {
    let doc;
    try { doc = { ...COMPLETE, ...JSON.parse(text) }; } catch (e) { doc = COMPLETE; }
    parseCount += 1;
    const normalised = { ...doc, parse_count: parseCount };
    return { data: { parsed: true, document: normalised, valid: true,
                     problems: [], normalized: normalised } };
  }

  beforeEach(() => {
    parseGate = null;
    parseCount = 0;
    parseFails = false;
    formatFails = false;
    axios.post.mockImplementation((url, body) => {
      posts.push({ url, body });
      if (url.includes('/api/links/parse')) {
        if (parseFails) {
          const err = new Error('parse failed');
          err.response = { status: 503, data: { error: 'parse unavailable',
                                                reason: 'backend down' } };
          return Promise.reject(err);
        }
        if (parseGate) return parseGate.promise.then(() => parseReply(body.text));
        return Promise.resolve(parseReply(body.text));
      }
      if (url.includes('/api/links/format')) {
        if (formatFails) {
          const err = new Error('format failed');
          err.response = { status: 500, data: { error: 'format failed',
                                                reason: 'renderer crashed' } };
          return Promise.reject(err);
        }
        return Promise.resolve({ data: { valid: true, problems: [],
                                         text: JSON.stringify(body.link) } });
      }
      if (url.includes('/api/links/deploy')) {
        return Promise.resolve({ data: { deployment_id: 'dep-9', link: body.link.name,
                                         status: 'running', steps: [], completed: [] } });
      }
      return Promise.resolve({ data: {} });
    });
  });

  function gate() {
    let release;
    const promise = new Promise((r) => { release = r; });
    parseGate = { promise, release };
    return parseGate;
  }

  async function typeName(value) {
    const input = container.querySelector('input[placeholder="orders-cache"]');
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  // Back to the first step, rename, and straight on to Review without waiting,
  // the way a quick hand would.
  async function renameAndReview(name) {
    await click(Array.from(container.querySelectorAll('.wz-steps button'))[0]);
    await typeName(name);
    for (let i = 0; i < 5; i += 1) {
      const next = Array.from(container.querySelectorAll('.wz-nav button'))
        .find((b) => b.textContent === 'Next' || b.textContent === 'Review');
      if (!next || next.disabled) break;
      await click(next);
    }
  }

  const armButton = () => buttonByText('Confirm and deploy');
  const summaryName = () => Array.from(container.querySelectorAll('.wz-summary tr'))
    .find((r) => r.querySelector('th').textContent === 'name')
    .querySelector('td').textContent;

  it('the deploy payload equals the reviewed document', async () => {
    await renderPage();
    await toReviewStep();
    await renameAndReview('reviewed-name');
    await settle();
    expect(summaryName()).toBe('reviewed-name');
    await click(armButton());
    await click(buttonByText('Yes, deploy it'));
    const calls = deployCalls();
    expect(calls).toHaveLength(1);
    // Exactly what the wizard holds (its last formatted copy), not the parsed one.
    expect(calls[0].body.link).toEqual(lastFormatted());
    expect(calls[0].body.link.name).toBe('reviewed-name');
    // The page's latest parse stamped a newer count than the wizard's copy
    // carries; sending that one would be sending a different document.
    expect(calls[0].body.link.parse_count).toBeLessThan(parseCount);
  });

  it('resolve and plan from the Review step use the reviewed document too', async () => {
    await renderPage();
    await toReviewStep();
    await renameAndReview('checked-name');
    await settle();
    await click(buttonByText('Check the infrastructure'));
    await click(buttonByText('Show what it will create'));
    const resolve = posts.find((p) => p.url.includes('/api/links/resolve'));
    const plan = posts.find((p) => p.url.includes('/api/links/plan'));
    expect(resolve.body.link).toEqual(lastFormatted());
    expect(plan.body.link).toEqual(lastFormatted());
    expect(plan.body.link.name).toBe('checked-name');
    expect(plan.body.link.parse_count).toBeLessThan(parseCount);
  });

  it('Deploy is disabled while the latest edit is being formatted or parsed', async () => {
    await renderPage();
    await toReviewStep();
    const g = gate();
    await renameAndReview('pending-name');
    // Formatting has not even started (350 ms debounce).
    expect(armButton().disabled).toBe(true);
    expect(container.textContent).toContain('Checking the latest change');
    await settle();
    // Formatted, but the parse is held open: still off.
    expect(lastFormatted().name).toBe('pending-name');
    expect(armButton().disabled).toBe(true);
    expect(buttonByText('Check the infrastructure').disabled).toBe(true);
    expect(buttonByText('Show what it will create').disabled).toBe(true);
    await act(async () => { g.release(); });
    await settle();
    expect(armButton().disabled).toBe(false);
    expect(container.textContent).not.toContain('Checking the latest change');
  });

  it('the confirm step cannot deploy while a newer edit is unchecked', async () => {
    await renderPage();
    await toReviewStep();
    await click(armButton());
    expect(buttonByText('Yes, deploy it').disabled).toBe(false);
    // Edit after arming: the wizard keeps `armed`, but the confirm button must
    // follow the check.
    gate();
    await renameAndReview('after-arming');
    const yes = buttonByText('Yes, deploy it');
    if (yes) expect(yes.disabled).toBe(true);
    else expect(armButton().disabled).toBe(true);
    expect(deployCalls()).toHaveLength(0);
  });

  it('Deploy stays disabled after a parse error', async () => {
    await renderPage();
    await toReviewStep();
    parseFails = true;
    await renameAndReview('broken-parse');
    await settle();
    expect(armButton().disabled).toBe(true);
    expect(container.textContent).toContain('could not be checked');
    expect(container.querySelector('.lk-apierror').textContent).toContain('backend down');
  });

  it('Deploy stays disabled after a format error', async () => {
    await renderPage();
    await toReviewStep();
    formatFails = true;
    await renameAndReview('broken-format');
    await settle();
    expect(armButton().disabled).toBe(true);
    expect(container.textContent).toContain('renderer crashed');
    expect(container.textContent).toContain('could not be checked');
  });

  it('confirming a redeploy re-sends the document that was refused', async () => {
    const base = axios.post.getMockImplementation();
    axios.post.mockImplementation((url, body) => {
      if (url.includes('/api/links/deploy') && !body.confirm_redeploy) {
        posts.push({ url, body });
        const err = new Error('Conflict');
        err.response = { status: 409, data: { already_deployed: true, link: 'shop',
                                              message: 'already deployed' } };
        return Promise.reject(err);
      }
      return base(url, body);
    });
    await renderPage();
    await toReviewStep();
    await renameAndReview('redeploy-me');
    await settle();
    await click(armButton());
    await click(buttonByText('Yes, deploy it'));
    await click(buttonByText('Update the existing pipeline'));
    const [first, confirmed] = deployCalls();
    expect(confirmed.body.confirm_redeploy).toBe(true);
    expect(confirmed.body.link).toEqual(first.body.link);
    expect(confirmed.body.link.name).toBe('redeploy-me');
    expect(confirmed.body.link.parse_count).toBeLessThan(parseCount);
  });

  // Jira story F187: from the File view too, and after the text has been edited while the
  // prompt was open. "Update" answers the question about the refused document,
  // so it must send that one, not whatever the editor holds now.
  it('a File-view redeploy re-sends the refused document after an edit', async () => {
    const base = axios.post.getMockImplementation();
    axios.post.mockImplementation((url, body) => {
      if (url.includes('/api/links/deploy') && !body.confirm_redeploy) {
        posts.push({ url, body });
        const err = new Error('Conflict');
        err.response = { status: 409, data: { already_deployed: true,
                                              link: body.link.name,
                                              message: 'already deployed' } };
        return Promise.reject(err);
      }
      return base(url, body);
    });
    await renderPage();
    await click(buttonByText('File'));
    const typeText = async (value) => {
      const editor = container.querySelector('.lk-editor');
      await act(async () => {
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(editor, value);
        editor.dispatchEvent(new Event('input', { bubbles: true }));
      });
    };
    await typeText(JSON.stringify({ ...COMPLETE, name: 'refused-one' }));
    await settle();
    await click(buttonByText('Deploy'));
    await click(buttonByText('Yes, deploy it'));
    expect(container.querySelector('.lk-redeploy')).toBeTruthy();
    // Edit while the prompt is open, and let the edit be parsed.
    await typeText(JSON.stringify({ ...COMPLETE, name: 'edited-after' }));
    await settle();
    expect(container.querySelector('.lk-status').textContent).toContain('edited-after');
    await click(buttonByText('Update and reload all tables'));
    const [first, confirmed] = deployCalls();
    expect(first.body.link.name).toBe('refused-one');
    expect(confirmed.body.confirm_redeploy).toBe(true);
    expect(confirmed.body.reload).toBe(true);
    expect(confirmed.body.link).toEqual(first.body.link);
  });

  it('Deploy waits for a parse in flight even when the text matches an older check', async () => {
    // A -> B -> back to A. The page holds a verdict for A's text from the first
    // round, but a parse is still running; the actions wait for it.
    await renderPage();
    await toReviewStep();
    await renameAndReview('aaa');
    await settle();
    expect(armButton().disabled).toBe(false);
    gate();
    await renameAndReview('bbb');
    await settle();
    await renameAndReview('aaa');
    await settle();
    expect(lastFormatted().name).toBe('aaa');
    expect(armButton().disabled).toBe(true);
    await act(async () => { parseGate.release(); });
    await settle();
    expect(armButton().disabled).toBe(false);
  });

  it('the File view also waits for the parse of the text on screen', async () => {
    await renderPage();
    await click(buttonByText('File'));
    expect(buttonByText('Deploy').disabled).toBe(false);
    const editor = container.querySelector('.lk-editor');
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(editor, JSON.stringify({ ...COMPLETE, name: 'typed' }));
      editor.dispatchEvent(new Event('input', { bubbles: true }));
    });
    // Typed but not parsed yet: the verdict on screen is for the old text.
    expect(buttonByText('Deploy').disabled).toBe(true);
    expect(buttonByText('Plan').disabled).toBe(true);
    await settle();
    expect(buttonByText('Deploy').disabled).toBe(false);
  });

  it('a late parse response cannot replace a newer one', async () => {
    // Two parses in flight; the OLDER one answers last. Its verdict must not
    // become the page's check.
    const pending = [];
    axios.post.mockImplementation((url, body) => {
      posts.push({ url, body });
      if (url.includes('/api/links/parse')) {
        return new Promise((resolve) => { pending.push({ body, resolve }); });
      }
      if (url.includes('/api/links/format')) {
        return Promise.resolve({ data: { text: JSON.stringify(body.link) } });
      }
      return Promise.resolve({ data: {} });
    });
    await act(async () => { root.render(<Links />); });
    await settle();
    await click(buttonByText('File'));
    const editor = container.querySelector('.lk-editor');
    const typeText = async (value) => {
      await act(async () => {
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(editor, value);
        editor.dispatchEvent(new Event('input', { bubbles: true }));
      });
    };
    await typeText(JSON.stringify({ ...COMPLETE, name: 'older' }));
    await settle(3);
    await typeText(JSON.stringify({ ...COMPLETE, name: 'newer' }));
    await settle(3);
    const older = pending.find((p) => p.body.text.includes('"older"'));
    const newer = pending.find((p) => p.body.text.includes('"newer"'));
    expect(older && newer).toBeTruthy();
    await act(async () => { newer.resolve(parseReply(newer.body.text)); });
    await settle(1);
    await act(async () => { older.resolve({ data: { parsed: true, valid: false,
      document: { ...COMPLETE, name: 'older' }, normalized: {},
      problems: [{ path: 'name', message: 'stale verdict' }] } }); });
    await settle(1);
    // The newer verdict stands: valid, for "newer".
    expect(container.querySelector('.lk-status').textContent).toContain('valid');
    expect(container.querySelector('.lk-status').textContent).toContain('newer');
    expect(container.textContent).not.toContain('stale verdict');
    expect(buttonByText('Deploy').disabled).toBe(false);
  });

  it('a late format response cannot replace a newer edit', async () => {
    const formats = [];
    axios.post.mockImplementation((url, body) => {
      posts.push({ url, body });
      if (url.includes('/api/links/parse')) return Promise.resolve(parseReply(body.text));
      if (url.includes('/api/links/format')) {
        return new Promise((resolve) => { formats.push({ body, resolve }); });
      }
      if (url.includes('/api/links/deploy')) {
        return Promise.resolve({ data: { deployment_id: 'd', link: body.link.name,
                                         status: 'running', steps: [], completed: [] } });
      }
      return Promise.resolve({ data: {} });
    });
    await act(async () => { root.render(<Links />); });
    await settle();
    // Release whatever the wizard formatted on load, then reach Review.
    await act(async () => { formats.splice(0).forEach((f) => f.resolve(
      { data: { text: JSON.stringify(f.body.link) } })); });
    await settle();
    await toReviewStep();
    await act(async () => { formats.splice(0).forEach((f) => f.resolve(
      { data: { text: JSON.stringify(f.body.link) } })); });
    await settle();
    await renameAndReview('first');
    await settle(2);
    await renameAndReview('second');
    await settle(2);
    const first = formats.find((f) => f.body.link.name === 'first');
    const second = formats.find((f) => f.body.link.name === 'second');
    expect(first && second).toBeTruthy();
    await act(async () => { second.resolve({ data: { text: JSON.stringify(second.body.link) } }); });
    await settle(3);
    await act(async () => { first.resolve({ data: { text: JSON.stringify(first.body.link) } }); });
    await settle(3);
    expect(container.querySelector('.lk-status').textContent).toContain('second');
    expect(armButton().disabled).toBe(false);
    await click(armButton());
    await click(buttonByText('Yes, deploy it'));
    expect(deployCalls()[0].body.link.name).toBe('second');
  });
});

/* Jira story F189: the run-history line for a deployment. Only `succeeded` is done; an
 * interrupted deploy read "all N steps done", and a run that left capture
 * paused did not say so anywhere on this page. */
describe('deployment run line', () => {
  const rec = (extra) => ({ steps: ['broker', 'cdc', 'loader'],
                            completed: ['broker'], ...extra });

  it('succeeded is the only done state', () => {
    expect(deployRunState(rec({ status: 'succeeded',
                                completed: ['broker', 'cdc', 'loader'] })))
      .toEqual({ status: 'ok', headline: 'all 3 steps done' });
  });

  it('running shows the step in progress', () => {
    expect(deployRunState(rec({ status: 'running' })))
      .toEqual({ status: 'running', headline: 'step 2 of 3' });
  });

  it('interrupted is a problem and says how far it got', () => {
    const out = deployRunState(rec({ status: 'interrupted' }));
    expect(out.status).toBe('bad');
    expect(out.headline).toBe('interrupted after 1 of 3 steps');
    expect(out.headline).not.toContain('done');
  });

  it('failed names the step', () => {
    expect(deployRunState(rec({ status: 'failed', failed_step: 'cdc' })))
      .toEqual({ status: 'bad', headline: 'failed at cdc' });
  });

  it('an unknown status is not reported as done', () => {
    const out = deployRunState(rec({ status: 'mystery' }));
    expect(out.status).toBe('bad');
    expect(out.headline).toBe('mystery: 1 of 3 steps done');
    expect(deployRunState({}).headline).toBe('unknown: 0 of 0 steps done');
  });

  it('capture left paused is in the line of a stopped run', () => {
    expect(deployRunState(rec({ status: 'interrupted', capture_paused: true })).headline)
      .toBe('interrupted after 1 of 3 steps -- capture left paused');
    expect(deployRunState(rec({ status: 'failed', failed_step: 'loader',
                                capture_paused: true })).headline)
      .toBe('failed at loader -- capture left paused');
    // false means it was resumed after the failure.
    expect(deployRunState(rec({ status: 'failed', capture_paused: false })).headline)
      .not.toContain('paused');
  });

  async function deployFromFile() {
    await renderPage();
    await click(buttonByText('File'));
    await click(buttonByText('Deploy'));
    await click(buttonByText('Yes, deploy it'));
  }

  it('the page shows an interrupted run with capture paused as such', async () => {
    const baseGet = axios.get.getMockImplementation();
    axios.get.mockImplementation((url) => {
      if (url.includes('/api/links/deployments/dep-1')) {
        return Promise.resolve({ data: {
          deployment_id: 'dep-1', link: 'shop', status: 'interrupted',
          steps: ['broker', 'cdc', 'pause_capture', 'loader'],
          completed: ['broker', 'cdc'], capture_paused: true,
          error: 'the control plane restarted while this deploy was running',
          hint: 'CAPTURE WAS LEFT PAUSED. Resume it.' } });
      }
      return baseGet(url);
    });
    await deployFromFile();
    await settle(2);
    const line = container.querySelector('.lk-runstatus').textContent;
    expect(line).toBe('interrupted after 2 of 4 steps -- capture left paused');
    expect(container.querySelector('.lk-runstatus-bad')).toBeTruthy();
    expect(container.querySelector('.lk-capturepaused').textContent)
      .toContain('Capture is paused');
  });

  it('a succeeded run shows no capture warning', async () => {
    const baseGet = axios.get.getMockImplementation();
    axios.get.mockImplementation((url) => {
      if (url.includes('/api/links/deployments/dep-1')) {
        return Promise.resolve({ data: {
          deployment_id: 'dep-1', link: 'shop', status: 'succeeded',
          steps: ['broker'], completed: ['broker'], capture_paused: false } });
      }
      return baseGet(url);
    });
    await deployFromFile();
    await settle(2);
    expect(container.querySelector('.lk-runstatus').textContent).toBe('all 1 steps done');
    expect(container.querySelector('.lk-capturepaused')).toBeNull();
  });
});
