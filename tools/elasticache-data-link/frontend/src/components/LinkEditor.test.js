import { vi } from 'vitest';
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

vi.mock('axios');

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
  vi.clearAllMocks();
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

  it('sends no client baseline: the server compares with the deployed stacks', async () => {
    await render();
    await click(buttonByText('Compare'));
    expect(repairPosts()[0].body.current).toBeUndefined();
    expect(repairPosts()[0].body.desired).toEqual(DOC);
  });

  it('a change needing a backfill is applied only after it is acknowledged', async () => {
    axios.post.mockImplementation((url, body) => {
      posts.push({ url, body });
      if (url.includes('/api/links/parse')) {
        return Promise.resolve({ data: {
          parsed: true, document: DOC, valid: true, problems: [], normalized: DOC } });
      }
      if (url.includes('/api/links/repair') && body.apply) {
        return Promise.resolve({ status: 202, data: {
          ok: true, changed: true, applied: true, deployment_id: 'deploy-shop-1',
          needs_acknowledge: true,
          diff: { severity: 'cleanup', changes: [] }, plan: { steps: [], notes: [] } } });
      }
      if (url.includes('/api/links/repair')) {
        return Promise.resolve({ data: {
          ok: true, changed: true, needs_acknowledge: true,
          diff: { severity: 'cleanup', changes: [
            { severity: 'cleanup', field: 'advanced.key_prefix', parameter: 'KeyPrefix',
              from: 'b', to: '', consequence: 'keys move', action: 'snapshot, clean' }] },
          plan: { steps: [], blocked: false, notes: [] },
          outstanding: [{ kind: 'signal', why: 'snapshot', statement: 'INSERT ...' }] } });
      }
      return Promise.resolve({ data: {} });
    });
    await render();
    await click(buttonByText('Compare'));
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    // The parameter change is shown, and Apply waits for the acknowledgement.
    expect(container.textContent).toContain('KeyPrefix: "b" → ""');
    expect(buttonByText('Apply the change').disabled).toBe(true);
    const box = container.querySelector('.lk-ack input');
    await click(box);
    expect(buttonByText('Apply the change').disabled).toBe(false);
    await click(buttonByText('Apply the change'));
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    const applied = repairPosts().find((p) => p.body.apply === true);
    expect(applied.body.acknowledge).toBe(true);
    // The update runs in the background; its record is followed.
    expect(container.querySelector('.lk-deploystatus').textContent)
      .toContain('deploy-shop-1');
    expect(buttonByText('Apply the change').disabled).toBe(true);
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

/* Jira story F188: Apply applies the document Compare showed. It used to send whatever the
 * editor held when it was clicked, so an edit after Compare went live without
 * ever being compared. Here parse echoes the text into the document, so the
 * document of each text is distinguishable in the request body. */
describe('Apply sends the compared document', () => {
  let compareGate;
  let parseGate;

  beforeEach(() => {
    compareGate = null;
    parseGate = null;
    axios.post.mockImplementation((url, body) => {
      posts.push({ url, body });
      if (url.includes('/api/links/parse')) {
        const reply = { data: { parsed: true, valid: true, problems: [],
                                document: { ...DOC, from_text: body.text },
                                normalized: DOC } };
        if (parseGate) return parseGate.promise.then(() => reply);
        return Promise.resolve(reply);
      }
      if (url.includes('/api/links/repair')) {
        const reply = { data: {
          ok: true, changed: true,
          diff: { severity: 'backfill', changes: [
            { severity: 'backfill', field: `compared ${body.desired.from_text}`,
              consequence: 'shape changed', action: 'incremental snapshot' }] },
          plan: { steps: [], blocked: false, notes: [] } } };
        if (compareGate && !body.apply) return compareGate.promise.then(() => reply);
        return Promise.resolve(reply);
      }
      return Promise.resolve({ data: {} });
    });
  });

  function gate() {
    let release;
    const promise = new Promise((r) => { release = r; });
    return { promise, release };
  }

  async function edit(value) {
    const editor = container.querySelector('.lk-editor');
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(editor, value);
      editor.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  const wait = (ms) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });
  const applyButton = () => buttonByText('Apply the change');

  it('applies exactly the document that was compared', async () => {
    await render();
    await click(buttonByText('Compare'));
    await wait(20);
    await click(applyButton());
    const [compare, applied] = repairPosts();
    expect(applied.body.apply).toBe(true);
    expect(applied.body.desired).toEqual(compare.body.desired);
    expect(applied.body.desired.from_text).toBe(TEXT);
  });

  it('an edit after Compare clears the comparison and turns Apply off', async () => {
    await render();
    await click(buttonByText('Compare'));
    await wait(20);
    expect(container.querySelector('.lk-repair-result')).toBeTruthy();
    expect(applyButton().disabled).toBe(false);
    await edit(`${TEXT}# edited\n`);
    // At once, before the edit is even parsed.
    expect(container.querySelector('.lk-repair-result')).toBeNull();
    expect(applyButton().disabled).toBe(true);
    await wait(700);
    // Parsed and valid, but not compared: still off.
    expect(buttonByText('Compare').disabled).toBe(false);
    expect(applyButton().disabled).toBe(true);
    expect(repairPosts().filter((p) => p.body.apply)).toHaveLength(0);
  });

  it('after an edit, Compare again and Apply sends the new document', async () => {
    await render();
    await click(buttonByText('Compare'));
    await wait(20);
    const edited = `${TEXT}# edited\n`;
    await edit(edited);
    await wait(700);
    await click(buttonByText('Compare'));
    await wait(20);
    expect(container.textContent).toContain(`compared ${edited}`);
    await click(applyButton());
    const applied = repairPosts().find((p) => p.body.apply);
    expect(applied.body.desired.from_text).toBe(edited);
  });

  it('the acknowledgement does not carry over to an edited document', async () => {
    axios.post.mockImplementation((url, body) => {
      posts.push({ url, body });
      if (url.includes('/api/links/parse')) {
        return Promise.resolve({ data: { parsed: true, valid: true, problems: [],
          document: { ...DOC, from_text: body.text }, normalized: DOC } });
      }
      if (url.includes('/api/links/repair')) {
        return Promise.resolve({ data: { ok: true, changed: true, needs_acknowledge: true,
          diff: { severity: 'cleanup', changes: [] },
          plan: { steps: [], blocked: false, notes: [] } } });
      }
      return Promise.resolve({ data: {} });
    });
    await render();
    await click(buttonByText('Compare'));
    await wait(20);
    await click(container.querySelector('.lk-ack input'));
    expect(applyButton().disabled).toBe(false);
    await edit(`${TEXT}# edited\n`);
    await wait(700);
    await click(buttonByText('Compare'));
    await wait(20);
    expect(container.querySelector('.lk-ack input').checked).toBe(false);
    expect(applyButton().disabled).toBe(true);
  });

  it('a Compare answer that arrives after an edit is dropped', async () => {
    await render();
    compareGate = gate();
    await click(buttonByText('Compare'));
    await edit(`${TEXT}# edited while comparing\n`);
    await act(async () => { compareGate.release(); });
    await wait(20);
    // The answer was for the old text, so nothing is shown and Apply stays off.
    expect(container.querySelector('.lk-repair-result')).toBeNull();
    expect(applyButton().disabled).toBe(true);
  });

  it('a Compare failure that arrives after an edit is not reported', async () => {
    await render();
    let reject;
    const base = axios.post.getMockImplementation();
    axios.post.mockImplementation((url, body) => {
      if (url.includes('/api/links/repair')) {
        posts.push({ url, body });
        return new Promise((_ok, no) => { reject = no; });
      }
      return base(url, body);
    });
    await click(buttonByText('Compare'));
    await edit(`${TEXT}# edited while comparing\n`);
    await act(async () => { reject(new Error('compare of the old text failed')); });
    await wait(20);
    expect(container.querySelector('.lk-apierror')).toBeNull();
    // And the page is not left busy.
    await wait(700);
    expect(buttonByText('Compare').disabled).toBe(false);
  });

  it('Compare waits for the parse of the text on screen', async () => {
    await render();
    expect(buttonByText('Compare').disabled).toBe(false);
    parseGate = gate();
    await edit(`${TEXT}# pending\n`);
    await wait(700);
    // The verdict on screen is for the old text; the new one is still parsing.
    expect(buttonByText('Compare').disabled).toBe(true);
    await act(async () => { parseGate.release(); });
    await wait(20);
    expect(buttonByText('Compare').disabled).toBe(false);
  });

  it('a late parse answer cannot replace a newer one', async () => {
    const pending = [];
    axios.post.mockImplementation((url, body) => {
      posts.push({ url, body });
      if (url.includes('/api/links/parse')) {
        return new Promise((resolve) => { pending.push({ body, resolve }); });
      }
      return Promise.resolve({ data: { ok: true, changed: true,
        diff: { severity: 'none', changes: [] }, plan: { steps: [], notes: [] } } });
    });
    await act(async () => {
      root.render(<LinkEditor link="shop" onClose={() => {}} />);
    });
    await wait(700);
    await edit('name: older\n');
    await wait(700);
    await edit('name: newer\n');
    await wait(700);
    const older = pending.find((p) => p.body.text === 'name: older\n');
    const newer = pending.find((p) => p.body.text === 'name: newer\n');
    expect(older && newer).toBeTruthy();
    const reply = (t, valid) => ({ data: { parsed: true, valid, problems: [],
      document: { ...DOC, from_text: t }, normalized: DOC } });
    await act(async () => { newer.resolve(reply(newer.body.text, true)); });
    await act(async () => { older.resolve(reply(older.body.text, false)); });
    await wait(20);
    expect(container.querySelector('.lk-status').textContent).toContain('valid');
    await click(buttonByText('Compare'));
    expect(repairPosts()[0].body.desired.from_text).toBe('name: newer\n');
  });
});
