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
    </div>
  );
}

export default Pipelines;
