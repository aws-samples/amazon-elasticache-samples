import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import './ComponentLogs.css';

/*
 * ComponentLogs — the per-component log view for one pipeline.
 *
 * Kept out of PipelineMonitor.js on purpose. That file is already ~1800 lines and
 * polls every 2 seconds; reading logs is a different access pattern (expensive, on
 * demand, paginated) and mixing the two would mean every dashboard refresh also made
 * a CloudWatch Logs call.
 *
 * Why this exists at all: for two of the three components the log IS the only
 * telemetry. Debezium Server publishes no metrics endpoint, and the Go sink's
 * counters are served on a port inside awsvpc that this control plane cannot
 * reach. Before this panel the entire UI surface for either was a single number,
 * "log errors (5m)", with no way to see what the errors said.
 *
 * The backend never accepts a log group name — only a component from an
 * allowlist — so nothing here can be pointed at an arbitrary group.
 */

const RANGES = [
  { minutes: 5, label: '5m' },
  { minutes: 15, label: '15m' },
  { minutes: 30, label: '30m' },
  { minutes: 60, label: '1h' },
  { minutes: 240, label: '4h' },
  { minutes: 1440, label: '24h' },
  { minutes: 4320, label: '3d' },
  { minutes: 10080, label: '7d' },
];

const FOLLOW_MS = 5000;
const COMPONENT_LABEL = {
  debezium: 'Debezium',
  sink: 'Sink',
  loader: 'Loader',
};

// Wording per state. These are deliberately different sentences: an absent log
// group, a denied read and a quiet log all produce an empty list, and collapsing
// them into "no logs" is how a permissions problem gets read as a healthy
// pipeline.
function stateMessage(payload, component) {
  const state = payload && payload.state;
  if (state === 'absent') {
    return {
      tone: 'idle',
      title: 'Nothing has run yet',
      detail: payload.note
        || `No log group for the ${component} yet, so its task has never started.`,
    };
  }
  if (state === 'denied') {
    return {
      tone: 'bad',
      title: 'Not allowed to read these logs',
      detail: payload.note
        || 'This control plane is missing logs:FilterLogEvents on that group.',
    };
  }
  if (state === 'throttled') {
    return {
      tone: 'warn',
      title: 'CloudWatch is throttling',
      detail: 'Too many log requests. Wait a moment and try again.',
    };
  }
  if (state === 'error') {
    return { tone: 'bad', title: 'Could not read the logs', detail: payload.error };
  }
  if (state === 'empty') {
    return {
      tone: 'idle',
      title: 'No matching lines',
      detail: payload.note || 'Nothing was logged in this window.',
    };
  }
  return null;
}

function clockOf(ts) {
  if (!ts) return '';
  try {
    return new Date(ts).toLocaleTimeString();
  } catch (e) {
    return '';
  }
}

function StateChip({ state }) {
  const tone = {
    ok: 'ok', empty: 'idle', absent: 'idle',
    denied: 'bad', error: 'bad', throttled: 'warn',
  }[state] || 'idle';
  return <span className={`cl-chip cl-chip-${tone}`}>{state || 'unknown'}</span>;
}

/* The counters and signals the backend could derive from the lines. Rendered
   above the log body so "what happened" is answerable without reading it all. */
function Activity({ activity }) {
  if (!activity || !activity.levels) return null;
  const { levels, signals, counters, not_in_logs: notInLogs } = activity;
  const seen = signals ? signals.filter((s) => s.matches > 0) : [];
  return (
    <div className="cl-activity">
      <div className="cl-act-row">
        <span className="cl-act-label">
          in the last {activity.window_min}m
          {activity.lines_scanned !== undefined
            && ` \u00b7 ${activity.lines_scanned} line(s) scanned`}
          {activity.truncated && ' (capped)'}
        </span>
        <span className={`cl-lv cl-lv-error${levels.error ? ' is-hot' : ''}`}>
          {levels.error} error
        </span>
        <span className={`cl-lv cl-lv-warn${levels.warn ? ' is-hot' : ''}`}>
          {levels.warn} warn
        </span>
        <span className="cl-lv">{levels.info} info</span>
      </div>

      {/* Two shapes, because the two components report different things: the
          sink counts writes it applied, Debezium counts records it captured. */}
      {counters && counters.observed ? (
        <div className="cl-act-row cl-act-counters">
          <span className="cl-act-label">
            {counters.records_sent !== undefined
              ? 'reported by the connector itself'
              : 'reported by the sink itself'}
          </span>
          {counters.records_sent !== undefined ? (
            <>
              <span className="cl-kv">
                <b>{Number(counters.records_sent).toLocaleString()}</b> records captured
              </span>
              <span className="cl-kv cl-dim">
                {`across ${counters.intervals} reported interval(s)`}
              </span>
            </>
          ) : (
            <>
              <span className="cl-kv"><b>{Number(counters.writes).toLocaleString()}</b> writes</span>
              <span className="cl-kv"><b>{Number(counters.deletes).toLocaleString()}</b> deletes</span>
              <span className="cl-kv"><b>{Number(counters.rate_per_sec).toLocaleString()}</b>/s</span>
              {counters.per_cycle && (
                <span className="cl-kv cl-dim">
                  {`per cycle: read ${counters.per_cycle.read} \u00b7 write `
                   + `${counters.per_cycle.write} \u00b7 batch ${counters.per_cycle.batch}`}
                </span>
              )}
            </>
          )}
        </div>
      ) : null}
      {counters && !counters.observed ? (
        <div className="cl-act-row">
          <span className="cl-act-note">{counters.note}</span>
        </div>
      ) : null}

      {seen.length ? (
        <div className="cl-act-row cl-act-signals">
          {seen.map((s) => (
            <span key={s.signal} className="cl-sig" title={s.newest || s.means}>
              {s.signal.replace(/_/g, ' ')} <b>{s.matches}</b>
            </span>
          ))}
        </div>
      ) : null}

      {/* Named explicitly, so nobody searches the log for a number that is not
          there. The sink's retry and dead-letter counters are not logged at all;
          they are measured from the broker on the integrity panel. */}
      {notInLogs ? (
        <div className="cl-act-row cl-notinlogs">
          {Object.entries(notInLogs).map(([key, why]) => (
            <span key={key} className="cl-act-note">
              <b>{key.replace(/_/g, ' ')}</b>: {why}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ComponentLogs({ apiUrl, link }) {
  const [components, setComponents] = useState(null);
  const [component, setComponent] = useState('debezium');
  const [minutes, setMinutes] = useState(30);
  const [filterText, setFilterText] = useState('');
  const [applied, setApplied] = useState('');
  const [stream, setStream] = useState('');
  const [streams, setStreams] = useState([]);
  const [payload, setPayload] = useState(null);
  const [older, setOlder] = useState([]);
  const [busy, setBusy] = useState(false);
  const [follow, setFollow] = useState(false);
  const [wrap, setWrap] = useState(false);
  const [error, setError] = useState(null);
  const [activity, setActivity] = useState(null);
  const bodyRef = useRef(null);

  const linkQuery = link ? `&link=${encodeURIComponent(link)}` : '';

  // Which components have logs, and whether each is readable. Also carries the
  // components that have none (the broker), so its absence is explained rather
  // than looking like a missing tab.
  useEffect(() => {
    let cancelled = false;
    axios.get(`${apiUrl}/api/monitor/logs/components?_=1${linkQuery}`,
              { timeout: 25000 })
      .then((res) => { if (!cancelled) setComponents(res.data); })
      .catch(() => { /* the tabs fall back to the three known names */ });
    return () => { cancelled = true; };
  }, [apiUrl, linkQuery]);

  // Switching component or link invalidates the stream choice: a stream name
  // belongs to one group, and sending another group's would be an error rather
  // than an empty result.
  useEffect(() => { setStream(''); setStreams([]); setOlder([]); }, [component, link]);

  useEffect(() => {
    let cancelled = false;
    axios.get(`${apiUrl}/api/monitor/logs/streams?component=${component}${linkQuery}`,
              { timeout: 25000 })
      .then((res) => { if (!cancelled) setStreams((res.data && res.data.streams) || []); })
      .catch(() => { if (!cancelled) setStreams([]); });
    return () => { cancelled = true; };
  }, [apiUrl, component, linkQuery]);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const params = [
        `component=${encodeURIComponent(component)}`,
        `minutes=${minutes}`,
        applied ? `filter=${encodeURIComponent(applied)}` : '',
        stream ? `stream=${encodeURIComponent(stream)}` : '',
      ].filter(Boolean).join('&');
      const res = await axios.get(`${apiUrl}/api/monitor/logs?${params}${linkQuery}`,
                                  { timeout: 30000 });
      setPayload(res.data);
      setOlder([]);
    } catch (e) {
      setError((e.response && e.response.data && e.response.data.error) || e.message);
      setPayload(null);
    } finally {
      setBusy(false);
    }
  }, [apiUrl, component, minutes, applied, stream, linkQuery]);

  const loadActivity = useCallback(async () => {
    try {
      const res = await axios.get(
        `${apiUrl}/api/monitor/logs/activity?component=${component}&minutes=${minutes}${linkQuery}`,
        { timeout: 30000 });
      setActivity(res.data);
    } catch (e) {
      setActivity(null);
    }
  }, [apiUrl, component, minutes, linkQuery]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadActivity(); }, [loadActivity]);

  // Opt-in, and slower than the dashboard: each tick is a CloudWatch Logs call.
  useEffect(() => {
    if (!follow) return undefined;
    const id = setInterval(() => { load(); loadActivity(); }, FOLLOW_MS);
    return () => clearInterval(id);
  }, [follow, load, loadActivity]);

  // Following means the newest line should be visible without scrolling.
  useEffect(() => {
    if (follow && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [payload, follow]);

  const loadMore = async () => {
    if (!payload || !payload.next) return;
    setBusy(true);
    try {
      const params = [
        `component=${encodeURIComponent(component)}`,
        `minutes=${minutes}`,
        applied ? `filter=${encodeURIComponent(applied)}` : '',
        stream ? `stream=${encodeURIComponent(stream)}` : '',
        `next=${encodeURIComponent(payload.next)}`,
      ].filter(Boolean).join('&');
      const res = await axios.get(`${apiUrl}/api/monitor/logs?${params}${linkQuery}`,
                                  { timeout: 30000 });
      setOlder((prev) => prev.concat((res.data && res.data.events) || []));
      setPayload((prev) => ({ ...prev, next: res.data && res.data.next }));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const events = useMemo(
    () => ((payload && payload.events) || []).concat(older),
    [payload, older],
  );

  const copyAll = () => {
    const text = events.map((e) => `${clockOf(e.ts)} ${e.message}`).join('\n');
    if (navigator.clipboard) navigator.clipboard.writeText(text);
  };

  const tabs = (components && components.components)
    || ['debezium', 'sink', 'loader'].map((c) => ({ component: c }));
  const withoutLogs = (components && components.without_logs) || [];
  const message = payload ? stateMessage(payload, component) : null;

  return (
    <section className="pm-section cl">
      <div className="cl-head">
        <h2>Component logs</h2>
        <span className="cl-sub">
          {link ? `link ${link}` : 'the configured pipeline'}
          {payload && payload.log_group
            ? <span className="cl-group">{` \u00b7 ${payload.log_group}`}</span> : null}
        </span>
        <div className="cl-spacer" />
        <label className="cl-toggle">
          <input type="checkbox" checked={follow}
                 onChange={(e) => setFollow(e.target.checked)} />
          follow
        </label>
        <button type="button" className="btn btn-secondary" onClick={() => {
          load(); loadActivity();
        }} disabled={busy}>
          {busy ? 'Loading\u2026' : 'Refresh'}
        </button>
      </div>

      <div className="cl-tabs">
        {tabs.map((t) => (
          <button key={t.component} type="button"
                  className={`cl-tab${t.component === component ? ' is-on' : ''}`}
                  onClick={() => setComponent(t.component)}>
            {COMPONENT_LABEL[t.component] || t.component}
            {t.state ? <StateChip state={t.state} /> : null}
            {t.last_event_age_sec !== undefined && t.last_event_age_sec !== null
              ? <span className="cl-tab-age">{Math.round(t.last_event_age_sec)}s ago</span>
              : null}
          </button>
        ))}
        {/* The broker has no container to log. Shown as a disabled tab carrying
            the reason, because a component that is missing without explanation makes users ask why. */}
        {withoutLogs.map((w) => (
          <span key={w.component} className="cl-tab is-off" title={w.note}>
            {`${w.component} \u2014 no logs`}
          </span>
        ))}
      </div>

      <div className="cl-controls">
        <div className="cl-ranges">
          {RANGES.map((r) => (
            <button key={r.minutes} type="button"
                    className={`cl-range${r.minutes === minutes ? ' is-on' : ''}`}
                    onClick={() => setMinutes(r.minutes)}>{r.label}</button>
          ))}
        </div>
        <form className="cl-filter" onSubmit={(e) => {
          e.preventDefault(); setApplied(filterText);
        }}>
          <input type="text" value={filterText} placeholder={'filter lines\u2026'}
                 aria-label="filter log lines"
                 onChange={(e) => setFilterText(e.target.value)} />
          <button type="submit" className="btn btn-secondary">Filter</button>
          {applied ? (
            <button type="button" className="cl-clear"
                    onClick={() => { setFilterText(''); setApplied(''); }}>clear</button>
          ) : null}
        </form>
        {streams.length > 1 ? (
          <label className="cl-streampick">
            task
            <select value={stream} onChange={(e) => setStream(e.target.value)}>
              <option value="">all ({streams.length})</option>
              {streams.map((s) => (
                <option key={s.stream} value={s.stream}>
                  {s.task_id ? s.task_id.slice(0, 12) : s.stream}
                  {s.last_event_age_sec !== null && s.last_event_age_sec !== undefined
                    ? ` (${Math.round(s.last_event_age_sec)}s ago)` : ''}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <div className="cl-spacer" />
        <label className="cl-toggle">
          <input type="checkbox" checked={wrap}
                 onChange={(e) => setWrap(e.target.checked)} />
          wrap
        </label>
        <button type="button" className="cl-clear" onClick={copyAll}
                disabled={!events.length}>copy</button>
      </div>

      <Activity activity={activity} />

      {error ? <div className="cl-banner cl-banner-bad">{error}</div> : null}

      {message ? (
        <div className={`cl-banner cl-banner-${message.tone}`}>
          <b>{message.title}</b>
          <span>{message.detail}</span>
        </div>
      ) : null}

      {events.length ? (
        <>
          <div className={`cl-body${wrap ? ' is-wrap' : ''}`} ref={bodyRef}>
            {events.map((e, i) => (
              <div key={`${e.ts}-${i}`} className={`cl-line cl-sev-${e.severity}`}>
                <span className="cl-time">{clockOf(e.ts)}</span>
                {!stream && e.task_id
                  ? <span className="cl-task">{e.task_id.slice(0, 8)}</span> : null}
                <span className="cl-msg">{e.message}</span>
              </div>
            ))}
          </div>
          <div className="cl-foot">
            <span>{events.length} line(s)</span>
            {payload && payload.next ? (
              <button type="button" className="btn btn-secondary"
                      onClick={loadMore} disabled={busy}>Load older</button>
            ) : <span className="cl-dim">no more in this window</span>}
          </div>
        </>
      ) : null}
    </section>
  );
}

export default ComponentLogs;
