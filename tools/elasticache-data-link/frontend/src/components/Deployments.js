import React, { useState, useEffect } from 'react';
import axios from 'axios';
import './Servers.css';
import API_URL from '../apiUrl';

// Records left by the superseded pipeline pages. They are listed for reference
// only: the endpoints that generated and deployed those packages were removed
// (they put database and cache credentials in plain text into the files and the
// EC2 user data they produced). Links are deployed from 2. Deploy -> Deployments.
function Deployments() {
  const [deployments, setDeployments] = useState([]);

  useEffect(() => {
    axios.get(`${API_URL}/api/deployments`)
      .then(res => setDeployments(res.data))
      .catch(e => console.error('Error:', e));
  }, []);

  return (
    <div className="servers-container">
      <div className="header">
        <h1>Pipeline Deployments</h1>
      </div>
      <p style={{ color: '#666', marginBottom: '20px' }}>
        Records from the superseded pipeline pages, kept for reference. These
        pages can no longer generate or deploy packages; deploy a link from
        2. Deploy → Deployments instead.
      </p>
      <div className="table-container">
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th><th>Pipeline</th><th>Type</th><th>Region</th>
              <th>VPC / Subnets</th><th>Status</th><th>Started</th>
            </tr>
          </thead>
          <tbody>
            {deployments.map(d => (
              <tr key={d.id}>
                <td>{d.id}</td>
                <td style={{ fontWeight: 600 }}>{d.pipeline_name || '—'}</td>
                <td>{d.deploy_type}</td>
                <td>{d.region}</td>
                <td style={{ fontFamily: 'monospace', fontSize: '11px', maxWidth: '220px', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {d.vpc_id ? <>{d.vpc_id}<br/>{d.subnet_ids}</> : '—'}
                </td>
                <td>
                  <span className={`status-badge status-${d.status}`}>{d.status}</span>
                </td>
                <td>{d.started || '—'}</td>
              </tr>
            ))}
            {deployments.length === 0 && (
              <tr>
                <td colSpan="7" style={{ textAlign: 'center', color: '#999', padding: '30px' }}>
                  No records.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default Deployments;
