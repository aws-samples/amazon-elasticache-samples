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
import Links from './Links';

jest.mock('axios');
jest.mock('../authClient', () => ({
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
  jest.clearAllMocks();
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
  // The page debounces its parse; let it settle so `canAct` becomes true.
  await act(async () => { await new Promise((r) => setTimeout(r, 700)); });
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
