import { vi } from 'vitest';
/*
 * Tests for the Randomizer's start and stop status. The page is hidden from the
 * UI for now (App.wiring.test.js), but it may come back, and its Stop button
 * used to say "stopped" on any response at all -- an error page included --
 * and stop polling, while the worker could still be inserting rows (Jira story F190).
 * react-dom + act with axios mocked, like the other component tests.
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import axios from 'axios';
import Randomizer from './Randomizer';

vi.mock('axios');

const SOURCE = { id: 7, name: 'shop', engine: 'mysql', host: 'db', port: 3306,
                 database_name: 'Inventory' };

let container;
let root;
let posts;
let statusReplies;     // what each status read returns, in order; the last repeats
let stopReply;         // a function returning the stop promise

function running(n) {
  return { status: 'running', records_written: n, errors: 0 };
}

beforeEach(() => {
  vi.clearAllMocks();
  posts = [];
  statusReplies = [running(5)];
  stopReply = () => Promise.resolve({ data: { success: true, status: running(5) } });
  container = window.document.createElement('div');
  window.document.body.appendChild(container);
  root = createRoot(container);

  axios.get.mockImplementation((url, config) => {
    if (url.endsWith('/api/sources')) return Promise.resolve({ data: [SOURCE] });
    if (url.endsWith('/api/sources/7/tables')) {
      return Promise.resolve({ data: { tables: ['orders'] } });
    }
    if (url.endsWith('/api/sources/7/columns')) {
      expect(config.params).toEqual({ table: 'orders' });
      return Promise.resolve({ data: { columns: [
        { name: 'id', type: 'int' }, { name: 'status', type: 'varchar', max_length: 10 }] } });
    }
    if (url.endsWith('/api/randomizer/status')) {
      const next = statusReplies.length > 1 ? statusReplies.shift() : statusReplies[0];
      return typeof next === 'function' ? next() : Promise.resolve({ data: next });
    }
    return Promise.resolve({ data: {} });
  });
  axios.post.mockImplementation((url, body) => {
    posts.push({ url, body });
    if (url.endsWith('/api/randomizer/start')) {
      return Promise.resolve({ data: { success: true, status: running(0) } });
    }
    if (url.endsWith('/api/randomizer/stop')) return stopReply();
    return Promise.resolve({ data: {} });
  });
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

const wait = (ms) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });

function buttonByText(text) {
  return Array.from(container.querySelectorAll('button'))
    .find((b) => b.textContent.includes(text));
}

async function click(el) {
  await act(async () => {
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
}

async function startOne() {
  await act(async () => { root.render(<Randomizer />); });
  await click(buttonByText('Analyze'));
  await click(buttonByText('Select'));
  await click(buttonByText('Start Generator'));
}

const badge = () => container.querySelector('.status-badge.status-running, '
  + '.status-badge.status-stopped, .status-badge.status-error');
const isRunningView = () => Boolean(buttonByText('Stop Generator')
                                    || buttonByText('Stopping'));

describe('Randomizer stop', () => {
  it('starts through axios and shows it running', async () => {
    await startOne();
    const start = posts.find((p) => p.url.endsWith('/api/randomizer/start'));
    expect(start.body.source_id).toBe(7);
    expect(start.body.columns.map((c) => c.name)).toEqual(['id', 'status']);
    expect(isRunningView()).toBe(true);
  });

  it('an accepted stop shows "stopping" until the status says stopped', async () => {
    await startOne();
    // The worker is still finishing its batch when the stop is accepted.
    statusReplies = [running(9), running(10), { status: 'stopped', records_written: 11, errors: 0 }];
    await click(buttonByText('Stop Generator'));
    expect(buttonByText('Stopping').disabled).toBe(true);
    expect(buttonByText('Start Generator')).toBeUndefined();
    expect(badge().textContent).toBe('running');
    // The poll keeps going, and only the stopped status ends it.
    await wait(1100);
    expect(buttonByText('Stopping')).toBeTruthy();
    await wait(1100);
    expect(buttonByText('Start Generator')).toBeTruthy();
    expect(badge().textContent).toBe('stopped');
    expect(container.textContent).toContain('11');
  });

  it('a stop the status already confirms ends at once', async () => {
    await startOne();
    statusReplies = [{ status: 'stopped', records_written: 3, errors: 0 }];
    await click(buttonByText('Stop Generator'));
    expect(buttonByText('Start Generator')).toBeTruthy();
    expect(badge().textContent).toBe('stopped');
  });

  it('a refused stop keeps it shown as running, with the reason', async () => {
    await startOne();
    stopReply = () => {
      const err = new Error('Request failed with status code 500');
      err.response = { status: 500, data: { error: 'database is locked' } };
      return Promise.reject(err);
    };
    await click(buttonByText('Stop Generator'));
    expect(container.textContent).toContain('may still be inserting rows');
    expect(container.textContent).toContain('database is locked');
    const stop = buttonByText('Stop Generator');
    expect(stop).toBeTruthy();
    expect(stop.disabled).toBe(false);
    // Polling was not stopped either: the status still updates.
    statusReplies = [running(42)];
    await wait(1100);
    expect(container.textContent).toContain('42');
  });

  it('a stop that fails with no server reason still says so', async () => {
    await startOne();
    stopReply = () => Promise.reject(new Error('Network Error'));
    await click(buttonByText('Stop Generator'));
    expect(container.textContent).toContain('Network Error');
    expect(isRunningView()).toBe(true);
  });

  it('a failed status read during a stop is not read as stopped', async () => {
    await startOne();
    statusReplies = [() => Promise.reject(new Error('timeout')),
                     { status: 'stopped', records_written: 1, errors: 0 }];
    await click(buttonByText('Stop Generator'));
    expect(buttonByText('Stopping')).toBeTruthy();
    await wait(1100);
    expect(buttonByText('Start Generator')).toBeTruthy();
  });

  it('a refused start shows the server reason and does not run', async () => {
    axios.post.mockImplementation((url) => {
      if (url.endsWith('/api/randomizer/start')) {
        const err = new Error('Request failed with status code 400');
        err.response = { status: 400, data: { error: 'unknown column: nope' } };
        return Promise.reject(err);
      }
      return Promise.resolve({ data: {} });
    });
    await startOne();
    expect(container.textContent).toContain('unknown column: nope');
    expect(isRunningView()).toBe(false);
  });
});
