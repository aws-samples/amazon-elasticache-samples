// ═══════════════════════════════════════════════════════════════════════════
// How a deployment is displayed — shared by the Links run history and the
// Deployments page.
//
// It lives here because it was written twice. Links.js grew a four-state step
// list that says what the running step is waiting on and for how long, and
// collapses repeated polls; LinkDeployments.js kept its original done/not-done
// list and a raw event dump. So the same deployment looked like progress on one
// page and like a hundred identical "stack_waiting" lines on the other,
// depending on which one you happened to open.
//
// Two surfaces showing one thing differently is the same failure as the
// file-versus-form split this project set out to remove, so the display is a
// shared module rather than code repeated on each page.
// ═══════════════════════════════════════════════════════════════════════════
import React from 'react';

export const STEP_LABEL = {
  done: 'done',
  running: 'in progress',
  pending: 'waiting for earlier steps',
  failed: 'failed',
};

// Four states, not two. Marking steps only done-or-not meant a deployment in
// progress showed six identical rows, with nothing to separate working from stuck.
export function stepState(deployment, sid) {
  if (deployment.failed_step === sid) return 'failed';
  if ((deployment.completed || []).includes(sid)) return 'done';
  if (deployment.status !== 'running') return 'pending';
  // The executor runs steps strictly in order, so the running one is the
  // first not yet completed. No extra reporting from the server is needed.
  const next = (deployment.steps || []).find(
    (s) => !(deployment.completed || []).includes(s));
  return next === sid ? 'running' : 'pending';
}

export function humanDuration(seconds) {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

// What the running step is doing, from the newest event about it, plus how long it
// has been running. "CREATE_IN_PROGRESS for 4m" is the difference between a
// deployment that is working and one that is stuck.
export function currentStepDetail(deployment, sid) {
  const mine = (deployment.events || []).filter((e) => e.step === sid);
  if (!mine.length) return null;
  const last = mine[mine.length - 1];
  const first = mine[0];
  const waits = mine.filter((e) => e.event === 'stack_waiting').length;

  const parts = [];
  if (last.stack) parts.push(last.stack);
  if (last.state) parts.push(last.state);
  if (last.event === 'service_waiting') {
    parts.push(`${last.running || 0} of ${last.desired} task(s) running`);
  }
  if (last.event === 'load_waiting') parts.push(`loading ${last.table}`);
  if (first.at && last.at) {
    const seconds = Math.round((last.at - first.at) / 1000);
    if (seconds >= 5) parts.push(`for ${humanDuration(seconds)}`);
  } else if (waits > 1) {
    parts.push(`checked ${waits} times`);
  }
  // Setting the expectation, so a long wait does not read as a hang. The broker
  // is an ElastiCache replication group and there is nothing to be done about how
  // long that takes.
  if (sid === 'broker' && last.state === 'CREATE_IN_PROGRESS') {
    parts.push('— ElastiCache usually takes 5 to 10 minutes');
  }
  return parts.join(' · ');
}

// Repeated polls collapse into one line with a count. A hundred identical
// "stack_waiting" entries say nothing that "waited 100 times" does not, and they
// push everything informative off the top of the log.
export function summariseEvents(events) {
  const out = [];
  (events || []).forEach((e) => {
    const bits = [e.event];
    if (e.step) bits.push(`step=${e.step}`);
    if (e.stack) bits.push(`stack=${e.stack}`);
    if (e.state) bits.push(`state=${e.state}`);
    if (e.table) bits.push(`table=${e.table}`);
    if (e.service) bits.push(`service=${e.service}`);
    if (e.action) bits.push(`action=${e.action}`);
    if (e.desired !== undefined) bits.push(`desired=${e.desired}`);
    if (e.running !== undefined) bits.push(`running=${e.running}`);
    if (e.reason) bits.push(`reason=${e.reason}`);
    const line = bits.join(' ');
    const previous = out[out.length - 1];
    if (previous && previous.line === line) {
      previous.count += 1;
    } else {
      out.push({ line, count: 1 });
    }
  });
  return out.map((entry) => (entry.count > 1
    ? `${entry.line}   (x${entry.count})`
    : entry.line));
}

export function DeploymentSteps({ deployment }) {
  return (
    <ol className="lk-steps lk-progress">
      {(deployment.steps || []).map((sid) => {
        const state = stepState(deployment, sid);
        const detail = state === 'running'
          ? currentStepDetail(deployment, sid) : null;
        return (
          <li key={sid} className={state}>
            <span className="lk-dot" />
            <span className="lk-stepname">{sid}</span>
            <span className="lk-stepstate">{STEP_LABEL[state]}</span>
            {detail && <div className="lk-stepdetail">{detail}</div>}
          </li>
        );
      })}
    </ol>
  );
}

export function DeploymentEvents({ events }) {
  const lines = summariseEvents(events);
  if (!lines.length) return null;
  return (
    <details className="lk-events">
      <summary>{lines.length} activity entries</summary>
      <pre>{lines.join('\n')}</pre>
    </details>
  );
}
