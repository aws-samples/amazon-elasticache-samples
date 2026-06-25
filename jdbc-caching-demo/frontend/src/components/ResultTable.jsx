import styles from './ResultTable.module.css'

export default function ResultTable({ columns, rows }) {
  if (!rows || rows.length === 0) {
    return <p className={styles.empty}>No rows returned.</p>
  }

  return (
    <div className={styles.wrapper}>
      <table className={styles.table}>
        <thead>
          <tr>
            {columns.map(col => (
              <th key={col}>{col}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map(col => (
                <td key={col}>
                  {row[col] === null || row[col] === undefined ? (
                    <span className={styles.null}>null</span>
                  ) : String(row[col])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
