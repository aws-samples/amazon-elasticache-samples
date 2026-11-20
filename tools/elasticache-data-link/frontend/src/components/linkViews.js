// ═══════════════════════════════════════════════════════════════════════════
// linkViews — presentational pieces shared by the Build Pipeline page (Links)
// and the pipeline editor (LinkEditor) so the two cannot drift.
//
// RepairDetail in particular renders the diff for a change after deployment: what a change to a running
// pipeline breaks, what applying it does, and what is left for the operator to
// run. It was defined inside Links.js when editing lived there; editing now lives
// on the Deployments page, so it moved here rather than being copied.
// ═══════════════════════════════════════════════════════════════════════════
import React from 'react';

export const SEVERITY_ORDER = ['none', 'deploy', 'backfill', 'cleanup', 'recreate'];

export function Problems({ problems }) {
  if (!problems || !problems.length) return null;
  return (
    <ul className="lk-problems">
      {problems.map((p, i) => (
        <li key={i}>
          {p.path ? <code>{p.path}</code> : <code>(root)</code>}
          <span>{p.message}</span>
        </li>
      ))}
    </ul>
  );
}

export function Severity({ level }) {
  return (
    <span className={`lk-sev lk-sev-${SEVERITY_ORDER.includes(level) ? level : 'none'}`}>
      {level}
    </span>
  );
}

// A rename or a column removal leaves the OLD field name sitting in every
// already-written target key: HSET merges, it does not replace, so the stale
// field lingers until the key is fully rewritten. The sink now rewrites each
// key on a snapshot read (the backfill a rename/removal forces), so this is
// handled automatically -- but only say so when the diff actually contains such
// a change, so the reassurance is never misplaced.
function cleansStaleFields(changes) {
  return (changes || []).some((c) =>
    /renamed|selected columns|removed/i.test(c.consequence || '')
  );
}

// The result of a compare or an apply against a running pipeline.
export function RepairDetail({ data: repair }) {
  if (!repair) return null;
  return (
    <section>
      <h2>Change to a running link</h2>
      {!repair.ok && (
        <div className="lk-bad">
          {repair.stage}: {repair.error || 'could not compare'}
          {repair.hint && <div className="lk-hint">{repair.hint}</div>}
          <Problems problems={repair.problems} />
        </div>
      )}

      {repair.ok && !repair.changed && (
        <div className="lk-good">
          no differences — nothing to do
        </div>
      )}

      {repair.ok && repair.changed && (
        <>
          <div className="lk-sevline">
            worst severity <Severity level={repair.diff.severity} />
            <span className="lk-muted">
              {' '}{repair.diff.changes.length} change(s)
            </span>
          </div>

          {repair.plan && repair.plan.blocked && (
            <div className="lk-bad lk-blocked">
              <strong>This cannot be applied to the running link.</strong>
              <div>
                It needs a new link. Applying part of it would leave the
                deployment matching neither configuration, so it is
                refused rather than half-done.
              </div>
            </div>
          )}

          {/* Each change says what it BREAKS, not just what differs --
              that is the part a redeploy silently gets wrong. */}
          <ul className="lk-changes">
            {repair.diff.changes.map((c, i) => (
              <li key={i}>
                <div>
                  <Severity level={c.severity} />
                  <strong>{c.field}</strong>
                  {c.table && <code>{c.table}</code>}
                  {c.parameter && c.parameter !== c.field && (
                    <code>
                      {c.parameter}: {JSON.stringify(c.from)} → {JSON.stringify(c.to)}
                    </code>
                  )}
                </div>
                <div className="lk-why">{c.consequence}</div>
                <div className="lk-muted">→ {c.action}</div>
              </li>
            ))}
          </ul>

          {cleansStaleFields(repair.diff.changes) && (
            <div className="lk-good lk-autoclean">
              Renamed or removed fields are cleaned automatically. The backfill
              above re-snapshots the table, and the sink rewrites each key on a
              snapshot read — so old field names from the previous shape are
              dropped. You do not need to clear them manually.
            </div>
          )}

          {(repair.plan.steps || []).length > 0 && (
            <>
              <h3>what applying it does</h3>
              <ol className="lk-steps">
                {repair.plan.steps.map((s) => (
                  <li key={s.id}>
                    <span className="lk-kind">{s.kind}</span>
                    <strong>{s.id}</strong>
                    {s.confirm && (
                      <span className="lk-needsconfirm">
                        needs confirmation
                      </span>
                    )}
                    <div className="lk-muted">{s.purpose}</div>
                    {s.collections && (
                      <div className="lk-wire">
                        {s.collections.join(', ')}
                      </div>
                    )}
                    {s.key_patterns && (
                      <div className="lk-wire">
                        {s.key_patterns.join(', ')}
                      </div>
                    )}
                  </li>
                ))}
              </ol>
            </>
          )}

          {repair.applied && (
            <div className="lk-good lk-applied">
              update started{repair.deployment_id ? ` (${repair.deployment_id})` : ''}.
              Capture keeps running — the pipeline is not paused and the bulk
              load is not re-run.
            </div>
          )}

          {/* Two things the control plane deliberately does not do. */}
          {(repair.outstanding || []).length > 0 && (
            <>
              <h3>still for you to run</h3>
              {repair.outstanding.map((o, i) => (
                <div key={i} className="lk-outstanding">
                  <div>
                    <strong>{o.kind}</strong> — {o.why}
                  </div>
                  <div className="lk-muted">
                    not done automatically: {o.reason_not_automatic}
                  </div>
                  {o.blocked_reason && (
                    <div className="lk-bad">{o.blocked_reason}</div>
                  )}
                  {o.statement && (
                    <pre>{o.statement}
    {'\n'}values: {JSON.stringify(o.values)}</pre>
                  )}
                  {o.key_patterns && (
                    <pre>{o.key_patterns.join('\n')}</pre>
                  )}
                </div>
              ))}
            </>
          )}

          {(repair.plan.notes || []).length > 0 && (
            <div className="lk-notes">
              {repair.plan.notes.map((n, i) => <div key={i}>– {n}</div>)}
            </div>
          )}

          {repair.plan_summary && (
            <details className="lk-events">
              <summary>full plan (identical to the CLI output)</summary>
              <pre>{repair.diff_summary}{'\n\n'}{repair.plan_summary}</pre>
            </details>
          )}
        </>
      )}
    </section>
  );
}
