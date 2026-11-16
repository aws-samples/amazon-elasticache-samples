import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import axios from 'axios';
import DeletePipeline from './DeletePipeline';

jest.mock('axios');

const PLAN = {
  plan: {
    link: 'shop', cluster_name: 'ecdl-shop',
    follow_up: { signal_table: 'ecdl_signal_shop',
                 pg_drop_slot_sql: "SELECT pg_drop_replication_slot('ecdl_shop');" },
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
  jest.resetAllMocks();
});

async function mount(props = {}) {
  const onStarted = jest.fn();
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
