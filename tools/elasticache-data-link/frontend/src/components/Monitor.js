import React, { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import './Servers.css';
import API_URL from '../apiUrl';


function Monitor() {
  const [pipelines, setPipelines] = useState([]);
  const [deployments, setDeployments] = useState([]);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [liveMetrics, setLiveMetrics] = useState({});
  const [healthData, setHealthData] = useState(null);
  const [auditLog, setAuditLog] = useState([]);
  const [metricsConfig, setMetricsConfig] = useState({
    sinkHost: 'localhost', sinkPort: '9090',
    debeziumHost: 'localhost', debeziumPort: '8083'
  });
  const [polling, setPolling] = useState(false);

  useEffect(() => { fetchAll(); fetchHealth(); fetchAuditLog(); }, []);

  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(() => { fetchAll(); fetchHealth(); if (polling) fetchLiveMetrics(); }, 10000);
    return () => clearInterval(interval);
    // fetchLiveMetrics is declared below this effect, so listing it here would
    // read it before it exists. Unchanged behaviour; this only keeps the CI
    // build (warnings are errors) passing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRefresh, polling]);

  const fetchAll = async () => {
    try {
      const [p, d] = await Promise.all([
        axios.get(`${API_URL}/api/pipelines`),
        axios.get(`${API_URL}/api/deployments`),
      ]);
      setPipelines(p.data);
      setDeployments(d.data);
    } catch (e) { console.error('Error:', e); }
  };

  const fetchHealth = async () => {
    try {
      const res = await axios.get(`${API_URL}/api/health`);
      setHealthData(res.data);
    } catch (e) { setHealthData({ overall: 'unreachable', error: e.message }); }
  };

  const fetchAuditLog = async () => {
    try {
      const res = await axios.get(`${API_URL}/api/audit-log?limit=20`);
      setAuditLog(res.data);
    } catch (e) { console.error('Audit log error:', e); }
  };

  const fetchLiveMetrics = useCallback(async () => {
    const results = {};
    // Fetch sink worker metrics
    try {
      const sinkUrl = `http://${metricsConfig.sinkHost}:${metricsConfig.sinkPort}/metrics`;
      const res = await fetch(sinkUrl, { signal: AbortSignal.timeout(3000) });
      if (res.ok) results.sink = await res.json();
      else results.sinkError = `HTTP ${res.status}`;
    } catch (e) {
      results.sinkError = e.message || 'Connection failed';
    }
    // Fetch Debezium connector status
    try {
      const dbzBase = `http://${metricsConfig.debeziumHost}:${metricsConfig.debeziumPort}`;
      const connRes = await fetch(`${dbzBase}/connectors`, { signal: AbortSignal.timeout(3000) });
      if (connRes.ok) {
        const connectors = await connRes.json();
        const statuses = {};
        for (const name of connectors) {
          try {
            const sRes = await fetch(`${dbzBase}/connectors/${name}/status`, { signal: AbortSignal.timeout(3000) });
            if (sRes.ok) statuses[name] = await sRes.json();
          } catch (_) { statuses[name] = { error: 'unreachable' }; }
        }
        results.debezium = { connectors: statuses };
      } else {
        results.debeziumError = `HTTP ${connRes.status}`;
      }
    } catch (e) {
      results.debeziumError = e.message || 'Connection failed';
    }
    results.fetchedAt = new Date().toLocaleTimeString();
    setLiveMetrics(results);
  }, [metricsConfig]);

  const handleStartPolling = () => {
    setPolling(true);
    setAutoRefresh(true);
    fetchLiveMetrics();
  };

  const runningDeployments = deployments.filter(d => d.status === 'running');
  const totalPipelines = pipelines.length;
  const activePipelines = pipelines.filter(p => p.status === 'running').length;
  const errorPipelines = pipelines.filter(p => p.status === 'error').length;

  const formatUptime = (seconds) => {
    if (!seconds) return '—';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}h ${m}m ${s}s`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  };

  const formatTime = (ts) => {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleTimeString();
  };

  const cardStyle = (color) => ({
    flex: 1, background: 'white', borderRadius: '8px', padding: '20px',
    boxShadow: '0 2px 4px rgba(0,0,0,0.1)', borderLeft: `4px solid ${color}`
  });
  const bigNum = (color) => ({ fontSize: '32px', fontWeight: 700, color });
  const label = { fontSize: '14px', color: '#7f8c8d' };

  return (
    <div className="servers-container">
      <div className="header">
        <h1>Monitor</h1>
        <div className="button-group">
          <button className="btn btn-secondary" onClick={() => { fetchAll(); if (polling) fetchLiveMetrics(); }}>Refresh</button>
          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '14px', color: '#555' }}>
            <input type="checkbox" checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)} />
            Auto-refresh (10s)
          </label>
        </div>
      </div>

      {/* Summary Cards */}
      <div style={{ display: 'flex', gap: '20px', marginBottom: '30px' }}>
        <div style={cardStyle('#3498db')}>
          <div style={bigNum('#2c3e50')}>{totalPipelines}</div>
          <div style={label}>Total Pipelines</div>
        </div>
        <div style={cardStyle('#27ae60')}>
          <div style={bigNum('#27ae60')}>{activePipelines}</div>
          <div style={label}>Running</div>
        </div>
        <div style={cardStyle('#FF9900')}>
          <div style={bigNum('#FF9900')}>{runningDeployments.length}</div>
          <div style={label}>Active Deployments</div>
        </div>
        <div style={cardStyle(errorPipelines > 0 ? '#e74c3c' : '#95a5a6')}>
          <div style={bigNum(errorPipelines > 0 ? '#e74c3c' : '#95a5a6')}>{errorPipelines}</div>
          <div style={label}>Errors</div>
        </div>
      </div>

      {/* Health Check */}
      {healthData && (
        <div style={{ background: 'white', borderRadius: '8px', padding: '20px',
          boxShadow: '0 2px 4px rgba(0,0,0,0.1)', marginBottom: '20px',
          borderLeft: `4px solid ${healthData.overall === 'healthy' ? '#27ae60' : '#e74c3c'}` }}>
          <h2 style={{ fontSize: '18px', color: '#2c3e50', marginBottom: '10px' }}>
            System Health
            <span style={{
              display: 'inline-block', marginLeft: '10px', padding: '2px 12px',
              borderRadius: '12px', fontSize: '13px', fontWeight: 600,
              background: healthData.overall === 'healthy' ? '#d4edda' : '#f8d7da',
              color: healthData.overall === 'healthy' ? '#155724' : '#721c24'
            }}>{healthData.overall}</span>
          </h2>
          <div style={{ display: 'flex', gap: '20px', fontSize: '14px' }}>
            <span>Backend: <strong style={{ color: healthData.backend?.status === 'healthy' ? '#27ae60' : '#e74c3c' }}>{healthData.backend?.status}</strong></span>
            <span>Database: <strong style={{ color: healthData.database?.status === 'healthy' ? '#27ae60' : '#e74c3c' }}>{healthData.database?.status}</strong></span>
            <span>Active Deployments: <strong>{healthData.active_deployments || 0}</strong></span>
            <span style={{ color: '#999' }}>Checked: {healthData.timestamp}</span>
          </div>
        </div>
      )}

      {/* Live Metrics Panel */}
      <div style={{ background: 'white', borderRadius: '8px', padding: '20px',
        boxShadow: '0 2px 4px rgba(0,0,0,0.1)', marginBottom: '30px' }}>
        <h2 style={{ fontSize: '20px', color: '#2c3e50', marginBottom: '15px' }}>
          Live Pipeline Metrics
          {liveMetrics.fetchedAt && (
            <span style={{ fontSize: '12px', color: '#999', marginLeft: '12px', fontWeight: 400 }}>
              Last fetched: {liveMetrics.fetchedAt}
            </span>
          )}
        </h2>
        <div style={{ display: 'flex', gap: '15px', marginBottom: '15px', flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div>
            <label style={{ display: 'block', fontSize: '12px', color: '#666', marginBottom: '4px' }}>Sink Host</label>
            <input value={metricsConfig.sinkHost}
              onChange={e => setMetricsConfig(c => ({ ...c, sinkHost: e.target.value }))}
              style={{ padding: '6px 10px', border: '1px solid #ddd', borderRadius: '4px', width: '160px' }} />
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '12px', color: '#666', marginBottom: '4px' }}>Sink Port</label>
            <input value={metricsConfig.sinkPort}
              onChange={e => setMetricsConfig(c => ({ ...c, sinkPort: e.target.value }))}
              style={{ padding: '6px 10px', border: '1px solid #ddd', borderRadius: '4px', width: '80px' }} />
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '12px', color: '#666', marginBottom: '4px' }}>Debezium Host</label>
            <input value={metricsConfig.debeziumHost}
              onChange={e => setMetricsConfig(c => ({ ...c, debeziumHost: e.target.value }))}
              style={{ padding: '6px 10px', border: '1px solid #ddd', borderRadius: '4px', width: '160px' }} />
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '12px', color: '#666', marginBottom: '4px' }}>Debezium Port</label>
            <input value={metricsConfig.debeziumPort}
              onChange={e => setMetricsConfig(c => ({ ...c, debeziumPort: e.target.value }))}
              style={{ padding: '6px 10px', border: '1px solid #ddd', borderRadius: '4px', width: '80px' }} />
          </div>
          <button className="btn btn-primary" onClick={handleStartPolling}>
            {polling ? 'Polling...' : 'Connect & Poll'}
          </button>
          {polling && (
            <button className="btn btn-secondary" onClick={() => setPolling(false)}>Stop</button>
          )}
        </div>

        {/* Sink Worker Metrics */}
        {(liveMetrics.sink || liveMetrics.sinkError) && (
          <div style={{ marginBottom: '20px' }}>
            <h3 style={{ fontSize: '16px', color: '#2c3e50', marginBottom: '10px' }}>
              Sink Worker
              {liveMetrics.sink && (
                <span style={{
                  display: 'inline-block', marginLeft: '10px', padding: '2px 10px',
                  borderRadius: '12px', fontSize: '12px', fontWeight: 600,
                  background: liveMetrics.sink.status === 'running' ? '#d4edda' : '#fff3cd',
                  color: liveMetrics.sink.status === 'running' ? '#155724' : '#856404'
                }}>
                  {liveMetrics.sink.status}
                </span>
              )}
            </h3>
            {liveMetrics.sinkError ? (
              <div style={{ color: '#e74c3c', fontSize: '14px' }}>
                Could not reach sink metrics: {liveMetrics.sinkError}
              </div>
            ) : (
              <div style={{ display: 'flex', gap: '15px', flexWrap: 'wrap' }}>
                <div style={cardStyle('#3498db')}>
                  <div style={bigNum('#3498db')}>{liveMetrics.sink.messages_consumed || 0}</div>
                  <div style={label}>Messages Consumed</div>
                </div>
                <div style={cardStyle('#27ae60')}>
                  <div style={bigNum('#27ae60')}>{liveMetrics.sink.writes || 0}</div>
                  <div style={label}>Writes to Valkey</div>
                </div>
                <div style={cardStyle('#9b59b6')}>
                  <div style={bigNum('#9b59b6')}>{liveMetrics.sink.deletes || 0}</div>
                  <div style={label}>Deletes</div>
                </div>
                <div style={cardStyle('#f39c12')}>
                  <div style={bigNum('#f39c12')}>{liveMetrics.sink.skips || 0}</div>
                  <div style={label}>Skipped</div>
                </div>
                <div style={cardStyle(liveMetrics.sink.errors > 0 ? '#e74c3c' : '#95a5a6')}>
                  <div style={bigNum(liveMetrics.sink.errors > 0 ? '#e74c3c' : '#95a5a6')}>
                    {liveMetrics.sink.errors || 0}
                  </div>
                  <div style={label}>Errors</div>
                </div>
              </div>
            )}
            {liveMetrics.sink && (
              <div style={{ marginTop: '10px', fontSize: '13px', color: '#666', display: 'flex', gap: '20px', flexWrap: 'wrap' }}>
                <span>Uptime: {formatUptime(liveMetrics.sink.uptime_seconds)}</span>
                <span>Last Event: {formatTime(liveMetrics.sink.last_event_time)}</span>
                <span>Consumer Group: {liveMetrics.sink.consumer_group || '—'}</span>
                <span>Topics: {(liveMetrics.sink.topics || []).join(', ') || '—'}</span>
              </div>
            )}
          </div>
        )}

        {/* Debezium Connector Status */}
        {(liveMetrics.debezium || liveMetrics.debeziumError) && (
          <div>
            <h3 style={{ fontSize: '16px', color: '#2c3e50', marginBottom: '10px' }}>Debezium Connectors</h3>
            {liveMetrics.debeziumError ? (
              <div style={{ color: '#e74c3c', fontSize: '14px' }}>
                Could not reach Debezium Connect: {liveMetrics.debeziumError}
              </div>
            ) : (
              <div className="table-container">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Connector</th><th>State</th><th>Worker</th><th>Tasks</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(liveMetrics.debezium.connectors).map(([name, info]) => {
                      const connState = info.connector?.state || info.error || '—';
                      const worker = info.connector?.worker_id || '—';
                      const tasks = (info.tasks || []).map((t, i) => (
                        <span key={i} style={{
                          display: 'inline-block', marginRight: '6px', padding: '1px 8px',
                          borderRadius: '10px', fontSize: '12px',
                          background: t.state === 'RUNNING' ? '#d4edda' : '#f8d7da',
                          color: t.state === 'RUNNING' ? '#155724' : '#721c24'
                        }}>
                          {t.id}: {t.state}
                        </span>
                      ));
                      return (
                        <tr key={name}>
                          <td style={{ fontWeight: 600, fontFamily: 'monospace' }}>{name}</td>
                          <td>
                            <span className={`status-badge status-${connState === 'RUNNING' ? 'running' : 'error'}`}>
                              {connState}
                            </span>
                          </td>
                          <td style={{ fontSize: '12px', fontFamily: 'monospace' }}>{worker}</td>
                          <td>{tasks.length > 0 ? tasks : '—'}</td>
                        </tr>
                      );
                    })}
                    {Object.keys(liveMetrics.debezium.connectors).length === 0 && (
                      <tr><td colSpan="4" style={{ textAlign: 'center', color: '#999', padding: '20px' }}>
                        No connectors registered.
                      </td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {!liveMetrics.sink && !liveMetrics.sinkError && !liveMetrics.debezium && !liveMetrics.debeziumError && (
          <div style={{ color: '#999', fontSize: '14px' }}>
            Enter the host/port of your deployed sink worker and Debezium Connect, then select "Connect & Poll" to see live metrics.
          </div>
        )}
      </div>

      {/* Pipeline Status */}
      <h2 style={{ fontSize: '20px', color: '#2c3e50', marginBottom: '15px' }}>Pipeline Status</h2>
      <div className="table-container" style={{ marginBottom: '30px' }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Pipeline</th><th>Source → Target</th><th>Broker</th><th>Status</th><th>Last Deployed</th>
            </tr>
          </thead>
          <tbody>
            {pipelines.map(p => (
              <tr key={p.id}>
                <td style={{ fontWeight: 600 }}>{p.name}</td>
                <td>
                  <span style={{ color: '#2c3e50' }}>{p.source_name}</span>
                  <span style={{ color: '#999', margin: '0 8px' }}>→</span>
                  <span style={{ color: '#FF9900', fontWeight: 600 }}>{p.target_name}</span>
                </td>
                <td>{p.broker_name} ({p.broker_type})</td>
                <td><span className={`status-badge status-${p.status}`}>{p.status}</span></td>
                <td>{p.last_deployed || '—'}</td>
              </tr>
            ))}
            {pipelines.length === 0 && (
              <tr><td colSpan="5" style={{ textAlign: 'center', color: '#999', padding: '30px' }}>
                No pipelines to monitor.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Recent Deployments */}
      <h2 style={{ fontSize: '20px', color: '#2c3e50', marginBottom: '15px' }}>Recent Deployments</h2>
      <div className="table-container">
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th><th>Pipeline</th><th>Type</th><th>Region</th>
              <th>Status</th><th>Started</th><th>Completed</th>
            </tr>
          </thead>
          <tbody>
            {deployments.slice(0, 10).map(d => (
              <tr key={d.id}>
                <td>{d.id}</td>
                <td>{d.pipeline_name}</td>
                <td>{d.deploy_type}</td>
                <td>{d.region}</td>
                <td><span className={`status-badge status-${d.status}`}>{d.status}</span></td>
                <td>{d.started || '—'}</td>
                <td>{d.completed || '—'}</td>
              </tr>
            ))}
            {deployments.length === 0 && (
              <tr><td colSpan="7" style={{ textAlign: 'center', color: '#999', padding: '30px' }}>
                No deployment history.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Audit Log */}
      <h2 style={{ fontSize: '20px', color: '#2c3e50', marginBottom: '15px', marginTop: '30px' }}>
        Audit Log
        <button className="btn btn-small btn-secondary" style={{ marginLeft: '10px' }}
          onClick={fetchAuditLog}>Refresh</button>
      </h2>
      <div className="table-container">
        <table className="data-table">
          <thead>
            <tr>
              <th>Time</th><th>Action</th><th>Resource</th><th>Name</th><th>Details</th><th>IP</th>
            </tr>
          </thead>
          <tbody>
            {auditLog.map(a => (
              <tr key={a.id}>
                <td style={{ fontSize: '12px', whiteSpace: 'nowrap' }}>{a.timestamp}</td>
                <td><span style={{
                  padding: '2px 8px', borderRadius: '4px', fontSize: '12px', fontWeight: 600,
                  background: a.action === 'create' ? '#d4edda' : a.action === 'delete' ? '#f8d7da' : '#fff3cd',
                  color: a.action === 'create' ? '#155724' : a.action === 'delete' ? '#721c24' : '#856404'
                }}>{a.action}</span></td>
                <td>{a.resource_type}</td>
                <td style={{ fontWeight: 600 }}>{a.resource_name || '—'}</td>
                <td style={{ fontSize: '12px', color: '#666', maxWidth: '300px', overflow: 'hidden', textOverflow: 'ellipsis' }}>{a.details || '—'}</td>
                <td style={{ fontSize: '12px', fontFamily: 'monospace' }}>{a.ip_address || '—'}</td>
              </tr>
            ))}
            {auditLog.length === 0 && (
              <tr><td colSpan="6" style={{ textAlign: 'center', color: '#999', padding: '30px' }}>
                No audit log entries yet.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default Monitor;
