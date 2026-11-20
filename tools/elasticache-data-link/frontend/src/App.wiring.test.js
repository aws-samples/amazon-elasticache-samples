/*
 * Every sidebar entry must render something.
 *
 * This exists because of a real mistake: the sidebar was pointed at a
 * `link-deployments` view before the component and its dispatch line existed, so
 * clicking the menu entry rendered a blank pane. Nothing failed, nothing logged --
 * the menu did nothing. It was caught by looking, which is not a method.
 *
 * The check is a text comparison rather than a render test on purpose: it must
 * fail for an entry that has no test of its own, which is precisely the
 * case that was missed.
 */
import fs from 'fs';
import path from 'path';

const SRC = path.join(__dirname);

function read(file) {
  return fs.readFileSync(path.join(SRC, file), 'utf8');
}

// Sidebar entries are declared as item('<view>', 'Label').
function sidebarViews() {
  const text = read('components/Sidebar.js');
  const found = new Set();
  const re = /item\(\s*'([a-z0-9-]+)'/g;
  let match = re.exec(text);
  while (match) {
    found.add(match[1]);
    match = re.exec(text);
  }
  return found;
}

// App.js dispatches with {activeView === '<view>' && <Component />}.
function routableViews() {
  const text = read('App.js');
  const found = new Set();
  const re = /activeView\s*===\s*'([a-z0-9-]+)'/g;
  let match = re.exec(text);
  while (match) {
    found.add(match[1]);
    match = re.exec(text);
  }
  return found;
}

describe('navigation wiring', () => {
  it('finds the sidebar entries and the routes at all', () => {
    // Checks the check itself. An empty set on either side would make every assertion
    // below pass while checking nothing.
    expect(sidebarViews().size).toBeGreaterThan(5);
    expect(routableViews().size).toBeGreaterThan(5);
  });

  it('every sidebar entry renders a component', () => {
    const routable = routableViews();
    const unroutable = [...sidebarViews()].filter((v) => !routable.has(v));
    expect(unroutable).toEqual([]);
  });

  it('Links is reachable, since it is the way in', () => {
    expect(sidebarViews().has('links')).toBe(true);
    expect(routableViews().has('links')).toBe(true);
  });

  it('the Randomizer is not offered until it is ready', () => {
    // It writes generated rows into the source database. Hidden from the
    // menu and not routable, so no click or stale view state reaches it.
    expect(sidebarViews().has('randomizer')).toBe(false);
    expect(routableViews().has('randomizer')).toBe(false);
    expect(read('components/Sidebar.js')).not.toMatch(/group-title">\s*4\. Test/);
    expect(read('App.js')).not.toContain('Randomizer');
  });

  it('the guided builder is a view of Links, not a separate destination', () => {
    // Two menu entries for "make a link" would be two ways to do one thing,
    // which is the split this work exists to close.
    expect(read('components/Links.js')).toContain('LinkWizard');
    expect(read('App.js')).not.toContain('LinkWizard');
    expect(sidebarViews().has('wizard')).toBe(false);
  });
});
