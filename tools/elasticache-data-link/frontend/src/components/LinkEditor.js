// ═══════════════════════════════════════════════════════════════════════════
// LinkEditor — change a pipeline that is already running.
//
// This is the edit half of what used to live on the Build Pipeline page. Build
// Pipeline is for CREATING a link; changing one that is deployed is a different
// question ("will the cache still be right afterwards", not "will it deploy"), so
// it lives on the Deployments page next to the pipeline it edits.
//
// It loads the running pipeline's configuration back into the link file, edits it
// as the file itself (no wizard: editing a running pipeline is a small, targeted
// change and a worked example is clearer than a form), and applies the change IN
// PLACE via /repair -- a transform change re-snapshots only the affected table
// and leaves capture running. It never deploys a new pipeline; the only actions
// are Compare and Apply.
// ═══════════════════════════════════════════════════════════════════════════
import React, { useCallback, useEffect, useState } from 'react';
import axios from 'axios';
import { RepairDetail } from './linkViews';
import './Links.css';
import API_URL from '../apiUrl';

// 'Inventory.orders' -> 'orders'. Table keys under transforms.tables are the
// BARE table name.
const bare = (t) => String(t || '').split('.').pop();

// One annotated YAML covering every transform, using this pipeline's own first
// table name. One entry per table under transforms.tables; a table that needs
// several transforms keeps them all under its single entry.
function exampleYaml(tables) {
  const t = (tables && tables[0]) || 'orders';
  return `# One entry per table under transforms.tables.
# Put ALL of a table's transforms under it — never list a table twice.
# Replace the table and column names with your own.
tables:
  - Inventory.${t}
  - Inventory.customers          # parent table, needed by ${t}'s denormalize

transforms:
  tables:
    ${t}:                        # this table uses several transforms at once
      columns:                   # cache only these columns (omit to keep all)
        - id
        - customer_id
        - amount
        - status
      computed:                  # extra field from the row (+ - * / and parens)
        - name: amount_cents
          expr: amount * 100
      filter:                    # cache only matching rows (= != > >= < <=)
        field: status
        op: '='
        value: SHIPPED
      rename:                    # the cached field takes the new name
        amount: total
      denormalize:               # embed a parent table (must be in tables above)
        - parent_table: customers
          join_field: customer_id   # the foreign-key column on ${t}
          fields: [name, tier]      # parent columns to embed
          prefix: cust_

    customers:                   # a second table gets its own block
      columns:
        - id
        - name
        - tier`;
}

function LinkEditor({ link, onClose }) {
  const [text, setText] = useState('');
  const [currentText, setCurrentText] = useState('');
  const [check, setCheck] = useState(null);
  const [repair, setRepair] = useState(null);
  const [busy, setBusy] = useState(null);
  const [apiError, setApiError] = useState(null);
  const [loadWarnings, setLoadWarnings] = useState([]);
  const [loaded, setLoaded] = useState(false);

  // Pull the running pipeline's configuration back into the link file. The
  // baseline (currentText) and the editable copy (text) start identical --
  // editing the copy is what produces a diff to apply.
  const loadDeployed = useCallback(async (name) => {
    if (!name) return;
    setBusy('reconstruct');
    setApiError(null);
    setLoadWarnings([]);
    setRepair(null);
    try {
      const res = await axios.get(`${API_URL}/api/links/reconstruct`,
                                  { params: { link: name }, timeout: 60000 });
      const data = res.data || {};
      if (data.ok && typeof data.text === 'string') {
        setText(data.text);
        setCurrentText(data.text);
        setLoadWarnings(data.warnings || []);
        setLoaded(true);
      } else {
        setApiError(data.error || `could not load "${name}"`);
      }
    } catch (e) {
      const d = e.response && e.response.data;
      setApiError((d && (d.error || d.reason)) || e.message
                  || 'could not load that pipeline');
    } finally {
      setBusy(null);
    }
  }, []);

  useEffect(() => {
    setLoaded(false);
    setText('');
    setCurrentText('');
    setRepair(null);
    loadDeployed(link);
  }, [link, loadDeployed]);

  // Parse + validate as the file is edited. Debounced: a round trip per keystroke.
  useEffect(() => {
    if (!text) { setCheck(null); return undefined; }
    const t = setTimeout(async () => {
      try {
        const res = await axios.post(`${API_URL}/api/links/parse`, { text },
                                     { timeout: 15000 });
        setCheck(res.data);
      } catch (e) {
        setCheck(null);
      }
    }, 500);
    return () => clearTimeout(t);
  }, [text]);

  const document_ = check && check.parsed ? check.document : null;
  const canAct = Boolean(document_ && check.valid);

  // Compare (apply=false) changes nothing; Apply (apply=true) updates the running
  // pipeline in place. Both diff the editable copy against the deployed baseline.
  const doCompare = async (apply) => {
    setApiError(null);
    let currentDoc = null;
    try {
      const parsed = await axios.post(`${API_URL}/api/links/parse`,
                                      { text: currentText }, { timeout: 15000 });
      if (!parsed.data.parsed) {
        setApiError('the deployed configuration could not be read');
        return;
      }
      currentDoc = parsed.data.document;
    } catch (e) {
      setApiError(e.message || 'could not read the deployed configuration');
      return;
    }
    setBusy(apply ? 'apply-change' : 'compare');
    try {
      const res = await axios.post(`${API_URL}/api/links/repair`, {
        current: currentDoc, desired: document_, region: null,
        apply: Boolean(apply),
      }, { timeout: 120000 });
      setRepair(res.data);
    } catch (e) {
      const d = e.response && e.response.data;
      setApiError((d && (d.error || d.reason)) || e.message || 'request failed');
    } finally {
      setBusy(null);
    }
  };

  const applyBlocked = !canAct || !repair || !repair.changed
    || (repair.plan && repair.plan.blocked) || Boolean(busy);

  return (
    <section className="lk-editor-panel">
      <div className="lk-detailhead">
        <h2>Edit {link}</h2>
        <button className="lk-mini" onClick={() => loadDeployed(link)}
                disabled={Boolean(busy)}>
          {busy === 'reconstruct' ? 'Reloading…' : 'Reload from deployed'}
        </button>
        {onClose && (
          <button className="lk-mini" onClick={onClose}>Close editor</button>
        )}
      </div>

      <p className="lk-muted">
        This is the link file, loaded from what is deployed. Edit it below and
        Compare — Apply updates the running pipeline in place where it can (a
        transform change re-snapshots only that table and leaves capture running).
        A change to the name, source or target needs a new pipeline instead.
      </p>

      {apiError && <div className="lk-apierror">{apiError}</div>}
      {loadWarnings.length > 0 && (
        <div className="lk-loadwarn">
          {loadWarnings.map((w, i) => <div key={i}>⚠ {w}</div>)}
        </div>
      )}

      {!loaded && !apiError && (
        <div className="lk-empty">Loading the deployed configuration…</div>
      )}

      {loaded && (
        <div className="lk-edit-grid">
          <div className="lk-edit-main">
            <textarea
              className="lk-editor"
              spellCheck="false"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />

            <div className="lk-status">
              {check && check.parsed && check.valid && (
                <span className="lk-good">
                  valid — {check.normalized && check.normalized.name} (
                  {check.normalized && check.normalized.mode}),{' '}
                  {check.normalized && (check.normalized.tables || []).length} table(s)
                </span>
              )}
              {check && check.parsed && !check.valid && (
                <span className="lk-bad">
                  {(check.problems || []).length} problem(s)
                </span>
              )}
              {check && !check.parsed && (
                <span className="lk-bad">
                  {check.error}
                  {check.line ? ` (line ${check.line}, column ${check.column})` : ''}
                </span>
              )}
            </div>

            <div className="lk-actions">
              <button disabled={!canAct || Boolean(busy)}
                      onClick={() => doCompare(false)}>
                {busy === 'compare' ? 'Comparing…' : 'Compare'}
              </button>
              <button className="lk-danger" disabled={applyBlocked}
                      onClick={() => doCompare(true)}>
                {busy === 'apply-change' ? 'Applying…' : 'Apply the change'}
              </button>
            </div>

            {repair && (
              <div className="lk-repair-result">
                <RepairDetail data={repair} />
              </div>
            )}
          </div>

          {/* One annotated example that shows every transform, so a change can be
              copied from it instead of written from memory. */}
          <aside className="lk-examples">
            <h3>Example — every transform</h3>
            <pre>{exampleYaml(
              ((check && check.normalized && check.normalized.tables)
                || (document_ && document_.tables) || []).map(bare),
            )}</pre>
          </aside>
        </div>
      )}
    </section>
  );
}

export default LinkEditor;
