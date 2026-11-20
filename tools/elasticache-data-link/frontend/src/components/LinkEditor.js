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
import React, { useCallback, useEffect, useRef, useState } from 'react';
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
  const [check, setCheck] = useState(null);
  const [repair, setRepair] = useState(null);
  const [busy, setBusy] = useState(null);
  const [apiError, setApiError] = useState(null);
  const [loadWarnings, setLoadWarnings] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [deployment, setDeployment] = useState(null);
  // What Compare showed is a verdict on one document. `compared` holds that
  // document and the text it came from, and Apply sends exactly it. Apply used
  // to send whatever the editor held when it was clicked, so an edit made after
  // Compare was applied without ever being compared. `checkedText` is the text
  // `check` describes; the sequence refs drop responses that a newer edit has
  // overtaken.
  const [compared, setCompared] = useState(null);
  const [checkedText, setCheckedText] = useState(null);
  const parseSeqRef = useRef(0);
  const compareSeqRef = useRef(0);

  // Pull the running pipeline's configuration back into the link file. Every
  // setting is in it, so applying it unedited changes nothing.
  const loadDeployed = useCallback(async (name) => {
    if (!name) return;
    setBusy('reconstruct');
    setApiError(null);
    setLoadWarnings([]);
    setRepair(null);
    setCompared(null);
    compareSeqRef.current += 1;
    try {
      const res = await axios.get(`${API_URL}/api/links/reconstruct`,
                                  { params: { link: name }, timeout: 60000 });
      const data = res.data || {};
      if (data.ok && typeof data.text === 'string') {
        setText(data.text);
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
    setRepair(null);
    setDeployment(null);
    loadDeployed(link);
  }, [link, loadDeployed]);

  // An applied change runs in the background like a deploy; follow its record.
  const deploymentId = deployment && deployment.id;
  const deploymentDone = deployment
    && ['succeeded', 'failed', 'interrupted'].includes(deployment.status);
  useEffect(() => {
    if (!deploymentId || deploymentDone) return undefined;
    const t = setTimeout(async () => {
      try {
        const res = await axios.get(
          `${API_URL}/api/links/deployments/${deploymentId}`, { timeout: 30000 });
        setDeployment({ ...res.data, id: deploymentId });
      } catch (e) {
        setDeployment((d) => ({ ...d, pollError: e.message }));
      }
    }, 5000);
    return () => clearTimeout(t);
  }, [deploymentId, deploymentDone, deployment]);

  // Parse + validate as the file is edited. Debounced: a round trip per keystroke.
  useEffect(() => {
    const seq = parseSeqRef.current + 1;
    parseSeqRef.current = seq;
    if (!text) { setCheck(null); setCheckedText(null); return undefined; }
    const t = setTimeout(async () => {
      try {
        const res = await axios.post(`${API_URL}/api/links/parse`, { text },
                                     { timeout: 15000 });
        if (seq !== parseSeqRef.current) return;      // a newer edit exists
        setCheck(res.data);
        setCheckedText(text);
      } catch (e) {
        if (seq !== parseSeqRef.current) return;
        setCheck(null);
        setCheckedText(null);
      }
    }, 500);
    return () => clearTimeout(t);
  }, [text]);

  const document_ = check && check.parsed ? check.document : null;
  // The verdict must be for the text on screen, not for an earlier one.
  const canAct = Boolean(document_ && check.valid && checkedText === text);

  // An edit makes the comparison on screen describe a different document, so it
  // goes, along with the acknowledgement given for it.
  const onEdit = (value) => {
    setText(value);
    compareSeqRef.current += 1;                 // a Compare in flight is stale too
    if (compared && value !== compared.text) {
      setCompared(null);
      setRepair(null);
      setAcknowledged(false);
    }
  };

  // Compare (apply=false) changes nothing; Apply (apply=true) updates the running
  // pipeline in place. The server compares the edited file with the DEPLOYED
  // STACKS, not with the text loaded here: a baseline sent from the browser
  // hid every setting the reconstructed file had left out.
  const doCompare = async (apply) => {
    // Apply sends the document Compare showed; Compare sends the one on screen.
    const sent = apply ? compared : { text, document: document_ };
    if (!sent || !sent.document) return;
    const seq = compareSeqRef.current + 1;
    compareSeqRef.current = seq;
    setApiError(null);
    setBusy(apply ? 'apply-change' : 'compare');
    try {
      const res = await axios.post(`${API_URL}/api/links/repair`, {
        desired: sent.document, region: null, apply: Boolean(apply),
        acknowledge: Boolean(apply && acknowledged),
      }, { timeout: 120000, validateStatus: (st) => st < 500 });
      // A Compare answer that an edit has overtaken describes the old text.
      // An Apply answer is always shown: the change has been made.
      if (!apply && seq !== compareSeqRef.current) return;
      setRepair(res.data);
      if (!apply) {
        setAcknowledged(false);
        setCompared(sent);
      }
      if (apply && res.data && res.data.deployment_id) {
        setDeployment({ id: res.data.deployment_id, status: 'running' });
      }
    } catch (e) {
      if (!apply && seq !== compareSeqRef.current) return;
      const d = e.response && e.response.data;
      setApiError((d && (d.error || d.reason)) || e.message || 'request failed');
    } finally {
      setBusy(null);
    }
  };

  const needsAck = Boolean(repair && repair.needs_acknowledge);
  const applyBlocked = !canAct || !repair || !repair.changed
    || !compared || compared.text !== text
    || (repair.plan && repair.plan.blocked) || Boolean(busy)
    || (needsAck && !acknowledged)
    || Boolean(deployment && !deploymentDone);

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
              onChange={(e) => onEdit(e.target.value)}
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

            {needsAck && !(repair.plan && repair.plan.blocked) && (
              <label className="lk-ack">
                <input type="checkbox" checked={acknowledged}
                       onChange={(e) => setAcknowledged(e.target.checked)} />
                {' '}After the update the cache needs the steps under
                {' '}&ldquo;still for you to run&rdquo;. I will run them.
              </label>
            )}

            {deployment && (
              <div className={`lk-deploystatus lk-st-${deployment.status}`}>
                update {deployment.id}: <strong>{deployment.status}</strong>
                {deployment.error && <div className="lk-bad">{deployment.error}</div>}
                {deployment.hint && <div className="lk-hint">{deployment.hint}</div>}
                {deployment.pollError && (
                  <div className="lk-muted">could not read progress: {deployment.pollError}</div>
                )}
              </div>
            )}

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
