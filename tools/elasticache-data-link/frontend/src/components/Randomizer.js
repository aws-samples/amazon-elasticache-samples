import React, { useState, useEffect, useRef } from 'react';
import './Form.css';
import './Servers.css';
import API_URL from '../apiUrl';


function Randomizer() {
  const [sources, setSources] = useState([]);
  const [selectedSource, setSelectedSource] = useState(null);
  const [tables, setTables] = useState([]);
  const [loadingTables, setLoadingTables] = useState(false);
  const [selectedTable, setSelectedTable] = useState('');
  const [columns, setColumns] = useState([]);
  const [selectedCols, setSelectedCols] = useState({});
  const [loadingCols, setLoadingCols] = useState(false);
  const [batchSize, setBatchSize] = useState(1);
  const [interval, setInterval_] = useState(1);
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState('');
  const pollRef = useRef(null);

  useEffect(() => {
    fetch(`${API_URL}/api/sources`).then(r => r.json()).then(setSources).catch(() => {});
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  const analyzeSource = async (src) => {
    setSelectedSource(src);
    setSelectedTable('');
    setColumns([]);
    setSelectedCols({});
    setTables([]);
    setError('');
    setLoadingTables(true);
    try {
      const res = await fetch(`${API_URL}/api/sources/${src.id}/tables`);
      const data = await res.json();
      if (data.error) { setError(data.error); return; }
      setTables(data.tables || []);
    } catch (e) { setError(e.message); }
    finally { setLoadingTables(false); }
  };

  const selectTable = async (tableName) => {
    setSelectedTable(tableName);
    setColumns([]);
    setSelectedCols({});
    setLoadingCols(true);
    setError('');
    try {
      const res = await fetch(`${API_URL}/api/sources/${selectedSource.id}/columns?table=${tableName}`);
      const data = await res.json();
      if (data.error) { setError(data.error); return; }
      const cols = data.columns || [];
      setColumns(cols);
      const sel = {};
      cols.forEach(c => { sel[c.name] = true; });
      setSelectedCols(sel);
    } catch (e) { setError(e.message); }
    finally { setLoadingCols(false); }
  };

  const toggleCol = (name) => {
    setSelectedCols(prev => ({ ...prev, [name]: !prev[name] }));
  };

  const selectAllCols = (val) => {
    const sel = {};
    columns.forEach(c => { sel[c.name] = val; });
    setSelectedCols(sel);
  };

  const startGenerator = async () => {
    setError('');
    const activeCols = columns.filter(c => selectedCols[c.name]);
    if (activeCols.length === 0) { setError('Select at least one column'); return; }
    try {
      const res = await fetch(`${API_URL}/api/randomizer/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_id: selectedSource.id,
          table: selectedTable,
          columns: activeCols.map(c => ({ name: c.name, type: c.type, max_length: c.max_length, precision: c.precision, scale: c.scale, allowed_values: c.allowed_values })),
          batch_size: batchSize,
          interval: interval,
          schema: selectedSource.schema_name || 'public'
        })
      });
      const data = await res.json();
      if (data.error) { setError(data.error); return; }
      setRunning(true);
      setStatus(data.status);
      // start polling
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = window.setInterval(async () => {
        try {
          const sr = await fetch(`${API_URL}/api/randomizer/status?source_id=${selectedSource.id}`);
          const sd = await sr.json();
          setStatus(sd);
          if (sd.status !== 'running') {
            setRunning(false);
            clearInterval(pollRef.current);
            pollRef.current = null;
          }
        } catch (_) {}
      }, 1000);
    } catch (e) { setError(e.message); }
  };

  const stopGenerator = async () => {
    try {
      await fetch(`${API_URL}/api/randomizer/stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_id: selectedSource.id })
      });
      setRunning(false);
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    } catch (e) { setError(e.message); }
  };

  const resetAll = () => {
    if (running) return;
    setSelectedSource(null);
    setTables([]);
    setSelectedTable('');
    setColumns([]);
    setSelectedCols({});
    setStatus(null);
    setError('');
  };

  // ── RENDER ──────────────────────────────────────────────

  // Step 1: Source selection
  if (!selectedSource) {
    return (
      <div className="content">
        <h1>Randomizer — Data Generator</h1>
        <p style={{ color: '#7f8c8d', marginBottom: 20 }}>Select a data source to generate random test data.</p>
        {error && <div style={{ color: '#c0392b', marginBottom: 12 }}>{error}</div>}
        <div className="table-container">
          <table className="data-table">
            <thead><tr><th>Name</th><th>Engine</th><th>Host</th><th>Database</th><th>Action</th></tr></thead>
            <tbody>
              {sources.length === 0 && <tr><td colSpan="5" style={{ textAlign: 'center', padding: 30, color: '#999' }}>No data sources defined. Add one in "Data Sources" first.</td></tr>}
              {sources.map(s => (
                <tr key={s.id}>
                  <td>{s.name}</td>
                  <td><span className="status-badge status-active">{s.engine}</span></td>
                  <td>{s.host}:{s.port}</td>
                  <td>{s.database_name}</td>
                  <td><button className="btn btn-primary btn-small" onClick={() => analyzeSource(s)}>Analyze</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    );
  }

  // Step 2: Table selection
  if (!selectedTable) {
    return (
      <div className="content">
        <h1>Randomizer — {selectedSource.name}</h1>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 16 }}>
          <span className="status-badge status-active">{selectedSource.engine}</span>
          <span style={{ color: '#7f8c8d' }}>{selectedSource.host}:{selectedSource.port} / {selectedSource.database_name}</span>
          <button className="btn btn-secondary btn-small" onClick={resetAll} style={{ marginLeft: 'auto' }}>← Back</button>
        </div>
        {error && <div style={{ color: '#c0392b', marginBottom: 12 }}>{error}</div>}
        {loadingTables ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: 30 }}><div className="spinner"></div> Connecting & listing tables…</div>
        ) : (
          <div className="table-container">
            <table className="data-table">
              <thead><tr><th>#</th><th>Table Name</th><th>Action</th></tr></thead>
              <tbody>
                {tables.length === 0 && <tr><td colSpan="3" style={{ textAlign: 'center', padding: 30, color: '#999' }}>No tables found.</td></tr>}
                {tables.map((t, i) => (
                  <tr key={t}>
                    <td>{i + 1}</td>
                    <td>{t}</td>
                    <td><button className="btn btn-primary btn-small" onClick={() => selectTable(t)}>Select</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    );
  }

  // Step 3: Column selection + generator controls
  const activeCount = Object.values(selectedCols).filter(Boolean).length;

  return (
    <div className="content">
      <h1>Randomizer — {selectedSource.name}</h1>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 16 }}>
        <span className="status-badge status-active">{selectedSource.engine}</span>
        <span style={{ color: '#7f8c8d' }}>{selectedSource.database_name}</span>
        <span style={{ color: '#34495e', fontWeight: 600 }}>→ {selectedTable}</span>
        {!running && <button className="btn btn-secondary btn-small" onClick={() => { setSelectedTable(''); setColumns([]); setSelectedCols({}); setStatus(null); }} style={{ marginLeft: 'auto' }}>← Tables</button>}
        {!running && <button className="btn btn-secondary btn-small" onClick={resetAll}>← Sources</button>}
      </div>

      {error && <div style={{ color: '#c0392b', marginBottom: 12 }}>{error}</div>}

      {loadingCols ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: 30 }}><div className="spinner"></div> Loading columns…</div>
      ) : (
        <>
          {/* Column selection table */}
          <div className="table-container" style={{ marginBottom: 20 }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th style={{ width: 40 }}>
                    <input type="checkbox" checked={activeCount === columns.length && columns.length > 0}
                      onChange={e => selectAllCols(e.target.checked)} disabled={running} />
                  </th>
                  <th>Column</th>
                  <th>Type</th>
                  <th>Constraints</th>
                  <th>Nullable</th>
                  <th>Default</th>
                </tr>
              </thead>
              <tbody>
                {columns.map(c => (
                  <tr key={c.name} style={{ opacity: selectedCols[c.name] ? 1 : 0.5 }}>
                    <td><input type="checkbox" checked={!!selectedCols[c.name]} onChange={() => toggleCol(c.name)} disabled={running} /></td>
                    <td style={{ fontFamily: 'monospace' }}>{c.name}</td>
                    <td><span className="status-badge status-configured">{c.type}{c.max_length ? `(${c.max_length})` : ''}</span></td>
                    <td style={{ fontFamily: 'monospace', fontSize: 11, color: '#7f8c8d', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {c.allowed_values ? c.allowed_values.join(', ') : '—'}
                    </td>
                    <td>{c.nullable}</td>
                    <td style={{ fontFamily: 'monospace', fontSize: 12, color: '#7f8c8d' }}>{c.default || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Controls */}
          <div className="form-container" style={{ maxWidth: 600 }}>
            <div className="form-row">
              <div className="form-group">
                <label>Records per batch</label>
                <input type="number" min="1" max="100" value={batchSize}
                  onChange={e => setBatchSize(Math.max(1, Math.min(100, parseInt(e.target.value) || 1)))} disabled={running} />
              </div>
              <div className="form-group">
                <label>Interval (seconds)</label>
                <select value={interval} onChange={e => setInterval_(parseInt(e.target.value))} disabled={running}>
                  {[1,2,3,4,5,6,7,8,9,10].map(v => <option key={v} value={v}>{v}s</option>)}
                </select>
              </div>
            </div>

            <div style={{ display: 'flex', gap: 12, marginTop: 16 }}>
              {!running ? (
                <button className="btn btn-success" onClick={startGenerator} disabled={activeCount === 0}>
                  ▶ Start Generator
                </button>
              ) : (
                <button className="btn btn-danger" onClick={stopGenerator}>
                  ■ Stop Generator
                </button>
              )}
            </div>
          </div>

          {/* Status panel */}
          {status && (
            <div className="form-container" style={{ maxWidth: 600, marginTop: 20 }}>
              <h2 style={{ fontSize: 16, marginBottom: 12 }}>Generator Status</h2>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                <div style={{ padding: 16, background: running ? '#e8f5e9' : '#eceff1', borderRadius: 8, textAlign: 'center' }}>
                  <div style={{ fontSize: 28, fontWeight: 700, color: running ? '#388e3c' : '#546e7a' }}>{status.records_written?.toLocaleString() || 0}</div>
                  <div style={{ fontSize: 12, color: '#7f8c8d' }}>Records Written</div>
                </div>
                <div style={{ padding: 16, background: status.errors > 0 ? '#ffebee' : '#eceff1', borderRadius: 8, textAlign: 'center' }}>
                  <div style={{ fontSize: 28, fontWeight: 700, color: status.errors > 0 ? '#d32f2f' : '#546e7a' }}>{status.errors || 0}</div>
                  <div style={{ fontSize: 12, color: '#7f8c8d' }}>Errors</div>
                </div>
              </div>
              <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 12, color: '#7f8c8d' }}>Status:</span>
                <span className={`status-badge ${status.status === 'running' ? 'status-running' : status.status === 'error' ? 'status-error' : 'status-stopped'}`}>
                  {status.status}
                </span>
              </div>
              {status.last_error && (
                <div style={{ marginTop: 8, padding: 8, background: '#ffebee', borderRadius: 4, fontSize: 12, color: '#c62828' }}>
                  Last error: {status.last_error}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default Randomizer;
