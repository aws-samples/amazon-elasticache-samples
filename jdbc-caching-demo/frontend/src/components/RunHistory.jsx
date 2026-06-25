import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell, Legend, LabelList
} from 'recharts'
import styles from './RunHistory.module.css'

const DIRECT_COLOR = '#3b48cc'
const CACHED_COLOR = '#c925d1'

function CustomTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const d = payload[0].payload
  return (
    <div className={styles.tooltip}>
      <div className={styles.tooltipTitle}>Run #{d.runIndex} — {d.mode}</div>
      <div>Total: {d.latencyMs}ms</div>
      <div style={{opacity:0.8, fontSize:'0.78rem', marginTop:2}}>
        connect {d.connectMs}ms · query {d.queryMs}ms · fetch {d.fetchMs}ms
      </div>
    </div>
  )
}

export default function RunHistory({ runs, activeId, onSelect }) {
  const chartData = runs.map(r => ({
    ...r,
    name: `#${r.runIndex}`,
    mode: r.cacheEnabled ? 'Cached' : 'Direct',
  }))

  return (
    <div className={styles.wrapper}>
      <ResponsiveContainer width="100%" height={240}>
        <BarChart data={chartData} margin={{ top: 10, right: 20, left: 0, bottom: 5 }}
          onClick={({ activePayload }) => activePayload?.[0] && onSelect(activePayload[0].payload)}>
          <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
          <XAxis dataKey="name" tick={{ fontSize: 12 }} />
          <YAxis
            tickFormatter={v => `${v}ms`}
            tick={{ fontSize: 11 }}
            label={{ value: 'Latency (ms)', angle: -90, position: 'insideLeft', offset: 10, style: { fontSize: 11 } }}
          />
          <Tooltip content={<CustomTooltip />} />
          {/* Stacked: query (mode color) + fetch (lighter) */}
          <Bar dataKey="queryMs" stackId="a" name="Query/Cache" maxBarSize={60} cursor="pointer"
               radius={[0,0,0,0]}>
            {chartData.map(entry => (
              <Cell key={entry.id} fill={entry.cacheEnabled ? CACHED_COLOR : DIRECT_COLOR}
                opacity={activeId === entry.id ? 1 : 0.7} />
            ))}
          </Bar>
          <Bar dataKey="fetchMs" stackId="a" name="Fetch rows" maxBarSize={60} cursor="pointer"
               radius={[5,5,0,0]}>
            {chartData.map(entry => (
              <Cell key={entry.id} fill={entry.cacheEnabled ? '#e090e8' : '#9099e0'}
                opacity={activeId === entry.id ? 1 : 0.7} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      {/* Legend */}
      <div className={styles.legend}>
        <span className={styles.legendDirect}>🗄 Direct PostgreSQL</span>
        <span className={styles.legendCached}>⚡ Cache Plugin</span>
        <span className={styles.legendStack}>■ Query/Cache &nbsp; ■ Fetch rows</span>
        <span className={styles.legendHint}>Click a bar to view results</span>
      </div>

      {/* Run list */}
      <div className={styles.runList}>
        {runs.map(r => (
          <button
            key={r.id}
            className={`${styles.runItem} ${activeId === r.id ? styles.runItemActive : ''} ${r.cacheEnabled ? styles.runCached : styles.runDirect}`}
            onClick={() => onSelect(r)}
          >
            <span className={styles.runNum}>#{r.runIndex}</span>
            <span className={styles.runMode}>{r.cacheEnabled ? '⚡ Cached' : '🗄 Direct'}</span>
            <span className={styles.runLatency}>{r.queryMs}ms</span>
            <span className={styles.runRows}>query · {r.rowCount} rows</span>
          </button>
        ))}
      </div>
    </div>
  )
}
