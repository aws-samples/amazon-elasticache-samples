/*
 * Tests for the shared link views. The one behaviour to test here is the
 * auto-clean banner in RepairDetail: it must appear only when the diff actually
 * renames or removes a field (the case the sink now cleans on re-snapshot), and
 * must stay hidden otherwise so the reassurance is never misplaced.
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { RepairDetail } from './linkViews';

let container;
let root;

beforeEach(() => {
  container = window.document.createElement('div');
  window.document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render(repair) {
  act(() => root.render(<RepairDetail data={repair} />));
}

const CLEAN = /cleaned automatically/i;

function repairWith(changes) {
  return {
    ok: true,
    changed: true,
    diff: { severity: 'backfill', changes },
    plan: { steps: [], notes: [] },
  };
}

it('shows the auto-clean banner when a field is renamed', () => {
  render(repairWith([
    { severity: 'backfill', field: 'transforms', table: 'Inventory.users',
      consequence: 'a field was renamed', action: 're-snapshot the table' },
  ]));
  expect(container.textContent).toMatch(CLEAN);
});

it('shows the auto-clean banner when the selected columns change', () => {
  render(repairWith([
    { severity: 'backfill', field: 'transforms', table: 'Inventory.users',
      consequence: 'the selected columns changed', action: 're-snapshot the table' },
  ]));
  expect(container.textContent).toMatch(CLEAN);
});

it('hides the banner when no field is renamed or removed', () => {
  render(repairWith([
    { severity: 'deploy', field: 'target', table: null,
      consequence: 'the target endpoint changed', action: 'redeploy the sink' },
  ]));
  expect(container.textContent).not.toMatch(CLEAN);
});

it('hides the banner when there are no differences', () => {
  render({ ok: true, changed: false });
  expect(container.textContent).not.toMatch(CLEAN);
});
