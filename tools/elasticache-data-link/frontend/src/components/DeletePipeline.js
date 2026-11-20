// ═══════════════════════════════════════════════════════════════════════════
// Delete a pipeline — the UI side of `ecdl destroy --link`.
//
// There was no way to remove a pipeline from the UI at all. This shows exactly
// what would be deleted and kept (read from the same teardown plan the CLI
// prints), and asks for the pipeline's name to be typed before deleting, because
// deleting stops replication and removes the broker.
// ═══════════════════════════════════════════════════════════════════════════
import React, { useEffect, useState } from 'react';
import axios from 'axios';
import API_URL from '../apiUrl';

// What the operator still has to do afterwards: these live in their own
// database or cache, not in the pipeline's stacks.
export function FollowUps({ followUp, link }) {
  if (!followUp) return null;
  return (
    <ul className="lk-followups">
      <li>
        The signalling table <code>{followUp.signal_table}</code> stays in your source
        database. Drop it there if you no longer need it.
      </li>
      <li>
        The cache keys this pipeline wrote stay in the target cache. Nothing updates
        them any more; delete them by key prefix if you want them gone.
      </li>
      <li>
        PostgreSQL sources only: drop the replication slot, or the database keeps
        its write-ahead log indefinitely and its disk fills:{' '}
        <code>{followUp.pg_drop_slot_sql}</code>
      </li>
      {followUp.broker_replication_group && (
        <li>
          The broker <code>{followUp.broker_replication_group}</code> is deleted with a
          final snapshot of its offsets, schema history, unapplied changes and dead
          letters. ElastiCache keeps it, and charges for it, until you delete it; the
          pipeline can be deployed again from it
          (<code>advanced.broker.restore_snapshot</code>).{' '}
          {followUp.broker_final_snapshot
            ? <>Snapshot: <code>{followUp.broker_final_snapshot}</code>. To delete
                it: <code>{followUp.broker_snapshot_delete_command}</code></>
            : <>Find it with: <code>{followUp.broker_snapshot_find_command}</code></>}
        </li>
      )}
    </ul>
  );
}

// What the broker holds that the cache does not (Jira story F136): changes not yet
// applied, per stream, and dead letters, per dead-letter stream. `data` is
// the API's broker_data block ({unapplied, dead_letters_total, backlog,
// dead_letters}); rendered only when it says something is at risk. `bare`
// drops the count line, for when the API's refusal has just said it.
export function AtRisk({ data, bare }) {
  if (!data || !data.at_risk) return null;
  const rows = [
    ...Object.entries(data.backlog || {}).map(([stream, n]) =>
      ({ stream, n, what: 'not yet in the cache' })),
    ...Object.entries(data.dead_letters || {}).map(([stream, n]) =>
      ({ stream, n, what: 'dead letters' })),
  ];
  return (
    <div className="lk-bad lk-atrisk">
      {!bare && (
        <div>
          <strong>
            The broker holds {data.unapplied || 0} change(s) not yet written to
            the cache and {data.dead_letters_total || 0} dead letter(s).
          </strong>{' '}
          Deleting it keeps them only in the broker's final snapshot.
        </div>
      )}
      {rows.length > 0 && (
        <table className="lk-table">
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.what}:${r.stream}`}>
                <td className="lk-mono">{r.stream}</td>
                <td>{r.n}</td>
                <td>{r.what}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function DeletePipeline({ link, onStarted, onCancel }) {
  const [plan, setPlan] = useState(null);
  const [error, setError] = useState(null);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  // Jira story F136. The API refuses to delete a broker holding unapplied changes or dead
  // letters unless the request says "delete_backlog": true. `refusal` is that
  // refusal and `dropBacklog` the operator's explicit answer to it. Unticked
  // by default, and the flag is never sent unless the box is shown and ticked.
  const [refusal, setRefusal] = useState(null);
  const [dropBacklog, setDropBacklog] = useState(false);

  useEffect(() => {
    let alive = true;
    axios.get(`${API_URL}/api/links/destroy-plan`, { params: { link }, timeout: 30000 })
      .then((res) => { if (alive) setPlan(res.data); })
      .catch((e) => {
        if (!alive) return;
        const d = e.response && e.response.data;
        setError((d && d.error) || e.message || 'could not read what would be deleted');
      });
    return () => { alive = false; };
  }, [link]);

  const remove = async () => {
    setBusy(true);
    setError(null);
    try {
      const body = { link, confirm: typed };
      if (dropBacklog && offerDrop) body.delete_backlog = true;
      const res = await axios.post(`${API_URL}/api/links/destroy`,
        body, { timeout: 60000 });
      onStarted(res.data.destroy_id);
    } catch (e) {
      const d = e.response && e.response.data;
      if (d && d.stage === 'backlog') {
        // Refused because of what the broker holds: say what, and offer the
        // checkbox. The counts in the refusal are fresher than the plan's.
        setRefusal({ data: d.broker || null, error: d.error, hint: d.hint });
      } else {
        setError((d && d.error) || e.message || 'the delete was refused');
      }
      setBusy(false);
    }
  };

  const stacks = (plan && plan.stacks) || [];
  const matches = typed.trim() === link;
  // What is at risk: the refusal's reading when there is one, else the plan's.
  const atRisk = (refusal && refusal.data)
    || (plan && plan.broker && plan.broker.at_risk ? plan.broker : null);
  // The checkbox is offered once something is known to be at risk: from the
  // plan, or from a refusal (whose counts are fresher).
  const offerDrop = Boolean(refusal || atRisk);
  return (
    <section className="lk-delete">
      <h2>Delete pipeline {link}</h2>
      {error && <div className="lk-bad lk-blocked">{error}</div>}
      {!plan && !error && <p className="lk-muted">Reading what would be deleted…</p>}
      {plan && (
        <>
          <p>
            Replication stops. These stacks are deleted, in this order, which takes
            about 5 to 10 minutes (most of it the broker cache):
          </p>
          <table className="lk-table">
            <tbody>
              {stacks.map((s) => (
                <tr key={s.name}>
                  <th>{s.id}</th>
                  <td className="lk-mono">{s.name}</td>
                  <td>{s.status || <span className="lk-muted">not deployed</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="lk-muted">
            Its ECS cluster <code>{plan.plan.cluster_name}</code> is removed too if it
            is empty afterwards.
          </p>
          <p><strong>Not touched:</strong> your source database and your target cache.</p>
          {refusal ? (
            <>
              <div className="lk-bad lk-blocked lk-backlog-refusal">
                {refusal.error}
                {refusal.hint && <div className="lk-hint">{refusal.hint}</div>}
              </div>
              <AtRisk data={refusal.data} bare />
            </>
          ) : atRisk ? (
            <AtRisk data={atRisk} />
          ) : plan.broker_summary ? (
            // Also says when the broker could not be read, which does not block.
            <p className="lk-muted lk-broker-summary">Broker: {plan.broker_summary}</p>
          ) : null}
          <p><strong>Left for you afterwards:</strong></p>
          <FollowUps followUp={plan.plan.follow_up} link={link} />
          {plan.exists ? (
            <div className="lk-deleteconfirm">
              <label htmlFor="lk-delete-name">
                Type <code>{link}</code> to confirm
              </label>
              <input id="lk-delete-name" className="wz-input" value={typed}
                     autoComplete="off" spellCheck="false"
                     onChange={(e) => setTyped(e.target.value)} />
              {offerDrop && (
                <label className="lk-delete-backlog">
                  <input type="checkbox" checked={dropBacklog}
                         onChange={(e) => setDropBacklog(e.target.checked)} />
                  {' '}Delete the backlog and dead letters too
                  <span className="lk-muted">
                    {' '}(kept only in the broker's final snapshot)
                  </span>
                </label>
              )}
              <div className="lk-actions">
                <button className="lk-danger" disabled={!matches || busy}
                        onClick={remove}>
                  {busy ? 'Deleting…' : 'Delete pipeline'}
                </button>
                <button onClick={onCancel} disabled={busy}>Cancel</button>
              </div>
            </div>
          ) : (
            <p className="lk-muted">
              Nothing to delete: this pipeline has no stacks.{' '}
              <button className="lk-mini" onClick={onCancel}>Back</button>
            </p>
          )}
        </>
      )}
      {!plan && error && <button className="lk-mini" onClick={onCancel}>Back</button>}
    </section>
  );
}

export default DeletePipeline;
