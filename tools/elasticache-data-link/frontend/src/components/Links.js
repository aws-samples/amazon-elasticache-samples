// ═══════════════════════════════════════════════════════════════════════════
// Links — define, review and deploy a data link.
//
// WHY THIS IS AN EDITOR AND NOT A FORM OF ITS OWN
// The requirement is that the UI and the config file accept the same thing. The
// surest way to get that is for the UI to edit THE CONFIG ITSELF, and for every
// judgement about it -- parsing, validation, resolution, planning -- to be made
// by the same code the CLI calls. So this page holds the text of a link file and
// sends it to /api/links/*; it never re-implements a rule locally. There is no
// YAML parser in here on purpose: two parsers can disagree about the same bytes,
// and that is exactly the file-versus-form split this is meant to remove.
//
// The four buttons are the four things you can do to a link, in the order you do
// them, and each one shows what it found rather than a spinner and a verdict:
//   Check    parse + validate            (no AWS calls)
//   Resolve  read the referenced ARNs    (read-only)
//   Plan     the ordered deployment      (no changes)
//   Deploy   run it                      (dry run available)
// ═══════════════════════════════════════════════════════════════════════════
import React, { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';
import LinkWizard from './LinkWizard';
// Presentational pieces shared with the pipeline editor (LinkEditor), so the two
// render a diff and a problem list identically.
import { Problems } from './linkViews';
// One implementation of how a deployment looks, shared with the
// Deployments page. It was written twice and the two drifted.
import { DeploymentEvents, DeploymentSteps } from './deploymentView';
// The token is authClient's business, not this component's. Asking it rather
// than reading sessionStorage keeps one place that knows where a token lives.
import { getToken, onTokenChange } from '../authClient';
import './Links.css';
import API_URL from '../apiUrl';


const EXAMPLE = `schema_version: 1
name: test2
mode: initial_load_and_cdc

source:
  arn: arn:aws:rds:us-east-2:111122223333:cluster:ecdl-demo-source-dbcluster-xxxxxxxxxxxx
  database: Inventory
  secret_arn: arn:aws:secretsmanager:us-east-2:111122223333:secret:my-db-secret

target:
  arn: arn:aws:elasticache:us-east-2:111122223333:replicationgroup:ecdl-test2-target

tables:
  - test2_orders
  - test2_items

transforms:
  tables:
    test2_orders:
      filter:
        field: status
        op: '='
        value: SHIPPED
      computed:
        - name: amount_cents
          expr: amount * 100
`;

// A deployment is minutes long, so polling must continue for a long time but not run too often.
const POLL_MS = 4000;

function Rows({ rows }) {
  return (
    <table className="lk-table">
      <tbody>
        {rows.map(([k, v], i) => (
          <tr key={i}>
            <th>{k}</th>
            <td>{v === null || v === undefined || v === '' ? '—' : String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ── result detail, one component per kind of run ────────────────────────────
//
// These hold the SAME JSX the page rendered inline. Moved, not rewritten: this
// change is about which results appear together, and altering what a result says
// at the same time would make a layout regression indistinguishable from a
// content one.

function ResolvedDetail({ data: resolution }) {
  const res = resolution && resolution.resolution;
  const egress = res && res.network && res.network.egress;
  return (
    <section>
      <h2>Resolved</h2>
      {!resolution.resolved && (
        <div className="lk-bad">
          {resolution.stage}: {resolution.error || 'could not resolve'}
          {resolution.hint && <div className="lk-hint">{resolution.hint}</div>}
          <Problems problems={resolution.problems} />
        </div>
      )}
      {resolution.resolved && res && (
        <>
          <Rows rows={[
            ['source', `${res.source.engine_label || res.source.engine} `
                       + `${res.source.engine_version || ''}`],
            ['source endpoint',
             `${res.source.endpoint}:${res.source.port}`],
            ['source user', res.source.username],
            ['credential', res.source.secret
              ? `${res.source.secret.name} (value never read here)`
              : null],
            ['target endpoint',
             `${res.target.endpoint}:${res.target.port}`],
            ['target', `${res.target.node_type}, `
                       + `${res.target.shards} shard(s)`],
            ['cluster mode', res.target.cluster_mode
              ? 'enabled' : 'disabled'],
            ['target TLS', res.target.tls ? 'required' : 'off'],
            ['target auth', res.target.auth_mode],
            ['VPC', res.network.vpc_id],
            ['subnets', (res.network.subnet_ids || []).join(', ')],
          ]} />

          {egress && (
            <div className={`lk-egress lk-egress-${egress.kind}`}>
              <strong>task egress: {egress.kind}</strong>
              <div>{egress.detail}</div>
              {(egress.missing_endpoints || []).length > 0 && (
                <div className="lk-muted">
                  missing interface endpoints:{' '}
                  {egress.missing_endpoints.join(', ')}
                </div>
              )}
            </div>
          )}

          <h3>tables</h3>
          <table className="lk-table lk-tables">
            <thead>
              <tr><th>table</th><th>stream</th><th>keys</th></tr>
            </thead>
            <tbody>
              {(res.tables || []).map((t) => (
                <tr key={t.qualified}>
                  <td>{t.qualified}</td>
                  <td><code>{t.stream}</code></td>
                  <td><code>{t.key_prefix}*</code></td>
                </tr>
              ))}
            </tbody>
          </table>

          {(res.warnings || []).length > 0 && (
            <div className="lk-warnings">
              {res.warnings.map((w, i) => <div key={i}>! {w}</div>)}
            </div>
          )}
          {(res.notes || []).length > 0 && (
            <div className="lk-notes">
              {res.notes.map((n, i) => <div key={i}>– {n}</div>)}
            </div>
          )}

          {/* The API returns the same summary text the CLI prints. Showing
              it verbatim is the simplest way to make sure that the two
              surfaces are describing the deployment identically. */}
          {resolution.summary && (
            <details className="lk-events">
              <summary>full summary (identical to the CLI output)</summary>
              <pre>{resolution.summary}</pre>
            </details>
          )}
        </>
      )}
    </section>
  );
}

// What the checks before deploying found: a table or column missing on the
// source, another pipeline already writing these keys into the cache, and the
// like. The API returned these with every plan, but nothing on this page showed
// them, so a plan that Deploy would refuse looked exactly like one it would run.
function PreflightFindings({ preflight }) {
  const findings = (preflight && preflight.findings) || [];
  if (!findings.length) return null;
  return (
    <ul className="lk-findings">
      {findings.map((f, i) => (
        <li key={i} className={`lk-finding lk-finding-${f.level}`}>
          <strong>{f.level === 'block' ? 'Will not deploy' : 'Note'}</strong>
          {f.path ? <code>{f.path}</code> : null}
          <span>{f.message}</span>
        </li>
      ))}
    </ul>
  );
}

function PlanDetail({ data: plan }) {
  const planDoc = plan && (plan.plan || plan);
  const steps = (planDoc && planDoc.steps) || [];
  if (!planDoc || !steps.length) {
    return <div className="lk-empty">{(plan && plan.error) || 'No steps.'}</div>;
  }
  return (
    <section>
      <h2>
        Plan{plan.dry_run ? ' (dry run — nothing was created)' : ''}
      </h2>
      <div className="lk-muted">
        ECS cluster {planDoc.cluster_name} · {steps.length} step(s)
      </div>
      <PreflightFindings preflight={plan.preflight} />
      <ol className="lk-steps">
        {steps.map((s) => (
          <li key={s.id}>
            <span className="lk-kind">{s.kind}</span>
            <strong>{s.id}</strong>
            {s.name && <code>{s.name}</code>}
            {s.template && <span className="lk-muted"> {s.template}</span>}
            {s.purpose && <div className="lk-muted">{s.purpose}</div>}
            {s.parameters && (
              <div className="lk-wire">
                {Object.keys(s.parameters).length} parameter(s)
                {(s.capabilities || []).length > 0
                  && `, ${s.capabilities.join(', ')}`}
              </div>
            )}
            {s.from_outputs && Object.keys(s.from_outputs).length > 0 && (
              <div className="lk-wire">
                wired from earlier steps:{' '}
                {Object.entries(s.from_outputs).map(([k, v]) => (
                  // Each value is [stepId, outputName]: these values do
                  // not exist until that step finishes, which is why the
                  // plan is an ordered list and not a flat parameter map.
                  `${k}←${Array.isArray(v) ? v.join('.') : v}`
                )).join(', ')}
              </div>
            )}
          </li>
        ))}
      </ol>
      {(planDoc.warnings || []).length > 0 && (
        <div className="lk-warnings">
          {planDoc.warnings.map((w, i) => <div key={i}>! {w}</div>)}
        </div>
      )}
      {(planDoc.notes || []).length > 0 && (
        <div className="lk-notes">
          {planDoc.notes.map((n, i) => <div key={i}>– {n}</div>)}
        </div>
      )}
      {plan.summary && (
        <details className="lk-events">
          <summary>full plan (identical to the CLI output)</summary>
          <pre>{plan.summary}</pre>
        </details>
      )}
    </section>
  );
}

function DeploymentDetail({ data: deployment }) {
  if (!deployment || !deployment.deployment_id) {
    return <div className="lk-empty">No deployment record.</div>;
  }
  return (
    <section>
      <h2>Deployment</h2>
      <Rows rows={[
        ['link', deployment.link],
        ['status', deployment.status],
        ['id', deployment.deployment_id],
      ]} />
      {deployment.capture_paused && deployment.status !== 'running' && (
        <div className="lk-bad lk-capturepaused">
          <strong>Capture is paused.</strong> Changes on the source do not reach
          the cache until it is resumed. The note below says how.
        </div>
      )}
      {deployment.error && (
        <div className="lk-bad">
          {deployment.failed_step ? `${deployment.failed_step}: ` : ''}
          {deployment.error}
          {deployment.hint && <div className="lk-hint">{deployment.hint}</div>}
        </div>
      )}

      {/* Each step says which of four states it is in, and the one in progress
          says what it is waiting on and for how long. The previous version marked
          steps done or not-done, so a deployment in progress showed six identical
          rows and a log of six identical "stack_waiting" lines -- no way to tell
          progress from a stall, and no hint that a five-to-ten minute wait on
          ElastiCache is normal. */}
      <DeploymentSteps deployment={deployment} />

      <DeploymentEvents events={deployment.events} />
    </section>
  );
}

// The run-history line for a deployment. Only `succeeded` is done: an
// `interrupted` run (the control plane restarted mid-deploy) used to read "all N
// steps done". And a run that stopped with capture paused says so in the line,
// because until capture is resumed no change on the source reaches the cache.
export function deployRunState(data) {
  const d = data || {};
  const done = (d.completed || []).length;
  const total = (d.steps || []).length;
  let status = 'bad';
  let headline;
  if (d.status === 'running') {
    status = 'running';
    headline = `step ${Math.min(done + 1, total)} of ${total}`;
  } else if (d.status === 'succeeded') {
    status = 'ok';
    headline = `all ${total} steps done`;
  } else if (d.status === 'failed') {
    headline = `failed at ${d.failed_step || 'an early step'}`;
  } else if (d.status === 'interrupted') {
    headline = `interrupted after ${done} of ${total} steps`;
  } else {
    headline = `${d.status || 'unknown'}: ${done} of ${total} steps done`;
  }
  if (d.capture_paused && d.status !== 'running') {
    headline += ' -- capture left paused';
  }
  return { status, headline };
}

const RUN_KINDS = {
  resolve: { label: 'Resolve', Detail: ResolvedDetail },
  plan: { label: 'Plan', Detail: PlanDetail },
  deploy: { label: 'Deploy', Detail: DeploymentDetail },
};

function runTime(at) {
  try {
    return new Date(at).toLocaleTimeString();
  } catch (e) {
    return '';
  }
}

// One row per run, newest first, each stamped with the link it was for and the
// time it ran. The pane used to hold one panel per KIND and always show the
// latest of each, so a Resolve from ten minutes ago sat directly above a fresh
// Deployment with nothing saying they described different configurations. That is
// how a stale credential gets read off the screen as current.
function Runs({ runs, expanded, onToggle }) {
  if (!runs.length) {
    return (
      <div className="lk-empty">Nothing run yet.</div>
    );
  }
  return (
    <div className="lk-runs">
      {runs.map((run, index) => {
        const kind = RUN_KINDS[run.kind] || RUN_KINDS.resolve;
        const open = expanded === run.key;
        const stale = index > 0;
        const Detail = kind.Detail;
        return (
          <section key={run.key}
                   className={`lk-run lk-run-${run.status}${stale ? ' is-stale' : ''}`}>
            <button type="button" className="lk-runhead" aria-expanded={open}
                    onClick={() => onToggle(open ? null : run.key)}>
              <span className="lk-runmark">{open ? '\u25be' : '\u25b8'}</span>
              <span className="lk-runkind">{kind.label}</span>
              <span className="lk-runlink">{run.link || '(unnamed)'}</span>
              <span className={`lk-runstatus lk-runstatus-${run.status}`}>
                {run.headline}
              </span>
              <span className="lk-runtime">{runTime(run.at)}</span>
            </button>
            {open && stale && (
              <div className="lk-stalenote">
                An earlier run. Newer runs are above, and this one may describe a
                different configuration.
              </div>
            )}
            {open && <div className="lk-runbody"><Detail data={run.data} /></div>}
          </section>
        );
      })}
    </div>
  );
}

function Links() {
  const [text, setText] = useState(EXAMPLE);
  const [check, setCheck] = useState(null);
  const [busy, setBusy] = useState(null);
  // One ordered history instead of one panel per kind of result. Each entry
  // records WHEN it ran and WHICH link it was for, so an older result cannot be
  // read as the current state of things.
  const [runs, setRuns] = useState([]);
  const [expandedRun, setExpandedRun] = useState(null);
  const [deployment, setDeployment] = useState(null);
  const [apiError, setApiError] = useState(null);
  // The pipeline already exists. Not an error -- a question: update it,
  // or cancel. Held here so the answer can re-send the deploy with
  // confirm_redeploy set.
  const [redeployPrompt, setRedeployPrompt] = useState(null);
  const [deployArmed, setDeployArmed] = useState(false);
  const pollRef = useRef(null);

  // ── the guided form ─────────────────────────────────────────────────────
  // `view` chooses which view of the SAME link you are looking at. The document
  // is the source of truth; the text and the form are two views of it. The
  // wizard keeps a working copy so typing stays instant, and that copy is
  // rendered into `text` by the server -- never serialised here, because a YAML
  // writer in the browser would mean the file someone commits was written by a
  // different implementation from the one that reads it.
  const [view, setView] = useState('wizard');
  const [wizardDoc, setWizardDoc] = useState(null);
  const [authMissing, setAuthMissing] = useState(false);
  const formatRef = useRef(null);
  // Sequence numbers, so a slow response cannot replace a newer one. `wizardRev`
  // counts wizard edits; `formatted` is the server rendering of the edit with
  // that number (or its error). `parseSeqRef` numbers parse requests and only
  // the newest one may set `check`. `checkedText` is the text that `check`
  // describes. Deploy from Guided is only allowed when the newest edit has been
  // formatted, that text has been parsed, and nothing is in flight -- otherwise
  // the verdict on screen belongs to a different document from the one sent.
  const wizardSeqRef = useRef(0);
  const [wizardRev, setWizardRev] = useState(0);
  const [formatted, setFormatted] = useState({ seq: 0, text: null, error: null });
  const parseSeqRef = useRef(0);
  const [parsePending, setParsePending] = useState(true);
  const [checkedText, setCheckedText] = useState(null);

  // Whether discovery can work at all, so the wizard can say "paste the token"
  // rather than showing an empty list and letting someone conclude they have no
  // databases. Re-evaluated when the token changes, so pasting one clears the
  // warning without a reload.
  useEffect(() => {
    let cancelled = false;
    let serverRequires = false;

    const recompute = () => {
      if (!cancelled) setAuthMissing(serverRequires && !getToken());
    };

    axios.get(`${API_URL}/api/auth/status`, { timeout: 15000 })
      .then((res) => {
        serverRequires = Boolean(res.data && res.data.enabled && res.data.configured);
        recompute();
      })
      .catch(() => { /* discovery will report its own error if it cannot run */ });

    const unsubscribe = onTokenChange(recompute);
    return () => {
      cancelled = true;
      if (typeof unsubscribe === 'function') unsubscribe();
    };
  }, []);

  const openWizard = () => {
    // Seed from whatever the text currently says, so switching views never
    // silently drops an edit made on the other side. The seed IS the parsed
    // document, so it counts as already formatted: its verdict is `check`.
    const seeded = Boolean(check && check.parsed && check.document);
    const seq = wizardSeqRef.current + 1;
    wizardSeqRef.current = seq;
    setWizardRev(seq);
    setFormatted({ seq, text: seeded ? checkedText : null, error: null });
    setWizardDoc(seeded ? check.document : {});
    setView('wizard');
  };

  const onWizardChange = useCallback((next) => {
    setWizardDoc(next);
    const seq = wizardSeqRef.current + 1;
    wizardSeqRef.current = seq;
    setWizardRev(seq);
    // Debounced: a keystroke in the name field should not be a round trip.
    if (formatRef.current) clearTimeout(formatRef.current);
    formatRef.current = setTimeout(async () => {
      try {
        const res = await axios.post(`${API_URL}/api/links/format`,
                                     { link: next }, { timeout: 15000 });
        if (seq !== wizardSeqRef.current) return;     // a newer edit exists
        if (res.data && typeof res.data.text === 'string') {
          setFormatted({ seq, text: res.data.text, error: null });
          setText(res.data.text);
        } else {
          setFormatted({ seq, text: null, error: 'the server returned no text' });
        }
      } catch (e) {
        if (seq !== wizardSeqRef.current) return;
        const d = e.response && e.response.data;
        const msg = d && d.reason ? `${d.error} — ${d.reason}`
          : (e.message || 'could not render the config');
        setFormatted({ seq, text: null, error: msg });
        setApiError(msg);
      }
    }, 350);
  }, []);

  // Parse+validate as you type. Debounced, because it is a round trip per edit.
  useEffect(() => {
    const seq = parseSeqRef.current + 1;
    parseSeqRef.current = seq;
    setParsePending(true);
    const sent = text;
    const t = setTimeout(async () => {
      try {
        const res = await axios.post(`${API_URL}/api/links/parse`, { text: sent },
                                     { timeout: 15000 });
        if (seq !== parseSeqRef.current) return;      // a newer parse is due
        setCheck(res.data);
        setCheckedText(sent);
        setApiError(null);
      } catch (e) {
        if (seq !== parseSeqRef.current) return;
        // A 503 from the fallback blueprint carries the reason the API is
        // missing, which is far more useful than "request failed".
        const d = e.response && e.response.data;
        setApiError(d && d.reason
          ? `${d.error} — ${d.reason}${d.hint ? ` (${d.hint})` : ''}`
          : (e.message || 'request failed'));
        setCheck(null);
        setCheckedText(null);
      }
      setParsePending(false);
    }, 500);
    return () => clearTimeout(t);
  }, [text]);

  const document_ = check && check.parsed ? check.document : null;
  // File view: the verdict must be for the text on screen, not an earlier one.
  const canAct = Boolean(document_ && check.valid && !parsePending
                         && checkedText === text);
  // Guided view: also the newest edit must have been formatted into that text.
  const wizardSynced = Boolean(wizardDoc) && formatted.seq === wizardRev
    && !formatted.error && formatted.text !== null
    && formatted.text === checkedText && !parsePending;
  const wizardCanAct = Boolean(document_ && check.valid) && wizardSynced;
  let wizardNote = null;
  if (wizardDoc && !wizardSynced) {
    if (formatted.seq === wizardRev && formatted.error) {
      wizardNote = 'The latest change could not be checked, so these actions are '
        + `off: ${formatted.error}`;
    } else if (formatted.seq !== wizardRev || parsePending) {
      wizardNote = 'Checking the latest change\u2026';
    } else {
      // The parse failed; its reason is in the error banner above.
      wizardNote = 'The latest change could not be checked, so these actions are off.';
    }
  }

  const call = useCallback(async (what, path, body) => {
    setBusy(what);
    setApiError(null);
    try {
      const res = await axios.post(`${API_URL}/api/links/${path}`, body,
                                   { timeout: 120000 });
      return res.data;
    } catch (e) {
      const d = e.response && e.response.data;
      setApiError(d && (d.reason || d.error)
        ? `${d.error || 'failed'}${d.reason ? ` — ${d.reason}` : ''}`
        : (e.message || 'request failed'));
      return null;
    } finally {
      setBusy(null);
    }
  }, []);

  // Every action is added to the history the same way, so there is one place that
  // decides what a run looks like and no kind of result can quietly not appear.
  const addRun = useCallback((kind, data, { status, headline, link }) => {
    const key = `${kind}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    setRuns((prev) => [{
      key, kind, data, status, headline,
      link: link || (document_ && document_.name) || null,
      at: Date.now(),
    }, ...prev].slice(0, 25));
    setExpandedRun(key);
    return key;
  }, [document_]);

  // `doc` is passed by the Guided view: the document on its Review step. The
  // File view passes nothing and acts on the parsed text.
  const doResolve = async (doc) => {
    const sent = doc || document_;
    const data = await call('resolve', 'resolve', { link: sent });
    if (!data) return;
    addRun('resolve', data, {
      link: sent && sent.name,
      status: data.resolved ? 'ok' : 'bad',
      headline: data.resolved ? 'read the source, target and tables'
                              : `could not resolve (${data.stage || 'error'})`,
    });
  };

  const doPlan = async (doc) => {
    const sent = doc || document_;
    const data = await call('plan', 'plan', { link: sent });
    if (!data) return;
    const planned = data.plan || data;
    const count = ((planned && planned.steps) || []).length;
    const blocks = (data.preflight && data.preflight.blocks) || 0;
    addRun('plan', data, {
      link: sent && sent.name,
      status: count && !blocks ? 'ok' : 'bad',
      headline: !count ? (data.error || 'could not plan')
        : blocks ? `${blocks} problem(s): Deploy would be refused -- open for details`
          : `${count} steps, nothing created`,
    });
  };

  // Takes dry_run EXPLICITLY. It used to read the checkbox out of state and
  // ignore anything passed in, so the wizard's "yes, deploy it" quietly ran a dry
  // run and returned a plan -- a real deploy that silently did nothing. Two
  // things caused it: an argument that was accepted and dropped, and one side
  // calling the flag "real" while the other called it "dryRun", so the polarity
  // inverted on the way through. Named once, passed once, no default.
  // `reload`: on an already-deployed link, run the bulk load again. Without it a
  // redeploy only updates the stacks; capture keeps running and nothing reloads.
  // `doc`: the exact document to deploy. Guided passes its reviewed document;
  // the redeploy prompt passes the one that was refused, so "update" re-sends
  // what was asked for rather than whatever was parsed last.
  const doDeploy = async (dry, confirm, reload, doc) => {
    const link = doc || document_;
    // Its own request rather than call(): call() turns any non-2xx into the
    // error banner, but a 409 here is not an error -- it is "this link is already
    // deployed", which we turn into a confirm prompt, not a red message.
    setBusy('deploy');
    setApiError(null);
    if (!confirm) setRedeployPrompt(null);
    let data;
    try {
      const res = await axios.post(`${API_URL}/api/links/deploy`,
        { link, dry_run: Boolean(dry),
          confirm_redeploy: Boolean(confirm), reload: Boolean(reload) },
        { timeout: 120000 });
      data = res.data;
    } catch (e) {
      const d = e.response && e.response.data;
      if (e.response && e.response.status === 409 && d && d.already_deployed) {
        setRedeployPrompt({ ...d, document: link });
        setBusy(null);
        return;
      }
      // `message` too: the "a deploy is already running" 409 carries its reason
      // there, and without it the banner read "Request failed with status 409".
      setApiError((d && (d.error || d.reason || d.message)) || e.message
                  || 'request failed');
      setBusy(null);
      return;
    }
    setBusy(null);
    setRedeployPrompt(null);
    if (!data) return;
    if (data.dry_run) {
      const count = ((data.plan && data.plan.steps) || []).length;
      addRun('plan', data, {
        status: 'ok', headline: `dry run: ${count} steps, nothing created`,
      });
      return;
    }
    if (data.deployment_id) {
      addRun('deploy', data, {
        status: 'running', headline: 'running\u2026', link: data.link,
      });
      setDeployment(data);
      return;
    }
    // Anything else is a refusal that arrived as HTTP 200 -- this API reports
    // verdicts in the body rather than in the status, so a link that cannot be
    // planned, or credentials that cannot be built, come back 200 with
    // `deployed: false`. This branch used to be absent, so pressing Deploy and
    // getting a reasoned refusal looked exactly like pressing Deploy and nothing
    // happening at all. Never fail silently on the button that costs money.
    setApiError(
      [data.stage ? `${data.stage}:` : null,
       data.error || 'the control plane refused the deployment but gave no reason',
       data.hint].filter(Boolean).join(' '));
    if ((data.problems || []).length) setCheck((prev) => ({
      ...(prev || {}), problems: data.problems, parsed: true, valid: false,
    }));
  };

  // Poll a running deployment until it stops running.
  useEffect(() => {
    const id = deployment && deployment.deployment_id;
    if (!id) return undefined;
    const tick = async () => {
      try {
        const res = await axios.get(
          `${API_URL}/api/links/deployments/${id}`, { timeout: 20000 });
        setDeployment((prev) => ({ ...prev, ...res.data }));

        // Updated in place. A deployment runs for minutes, so its progress has to
        // appear where it already is rather than as a second thing on the page
        // that has to be reconciled with the first.
        // From the merged record, so an answer that leaves a field out keeps
        // what is already known rather than reading as an unknown status.
        setRuns((prev) => prev.map((run) => {
          if (!(run.kind === 'deploy' && run.data && run.data.deployment_id === id)) {
            return run;
          }
          const data = { ...run.data, ...res.data };
          return { ...run, ...deployRunState(data), data };
        }));

        if (res.data.status && res.data.status !== 'running') {
          clearInterval(pollRef.current);
        }
      } catch (e) { /* keep polling; a temporary failure should not stop the view */ }
    };
    tick();
    pollRef.current = setInterval(tick, POLL_MS);
    return () => clearInterval(pollRef.current);
  }, [deployment && deployment.deployment_id]);  // eslint-disable-line

  // These derivations used to live here for the inline result panels. They now
  // belong to the per-kind detail components, which each compute what they need
  // from the run they were handed -- so a component cannot accidentally render
  // one run's detail using another run's data.

  return (
    <div className="lk">
      <div className="lk-head">
        <h1>Build Pipeline</h1>
        <p>
          Pick a source, a credential, a target and some tables, and ECDL creates
          everything the pipeline needs: the broker, change capture, the sink, and
          the initial load. <strong>Guided</strong> and <strong>File</strong> are
          two views of the same link, so anything built here can be committed and
          deployed with <code>ecdl apply</code>, and every check on this page is
          run by the same code the CLI runs.
          {' '}To change a pipeline that is already running, use
          {' '}<strong>Edit this pipeline</strong> on the Deployments page.
        </p>
      </div>

      {apiError && <div className="lk-apierror">{apiError}</div>}

      {redeployPrompt && (
        <div className="lk-redeploy">
          <strong>This pipeline already exists</strong>
          <span>{redeployPrompt.message}</span>
          <span>
            Updating changes the stacks only: capture keeps running and the cache
            is not reloaded. Reload only if an earlier load failed.
          </span>
          <div className="lk-redeploy-actions">
            <button
              className="lk-danger"
              disabled={busy === 'deploy'}
              onClick={() => doDeploy(false, true, false, redeployPrompt.document)}
            >
              {busy === 'deploy' ? 'Working\u2026' : 'Update the existing pipeline'}
            </button>
            <button
              disabled={busy === 'deploy'}
              title="Pauses capture, bulk-loads every table again, then resumes capture"
              onClick={() => doDeploy(false, true, true, redeployPrompt.document)}
            >
              Update and reload all tables
            </button>
            <button onClick={() => setRedeployPrompt(null)}>Cancel</button>
          </div>
        </div>
      )}

      <div className="lk-split">
        <div className="lk-pane">
          <div className="lk-panehead">
            {/* Two views of one link, not two ways to make one: Guided assembles
                the document, File is that document as text. */}
            <div className="lk-viewtabs" role="tablist">
              <button
                role="tab"
                aria-selected={view === 'wizard'}
                className={view === 'wizard' ? 'is-on' : ''}
                onClick={openWizard}
              >Guided</button>
              <button
                role="tab"
                aria-selected={view === 'editor'}
                className={view === 'editor' ? 'is-on' : ''}
                onClick={() => setView('editor')}
              >File</button>
            </div>
            <div>
              {view === 'editor' && (
                <button className="lk-mini" onClick={() => setText(EXAMPLE)}>
                  Load example
                </button>
              )}
            </div>
          </div>

          {view === 'wizard' ? (
            <LinkWizard
              document={wizardDoc || {}}
              onChange={onWizardChange}
              onDone={() => setView('editor')}
              tokenMissing={authMissing}
              // The same handlers the File view uses, so both surfaces resolve,
              // plan and deploy through one code path.
              onResolve={() => doResolve(wizardDoc)}
              onPlan={() => doPlan(wizardDoc)}
              // An object, not a bare boolean: `onDeploy(false)` reads equally
              // well as "not a dry run" and "do not deploy", and that ambiguity
              // is exactly what inverted it last time.
              // Exactly the document shown on the Review step.
              onDeploy={({ dryRun: dry }) => doDeploy(dry, false, false, wizardDoc)}
              busy={busy}
              canAct={wizardCanAct}
              actNote={wizardNote}
            />
          ) : (
            <textarea
              className="lk-editor"
              spellCheck="false"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          )}

          {/* In Guided view an incomplete link is the NORMAL state -- it is
              incomplete for the whole time you are filling it in. Listing six
              "is required" problems against a form on its first step reads as
              something being broken, when the wizard's own per-step message is
              already saying precisely what to do next. So the file verdict is
              shown once the link is complete, or in full in File view. */}
          <div className="lk-status">
            {!check && <span className="lk-muted">checking…</span>}
            {check && !check.parsed && (
              <span className="lk-bad">
                {check.error}
                {check.line ? ` (line ${check.line}, column ${check.column})` : ''}
              </span>
            )}
            {check && check.parsed && check.valid && (
              <span className="lk-good">
                valid — {check.normalized && check.normalized.name} (
                {check.normalized && check.normalized.mode}),{' '}
                {check.normalized && (check.normalized.tables || []).length} table(s)
              </span>
            )}
            {check && check.parsed && !check.valid && view === 'editor' && (
              <span className="lk-bad">
                {check.problems.length} problem(s)
              </span>
            )}
            {check && check.parsed && !check.valid && view === 'wizard' && (
              <span className="lk-muted">
                {/* Name the fields. A count alone ("1 field(s) still to fill
                    in") left the deploy buttons greyed out with no way to tell
                    which field was meant. */}
                not finished yet:{' '}
                {check.problems.slice(0, 3)
                  .map((p) => `${p.path || 'link'} ${p.message || 'is invalid'}`)
                  .join('; ')}
                {check.problems.length > 3 ? ` (+${check.problems.length - 3} more)` : ''}
              </span>
            )}
          </div>
          {check && check.parsed && view === 'editor'
            && <Problems problems={check.problems} />}

          {/* File view keeps the compact row. In Guided view the same actions live
              on the Review step, next to the summary they act on -- one set of
              controls per view, rather than two rows of buttons where the reader
              has to work out which belongs to what. */}
          {view === 'editor' && (
            <div className="lk-actions">
              <button disabled={!canAct || busy} onClick={() => doResolve()}>
                {busy === 'resolve' ? 'Resolving…' : 'Resolve'}
              </button>
              <button disabled={!canAct || busy} onClick={() => doPlan()}>
                {busy === 'plan' ? 'Planning…' : 'Plan'}
              </button>
              {/* Deploy means deploy. This button used to default to a dry run
                  (a pre-ticked checkbox), so "Deploy" planned, reported the plan
                  in the side pane and created nothing -- which read as a deploy
                  that silently did nothing. Plan is the button for "show me";
                  Deploy asks once, the same way the Guided view does. */}
              {!deployArmed ? (
                <button
                  className="lk-danger"
                  disabled={!canAct || busy}
                  onClick={() => setDeployArmed(true)}
                >
                  {busy === 'deploy' ? 'Deploying…' : 'Deploy'}
                </button>
              ) : (
                <>
                  <span className="lk-confirm">
                    Creates real infrastructure and starts billing.
                  </span>
                  <button
                    className="lk-danger"
                    disabled={!canAct || Boolean(busy)}
                    // Wrapped: as a bare onClick handler it would receive the
                    // click event as its first argument.
                    onClick={() => { setDeployArmed(false); doDeploy(false); }}
                  >
                    Yes, deploy it
                  </button>
                  <button onClick={() => setDeployArmed(false)}>Cancel</button>
                </>
              )}
            </div>
          )}

        </div>

        <div className="lk-pane lk-results">
          <div className="lk-runshead">
            <span>runs</span>
            {runs.length > 1 && (
              <button className="lk-mini" onClick={() => setRuns([])}>
                Clear history
              </button>
            )}
          </div>
          <Runs runs={runs} expanded={expandedRun} onToggle={setExpandedRun} />
        </div>
      </div>
    </div>
  );
}

export default Links;
