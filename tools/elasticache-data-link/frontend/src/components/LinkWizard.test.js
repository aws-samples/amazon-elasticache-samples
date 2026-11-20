import { vi } from 'vitest';
/*
 * Render tests for the guided link builder. react-dom directly, axios mocked, so
 * nothing touches the network.
 *
 * The invariant most of these check: the wizard holds no configuration of its
 * own. Every choice is written into the link DOCUMENT and handed back, which is
 * what lets the form and the file stay one thing. A test that only checked "the
 * source list renders" would pass just as well against a wizard that kept its own
 * state and drifted from the file, so these assert on what onChange receives.
 *
 * Run: npm test -- --watchAll=false
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import axios from 'axios';
import LinkWizard, { withAlarmEmail } from './LinkWizard';

vi.mock('axios');

const SRC = 'arn:aws:rds:us-east-2:111122223333:cluster:shop';
const MEMBER = 'arn:aws:rds:us-east-2:111122223333:db:shop-writer';
const DOCDB = 'arn:aws:rds:us-east-2:111122223333:cluster:docs';
const TGT = 'arn:aws:elasticache:us-east-2:111122223333:replicationgroup:cache';
const AUTHTGT = 'arn:aws:elasticache:us-east-2:111122223333:replicationgroup:locked';
const SEC = 'arn:aws:secretsmanager:us-east-2:111122223333:secret:shop-password';

const INVENTORY = {
  discovered: true,
  sources: [
    {
      arn: SRC, identifier: 'shop', kind: 'cluster', engine: 'mysql',
      engine_label: 'MySQL', engine_version: '8.0', endpoint: 'shop.rds:3306',
      username: 'admin', database: null, status: 'available', usable: true,
      blockers: [], master_user_secret_arn: null,
    },
    {
      arn: MEMBER, identifier: 'shop-writer', kind: 'instance', engine: 'mysql',
      engine_label: 'MySQL', endpoint: 'shop-writer.rds:3306', status: 'available',
      usable: true, blockers: [], aurora_member: true,
      cluster_identifier: 'shop',
      notes: ['this is a member of Aurora cluster shop. Point the link at the '
              + 'cluster instead, so capture follows a failover.'],
    },
    {
      arn: DOCDB, identifier: 'docs', kind: 'cluster', engine: null,
      engine_raw: 'docdb', status: 'available', usable: false,
      blockers: ["engine 'docdb' is not supported"],
    },
  ],
  targets: [
    {
      arn: TGT, identifier: 'cache', endpoint: 'clustercfg.cache:6379',
      cluster_mode: true, shards: 3, node_type: 'cache.m7g.large', tls: true,
      auth_mode: 'none', needs_secret: false, status: 'available',
      usable: true, blockers: [], notes: [],
    },
    {
      arn: AUTHTGT, identifier: 'locked', endpoint: 'locked:6379',
      cluster_mode: false, shards: 1, node_type: 'cache.t4g.small', tls: true,
      auth_mode: 'token', needs_secret: true, status: 'available',
      usable: true, blockers: [],
      notes: ['this cache requires authentication (token), so the link needs '
              + 'target.secret_arn'],
    },
  ],
  secrets: [
    { arn: SEC, name: 'shop-password', likely: true,
      match_reason: 'shares shop with the database name' },
    { arn: 'arn:aws:secretsmanager:us-east-2:111122223333:secret:other',
      name: 'other', likely: false },
  ],
  counts: { sources: 3, targets: 2, secrets: 2 },
  truncated: {},
  problems: [],
};

const TABLES = {
  listed: true,
  count: 2,
  namespace: 'Inventory',
  namespace_kind: 'database',
  engine_label: 'MySQL',
  prerequisites: [
    { name: 'binlog_format is ROW', ok: true, detail: 'binlog_format=ROW' },
  ],
  tables: [
    {
      table: 'orders', row_estimate: 1002, primary_key: ['id'],
      capturable: true,
      columns: [{ name: 'id', type: 'bigint', nullable: false },
                { name: 'status', type: 'varchar', nullable: true },
                { name: 'amount', type: 'decimal', nullable: true }],
    },
    {
      table: 'audit', row_estimate: 10, primary_key: [], capturable: false,
      warning: 'no primary key, so there is no key to cache each row under',
      columns: [{ name: 'note', type: 'text', nullable: true }],
    },
  ],
};

let container;
let root;

function mockDiscovery(inventory = INVENTORY, tables = TABLES) {
  axios.get.mockImplementation((url) => {
    if (url.includes('/api/links/discover')) {
      return Promise.resolve({ data: inventory });
    }
    return Promise.resolve({ data: {} });
  });
  axios.post.mockImplementation((url) => {
    if (url.includes('/discover/tables')) return Promise.resolve({ data: tables });
    return Promise.resolve({ data: {} });
  });
}

// A CONTROLLED harness, holding the document and feeding it back exactly as
// Links.js does. The first version of this only recorded what onChange received
// and never re-rendered with it, so the wizard was always working from the
// document it started with. Every assertion still passed, because most of them
// only look at the newest change -- but the component under test was never
// rendered with the state it had just requested, which is not the component that
// runs in the browser. It hid a real defect until one test happened to depend on
// two changes in sequence.
function Harness({ initial, report, tokenMissing, onDone }) {
  const [doc, setDoc] = React.useState(initial || {});
  React.useEffect(() => { report(doc); }, [doc, report]);
  return (
    <LinkWizard document={doc} onChange={setDoc}
                onDone={onDone || (() => {})} tokenMissing={tokenMissing} />
  );
}

async function render(props) {
  const seen = [];
  let latest = props.document || {};
  const report = (doc) => { latest = doc; seen.push(doc); };
  await act(async () => {
    root.render(<Harness initial={props.document || {}} report={report}
                         tokenMissing={props.tokenMissing}
                         onDone={props.onDone} />);
  });
  return {
    // The document as it stands now -- what the parent would hold.
    doc: () => latest,
    seen,
  };
}

function findByText(selector, text) {
  return Array.from(container.querySelectorAll(selector))
    .find((el) => (el.textContent || '').includes(text));
}

async function click(el) {
  await act(async () => {
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
}


// Choosing a resource is now a dropdown, not a card. `pick` finds the select whose
// options include the given ARN and selects it, going through React's own value
// setter so the change is visible to it.
async function pick(arn) {
  const select = Array.from(container.querySelectorAll('select'))
    .find((s) => Array.from(s.options).some((o) => o.value === arn));
  if (!select) throw new Error('no dropdown offers ' + arn);
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLSelectElement.prototype, 'value').set;
    setter.call(select, arn);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

// The option text for an ARN, which is where an unusable entry states its reason.
function optionFor(arn) {
  for (const select of container.querySelectorAll('select')) {
    for (const o of select.options) {
      if (o.value === arn) return o;
    }
  }
  return null;
}

async function next() {
  const button = Array.from(container.querySelectorAll('.wz-nav button'))
    .find((b) => b.textContent === 'Next' || b.textContent === 'Review');
  await click(button);
}

beforeEach(() => {
  vi.clearAllMocks();
  container = window.document.createElement('div');
  window.document.body.appendChild(container);
  root = createRoot(container);
  mockDiscovery();
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe('the source step', () => {
  it('lists the databases in the account', async () => {
    await render({});
    expect(container.textContent).toContain('shop');
    expect(container.textContent).toContain('MySQL');
  });

  it('writes the chosen source into the document, not into its own state',
     async () => {
       const { doc } = await render({});
       await pick(SRC);
       const last = doc();
       expect(last.source.arn).toBe(SRC);
     });

  it('keeps an unsupported engine visible and says why it cannot be used',
     async () => {
       // Present but unselectable, with the reason in the label. Hiding it would
       // turn "docdb is not supported" into "where is my database?".
       await render({});
       const option = optionFor(DOCDB);
       expect(option).toBeTruthy();
       expect(option.disabled).toBe(true);
       expect(option.textContent).toContain("engine 'docdb' is not supported");
     });

  it('warns that an Aurora member instance is the wrong thing to point at',
     async () => {
       // Selectable, because it does deploy -- it is a correctness risk, not an
       // error -- so the warning appears against the chosen item.
       await render({});
       await pick(MEMBER);
       expect(container.querySelector('.wz-chosen').textContent)
         .toContain('Point the link at the cluster instead');
     });

  it('pre-fills the credential when RDS manages the password', async () => {
    const managed = JSON.parse(JSON.stringify(INVENTORY));
    managed.sources[0].master_user_secret_arn = SEC;
    mockDiscovery(managed);
    const { doc } = await render({});
    await pick(SRC);
    expect(doc().source.secret_arn).toBe(SEC);
  });

  it('asks for a database name because Aurora reports none', async () => {
    await render({ document: { name: 'shop', source: { arn: SRC } } });
    expect(container.querySelector('.wz-blocked').textContent)
      .toContain('database name');
  });

  it('writes the default mode into the document instead of only showing it', async () => {
    // The bug this covers: the mode list rendered "Load then keep in sync" as
    // selected while `mode` was absent from the document, so accepting the
    // default -- which is what a default is for -- produced
    // "mode is required" and a Next button that would not move.
    const { doc } = await render({});
    const filled = doc();
    expect(filled).toBeTruthy();
    expect(filled.mode).toBe('initial_load_and_cdc');
    expect(filled.schema_version).toBe(1);
  });

  it('does not name the pipeline after the cluster; it asks for a short name', async () => {
    // A cluster id like ecdl-demo-source-dbcluster-pnxyw... used to become the
    // pipeline name, and every stack and log group was named after it.
    const { doc } = await render({ document: { source: { database: 'Inventory' } } });
    await pick(SRC);
    expect(doc().name).toBeFalsy();
    expect(container.querySelector('.wz-blocked').textContent)
      .toContain('short name');
  });

  it('does not overwrite a name that was already typed', async () => {
    const { doc } = await render({ document: { name: 'my-own-name' } });
    await pick(SRC);
    expect(doc().name).toBe('my-own-name');
  });

  it('accepting every default leaves the database (then the name) to fill in', async () => {
    // The exact sequence that failed: choose a source, change nothing else.
    // Everything except the database (which Aurora genuinely does not report)
    // should already be answered.
    const { doc } = await render({});
    await pick(SRC);
    const built = doc();
    expect(built.mode).toBe('initial_load_and_cdc');
    expect(built.schema_version).toBe(1);
    expect(built.source.arn).toBe(SRC);
    expect(container.querySelector('.wz-blocked').textContent)
      .toContain('database name');
  });

  it('unblocks once the database is given', async () => {
    await render({
      document: {
        schema_version: 1, name: 'shop', mode: 'initial_load_and_cdc',
        source: { arn: SRC, database: 'Inventory' },
      },
    });
    expect(container.querySelector('.wz-blocked').textContent).toBe('');
    const nextButton = Array.from(container.querySelectorAll('.wz-nav button'))
      .find((b) => b.textContent === 'Next');
    expect(nextButton.disabled).toBe(false);
  });

  it('marks the field the Next button is waiting on', async () => {
    await render({ document: { name: 'shop', source: { arn: SRC } } });
    expect(container.querySelector('.wz-input.is-needed')).toBeTruthy();
  });

  it('the source list is above the fields that depend on it', async () => {
    // Ordering is the fix for the original complaint: the blocker used to name a
    // field that had scrolled off the top of the page.
    await render({ document: { name: 'shop', source: { arn: SRC } } });
    const html = container.innerHTML;
    expect(html.indexOf('wz-list')).toBeLessThan(html.indexOf('wz-after'));
  });

  it('refuses characters the schema would reject, as they are typed', async () => {
    const { doc } = await render({ document: { source: { arn: SRC } } });
    const nameInput = Array.from(container.querySelectorAll('.wz-input'))
      .find((i) => i.getAttribute('placeholder') === 'orders-cache');
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set;
      setter.call(nameInput, 'My Cache!');
      nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(doc().name).toBe('mycache');
  });
});

describe('a self-managed / on-prem source', () => {
  async function toExternal(doc) {
    const out = await render({ document: doc || {} });
    await click(findByText('.wz-choice', 'Self-managed / on-premises'));
    return out;
  }

  async function setByPlaceholder(placeholder, value) {
    const el = Array.from(container.querySelectorAll('input'))
      .find((i) => i.getAttribute('placeholder') === placeholder);
    if (!el) throw new Error('no input with placeholder ' + placeholder);
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set;
      setter.call(el, value);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  it('switching to it sets kind=external and drops any arn', async () => {
    const { doc } = await toExternal({ source: { arn: SRC } });
    expect(doc().source.kind).toBe('external');
    expect(doc().source.arn).toBeFalsy();
  });

  it('offers exactly the four supported engines', async () => {
    await toExternal();
    const engineSelect = Array.from(container.querySelectorAll('select'))
      .find((s) => Array.from(s.options).some((o) => o.value === 'postgresql'));
    expect(engineSelect).toBeTruthy();
    const values = Array.from(engineSelect.options)
      .map((o) => o.value).filter(Boolean);
    expect(values).toEqual(['mysql', 'postgresql', 'sqlserver', 'oracle']);
  });

  it('writes engine, host, user and database into the document', async () => {
    const { doc } = await toExternal();
    await pick('postgresql');
    await setByPlaceholder('db.corp.internal', 'db.corp.internal');
    await setByPlaceholder('repl', 'replicator');
    await setByPlaceholder('Inventory', 'Inventory');
    const s = doc().source;
    expect(s.engine).toBe('postgresql');
    expect(s.host).toBe('db.corp.internal');
    expect(s.username).toBe('replicator');
    expect(s.database).toBe('Inventory');
    // TLS defaults on, so it is left out of the document rather than written.
    expect(s.tls).toBeUndefined();
  });

  it('blocks until engine, host and user are given', async () => {
    await toExternal();
    expect(container.querySelector('.wz-blocked').textContent).toContain('engine');
    await pick('postgresql');
    expect(container.querySelector('.wz-blocked').textContent).toContain('host');
    await setByPlaceholder('db.corp.internal', 'db.corp.internal');
    expect(container.querySelector('.wz-blocked').textContent)
      .toContain('database user');
  });

  it('does not name the pipeline after the host', async () => {
    const { doc } = await toExternal();
    await pick('postgresql');
    await setByPlaceholder('db.corp.internal', 'orders-db.corp.internal');
    expect(doc().name).toBeFalsy();
  });

  it('records tls:false only when TLS is turned off', async () => {
    const { doc } = await toExternal();
    await pick('postgresql');
    const box = container.querySelector('.wz-check input[type=checkbox]');
    expect(box.checked).toBe(true);
    await click(box);
    expect(doc().source.tls).toBe(false);
  });
});

describe('the target step', () => {
  const readyDoc = {
    name: 'shop', mode: 'cdc',
    source: { arn: SRC, database: 'Inventory', secret_arn: SEC },
  };

  it('requires a credential for a cache that demands authentication', async () => {
    await render({ document: { ...readyDoc, target: { arn: AUTHTGT } } });
    // Step past source and credential to reach the target step.
    await next();
    await next();
    expect(container.querySelector('.wz-blocked').textContent)
      .toContain('requires authentication');
  });

  it('does not ask for a cache credential when the cache has no auth', async () => {
    await render({ document: { ...readyDoc, target: { arn: TGT } } });
    await next();
    await next();
    expect(container.querySelector('.wz-blocked').textContent).toBe('');
  });

  it('shows cluster mode and TLS, since both change how the sink connects',
     async () => {
       await render({ document: readyDoc });
       await next();
       await next();
       const option = optionFor(TGT);
       expect(option.textContent).toContain('cluster mode on');
       expect(option.textContent).toContain('3 shard(s)');
       expect(option.textContent).toContain('TLS');
     });
});

describe('the tables step', () => {
  const readyDoc = {
    name: 'shop', mode: 'cdc',
    source: { arn: SRC, database: 'Inventory', secret_arn: SEC },
    target: { arn: TGT },
  };

  async function toTables(doc) {
    const out = await render({ document: doc || readyDoc });
    await next();
    await next();
    await next();
    return out;
  }

  it('lists tables with their keys and row counts', async () => {
    await toTables();
    expect(container.textContent).toContain('orders');
    expect(container.textContent).toContain('key: id');
    expect(container.textContent).toContain('1,002');
  });

  it('shows the capture prerequisites it checked while connected', async () => {
    await toTables();
    expect(container.querySelector('.wz-prereq').textContent)
      .toContain('binlog_format is ROW');
  });

  it('flags a table with no primary key rather than hiding it', async () => {
    await toTables();
    expect(findByText('.wz-choice', 'audit').textContent)
      .toContain('no primary key');
  });

  it('writes the selection into the document', async () => {
    const { doc } = await toTables();
    await click(findByText('.wz-choice', 'key: id'));
    expect(doc().tables).toEqual(['orders']);
  });

  it('offers typed names even when the catalogue WAS readable', async () => {
    // Available always, not only as a failure path: listing needs a network route
    // into the customer's VPC that often does not exist, and someone may want a
    // table the catalogue does not list yet.
    await toTables();
    expect(container.querySelector('.wz-manual')).toBeTruthy();
    expect(container.querySelector('.wz-manual').textContent)
      .toContain('type table names instead');
  });

  it('offers typed names when the catalogue cannot be read', async () => {
    mockDiscovery(INVENTORY, {
      listed: false, stage: 'connect',
      error: 'could not reach MySQL at shop.rds:3306 within 8s',
      hint: 'The control plane needs a network path to the database.',
    });
    const { doc } = await toTables();
    expect(container.textContent).toContain('could not reach MySQL');
    const box = container.querySelector('.wz-textarea');
    expect(box).toBeTruthy();
    await act(async () => {
      // React tracks the value through its own property descriptor, so assigning
      // `.value` directly is invisible to it. The native setter is what makes a
      // programmatic change look like typing.
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(box, 'orders, items');
      box.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(doc().tables).toEqual(['orders', 'items']);
  });

  it('drops a table\'s shaping when the table is deselected', async () => {
    // Otherwise the document is left with a transform for a table that is not in
    // `tables`, which the validator rejects -- the form would be producing an
    // invalid config through ordinary use.
    const withTransform = {
      ...readyDoc,
      tables: ['orders'],
      transforms: { tables: { orders: { columns: ['id'] } } },
    };
    const { doc } = await toTables(withTransform);
    await click(findByText('.wz-choice', 'key: id'));
    const last = doc();
    expect(last.tables).toEqual([]);
    expect(last.transforms).toBeUndefined();
  });
});

describe('the shape step', () => {
  const doc = {
    name: 'shop', mode: 'cdc',
    source: { arn: SRC, database: 'Inventory', secret_arn: SEC },
    target: { arn: TGT },
    tables: ['orders'],
  };

  async function toShape() {
    const out = await render({ document: doc });
    await next(); await next(); await next(); await next();
    return out;
  }

  it('offers the discovered columns rather than asking for typed names',
     async () => {
       await toShape();
       const pills = Array.from(container.querySelectorAll('.wz-pill'))
         .map((p) => p.textContent);
       expect(pills).toEqual(expect.arrayContaining(['id', 'status', 'amount']));
     });

  it('writes a column selection into the document', async () => {
    const { doc } = await toShape();
    await click(Array.from(container.querySelectorAll('.wz-pill'))
      .find((p) => p.textContent === 'status'));
    expect(doc().transforms.tables.orders.columns)
      .toEqual(['status']);
  });

  it('shows no operator until a filter field is chosen', async () => {
    // An operator for a filter that does not exist is a control with nothing to
    // control, and would write a half-built filter into the document.
    await toShape();
    expect(container.querySelector('.wz-op')).toBeNull();
  });

  it('writes the chosen filter field into the document', async () => {
    const { doc } = await toShape();
    const fieldSelect = container.querySelector('.wz-select');
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLSelectElement.prototype, 'value').set;
      setter.call(fieldSelect, 'status');
      fieldSelect.dispatchEvent(new Event('change', { bubbles: true }));
    });
    const filter = doc().transforms.tables.orders.filter;
    expect(filter.field).toBe('status');
    expect(filter.op).toBe('=');
  });

  it('lets a column be named manually when the catalogue is unavailable', async () => {
    // Without this, shaping is unusable whenever listing failed: the form offers
    // an empty set of columns and no way to add one.
    mockDiscovery(INVENTORY, {
      listed: false, stage: 'connect', error: 'could not reach MySQL',
      hint: 'no route from the control plane',
    });
    const { doc: current } = await render({ document: doc });
    await next(); await next(); await next(); await next();
    const adder = Array.from(container.querySelectorAll('.wz-addcol input'))[0];
    expect(adder).toBeTruthy();
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set;
      setter.call(adder, 'status');
      adder.dispatchEvent(new Event('input', { bubbles: true }));
      adder.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter',
                                                         bubbles: true }));
    });
    const pills = Array.from(container.querySelectorAll('.wz-pill'))
      .map((p) => p.textContent);
    expect(pills).toContain('status');
    // And it is then selectable like any discovered column.
    await click(Array.from(container.querySelectorAll('.wz-pill'))
      .find((p) => p.textContent === 'status'));
    expect(current().transforms.tables.orders.columns).toEqual(['status']);
  });

  it('only offers filter operators the sink implements', async () => {
    // `contains` and `in` are absent deliberately: evalFilter returns true for an
    // unknown operator, so offering one would silently disable the filter rather
    // than fail. Started from a document that already has a filter, since that is
    // what reveals the operator control.
    const withFilter = {
      ...doc,
      transforms: { tables: { orders: {
        filter: { field: 'status', op: '=', value: 'SHIPPED' } } } },
    };
    await render({ document: withFilter });
    await next(); await next(); await next(); await next();
    const ops = Array.from(container.querySelectorAll('.wz-op option'))
      .map((o) => o.value);
    expect(ops).toEqual(['=', '!=', '>', '>=', '<', '<=']);
    expect(ops).not.toContain('contains');
    expect(ops).not.toContain('in');
  });
});

describe('denormalize in the shape step', () => {
  // Two tables, because a denorm rule embeds ONE of them into the other; the
  // parent must be a table in the link (it validates that), so the parent picker
  // offers only the link's other tables.
  const twoTableDoc = {
    name: 'shop', mode: 'cdc',
    source: { arn: SRC, database: 'Inventory', secret_arn: SEC },
    target: { arn: TGT },
    tables: ['orders', 'customers'],
  };

  async function toShape(document) {
    const out = await render({ document: document || twoTableDoc });
    await next(); await next(); await next(); await next();
    return out;
  }

  async function setSelect(sel, value) {
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLSelectElement.prototype, 'value').set;
      setter.call(sel, value);
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    });
  }

  async function setInput(el, value) {
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set;
      setter.call(el, value);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  it('adds a denormalize rule to the first table', async () => {
    const { doc } = await toShape();
    await click(findByText('.wz-mini', 'add a denormalize rule'));
    expect(container.querySelector('.wz-denorm')).toBeTruthy();
    expect(doc().transforms.tables.orders.denormalize.length).toBe(1);
  });

  it('offers the link\'s other tables as the parent, not this one', async () => {
    await toShape();
    await click(findByText('.wz-mini', 'add a denormalize rule'));
    const parentSelect = container.querySelector('.wz-denorm select');
    const values = Array.from(parentSelect.options)
      .map((o) => o.value).filter(Boolean);
    expect(values).toEqual(['customers']);
  });

  it('auto-fills the parent key prefix when the parent is chosen', async () => {
    const { doc } = await toShape();
    await click(findByText('.wz-mini', 'add a denormalize rule'));
    const parentSelect = container.querySelector('.wz-denorm select');
    await setSelect(parentSelect, 'customers');
    const rule = doc().transforms.tables.orders.denormalize[0];
    expect(rule.parent_table).toBe('customers');
    expect(rule.parent_key_prefix).toBe('customers:');
  });

  it('writes the join column and the embedded fields', async () => {
    const { doc } = await toShape();
    await click(findByText('.wz-mini', 'add a denormalize rule'));
    const block = container.querySelector('.wz-denorm');
    const selects = block.querySelectorAll('select');
    await setSelect(selects[0], 'customers');   // parent table
    await setSelect(selects[1], 'status');       // join column (a real column)
    const fieldsInput = block.querySelector('input[placeholder^="parent fields"]');
    await setInput(fieldsInput, 'name, tier');
    const rule = doc().transforms.tables.orders.denormalize[0];
    expect(rule.join_field).toBe('status');
    expect(rule.fields).toEqual(['name', 'tier']);
  });

  it('warns that embedding a volatile field multiplies writes', async () => {
    await toShape();
    await click(findByText('.wz-mini', 'add a denormalize rule'));
    expect(container.querySelector('.wz-denorm-warn').textContent)
      .toContain('rewrites it on every child row');
  });

  it('cannot add a rule when the link has only one table', async () => {
    // A parent to embed from has to exist; with one table there is none.
    await toShape({ ...twoTableDoc, tables: ['orders'] });
    const button = findByText('.wz-mini', 'select another table to embed from');
    expect(button).toBeTruthy();
    expect(button.disabled).toBe(true);
  });
});

describe('the review step', () => {
  const complete = {
    schema_version: 1, name: 'shop', mode: 'initial_load_and_cdc',
    source: { arn: SRC, database: 'Inventory', secret_arn: SEC },
    target: { arn: TGT },
    tables: ['orders'],
  };

  async function toReview(extra) {
    const calls = { resolve: 0, plan: 0, deploy: [] };
    await act(async () => {
      root.render(<LinkWizard document={complete} onChange={() => {}}
                              onDone={() => {}} canAct
                              onResolve={() => { calls.resolve += 1; }}
                              onPlan={() => { calls.plan += 1; }}
                              onDeploy={(real) => calls.deploy.push(real)}
                              {...extra} />);
    });
    for (let i = 0; i < 5; i += 1) await next();
    return calls;
  }

  it('puts the actions on the step, not somewhere else on the page', async () => {
    await toReview();
    const labels = Array.from(container.querySelectorAll('.wz-actions button, .wz-danger'))
      .map((b) => b.textContent);
    expect(labels).toContain('Check the infrastructure');
    expect(labels).toContain('Show what it will create');
    expect(labels).toContain('Confirm and deploy');
  });

  it('has a button that plainly says Deploy', async () => {
    // The original had one button whose label became "Deploy (dry run)" whenever a
    // checkbox was ticked, so there was never a button labelled only Deploy and
    // it was not clear that the checkbox was the cause.
    await toReview();
    const labels = Array.from(container.querySelectorAll('button'))
      .map((b) => b.textContent);
    expect(labels.some((l) => /^Confirm and deploy$/.test(l))).toBe(true);
    expect(labels.some((l) => l.includes('(dry run)'))).toBe(false);
  });

  it('does not deploy on the first click', async () => {
    // It creates infrastructure and starts billing, so it takes a second,
    // deliberate click.
    const calls = await toReview();
    await click(findByText('.wz-danger', 'Confirm and deploy'));
    expect(calls.deploy).toEqual([]);
    expect(container.querySelector('.wz-confirm')).toBeTruthy();
    expect(container.querySelector('.wz-confirm').textContent)
      .toContain('starts billing');
  });

  it('deploys on the confirming click', async () => {
    // Asserted as an OBJECT, because the first version passed a bare boolean and
    // the two sides disagreed about whether it meant "real" or "dryRun". The
    // polarity inverted on the way through, so confirming a real deploy quietly
    // ran a dry run and created nothing.
    const calls = await toReview();
    await click(findByText('.wz-danger', 'Confirm and deploy'));
    await click(findByText('.wz-danger', 'Yes, deploy it'));
    expect(calls.deploy).toEqual([{ dryRun: false }]);
  });

  it('cancel exits without deploying', async () => {
    const calls = await toReview();
    await click(findByText('.wz-danger', 'Confirm and deploy'));
    await click(findByText('.wz-mini', 'Cancel'));
    expect(calls.deploy).toEqual([]);
    expect(container.querySelector('.wz-confirm')).toBeNull();
  });

  it('names the stacks to delete to undo it', async () => {
    await toReview();
    await click(findByText('.wz-danger', 'Confirm and deploy'));
    expect(container.querySelector('.wz-confirm').textContent).toContain('shop-*');
  });

  it('resolve and plan are wired to the same handlers the File view uses', async () => {
    const calls = await toReview();
    await click(findByText('.wz-actions button', 'Check the infrastructure'));
    await click(findByText('.wz-actions button', 'Show what it will create'));
    expect(calls.resolve).toBe(1);
    expect(calls.plan).toBe(1);
  });

  it('actions are disabled while something is already running', async () => {
    await toReview({ busy: 'plan' });
    const planButton = findByText('.wz-actions button', 'Working…');
    expect(planButton.disabled).toBe(true);
  });

  it('in edit mode replaces deploy with a point to Compare/Apply', async () => {
    // hideDeployActions is set when the wizard is editing a running pipeline: it
    // must not offer to deploy a new one.
    await toReview({ hideDeployActions: true });
    const labels = Array.from(container.querySelectorAll('button'))
      .map((b) => b.textContent);
    expect(labels).not.toContain('Confirm and deploy');
    expect(labels).not.toContain('Check the infrastructure');
    expect(container.querySelector('.wz-editnote')).toBeTruthy();
    expect(container.querySelector('.wz-editnote').textContent)
      .toContain('Compare');
  });
});

describe('the alarm email (Jira story F105)', () => {
  // Without an email the CDC stack's alarm topic has no subscriber, so a link
  // deployed from this form raised every alarm to nobody.
  const complete = {
    schema_version: 1, name: 'shop', mode: 'initial_load_and_cdc',
    source: { arn: SRC, database: 'Inventory', secret_arn: SEC },
    target: { arn: TGT },
    tables: ['orders'],
  };
  async function toReview(document) {
    const handle = await render({ document });
    for (let i = 0; i < 5; i += 1) await next();
    return handle;
  }
  function emailInput() {
    return container.querySelector('.wz-alarm-email');
  }
  async function typeEmail(value) {
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set;
      setter.call(emailInput(), value);
      emailInput().dispatchEvent(new Event('input', { bubbles: true }));
    });
  }
  it('is optional: an empty field writes nothing and shows no error', async () => {
    const handle = await toReview(complete);
    expect(emailInput()).toBeTruthy();
    expect(emailInput().value).toBe('');
    expect(handle.doc().advanced).toBeUndefined();
    expect(container.querySelector('.wz-alarm .wz-err')).toBeNull();
  });
  it('writes advanced.alarms.email into the document', async () => {
    const handle = await toReview(complete);
    await typeEmail('ops@example.com');
    expect(handle.doc().advanced).toEqual({ alarms: { email: 'ops@example.com' } });
    expect(emailInput().value).toBe('ops@example.com');
    expect(container.querySelector('.wz-alarm .wz-err')).toBeNull();
  });
  it('says SNS sends a confirmation that must be accepted', async () => {
    await toReview(complete);
    const text = container.querySelector('.wz-alarm').textContent;
    expect(text).toContain('confirmation');
    expect(text).toContain('accepted');
  });
  it('flags an address that does not look like one, and still writes it', async () => {
    // The document stays the one source of truth: the API's validator reports
    // it too, and the form does not hold a second copy of the value.
    const handle = await toReview(complete);
    await typeEmail('ops@example');
    expect(container.querySelector('.wz-alarm .wz-err').textContent)
      .toContain('does not look like an email address');
    expect(handle.doc().advanced.alarms.email).toBe('ops@example');
    await typeEmail('ops@example.com');
    expect(container.querySelector('.wz-alarm .wz-err')).toBeNull();
  });
  it('clearing it removes the email and keeps the other advanced settings', async () => {
    const handle = await toReview({
      ...complete,
      advanced: { broker: { node_type: 'cache.r7g.large' },
                  alarms: { email: 'old@example.com', sink_lag_seconds: 120 } },
    });
    // The form reflects what the document already holds.
    expect(emailInput().value).toBe('old@example.com');
    await typeEmail('');
    expect(handle.doc().advanced).toEqual({
      broker: { node_type: 'cache.r7g.large' },
      alarms: { sink_lag_seconds: 120 },
    });
  });
  it('clearing the only advanced setting removes the advanced block', async () => {
    const handle = await toReview({ ...complete, advanced: { alarms: { email: 'a@b.co' } } });
    await typeEmail('   ');
    expect('advanced' in handle.doc()).toBe(false);
  });
  it('is not offered for a one-off load, which creates no alarms', async () => {
    await toReview({ ...complete, mode: 'initial_load' });
    expect(emailInput()).toBeNull();
    expect(container.querySelector('.wz-alarm-none').textContent)
      .toContain('creates no alarms');
  });
  it('is offered in edit mode too, where Apply updates it in place', async () => {
    await act(async () => {
      root.render(<LinkWizard document={complete} onChange={() => {}}
                              onDone={() => {}} hideDeployActions />);
    });
    for (let i = 0; i < 5; i += 1) await next();
    expect(emailInput()).toBeTruthy();
  });
});
describe('withAlarmEmail', () => {
  it('trims the address', () => {
    expect(withAlarmEmail({ name: 'x' }, '  ops@example.com ')).toEqual(
      { name: 'x', advanced: { alarms: { email: 'ops@example.com' } } });
  });
  it('does not change the document it was given', () => {
    const doc = { advanced: { alarms: { email: 'a@b.co' } } };
    withAlarmEmail(doc, 'c@d.co');
    expect(doc.advanced.alarms.email).toBe('a@b.co');
  });
  it('replaces a malformed advanced or alarms value instead of spreading it', () => {
    expect(withAlarmEmail({ advanced: 'oops' }, 'a@b.co'))
      .toEqual({ advanced: { alarms: { email: 'a@b.co' } } });
    expect(withAlarmEmail({ advanced: { alarms: ['x'] } }, 'a@b.co'))
      .toEqual({ advanced: { alarms: { email: 'a@b.co' } } });
  });
  it('a blank value on a document without advanced leaves it untouched', () => {
    expect(withAlarmEmail({ name: 'x' }, '')).toEqual({ name: 'x' });
    expect(withAlarmEmail({ name: 'x' }, null)).toEqual({ name: 'x' });
  });
});
describe('authentication', () => {
  it('says a token is needed instead of showing an empty account', async () => {
    // An empty list would let someone conclude they have no databases.
    await render({ tokenMissing: true });
    expect(container.querySelector('.wz-banner-warn').textContent)
      .toContain('needs the API token');
    expect(axios.get).not.toHaveBeenCalled();
  });

  it('reports a discovery failure with its hint', async () => {
    axios.get.mockResolvedValue({
      data: { discovered: false, error: 'no usable AWS credentials',
              hint: 'Refresh them.' },
    });
    await render({});
    expect(container.querySelector('.wz-banner-bad').textContent)
      .toContain('no usable AWS credentials');
    expect(container.querySelector('.wz-banner-bad').textContent)
      .toContain('Refresh them.');
  });

  it('surfaces a partial discovery failure without blocking the rest', async () => {
    mockDiscovery({
      ...INVENTORY,
      problems: [{ what: 'secrets', error: 'not permitted to list secrets',
                   hint: 'Everything else still works.' }],
    });
    await render({});
    expect(container.textContent).toContain('not permitted to list secrets');
    // The sources still rendered, as selectable options.
    expect(optionFor(SRC)).toBeTruthy();
  });
});

describe('opening on a step', () => {
  // Editing a running pipeline opens Guided on Shape, not the Source step, so the
  // reviewer does not have to start from the source widget every time.
  const doc = {
    schema_version: 1, name: 'shop', mode: 'cdc',
    source: { arn: SRC, database: 'Inventory', secret_arn: SEC },
    target: { arn: TGT }, tables: ['orders'],
  };

  it('starts on the Shape step when initialStep is "transforms"', async () => {
    await act(async () => {
      root.render(<LinkWizard document={doc} onChange={() => {}} onDone={() => {}}
                              initialStep="transforms" />);
    });
    await act(async () => { await new Promise((r) => setTimeout(r, 700)); });
    expect(container.textContent).toContain('Shape what gets cached');
  });

  it('defaults to the Source step with no initialStep', async () => {
    await act(async () => {
      root.render(<LinkWizard document={doc} onChange={() => {}} onDone={() => {}} />);
    });
    await act(async () => { await new Promise((r) => setTimeout(r, 300)); });
    expect(container.textContent).not.toContain('Shape what gets cached');
  });
});
