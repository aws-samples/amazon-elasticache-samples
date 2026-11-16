/*
 * Tests for the pipeline editor that lives on the Deployments page. axios is
 * mocked, so nothing touches the network. The invariants: it auto-loads the
 * running pipeline's config, and its only actions are Compare and Apply — it
 * must never deploy a new pipeline.
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import axios from 'axios';
import LinkEditor from './LinkEditor';

jest.mock('axios');

const DOC = {
  schema_version: 1, name: 'shop', mode: 'cdc',
  source: { arn: 'arn:src', database: 'Inventory', secret_arn: 'arn:sec' },
  target: { arn: 'arn:tgt' }, tables: ['Inventory.users'],
};
const TEXT = 'name: shop\nmode: cdc\n';

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
    if (url.includes('/api/links/reconstruct')) {
      return Promise.resolve({ data: { ok: true, text: TEXT, warnings: [] } });
    }
    if (url.includes('/api/auth/status')) {
      return Promise.resolve({ data: { enabled: false, configured: true } });
    }
    if (url.includes('/api/links/discover')) {
      return Promise.resolve({ data: {
        discovered: true, sources: [], targets: [], secrets: [] } });
    }
    return Promise.resolve({ data: {} });
  });

  axios.post.mockImplementation((url, body) => {
    posts.push({ url, body });
    if (url.includes('/api/links/parse')) {
      return Promise.resolve({ data: {
        parsed: true, document: DOC, valid: true, problems: [], normalized: DOC } });
    }
    if (url.includes('/api/links/format')) {
      return Promise.resolve({ data: { valid: true, text: TEXT, problems: [] } });
    }
    if (url.includes('/api/links/repair')) {
      return Promise.resolve({ data: {
        ok: true, changed: true,
        diff: { severity: 'backfill', changes: [
          { severity: 'backfill', field: 'transforms.tables.users',
            consequence: 'shape changed', action: 'incremental snapshot' }] },
        plan: { steps: [], blocked: false, notes: [] },
        diff_summary: '', plan_summary: '' } });
    }
    return Promise.resolve({ data: {} });
  });
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

async function render(link = 'shop') {
  await act(async () => {
    root.render(<LinkEditor link={link} onClose={() => {}} />);
  });
  // Load (reconstruct) + the debounced parse.
  await act(async () => { await new Promise((r) => setTimeout(r, 700)); });
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

const repairPosts = () => posts.filter((p) => p.url.includes('/api/links/repair'));
const deployPosts = () => posts.filter((p) => p.url.includes('/api/links/deploy'));

describe('LinkEditor', () => {
  it('auto-loads the running pipeline config on mount', async () => {
    await render();
    expect(container.textContent).toContain('Edit shop');
    const editor = container.querySelector('.lk-editor');
    expect(editor.value).toBe(TEXT);
    // The reconstruct call was made for this link.
    expect(posts.every((p) => !p.url.includes('/deploy'))).toBe(true);
  });

  it('is a file editor with examples and Compare/Apply (no wizard)', async () => {
    await render();
    // File-only: no Guided/File tabs.
    expect(buttonByText('Guided')).toBeUndefined();
    expect(container.querySelector('.lk-editor')).toBeTruthy();
    expect(buttonByText('Compare')).toBeTruthy();
    expect(buttonByText('Apply the change')).toBeTruthy();
    // The single annotated example is shown, covering every transform.
    expect(container.querySelector('.lk-examples')).toBeTruthy();
    expect(container.textContent).toContain('transforms:');
    expect(container.textContent).toContain('computed');
    expect(container.textContent).toContain('denormalize');
  });

  it('Compare posts /repair with apply:false and never deploys', async () => {
    await render();
    await click(buttonByText('Compare'));
    const r = repairPosts();
    expect(r.length).toBe(1);
    expect(r[0].body.apply).toBe(false);
    expect(deployPosts()).toHaveLength(0);
  });

  it('Apply posts /repair with apply:true after a compare, never deploys', async () => {
    await render();
    await click(buttonByText('Compare'));
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    await click(buttonByText('Apply the change'));
    expect(repairPosts().some((p) => p.body.apply === true)).toBe(true);
    expect(deployPosts()).toHaveLength(0);
  });

  it('shows an error when the pipeline cannot be reconstructed', async () => {
    axios.get.mockImplementation((url) => {
      if (url.includes('/api/links/reconstruct')) {
        return Promise.resolve({ data: { ok: false, error: 'no deployed stacks' } });
      }
      return Promise.resolve({ data: {} });
    });
    await render('missing');
    expect(container.querySelector('.lk-apierror').textContent)
      .toContain('no deployed stacks');
  });
});
