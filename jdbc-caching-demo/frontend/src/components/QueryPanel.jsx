import { useState } from 'react'
import styles from './QueryPanel.module.css'

export default function QueryPanel({ query, queryPlan }) {
  const [planOpen, setPlanOpen] = useState(false)

  return (
    <div className={styles.wrapper}>
      {/* SQL Query — always visible */}
      <div className={styles.section}>
        <div className={styles.sectionHeader}>
          <span className={styles.label}>SQL Query</span>
        </div>
        <pre className={styles.code}>{query}</pre>
      </div>

      {/* Query Plan — collapsible, only shown if available */}
      {queryPlan && (
        <div className={styles.section}>
          <button
            className={styles.toggle}
            onClick={() => setPlanOpen(o => !o)}
            aria-expanded={planOpen}
          >
            <span className={styles.arrow}>{planOpen ? '▼' : '▶'}</span>
            <span className={styles.label}>Query Plan (EXPLAIN ANALYZE)</span>
          </button>
          {planOpen && (
            <pre className={`${styles.code} ${styles.plan}`}>{queryPlan}</pre>
          )}
        </div>
      )}
    </div>
  )
}
