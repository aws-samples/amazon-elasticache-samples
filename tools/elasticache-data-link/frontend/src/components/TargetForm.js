import React, { useState, useEffect } from 'react';
import './Form.css';


function TargetForm({ target, onSave, onCancel }) {
  const [formData, setFormData] = useState({
    name: '', cluster_endpoint: '', port: 6379, auth_token: '',
    ssl_enabled: 1, cluster_mode: 1, key_prefix: '', key_pattern: '{table}:{pk}',
    write_mode: 'hset', pipeline_batch_size: 100, lww_enabled: 1,
    dev_test: 0, sink_replicas: 1,
    config_json: '', status: 'new', notes: ''
  });
  const [showToken, setShowToken] = useState(false);

  useEffect(() => {
    if (target) setFormData(target);
  }, [target]);

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: type === 'checkbox' ? (checked ? 1 : 0) : value
    }));
  };

  const handleSubmit = (e) => { e.preventDefault(); onSave(formData); };

  return (
    <div className="form-container">
      <h2>{target ? 'Edit Valkey Target' : 'New Valkey Target'}</h2>
      <form onSubmit={handleSubmit}>
        <div className="form-row">
          <div className="form-group">
            <label>Name</label>
            <input type="text" name="name" value={formData.name} onChange={handleChange} required />
          </div>
          <div className="form-group">
            <label>Status</label>
            <select name="status" value={formData.status} onChange={handleChange}>
              <option value="new">New</option>
              <option value="active">Active</option>
              <option value="inactive">Inactive</option>
            </select>
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Cluster Endpoint</label>
            <input type="text" name="cluster_endpoint" value={formData.cluster_endpoint}
              onChange={handleChange} required placeholder="my-cluster.xxx.cache.amazonaws.com" />
          </div>
          <div className="form-group" style={{ maxWidth: '120px' }}>
            <label>Port</label>
            <input type="number" name="port" value={formData.port} onChange={handleChange} />
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>ACL Username <span style={{ fontSize: '11px', color: '#999', fontWeight: 400 }}>(optional, for ACL-based auth)</span></label>
            <input type="text" name="auth_username" value={formData.auth_username || ''}
              onChange={handleChange} placeholder="default (leave empty if not using ACLs)" />
          </div>
          <div className="form-group">
            <label>Auth Token / Password</label>
            <div style={{ display: 'flex', gap: '10px' }}>
              <input type={showToken ? 'text' : 'password'} name="auth_token"
                value={formData.auth_token || ''} onChange={handleChange} style={{ flex: 1 }}
                placeholder="Leave empty if no auth" />
              <button type="button" className="btn btn-secondary btn-small"
                onClick={() => setShowToken(!showToken)}>{showToken ? 'Hide' : 'Show'}</button>
            </div>
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Key Prefix</label>
            <input type="text" name="key_prefix" value={formData.key_prefix}
              onChange={handleChange} placeholder="app:" />
          </div>
          <div className="form-group">
            <label>Key Pattern</label>
            <input type="text" name="key_pattern" value={formData.key_pattern}
              onChange={handleChange} placeholder="{table}:{pk}" />
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Write Mode</label>
            <select name="write_mode" value={formData.write_mode} onChange={handleChange}>
              <option value="hset">HSET (Hash per row)</option>
              <option value="json">JSON (String per row)</option>
              <option value="json_object">JSON (JSON Object)</option>
            </select>
          </div>
          <div className="form-group">
            <label>Pipeline Batch Size</label>
            <input type="number" name="pipeline_batch_size" value={formData.pipeline_batch_size}
              onChange={handleChange} min="1" max="1000" />
          </div>
          <div className="form-group">
            <label>Sink Worker Replicas</label>
            <input type="number" name="sink_replicas" value={formData.sink_replicas || 1}
              onChange={handleChange} min="1" max="10" />
            <span style={{ fontSize: '12px', color: '#999' }}>Match with Kafka partitions for parallel processing</span>
          </div>
        </div>
        <div style={{ marginBottom: '20px' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px', fontWeight: 600, color: '#34495e' }}>
            <input type="checkbox" name="ssl_enabled" checked={!!formData.ssl_enabled}
              onChange={handleChange} /> SSL/TLS Enabled
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px', fontWeight: 600, color: '#34495e' }}>
            <input type="checkbox" name="cluster_mode" checked={!!formData.cluster_mode}
              onChange={handleChange} /> Cluster Mode
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px', fontWeight: 600, color: '#34495e' }}>
            <input type="checkbox" name="lww_enabled" checked={!!formData.lww_enabled}
              onChange={handleChange} /> Last-Write-Wins (LWW)
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px', fontWeight: 600, color: '#34495e' }}>
            <input type="checkbox" name="dev_test" checked={!!formData.dev_test}
              onChange={handleChange} /> Dev/Test Mode (fresh consumer group per deploy)
          </label>
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

export default TargetForm;
