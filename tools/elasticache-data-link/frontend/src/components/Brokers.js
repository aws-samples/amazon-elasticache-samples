import React, { useState, useEffect } from 'react';
import axios from 'axios';
import './Servers.css';
import BrokerForm from './BrokerForm';
import API_URL from '../apiUrl';


function Brokers() {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [isEdit, setIsEdit] = useState(false);

  useEffect(() => { fetchItems(); }, []);

  const fetchItems = async () => {
    try {
      const res = await axios.get(`${API_URL}/api/brokers`);
      setItems(res.data);
    } catch (e) { console.error('Error:', e); }
  };

  const handleNew = () => { setSelected(null); setIsEdit(false); setShowForm(true); };
  const handleEdit = (item) => { setSelected(item); setIsEdit(true); setShowForm(true); };

  const handleSave = async (data) => {
    try {
      if (isEdit) await axios.put(`${API_URL}/api/brokers/${data.id}`, data);
      else await axios.post(`${API_URL}/api/brokers`, data);
      await fetchItems();
      setShowForm(false); setSelected(null);
    } catch (e) {
      alert(`Error: ${e.response?.data?.error || e.message}`);
    }
  };

  const handleDelete = async () => {
    if (!selected) return;
    if (window.confirm(`Delete broker "${selected.name}"?`)) {
      await axios.delete(`${API_URL}/api/brokers/${selected.id}`);
      await fetchItems(); setSelected(null);
    }
  };

  return (
    <div className="servers-container">
      <div className="header">
        <h1>Message Brokers</h1>
        <div className="button-group">
          <button className="btn btn-primary" onClick={handleNew}>New</button>
          <button className="btn btn-secondary" onClick={() => selected && handleEdit(selected)}
            disabled={!selected}>Edit</button>
          <button className="btn btn-danger" onClick={handleDelete} disabled={!selected}>Delete</button>
        </div>
      </div>
      {showForm ? (
        <BrokerForm broker={isEdit ? selected : null} onSave={handleSave}
          onCancel={() => { setShowForm(false); setSelected(null); }} />
      ) : (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th><th>Type</th><th>Auth</th><th>Endpoint</th>
                <th>Partitions</th><th>Retention</th><th>Status</th><th>Created</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map(item => (
                <tr key={item.id} className={selected?.id === item.id ? 'selected' : ''}
                  onClick={() => setSelected(item)}>
                  <td>{item.name}</td>
                  <td>{item.broker_type === 'kafka' ? 'Kafka (MSK)' : 'Kinesis'}</td>
                  <td>{item.auth_type === 'iam' ? 'IAM' : 'None'}</td>
                  <td style={{ fontFamily: 'monospace', fontSize: '12px', maxWidth: '250px', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {item.broker_type === 'kafka' ? item.bootstrap_servers : item.stream_name}
                  </td>
                  <td>{item.partitions}</td>
                  <td>{item.retention_hours}h</td>
                  <td><span className={`status-badge status-${item.status}`}>{item.status}</span></td>
                  <td>{item.created}</td>
                  <td>
                    <div className="action-buttons">
                      <button className="btn btn-small btn-secondary" onClick={(e) => { e.stopPropagation(); handleEdit(item); }}>Edit</button>
                      <button className="btn btn-small btn-danger" onClick={(e) => { e.stopPropagation(); if (window.confirm(`Delete broker "${item.name}"?`)) { axios.delete(`${API_URL}/api/brokers/${item.id}`).then(fetchItems); } }}>Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr><td colSpan="9" style={{ textAlign: 'center', color: '#999', padding: '30px' }}>
                  No brokers defined yet. Click "New" to add one.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default Brokers;
