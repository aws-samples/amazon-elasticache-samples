import React, { useState, useEffect } from 'react';
import axios from 'axios';
import './Form.css';
import API_URL from '../apiUrl';


function PipelineForm({ pipeline, onSave, onCancel }) {
  const [formData, setFormData] = useState({
    name: '', source_id: '', connector_id: '', broker_id: '', target_id: '',
    status: 'draft', notes: ''
  });
  const [sources, setSources] = useState([]);
  const [connectors, setConnectors] = useState([]);
  const [brokers, setBrokers] = useState([]);
  const [targets, setTargets] = useState([]);

  useEffect(() => {
    fetchAll();
    if (pipeline) setFormData(pipeline);
  }, [pipeline]);

  const fetchAll = async () => {
    try {
      const [s, c, b, t] = await Promise.all([
        axios.get(`${API_URL}/api/sources`),
        axios.get(`${API_URL}/api/connectors`),
        axios.get(`${API_URL}/api/brokers`),
        axios.get(`${API_URL}/api/targets`),
      ]);
      setSources(s.data);
      setConnectors(c.data);
      setBrokers(b.data);
      setTargets(t.data);
    } catch (e) { console.error('Error fetching data:', e); }
  };

  const handleChange = (e) => {
    setFormData(prev => ({ ...prev, [e.target.name]: e.target.value }));
  };

  const handleSubmit = (e) => { e.preventDefault(); onSave(formData); };

  return (
    <div className="form-container">
      <h2>{pipeline ? 'Edit Pipeline' : 'New Pipeline'}</h2>
      <form onSubmit={handleSubmit}>
        <div className="form-group">
          <label>Pipeline Name</label>
          <input type="text" name="name" value={formData.name} onChange={handleChange} required />
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Data Source</label>
            <select name="source_id" value={formData.source_id} onChange={handleChange} required>
              <option value="">Select source</option>
              {sources.map(s => <option key={s.id} value={s.id}>{s.name} ({s.engine})</option>)}
            </select>
          </div>
          <div className="form-group">
            <label>Connector</label>
            <select name="connector_id" value={formData.connector_id} onChange={handleChange} required>
              <option value="">Select connector</option>
              {connectors.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Broker</label>
            <select name="broker_id" value={formData.broker_id} onChange={handleChange} required>
              <option value="">Select broker</option>
              {brokers.map(b => <option key={b.id} value={b.id}>{b.name} ({b.broker_type})</option>)}
            </select>
          </div>
          <div className="form-group">
            <label>Valkey Target</label>
            <select name="target_id" value={formData.target_id} onChange={handleChange} required>
              <option value="">Select target</option>
              {targets.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </div>
        </div>
        <div className="form-group">
          <label>Status</label>
          <select name="status" value={formData.status} onChange={handleChange}>
            <option value="draft">Draft</option>
            <option value="configured">Configured</option>
            <option value="deploying">Deploying</option>
            <option value="running">Running</option>
            <option value="stopped">Stopped</option>
            <option value="error">Error</option>
          </select>
        </div>
        <div className="form-group">
          <label>Notes</label>
          <textarea name="notes" value={formData.notes || ''} onChange={handleChange} rows="2" />
        </div>
        <div className="form-actions">
          <button type="submit" className="btn btn-primary">Save</button>
          <button type="button" className="btn btn-secondary" onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </div>
  );
}

export default PipelineForm;
