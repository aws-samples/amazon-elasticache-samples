import React, { useState, useEffect } from 'react';
import axios from 'axios';
import './Servers.css';
import TargetForm from './TargetForm';
import API_URL from '../apiUrl';


function Targets() {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [isEdit, setIsEdit] = useState(false);

  useEffect(() => { fetchItems(); }, []);

  const fetchItems = async () => {
    try {
      const res = await axios.get(`${API_URL}/api/targets`);
      setItems(res.data);
    } catch (e) { console.error('Error:', e); }
  };

  const handleNew = () => { setSelected(null); setIsEdit(false); setShowForm(true); };
  const handleEdit = (item) => { setSelected(item); setIsEdit(true); setShowForm(true); };

  const handleSave = async (data) => {
    try {
      if (isEdit) await axios.put(`${API_URL}/api/targets/${data.id}`, data);
      else await axios.post(`${API_URL}/api/targets`, data);
      await fetchItems();
      setShowForm(false); setSelected(null);
    } catch (e) {
      alert(`Error: ${e.response?.data?.error || e.message}`);
    }
  };

  const handleDelete = async () => {
    if (!selected) return;
    if (window.confirm(`Delete target "${selected.name}"?`)) {
      await axios.delete(`${API_URL}/api/targets/${selected.id}`);
      await fetchItems(); setSelected(null);
    }
  };

  return (
    <div className="servers-container">
      <div className="header">
        <h1>Valkey Targets</h1>
        <div className="button-group">
          <button className="btn btn-primary" onClick={handleNew}>New</button>
          <button className="btn btn-secondary" onClick={() => selected && handleEdit(selected)}
            disabled={!selected}>Edit</button>
          <button className="btn btn-danger" onClick={handleDelete} disabled={!selected}>Delete</button>
        </div>
      </div>
      {showForm ? (
        <TargetForm target={isEdit ? selected : null} onSave={handleSave}
          onCancel={() => { setShowForm(false); setSelected(null); }} />
      ) : (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th><th>Endpoint</th><th>Port</th>
                <th>Write Mode</th><th>Batch</th><th>LWW</th><th>Dev</th><th>Status</th><th>Created</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map(item => (
                <tr key={item.id} className={selected?.id === item.id ? 'selected' : ''}
                  onClick={() => setSelected(item)}>
                  <td>{item.name}</td>
                  <td style={{ fontFamily: 'monospace', fontSize: '12px' }}>{item.cluster_endpoint}</td>
                  <td>{item.port}</td>
                  <td>{item.write_mode}</td>
                  <td>{item.pipeline_batch_size}</td>
                  <td>{item.lww_enabled ? '✓' : '—'}</td>
                  <td>{item.dev_test ? '✓' : '—'}</td>
                  <td><span className={`status-badge status-${item.status}`}>{item.status}</span></td>
                  <td>{item.created}</td>
                  <td>
                    <div className="action-buttons">
                      <button className="btn btn-small btn-secondary" onClick={(e) => { e.stopPropagation(); handleEdit(item); }}>Edit</button>
                      <button className="btn btn-small btn-danger" onClick={(e) => { e.stopPropagation(); if (window.confirm(`Delete target "${item.name}"?`)) { axios.delete(`${API_URL}/api/targets/${item.id}`).then(fetchItems); } }}>Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr><td colSpan="10" style={{ textAlign: 'center', color: '#999', padding: '30px' }}>
                  No Valkey targets defined yet. Select "New" to add one.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default Targets;
