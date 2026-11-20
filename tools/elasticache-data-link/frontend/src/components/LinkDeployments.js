// ═══════════════════════════════════════════════════════════════════════════
// Deployments — link deployments, from /api/links/deployments.
//
// This is separate from the older Deployments page on purpose. That one is built
// on /api/pipelines, which is the object model a link replaces, so pointing the
// primary flow at it would lead straight back into the thing being retired.
//
// A deployment here is a sequence of named steps, and the interesting question is
// never just "did it work" but "how far did it get" -- a link that created its
// broker and then failed on the CDC stack is in a different position from one that
// failed before anything existed. So the step list is the main content, and a
// failure names the step it failed on.
// ═══════════════════════════════════════════════════════════════════════════
import React, { useCallback, useEffect, useRef, useState } from 'react';
// The SAME deployment view the Links run history uses. This page had its
// own done/not-done step list and a raw event dump, so the identical
// deployment read as progress on one page and as a hundred identical
// "stack_waiting" lines on the other.
import { DeploymentEvents, DeploymentSteps } from './deploymentView';
// The same log panel the monitor uses. A deploy that fails because a task will
// not start is only explicable from that task's log, and sending someone to the
// CloudWatch console for it means leaving the tool mid-diagnosis.
import ComponentLogs from './ComponentLogs';
// Editing a running pipeline lives here, next to the pipeline it changes, rather
// than on the Build Pipeline page (which is for creating a new one).
import LinkEditor from './LinkEditor';
// Deleting a pipeline: the same teardown as `ecdl destroy --link`.
import DeletePipeline, { FollowUps } from './DeletePipeline';
import axios from 'axios';
import './Links.css';
import API_URL from '../apiUrl';

const POLL_MS = 4000;

// `paused` (Jira story F135) is a link whose stacks are fine but whose Debezium service is
// held at desired count 0: nothing is captured. It has its own style because
// green would read as "working" and red as "broken", and it is neither.
export function StatusChip({ status }) {
  const known = ['running', 'succeeded', 'failed', 'paused'].includes(status)
    ? status : 'unknown';
  return <span className={`lk-chip lk-chip-${known}`}>{status || 'unknown'}</span>;
}

// CloudFormation reports strings, not booleans, so compare against 'true'.
const isTrue = (v) => String(v) === 'true';
const tlsLabel = (v) => (isTrue(v) ? 'TLS' : 'no TLS');

// Live running/desired for one ECS service. Green only when it is fully up,
// amber while partially up, red at zero -- the same three-state indicator that a stack
// status cannot give, because a healthy stack can still be running 0 tasks.
// A desired count of 0 is a deliberate stop (capture is paused that way for a
// bulk load, Jira story F135), so 0/0 is paused, not red: red is for tasks that should
// be running and are not.
export function ServiceChip({ svc }) {
  const desired = Number(svc.desired) || 0;
  const running = Number(svc.running) || 0;
  let cls = 'lk-chip-failed';
  if (running > 0 && running >= desired && desired > 0) cls = 'lk-chip-succeeded';
  else if (running > 0) cls = 'lk-chip-running';
  else if (desired === 0) cls = 'lk-chip-paused';
  return (
    <span className={`lk-chip ${cls}`}
          title={desired === 0 ? 'desired count 0: paused' : undefined}>
      {running}/{desired}
    </span>
  );
}

// A one-line summary for the list row, so the topology is visible without a
// action: engine, stream count, and how many of the pipeline's services are up.
function rowSummary(d) {
  const c = d.components;
  if (!c) return null;
  const eng = (c.source && c.source.engine) || 'db';
  const n = (c.streams || []).length;
  const svcs = (c.runtime && c.runtime.services) || [];
  // Up means fully up, the same test as the green ServiceChip. Counting any
  // service with one task running called a 1/3 service "up".
  const up = svcs.filter((s) => {
    const desired = Number(s.desired) || 0;
    return desired > 0 && (Number(s.running) || 0) >= desired;
  }).length;
  const parts = [eng, `${n} stream${n === 1 ? '' : 's'}`];
  if (svcs.length) parts.push(`${up}/${svcs.length} services up`);
  return parts.join(' · ');
}

// The wiring: what a link is actually connected to, and whether it is running.
// Built from the CDC stack's parameters, so it is the durable topology rather
// than anything this process happened to remember.
function Wiring({ components }) {
  if (!components) return null;
  const { source = {}, broker = {}, target = {}, streams = [], runtime = {} } =
    components;
  const services = runtime.services || [];
  return (
    <section className="lk-wiring">
      <h3>components</h3>

      <div className="lk-flow">
        <div className="lk-flow-node">
          <div className="lk-flow-role">source</div>
          <div className="lk-flow-name">{source.engine || 'database'}</div>
          <div className="lk-muted lk-mono">{source.host}</div>
        </div>
        <div className="lk-flow-arrow">→</div>
        <div className="lk-flow-node">
          <div className="lk-flow-role">broker</div>
          <div className="lk-flow-name">Valkey Streams</div>
          <div className="lk-muted lk-mono">{broker.host}</div>
        </div>
        <div className="lk-flow-arrow">→</div>
        <div className="lk-flow-node">
          <div className="lk-flow-role">target</div>
          <div className="lk-flow-name">
            Valkey{isTrue(target.cluster_mode) ? ' (cluster)' : ''}
          </div>
          <div className="lk-muted lk-mono">{target.host}</div>
        </div>
      </div>

      <div className="lk-wiring-grid">
        <div className="lk-outblock">
          <h4>source</h4>
          <table className="lk-table"><tbody>
            <tr><th>engine</th><td>{source.engine}</td></tr>
            <tr><th>host</th><td className="lk-mono">{source.host}</td></tr>
            <tr><th>port</th><td>{source.port}</td></tr>
            <tr><th>database</th><td>{source.database}</td></tr>
            <tr><th>user</th><td>{source.user}</td></tr>
            <tr><th>tables</th><td>{source.tables}</td></tr>
          </tbody></table>
        </div>
        <div className="lk-outblock">
          <h4>broker</h4>
          <table className="lk-table"><tbody>
            <tr><th>host</th><td className="lk-mono">{broker.host}</td></tr>
            <tr><th>port</th><td>{broker.port} · {tlsLabel(broker.tls)}</td></tr>
            <tr><th>offset key</th><td className="lk-mono">{broker.offset_key}</td></tr>
            <tr>
              <th>schema history</th>
              <td className="lk-mono">{broker.schema_history_key}</td>
            </tr>
          </tbody></table>
        </div>
        <div className="lk-outblock">
          <h4>target</h4>
          <table className="lk-table"><tbody>
            <tr><th>host</th><td className="lk-mono">{target.host}</td></tr>
            <tr><th>port</th><td>{target.port} · {tlsLabel(target.tls)}</td></tr>
            <tr>
              <th>cluster mode</th>
              <td>{isTrue(target.cluster_mode) ? 'on' : 'off'}</td>
            </tr>
          </tbody></table>
        </div>
      </div>

      {streams.length > 0 && (
        <div className="lk-outblock">
          <h4>streams</h4>
          <table className="lk-table lk-tables">
            <thead><tr><th>stream</th><th>dead-letter queue</th></tr></thead>
            <tbody>
              {streams.map((s) => (
                <tr key={s.stream}>
                  <td className="lk-mono">{s.stream}</td>
                  <td className="lk-mono">{s.dlq}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(services.length > 0 || runtime.cluster) && (
        <div className="lk-outblock">
          <h4>
            running services{runtime.cluster ? ` · cluster ${runtime.cluster}` : ''}
          </h4>
          {services.length > 0 ? (
            <table className="lk-table lk-tables">
              <thead>
                <tr><th>service</th><th>state</th><th>running / desired</th></tr>
              </thead>
              <tbody>
                {services.map((svc) => (
                  <tr key={svc.name}>
                    <td className="lk-mono">{svc.name}</td>
                    <td>{svc.status}</td>
                    <td><ServiceChip svc={svc} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="lk-muted">service status unavailable</div>
          )}
          {runtime.sink_scaling && (
            <div className="lk-hint">sink: {runtime.sink_scaling}</div>
          )}
        </div>
      )}
    </section>
  );
}


// Runs grouped by pipeline, newest run first within each, groups ordered by
// their latest run. A discovered pipeline (no tracked run) is a group of one.
export function groupByPipeline(items) {
  const byLink = new Map();
  items.forEach((d) => {
    const key = d.link || d.id;
    if (!byLink.has(key)) byLink.set(key, []);
    byLink.get(key).push(d);
  });
  const groups = Array.from(byLink.values()).map((runs) => {
    const sorted = runs.slice().sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
    return { latest: sorted[0], runs: sorted };
  });
  return groups.sort((a, b) => (b.latest.started_at || 0) - (a.latest.started_at || 0));
}

function LinkDeployments({ onWatch }) {
  const [items, setItems] = useState(null);
  const [showLogs, setShowLogs] = useState(false);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState(null);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [showDeleted, setShowDeleted] = useState(false);
  // True when the newest refresh failed. The last good list stays on screen,
  // marked as stale: replacing it with an empty one read as "No pipelines"
  // whenever the API was unreachable.
  const [stale, setStale] = useState(false);
  const loadSeqRef = useRef(0);
  const loadShownRef = useRef(0);

  const load = useCallback(async () => {
    loadSeqRef.current += 1;
    const seq = loadSeqRef.current;
    try {
      const res = await axios.get(`${API_URL}/api/links/deployments`,
                                  { timeout: 20000 });
      // The poll and Refresh can overlap; an older answer must not win.
      if (seq < loadShownRef.current) return;
      loadShownRef.current = seq;
      setItems(res.data.deployments || []);
      setError(null);
      setStale(false);
    } catch (e) {
      if (seq < loadShownRef.current) return;
      loadShownRef.current = seq;
      const d = e.response && e.response.data;
      setError(d && d.reason ? `${d.error} — ${d.reason}`
                             : (e.message || 'could not load deployments'));
      setStale(true);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  // Poll the open one too, so a running deployment updates without any action.
  // Only answers for the current selection, newest request first, may set the
  // detail: a slow answer for the pipeline selected before used to land after
  // the new one and show it under the new selection.
  useEffect(() => {
    if (!selected) return undefined;
    let cancelled = false;
    let sent = 0;
    let shown = 0;
    const tick = async () => {
      sent += 1;
      const seq = sent;
      try {
        const res = await axios.get(
          `${API_URL}/api/links/deployments/${selected}`, { timeout: 20000 });
        if (cancelled || seq < shown) return;
        shown = seq;
        setDetail(res.data);
        setDetailError(null);
      } catch (e) {
        // A temporary failure should not clear the view. Said only while there
        // is nothing to show, so "Loading" does not stand in for a failure.
        if (!cancelled && seq >= shown) setDetailError(e.message || 'request failed');
      }
    };
    tick();
    const t = setInterval(tick, POLL_MS);
    return () => { cancelled = true; clearInterval(t); };
  }, [selected]);

  // A new selection clears the detail at once, so the pane never shows one
  // pipeline under another's row while the new one loads.
  const select = (id) => {
    if (id !== selected) { setDetail(null); setDetailError(null); }
    setSelected(id);
    setEditing(false);
    setDeleting(false);
  };

  const running = (items || []).filter((d) => d.status === 'running').length;
  // One row per PIPELINE, showing its latest run. The API returns every run
  // (each deploy, reload and delete is one), so a pipeline deployed twice used
  // to appear twice and read as two pipelines. Older runs are listed in the
  // detail pane. Pipelines that have since been deleted are hidden behind a
  // toggle rather than sitting among the live ones.
  const groups = groupByPipeline(items || []);
  const liveGroups = groups.filter((g) => !g.latest.gone);
  const deletedGroups = groups.filter((g) => g.latest.gone);
  const shown = showDeleted ? groups : liveGroups;
  const runsOfSelected = (groups.find((g) => g.runs.some((r) => r.id === selected))
                          || { runs: [] }).runs;
  // A record of a run whose pipeline has since been deleted (here or by the CLI).
  const gone = Boolean(detail && (items || []).some((d) => d.id === detail.id && d.gone));
  const live = detail && detail.kind !== 'destroy' && !gone;
  // Jira story F135. The list marks a tracked run of a link whose capture is held at 0
  // as paused, while the detail endpoint keeps what the run itself did
  // (succeeded). Take the list's word for the status, so the row and the
  // detail never disagree, and keep the run's own result beside it.
  const listed = detail && (items || []).find((d) => d.id === detail.id);
  const listedPaused = Boolean(listed && listed.status === 'paused');
  const detailStatus = detail && (listedPaused ? 'paused' : detail.status);
  const pausedNote = detail && (detail.capture_paused_note
    || (listedPaused ? listed.capture_paused_note : null));

  return (
    <div className="lk">
      <div className="lk-head">
        <h1>Deployments</h1>
        <p>
          Every pipeline, with its latest run and how far it got. Pipelines are
          found from their CloudFormation stacks, so one deployed from the CLI is
          listed too; earlier runs of a pipeline are in its detail.
        </p>
      </div>

      {error && <div className="lk-apierror">{error}</div>}

      <div className="lk-split">
        <div className="lk-pane">
          <div className="lk-panehead">
            <span>
              {items === null ? (stale ? 'could not load the list' : 'loading…')
                : `${liveGroups.length} pipeline(s)${running ? `, ${running} running` : ''}`}
            </span>
            <button className="lk-mini" onClick={load}>Refresh</button>
          </div>

          {stale && items !== null && (
            <div className="lk-hint lk-stalelist">
              Could not refresh: this is the list from the last successful read.
            </div>
          )}

          {items && liveGroups.length === 0 && !stale && (
            <div className="lk-empty">
              No pipelines. Create one on the Build Pipeline page.
            </div>
          )}

          {shown.map(({ latest: d, runs }) => (
            <button
              key={d.id}
              className={`lk-deprow ${runs.some((r) => r.id === selected) ? 'active' : ''}`}
              onClick={() => select(d.id)}
            >
              <div className="lk-deprow-top">
                <strong>{d.link}</strong>
                <StatusChip status={d.status} />
              </div>
              {d.gone && d.kind !== 'destroy' && (
                <div className="lk-muted">deleted since: no stacks are left</div>
              )}
              <div className="lk-muted">
                {d.mode ? `${d.mode} · ` : ''}{(d.completed || []).length} of{' '}
                {(d.steps || []).length} step(s)
              </div>
              {rowSummary(d) && <div className="lk-muted">{rowSummary(d)}</div>}
              {d.initial_load_incomplete && (
                <div className="lk-bad">initial load incomplete</div>
              )}
              {d.error && <div className="lk-bad">{d.error}</div>}
              {runs.length > 1 && (
                <div className="lk-muted">{runs.length} runs (latest shown)</div>
              )}
            </button>
          ))}

          {deletedGroups.length > 0 && (
            <button className="lk-mini lk-showdeleted"
                    onClick={() => setShowDeleted((v) => !v)}>
              {showDeleted ? 'Hide deleted pipelines'
                : `Show ${deletedGroups.length} deleted pipeline(s)`}
            </button>
          )}
        </div>

        <div className="lk-pane lk-results">
          {!detail && (
            <div className="lk-empty">
              {!selected && 'Select a deployment to see its steps and events.'}
              {selected && !detailError && 'Loading\u2026'}
              {selected && detailError && `Could not load ${selected}: ${detailError}`}
            </div>
          )}
          {detail && editing && (
            <LinkEditor link={detail.link} onClose={() => setEditing(false)} />
          )}
          {detail && deleting && (
            <DeletePipeline
              link={detail.link}
              onCancel={() => setDeleting(false)}
              onStarted={(destroyId) => {
                // Follow the delete like a deploy: it is a tracked run with
                // steps (cdc, loader, broker) and events.
                setDeleting(false);
                setDetail(null);
                setSelected(destroyId);
                load();
              }}
            />
          )}
          {detail && !editing && !deleting && (
            <section>
              <div className="lk-detailhead">
                <h2>{detail.link}</h2>
                {/* Change a running pipeline right here, next to what it is wired
                    to. Creating a new one is a different job and lives on the
                    Build Pipeline page. */}
                {live && (
                  <button className="lk-mini lk-primary-mini"
                          onClick={() => setEditing(true)}>
                    Edit this pipeline
                  </button>
                )}
                {/* The question after every deploy is "is it actually working",
                    and the answer lives on another page. Before this you had to
                    switch pages and re-find the link in a dropdown. */}
                {onWatch && live && (
                  <button className="lk-mini" onClick={() => onWatch(detail.link)}>
                    Watch this pipeline
                  </button>
                )}
                <button className="lk-mini" onClick={() => setShowLogs((s) => !s)}>
                  {showLogs ? 'Hide component logs' : 'Show component logs'}
                </button>
                {live && (
                  <button className="lk-mini lk-danger-mini"
                          onClick={() => setDeleting(true)}>
                    Delete pipeline
                  </button>
                )}
              </div>
              {gone && detail.kind !== 'destroy' && (
                <div className="lk-hint">
                  This pipeline has been deleted: none of its stacks exist any more.
                  This is the record of an earlier run.
                </div>
              )}
              <table className="lk-table">
                <tbody>
                  <tr><th>id</th><td>{detail.id}</td></tr>
                  {detail.mode && <tr><th>mode</th><td>{detail.mode}</td></tr>}
                  <tr>
                    <th>status</th>
                    <td><StatusChip status={detailStatus} /></td>
                  </tr>
                  {listedPaused && listed.run_status && (
                    <tr>
                      <th>this run</th>
                      <td><StatusChip status={listed.run_status} /></td>
                    </tr>
                  )}
                </tbody>
              </table>
              {detailStatus === 'paused' && pausedNote && (
                <div className="lk-bad lk-capturepaused">{pausedNote}</div>
              )}

              {detail.initial_load_incomplete && (
                <div className="lk-bad lk-blocked">{detail.initial_load_note}</div>
              )}
              {detail.error && (
                <div className="lk-bad lk-blocked">
                  {detail.failed_step ? <strong>{detail.failed_step}: </strong> : null}
                  {detail.error}
                  {detail.hint && <div className="lk-hint">{detail.hint}</div>}
                </div>
              )}

              {/* The wiring goes first: after a deploy the question is "what is
                  this connected to, and is it running", not the step order. */}
              <Wiring components={detail.components} />

              {runsOfSelected.length > 1 && (
                <>
                  <h3>runs of this pipeline</h3>
                  <table className="lk-table">
                    <tbody>
                      {runsOfSelected.map((r) => (
                        <tr key={r.id}>
                          <td>
                            <button className="lk-linkbtn" onClick={() => select(r.id)}>
                              {r.kind === 'destroy' ? 'delete' : (r.for_update ? 'update' : 'deploy')}
                            </button>
                          </td>
                          <td><StatusChip status={r.status} /></td>
                          <td className="lk-muted">
                            {r.started_at ? new Date(r.started_at).toLocaleString() : ''}
                          </td>
                          <td>{r.id === detail.id ? 'shown' : ''}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}

              {detail.kind === 'destroy' && detail.status === 'succeeded' && (
                <>
                  <h3>deleted</h3>
                  <p>
                    {(detail.deleted || []).join(', ') || 'nothing (no stacks were left)'}
                    {detail.cluster_deleted ? ', and its ECS cluster' : ''}.
                    Your source database and target cache were not touched.
                  </p>
                  <h3>left for you</h3>
                  <FollowUps followUp={detail.follow_up} link={detail.link} />
                </>
              )}

              <h3>steps</h3>
              <DeploymentSteps deployment={detail} />
              {detail.note && <div className="lk-hint">{detail.note}</div>}

              {detail.outputs && Object.keys(detail.outputs).length > 0 && (
                <>
                  <h3>stack outputs</h3>
                  {/* Values, not just key names. This listed only the KEYS, which
                      made the section useless: the endpoint the broker was
                      created at, and the secret ARN the tasks read, are the whole
                      reason to look. */}
                  {Object.entries(detail.outputs).map(([stack, outs]) => (
                    <div key={stack} className="lk-outblock">
                      <h4>{stack}</h4>
                      {outs && typeof outs === 'object' ? (
                        <table className="lk-table">
                          <tbody>
                            {Object.entries(outs).map(([key, value]) => (
                              <tr key={key}>
                                <th>{key}</th>
                                <td className="lk-mono">{String(value)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      ) : <div className="lk-mono">{String(outs)}</div>}
                    </div>
                  ))}
                </>
              )}

              {(detail.events || []).length > 0 && (
                <DeploymentEvents events={detail.events} />
              )}

              {/* Opt-in: reading logs costs a CloudWatch call, and most visits to
                  this page are about a deploy that worked. Opened automatically
                  is wrong; one action away is right. */}
              {showLogs && (
                <ComponentLogs apiUrl={API_URL} link={detail.link} />
              )}
            </section>
          )}
        </div>
      </div>
    </div>
  );
}

export default LinkDeployments;
