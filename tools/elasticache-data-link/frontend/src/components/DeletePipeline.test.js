import { vi } from 'vitest';
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import axios from 'axios';
import DeletePipeline, { FollowUps } from './DeletePipeline';

vi.mock('axios');

const PLAN = {
  plan: {
    link: 'shop', cluster_name: 'ecdl-shop',
    follow_up: { signal_table: 'ecdl_signal_shop',
                 pg_drop_slot_sql: "SELECT pg_drop_replication_slot('ecdl_shop');",
                 broker_replication_group: 'ecdl-shop-broker',
                 broker_snapshot_find_command:
                   'aws elasticache describe-snapshots --replication-group-id ecdl-shop-broker' },
  },
  stacks: [
    { id: 'cdc', name: 'ecdl-shop-cdc', status: 'CREATE_COMPLETE' },
    { id: 'loader', name: 'ecdl-shop-loader', status: 'CREATE_COMPLETE' },
    { id: 'broker', name: 'ecdl-shop-broker', status: 'CREATE_COMPLETE' },
  ],
  exists: true,
};

let container;
let root;
beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  axios.get.mockResolvedValue({ data: PLAN });
  axios.post.mockResolvedValue({ data: { destroy_id: 'destroy-shop-1' } });
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.resetAllMocks();
});

async function mount(props = {}) {
  const onStarted = vi.fn();
  await act(async () => {
    root.render(<DeletePipeline link="shop" onStarted={onStarted}
                                onCancel={() => {}} {...props} />);
  });
  return onStarted;
}

function deleteButton() {
  return Array.from(container.querySelectorAll('button'))
    .find((b) => b.textContent === 'Delete pipeline');
}

async function type(value) {
  const input = container.querySelector('#lk-delete-name');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

it('shows the stacks it would delete and what it leaves for you', async () => {
  await mount();
  const text = container.textContent;
  expect(text).toContain('ecdl-shop-cdc');
  expect(text).toContain('ecdl-shop-broker');
  expect(text).toContain('ecdl_signal_shop');
  expect(text).toContain('source database and your target cache');
});

it('stays disabled until the exact name is typed', async () => {
  await mount();
  expect(deleteButton().disabled).toBe(true);
  await type('sho');
  expect(deleteButton().disabled).toBe(true);
  await type('shop');
  expect(deleteButton().disabled).toBe(false);
});

it('sends the typed name as confirmation and hands back the run id', async () => {
  const onStarted = await mount();
  await type('shop');
  await act(async () => { deleteButton().click(); });
  expect(axios.post).toHaveBeenCalledWith(
    expect.stringContaining('/api/links/destroy'),
    { link: 'shop', confirm: 'shop' }, expect.anything());
  expect(onStarted).toHaveBeenCalledWith('destroy-shop-1');
});

it('says so when there is nothing to delete', async () => {
  axios.get.mockResolvedValue({ data: { ...PLAN, exists: false } });
  await mount();
  expect(container.textContent).toContain('Nothing to delete');
  expect(deleteButton()).toBeUndefined();
});

it('says the broker leaves a final snapshot before the delete', async () => {  // Jira story F022
  await mount();
  const text = container.textContent;
  expect(text).toContain('final snapshot');
  expect(text).toContain('restore_snapshot');
  expect(text).toContain('--replication-group-id ecdl-shop-broker');
});

it('names the final snapshot and how to delete it afterwards', async () => {
  const followUp = { ...PLAN.plan.follow_up, broker_final_snapshot: 'ecdl-shop-broker-final',
                     broker_snapshot_delete_command:
                       'aws elasticache delete-snapshot --snapshot-name ecdl-shop-broker-final' };
  await act(async () => { root.render(<FollowUps followUp={followUp} link="shop" />); });
  const text = container.textContent;
  expect(text).toContain('Snapshot: ecdl-shop-broker-final');
  expect(text).toContain('delete-snapshot --snapshot-name ecdl-shop-broker-final');
  expect(text).not.toContain('describe-snapshots');
});

// ── Jira story F136: a broker with unapplied changes or dead letters ───────────────────
// The API refuses (409, stage "backlog") to delete a broker that holds changes
// not yet in the cache, or dead letters, unless the request carries
// "delete_backlog": true. The dialog has to say what is at risk and let the
// operator opt in, and must never send the flag on its own.
const AT_RISK = {
  exists: true, readable: true, at_risk: true,
  backlog: { 'data:shop.Inventory.orders': 120 },
  dead_letters: { 'dlq:data:shop.Inventory.orders': 3 },
  unapplied: 120, dead_letters_total: 3,
};
function backlogRefusal(broker = AT_RISK) {
  const e = new Error('Request failed with status code 409');
  e.response = { status: 409, data: {
    stage: 'backlog', broker,
    error: "pipeline 'shop' was not deleted: the broker holds 120 change(s) not yet "
      + 'written to the cache and 3 dead letter(s).',
    hint: 'Let the sink catch up, or replay the dead letters, then delete again.',
  } };
  return e;
}
function backlogBox() {
  return container.querySelector('.lk-delete-backlog input[type="checkbox"]');
}
async function tick() {
  await act(async () => { backlogBox().click(); });
}
async function pressDelete() {
  await act(async () => { deleteButton().click(); });
}

describe('a broker holding a backlog (Jira story F136)', () => {
  it('a clean broker offers no checkbox and sends no flag', async () => {
    axios.get.mockResolvedValue({ data: { ...PLAN,
      broker: { exists: true, readable: true, at_risk: false, unapplied: 0,
                dead_letters_total: 0, backlog: {}, dead_letters: {} },
      broker_summary: 'the broker holds no unapplied changes and no dead letters' } });
    await mount();
    expect(backlogBox()).toBeNull();
    expect(container.querySelector('.lk-broker-summary').textContent)
      .toContain('no unapplied changes');
    await type('shop');
    await pressDelete();
    expect(axios.post.mock.calls[0][1]).toEqual({ link: 'shop', confirm: 'shop' });
  });

  it('a refusal shows what is at risk and offers an unticked checkbox', async () => {
    axios.post.mockRejectedValueOnce(backlogRefusal());
    const onStarted = await mount();
    await type('shop');
    await pressDelete();
    expect(onStarted).not.toHaveBeenCalled();
    const text = container.textContent;
    expect(text).toContain('120 change(s) not yet written to the cache');
    expect(text).toContain('Let the sink catch up');
    // per stream, so the operator can see which table is behind
    expect(text).toContain('data:shop.Inventory.orders');
    expect(text).toContain('dlq:data:shop.Inventory.orders');
    expect(container.querySelector('.lk-delete-backlog').textContent)
      .toContain('Delete the backlog and dead letters too');
    expect(backlogBox().checked).toBe(false);
    // The button stays usable: the backlog may have drained by the next try.
    expect(deleteButton().disabled).toBe(false);
  });

  it('resending without ticking the box does not send delete_backlog', async () => {
    axios.post.mockRejectedValueOnce(backlogRefusal())
      .mockRejectedValueOnce(backlogRefusal());
    await mount();
    await type('shop');
    await pressDelete();
    await pressDelete();
    expect(axios.post).toHaveBeenCalledTimes(2);
    axios.post.mock.calls.forEach(([, body]) => {
      expect(body).toEqual({ link: 'shop', confirm: 'shop' });
    });
  });

  it('ticking the box resends with delete_backlog: true', async () => {
    axios.post.mockRejectedValueOnce(backlogRefusal())
      .mockResolvedValueOnce({ data: { destroy_id: 'destroy-shop-2' } });
    const onStarted = await mount();
    await type('shop');
    await pressDelete();
    await tick();
    expect(backlogBox().checked).toBe(true);
    await pressDelete();
    expect(axios.post.mock.calls[1][1])
      .toEqual({ link: 'shop', confirm: 'shop', delete_backlog: true });
    expect(onStarted).toHaveBeenCalledWith('destroy-shop-2');
  });

  it('unticking it again sends no flag', async () => {
    axios.post.mockRejectedValueOnce(backlogRefusal());
    await mount();
    await type('shop');
    await pressDelete();
    await tick();
    await tick();
    await pressDelete();
    expect(axios.post.mock.calls[1][1]).toEqual({ link: 'shop', confirm: 'shop' });
  });

  it('a plan that already shows a backlog says so before the first try', async () => {
    axios.get.mockResolvedValue({ data: { ...PLAN, broker: AT_RISK,
      broker_summary: 'the broker holds 120 change(s) not yet written to the cache' } });
    await mount();
    expect(container.querySelector('.lk-atrisk').textContent)
      .toContain('120 change(s) not yet written to the cache and 3 dead letter(s)');
    expect(backlogBox().checked).toBe(false);
    await type('shop');
    await pressDelete();
    expect(axios.post.mock.calls[0][1]).toEqual({ link: 'shop', confirm: 'shop' });
  });

  it('an unreadable broker is reported but does not offer the checkbox', async () => {
    // The API does not block on a broker it cannot read, so there is nothing
    // to opt in to.
    const summary = 'could not read the broker (timeout), so whether it holds changes '
      + 'not yet written to the cache, or dead letters, is unknown.';
    axios.get.mockResolvedValue({ data: { ...PLAN,
      broker: { exists: true, readable: false, reason: 'timeout', at_risk: false },
      broker_summary: summary } });
    await mount();
    expect(container.querySelector('.lk-broker-summary').textContent).toContain(summary);
    expect(backlogBox()).toBeNull();
  });

  it('any other refusal is an error, with no checkbox', async () => {
    const busy = new Error('Request failed with status code 409');
    busy.response = { status: 409, data: {
      error: "pipeline 'shop' is being changed right now (ecdl-shop-cdc UPDATE_IN_PROGRESS)" } };
    axios.post.mockRejectedValueOnce(busy);
    await mount();
    await type('shop');
    await pressDelete();
    expect(container.textContent).toContain('is being changed right now');
    expect(backlogBox()).toBeNull();
  });

  it('a later different refusal keeps the box and its answer visible', async () => {
    // Hiding the box while keeping its ticked state would send the flag on
    // the next press without the operator seeing it.
    const busy = new Error('Request failed with status code 409');
    busy.response = { status: 409, data: { error: 'being changed right now' } };
    axios.post.mockRejectedValueOnce(backlogRefusal()).mockRejectedValueOnce(busy);
    await mount();
    await type('shop');
    await pressDelete();
    await tick();
    await pressDelete();
    expect(container.textContent).toContain('being changed right now');
    expect(backlogBox()).not.toBeNull();
    expect(backlogBox().checked).toBe(true);
  });
});
