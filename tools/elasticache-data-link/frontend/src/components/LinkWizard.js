// ═══════════════════════════════════════════════════════════════════════════
// LinkWizard — pick a source, a credential, a target, some tables, optionally
// shape them, and deploy. Six steps, no ARN typed manually.
//
// THE ONE RULE THIS COMPONENT IS BUILT AROUND
// It does not hold a configuration of its own. Every choice is written straight
// into the link DOCUMENT, and the document is handed back to the parent, which
// also holds the editor. So the wizard and the editor are two views of one thing:
// choose a target in the form and the `target:` block changes in the text; edit
// the text and the form reflects it when you go back.
//
// This is the main design goal, not a matter of style. A wizard that kept
// its own state and serialised at the end would be a second definition of a link,
// and two definitions drift -- which is exactly the file-versus-UI split this
// work exists to close.
//
// Two consequences:
//   * There is no YAML writer in here. Rendering goes through POST
//     /api/links/format so the file is written by the same library that reads it.
//   * There is no validation in here either. Every verdict comes from the API,
//     which calls the same validator the CLI calls.
//
// What the form deliberately does NOT cover: `advanced` (broker sizing, image
// pins, subnet overrides) and `denormalize`. Both are real and both stay
// available in the editor. A form that tries to cover everything stops being
// simpler than the file it replaces. The one exception is
// advanced.alarms.email (Jira story F105): without it the alarm topic has no subscriber,
// so a pipeline deployed from this form raised every alarm to nobody.
// ═══════════════════════════════════════════════════════════════════════════
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import './LinkWizard.css';
import API_URL from '../apiUrl';


const STEPS = [
  { id: 'source', label: 'Source', hint: 'the database changes come from' },
  { id: 'credential', label: 'Credential', hint: 'how to connect to it' },
  { id: 'target', label: 'Target', hint: 'the cache changes go to' },
  { id: 'tables', label: 'Tables', hint: 'what to keep in the cache' },
  { id: 'transforms', label: 'Shape', hint: 'optional: trim, rename, compute' },
  { id: 'review', label: 'Review', hint: 'check, then deploy' },
];

const MODES = [
  {
    id: 'initial_load_and_cdc',
    label: 'Load then keep in sync',
    detail: 'Copy what is there now, then stream every change. The usual choice.',
  },
  {
    id: 'cdc',
    label: 'Keep in sync only',
    detail: 'Stream changes from now on. Rows that never change again will not '
            + 'be in the cache.',
  },
  {
    id: 'initial_load',
    label: 'One-off load',
    detail: 'Copy what is there now and stop. The cache goes stale from that '
            + 'moment.',
  },
];

// Only the operators the Go sink actually implements. `contains` and `in` are
// absent on purpose: evalFilter returns true for an operator it does not know, so
// offering one would silently disable the filter rather than fail.
const FILTER_OPS = ['=', '!=', '>', '>=', '<', '<='];

// The engines the resolver has a connector for, with their standard ports. Used
// for a self-managed / on-premises source, where there is no ARN to read the
// engine and port from -- they are stated instead. The id is the schema's
// source.engine value; the port is only a placeholder, since source.port is
// optional and the resolver fills the same default when it is left blank.
const ENGINES = [
  { id: 'mysql', label: 'MySQL', port: 3306 },
  { id: 'postgresql', label: 'PostgreSQL', port: 5432 },
  { id: 'sqlserver', label: 'SQL Server', port: 1433 },
  { id: 'oracle', label: 'Oracle', port: 1521 },
];

const DEFAULT_MODE = 'initial_load_and_cdc';

// The schema allows 28 characters, and the limit is not arbitrary: the broker's
// ElastiCache replication group is named ecdl-<name>-broker, and ElastiCache
// rejects a replication group id longer than 40. A proposal that does not satisfy
// that would cause a later failure -- it deployed three stacks, waiting for each,
// before failing on the name it had been handed.
export const NAME_MAX = 28;
// The schema's own pattern for advanced.alarms.email, so the form flags a typo
// before the API does. Deliberately loose: SNS confirms the address anyway.
export const ALARM_EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
// The document with advanced.alarms.email set to `raw`, or removed when it is
// blank. Empty blocks are removed too, so clearing the field leaves the
// document exactly as it was before, and other advanced settings are kept.
export function withAlarmEmail(link, raw) {
  const email = String(raw || '').trim();
  const isObj = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
  const advanced = isObj(link.advanced) ? { ...link.advanced } : {};
  const alarms = isObj(advanced.alarms) ? { ...advanced.alarms } : {};
  if (email) alarms.email = email;
  else delete alarms.email;
  if (Object.keys(alarms).length) advanced.alarms = alarms;
  else delete advanced.alarms;
  const updated = { ...link };
  if (Object.keys(advanced).length) updated.advanced = advanced;
  else delete updated.advanced;
  return updated;
}


// The ENGINES row for a given engine id, or {} so callers can read .label/.port
// safely before an engine is chosen.
function enginePort(engine) {
  return ENGINES.find((en) => en.id === engine) || {};
}

function Chip({ tone, children }) {
  return <span className={`wz-chip wz-chip-${tone || 'plain'}`}>{children}</span>;
}

function Field({ label, hint, children, error }) {
  return (
    <label className="wz-field">
      <span className="wz-label">{label}</span>
      {hint && <span className="wz-hint">{hint}</span>}
      {children}
      {error && <span className="wz-err">{error}</span>}
    </label>
  );
}

// A pickable row, still used for the small fixed choices (the three modes) where
// each option needs a sentence of explanation.
function Choice({ selected, disabled, onSelect, title, right, lines, notes }) {
  return (
    <button
      type="button"
      className={`wz-choice${selected ? ' is-selected' : ''}`
                 + `${disabled ? ' is-disabled' : ''}`}
      onClick={disabled ? undefined : onSelect}
      aria-pressed={selected}
      aria-disabled={disabled || undefined}
    >
      <span className="wz-choice-head">
        <span className="wz-choice-title">{title}</span>
        <span className="wz-choice-right">{right}</span>
      </span>
      {(lines || []).filter(Boolean).map((line, i) => (
        <span key={i} className="wz-choice-line">{line}</span>
      ))}
      {(notes || []).filter(Boolean).map((note, i) => (
        <span key={i} className="wz-choice-note">{note}</span>
      ))}
    </button>
  );
}

// A dropdown over discovered resources, with a typed ARN as a fallback.
//
// This replaced a list of cards, one per resource, each showing an identifier, an
// engine, a status and a full endpoint. With six databases that is eighteen lines
// of mostly-irrelevant text to scroll past before reaching the fields underneath,
// and the one important detail -- which of them you picked -- was the hardest
// to see. A dropdown is one line, and the detail of the CHOSEN item is shown
// beneath it where it means something.
//
// Two properties kept from the card list:
//   * Unusable options are still present, as disabled entries carrying the reason.
//     A list that hides what someone is looking for leaves them unable to tell
//     why it is missing.
//   * Notes on the chosen item still show. The Aurora-member warning in
//     particular has to be read, not filtered out.
function ArnPicker({
  label, hint, options, value, onChange, describe, placeholder, emptyNote,
}) {
  // Typed mode latches on when the current value is not one of the options, so
  // reopening a link whose ARN was typed manually does not silently lose it.
  const known = options.some((o) => o.arn === value);
  const [typing, setTyping] = useState(Boolean(value) && !known);
  const chosen = options.find((o) => o.arn === value);
  const detail = chosen && describe ? describe(chosen) : null;

  return (
    <Field label={label} hint={hint}>
      {typing ? (
        <>
          <input
            className="wz-input wz-arn"
            value={value || ''}
            placeholder={placeholder}
            spellCheck="false"
            onChange={(e) => onChange(e.target.value.trim())}
          />
          <button type="button" className="wz-link"
                  onClick={() => setTyping(false)}>
            choose from the list instead
          </button>
        </>
      ) : (
        <>
          <select
            className="wz-input wz-select"
            value={value || ''}
            onChange={(e) => onChange(e.target.value)}
          >
            <option value="">
              {options.length ? 'choose…' : (emptyNote || 'nothing found')}
            </option>
            {options.map((o) => (
              <option key={o.arn} value={o.arn} disabled={o.usable === false}>
                {o.label}{o.usable === false ? ` — ${o.why}` : ''}
              </option>
            ))}
          </select>
          <button type="button" className="wz-link"
                  onClick={() => setTyping(true)}>
            or paste an ARN
          </button>
        </>
      )}

      {detail && (
        <div className="wz-chosen">
          {(detail.lines || []).filter(Boolean).map((line, i) => (
            <div key={i} className="wz-chosen-line">{line}</div>
          ))}
          {(detail.notes || []).filter(Boolean).map((note, i) => (
            <div key={i} className="wz-chosen-note">{note}</div>
          ))}
        </div>
      )}
      {typing && value && !known && (
        <div className="wz-chosen">
          <div className="wz-chosen-note">
            Not one of the resources found in this account, so nothing about it
            could be checked here. Resolve will read it and report what it is.
          </div>
        </div>
      )}
    </Field>
  );
}

function LinkWizard({ document: doc, onChange, onDone, tokenMissing,
                      onResolve, onPlan, onDeploy, busy, canAct,
                      hideDeployActions, initialStep, actNote }) {
  const [step, setStep] = useState(0);
  // Open on a given step instead of the Source step. Used when editing a running
  // pipeline, where the source/credential/target are already set and the work is
  // in Shape (or Tables) -- starting from Source every time adds unnecessary steps.
  useEffect(() => {
    if (!initialStep) return;
    const i = STEPS.findIndex((s) => s.id === initialStep);
    if (i >= 0) setStep(i);
    // Run once on mount; initialStep is fixed for the life of this wizard.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [inventory, setInventory] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [tableInfo, setTableInfo] = useState(null);
  const [tablesBusy, setTablesBusy] = useState(false);
  const [manualTables, setManualTables] = useState('');
  // Column names typed manually, per table, for when the catalogue could not be
  // read. Kept outside the link document because they are not part of the config
  // -- they are only the column names the form offers.
  const [manualColumns, setManualColumns] = useState({});
  // Deploying costs money and creates infrastructure, so it takes a second,
  // deliberate action. It is not a modal dialog: people close a dialog without
  // reading it, and this text must be read.
  const [armed, setArmed] = useState(false);

  // Memoised because these feed useCallback dependency lists. Without it each
  // render produces a fresh `{}`, so `fetchTables` is rebuilt every render and
  // the effect that calls it can re-fire in a loop. Other checks prevent that
  // today, but the code should not depend on them.
  const link = useMemo(() => doc || {}, [doc]);
  const source = useMemo(() => link.source || {}, [link]);
  const target = useMemo(() => link.target || {}, [link]);
  const tables = useMemo(() => link.tables || [], [link]);
  const transformTables = useMemo(
    () => (link.transforms && link.transforms.tables) || {}, [link]);

  // Every edit goes through here, so there is exactly one way the document
  // changes and the parent is always told.
  const patch = useCallback((changes) => {
    onChange({ ...link, ...changes });
  }, [link, onChange]);

  const patchSource = useCallback((changes) => {
    patch({ source: { ...source, ...changes } });
  }, [patch, source]);

  const load = useCallback(async (forSource) => {
    setLoading(true);
    setError(null);
    try {
      const params = forSource ? { for_source: forSource } : {};
      const res = await axios.get(`${API_URL}/api/links/discover`,
                                  { params, timeout: 60000 });
      if (res.data && res.data.discovered === false) {
        setError(`${res.data.error}${res.data.hint ? ` ${res.data.hint}` : ''}`);
        setInventory(null);
      } else {
        setInventory(res.data);
      }
    } catch (e) {
      const d = e.response && e.response.data;
      setError(d && (d.error || d.reason)
        ? `${d.error || 'failed'}${d.reason ? ` — ${d.reason}` : ''}`
        : (e.message || 'could not reach the control plane'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!tokenMissing) load(null);
  }, [load, tokenMissing]);

  // The form's defaults are part of the DOCUMENT, not display-only values separate from it.
  // This was a real bug: the mode list rendered "Load then keep in sync" as
  // selected while `mode` was absent from the document, so anyone who accepted
  // the default -- which most users do -- got
  // "mode is required" and a Next button that would not move.
  useEffect(() => {
    const fill = {};
    if (!link.schema_version) fill.schema_version = 1;
    if (!link.mode) fill.mode = DEFAULT_MODE;
    if (Object.keys(fill).length) onChange({ ...link, ...fill });
  }, [link, onChange]);

  // Re-rank credentials once a source is chosen. The ranking lives in the API so
  // only one place decides which secret matches.
  useEffect(() => {
    if (source.arn && inventory && inventory.ranked_for !== source.arn) {
      load(source.arn);
    }
  }, [source.arn, inventory, load]);

  const chosenSource = useMemo(
    () => (inventory ? (inventory.sources || []).find((s) => s.arn === source.arn) : null),
    [inventory, source.arn]);

  // 'aws' (an RDS/Aurora ARN) or 'external' (a self-managed / on-prem database
  // given by engine + host). Absent means aws, matching the schema default.
  const sourceKind = source.kind || 'aws';
  // The engine drives which fields show (the schema field is hidden for MySQL)
  // and comes from the discovered source for an AWS link, or from what was
  // chosen for an external one.
  const effectiveEngine = sourceKind === 'external'
    ? source.engine
    : (chosenSource && chosenSource.engine);
  // The shared "About this link" block (database, name, mode) is shown only when
  // the source is known: a resolved AWS source, or an external one with its
  // engine and host filled in.
  const sourceReady = sourceKind === 'external'
    ? Boolean(source.engine && source.host)
    : Boolean(chosenSource);
  const chosenTarget = useMemo(
    () => (inventory ? (inventory.targets || []).find((t) => t.arn === target.arn) : null),
    [inventory, target.arn]);

  const fetchTables = useCallback(async () => {
    if (!source.arn || !source.secret_arn) return;
    setTablesBusy(true);
    try {
      const res = await axios.post(`${API_URL}/api/links/discover/tables`,
                                   { link }, { timeout: 60000 });
      setTableInfo(res.data);
    } catch (e) {
      const d = e.response && e.response.data;
      setTableInfo({
        listed: false,
        error: (d && d.error) || e.message || 'could not list tables',
        hint: d && d.hint,
      });
    } finally {
      setTablesBusy(false);
    }
  }, [link, source.arn, source.secret_arn]);

  // Listing needs a source, a credential and a database, so it runs when the
  // tables step is reached rather than on mount.
  useEffect(() => {
    // Both Tables (to pick tables) and Shape (to pick columns) need the
    // catalogue, so fetch it on either -- opening straight to Shape (editing)
    // must still populate the column pills.
    const id = STEPS[step].id;
    if ((id === 'tables' || id === 'transforms') && !tableInfo && !tablesBusy
        && source.arn && source.secret_arn) {
      fetchTables();
    }
  }, [step, tableInfo, tablesBusy, source.arn, source.secret_arn, fetchTables]);

  const toggleTable = (name) => {
    const next = tables.includes(name)
      ? tables.filter((t) => t !== name)
      : [...tables, name];
    // A transform for a table that is no longer selected is a validation error
    // ("has no matching entry in 'tables'"), so removing the table removes its
    // shaping too rather than leaving the document invalid.
    const keptTransforms = {};
    Object.keys(transformTables).forEach((t) => {
      if (next.includes(t)) keptTransforms[t] = transformTables[t];
    });
    const changes = { tables: next };
    changes.transforms = Object.keys(keptTransforms).length
      ? { ...(link.transforms || {}), tables: keptTransforms }
      : undefined;
    if (!changes.transforms) delete changes.transforms;
    const updated = { ...link, ...changes };
    if (!Object.keys(keptTransforms).length) delete updated.transforms;
    onChange(updated);
  };

  const setTransform = (table, spec) => {
    const nextTables = { ...transformTables };
    // An empty spec is removed rather than written as `{}`: an empty transform
    // block is not the same as no transform to a reader, and only one of those
    // is what was meant.
    if (spec && Object.keys(spec).length) nextTables[table] = spec;
    else delete nextTables[table];
    const updated = { ...link };
    if (Object.keys(nextTables).length) {
      updated.transforms = { ...(link.transforms || {}), tables: nextTables };
    } else {
      delete updated.transforms;
    }
    onChange(updated);
  };

  // Columns come from the catalogue when it could be read, and otherwise from
  // whatever the person has already named in this table's transform. That second
  // source is what makes shaping usable at all when listing is unavailable: the
  // form would otherwise offer an empty set of columns and no way to add one.
  const columnsFor = (table) => {
    const found = ((tableInfo && tableInfo.tables) || []).find(
      (t) => t.table === table);
    if (found) return found.columns.map((c) => c.name);
    const spec = transformTables[table] || {};
    const named = new Set([
      ...(spec.columns || []),
      ...Object.keys(spec.rename || {}),
      ...(spec.filter && spec.filter.field ? [spec.filter.field] : []),
      ...(manualColumns[table] || []),
    ]);
    return [...named].filter(Boolean);
  };

  const addManualColumn = (table, raw) => {
    const names = String(raw).split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    if (!names.length) return;
    setManualColumns((prev) => ({
      ...prev,
      [table]: [...new Set([...(prev[table] || []), ...names])],
    }));
  };

  // What each step needs before it can be left. Returned as a reason so the
  // button can say why it is disabled instead of only being disabled.
  const blockedReason = () => {
    switch (STEPS[step].id) {
      // Ordered the way the step is now laid out, so the reason always refers to
      // the next thing DOWN the page rather than something higher up the page.
      case 'source':
        if (sourceKind === 'external') {
          if (!source.engine) return 'choose the source engine above';
          if (!source.host) return 'enter the source host';
          if (!source.username) return 'enter the database user';
        } else if (!source.arn) {
          return 'choose the source database above';
        }
        if (!source.database) return 'fill in the database name';
        if (!link.name) return 'give the pipeline a short name (for example orders-cache)';
        return null;
      case 'credential':
        return source.secret_arn ? null : 'choose the credential for the source';
      case 'target':
        if (!target.arn) return 'choose a target cache';
        if (chosenTarget && chosenTarget.needs_secret && !target.secret_arn) {
          return 'this cache requires authentication, so choose its credential';
        }
        return null;
      case 'tables':
        return tables.length ? null : 'choose at least one table';
      case 'transforms': {
        // A half-filled computed field is invalid, and it used to be caught only
        // on the Review step as "1 field(s) still to fill in", naming nothing.
        const specs = ((link.transforms || {}).tables) || {};
        for (const table of Object.keys(specs)) {
          for (const entry of ((specs[table] || {}).computed || [])) {
            if (!entry || !String(entry.name || '').trim()) {
              return `${table}: a computed field has no name -- fill it in or remove it`;
            }
            if (!String(entry.expr || '').trim()) {
              return `${table}: the computed field '${entry.name}' needs a formula `
                + '(for example price * 100) -- fill it in or remove it';
            }
          }
        }
        return null;
      }
      default:
        return null;
    }
  };
  const blocked = blockedReason();
  const alarmEmail = ((link.advanced || {}).alarms || {}).email || '';

  return (
    <div className="wz">
      <ol className="wz-steps">
        {STEPS.map((s, i) => (
          <li key={s.id}
              className={`${i === step ? 'is-current' : ''}`
                         + `${i < step ? ' is-done' : ''}`}>
            <button type="button" onClick={() => i <= step && setStep(i)}
                    disabled={i > step}>
              <span className="wz-stepnum">{i + 1}</span>
              <span className="wz-steplabel">{s.label}</span>
              <span className="wz-stephint">{s.hint}</span>
            </button>
          </li>
        ))}
      </ol>

      {tokenMissing && (
        <div className="wz-banner wz-banner-warn">
          Discovery reads your account, so it needs the API token. Paste it in the
          bar at the top of the page.
        </div>
      )}
      {error && <div className="wz-banner wz-banner-bad">{error}</div>}
      {inventory && (inventory.problems || []).length > 0 && (
        <div className="wz-banner wz-banner-warn">
          {inventory.problems.map((p, i) => (
            <div key={i}>{p.error}{p.hint ? ` ${p.hint}` : ''}</div>
          ))}
        </div>
      )}

      <div className="wz-body">
        {/* The source list comes FIRST and the other fields appear below it, after
            a source is chosen. The step used to start with Name and mode above a
            long list, so after the user picked a database further down, the
            "name the link" message pointed to a field they could no longer see.
            Each field is shown only when it applies. */}
        {STEPS[step].id === 'source' && (
          <section>
            <h2>
              Which database do the changes come from?
              {loading && <span className="wz-muted"> looking…</span>}
            </h2>

            {/* Where the source lives. An AWS source is picked by ARN; a
                self-managed / on-prem one is described by engine and host and
                reached over Direct Connect or a VPN. The target is an ElastiCache
                cache either way. */}
            <div className="wz-modes wz-kind">
              <Choice
                selected={sourceKind === 'aws'}
                onSelect={() => patchSource({
                  kind: undefined, engine: undefined, host: undefined,
                  port: undefined, tls: undefined,
                })}
                title="AWS RDS / Aurora"
                lines={['A managed database in this account, chosen by ARN.']}
              />
              <Choice
                selected={sourceKind === 'external'}
                onSelect={() => patchSource({ kind: 'external', arn: undefined })}
                title="Self-managed / on-premises"
                lines={['A database you run yourself, reached over Direct Connect '
                        + 'or VPN. Given by engine and host.']}
              />
            </div>

            {sourceKind === 'aws' && (
            <ArnPicker
              label="Source database"
              hint="An RDS or Aurora cluster in this account, or paste any ARN."
              placeholder="arn:aws:rds:us-east-2:123456789012:cluster:my-cluster"
              emptyNote="no databases found — paste an ARN instead"
              value={source.arn}
              options={((inventory && inventory.sources) || []).map((s) => ({
                arn: s.arn,
                label: `${s.identifier} · ${s.engine_label || s.engine_raw}`
                       + `${s.kind === 'instance' ? ' · instance' : ''}`,
                usable: s.usable,
                why: (s.blockers || [])[0],
                raw: s,
              }))}
              describe={(o) => ({
                lines: [`${o.raw.endpoint}:${o.raw.port || ''}`,
                        `${o.raw.engine_version || ''} · ${o.raw.status}`
                        + (o.raw.username ? ` · user ${o.raw.username}` : '')],
                notes: [...(o.raw.blockers || []), ...(o.raw.notes || [])],
              })}
              onChange={(arn) => {
                const found = ((inventory && inventory.sources) || [])
                  .find((s) => s.arn === arn);
                patch({
                  // The name is NOT proposed from the cluster any more. A
                  // cluster id like ecdl-demo-source-dbcluster-pnxyw... became a
                  // 28-character pipeline name nobody chose, and every stack,
                  // stream and log group was named after it. The Name field
                  // below asks for a short one instead.
                  source: {
                    ...source,
                    arn,
                    database: (found && found.database) || source.database || '',
                    // RDS names the secret when it manages the password, so the
                    // next step can be pre-filled.
                    secret_arn: (found && found.master_user_secret_arn)
                                || source.secret_arn,
                  },
                });
              }}
            />
            )}

            {sourceKind === 'external' && (
              <div className="wz-after">
                <Field label="Engine"
                       error={!source.engine ? 'needed before you can continue' : null}>
                  <select
                    className={`wz-input wz-select${!source.engine ? ' is-needed' : ''}`}
                    value={source.engine || ''}
                    onChange={(e) => patchSource({
                      engine: e.target.value || undefined,
                    })}
                  >
                    <option value="">choose an engine…</option>
                    {ENGINES.map((en) => (
                      <option key={en.id} value={en.id}>{en.label}</option>
                    ))}
                  </select>
                </Field>

                <Field
                  label="Host"
                  hint={'Hostname or IP the pipeline connects to, reachable from '
                        + 'AWS over Direct Connect or a VPN.'}
                  error={!source.host ? 'needed before you can continue' : null}
                >
                  <input
                    className={`wz-input${!source.host ? ' is-needed' : ''}`}
                    value={source.host || ''}
                    placeholder="db.corp.internal"
                    onChange={(e) => {
                      const host = e.target.value;
                      // The name is asked for below, not derived from the host.
                      patch({ source: { ...source, host } });
                    }}
                  />
                </Field>

                <Field
                  label="Port"
                  hint={source.engine
                    ? `leave blank for the ${enginePort(source.engine).label} `
                      + `default (${enginePort(source.engine).port})`
                    : "leave blank for the engine's default port"}
                >
                  <input
                    className="wz-input"
                    type="number"
                    value={source.port || ''}
                    placeholder={String(enginePort(source.engine).port || '')}
                    onChange={(e) => patchSource({
                      port: e.target.value ? Number(e.target.value) : undefined,
                    })}
                  />
                </Field>

                <Field
                  label="Database user"
                  hint={'The login the pipeline authenticates as. Its password is '
                        + 'the credential chosen in the next step.'}
                  error={!source.username ? 'needed before you can continue' : null}
                >
                  <input
                    className={`wz-input${!source.username ? ' is-needed' : ''}`}
                    value={source.username || ''}
                    placeholder="repl"
                    onChange={(e) => patchSource({
                      username: e.target.value || undefined,
                    })}
                  />
                </Field>

                <label className="wz-check">
                  <input
                    type="checkbox"
                    checked={source.tls !== false}
                    onChange={(e) => patchSource({
                      // Stored only when off, so the document stays clean on the
                      // default (TLS on).
                      tls: e.target.checked ? undefined : false,
                    })}
                  />
                  <span>Connect over TLS{' '}
                    <span className="wz-muted">(recommended; on by default)</span>
                  </span>
                </label>
              </div>
            )}

            {sourceReady && (
              <div className="wz-after">
                <h3>About this link</h3>

                <Field
                  label="Database"
                  hint={'Which database on that server. RDS reports no default for '
                        + 'most Aurora clusters, so it has to be given.'}
                  error={!source.database ? 'needed before you can continue' : null}
                >
                  <input
                    className={`wz-input${!source.database ? ' is-needed' : ''}`}
                    value={source.database || ''}
                    placeholder="Inventory"
                    autoFocus={!source.database}
                    onChange={(e) => patchSource({ database: e.target.value })}
                  />
                </Field>

                {effectiveEngine !== 'mysql' && (
                  <Field label="Schema" hint="leave blank for the engine default">
                    <input
                      className="wz-input"
                      value={source.schema || ''}
                      placeholder={effectiveEngine === 'postgresql' ? 'public' : 'dbo'}
                      onChange={(e) => patchSource({
                        schema: e.target.value || undefined,
                      })}
                    />
                  </Field>
                )}

                <Field
                  label="Pipeline name"
                  hint={'A short name for this pipeline, for example orders-cache. '
                        + 'It names the stacks, streams and logs it creates. '
                        + `Lowercase letters, digits and hyphens, up to ${NAME_MAX}.`}
                  error={!link.name ? 'give the pipeline a short name' : null}
                >
                  <input
                    className={`wz-input${!link.name ? ' is-needed' : ''}`}
                    value={link.name || ''}
                    placeholder="orders-cache"
                    maxLength={NAME_MAX}
                    onChange={(e) => patch({
                      // Lowercased and stripped of what the schema rejects, as you
                      // type, so an invalid name cannot be typed rather than being
                      // reported afterwards.
                      name: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''),
                    })}
                  />
                </Field>

                <Field label="What should it do?">
                  <div className="wz-modes">
                    {MODES.map((m) => (
                      <Choice
                        key={m.id}
                        selected={(link.mode || DEFAULT_MODE) === m.id}
                        onSelect={() => patch({ mode: m.id })}
                        title={m.label}
                        lines={[m.detail]}
                      />
                    ))}
                  </div>
                </Field>
              </div>
            )}
          </section>
        )}

        {STEPS[step].id === 'credential' && (
          <section>
            <h2>How should the pipeline connect to {chosenSource
              ? chosenSource.identifier : 'the source'}?</h2>
            <p className="wz-muted">
              A Secrets Manager secret holding the database password. The control
              plane hands the ARN to the pipeline, which reads it at start-up;
              the value is not stored here.
              {chosenSource && chosenSource.username
                && ` The database user is ${chosenSource.username}, the cluster's
                     RDS admin user.`}
            </p>

            <ArnPicker
              label="Credential"
              hint="Ordered with the likeliest match first, based on the words it
                    shares with the database name."
              placeholder="arn:aws:secretsmanager:us-east-2:123456789012:secret:my-db-password"
              emptyNote="no secrets listed — paste an ARN instead"
              value={source.secret_arn}
              options={((inventory && inventory.secrets) || []).map((s) => ({
                arn: s.arn,
                label: s.name + (s.likely ? '  (likely)' : '')
                       + (s.rds_managed ? '  (RDS managed)' : ''),
                usable: !s.deleted_pending,
                why: 'scheduled for deletion',
                raw: s,
              }))}
              describe={(o) => ({
                lines: [o.raw.match_reason, o.raw.description],
                notes: [o.raw.deleted_pending
                  ? 'scheduled for deletion, so a task using it could not start'
                  : null],
              })}
              onChange={(arn) => patchSource({ secret_arn: arn })}
            />
          </section>
        )}

        {STEPS[step].id === 'target' && (
          <section>
            <h2>Which cache should the data be written to?</h2>

            <ArnPicker
              label="Target cache"
              hint="An ElastiCache replication group, or paste any ARN."
              placeholder="arn:aws:elasticache:us-east-2:123456789012:replicationgroup:my-cache"
              emptyNote="no caches found — paste an ARN instead"
              value={target.arn}
              options={((inventory && inventory.targets) || []).map((t) => ({
                arn: t.arn,
                label: `${t.identifier} · ${t.shards} shard(s) · `
                       + `cluster mode ${t.cluster_mode ? 'on' : 'off'}`
                       + `${t.tls ? ' · TLS' : ''}`,
                usable: t.usable,
                why: (t.blockers || [])[0],
                raw: t,
              }))}
              describe={(o) => ({
                lines: [`${o.raw.endpoint}:6379`,
                        `${o.raw.node_type} · ${o.raw.engine || ''} · `
                        + `${o.raw.status} · auth ${o.raw.auth_mode}`],
                notes: [...(o.raw.blockers || []), ...(o.raw.notes || [])],
              })}
              onChange={(arn) => patch({ target: { ...target, arn } })}
            />

            {chosenTarget && chosenTarget.needs_secret && (
              <ArnPicker
                label="Cache credential"
                hint={`${chosenTarget.identifier} requires authentication `
                      + `(${chosenTarget.auth_mode}). Without one the sink `
                      + `connects, reports itself healthy, and dead-letters every `
                      + `write.`}
                placeholder="arn:aws:secretsmanager:us-east-2:123456789012:secret:my-cache-token"
                emptyNote="no secrets listed — paste an ARN instead"
                value={target.secret_arn}
                options={((inventory && inventory.secrets) || []).map((s) => ({
                  arn: s.arn,
                  label: s.name,
                  usable: !s.deleted_pending,
                  why: 'scheduled for deletion',
                  raw: s,
                }))}
                describe={(o) => ({ lines: [o.raw.description], notes: [] })}
                onChange={(arn) => patch({
                  target: { ...target, secret_arn: arn },
                })}
              />
            )}
          </section>
        )}

        {STEPS[step].id === 'tables' && (
          <section>
            <h2>Which tables should the cache hold?</h2>

            {sourceKind === 'external' && (
              <p className="wz-muted">
                The catalogue cannot be read for a self-managed source from here,
                so type the table names to replicate — qualified as the engine
                needs (for example schema.table for PostgreSQL/SQL Server).
              </p>
            )}

            {tablesBusy && <p className="wz-muted">reading the catalogue…</p>}

            {tableInfo && tableInfo.listed && (
              <>
                <p className="wz-muted">
                  {tableInfo.count} table(s) in {tableInfo.namespace}
                  {' '}({tableInfo.namespace_kind}) on {tableInfo.engine_label}.
                </p>
                {(tableInfo.prerequisites || []).length > 0 && (
                  <div className="wz-prereq">
                    <strong>Change capture prerequisites</strong>
                    {tableInfo.prerequisites.map((c, i) => (
                      <div key={i} className={c.ok === false ? 'is-bad' : ''}>
                        <Chip tone={c.ok ? 'ok' : (c.ok === null ? 'plain' : 'bad')}>
                          {c.ok ? 'ok' : (c.ok === null ? 'unknown' : 'not set')}
                        </Chip>
                        {c.name} <span className="wz-muted">{c.detail}</span>
                        {c.fix && <div className="wz-fix">{c.fix}</div>}
                      </div>
                    ))}
                  </div>
                )}
                <div className="wz-list">
                  {tableInfo.tables.map((t) => (
                    <Choice
                      key={t.table}
                      selected={tables.includes(t.table)}
                      onSelect={() => toggleTable(t.table)}
                      title={t.table}
                      right={<span className="wz-muted">
                        {t.row_estimate === null || t.row_estimate === undefined
                          ? '' : `~${Number(t.row_estimate).toLocaleString()} rows`}
                      </span>}
                      lines={[
                        t.primary_key && t.primary_key.length
                          ? `key: ${t.primary_key.join(', ')} · `
                            + `${t.columns.length} columns`
                          : `${t.columns.length} columns`,
                      ]}
                      notes={[t.warning]}
                    />
                  ))}
                </div>
              </>
            )}

            {tableInfo && !tableInfo.listed && (
              <div className="wz-banner wz-banner-warn">
                <strong>{tableInfo.error}</strong>
                {tableInfo.hint && <div>{tableInfo.hint}</div>}
                <button type="button" className="wz-link" onClick={fetchTables}>
                  try listing again
                </button>
              </div>
            )}

            {/* Typing names is available ALWAYS, not only when listing fails.
                Listing needs a network path from the control plane into the
                customer's VPC, which often does not exist, and even when it works
                someone may want a table the catalogue does not show yet. The
                pipeline itself never depends on this. */}
            <details className="wz-manual"
                     open={Boolean((tableInfo && !tableInfo.listed)
                                   || sourceKind === 'external')}>
              <summary>type table names instead</summary>
              <Field label="Table names" hint="one per line, or comma separated">
                <textarea
                  className="wz-input wz-textarea"
                  value={manualTables}
                  placeholder={'orders\nitems'}
                  onChange={(e) => {
                    setManualTables(e.target.value);
                    patch({
                      tables: e.target.value
                        .split(/[\n,]/).map((s) => s.trim()).filter(Boolean),
                    });
                  }}
                />
              </Field>
            </details>

            {tables.length > 0 && (
              <p className="wz-selected">
                {tables.length} selected: {tables.join(', ')}
              </p>
            )}
          </section>
        )}

        {STEPS[step].id === 'transforms' && (
          <section>
            <h2>Shape what gets cached</h2>
            <p className="wz-muted">
              Optional. Skip this and each row is cached as it appears in the
              database. To embed a parent's fields onto a child
              (denormalize, below), select both tables on the previous step.
            </p>

            <details className="wz-help">
              <summary>How do I…? — worked examples</summary>
              <ul className="wz-help-list">
                <li>
                  <strong>Cache only some columns</strong> — under
                  {' '}<em>Columns to keep</em>, select the columns you want (e.g.
                  {' '}<code>id</code>, <code>status</code>, <code>amount</code>).
                  Leave none selected to cache every column.
                </li>
                <li>
                  <strong>Add a computed column</strong> — under
                  {' '}<em>Computed fields</em>, give it a name and an expression
                  over this row's columns. e.g. name <code>amount_cents</code>,
                  expression <code>amount * 100</code>. Supports
                  {' '}<code>+ - * /</code> and parentheses.
                </li>
                <li>
                  <strong>Add a value that is not a column</strong> — the cache
                  holds the source's columns plus computed ones, so add it as a
                  {' '}<em>computed field</em> (e.g. <code>price * 1.1</code> as
                  {' '}<code>price_with_tax</code>).
                </li>
                <li>
                  <strong>Denormalize (embed a parent's fields)</strong> — under
                  {' '}<em>Denormalize</em>, pick the parent table (one of this
                  link's tables), the join column (the foreign key on this row,
                  e.g. <code>customer_id</code>), and the parent fields to embed
                  (e.g. <code>name, tier</code>). Optionally a prefix like
                  {' '}<code>cust_</code>. A read then needs one key lookup instead
                  of a join.
                </li>
                <li>
                  <strong>Rename a column</strong> — under <em>Rename columns</em>,
                  pick a column and type the name it should have in the cache.
                </li>
              </ul>
            </details>

            {tables.map((table) => {
              const spec = transformTables[table] || {};
              const available = columnsFor(table);
              return (
                <div key={table} className="wz-tf">
                  <h3>{table}</h3>

                  <Field label="Columns to keep"
                         hint={available.length
                           ? 'none selected means every column'
                           : 'the catalogue could not be read, so add the column '
                             + 'names you want to work with'}>
                    {/* Always offered, because the catalogue is often unreachable
                        and shaping should not depend on it. A name added here
                        becomes available to the filter and rename controls too. */}
                    <div className="wz-row wz-addcol">
                      <input
                        className="wz-input"
                        placeholder="add a column name"
                        onKeyDown={(e) => {
                          if (e.key !== 'Enter') return;
                          e.preventDefault();
                          addManualColumn(table, e.target.value);
                          e.target.value = '';
                        }}
                      />
                      <span className="wz-hint">press enter</span>
                    </div>
                    <div className="wz-cols">
                      {available.map((col) => {
                        const kept = (spec.columns || []).includes(col);
                        return (
                          <button
                            type="button"
                            key={col}
                            className={`wz-pill${kept ? ' is-on' : ''}`}
                            onClick={() => {
                              const next = kept
                                ? (spec.columns || []).filter((c) => c !== col)
                                : [...(spec.columns || []), col];
                              const updated = { ...spec };
                              if (next.length) updated.columns = next;
                              else delete updated.columns;
                              // A rename or filter pointing at a column no longer
                              // selected is a validation error, so drop it here
                              // rather than let the document go invalid.
                              if (updated.rename) {
                                const renames = {};
                                Object.keys(updated.rename).forEach((from) => {
                                  if (!next.length || next.includes(from)) {
                                    renames[from] = updated.rename[from];
                                  }
                                });
                                if (Object.keys(renames).length) updated.rename = renames;
                                else delete updated.rename;
                              }
                              setTransform(table, updated);
                            }}
                          >
                            {col}
                          </button>
                        );
                      })}
                    </div>
                  </Field>

                  <Field label="Only cache rows where"
                         hint={'rows that do not match are not cached, and a row '
                               + 'that stops matching is removed'}>
                    <div className="wz-row">
                      <select
                        className="wz-input wz-select"
                        value={(spec.filter && spec.filter.field) || ''}
                        onChange={(e) => {
                          const updated = { ...spec };
                          if (!e.target.value) delete updated.filter;
                          else {
                            updated.filter = {
                              ...(spec.filter || { op: '=', value: '' }),
                              field: e.target.value,
                            };
                          }
                          setTransform(table, updated);
                        }}
                      >
                        <option value="">(no filter)</option>
                        {available.map((c) => <option key={c} value={c}>{c}</option>)}
                      </select>
                      {spec.filter && (
                        <>
                          <select
                            className="wz-input wz-select wz-op"
                            value={spec.filter.op || '='}
                            onChange={(e) => setTransform(table, {
                              ...spec,
                              filter: { ...spec.filter, op: e.target.value },
                            })}
                          >
                            {FILTER_OPS.map((op) => (
                              <option key={op} value={op}>{op}</option>
                            ))}
                          </select>
                          <input
                            className="wz-input"
                            placeholder="value"
                            value={spec.filter.value === undefined
                              ? '' : String(spec.filter.value)}
                            onChange={(e) => setTransform(table, {
                              ...spec,
                              filter: { ...spec.filter, value: e.target.value },
                            })}
                          />
                        </>
                      )}
                    </div>
                  </Field>

                  <Field label="Computed fields"
                         hint={"an expression over this row's columns, stored "
                               + 'alongside them'}>
                    {(spec.computed || []).map((entry, i) => (
                      <div key={i} className="wz-row">
                        <input
                          className="wz-input"
                          placeholder="name"
                          value={entry.name || ''}
                          onChange={(e) => {
                            const next = [...(spec.computed || [])];
                            next[i] = { ...entry, name: e.target.value };
                            setTransform(table, { ...spec, computed: next });
                          }}
                        />
                        <input
                          className="wz-input"
                          placeholder="amount * 100"
                          value={entry.expr || ''}
                          onChange={(e) => {
                            const next = [...(spec.computed || [])];
                            next[i] = { ...entry, expr: e.target.value };
                            setTransform(table, { ...spec, computed: next });
                          }}
                        />
                        <button
                          type="button"
                          className="wz-mini"
                          onClick={() => {
                            const next = (spec.computed || []).filter(
                              (_, j) => j !== i);
                            const updated = { ...spec };
                            if (next.length) updated.computed = next;
                            else delete updated.computed;
                            setTransform(table, updated);
                          }}
                        >remove</button>
                      </div>
                    ))}
                    <button
                      type="button"
                      className="wz-mini"
                      onClick={() => setTransform(table, {
                        ...spec,
                        computed: [...(spec.computed || []), { name: '', expr: '' }],
                      })}
                    >add a computed field</button>
                  </Field>

                  <Field label="Rename columns"
                         hint="the cached field takes the new name">
                    {Object.keys(spec.rename || {}).map((from) => (
                      <div key={from} className="wz-row">
                        <span className="wz-from">{from}</span>
                        <span className="wz-arrow">→</span>
                        <input
                          className="wz-input"
                          value={spec.rename[from]}
                          onChange={(e) => setTransform(table, {
                            ...spec,
                            rename: { ...spec.rename, [from]: e.target.value },
                          })}
                        />
                        <button
                          type="button"
                          className="wz-mini"
                          onClick={() => {
                            const renames = { ...spec.rename };
                            delete renames[from];
                            const updated = { ...spec };
                            if (Object.keys(renames).length) updated.rename = renames;
                            else delete updated.rename;
                            setTransform(table, updated);
                          }}
                        >remove</button>
                      </div>
                    ))}
                    <select
                      className="wz-input wz-select"
                      value=""
                      onChange={(e) => {
                        if (!e.target.value) return;
                        setTransform(table, {
                          ...spec,
                          rename: { ...(spec.rename || {}),
                                    [e.target.value]: e.target.value },
                        });
                      }}
                    >
                      <option value="">rename a column…</option>
                      {available
                        .filter((c) => !(spec.rename || {})[c])
                        .filter((c) => !spec.columns || spec.columns.includes(c))
                        .map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </Field>

                  <Field label="Denormalize (embed a parent's fields)"
                         hint={"copy chosen columns of a related parent row onto "
                               + 'this row, so a read is one key lookup instead '
                               + 'of a join'}>
                    {(spec.denormalize || []).map((rule, i) => {
                      const setRule = (patch) => {
                        const next = [...(spec.denormalize || [])];
                        next[i] = { ...rule, ...patch };
                        setTransform(table, { ...spec, denormalize: next });
                      };
                      const removeRule = () => {
                        const next = (spec.denormalize || []).filter(
                          (_, j) => j !== i);
                        const updated = { ...spec };
                        if (next.length) updated.denormalize = next;
                        else delete updated.denormalize;
                        setTransform(table, updated);
                      };
                      // Only this link's OTHER tables can be a parent: the parent
                      // must itself be cached for there to be anything to embed,
                      // which is exactly what the backend validates.
                      const parentOptions = tables.filter((t) => t !== table);
                      const bare = (t) => String(t).split('.').pop();
                      return (
                        <div key={i} className="wz-denorm">
                          <div className="wz-row">
                            <select
                              className="wz-input wz-select"
                              value={rule.parent_table || ''}
                              onChange={(e) => {
                                const parent = e.target.value;
                                const patch = { parent_table: parent };
                                // Offer the standard key prefix, still editable.
                                // Only overwrite while the field is empty or still
                                // holds the previous auto-filled value.
                                const auto = rule.parent_table
                                  ? `${bare(rule.parent_table)}:` : '';
                                if (!rule.parent_key_prefix
                                    || rule.parent_key_prefix === auto) {
                                  patch.parent_key_prefix = parent
                                    ? `${bare(parent)}:` : '';
                                }
                                setRule(patch);
                              }}
                            >
                              <option value="">parent table…</option>
                              {parentOptions.map(
                                (t) => <option key={t} value={t}>{t}</option>)}
                            </select>
                            <span className="wz-arrow">joined on</span>
                            <select
                              className="wz-input wz-select"
                              value={rule.join_field || ''}
                              onChange={(e) => setRule(
                                { join_field: e.target.value })}
                            >
                              <option value="">
                                {`this row's FK column…`}
                              </option>
                              {available.map(
                                (c) => <option key={c} value={c}>{c}</option>)}
                            </select>
                            <button type="button" className="wz-mini"
                                    onClick={removeRule}>remove</button>
                          </div>
                          <div className="wz-row">
                            <input
                              className="wz-input"
                              placeholder="parent fields to embed, e.g. name, tier"
                              value={(rule.fields || []).join(', ')}
                              onChange={(e) => setRule({
                                fields: e.target.value
                                  .split(/[\s,]+/).map((s) => s.trim())
                                  .filter(Boolean),
                              })}
                            />
                          </div>
                          <div className="wz-row">
                            <input
                              className="wz-input wz-op"
                              placeholder="prefix (e.g. cust_)"
                              value={rule.prefix || ''}
                              onChange={(e) => setRule(
                                { prefix: e.target.value })}
                            />
                            <input
                              className="wz-input"
                              placeholder={`parent key prefix (default ${
                                rule.parent_table
                                  ? `${bare(rule.parent_table)}:` : 'customers:'})`}
                              value={rule.parent_key_prefix || ''}
                              onChange={(e) => setRule(
                                { parent_key_prefix: e.target.value })}
                            />
                            <select
                              className="wz-input wz-select"
                              value={rule.on_parent_delete || 'null'}
                              onChange={(e) => setRule(
                                { on_parent_delete: e.target.value })}
                            >
                              <option value="null">
                                on parent delete: blank the fields
                              </option>
                              <option value="leave">
                                on parent delete: keep last values
                              </option>
                            </select>
                          </div>
                          <p className="wz-muted wz-denorm-warn">
                            Embed fields that rarely change. Every change to an
                            embedded parent field rewrites it on every child row
                            that points at that parent, so a volatile column
                            multiplies write traffic.
                          </p>
                        </div>
                      );
                    })}
                    <button
                      type="button"
                      className="wz-mini"
                      disabled={tables.length < 2}
                      onClick={() => setTransform(table, {
                        ...spec,
                        denormalize: [
                          ...(spec.denormalize || []),
                          { parent_table: '', join_field: '', fields: [],
                            on_parent_delete: 'null' },
                        ],
                      })}
                    >
                      {tables.length < 2
                        ? 'select another table to embed from'
                        : 'add a denormalize rule'}
                    </button>
                  </Field>
                </div>
              );
            })}
          </section>
        )}

        {STEPS[step].id === 'review' && (
          <section>
            <h2>Ready</h2>
            <p className="wz-muted">
              This is the link file. It is the same document the CLI reads, so
              you can commit it and get the same deployment from
              <code> ecdl apply</code>.
            </p>
            <table className="wz-summary">
              <tbody>
                <tr><th>name</th><td>{link.name}</td></tr>
                <tr><th>mode</th><td>
                  {(MODES.find((m) => m.id === link.mode) || MODES[0]).label}
                </td></tr>
                <tr><th>source</th><td>
                  {sourceKind === 'external'
                    ? `${source.host}${source.port ? `:${source.port}` : ''} `
                      + `(${(enginePort(source.engine).label) || source.engine} · self-managed)`
                    : (chosenSource ? `${chosenSource.identifier} `
                      + `(${chosenSource.engine_label})` : source.arn)}
                  {' · '}{source.database}
                </td></tr>
                <tr><th>target</th><td>
                  {chosenTarget ? `${chosenTarget.identifier} · `
                    + `${chosenTarget.shards} shard(s) · cluster mode `
                    + `${chosenTarget.cluster_mode ? 'on' : 'off'}`
                    : target.arn}
                </td></tr>
                <tr><th>tables</th><td>{tables.join(', ')}</td></tr>
                <tr><th>shaped</th><td>
                  {Object.keys(transformTables).length
                    ? Object.keys(transformTables).join(', ')
                    : 'none — rows cached as they are'}
                </td></tr>
              </tbody>
            </table>
            {/* Jira story F105. The alarms belong to the CDC stack, which a one-off load
                does not create (the schema refuses advanced.alarms there), so
                the field is only offered when there will be alarms to send. */}
            {link.mode === 'initial_load' ? (
              <p className="wz-muted wz-alarm-none">
                A one-off load creates no alarms, so there is no alarm email to set.
              </p>
            ) : (
              <div className="wz-alarm">
                <Field
                  label="Alarm email (optional)"
                  hint={'Pipeline alarms (capture stopped, sink behind, dead '
                        + 'letters) are sent here. AWS SNS emails this address a '
                        + 'confirmation link first: nothing arrives until it is '
                        + 'accepted. Leave it empty to send alarms to nobody, or '
                        + 'subscribe to the alarm topic yourself.'}
                  error={alarmEmail && !ALARM_EMAIL_RE.test(alarmEmail)
                    ? 'that does not look like an email address (like ops@example.com)'
                    : null}
                >
                  <input
                    className="wz-input wz-alarm-email"
                    type="email"
                    value={alarmEmail}
                    placeholder="ops@example.com"
                    autoComplete="off"
                    spellCheck="false"
                    onChange={(e) => onChange(withAlarmEmail(link, e.target.value))}
                  />
                </Field>
              </div>
            )}
            {/* The actions live HERE, on the step where you are finished, rather
                than below the form. They used to be in a row under the wizard next
                to a "dry run" checkbox, which meant the button read
                "Deploy (dry run)" and there was never a button labelled only
                Deploy. Two named buttons cannot be misread the way a checkbox that
                rewrites a label can. */}
            {hideDeployActions ? (
              // Edit mode: this wizard is changing a pipeline that already runs,
              // so it does not deploy. The change is applied by Compare / Apply
              // below the form, which updates the running pipeline in place.
              <p className="wz-muted wz-editnote">
                Editing a running pipeline. Add or change tables and shaping above,
                then use <strong>Compare</strong> and <strong>Apply the change</strong>
                {' '}below to update it in place — no new pipeline is created.
              </p>
            ) : (
              <>
                {/* The actions live HERE, on the step where you are finished,
                    rather than below the form. */}
                {/* Why they are off, when the page is still checking the latest
                    edit or could not check it. */}
                {actNote ? <p className="wz-muted wz-actnote">{actNote}</p> : null}
                <div className="wz-actions">
                  <button type="button" className="wz-mini"
                          disabled={!canAct || Boolean(busy)}
                          onClick={onResolve}>
                    {busy === 'resolve' ? 'Checking…' : 'Check the infrastructure'}
                  </button>
                  <button type="button" className="wz-primary"
                          disabled={!canAct || Boolean(busy)}
                          onClick={onPlan}>
                    {busy === 'plan' ? 'Working…' : 'Show what it will create'}
                  </button>
                </div>
                <p className="wz-muted">
                  Neither of those changes anything. Plan lists every stack and
                  action in the order it would run.
                </p>

                <div className="wz-deploy">
                  {!armed ? (
                    <button type="button" className="wz-danger"
                            disabled={!canAct || Boolean(busy)}
                            onClick={() => setArmed(true)}>
                      Confirm and deploy
                    </button>
                  ) : (
                    <div className="wz-confirm">
                      <strong>This creates real infrastructure and starts billing.</strong>
                      <span>
                        A broker cache (three cache.m7g.large nodes), change capture and the sink on
                        Fargate, and a one-off loader task. Roughly 15 to 25 minutes,
                        most of it waiting for the cache. Undo by deleting the three
                        <code> {link.name || '<name>'}-*</code> CloudFormation stacks.
                      </span>
                      <div className="wz-actions">
                        <button type="button" className="wz-danger"
                                disabled={!canAct || Boolean(busy)}
                                onClick={() => {
                                  setArmed(false);
                                  onDeploy({ dryRun: false });
                                }}>
                          {busy === 'deploy' ? 'Deploying…' : 'Yes, deploy it'}
                        </button>
                        <button type="button" className="wz-mini"
                                onClick={() => setArmed(false)}>
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </div>

                <button type="button" className="wz-mini" onClick={onDone}>
                  Edit as a file instead
                </button>
              </>
            )}
          </section>
        )}
      </div>

      <div className="wz-nav">
        <button type="button" className="wz-mini" disabled={step === 0}
                onClick={() => setStep(step - 1)}>Back</button>
        <span className="wz-blocked">{blocked}</span>
        {step < STEPS.length - 1 && (
          <button type="button" className="wz-primary" disabled={Boolean(blocked)}
                  onClick={() => setStep(step + 1)}>
            {STEPS[step].id === 'transforms' ? 'Review' : 'Next'}
          </button>
        )}
      </div>
    </div>
  );
}

export default LinkWizard;
