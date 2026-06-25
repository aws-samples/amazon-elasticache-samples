import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Cell
} from 'recharts'
import styles from './PerformanceChart.module.css'

const DIRECT_COLOR = '#3b48cc'
const CACHED_COLOR = '#c925d1'

export default function PerformanceChart({ direct, cached }) {
  // Build comparison data — only include metrics where both exist
  const metrics = [
    { key: 'latencyMs', label: 'Latency (ms)', unit: 'ms' },
  ]

  // Bar chart data: one bar per mode per metric
  const latencyData = [
    direct && { name: 'Direct (PostgreSQL)', value: direct.latencyMs, fill: DIRECT_COLOR },
    cached && { name: 'Cached (Plugin)', value: cached.latencyMs, fill: CACHED_COLOR },
  ].filter(Boolean)

  // Improvement callout
  let improvement = null
  if (direct && cached && direct.latencyMs > 0) {
    const pct = Math.round(((direct.latencyMs - cached.latencyMs) / direct.latencyMs) * 100)
    improvement = pct
  }

  return (
    <div className={styles.wrapper}>
      <div className={styles.chartArea}>
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={latencyData} margin={{ top: 10, right: 30, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis dataKey="name" tick={{ fontSize: 13 }} />
            <YAxis
              tickFormatter={v => `${v}ms`}
              tick={{ fontSize: 12 }}
              label={{ value: 'Latency (ms)', angle: -90, position: 'insideLeft', offset: 10, style: { fontSize: 12 } }}
            />
            <Tooltip formatter={(v) => [`${v}ms`, 'Latency']} />
            <Bar dataKey="value" radius={[6, 6, 0, 0]} maxBarSize={80}>
              {latencyData.map((entry, i) => (
                <Cell key={i} fill={entry.fill} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      <div className={styles.stats}>
        {direct && (
          <div className={`${styles.stat} ${styles.statDirect}`}>
            <div className={styles.statLabel}>🗄 Direct</div>
            <div className={styles.statValue}>{direct.latencyMs}ms</div>
            <div className={styles.statSub}>{direct.rowCount} rows</div>
          </div>
        )}
        {cached && (
          <div className={`${styles.stat} ${styles.statCached}`}>
            <div className={styles.statLabel}>⚡ Cached</div>
            <div className={styles.statValue}>{cached.latencyMs}ms</div>
            <div className={styles.statSub}>{cached.rowCount} rows</div>
          </div>
        )}
        {improvement !== null && (
          <div className={`${styles.stat} ${styles.statImprovement}`}>
            <div className={styles.statLabel}>🚀 Improvement</div>
            <div className={styles.statValue}>
              {improvement > 0 ? `${improvement}% faster` : `${Math.abs(improvement)}% slower`}
            </div>
            <div className={styles.statSub}>
              {direct.latencyMs}ms → {cached.latencyMs}ms
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
