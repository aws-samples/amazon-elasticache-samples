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
        its write-ahead log for ever and its disk fills:{' '}
        <code>{followUp.pg_drop_slot_sql}</code>
      </li>
    </ul>
  );
}

function DeletePipeline({ link, onStarted, onCancel }) {
  const [plan, setPlan] = useState(null);
  const [error, setError] = useState(null);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);

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
      const res = await axios.post(`${API_URL}/api/links/destroy`,
        { link, confirm: typed }, { timeout: 60000 });
      onStarted(res.data.destroy_id);
    } catch (e) {
      const d = e.response && e.response.data;
      setError((d && d.error) || e.message || 'the delete was refused');
      setBusy(false);
    }
  };

  const stacks = (plan && plan.stacks) || [];
  const matches = typed.trim() === link;
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
