import React, { useState, useEffect } from 'react';
import './Form.css';

function BrokerForm({ broker, onSave, onCancel }) {
  const [formData, setFormData] = useState({
    name: '', broker_type: 'kafka', bootstrap_servers: '', auth_type: 'none',
    security_protocol: 'PLAINTEXT', tls_enabled: 0,
    stream_name: '', region: 'us-east-1', partitions: 3, retention_hours: 168,
    config_json: '', status: 'new', notes: ''
  });

  useEffect(() => {
    if (broker) setFormData(broker);
  }, [broker]);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({ ...prev, [name]: value }));
  };

  const handleSubmit = (e) => { e.preventDefault(); onSave(formData); };

  const isKafka = formData.broker_type === 'kafka' || formData.broker_type === 'msk';

  return (
    <div className="form-container">
      <h2>{broker ? 'Edit Broker' : 'New Broker'}</h2>
      <form onSubmit={handleSubmit}>
        <div className="form-row">
          <div className="form-group">
            <label>Name</label>
            <input type="text" name="name" value={formData.name} onChange={handleChange} required />
          </div>
          <div className="form-group">
            <label>Broker Type</label>
            <select name="broker_type" value={formData.broker_type} onChange={handleChange}>
              <option value="kafka">Apache Kafka (self-managed)</option>
              <option value="msk">Amazon MSK</option>
              <option value="kinesis">Amazon Kinesis</option>
            </select>
          </div>
        </div>
        {isKafka ? (
          <>
          <div className="form-group">
            <label>Bootstrap Servers</label>
            <input type="text" name="bootstrap_servers" value={formData.bootstrap_servers}
              onChange={handleChange} placeholder="b-1.msk-cluster.xxx.kafka.us-east-1.amazonaws.com:9092" />
          </div>
          <div className="form-row">
            <div className="form-group">
              <label>Authentication</label>
              <select name="auth_type" value={formData.auth_type} onChange={handleChange}>
                <option value="none">None (local Kafka)</option>
                <option value="iam">AWS IAM (MSK Serverless / MSK IAM)</option>
              </select>
            </div>
            <div className="form-group">
              <label>Security Protocol</label>
              <select name="security_protocol" value={formData.security_protocol || 'PLAINTEXT'} onChange={handleChange}>
                <option value="PLAINTEXT">PLAINTEXT (no encryption)</option>
                <option value="SSL">SSL (TLS encryption)</option>
                <option value="SASL_PLAINTEXT">SASL_PLAINTEXT (auth, no encryption)</option>
                <option value="SASL_SSL">SASL_SSL (auth + TLS encryption)</option>
              </select>
              <span style={{ fontSize: '12px', color: '#999' }}>
                {formData.broker_type === 'msk' ? 'MSK with IAM: use SASL_SSL. MSK without IAM: use SSL or PLAINTEXT.' : 'PLAINTEXT for dev. SSL for encrypted. SASL_SSL for auth + encryption.'}
              </span>
            </div>
          </div>
          <div className="form-row">
            <div className="form-group">
              <label>Region</label>
              <input type="text" name="region" value={formData.region} onChange={handleChange} />
            </div>
          </div>
          </>
        ) : (
          <div className="form-row">
            <div className="form-group">
              <label>Stream Name</label>
              <input type="text" name="stream_name" value={formData.stream_name}
                onChange={handleChange} placeholder="ecdl-cdc-stream" />
            </div>
            <div className="form-group">
              <label>Region</label>
              <input type="text" name="region" value={formData.region} onChange={handleChange} />
            </div>
          </div>
        )}
        <div className="form-row">
          <div className="form-group">
            <label>{isKafka ? 'Partitions' : 'Shards'}</label>
            <input type="number" name="partitions" value={formData.partitions} onChange={handleChange} min="1" />
          </div>
          <div className="form-group">
            <label>Retention (hours)</label>
            <input type="number" name="retention_hours" value={formData.retention_hours}
              onChange={handleChange} min="1" />
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

export default BrokerForm;
