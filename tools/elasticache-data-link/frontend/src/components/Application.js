import React, { useState, useEffect } from 'react';
import axios from 'axios';
import './Application.css';
import API_URL from '../apiUrl';


function Application() {
  const [defaultRegion, setDefaultRegion] = useState('us-east-1');
  const [savedRegion, setSavedRegion] = useState('us-east-1');
  const [loading, setLoading] = useState(true);

  useEffect(() => { fetchSettings(); }, []);

  const fetchSettings = async () => {
    try {
      const res = await axios.get(`${API_URL}/api/settings/default_region`);
      const val = res.data.s_value || 'us-east-1';
      setDefaultRegion(val);
      setSavedRegion(val);
    } catch (e) { console.error('Error:', e); }
    finally { setLoading(false); }
  };

  const handleSaveRegion = async () => {
    try {
      await axios.post(`${API_URL}/api/settings`, {
        key: 'default_region', s_value: defaultRegion
      });
      setSavedRegion(defaultRegion);
    } catch (e) {
      alert('Failed to save setting');
    }
  };

  if (loading) {
    return (
      <div className="application-container">
        <h1>Application Settings</h1>
        <div className="loading">Loading...</div>
      </div>
    );
  }

  return (
    <div className="application-container">
      <h1>Application Settings</h1>
      <div className="settings-section">
        <div className="setting-item">
          <div className="setting-info">
            <h3>Default AWS Region</h3>
            <p>Default region for new brokers, targets, and deployments</p>
          </div>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <select value={defaultRegion} onChange={(e) => setDefaultRegion(e.target.value)}
              style={{ padding: '8px 12px', border: '1px solid #ccc', borderRadius: '4px', fontSize: '14px' }}>
              <option value="us-east-1">us-east-1</option>
              <option value="us-west-2">us-west-2</option>
              <option value="eu-west-1">eu-west-1</option>
              <option value="eu-central-1">eu-central-1</option>
              <option value="ap-southeast-1">ap-southeast-1</option>
              <option value="ap-northeast-1">ap-northeast-1</option>
            </select>
            <button className="btn btn-primary btn-small" onClick={handleSaveRegion}
              disabled={defaultRegion === savedRegion} style={{ padding: '8px 16px' }}>Save</button>
          </div>
        </div>
      </div>

      <div style={{ marginTop: '30px', background: 'white', borderRadius: '8px',
        boxShadow: '0 2px 4px rgba(0,0,0,0.1)', padding: '25px' }}>
        <h2 style={{ fontSize: '18px', color: '#2c3e50', marginBottom: '15px' }}>About ECDL</h2>
        <p style={{ color: '#555', lineHeight: '1.6', fontSize: '14px' }}>
          ElastiCache Datalink (ECDL) is an integration tool
          that connects relational databases (PostgreSQL, MySQL, Oracle, SQL Server) with
          ElastiCache for Valkey using Change Data Capture (CDC) with Debezium.
        </p>
        <div style={{ marginTop: '15px', display: 'flex', gap: '30px', fontSize: '13px', color: '#7f8c8d' }}>
          <div>CDC Engine: Debezium</div>
          <div>Brokers: Kafka (MSK) / Kinesis</div>
          <div>Target: ElastiCache for Valkey</div>
          <div>Write: Pipelined HSET + LWW</div>
        </div>
      </div>
    </div>
  );
}

export default Application;
