import { useState, useEffect } from 'react'
import ResultTable from './components/ResultTable'
import QueryPanel from './components/QueryPanel'
import RunHistory from './components/RunHistory'
import styles from './App.module.css'

const API = '/api'

const DEFAULT_SQL = `SELECT
  location,
  COUNT(*) AS total_cinemas,
  AVG(capacity) AS avg_capacity,
  MAX(capacity) AS largest,
  MIN(capacity) AS smallest,
  SUM(capacity) AS total_seats
FROM cinemas
GROUP BY location
ORDER BY total_seats DESC`

export default function App() {
  const [sql, setSql] = useState(DEFAULT_SQL)
  const [showPlan, setShowPlan] = useState(false)
  const [ttl, setTtl] = useState('300s')
  const [loading, setLoading] = useState({ direct: false, cached: false, conn: false })
  const [error, setError] = useState(null)
  const [runs, setRuns] = useState([])
  const [activeRun, setActiveRun] = useState(null)
  const [connStatus, setConnStatus] = useState({ direct: 'disconnected', cached: 'disconnected' })

  // Poll connection status on mount
  useEffect(() => {
    fetchStatus()
  }, [])

  async function fetchStatus() {
    try {
      const res = await fetch(`${API}/connections/status`)
      if (res.ok) setConnStatus(await res.json())
    } catch (_) {}
  }

  async function handleConnect() {
    setLoading(l => ({ ...l, conn: true }))
    setError(null)
    try {
      const res = await fetch(`${API}/connections/connect`, { method: 'POST' })
      const text = await res.text()
      const data = text ? JSON.parse(text) : {}
      setConnStatus(data)
      // Surface any connection errors
      const errs = Object.entries(data)
        .filter(([, v]) => typeof v === 'string' && v.startsWith('error'))
        .map(([k, v]) => `${k}: ${v}`)
      if (errs.length) setError(errs.join(' | '))
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(l => ({ ...l, conn: false }))
    }
  }

  async function handleDisconnect() {
    setLoading(l => ({ ...l, conn: true }))
    try {
      const res = await fetch(`${API}/connections/disconnect`, { method: 'POST' })
      setConnStatus(await res.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(l => ({ ...l, conn: false }))
    }
  }

  async function runQuery(mode) {
    setLoading(l => ({ ...l, [mode]: true }))
    setError(null)
    try {
      const params = new URLSearchParams({ includePlan: showPlan, sql })
      if (mode === 'cached') params.set('ttl', ttl)
      const res = await fetch(`${API}/query/${mode}?${params}`)
      const text = await res.text()
      const data = text ? JSON.parse(text) : {}
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`)
      const run = { ...data, id: Date.now(), runIndex: runs.length + 1 }
      setRuns(r => [...r, run])
      setActiveRun(run)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(l => ({ ...l, [mode]: false }))
    }
  }

  const isConnected = s => s === 'connected'
  const bothConnected = isConnected(connStatus.direct) && isConnected(connStatus.cached)

  return (
    <div className={styles.app}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <h1>AWS JDBC Wrapper — Remote Cache Plugin Demo</h1>
          <p>Compare query latency between direct PostgreSQL and the JDBC cache plugin</p>
        </div>
      </header>

      <main className={styles.main}>

        {/* Connection bar */}
        <section className={styles.connBar}>
          <div className={styles.connIndicators}>
            <ConnDot label="PostgreSQL" status={connStatus.direct} />
            <ConnDot label="Valkey Cache" status={connStatus.cached} />
          </div>
          <div className={styles.connActions}>
            <button
              className={`${styles.btn} ${styles.btnConnect}`}
              onClick={handleConnect}
              disabled={loading.conn}
            >
              {loading.conn ? '⏳' : '🔌'} {bothConnected ? 'Reconnect' : 'Connect'}
            </button>
            {bothConnected && (
              <button
                className={`${styles.btn} ${styles.btnDisconnect}`}
                onClick={handleDisconnect}
                disabled={loading.conn}
              >
                ✕ Disconnect
              </button>
            )}
          </div>
        </section>

        {error && <div className={styles.errorBox}>{error}</div>}

        {/* SQL Editor */}
        <section className={styles.card}>
          <div className={styles.editorHeader}>
            <span className={styles.sectionTitle}>SQL Query</span>
            <button className={styles.resetBtn} onClick={() => setSql(DEFAULT_SQL)}>Reset to default</button>
          </div>
          <textarea
            className={styles.sqlEditor}
            value={sql}
            onChange={e => setSql(e.target.value)}
            rows={8}
            spellCheck={false}
          />
          <div className={styles.editorFooter}>
            <label className={styles.toggle}>
              <input type="checkbox" checked={showPlan} onChange={e => setShowPlan(e.target.checked)} />
              Include EXPLAIN ANALYZE plan
            </label>
            <div className={styles.ttlInline}>
              <code className={styles.hintPrefix}>/* CACHE_PARAM(ttl=</code>
              <input
                type="text"
                className={styles.ttlInput}
                value={ttl}
                onChange={e => setTtl(e.target.value)}
                placeholder="300s"
              />
              <code className={styles.hintSuffix}>) */</code>
              <div className={styles.ttlPresets}>
                {['30s','60s','5m','15m','1h'].map(t => (
                  <button key={t} className={`${styles.preset} ${ttl === t ? styles.presetActive : ''}`}
                    onClick={() => setTtl(t)}>{t}</button>
                ))}
              </div>
            </div>
          </div>
        </section>

        {/* Run buttons */}
        <section className={styles.controls}>
          <button
            className={`${styles.btn} ${styles.btnDirect}`}
            onClick={() => runQuery('direct')}
            disabled={loading.direct || !isConnected(connStatus.direct)}
            title={!isConnected(connStatus.direct) ? 'Connect first' : ''}
          >
            {loading.direct ? '⏳ Running…' : '▶ Run Direct (PostgreSQL)'}
          </button>
          <button
            className={`${styles.btn} ${styles.btnCached}`}
            onClick={() => runQuery('cached')}
            disabled={loading.cached || !isConnected(connStatus.cached)}
            title={!isConnected(connStatus.cached) ? 'Connect first' : ''}
          >
            {loading.cached ? '⏳ Running…' : '⚡ Run Cached (JDBC Plugin)'}
          </button>
          {runs.length > 0 && (
            <button className={styles.clearBtn} onClick={() => { setRuns([]); setActiveRun(null) }}>
              Clear history
            </button>
          )}
        </section>

        {/* Run history + bar chart */}
        {runs.length > 0 && (
          <section className={styles.card}>
            <div className={styles.sectionTitle}>Run History</div>
            <RunHistory runs={runs} activeId={activeRun?.id} onSelect={setActiveRun} />
          </section>
        )}

        {/* Active run result */}
        {activeRun && (
          <section className={styles.card}>
            <div className={styles.resultMeta}>
              <span className={activeRun.cacheEnabled ? styles.tagCached : styles.tagDirect}>
                {activeRun.cacheEnabled ? '⚡ Cache Plugin' : '🗄 Direct PostgreSQL'}
              </span>
              <span>Run #{activeRun.runIndex}</span>
              <span>{activeRun.rowCount} rows</span>
              <span className={styles.latency}>{activeRun.latencyMs}ms total</span>
              <span className={styles.breakdown}>
                query {activeRun.queryMs}ms · fetch {activeRun.fetchMs}ms
              </span>
              {activeRun.cacheHint && (
                <code className={styles.hintBadge}>{activeRun.cacheHint}</code>
              )}
            </div>
            <ResultTable columns={activeRun.columns} rows={activeRun.rows} />
            <QueryPanel query={activeRun.query} queryPlan={activeRun.queryPlan} />
          </section>
        )}

        {runs.length === 0 && !loading.direct && !loading.cached && (
          <div className={styles.empty}>
            <p>{bothConnected ? 'Run a query to see results' : 'Click Connect to initialize database connections'}</p>
            <p className={styles.emptyHint}>
              {bothConnected
                ? 'Try "Run Direct" then "Run Cached" multiple times to see the cache warm up'
                : 'Both PostgreSQL and Valkey connections will be established'}
            </p>
          </div>
        )}
      </main>
    </div>
  )
}

function ConnDot({ label, status }) {
  const s = typeof status === 'string' ? status : ''
  const connected = s === 'connected'
  const errored = s.startsWith('error')
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.85rem' }}>
      <span style={{
        width: 10, height: 10, borderRadius: '50%', display: 'inline-block',
        background: errored ? '#ef4444' : connected ? '#22c55e' : '#94a3b8',
        boxShadow: connected ? '0 0 6px #22c55e88' : 'none'
      }} />
      <span style={{ color: '#ccc' }}>{label}</span>
      <span style={{ color: errored ? '#fca5a5' : connected ? '#86efac' : '#94a3b8', fontSize: '0.75rem' }}>
        {connected ? 'connected' : errored ? 'error' : 'disconnected'}
      </span>
    </div>
  )
}
