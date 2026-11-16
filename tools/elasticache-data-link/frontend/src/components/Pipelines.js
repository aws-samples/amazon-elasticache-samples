import React, { useState, useEffect } from 'react';
import axios from 'axios';
import './Servers.css';
import PipelineForm from './PipelineForm';
import API_URL from '../apiUrl';


function Pipelines() {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [isEdit, setIsEdit] = useState(false);
  const [configPopup, setConfigPopup] = useState(null);
  const [loadingConfig, setLoadingConfig] = useState(false);

  useEffect(() => { fetchItems(); }, []);

  const fetchItems = async () => {
    try {
      const res = await axios.get(`${API_URL}/api/pipelines`);
      setItems(res.data);
    } catch (e) { console.error('Error:', e); }
  };

  const handleNew = () => { setSelected(null); setIsEdit(false); setShowForm(true); };
  const handleEdit = (item) => { setSelected(item); setIsEdit(true); setShowForm(true); };

  const handleSave = async (data) => {
    try {
      if (isEdit) await axios.put(`${API_URL}/api/pipelines/${data.id}`, data);
      else await axios.post(`${API_URL}/api/pipelines`, data);
      await fetchItems();
      setShowForm(false); setSelected(null);
    } catch (e) {
      alert(`Error: ${e.response?.data?.error || e.message}`);
    }
  };

  const handleDelete = async () => {
    if (!selected) return;
    if (window.confirm(`Delete pipeline "${selected.name}"?`)) {
      await axios.delete(`${API_URL}/api/pipelines/${selected.id}`);
      await fetchItems(); setSelected(null);
    }
  };

  const handleViewConfig = async (item, e) => {
    e.stopPropagation();
    setLoadingConfig(true);
    try {
      const res = await axios.get(`${API_URL}/api/pipelines/${item.id}/generate-config`);
      setConfigPopup({ name: item.name, ...res.data });
    } catch (e) {
      alert(`Error generating config: ${e.response?.data?.error || e.message}`);
    } finally {
      setLoadingConfig(false);
    }
  };

  return (
    <div className="servers-container">
      <div className="header">
        <h1>Pipelines</h1>
        <div className="button-group">
          <button className="btn btn-primary" onClick={handleNew}>New</button>
          <button className="btn btn-secondary" onClick={() => selected && handleEdit(selected)}
            disabled={!selected}>Edit</button>
          <button className="btn btn-danger" onClick={handleDelete} disabled={!selected}>Delete</button>
        </div>
      </div>
      {showForm ? (
        <PipelineForm pipeline={isEdit ? selected : null} onSave={handleSave}
          onCancel={() => { setShowForm(false); setSelected(null); }} />
      ) : (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th><th>Source</th><th>Connector</th>
                <th>Broker</th><th>Target</th><th>Status</th><th>Last Deployed</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map(item => (
                <tr key={item.id} className={selected?.id === item.id ? 'selected' : ''}
                  onClick={() => setSelected(item)}>
                  <td style={{ fontWeight: 600 }}>{item.name}</td>
                  <td>{item.source_name || '—'} <span style={{ color: '#999', fontSize: '11px' }}>({item.source_engine})</span></td>
                  <td>{item.connector_name || '—'}</td>
                  <td>{item.broker_name || '—'} <span style={{ color: '#999', fontSize: '11px' }}>({item.broker_type})</span></td>
                  <td>{item.target_name || '—'}</td>
                  <td><span className={`status-badge status-${item.status}`}>{item.status}</span></td>
                  <td>{item.last_deployed || '—'}</td>
                  <td>
                    <div className="action-buttons">
                      <button className="btn btn-small btn-secondary" onClick={(e) => { e.stopPropagation(); handleEdit(item); }}>Edit</button>
                      <button className="btn btn-small btn-danger" onClick={(e) => { e.stopPropagation(); if (window.confirm(`Delete pipeline "${item.name}"?`)) { axios.delete(`${API_URL}/api/pipelines/${item.id}`).then(fetchItems); } }}>Delete</button>
                      <button className="btn-icon" onClick={(e) => handleViewConfig(item, e)}
                        disabled={loadingConfig} title="View Generated Config">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                          strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="16 18 22 12 16 6"></polyline>
                          <polyline points="8 6 2 12 8 18"></polyline>
                        </svg>
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr><td colSpan="8" style={{ textAlign: 'center', color: '#999', padding: '30px' }}>
                  No pipelines defined yet. Define sources, connectors, brokers, and targets first.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {configPopup && (
        <div className="popup-overlay" onClick={() => setConfigPopup(null)}>
          <div className="popup-content" style={{ minWidth: '700px', maxWidth: '900px' }}
            onClick={(e) => e.stopPropagation()}>
            <div className="popup-header">
              <h2>Generated Config: {configPopup.name}</h2>
              <button className="popup-close" onClick={() => setConfigPopup(null)}>×</button>
            </div>
            <div style={{ padding: '20px', overflow: 'auto', maxHeight: 'calc(80vh - 60px)' }}>
              <h3 style={{ marginBottom: '10px', color: '#2c3e50' }}>Debezium Connector Config</h3>
              <pre style={{ background: '#f8f9fa', padding: '15px', borderRadius: '6px',
                fontSize: '13px', overflow: 'auto', border: '1px solid #e0e0e0' }}>
                {JSON.stringify(configPopup.debezium_config, null, 2)}
              </pre>
              <h3 style={{ marginTop: '20px', marginBottom: '10px', color: '#2c3e50' }}>Valkey Sink Worker Config</h3>
              <pre style={{ background: '#f8f9fa', padding: '15px', borderRadius: '6px',
                fontSize: '13px', overflow: 'auto', border: '1px solid #e0e0e0' }}>
                {JSON.stringify(configPopup.sink_config, null, 2)}
              </pre>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default Pipelines;
