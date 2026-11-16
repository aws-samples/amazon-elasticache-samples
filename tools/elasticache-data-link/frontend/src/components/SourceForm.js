import React, { useState, useEffect } from 'react';
import './Form.css';

function SourceForm({ source, onSave, onCancel }) {
  const [formData, setFormData] = useState({
    name: '', engine: 'postgres', host: '', port: 5432,
    database_name: '', schema_name: 'public', username: '', password: '',
    ssl_mode: 'require', status: 'new', notes: ''
  });
  const [showPassword, setShowPassword] = useState(false);

  useEffect(() => {
    if (source) setFormData(source);
  }, [source]);

  const handleChange = (e) => {
    const { name, value } = e.target;
    const updates = { [name]: value };
    if (name === 'engine') {
      const portMap = { postgres: 5432, mysql: 3306, oracle: 1521, sqlserver: 1433 };
      updates.port = portMap[value] || 5432;
    }
    setFormData(prev => ({ ...prev, ...updates }));
  };

  const handleSubmit = (e) => { e.preventDefault(); onSave(formData); };

  return (
    <div className="form-container">
      <h2>{source ? 'Edit Data Source' : 'New Data Source'}</h2>
      <form onSubmit={handleSubmit}>
        <div className="form-row">
          <div className="form-group">
            <label>Name</label>
            <input type="text" name="name" value={formData.name} onChange={handleChange} required />
          </div>
          <div className="form-group">
            <label>Engine</label>
            <select name="engine" value={formData.engine} onChange={handleChange}>
              <option value="postgres">PostgreSQL</option>
              <option value="mysql">MySQL / Aurora</option>
              <option value="oracle">Oracle</option>
              <option value="sqlserver">SQL Server</option>
            </select>
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Host</label>
            <input type="text" name="host" value={formData.host} onChange={handleChange} required
              placeholder="mydb.cluster-xxx.us-east-1.rds.amazonaws.com" />
          </div>
          <div className="form-group" style={{ maxWidth: '120px' }}>
            <label>Port</label>
            <input type="number" name="port" value={formData.port} onChange={handleChange} />
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Database Name</label>
            <input type="text" name="database_name" value={formData.database_name} onChange={handleChange} required />
          </div>
          <div className="form-group">
            <label>Schema</label>
            <input type="text" name="schema_name" value={formData.schema_name} onChange={handleChange} />
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Username</label>
            <input type="text" name="username" value={formData.username} onChange={handleChange} />
          </div>
          <div className="form-group">
            <label>Password</label>
            <div style={{ display: 'flex', gap: '10px' }}>
              <input type={showPassword ? 'text' : 'password'} name="password"
                value={formData.password} onChange={handleChange} style={{ flex: 1 }} />
              <button type="button" className="btn btn-secondary btn-small"
                onClick={() => setShowPassword(!showPassword)}>{showPassword ? 'Hide' : 'Show'}</button>
            </div>
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>SSL Mode</label>
            <select name="ssl_mode" value={formData.ssl_mode} onChange={handleChange}>
              <option value="disable">Disable</option>
              <option value="require">Require</option>
              <option value="verify-ca">Verify CA</option>
              <option value="verify-full">Verify Full</option>
            </select>
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

export default SourceForm;
