import React, { useState, useEffect, useRef, useCallback } from 'react';
import axios from 'axios';
import ComponentLogs from './ComponentLogs';
import './PipelineMonitor.css';
import API_URL from '../apiUrl';

/*
 * Pipeline Monitor — one page, one card per component, in flow order:
 *   source Aurora -> Debezium Server -> broker stream -> ECS sink -> target Valkey
 *
 * Everything comes from a single GET /api/monitor/dashboard every POLL_MS. Rates
 * are already computed server-side, so this component only formats and draws.
 * Sparklines are plain SVG from a client-side ring buffer — no chart dependency.
 */

const POLL_MS = 2000;
const SPARK_SAMPLES = 60;

/* ---------- formatting ---------- */
const dash = '\u2014';

// Phase titles come from GET /api/monitor/phases so the backend and the page
// cannot drift; the tab strip only falls back to hardcoded names if that fails.

function num(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return dash;
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

function compact(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return dash;
  const n = Number(value);
  // Row and key counts here run into the millions, and "5427.0K" is harder to
  // read than "5.4M".
  if (Math.abs(n) >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (Math.abs(n) >= 10000) return (n / 1000).toFixed(1) + 'K';
  return num(Math.round(n));
}

function ms(value) {
  if (value === null || value === undefined) return dash;
  const n = Number(value);
  if (n >= 1000) return (n / 1000).toFixed(1) + ' s';
  // Precision matters at both ends: sub-10ms keeps 2 decimals so the
  // Aurora-vs-Valkey gap survives, and 10-100ms keeps 1 so DML 12.27 and
  // commit 11.73 do not both render as "12 ms".
  if (n < 10) return n.toFixed(2) + ' ms';
  if (n < 100) return n.toFixed(1) + ' ms';
  return Math.round(n) + ' ms';
}

/* Valkey command latency is microseconds — showing it in ms would round a
   3 µs HGETALL to "0.00 ms" and hide the difference being measured. */
function us(value) {
  if (value === null || value === undefined) return dash;
  const n = Number(value);
  if (n >= 1000) return (n / 1000).toFixed(2) + ' ms';
  return n.toFixed(2) + ' \u00b5s';
}

function pct(value) {
  return value === null || value === undefined ? dash : Number(value).toFixed(1) + '%';
}

function bytes(value) {
  if (value === null || value === undefined) return dash;
  const n = Number(value);
  if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return num(n) + ' B';
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return dash;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${Math.floor(seconds % 60)}s`;
  return `${Math.floor(seconds)}s`;
}

// 'paused' is a deliberate stop (ECS desiredCount 0), not a failure. It gets a
// neutral colour so a planned handoff step does not render as an outage.
// 'starting' is a task coming up after the handoff, so it shares the neutral
// treatment rather than going red for the two minutes Debezium takes to boot.
const stateClass = (s) => 'pm-s-' + (
  s === 'ok' ? 'ok'
    : s === 'warn' ? 'warn'
      : s === 'paused' ? 'paused'
        : s === 'starting' ? 'starting'
          // Not red. 'unreachable' means the control plane could not read the
          // component, which is a gap in our view rather than evidence of a
          // fault -- showing it as an outage marks healthy pipelines as failed.
          : s === 'unreachable' ? 'unknown' : 'bad');

/* "ok" as a status pill says nothing. Each verdict gets the words a reader
   actually needs: what is true right now. */
const VERDICT_WORDS = {
  ok: 'no loss',
  loss: 'entries lost',
  stalled: 'not draining',
  stuck: 'stuck',
  pressure: 'memory pressure',
  retrying: 'retrying',
};

/* What the pipeline should be doing in each phase.
 *
 * The DB-only phase is the one that confuses people: the tab says "DB only" but
 * CDC is clearly moving events. That is correct. Writes go to Aurora in every
 * phase, and ECDL replicates them regardless of which store answers reads — the
 * phase only moves the READ path. The one phase where CDC really should be
 * quiet is the bulk load, because that writes to Valkey directly. */
/* Only for phases where CDC lag is shown at all. Phases 1 and 2 no longer
 * display a lag figure, so they no longer need a paragraph explaining one. */
const CDC_CONTEXT = {
  3: 'Reads are served from Valkey. Writes still go to Aurora, and CDC is what keeps '
     + 'the cache current — so this lag is the freshness of what the app just read.',
};

/* The binlog gap has a non-zero baseline, so only its direction is meaningful:
   flat = lag is steady, rising = lag is growing. */
function trendArrow(perSec) {
  if (perSec === null || perSec === undefined) return '';
  if (perSec > 1000) return '\u25b2 growing';
  if (perSec < -1000) return '\u25bc catching up';
  return '\u2500 flat';
}

/* ---------- tiny presentational pieces ---------- */
function Spark({ values, color }) {
  if (!values || values.length < 2) return <svg width="104" height="22" aria-hidden="true" />;
  const w = 104;
  const h = 22;
  const max = Math.max(...values, 1);
  const points = values.map((v, i) => {
    const x = (i * (w - 2)) / (values.length - 1) + 1;
    const y = h - 2 - ((v || 0) / max) * (h - 5);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-hidden="true">
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  );
}

/* Utilisation bar. "Resource usage" is a proportion, so it reads faster as a
 * bar than as a number you have to compare mentally against a maximum. */
function Bar({ value, warn = 70, bad = 90 }) {
  const n = Number(value);
  if (value === null || value === undefined || Number.isNaN(n)) {
    return <div className="pm-bar pm-bar-empty" />;
  }
  const clamped = Math.max(0, Math.min(100, n));
  const tone = n >= bad ? 'bad' : n >= warn ? 'warn' : 'ok';
  return (
    <div className="pm-bar" role="img" aria-label={`${clamped.toFixed(0)} percent`}>
      <div className={`pm-bar-fill pm-bar-${tone}`} style={{ width: `${clamped}%` }} />
    </div>
  );
}

/* One component's health, compressed to a dot, a name and a single number. The
 * full detail still exists, one click away, rather than always on screen. */
function HealthChip({ name, state, headline, detail }) {
  return (
    <div className={'pm-chip ' + stateClass(state)}>
      <span className={'pm-dot ' + stateClass(state)} />
      <span className="pm-chip-name">{name}</span>
      <span className="pm-chip-head">{headline}</span>
      {detail ? <span className="pm-chip-detail">{detail}</span> : null}
    </div>
  );
}

/* A store's resource usage: utilisation first, then throughput, then that
 * store's own latency percentiles. Same row order on both cards so they can be
 * read against each other without sharing an axis. */
function Resource({ title, subtitle, state, label, usage, rows, percentiles, note,
                    error }) {
  // Same rule as Card: a read failure makes this panel unknown, not unhealthy.
  // Without this the bars and rows rendered as dashes with nothing saying why,
  // which reads as "idle" -- the opposite of the actual state.
  const unreachable = Boolean(error);
  const shown = unreachable ? 'unreachable' : state;
  return (
    <div className={'pm-res-card' + (unreachable ? ' pm-card-unknown' : '')}>
      <header>
        <h2>{title}</h2>
        <span className={'pm-pill ' + stateClass(shown)}>
          {unreachable ? 'unreachable' : (label || state || '?')}
        </span>
      </header>
      <div className="pm-res-sub" title={subtitle}>{subtitle}</div>
      {unreachable ? (
        <div className="pm-unknown">
          <b>Not readable from the control plane.</b>
          <span>
            The component may be healthy — what failed is reading it. The figures
            below are unknown, not zero.
          </span>
          <code>{error}</code>
        </div>
      ) : null}
      <div className="pm-usage">
        {usage.map((u, i) => (
          <div className="pm-usage-row" key={i}>
            <div className="pm-usage-head">
              <span>{u.label}</span>
              <b>{u.value === null || u.value === undefined ? dash : pct(u.value)}</b>
            </div>
            <Bar value={u.value} warn={u.warn} bad={u.bad} />
            {u.sub ? <div className="pm-usage-sub">{u.sub}</div> : null}
          </div>
        ))}
      </div>
      <Rows items={rows} />
      {percentiles ? (
        <table className="pm-pct-table">
          <thead>
            <tr><th>{percentiles.title}</th><th>p50</th><th>p90</th><th>p99</th></tr>
          </thead>
          <tbody>
            {percentiles.lines.map((l) => (
              <tr key={l.label} className={l.idle ? 'pm-lat-idle' : ''}>
                <th>{l.label}</th>
                {l.idle
                  ? <td colSpan="3" className="pm-lat-n">{l.idleText || 'nothing this interval'}</td>
                  : [l.p50, l.p90, l.p99].map((v, i) => (
                    <td key={i} className={'pm-lat-' + (l.tone || 'good')}>{ms(v)}</td>
                  ))}
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {note ? <div className="pm-note-inline">{note}</div> : null}
    </div>
  );
}

/* What one phase actually measured, over its own window. This is the number set
 * you compare between tabs, so it is grouped the same way every time: the
 * source, the target, then the pipeline between them. */
/*
 * One chart, one source. Several series are fine as long as they belong to the
 * SAME store — p50/p90/p99 of Aurora reads on one chart is the comparison you
 * want; Aurora next to Valkey on one chart is not, because the axes differ by
 * orders of magnitude and the smaller one flattens to nothing.
 */
function fmtUnit(value, unit) {
  if (value === null || value === undefined) return dash;
  if (unit === 'pct') return pct(value);
  if (unit === 'ms') return ms(value);
  if (unit === 'us') return us(value);
  return compact(value);
}

function Chart({ title, points, series, unit, hint, height = 118, stat = 'max' }) {
  const w = 520;
  const padL = 4;
  const padB = 15;
  const usable = (points || []).filter(
    (p) => series.some((s) => p[s.key] !== null && p[s.key] !== undefined));
  if (usable.length < 2) {
    return (
      <div className="pm-chart">
        <div className="pm-chart-title">{title}</div>
        <div className="pm-chart-empty" style={{ height }}>
          no data in this window yet
        </div>
      </div>
    );
  }
  const values = series.flatMap(
    (s) => usable.map((p) => Number(p[s.key])).filter((v) => !Number.isNaN(v)));
  const max = Math.max(1e-9, ...values);
  // "Tail latency" is a high percentile, not the absolute max: a single brief
  // spike (a reconnect, or a synthetic high-volume burst) must not define the headline
  // or flatten the whole chart. stat='p95' headlines the 95th percentile and
  // scales the y-axis to it (rare higher points clamp to the top edge). Other
  // charts keep the true max, where a peak is the meaningful number.
  const sorted = [...values].sort((a, b) => a - b);
  const pctl = (q) => (sorted.length
    ? sorted[Math.min(sorted.length - 1, Math.floor(q * (sorted.length - 1)))] : 0);
  const headline = stat === 'p95' ? Math.max(1e-9, pctl(0.95)) : max;
  const scale = Math.max(1e-9, headline);
  const t0 = usable[0].t;
  const t1 = usable[usable.length - 1].t;
  const span = Math.max(1, t1 - t0);
  const x = (t) => padL + ((t - t0) / span) * (w - padL * 2);
  const y = (v) => height - padB
    - (Math.min(Number(v) || 0, scale) / scale) * (height - padB - 6);
  const last = (key) => {
    for (let i = usable.length - 1; i >= 0; i -= 1) {
      if (usable[i][key] !== null && usable[i][key] !== undefined) return usable[i][key];
    }
    return null;
  };
  return (
    <div className="pm-chart">
      <div className="pm-chart-title">
        {title}
        <span className="pm-chart-legend">
          {series.map((s) => (
            <span key={s.key} style={{ color: s.color }}>
              &#9632; {s.label} <b>{fmtUnit(last(s.key), unit)}</b>
            </span>
          ))}
        </span>
      </div>
      <svg width="100%" height={height} viewBox={`0 0 ${w} ${height}`} role="img"
        aria-label={`${typeof title === 'string' ? title : 'series'} over time`}>
        <line x1={padL} y1={height - padB} x2={w - padL} y2={height - padB}
          stroke="#dfe5ea" />
        <text x={padL} y={11} className="pm-chart-ax">
          {stat === 'p95' ? `p95 ${fmtUnit(headline, unit)}` : `peak ${fmtUnit(max, unit)}`}
        </text>
        {series.map((s) => (
          <polyline key={s.key} fill="none" stroke={s.color} strokeWidth="1.6"
            strokeLinejoin="round"
            points={usable
              .filter((p) => p[s.key] !== null && p[s.key] !== undefined)
              .map((p) => `${x(p.t).toFixed(1)},${y(p[s.key]).toFixed(1)}`)
              .join(' ')} />
        ))}
        <text x={padL} y={height - 3} className="pm-chart-ax">
          {new Date(t0 * 1000).toLocaleTimeString()}
        </text>
        <text x={w - padL} y={height - 3} textAnchor="end" className="pm-chart-ax">now</text>
      </svg>
      {hint ? <div className="pm-chart-hint">{hint}</div> : null}
    </div>
  );
}

function Summary({ run }) {
  if (!run || !run.summary) {
    return (
      <div className="pm-wide pm-summary">
        <header><h2>This phase</h2></header>
        <div className="pm-summary-empty">
          No run recorded for this phase yet. Start it above and the numbers for
          its own window appear here.
        </div>
      </div>
    );
  }
  const s = run.summary;
  const v = (key, fmt = compact) => (s[key] === null || s[key] === undefined
    ? dash : fmt(s[key]));
  // No "Source · Aurora" / "Target · Valkey" sub-headings: the store is already
  // in every label, so the heading row was a second thing to read that said
  // nothing new. Still three columns, so they can be grouped visually by position.
  // A store that served no reads in this phase has no read percentiles. Printing
  // "— / —" for them looks like a broken collector; saying so in one line is
  // shorter and true. Applies both ways round: Aurora's read rows disappear in
  // the cache phase for the same reason.
  const has = (...keys) => keys.some((k) => s[k] !== null && s[k] !== undefined);
  // Pipeline phases (the CDC catch-up) generate no requests. Backend sends the
  // flag; fall back to the phase number if an older run record predates it.
  const loadPhase = run.load !== undefined && run.load !== null
    ? String(run.load) !== '0'
    : String(run.phase || run.steps || '') !== '3';
  const auroraServedReads = has('aurora_read_p50_avg', 'aurora_read_p90_avg',
                                'aurora_read_p99_max');
  const valkeyServedReads = has('valkey_read_p50_avg', 'valkey_read_p90_avg',
                                'valkey_read_p99_max');

  // The bulk load reads Aurora with a few very large SELECTs, so the
  // statement rate rounds to nothing while Aurora is reading tens of thousands
  // of rows a second. Reporting only statements made that phase look as though
  // Aurora was idle while the cache filled with no visible source. Rows first,
  // statements second, both labelled for what they actually count.
  const auroraLines = [
    ['Aurora CPU avg', v('aurora_cpu_pct_avg', pct)],
    // Peak comes from CloudWatch's Maximum statistic. The old value was the max
    // of 60-second averages, which flattened short bursts badly: 3.9% reported
    // where the real per-minute maximum was 12.6%.
    ['Aurora CPU peak \u00b7 true max', v('aurora_cpu_max_pct_max', pct)],
    ['Aurora rows read/s avg', v('aurora_rows_read_per_sec_avg')],
    ['Aurora rows read/s peak', v('aurora_rows_read_per_sec_max')],
    ['Aurora SELECTs/s avg \u00b7 statements', v('aurora_reads_per_sec_avg')],
    ['Aurora writes/s avg \u00b7 statements', v('aurora_writes_per_sec_avg')],
    ['Aurora queries running avg', v('aurora_active_queries_avg', (x) => num(x, 1))],
  ];
  if (auroraServedReads) {
    auroraLines.push(
      ['Aurora read p50 / p90',
        `${v('aurora_read_p50_avg', ms)} / ${v('aurora_read_p90_avg', ms)}`],
      ['Aurora read p99 worst', v('aurora_read_p99_max', ms)],
    );
  } else if (loadPhase) {
    // Percentiles come from the application. Aurora can be reading heavily for a
    // client that is not the app (the bulk loader), so this says "not through
    // the app" rather than "no reads", which would contradict the rows/s above.
    auroraLines.push(['Aurora reads via the app', 'none this phase']);
  }
  // A phase that generates no requests has no write percentiles either. Only
  // the pipeline phases are like this (the catch-up), and printing "— / —" for
  // them is what made a healthy catch-up look like a failed load.
  if (has('aurora_write_p50_avg', 'aurora_write_p90_avg', 'aurora_write_p99_max')) {
    auroraLines.push(
      ['Aurora write p50 / p90',
        `${v('aurora_write_p50_avg', ms)} / ${v('aurora_write_p90_avg', ms)}`],
      ['Aurora write p99 worst', v('aurora_write_p99_max', ms)],
    );
  }

  // Read rate and write rate, not one combined ops/s. A combined figure of
  // 3,119 next to "Valkey reads: none served" reads as a contradiction; split,
  // it states plainly that the traffic was ECDL applying writes.
  // Who is writing to the cache depends on the phase. In the bulk load it is the
  // loader writing straight in; in the other phases it is the sink applying CDC.
  // Labelling both "ECDL applying" was wrong for exactly the phase the user was
  // looking at.
  const bulkPhase = String(run.phase || run.steps || '') === '2';
  const valkeyLines = [
    ['Valkey engine CPU avg', v('valkey_engine_cpu_pct_avg', pct)],
    ['Valkey engine CPU peak \u00b7 true max', v('valkey_engine_cpu_max_pct_max', pct)],
    ['Valkey reads/s avg', v('valkey_reads_per_sec_avg')],
    [bulkPhase
      ? 'Valkey writes/s avg \u00b7 bulk loader, direct'
      : 'Valkey writes/s avg \u00b7 ECDL applying', v('valkey_writes_per_sec_avg')],
    ['Valkey memory peak', v('valkey_memory_pct_max', pct)],
    ['Valkey keys added', v('valkey_keys_delta')],
  ];
  if (valkeyServedReads) {
    valkeyLines.push(
      ['Valkey read p50 / p90',
        `${v('valkey_read_p50_avg', ms)} / ${v('valkey_read_p90_avg', ms)}`],
      ['Valkey read p99 worst', v('valkey_read_p99_max', ms)],
    );
  } else if (loadPhase) {
    valkeyLines.push(['Valkey reads via the app', 'none this phase']);
  }
  valkeyLines.push(['Valkey write avg', v('valkey_write_us_avg', us)]);

  // CDC lag is only observable while events are still queued. When the sink keeps
  // up there is nothing in flight to time, so an average over "the samples that
  // had a value" is an average over the moments the pipeline was behind. Showing
  // the coverage inline stops that reading as a typical figure - a 26 s lag next
  // to a 58-entry backlog is not physically possible and gets challenged.
  const lagN = s.cdc_lag_ms_n;
  const lagCoverage = lagN ? ` (${lagN} of ${s.samples})` : '';
  const cdcLines = lagN
    ? [
      [`CDC lag avg${lagCoverage}`, v('cdc_lag_ms_avg', ms)],
      ['CDC lag worst', v('cdc_lag_ms_max', ms)],
    ]
    : [['CDC lag', 'not measurable this phase']];
  if (!loadPhase) {
    // Says up front what this phase is, so "no app traffic" is read as the
    // design rather than as a failure.
    cdcLines.unshift(['Phase type', 'pipeline catch-up \u00b7 no app traffic']);
  }
  const groups = [auroraLines, valkeyLines, cdcLines.concat([
    ['CDC backlog peak', v('cdc_backlog_max')],
    ['samples', num(s.samples)],
    ['window', s.window_sec ? `${s.window_sec}s` : dash],
  ])];

  return (
    <div className="pm-wide pm-summary">
      <header>
        <h2>This phase</h2>
        <span className="pm-res">
          {run.title || ''}
          {run.status ? ` \u00b7 ${run.status}` : ''}
          {run.started_ms
            ? ` \u00b7 started ${new Date(run.started_ms).toLocaleTimeString()}` : ''}
        </span>
      </header>
      <div className="pm-summary-grid">
        {groups.map((lines, i) => (
          // eslint-disable-next-line react/no-array-index-key
          <div className="pm-summary-col" key={i}>
            {lines.map(([label, value]) => (
              <div className="pm-summary-row" key={label}>
                <span>{label}</span><b>{value}</b>
              </div>
            ))}
          </div>
        ))}
      </div>
      {s.aurora_cpu_note ? <div className="pm-note-inline">{s.aurora_cpu_note}</div> : null}
      {s.cdc_lag_note ? <div className="pm-note-inline">{s.cdc_lag_note}</div> : null}
    </div>
  );
}

/*
 * Bulk load, straight into Valkey. A load that rewrites keys already present
 * changes no key count and no value, so there is nothing to see from outside —
 * these numbers are the loader's own report of where each worker has reached.
 * Watching the per-worker primary key climb is the "ids moving" view.
 */
function BulkFlow({ bulk, valkeyWrites }) {
  const b = bulk || {};
  const workers = b.workers || [];
  return (
    <div className={'pm-flowcard' + (b.active ? ' pm-flowcard-live' : '')}>
      <header>
        <h2>Bulk load into Valkey</h2>
        <span className={'pm-pill ' + (b.active ? 'pm-ok' : 'pm-idle')}>
          {b.active ? 'loading' : 'idle'}
        </span>
      </header>
      {b.error ? <div className="pm-error">{b.error}</div> : null}
      {b.active ? (
        <>
          <div className="pm-flowtop">
            <BigNum value={compact(b.rows_loaded)} label="rows written"
              sub={b.pct !== null && b.pct !== undefined ? `${b.pct}% of range` : null} />
            <BigNum value={compact(b.rows_per_sec)} label="rows/s" />
            <BigNum value={`${num(b.workers_done)}/${num(b.worker_count)}`}
              label="workers finished" />
          </div>
          <table className="pm-table pm-table-tight">
            <thead>
              <tr><th>worker</th><th className="pm-num">last id written</th>
                <th className="pm-num">rows</th><th> </th></tr>
            </thead>
            <tbody>
              {workers.map((w) => (
                <tr key={w.worker}>
                  <td>#{w.worker}</td>
                  <td className="pm-num pm-mono">{num(w.last_pk)}</td>
                  <td className="pm-num">{compact(w.rows)}</td>
                  <td className={w.done ? 'pm-state-synced' : 'pm-state-differs'}>
                    {w.done ? '\u25cf done' : '\u25d0 writing'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="pm-note-inline">{b.note}</div>
        </>
      ) : (
        <div className="pm-flowidle">
          {b.finished_note
            ? `${b.finished_note} \u00b7 ${compact(b.rows_loaded)} rows at `
              + `${compact(b.rows_per_sec)}/s`
            : (b.note || 'no bulk load running')}
          {valkeyWrites ? (
            <div className="pm-flowidle-sub">
              Valkey is taking {compact(valkeyWrites)} writes/s right now — from CDC,
              not from a bulk load.
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

function BigNum({ value, label, tone, spark, sub }) {
  return (
    <div className="pm-bignum">
      <div className={'pm-v' + (tone ? ' pm-' + tone : '')}>{value}</div>
      <div className="pm-k">{label}</div>
      {sub ? <div className="pm-k" style={{ marginTop: 4 }}>{sub}</div> : null}
      {spark || null}
    </div>
  );
}

function Rows({ items }) {
  return (
    <dl className="pm-rows">
      {items.filter(Boolean).map(({ label, value, tone, mono }) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd className={(mono ? 'pm-mono' : '') + (tone ? ' pm-' + tone : '')}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Card({ icon, title, resource, data, bignums, rows, note }) {
  // An error means the read failed, so this panel's numbers are unknown rather
  // than bad. Everything below it is stale or absent, and saying "error" in red
  // led readers to conclude that the component had failed.
  const unreachable = Boolean(data.error);
  const state = unreachable ? 'unreachable'
    : (data.ok === false ? 'bad' : (data.state || 'ok'));
  const label = unreachable ? 'unreachable' : (data.label || 'up');
  return (
    <section className={'pm-card' + (unreachable ? ' pm-card-unknown' : '')}>
      <header>
        <h2>{icon} {title}</h2>
        <span className={'pm-pill ' + stateClass(state)}>{label}</span>
      </header>
      <div className="pm-res">{resource}</div>
      {unreachable ? (
        <div className="pm-unknown">
          <b>Not readable from the control plane.</b>
          <span>
            The component may be healthy — what failed is reading it. The figures
            below are unknown, not zero.
          </span>
          <code>{data.error}</code>
        </div>
      ) : null}
      <div className="pm-bignums">{bignums}</div>
      <Rows items={rows} />
      {note ? <div className="pm-note-inline">{note}</div> : null}
    </section>
  );
}

/* ---------- page ---------- */
function PipelineMonitor({ initialLink }) {
  // Which pipeline this page is describing. The configuration names one, which
  // was fine while there was one and misleading once the control plane could
  // deploy more: a deployed link opened this page and read a healthy dashboard
  // about a different pipeline entirely.
  const [links, setLinks] = useState([]);
  // True once discovery has answered WITHOUT a problem, so "no links" means
  // nothing is deployed rather than "could not list stacks".
  const [linksListed, setLinksListed] = useState(false);
  // `initialLink` wins over the remembered selection: it is only set when
  // something navigated here about a SPECIFIC link (the "Watch this pipeline"
  // action on a deployment), and honouring a stale sessionStorage value there
  // would open a different pipeline than the one just asked for.
  const [watching, setWatching] = useState(
    () => {
      if (initialLink) return initialLink;
      try {
        return sessionStorage.getItem('ecdl.monitorLink') || '';
      } catch (e) {
        return '';
      }
    });
  const [linkError, setLinkError] = useState(null);
  // Whether the configured default still exists, so the option can say so rather
  // than silently opening a pipeline whose components were deleted.
  const [defaultInfo, setDefaultInfo] = useState(null);
  const [data, setData] = useState(null);
  const [fetchError, setFetchError] = useState(null);
  const [paused, setPaused] = useState(false);
  const [fetchedAt, setFetchedAt] = useState(null);
  const [now, setNow] = useState(() => Date.now());
  const [probing, setProbing] = useState(false);
  const [probeError, setProbeError] = useState(null);
  const [runs, setRuns] = useState(null);
  const [phases, setPhases] = useState(null);
  const [runError, setRunError] = useState(null);
  // One tab per demo phase. The tab is the unit of the demo: it starts that
  // phase's load and scopes the history to that phase's own window, so switching
  // between tabs is the comparison.
  const [activeTab, setActiveTab] = useState('1');
  // The per-component internals are diagnostic, not the main content. Off by default,
  // but forced open below whenever something is actually wrong.
  const [showDetails, setShowDetails] = useState(false);
  // Random-id spot check. Held separately from the polled dashboard because it
  // is a snapshot: the backend picks new ids on every call, so polling it would
  // reshuffle the rows continuously and never let anyone read them.
  const [sampleMode, setSampleMode] = useState('newest');
  const [randomFlow, setRandomFlow] = useState(null);
  const [randomAt, setRandomAt] = useState(null);
  const [randomNonce, setRandomNonce] = useState(0);
  const [sampling, setSampling] = useState(false);
  const [recon, setRecon] = useState(null);
  const [reconciling, setReconciling] = useState(false);
  const [flowErr, setFlowErr] = useState(null);
  const [trend, setTrend] = useState(null);
  const [trendMinutes, setTrendMinutes] = useState(120);
  const history = useRef({});

  const push = useCallback((key, value) => {
    if (value === null || value === undefined) return;
    const series = history.current[key] || [];
    series.push(Number(value));
    if (series.length > SPARK_SAMPLES) series.shift();
    history.current[key] = series;
  }, []);

  useEffect(() => {
    let cancelled = false;
    // Re-read every 30 s, so a pipeline deployed while this page is open shows
    // up without a reload (the empty "nothing deployed" state depends on it).
    const refresh = () => axios.get(`${API_URL}/api/monitor/links`, { timeout: 20000 })
      .then((res) => {
        if (cancelled) return;
        setLinks(res.data.links || []);
        setLinksListed(!(res.data.problems || []).length);
        setDefaultInfo(res.data.default || null);
        (res.data.problems || []).forEach((problem) => setLinkError(problem));
        // Open on a real pipeline rather than on a configured default that no
        // longer exists. Without this, deploying a link and opening this page
        // showed every component unreachable -- the config still named a
        // pipeline whose stacks had been deleted -- and the link that DID work
        // sat unselected in the dropdown, so the page read as broken.
        // Only when nothing was explicitly asked for: an explicit choice, a
        // remembered one, or "Watch this pipeline" all outrank a default.
        if (res.data.prefer && !initialLink) {
          let remembered = '';
          try {
            remembered = sessionStorage.getItem('ecdl.monitorLink') || '';
          } catch (e) { remembered = ''; }
          if (!remembered) chooseLink(res.data.prefer);
        }
      })
      .catch(() => { /* the page still works on its configured pipeline */ });
    refresh();
    const timer = setInterval(refresh, 30000);
    return () => { cancelled = true; clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialLink]);

  const chooseLink = (name) => {
    setWatching(name);
    setData(null);          // never show one pipeline's numbers under another's name
    setLinkError(null);
    try {
      if (name) sessionStorage.setItem('ecdl.monitorLink', name);
      else sessionStorage.removeItem('ecdl.monitorLink');
    } catch (e) { /* selection just will not persist */ }
  };

  const load = useCallback(async () => {
    try {
      const res = await axios.get(
        `${API_URL}/api/monitor/dashboard${watching ? `?link=${encodeURIComponent(watching)}` : ''}`,
        { timeout: 15000 });
      const d = res.data;
      push('aurora.reads', d.aurora?.reads_per_sec);
      push('aurora.writes', d.aurora?.writes_per_sec);
      push('broker.produced', d.broker?.produced_per_sec);
      push('broker.backlog', d.broker?.backlog);
      push('sink.applied', d.sink?.applied_per_sec);
      push('target.ops', d.target?.ops_per_sec);
      setData(d);
      setFetchedAt(Date.now());
      setFetchError(null);
    } catch (e) {
      // A 404 here means one specific thing: the selected link has no CDC stack,
      // so it is not deployed (any more). That happens routinely -- the selection
      // is remembered in sessionStorage, and the pipeline it names can be torn
      // down while the tab stays open. Recover from it instead of leaving the
      // page blank behind "Request failed with status code 404", which is what it
      // used to do and reads as "the monitor is broken" rather than "that
      // pipeline is gone".
      const gone = e.response && e.response.status === 404;
      if (gone && watching) {
        const detail = (e.response.data && e.response.data.error) || '';
        setLinkError(`${watching} is not deployed any more${detail ? ` \u2014 ${detail}` : ''}`
                     + '. Showing this control plane\u2019s configured pipeline instead.');
        // Drop the stale selection so a reload does not open it again.
        try { sessionStorage.removeItem('ecdl.monitorLink'); } catch (err) { /* fine */ }
        setWatching('');
        setFetchError(null);
        return;
      }
      setFetchError(e.message || 'request failed');
    }
  }, [push, watching]);

  // The one action on this page that writes: it stamps a single dedicated
  // heartbeat row in Aurora and times how long that value takes to reach the
  // cache. Explicit click only — never fired by the poll loop.
  const probe = useCallback(async () => {
    setProbing(true);
    setProbeError(null);
    try {
      // The link goes in the body, so the action cannot run against the
      // configured pipeline while this page is showing another one. For a
      // deployed link the backend refuses, because the probe writes to the
      // source table.
      const res = await axios.post(`${API_URL}/api/monitor/probe`,
                                   watching ? { link: watching } : {},
                                   { timeout: 20000 });
      if (!res.data.ok) setProbeError(res.data.error || 'probe failed');
      await load();
    } catch (e) {
      setProbeError(e.message || 'probe request failed');
    } finally {
      setProbing(false);
    }
    // `watching` is a dependency: without it the callback closes over the link
    // selected when it was created, so switching links and pressing the button
    // would act on the previous one.
  }, [load, watching]);

  // Exhaustive row-vs-key count. Explicit click only: it walks every key in the
  // cache, which is seconds of single-threaded cache CPU and should not be
  // running inside a 2-second poll.
  const reconcile = useCallback(async () => {
    setReconciling(true);
    try {
      const res = await axios.post(`${API_URL}/api/monitor/reconcile`,
                                   watching ? { link: watching } : {},
                                   { timeout: 300000 });
      setRecon(res.data);
    } catch (e) {
      setRecon({ error: e.message || 'reconcile request failed' });
    } finally {
      setReconciling(false);
    }
  }, [watching]);

  // Run controls. A phase is queued on the broker and picked up by the agent on
  // the loadgen host, which runs run_3step_demo.sh. The page sends a phase id and
  // nothing else - never a command - so the button cannot be used to run shell commands.
  const loadRuns = useCallback(async () => {
    try {
      const res = await axios.get(`${API_URL}/api/monitor/runs?limit=8`,
                                  { timeout: 15000 });
      setRuns(res.data);
    } catch (e) { /* keep the last known state */ }
  }, []);

  useEffect(() => { loadRuns(); }, [loadRuns]);
  useEffect(() => {
    const id = setInterval(loadRuns, 5000);
    return () => clearInterval(id);
  }, [loadRuns]);

  useEffect(() => {
    axios.get(`${API_URL}/api/monitor/phases`, { timeout: 15000 })
      .then((res) => setPhases((res.data && res.data.phases) || null))
      .catch(() => {});
  }, []);

  const startPhase = async (phase) => {
    setRunError(null);
    try {
      await axios.post(`${API_URL}/api/monitor/run`,
                       { phase, conc: 1000, dur: 240, write_pct: 5 },
                       { timeout: 20000 });
      loadRuns();
    } catch (e) {
      setRunError((e.response && e.response.data && e.response.data.error) || e.message);
    }
  };

  const cancelRun = async () => {
    setRunError(null);
    try {
      await axios.post(`${API_URL}/api/monitor/run/cancel`, {},
                       { timeout: 20000 });
      loadRuns();
    } catch (e) {
      setRunError((e.response && e.response.data && e.response.data.error) || e.message);
    }
  };

  // History is sampled and persisted server-side, so it covers phases that have
  // already finished and survives a reload. Polled slower than the live view.
  const loadTrend = useCallback(async () => {
    try {
      const res = await axios.get(
        `${API_URL}/api/monitor/history?minutes=${trendMinutes}`
        + `${watching ? `&link=${encodeURIComponent(watching)}` : ''}`,
        { timeout: 25000 });
      setTrend(res.data);
    } catch (e) { /* keep the chart that is already on screen */ }
  }, [trendMinutes, watching]);

  useEffect(() => { loadTrend(); }, [loadTrend]);

  // One fetch per explicit request: switching to random, or pressing re-sample.
  useEffect(() => {
    if (sampleMode !== 'random') return;
    let cancelled = false;
    setSampling(true);
    setFlowErr(null);
    axios.get(`${API_URL}/api/monitor/dataflow?sample=random&limit=3`
              + `${watching ? `&link=${encodeURIComponent(watching)}` : ''}`,
              { timeout: 30000 })
      .then((res) => {
        if (cancelled) return;
        setRandomFlow(res.data);
        setRandomAt(new Date().toLocaleTimeString());
      })
      .catch((e) => { if (!cancelled) setFlowErr(`random sample failed: ${e.message}`); })
      .finally(() => { if (!cancelled) setSampling(false); });
    return () => { cancelled = true; };
  }, [sampleMode, randomNonce, watching]);
  useEffect(() => {
    if (paused) return undefined;
    const id = setInterval(loadTrend, 10000);
    return () => clearInterval(id);
  }, [paused, loadTrend]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (paused) return undefined;
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [paused, load]);

  // Ticks the clock so "updated Ns ago" keeps counting up, which is how a
  // paused or stalled page stays visibly stale instead of looking current.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const ageSec = fetchedAt ? Math.max(0, Math.floor((now - fetchedAt) / 1000)) : null;
  const stale = ageSec !== null && ageSec > 10 && !paused;
  // Never let a collapsed section hide a failure: anything red opens it.
  const alarm = (dataParsed) => {
    const h = (dataParsed.flow && dataParsed.flow.hops) || {};
    const v = (dataParsed.integrity || {}).verdict;
    return Object.values(h).includes('bad') || v === 'loss' || v === 'stalled';
  };

  // A fresh control plane: no link deployed, and the configured default is not
  // a real pipeline. Say that, instead of rendering every component as an
  // error against placeholder endpoints -- which read as a broken product on
  // the first visit after install.
  const nothingDeployed = !watching && linksListed && links.length === 0
    && defaultInfo && defaultInfo.available === false;
  if (nothingDeployed) {
    return (
      <div className="pm">
        <div className="pm-head"><h1>Pipeline Monitor</h1></div>
        <p className="pm-page-note">
          No pipeline is deployed yet. Create one in <strong>1. Define → Build
          Pipeline</strong>; it appears here once its change-capture stack exists.
        </p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="pm">
        <div className="pm-head"><h1>Pipeline Monitor</h1></div>
        {linkError && <div className="pm-banner-error">{linkError}</div>}
        {fetchError
          ? <div className="pm-banner-error">Could not reach {API_URL}/api/monitor/dashboard — {fetchError}</div>
          : <p className="pm-page-note">Loading pipeline state…</p>}
      </div>
    );
  }

  const {
    aurora = {}, debezium = {}, broker = {}, sink = {}, target = {},
    dataflow = {}, integrity = {}, flow = {}, bulkload = {},
  } = data;
  // Per-table join, added alongside the roll-ups rather than replacing them.
  const perTable = data.tables || {};
  const tableRows = perTable.tables || [];
  const hops = flow.hops || {};
  const lagBad = flow.cdc_lag_ms === null || flow.cdc_lag_ms > 1000;
  const cw = aurora.cloudwatch || {};
  const tcw = target.cloudwatch || {};
  // Promoted to the top level in the payload: it compares both stores, so it is
  // not a property of either one. Older payloads kept it under target.
  const app = data.app_latency || target.demo_app || null;
  // CloudWatch values are 1-minute aggregates published a minute or so late.
  // Without the age on screen, a stale "76% CPU" next to a live "4 reads/s"
  // reads as a bug.
  const cwAge = (age) => (age === null || age === undefined
    ? '60s avg' : `60s avg \u00b7 ${age}s old`);
  const appSamples = (key) => !!(app && app[`${key}_samples`]);
  const detailsOpen = showDetails || alarm(data);
  // Fall back to the polled newest-rows view until a random snapshot exists.
  const shownFlow = (sampleMode === 'random' && randomFlow) ? randomFlow : dataflow;
  // The compared columns come from the backend rows (db_<field>/cache_<field>),
  // which are this link's own columns after its transform -- not a hardcoded
  // price/stock pair. So a catalog link shows price/name/category, not a phantom
  // "stock" column it never had.
  // Sample rows per table from the dataflow sampler. The payload is now
  // {tables:[{table,rows,...}]} (one per table in the link); fall back to the
  // older single-table shape so an old payload still renders.
  const flowRowTables = shownFlow.tables
    || (shownFlow.table
      ? [{ table: shownFlow.table, key_prefix: shownFlow.key_prefix, rows: shownFlow.rows || [] }]
      : []);
  const rowsByTable = {};
  flowRowTables.forEach((t) => { if (t.table) rowsByTable[t.table] = t; });

  // Per-table "data flowing" blocks. The DMS-style counters come from each
  // table's broker stream (live every 2s): rows read (CDC) = entries_added,
  // rows applied (CDC) = read - backlog, backlog = read - applied. The sample
  // keys come from the sampler above, matched to each table by name, so EVERY
  // table in a multi-table link shows its own rows.
  const flowStreams = (broker.streams && broker.streams.length)
    ? broker.streams
    : flowRowTables.map((t) => ({ table: t.table, key_prefix: t.key_prefix }));
  const flowTables = flowStreams.map((s) => {
    const dt = rowsByTable[s.table];
    // Show only rows that are actually in the cache (synced or stale). A row
    // absent from the cache is not shown: on a caught-up table it was excluded
    // by a transform filter (nothing to compare), and completeness is proven by
    // the rows-read == rows-applied counters + delivery integrity, not by a
    // sample row. This is the sample's job: prove source == target for the rows
    // that ARE cached.
    const rows = dt
      ? (dt.rows || []).filter((r) => r.state === 'synced' || r.state === 'differs').slice(0, 3)
      : [];
    const read = s.entries_added;
    const backlog = s.backlog || 0;
    const applied = (read === null || read === undefined)
      ? null : Math.max(0, read - backlog);
    return {
      table: s.table || '(table)',
      read, applied, backlog,
      readRate: s.produced_per_sec, appliedRate: s.applied_per_sec,
      caught: backlog === 0,
      rows, hasRows: !!dt,
    };
  });
  const caughtCount = flowTables.filter((t) => t.caught).length;
  // Join a row's own db_/cache_ fields into one plain cell, derived per row so
  // different tables with different columns each render their own values:
  // "demo · Sustained 1269799 · 8.99".
  const flowVal = (r, prefix) => Object.keys(r)
    .filter((k) => k.indexOf(prefix) === 0)
    .map((k) => r[k])
    .filter((v) => v !== null && v !== undefined)
    .map(String).join(' \u00b7 ');
  const flowRate = (r) => (r === null || r === undefined) ? ''
    : (r > 0 ? `+${compact(r)}/s` : 'idle');

  const degraded = Object.values(hops).some((v) => v && v !== 'ok');
  // Only a genuine 'bad' hop justifies the red treatment. 'unreachable' is a gap
  // in our view, not a fault we have observed.
  const anyDown = Object.values(hops).some((v) => v === 'bad');
  // Connections as a proportion of the server's limit, so it reads as usage
  // rather than a bare number with no ceiling.
  const connPct = aurora.max_connections
    ? (aurora.threads_connected / aurora.max_connections) * 100
    : null;

  // ── tabs ─────────────────────────────────────────────────────────────────
  // Phase definitions come from the backend so the wording lives in one place;
  // the fallback only matters if that call failed.
  // cdc mirrors the backend flag: a lag figure is only shown once something is
  // actually being served from the cache. load=false marks the phases that are
  // pipeline operations rather than traffic, so the page does not ask them for
  // request latency it could never have. Kept in the fallback too, or the page
  // would behave differently depending on whether one call succeeded.
  const phaseList = (phases && phases.length) ? phases : [
    { id: '1', title: 'DB load only', cdc: false, load: true },
    { id: '2', title: 'Bulk insert into cache', cdc: false, load: true },
    { id: '3', title: 'CDC on: catch up, then cache reads', cdc: true, load: true },
  ];
  const runsList = (runs && runs.runs) || [];
  // LPUSH order, so the first match is the most recent.
  const runningRun = runsList.find((r) => r.status === 'running' || r.status === 'queued');
  const phaseRun = runsList.find((r) => String(r.phase) === String(activeTab));
  const activePhase = phaseList.find((p) => String(p.id) === String(activeTab)) || {};
  // CDC lag is only shown for phases that involve reading the cache.
  // Driven by the backend's phase metadata rather than hardcoded tab numbers; if
  // that endpoint is unavailable, fall back to showing it.
  const cdcShown = phaseList.length ? activePhase.cdc === true : true;
  const agentOk = !runs || runs.agent_ok !== false;
  return (
    <div className="pm">
      {/* Also shown here, not only on the pre-data branch. The commonest cause
          is a remembered selection whose pipeline has been torn down: the page
          recovers onto the configured pipeline, so `data` arrives and the
          early-return banner never gets a chance to say why the subject
          changed. */}
      {linkError && <div className="pm-banner-warn">{linkError}</div>}
      <div className="pm-head">
        <h1>Pipeline Monitor</h1>
        <div className="pm-linkpick">
          <label htmlFor="pm-link">watching</label>
          <select id="pm-link" value={watching}
                  onChange={(e) => chooseLink(e.target.value)}>
            {/* Says whether the default still exists. A default that no longer exists but reads
                like an ordinary choice is how a deleted pipeline gets mistaken
                for a broken product: every component shows unreachable and the
                link that works sits unselected below it. */}
            <option value="">
              {defaultInfo && defaultInfo.available === false
                ? `this control plane's configured pipeline (gone)`
                : `this control plane's configured pipeline`}
            </option>
            {/* The watched link always gets an option, even if the discovery
                call has not answered yet or does not list it. A <select> whose
                value is not among its options renders BLANK, so the selector read
                "this control plane's configured pipeline" while every panel
                below showed a specific link -- the precise mislabelling this
                selector exists to prevent. Happens whenever the page is opened
                about a link from elsewhere, which is now a button. */}
            {watching && !links.some((l) => l.link === watching) && (
              <option value={watching}>{watching}</option>
            )}
            {links.map((l) => (
              <option key={l.link} value={l.link}>{l.link}</option>
            ))}
          </select>
          {/* Said plainly, because a dashboard that does not name its subject is
              how a healthy-looking page about the wrong pipeline goes unnoticed. */}
          <span className="pm-watchnote">
            {watching
              ? `link ${watching}`
              : 'not a deployed link — the pipeline this page was configured with'}
          </span>
        </div>
        <div className="pm-spacer" />
        <button className="btn btn-secondary" onClick={() => setPaused((p) => !p)}>
          {paused ? '\u25b6 Resume' : '\u275a\u275a Pause'}
        </button>
        <span className={'pm-stamp' + (stale ? ' pm-stale' : '')}>
          <span className={'pm-live-dot' + (paused ? ' pm-paused' : '')} />
          {paused ? 'paused' : `polling every ${POLL_MS / 1000}s`}
          {ageSec !== null ? ` \u00b7 updated ${ageSec}s ago` : ''}
        </span>
      </div>

      {fetchError
        ? <div className="pm-banner-error">Last poll failed ({fetchError}) — showing the previous reading.</div>
        : null}

      {/* ── TABS: one per demo phase ────────────────────────────────────────
          Each tab starts that phase's load and scopes the history below to that
          phase's window. Starting is a separate click from selecting: a tab
          click that silently began a 1000-connection load would be too easy to
          fire by accident on shared infrastructure.

          Hidden entirely while watching a deployed link. These drive a load
          generator on the demo host, against the demo pipeline's broker — so on
          a link they were both meaningless and broken: pressing one reported
          "Name or service not known" for a broker belonging to another pipeline.
          A control that cannot work here should not be here. */}
      {watching ? (
        <div className="pm-demo-note">
          Load phases are part of the demo harness and run against the pipeline
          this control plane was configured with, so they are not shown for a
          deployed link. Everything below is scoped to <b>{watching}</b>.
        </div>
      ) : (
      <>
      <div className="pm-tabs" role="tablist">
        {phaseList.map((p, i) => {
          const last = runsList.find((r) => String(r.phase) === String(p.id));
          const isRunning = runningRun && String(runningRun.phase) === String(p.id);
          return (
            <button key={p.id} role="tab"
              aria-selected={String(activeTab) === String(p.id)}
              className={'pm-tab' + (String(activeTab) === String(p.id) ? ' pm-tab-on' : '')
                + (isRunning ? ' pm-tab-running' : '')}
              onClick={() => setActiveTab(String(p.id))}>
              <span className="pm-tab-n">{i + 1}</span>
              <span className="pm-tab-title">{p.title}</span>
              <span className="pm-tab-sub">
                {isRunning ? <><span className="pm-live-dot" /> running</>
                  : last ? `last: ${last.status || 'done'}` : 'not run yet'}
              </span>
            </button>
          );
        })}
      </div>

      <div className="pm-tabpanel" role="tabpanel">
        <div className="pm-tab-detail">{activePhase.detail || ''}</div>
        <div className="pm-runbar">
          <button className="pm-run-btn"
            disabled={!!runningRun || !agentOk}
            onClick={() => startPhase(String(activeTab))}>
            {runningRun ? 'a run is in progress' : `Run: ${activePhase.title || 'phase'}`}
          </button>
          <button className="pm-run-stop" onClick={cancelRun}
            disabled={!runningRun}>Stop</button>
          <span className="pm-runbar-state">
            {runningRun
              ? <><span className="pm-live-dot" />
                {`${runningRun.title || 'phase ' + runningRun.phase} \u00b7 ${runningRun.status}`}</>
              : '95% read / 5% write \u00b7 conc 1000 \u00b7 240s'}
          </span>
          {/* A queued job with nobody to run it looks identical to a slow start
              unless the page says which it is. */}
          {!agentOk ? (
            <span className="pm-run-err">
              agent on the loadgen host is not reporting in — start it with
              {' '}<code>setsid ./monitor_agent.py &amp;</code> or the buttons do nothing
            </span>
          ) : null}
          {runError ? <span className="pm-run-err">{runError}</span> : null}
        </div>
        {runningRun && runningRun.output_tail ? (
          <pre className="pm-run-out">{runningRun.output_tail}</pre>
        ) : null}
      </div>
      </>
      )}

      {/* ── 1. CDC LAG + PIPELINE HEALTH ───────────────────────────────────
          The lag is the headline. The three components and delivery integrity
          are reduced to a dot and one number each: at a glance, either the
          pipeline is fine or it is not. Detail lives behind the toggle. */}
      <div className={'pm-banner' + (cdcShown ? '' : ' pm-banner-nolag')}>
        {/* Shown only in phases where something is being served from the cache.
            In "DB load only" and "Bulk insert" nothing reads the cache, so its
            staleness is not relevant - and because a healthy sink drains the
            stream to empty there is nothing in flight to measure either, which
            rendered as an empty panel asking you to run a probe. The probe still
            exists, under Show detail. */}
        {!cdcShown ? null : flow.cdc_in_path === false ? (
          <div className="pm-lagbox pm-lagbox-lead pm-lagbox-off">
            <div className="pm-k">CDC lag<span className="pm-src">not in path</span></div>
            <div className="pm-v pm-v-off">{'\u2014'}</div>
            <div className="pm-d">
              {flow.cdc_context_note
                || 'a direct load is writing the cache, bypassing the broker'}
            </div>
          </div>
        ) : (
          <div className="pm-lagbox pm-lagbox-lead">
            <div className="pm-k">
              CDC lag
              {flow.lag_source ? <span className="pm-src">{flow.lag_source}</span> : null}
              {flow.cdc_context === 'draining'
                ? <span className="pm-src pm-src-warn">draining</span> : null}
            </div>
            <div className={'pm-v' + (lagBad ? ' pm-bad' : '')
              + (flow.lag_stale ? ' pm-stale-lag' : '')}>
              {flow.cdc_lag_ms === null ? dash : ms(flow.cdc_lag_ms)}
              {flow.cdc_lag_ms > 1000 ? ' \u25b2' : ''}
            </div>
            <div className={'pm-d' + (flow.lag_stale ? ' pm-d-stale' : '')}>
              {flow.lag_note
                || (flow.lag_source === 'in-flight'
                  ? `capture ${ms(flow.capture_ms)} \u00b7 broker wait ${ms(flow.broker_wait_ms)}`
                  : 'not measurable right now')}
            </div>
            <button className="pm-probe-btn" onClick={probe} disabled={probing}>
              {probing ? 'measuring\u2026' : 'Measure now'}
            </button>
            {probeError ? <div className="pm-probe-err">{probeError}</div> : null}
          </div>
        )}
        {!cdcShown ? (
          <div className="pm-nolag-note">
            {activePhase.title
              ? `${activePhase.title}: nothing is read from the cache in this phase, `
                + 'so CDC lag is not shown. Component health is below.'
              : 'CDC lag is not shown in this phase.'}
          </div>
        ) : null}

        <div className="pm-chips">
          <HealthChip name="Debezium" state={hops.debezium}
            headline={`${debezium.running ?? dash}/${debezium.desired ?? dash} tasks`}
            detail={debezium.capture_lag_ms !== null
              && debezium.capture_lag_ms !== undefined
              ? `capture ${ms(debezium.capture_lag_ms)}` : 'reading binlog'} />
          <HealthChip name="Broker" state={hops.broker}
            headline={`${compact(broker.produced_per_sec)} ev/s`}
            detail={`backlog ${compact(broker.backlog)}`} />
          <HealthChip name="Sink" state={hops.sink}
            headline={`${sink.running ?? dash}/${sink.desired ?? dash} tasks`}
            detail={`${compact(sink.applied_per_sec)} applied/s`} />
          <HealthChip name="Delivery integrity" state={integrity.state}
            headline={VERDICT_WORDS[integrity.verdict] || integrity.label || 'unknown'}
            detail={integrity.dead_lettered || integrity.evicted_keys
              ? `${num(integrity.dead_lettered)} dead-lettered \u00b7 ${num(integrity.evicted_keys)} evicted`
              : 'nothing lost'} />
          <button className="pm-details-btn" onClick={() => setShowDetails((s) => !s)}>
            {detailsOpen ? 'Hide detail' : 'Show detail'}
          </button>
        </div>

        {/* Why the pipeline is busy (or not) in THIS phase. Without this, the
            DB-only phase looks broken: the tab says "DB only" yet CDC is moving
            events. It is not a bug — writes always go to Aurora, and ECDL keeps
            replicating them whatever is serving reads. Only the read path moves
            between phases. */}
        {CDC_CONTEXT[String(activeTab)] ? (
          <div className="pm-cdc-context">{CDC_CONTEXT[String(activeTab)]}</div>
        ) : null}

        {/* Only when something is wrong. When everything is fine the chips
            already say so, and continuous status text about a healthy pipeline is
            what made the old banner claim things like "idle" incorrectly. */}
        {/* Red is reserved for something actually being wrong. A hop we merely
            cannot read is grey: the pipeline may be entirely healthy, and
            colouring a visibility gap as an outage is what made a working pipeline
            look broken. */}
        {degraded && flow.message ? (
          <div className={'pm-flowmsg ' + (anyDown ? 'pm-bad' : 'pm-unknown-msg')}>
            {flow.message}
          </div>
        ) : null}
        {flow.visibility_note ? (
          <div className="pm-visibilitynote">{flow.visibility_note}</div>
        ) : null}
        {flow.integrity_alert ? (
          <div className={'pm-integ-alert'
            + (integrity.verdict === 'loss' ? ' pm-integ-alert-loss' : '')}>
            {integrity.verdict === 'loss' ? '\u26a0 ' : ''}{flow.integrity_alert}
          </div>
        ) : null}
      </div>

      {/* ── 2. RESOURCE USAGE, ONE CARD PER STORE ──────────────────────────
          Utilisation first as a bar, then throughput, then that store's own
          read and write percentiles. Same row order both sides so they can be
          read against each other without the two being mixed on one chart. */}
      <div className="pm-resources">
        <Resource
          title={'Source \u00b7 Aurora'} state={aurora.state} label={aurora.label}
          error={aurora.error}
          subtitle={aurora.endpoint || 'Aurora MySQL'}
          usage={[
            { label: <>CPU <span className="pm-cw">{cwAge(cw.age_sec)}</span></>,
              value: cw.cpu_pct, warn: 70, bad: 90 },
            { label: <>connections <span className="pm-cw">live</span></>,
              value: connPct,
              sub: `${num(aurora.threads_connected)} of ${num(aurora.max_connections)} max`
                + ` \u00b7 ${num(aurora.threads_running)} running a query now` },
          ]}
          rows={[
            { label: <>reads/s <span className="pm-cw">live</span></>,
              value: compact(aurora.reads_per_sec) },
            { label: <>writes/s <span className="pm-cw">live</span></>,
              value: compact(aurora.writes_per_sec) },
            // CloudWatch 60s averages, always available (SelectLatency = read,
            // DMLLatency = write, CommitLatency = durable-write cost). Shown here
            // so the source card carries read/write latency the same way the
            // target card does, instead of only in the footer.
            { label: <>read latency <span className="pm-cw">{cwAge(cw.age_sec)}</span></>,
              value: ms(cw.select_latency_ms) },
            { label: <>write latency <span className="pm-cw">{cwAge(cw.age_sec)}</span></>,
              value: ms(cw.dml_latency_ms) },
            { label: <>commit latency <span className="pm-cw">{cwAge(cw.age_sec)}</span></>,
              value: ms(cw.commit_latency_ms) },
          ]}
          percentiles={{
            title: <>latency <span className="pm-cw">app-measured, this interval</span></>,
            lines: [
              { label: 'read', tone: 'bad', idle: !appSamples('aurora_read'),
                idleText: 'no reads served by Aurora this interval',
                p50: app && app.aurora_read_p50, p90: app && app.aurora_read_p90,
                p99: app && app.aurora_read_p99 },
              { label: 'write', tone: 'bad', idle: !appSamples('aurora_write'),
                idleText: 'no writes this interval',
                p50: app && app.aurora_write_p50, p90: app && app.aurora_write_p90,
                p99: app && app.aurora_write_p99 },
            ],
          }}
        />

        <Resource
          title={'Target \u00b7 Valkey'} state={target.state} label={target.label}
          error={target.error}
          subtitle={(target.endpoint || 'target')
            + (target.version ? ` \u00b7 v${target.version}` : '')
            + (target.shards ? ` \u00b7 ${target.shards} shard` : '')
            + (target.tls ? ' \u00b7 TLS' : '')}
          usage={[
            { label: <>engine CPU <span className="pm-cw">{cwAge(tcw.age_sec)}</span></>,
              value: tcw.engine_cpu_pct, warn: 70, bad: 90,
              sub: 'single-threaded hot path, so this is one core' },
          ]}
          rows={[
            // Split rather than one ops/s: reads are the application being
            // served, writes are the sink applying CDC. One number hides which
            // of the two is busy, and in a DB-only phase that is exactly what matters.
            { label: <>reads/s <span className="pm-cw">live</span></>,
              value: compact(target.reads_per_sec) },
            { label: <>writes/s <span className="pm-cw">{'live \u00b7 from the sink'}</span></>,
              value: compact(target.writes_per_sec) },
            { label: <>read latency <span className="pm-cw">live avg</span></>,
              value: us(target.read_latency_us) },
            { label: <>write latency <span className="pm-cw">live avg</span></>,
              value: us(target.write_latency_us) },
            { label: <>keys stored <span className="pm-cw">live</span></>,
              value: compact(target.keys) },
          ]}
          percentiles={{
            title: <>latency <span className="pm-cw">app-measured, this interval</span></>,
            lines: [
              { label: 'read', tone: 'good', idle: !appSamples('valkey_read'),
                idleText: 'no reads served by Valkey this interval',
                p50: app && app.valkey_read_p50, p90: app && app.valkey_read_p90,
                p99: app && app.valkey_read_p99 },
            ],
          }}
        />
      </div>

      {/* ── 2a. THE PIPELINE BETWEEN THE STORES ────────────────────────────
          Debezium, the broker and the sink as full cards, always visible, in
          flow order, so all five stages read the same way rather than the
          middle three being reduced to chips. Each degrades to "unreachable"
          on a read error, exactly like the store cards. */}
      <div className="pm-resources pm-resources-3">
        <Resource
          title={'Capture \u00b7 Debezium'} state={debezium.state}
          label={debezium.label} error={debezium.error}
          subtitle={debezium.service || 'Debezium (ECS Fargate)'}
          usage={[]}
          rows={[
            { label: <>tasks <span className="pm-cw">running / desired</span></>,
              value: `${debezium.running ?? dash}/${debezium.desired ?? dash}` },
            { label: <>capture lag <span className="pm-cw">binlog {'\u2192'} stream</span></>,
              value: ms(debezium.capture_lag_ms) },
            { label: <>restarts <span className="pm-cw">last 2h</span></>,
              value: num((debezium.restarts_2h || {}).restarts) },
            { label: <>binlog position <span className="pm-cw">committed</span></>,
              value: debezium.offset_binlog_file
                ? <span className="pm-mono">{debezium.offset_binlog_file}
                    :{debezium.offset_binlog_pos ?? '?'}</span>
                : dash },
          ]}
          note={debezium.capture_note || null}
        />

        <Resource
          title={'Broker \u00b7 Valkey Streams'} state={broker.state}
          label={broker.label} error={broker.error}
          subtitle={broker.endpoint || 'broker stream (Valkey)'}
          usage={[]}
          rows={[
            { label: <>produced <span className="pm-cw">events/s</span></>,
              value: compact(broker.produced_per_sec) },
            { label: <>backlog <span className="pm-cw">queued, not applied</span></>,
              value: compact(broker.backlog) },
            { label: <>ops/s <span className="pm-cw">live</span></>,
              value: compact(broker.ops_per_sec) },
            { label: <>clients <span className="pm-cw">connected</span></>,
              value: num(broker.connected_clients) },
            { label: <>memory <span className="pm-cw">used</span></>,
              value: broker.used_memory || dash },
          ]}
        />

        <Resource
          title={'Sink \u00b7 ECS'} state={sink.state}
          label={sink.label} error={sink.error}
          subtitle={sink.service || 'Go sink (ECS Fargate)'}
          usage={[]}
          rows={[
            { label: <>tasks <span className="pm-cw">running / desired</span></>,
              value: `${sink.running ?? dash}/${sink.desired ?? dash}` },
            { label: <>applied <span className="pm-cw">writes/s to Valkey</span></>,
              value: compact(sink.applied_per_sec) },
            { label: <>pending <span className="pm-cw">delivered, unacked</span></>,
              value: sink.pending === null || sink.pending === undefined
                ? dash : compact(sink.pending) },
            { label: <>consumers <span className="pm-cw">in group</span></>,
              value: sink.consumers === null || sink.consumers === undefined
                ? dash : num(sink.consumers) },
            { label: <>restarts <span className="pm-cw">last 2h</span></>,
              value: num((sink.restarts_2h || {}).restarts) },
          ]}
        />
      </div>

      {/* Aggregates for this phase's own window - the set you compare between
          tabs, rather than comparing two charts by eye. */}
      <Summary run={phaseRun} />

      {/* ── 2b. DATA ACTUALLY MOVING ───────────────────────────────────────
          Two different paths, so two different views. A bulk load writes to
          Valkey directly and shows up in the left panel; ordinary writes go
          Aurora -> CDC -> Valkey and show up on the right. Only one of them is
          usually busy, which is itself useful information. */}
      <div className="pm-flowpair">
        <BulkFlow bulk={bulkload} valkeyWrites={target.writes_per_sec} />
      </div>

      {/* ── 3. HISTORY, ONE COLUMN PER SOURCE ──────────────────────────────
          Kept separate on purpose. Aurora and Valkey differ by two or three
          orders of magnitude, so putting them on one axis flattens the faster
          one into the baseline and the chart no longer shows useful information. */}
      <div className="pm-wide pm-history">
        {/* Whose trends these are. The sampler is a background thread with no
            request and therefore no link selection, so it follows the configured
            pipeline only. Showing its points under a chosen link's name without
            saying so would put one pipeline's history in another's dashboard --
            the exact confusion the selector exists to remove. */}
        {trend && trend.scope_note && (
          <div className="pm-scopewarn">{trend.scope_note}</div>
        )}
        <header>
          <h2>History</h2>
          <span className="pm-res">
            {trend && Array.isArray(trend.points)
              ? `${trend.points.length} points`
                + (trend.samples ? ` from ${trend.samples} samples` : '')
                + (trend.interval_sec ? ` \u00b7 every ${trend.interval_sec}s` : '')
                + (trend.retention_minutes ? ` \u00b7 keeps ${trend.retention_minutes} min` : '')
                + (trend.persisted ? ' \u00b7 on disk' : '')
              : 'loading\u2026'}
          </span>
          <span className="pm-trend-range">
            {[15, 60, 120].map((m) => (
              <button key={m} type="button"
                className={'pm-range-btn' + (trendMinutes === m ? ' pm-range-on' : '')}
                onClick={() => setTrendMinutes(m)}>{m}m</button>
            ))}
          </span>
        </header>

        <div className="pm-hist-cols">
          <div className="pm-hist-col">
            <div className="pm-hist-head pm-hist-head-db">Source &middot; Aurora</div>
            <Chart title="CPU" unit="pct" points={trend && trend.points}
              series={[{ key: 'aurora_cpu_pct', label: 'CPU', color: '#e74c3c' }]} />
            <Chart title="Throughput" unit="n" points={trend && trend.points}
              series={[
                { key: 'aurora_reads_per_sec', label: 'reads/s', color: '#e74c3c' },
                { key: 'aurora_writes_per_sec', label: 'writes/s', color: '#FF9900' },
              ]} />
            <Chart title="Read latency" unit="ms" points={trend && trend.points}
              series={[
                { key: 'aurora_read_ms', label: 'server avg', color: '#e74c3c' },
                { key: 'aurora_read_p50', label: 'p50', color: '#95a5a6' },
                { key: 'aurora_read_p90', label: 'p90', color: '#e67e22' },
                { key: 'aurora_read_p99', label: 'p99', color: '#c0392b' },
              ]}
              hint="server average from CloudWatch (SelectLatency), always present;
                    p50/p90/p99 are drawn only while the app reports read samples" />
            <Chart title="Write latency" unit="ms" points={trend && trend.points}
              series={[
                { key: 'aurora_write_ms', label: 'server avg', color: '#FF9900' },
                { key: 'aurora_write_p50', label: 'p50', color: '#95a5a6' },
                { key: 'aurora_write_p90', label: 'p90', color: '#e67e22' },
                { key: 'aurora_write_p99', label: 'p99', color: '#c0392b' },
              ]}
              hint="server average from CloudWatch (DMLLatency), always present;
                    p50/p90/p99 are drawn only while the app reports write samples" />
            <Chart title="Memory (freeable)" unit="n" points={trend && trend.points}
              series={[{ key: 'aurora_freeable_mb', label: 'freeable MB', color: '#8e44ad' }]}
              hint="RDS FreeableMemory; less freeable means more memory in use" />
            <Chart title="Connections" unit="n" points={trend && trend.points}
              series={[{ key: 'aurora_connections', label: 'open', color: '#FF9900' }]}
              hint="open connections to Aurora (live)" />
            <Chart title="New connections/s" unit="n" points={trend && trend.points}
              series={[{ key: 'aurora_new_conn_per_sec', label: 'new/s', color: '#d35400' }]}
              hint="rate of new connections opened to Aurora" />
          </div>

          <div className="pm-hist-col">
            <div className="pm-hist-head pm-hist-head-cache">Target &middot; Valkey</div>
            <Chart title="Engine CPU" unit="pct" points={trend && trend.points}
              series={[{ key: 'valkey_engine_cpu_pct', label: 'engine CPU', color: '#27ae60' }]} />
            <Chart title="Throughput" unit="n" points={trend && trend.points}
              series={[
                { key: 'valkey_reads_per_sec', label: 'reads/s', color: '#27ae60' },
                { key: 'valkey_writes_per_sec', label: 'writes/s (sink)', color: '#3498db' },
              ]} />
            <Chart title="Read latency" unit="ms" points={trend && trend.points}
              series={[
                { key: 'valkey_read_p50', label: 'p50', color: '#95a5a6' },
                { key: 'valkey_read_p90', label: 'p90', color: '#16a085' },
                { key: 'valkey_read_p99', label: 'p99', color: '#1e8449' },
              ]}
              hint="only drawn while Valkey is serving reads" />
            <Chart title="Write latency" unit="us" points={trend && trend.points}
              series={[{ key: 'valkey_write_us', label: 'server average', color: '#3498db' }]}
              hint="average, not percentiles: the app never writes to the cache, so
                    there is no client-side histogram, and ElastiCache does not
                    expose server-side percentiles" />
            <Chart title="Memory used" unit="pct" points={trend && trend.points}
              series={[{ key: 'valkey_memory_pct', label: 'used %', color: '#8e44ad' }]}
              hint="used memory as a percentage of maxmemory" />
            <Chart title="Connections" unit="n" points={trend && trend.points}
              series={[{ key: 'valkey_connections', label: 'clients', color: '#27ae60' }]}
              hint="connected clients (live)" />
            <Chart title="New connections/s" unit="n" points={trend && trend.points}
              series={[{ key: 'valkey_new_conn_per_sec', label: 'new/s', color: '#16a085' }]}
              hint="rate of new client connections to the cache" />
          </div>
        </div>

        <div className="pm-hist-cdc">
          <div className="pm-hist-head pm-hist-head-cdc">Pipeline &middot; CDC</div>
          <div className="pm-hist-cdc-grid">
            <Chart title="CDC lag" unit="ms" points={trend && trend.points} stat="p95"
              series={[{ key: 'cdc_lag_ms', label: 'lag', color: '#FF9900' }]}
              hint="headline is p95 tail latency; a rare spike (e.g. a synthetic burst) clamps to the top and is not the p95. Gaps are phases where CDC was not in the path." />
          </div>
        </div>

        {trend && trend.marks && trend.marks.length ? (
          <div className="pm-foot">
            {trend.marks.map((m) => (
              <span key={`${m.t}-${m.label}`}>
                <b>{new Date(m.t * 1000).toLocaleTimeString()}</b> {m.label}
              </span>
            ))}
          </div>
        ) : null}
      </div>

      {/* Data flowing, one block per table: a DMS-style live counter strip
          (rows read vs applied, both CDC change-events) that shows at a glance
          whether the table is caught up, plus a few sample keys comparing the
          value at the source against the value at the target. */}
      <div className="pm-wide pm-dataflow">
        <header>
          <h2>Data flowing</h2>
          <span className="pm-res">per table: rows read vs applied (CDC), and sample keys source vs target</span>
          <span className="pm-trend-range">
            <button className={'pm-sample-btn' + (sampleMode === 'newest' ? ' pm-sample-on' : '')}
              onClick={() => setSampleMode('newest')}>newest</button>
            <button className={'pm-sample-btn' + (sampleMode === 'random' ? ' pm-sample-on' : '')}
              onClick={() => setSampleMode('random')}>random</button>
            {sampleMode === 'random' ? (
              <button className="pm-sample-btn" disabled={sampling}
                onClick={() => setRandomNonce((n) => n + 1)}>
                {sampling ? 'sampling\u2026' : 're-sample'}
              </button>
            ) : null}
          </span>
        </header>
        {flowErr ? <div className="pm-error">{flowErr}</div> : null}
        {shownFlow.error ? <div className="pm-error">{shownFlow.error}</div> : null}
        {shownFlow.cache_error ? <div className="pm-error">{shownFlow.cache_error}</div> : null}
        <div className="pm-totals">
          <span>tables <b>{num(flowTables.length)}</b></span>
          <span className={caughtCount === flowTables.length ? '' : 'pm-warn'}>
            caught up <b>{num(caughtCount)}/{num(flowTables.length)}</b>
          </span>
          <button className="pm-recon-btn" onClick={reconcile} disabled={reconciling}>
            {reconciling ? 'counting\u2026' : 'Reconcile exactly'}
          </button>
          {sampleMode === 'random' && randomAt
            ? <span className="pm-cw">sampled {randomAt}</span> : null}
        </div>
        {recon ? (
          <div className={'pm-recon'
            + (recon.error ? ' pm-recon-bad'
              : recon.matched ? ' pm-recon-ok' : ' pm-recon-bad')}>
            {recon.error ? <span>reconcile failed: {recon.error}</span> : (
              <>
                <span>
                  <b>{num(recon.aurora_rows)}</b> Aurora rows vs{' '}
                  <b>{num(recon.product_keys)}</b>{' '}
                  <code>{shownFlow.key_prefix || 'products:'}</code> keys{' \u2014 '}
                  {recon.matched ? 'exact match'
                    : `off by ${num(recon.difference)}`}
                </span>
                <span className="pm-cw">
                  {num(recon.other_total)} keys belong to other families
                  {Object.keys(recon.other_families || {}).length
                    ? `: ${Object.entries(recon.other_families)
                      .map(([k, v]) => `${k} ${compact(v)}`).join(', ')}`
                    : ''}
                  {' \u00b7 '}scanned every key in {recon.scan_sec}s
                </span>
              </>
            )}
          </div>
        ) : null}

        {flowTables.map((t) => (
          <div className="pm-flowtable" key={t.table}>
            <div className="pm-flowtable-head">
              <span className="pm-flowtable-name">{t.table}</span>
              <span className={'pm-flowbadge ' + (t.caught ? 'pm-flowbadge-ok' : 'pm-flowbadge-wait')}>
                {t.caught ? '\u25cf caught up' : `\u25d0 ${num(t.backlog)} behind`}
              </span>
            </div>
            <div className="pm-flowctr">
              <div className="pm-flowctr-cell">
                <div className="pm-flowctr-lbl">rows read (CDC)</div>
                <div className="pm-flowctr-num">{num(t.read)}</div>
                <div className={'pm-flowctr-rate' + (t.readRate > 0 ? '' : ' pm-flowctr-idle')}>
                  {flowRate(t.readRate)}</div>
              </div>
              <div className="pm-flowctr-cell">
                <div className="pm-flowctr-lbl">rows applied (CDC)</div>
                <div className={'pm-flowctr-num' + (t.caught ? '' : ' pm-flowctr-behind')}>
                  {num(t.applied)}</div>
                <div className={'pm-flowctr-rate' + (t.appliedRate > 0 ? '' : ' pm-flowctr-idle')}>
                  {flowRate(t.appliedRate)}</div>
              </div>
            </div>
            {t.hasRows ? (
              <table className="pm-table pm-flow-rows">
                <thead>
                  <tr>
                    <th>PK</th>
                    <th className="pm-flow-src">value at source &middot; Aurora</th>
                    <th className="pm-flow-tgt">value at target &middot; Valkey</th>
                  </tr>
                </thead>
                <tbody>
                  {t.rows.map((r) => (
                    <tr key={r.id} className={r.state === 'synced' ? '' : 'pm-row-' + r.state}>
                      <td className="pm-pk">
                        <span className={'pm-dot pm-dot-' + r.state} />{num(r.id)}
                      </td>
                      <td className="pm-flowval">{flowVal(r, 'db_') || dash}</td>
                      <td className={'pm-flowval' + (r.state === 'synced' ? '' : ' pm-flowval-' + r.state)}>
                        {flowVal(r, 'cache_') || dash}
                      </td>
                    </tr>
                  ))}
                  {!t.rows.length ? (
                    <tr><td colSpan={3} className="pm-empty">
                      {sampling ? 'sampling\u2026' : 'no cached rows in this sample'}
                    </td></tr>
                  ) : null}
                </tbody>
              </table>
            ) : (
              <div className="pm-note-inline">
                sample keys are shown for the selected table; counters above are live for this one.
              </div>
            )}
          </div>
        ))}

        <div className="pm-note-inline">
          the sample shows the newest rows that are in the cache, source value vs target
          value. Rows a transform filter excludes are not shown here &mdash; completeness is
          proven by <b>rows read = rows applied</b> (the sink applied every captured change)
          and delivery integrity, not by a sample row. Use Reconcile for an exact full-table
          vs cache-keys count.
        </div>
      </div>

      {detailsOpen ? (
      <>
      {/* ── 4. DETAIL, ON DEMAND ───────────────────────────────────────────
          Everything diagnostic. Opens automatically if anything is failing. */}

      {/* The CDC lag probe. Hidden from the banner in phases that serve nothing
          from the cache, but still available here on any phase. */}
      {!cdcShown ? (
        <div className="pm-wide pm-detail-strip">
          <header>
            <h2>CDC lag</h2>
            <span className="pm-res">not part of this phase, measure on demand</span>
          </header>
          <div className="pm-foot pm-cdc-ondemand">
            <span>
              last measured <b>{flow.cdc_lag_ms === null || flow.cdc_lag_ms === undefined
                ? dash : ms(flow.cdc_lag_ms)}</b>
              {flow.lag_source ? <span className="pm-cw"> {flow.lag_source}</span> : null}
            </span>
            <span>
              <button className="pm-probe-btn" onClick={probe} disabled={probing}>
                {probing ? 'measuring\u2026' : 'Measure now'}
              </button>
            </span>
            {probeError ? <span className="pm-run-err">{probeError}</span> : null}
          </div>
        </div>
      ) : null}

      {/* Extra store detail that does not belong on the summary cards. */}
      <div className="pm-wide pm-detail-strip">
        <header><h2>Store detail</h2></header>
        <div className="pm-foot">
          <span>Aurora SELECT latency <b>{ms(cw.select_latency_ms)}</b>{' '}
            <span className="pm-cw">{cwAge(cw.age_sec)}</span></span>
          <span>Aurora DML latency <b>{ms(cw.dml_latency_ms)}</b></span>
          <span>Aurora storage read <b>{ms(cw.storage_read_latency_ms)}</b>{' '}
            <span className="pm-cw">0 = served from buffer pool</span></span>
          <span>binlog position <b className="pm-mono">{aurora.binlog || dash}</b></span>
          <span>Valkey GET cmds <b>{us(tcw.get_cmds_latency_us)}</b></span>
          <span>Valkey SET cmds <b>{us(tcw.set_cmds_latency_us)}</b></span>
          {/* Off the main card, but not dropped: memory decides whether the
              broker starts evicting, which is a correctness question. */}
          <span>Valkey memory <b>{target.used_pct === null || target.used_pct === undefined
            ? dash : `${target.used_pct}%`}</b>{' '}
            <span className="pm-cw">
              {target.used_memory || '?'} of {target.maxmemory || '?'}
            </span>
          </span>
          <span>Valkey hit rate <b>{pct(target.hit_rate_pct)}</b></span>
          <span>Valkey clients <b>{num(target.connected_clients)}</b></span>
          {target.keys_added_per_sec
            ? <span>keys added <b>+{compact(target.keys_added_per_sec)}/s</b></span> : null}
        </div>
        {target.latency_note
          ? <div className="pm-note-inline">{target.latency_note}</div> : null}
      </div>

      {/* The components, in order, plus the integrity accounting. */}
      <section className="pm-section pm-section-pipeline">
      <div className={'pm-wide pm-integ pm-integ-' + (integrity.verdict || 'ok')}>
        <header>
          <h2>Delivery integrity</h2>
          <span className={'pm-pill ' + stateClass(integrity.state)}>
            {VERDICT_WORDS[integrity.verdict] || integrity.label || 'unknown'}
          </span>
          <span className="pm-integ-headline">{integrity.headline || ''}</span>
        </header>
        {integrity.error ? <div className="pm-error">{integrity.error}</div> : null}
        <div className="pm-quad">
          <BigNum value={num(integrity.dead_lettered)} label="dead-lettered"
            tone={integrity.dead_lettered ? 'bad' : null}
            sub={integrity.dead_lettered ? 'gave up after retries' : 'none, ever'} />
          <BigNum value={num(integrity.evicted_keys)} label="evicted by broker"
            tone={integrity.evicted_keys ? 'bad' : null}
            sub={integrity.maxmemory_policy || null} />
          <BigNum value={num(integrity.backlog)} label="queued, not yet applied"
            tone={integrity.verdict === 'stalled' ? 'bad' : null}
            sub={integrity.drained_per_sec === null || integrity.drained_per_sec === undefined
              ? 'drain rate unknown yet'
              : `draining ${compact(integrity.drained_per_sec)}/s`} />
          <BigNum value={num(integrity.retrying)} label="retrying now"
            tone={integrity.retrying ? 'warn' : null}
            sub={integrity.max_attempt_seen
              ? `worst attempt ${integrity.max_attempt_seen} of ${integrity.max_deliveries}`
              : 'no entry awaiting retry'} />
        </div>
        <div className="pm-foot">
          <span>oldest unacked <b>{ms(integrity.oldest_unacked_ms)}</b></span>
          <span>broker memory <b>{integrity.broker_used_pct === null
            || integrity.broker_used_pct === undefined
            ? dash : integrity.broker_used_pct + '%'}</b>{' '}
            {integrity.broker_used_memory
              ? `(${integrity.broker_used_memory} / ${integrity.broker_maxmemory || '?'})` : ''}
          </span>
          <span>
            consumers <b>{num(integrity.consumers_active)}</b> active
            {' '}of {num(integrity.consumers_registered)} registered
          </span>
          {integrity.binlog_retention_hours !== undefined
            && integrity.binlog_retention_hours !== null ? (
              <span>binlog retention <b>{integrity.binlog_retention_hours}h</b>{' '}
                <span className="pm-cw">hard loss boundary</span>
              </span>
            ) : null}
          {integrity.binlog_retention_note
            ? <span className="pm-cw">{integrity.binlog_retention_note}</span> : null}
          {integrity.eviction_note
            ? <span className="pm-cw">{integrity.eviction_note}</span> : null}
        </div>
        {(integrity.retry_sample || []).length ? (
          <table className="pm-table">
            <thead>
              <tr>
                <th>entry being retried</th><th>consumer</th>
                <th className="pm-num">attempt</th><th className="pm-num">idle</th>
              </tr>
            </thead>
            <tbody>
              {integrity.retry_sample.map((r) => (
                <tr key={r.id} className="pm-row-differs">
                  <td>{r.id}</td>
                  <td>{r.consumer}</td>
                  <td className="pm-num">{r.attempt} of {r.of_max}</td>
                  <td className="pm-num">{ms(r.idle_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {(integrity.dlq_sample || []).length ? (
          <table className="pm-table">
            <thead>
              <tr>
                <th>dead-lettered entry</th><th>reason</th>
                <th className="pm-num">deliveries</th><th className="pm-num">age</th>
              </tr>
            </thead>
            <tbody>
              {integrity.dlq_sample.map((r) => (
                <tr key={r.id} className="pm-row-pending">
                  <td>{r.id}</td>
                  <td>{r.reason || dash}</td>
                  <td className="pm-num">{num(r.deliveries)}</td>
                  <td className="pm-num">{ms(r.age_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {integrity.dead_lettered ? (
          <div className="pm-note-inline">
            Dead-lettered entries are in <code>{integrity.dlq_key}</code> on the broker.
            They are not lost from Aurora — Aurora remains authoritative — but the cache will
            not reflect them until they are replayed.
          </div>
        ) : null}
      </div>

      {/* Per table. Each table in a link gets its own broker stream, so the link
          can be caught up on one and hours behind on another — the cards above
          are the TOTAL, which is the wrong number for "which table is the
          problem". Shown when there is more than one table, or when any single
          one is unhealthy. */}
      {tableRows.length > 1 || (tableRows.length && perTable.worst_verdict
        && perTable.worst_verdict !== 'ok') ? (
        <div className="pm-wide">
          <header>
            <h2>Per table</h2>
            <span className="pm-res">
              {perTable.count} table{perTable.count === 1 ? '' : 's'} in this link
              {perTable.worst_verdict && perTable.worst_verdict !== 'ok'
                ? ` \u00b7 worst: ${VERDICT_WORDS[perTable.worst_verdict]
                  || perTable.worst_verdict}` : ''}
            </span>
          </header>
          <table className="pm-table">
            <thead>
              <tr>
                <th>table</th>
                <th className="pm-num">events/s</th>
                <th className="pm-num">applied/s</th>
                <th className="pm-num">backlog</th>
                <th className="pm-num">dead-lettered</th>
                <th className="pm-num">retrying</th>
                <th className="pm-num">wait</th>
                <th>integrity</th>
              </tr>
            </thead>
            <tbody>
              {tableRows.map((t) => (
                <tr key={t.stream}
                    className={t.verdict && t.verdict !== 'ok' ? 'pm-row-differs' : ''}>
                  <td>
                    <b>{t.table || dash}</b>
                    <div className="pm-cw pm-mono">{t.stream}</div>
                    {t.stream_error
                      ? <div className="pm-bad">{t.stream_error}</div> : null}
                    {t.group_note
                      ? <div className="pm-cw">{t.group_note}</div> : null}
                  </td>
                  <td className="pm-num">{compact(t.produced_per_sec)}</td>
                  <td className="pm-num">{compact(t.applied_per_sec)}</td>
                  <td className={'pm-num' + (t.backlog ? ' pm-bad' : '')}>
                    {num(t.backlog)}
                  </td>
                  <td className={'pm-num' + (t.dead_lettered ? ' pm-bad' : '')}>
                    {num(t.dead_lettered)}
                  </td>
                  <td className="pm-num">{num(t.retrying)}</td>
                  <td className="pm-num">{ms(t.broker_wait_ms)}</td>
                  <td className={'pm-state pm-state-'
                    + (t.verdict === 'ok' ? 'synced' : 'differs')}>
                    {VERDICT_WORDS[t.verdict] || t.verdict || dash}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="pm-note-inline">{perTable.note}</div>
        </div>
      ) : null}

      <div className="pm-grid pm-grid-3">
        <Card
          icon={'\u2461'} title="Debezium Server" data={debezium}
          resource={`${debezium.service || 'debezium'} \u00b7 ECS Fargate`}
          bignums={[
            <BigNum key="t" value={`${debezium.running ?? dash}/${debezium.desired ?? dash}`}
              label="tasks running" tone={hops.debezium === 'bad' ? 'bad' : null} />,
            <BigNum key="c" value={ms(debezium.capture_lag_ms)} label="capture lag" />,
          ]}
          rows={[
            { label: 'uptime', value: duration(debezium.uptime_sec) },
            { label: 'offset committed', value: debezium.offset_age_sec !== undefined
              ? `${debezium.offset_age_sec}s ago` : dash },
            { label: <>binlog gap <span className="pm-cw">trend, not value</span></>,
              value: debezium.binlog_gap_bytes === undefined
                || debezium.binlog_gap_bytes === null
                ? dash
                : `${bytes(debezium.binlog_gap_bytes)} ${trendArrow(debezium.binlog_gap_trend_bytes_per_sec)}`,
              tone: debezium.binlog_gap_trend_bytes_per_sec > 0 ? 'warn' : null },
            { label: 'offset position', value: debezium.offset_binlog || dash, mono: true },
            { label: 'log errors (5m)', value: num(debezium.log_errors),
              tone: debezium.log_errors ? 'bad' : null },
            { label: 'restarts (2h)',
              value: debezium.restarts_2h ? num(debezium.restarts_2h.restarts) : dash,
              tone: debezium.restarts_2h && debezium.restarts_2h.restarts ? 'bad' : null },
            ...(debezium.restarts_2h && debezium.restarts_2h.restarts ? [{
              label: 'last restart',
              value: `${Math.round(debezium.restarts_2h.last_restart_age_sec)}s ago`
                + (debezium.restarts_2h.last_restart_reason
                   ? ` · ${debezium.restarts_2h.last_restart_reason}` : ''),
              tone: 'bad' }] : []),
          ]}
          note={debezium.capture_note || debezium.offset_note}
        />

        <Card
          icon={'\u2462'} title="Broker Stream" data={broker}
          resource={broker.stream || 'broker stream'}
          bignums={[
            <BigNum key="p" value={compact(broker.produced_per_sec)} label="events/s"
              spark={<Spark values={history.current['broker.produced']} color="#9b59b6" />} />,
            <BigNum key="b" value={compact(broker.backlog)} label="backlog"
              tone={broker.state === 'warn' ? 'warn' : null}
              spark={<Spark values={history.current['broker.backlog']}
                color={broker.state === 'warn' ? '#e74c3c' : '#95a5a6'} />} />,
          ]}
          rows={[
            { label: 'entries added', value: num(broker.entries_added), mono: true },
            { label: 'consumer group lag', value: num(broker.group_lag),
              tone: broker.group_lag ? 'bad' : null },
            { label: 'stream length', value: num(broker.length) },
            { label: 'oldest entry', value: ms(broker.oldest_entry_age_ms) },
            { label: 'broker memory', value: broker.used_memory || dash },
          ]}
          note={broker.group_note}
        />

        <Card
          icon={'\u2463'} title="Sink Workers" data={sink}
          resource={`${sink.service || 'sink'} \u00b7 ECS Fargate`}
          bignums={[
            <BigNum key="t" value={`${sink.running ?? dash}/${sink.desired ?? dash}`}
              label="tasks running" tone={hops.sink === 'bad' ? 'bad' : null} />,
            <BigNum key="a" value={compact(sink.applied_per_sec)} label="applied/s"
              tone={hops.sink === 'bad' ? 'bad' : null}
              spark={<Spark values={history.current['sink.applied']} color="#27ae60" />} />,
          ]}
          rows={[
            { label: 'consumers attached', value: num(sink.consumers),
              tone: sink.consumers === 0 ? 'bad' : null },
            { label: 'consumer group', value: sink.group || dash, mono: true },
            { label: 'pending entries', value: num(sink.pending) },
            { label: 'oldest entry wait', value: ms(sink.oldest_pending_age_ms),
              tone: hops.sink === 'bad' ? 'bad' : null },
            { label: 'uptime', value: duration(sink.uptime_sec) },
            { label: 'log errors (5m)', value: num(sink.log_errors),
              tone: sink.log_errors ? 'bad' : null },
            { label: 'restarts (2h)',
              value: sink.restarts_2h ? num(sink.restarts_2h.restarts) : dash,
              tone: sink.restarts_2h && sink.restarts_2h.restarts ? 'bad' : null },
            ...(sink.restarts_2h && sink.restarts_2h.restarts ? [{
              label: 'last restart',
              value: `${Math.round(sink.restarts_2h.last_restart_age_sec)}s ago`
                + (sink.restarts_2h.last_restart_reason
                   ? ` · ${sink.restarts_2h.last_restart_reason}` : ''),
              tone: 'bad' }] : []),
          ]}
        />
      </div>
      </section>

      {/* The lines behind the "log errors" counts on the cards above. Its own
          component and its own fetch rhythm: this page polls every 2s, and a
          CloudWatch Logs call per tick would be slow and billable for no gain. */}
      <ComponentLogs apiUrl={API_URL} link={watching} />

      <p className="pm-page-note">
        Values marked <span className="pm-cw">60s avg</span> come from CloudWatch and lag the live
        counters, which is why they carry their age. <b>CDC lag</b> is labelled with its source:{' '}
        <span className="pm-src-inline">in-flight</span> = observed from events still queued
        (capture + broker wait), available only while the sink is behind;{' '}
        <span className="pm-src-inline">probe</span> = measured end to end by stamping one
        dedicated heartbeat row in Aurora and timing its arrival in the cache. A healthy sink
        drains the stream to empty, so there is normally nothing in flight to observe — that is
        why the probe exists. Everything else on this page is read-only.
      </p>
      </>
      ) : null}
    </div>
  );
}

export default PipelineMonitor;
