import React, { useState, useEffect } from 'react';
import axios from 'axios';
import DeployWizard from './DeployWizard';
import './Servers.css';
import API_URL from '../apiUrl';


function Deployments() {
  const [deployments, setDeployments] = useState([]);
  const [pipelines, setPipelines] = useState([]);
  const [showDeploy, setShowDeploy] = useState(false);
  const [deployForm, setDeployForm] = useState({
    pipeline_id: '', deploy_type: 'docker', region: 'us-east-1',
    vpc_id: '', subnet_ids: ''
  });
  const [deploying, setDeploying] = useState(false);
  const [showWizard, setShowWizard] = useState(false);
  const [previewData, setPreviewData] = useState(null);
  const [previewFile, setPreviewFile] = useState(null);
  const [generating, setGenerating] = useState(false);

  useEffect(() => { fetchAll(); }, []);

  const fetchAll = async () => {
    try {
      const [d, p] = await Promise.all([
        axios.get(`${API_URL}/api/deployments`),
        axios.get(`${API_URL}/api/pipelines`),
      ]);
      setDeployments(d.data);
      setPipelines(p.data);
    } catch (e) { console.error('Error:', e); }
  };

  const handleDeploy = async (e) => {
    e.preventDefault();
    setDeploying(true);
    try {
      await axios.post(`${API_URL}/api/deployments`, deployForm);
      await fetchAll();
      setShowDeploy(false);
      setDeployForm({ pipeline_id: '', deploy_type: 'docker', region: 'us-east-1', vpc_id: '', subnet_ids: '' });
    } catch (e) {
      alert(`Error: ${e.response?.data?.error || e.message}`);
    } finally { setDeploying(false); }
  };

  const handleStatusChange = async (id, newStatus) => {
    try {
      await axios.put(`${API_URL}/api/deployments/${id}/status`, { status: newStatus });
      await fetchAll();
    } catch (e) { alert(`Error: ${e.response?.data?.error || e.message}`); }
  };

  const handleGenerateCompose = async () => {
    if (!deployForm.pipeline_id) { alert('Select a pipeline first'); return; }
    setGenerating(true);
    try {
      const res = await axios.get(
        `${API_URL}/api/pipelines/${deployForm.pipeline_id}/generate-docker-compose`
      );
      setPreviewData(res.data);
      setPreviewFile('docker-compose.yml');
    } catch (e) {
      alert(`Error: ${e.response?.data?.error || e.message}`);
    } finally { setGenerating(false); }
  };

  const handleDownloadZip = async () => {
    if (!deployForm.pipeline_id) return;
    try {
      const res = await fetch(
        `${API_URL}/api/pipelines/${deployForm.pipeline_id}/download-deployment`
      );
      const blob = await res.blob();
      const disposition = res.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename=(.+)/);
      const filename = match ? match[1] : 'deployment.zip';
      const url = window.URL.createObjectURL(new Blob([blob], { type: 'application/zip' }));
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (e) {
      alert(`Download error: ${e.message}`);
    }
  };

  const ecsQueryParams = () => {
    const params = new URLSearchParams();
    if (deployForm.vpc_id) params.set('vpc_id', deployForm.vpc_id);
    if (deployForm.subnet_ids) params.set('subnet_ids', deployForm.subnet_ids);
    const qs = params.toString();
    return qs ? `?${qs}` : '';
  };

  const handleGenerateEcs = async () => {
    if (!deployForm.pipeline_id) { alert('Select a pipeline first'); return; }
    setGenerating(true);
    try {
      const res = await axios.get(
        `${API_URL}/api/pipelines/${deployForm.pipeline_id}/generate-ecs-fargate${ecsQueryParams()}`
      );
      setPreviewData(res.data);
      setPreviewFile('cloudformation.json');
    } catch (e) {
      alert(`Error: ${e.response?.data?.error || e.message}`);
    } finally { setGenerating(false); }
  };

  const handleDownloadEcsZip = async () => {
    if (!deployForm.pipeline_id) return;
    try {
      const res = await fetch(
        `${API_URL}/api/pipelines/${deployForm.pipeline_id}/download-ecs-deployment${ecsQueryParams()}`
      );
      const blob = await res.blob();
      const disposition = res.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename=(.+)/);
      const filename = match ? match[1] : 'ecs-deployment.zip';
      const url = window.URL.createObjectURL(new Blob([blob], { type: 'application/zip' }));
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (e) {
      alert(`Download error: ${e.message}`);
    }
  };

  const fileList = previewData?.files ? Object.keys(previewData.files) : [];

  return (
    <div className="servers-container">
      <div className="header">
        <h1>Deployments</h1>
        <button className="btn btn-success" onClick={() => setShowWizard(true)}
          style={{ background: '#27ae60', borderColor: '#27ae60' }}>
          🚀 Deploy Pipeline
        </button>
      </div>

      {/* One-Click Deploy Wizard */}
      {showWizard && (
        <DeployWizard
          pipelines={pipelines}
          onDeploy={async (config) => {
            try {
              setDeploying(true);
              const res = await axios.post(`${API_URL}/api/pipelines/${config.pipeline_id}/deploy-cloud`, config);
              alert(`Deployment initiated!\n\nID: ${res.data.deployment_id}\nStatus: ${res.data.status}\n\n${res.data.message || 'Check deployment status below.'}`);
              setShowWizard(false);
              await fetchAll();
            } catch (e) {
              alert(`Deploy error: ${e.response?.data?.error || e.message}`);
            } finally { setDeploying(false); }
          }}
          onPreview={async (config) => {
            try {
              const endpoint = config.deploy_target === 'ecs'
                ? `${API_URL}/api/pipelines/${config.pipeline_id}/generate-ecs-fargate`
                : `${API_URL}/api/pipelines/${config.pipeline_id}/generate-docker-compose`;
              const res = await axios.get(endpoint);
              setPreviewData(res.data);
              setPreviewFile(Object.keys(res.data.files || {})[0] || null);
            } catch (e) {
              alert(`Preview error: ${e.response?.data?.error || e.message}`);
            }
          }}
          onDownload={async (config) => {
            try {
              const endpoint = config.deploy_target === 'ecs'
                ? `${API_URL}/api/pipelines/${config.pipeline_id}/download-ecs-deployment`
                : `${API_URL}/api/pipelines/${config.pipeline_id}/download-deployment`;
              const res = await fetch(endpoint);
              const blob = await res.blob();
              const url = window.URL.createObjectURL(new Blob([blob], { type: 'application/zip' }));
              const a = document.createElement('a');
              a.href = url;
              a.download = `ecdl-${config.deploy_target}-deployment.zip`;
              document.body.appendChild(a);
              a.click();
              a.remove();
              window.URL.revokeObjectURL(url);
            } catch (e) {
              alert(`Download error: ${e.message}`);
            }
          }}
          onCancel={() => { setShowWizard(false); setPreviewData(null); }}
        />
      )}

      {showDeploy && (
        <div style={{ background: 'white', borderRadius: '8px', padding: '25px',
          boxShadow: '0 2px 4px rgba(0,0,0,0.1)', marginBottom: '20px' }}>
          <h2 style={{ marginBottom: '20px', color: '#2c3e50' }}>New Deployment</h2>
          <form onSubmit={handleDeploy}>
            <div style={{ display: 'flex', gap: '20px', marginBottom: '20px', flexWrap: 'wrap' }}>
              <div style={{ flex: 1, minWidth: '200px' }}>
                <label style={{ display: 'block', marginBottom: '8px', fontWeight: 600 }}>
                  Pipeline
                </label>
                <select value={deployForm.pipeline_id} required
                  onChange={(e) => setDeployForm(prev => ({ ...prev, pipeline_id: e.target.value }))}
                  style={{ width: '100%', padding: '10px', border: '1px solid #ddd',
                    borderRadius: '5px' }}>
                  <option value="">Select pipeline</option>
                  {pipelines.map(p => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label style={{ display: 'block', marginBottom: '8px', fontWeight: 600 }}>
                  Deploy Type
                </label>
                <select value={deployForm.deploy_type}
                  onChange={(e) => setDeployForm(prev => ({ ...prev, deploy_type: e.target.value }))}
                  style={{ padding: '10px', border: '1px solid #ddd', borderRadius: '5px' }}>
                  <option value="docker">Docker Compose</option>
                  <option value="ecs">ECS Fargate</option>
                  <option value="ec2">EC2</option>
                </select>
              </div>
              <div>
                <label style={{ display: 'block', marginBottom: '8px', fontWeight: 600 }}>
                  Region
                </label>
                <select value={deployForm.region}
                  onChange={(e) => setDeployForm(prev => ({ ...prev, region: e.target.value }))}
                  style={{ padding: '10px', border: '1px solid #ddd', borderRadius: '5px' }}>
                  <option value="us-east-1">us-east-1</option>
                  <option value="us-west-2">us-west-2</option>
                  <option value="eu-west-1">eu-west-1</option>
                  <option value="ap-southeast-1">ap-southeast-1</option>
                </select>
              </div>
            </div>
            {deployForm.deploy_type === 'ecs' && (
              <div style={{ display: 'flex', gap: '20px', marginBottom: '20px', flexWrap: 'wrap' }}>
                <div style={{ flex: 1, minWidth: '200px' }}>
                  <label style={{ display: 'block', marginBottom: '8px', fontWeight: 600 }}>
                    VPC ID
                  </label>
                  <input type="text" value={deployForm.vpc_id}
                    onChange={(e) => setDeployForm(prev => ({ ...prev, vpc_id: e.target.value }))}
                    placeholder="vpc-xxxxxxxxx"
                    style={{ width: '100%', padding: '10px', border: '1px solid #ddd',
                      borderRadius: '5px', fontFamily: 'monospace', fontSize: '13px' }} />
                </div>
                <div style={{ flex: 2, minWidth: '300px' }}>
                  <label style={{ display: 'block', marginBottom: '8px', fontWeight: 600 }}>
                    Subnet IDs <span style={{ fontWeight: 400, color: '#999', fontSize: '12px' }}>(comma-separated, private subnets with NAT route)</span>
                  </label>
                  <input type="text" value={deployForm.subnet_ids}
                    onChange={(e) => setDeployForm(prev => ({ ...prev, subnet_ids: e.target.value }))}
                    placeholder="subnet-aaa,subnet-bbb"
                    style={{ width: '100%', padding: '10px', border: '1px solid #ddd',
                      borderRadius: '5px', fontFamily: 'monospace', fontSize: '13px' }} />
                </div>
              </div>
            )}
            <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
              <button type="submit" className="btn btn-primary" disabled={deploying}>
                {deploying ? 'Deploying...' : 'Deploy'}
              </button>
              {deployForm.deploy_type === 'docker' && (
                <>
                  <button type="button" className="btn btn-secondary"
                    onClick={handleGenerateCompose}
                    disabled={generating || !deployForm.pipeline_id}>
                    {generating ? 'Generating...' : 'Preview Docker Compose'}
                  </button>
                  <button type="button" className="btn btn-success"
                    onClick={handleDownloadZip}
                    disabled={!deployForm.pipeline_id}>
                    Download Deployment Package (.zip)
                  </button>
                  <button type="button" className="btn btn-primary"
                    onClick={async () => {
                      if (!deployForm.pipeline_id) { alert('Select a pipeline first'); return; }
                      try {
                        const res = await axios.post(`${API_URL}/api/pipelines/${deployForm.pipeline_id}/deploy-local`);
                        alert(`Deployment files written to:\n${res.data.deploy_dir}\n\nTo start the pipeline, run from EC2:\ncd ${res.data.deploy_dir} && ./start.sh`);
                        await fetchAll();
                      } catch (e) {
                        alert(`Error: ${e.response?.data?.error || e.message}`);
                      }
                    }}
                    disabled={!deployForm.pipeline_id}>
                    🚀 Deploy on this Server
                  </button>
                </>
              )}
              {deployForm.deploy_type === 'ecs' && (
                <>
                  <button type="button" className="btn btn-secondary"
                    onClick={handleGenerateEcs}
                    disabled={generating || !deployForm.pipeline_id}>
                    {generating ? 'Generating...' : 'Preview ECS Fargate'}
                  </button>
                  <button type="button" className="btn btn-success"
                    onClick={handleDownloadEcsZip}
                    disabled={!deployForm.pipeline_id}>
                    Download ECS Package (.zip)
                  </button>
                </>
              )}
              <button type="button" className="btn btn-secondary"
                onClick={() => { setShowDeploy(false); setPreviewData(null); }}>
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Docker Compose Preview Panel */}
      {previewData && previewData.files && (
        <div style={{ background: 'white', borderRadius: '8px',
          boxShadow: '0 2px 4px rgba(0,0,0,0.1)', marginBottom: '20px', overflow: 'hidden' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            padding: '15px 20px', borderBottom: '1px solid #e0e0e0', background: '#f8f9fa' }}>
            <h3 style={{ margin: 0, color: '#2c3e50' }}>
              Deployment Package: {previewData.pipeline_name}
            </h3>
            <button className="btn btn-small btn-secondary"
              onClick={() => setPreviewData(null)}>Close</button>
          </div>
          <div style={{ display: 'flex', minHeight: '400px' }}>
            {/* File tree */}
            <div style={{ width: '220px', borderRight: '1px solid #e0e0e0',
              background: '#fafafa', padding: '10px 0' }}>
              {fileList.map(f => (
                <button key={f}
                  onClick={() => setPreviewFile(f)}
                  style={{
                    display: 'block', width: '100%', textAlign: 'left',
                    padding: '8px 15px', border: 'none', cursor: 'pointer',
                    fontSize: '13px', fontFamily: 'monospace',
                    background: previewFile === f ? '#e3f2fd' : 'transparent',
                    color: previewFile === f ? '#1565c0' : '#555',
                    fontWeight: previewFile === f ? 600 : 400,
                    borderLeft: previewFile === f ? '3px solid #1565c0' : '3px solid transparent'
                  }}>
                  {f.includes('/') ? '  ' : ''}{f.split('/').pop()}
                  {f.includes('/') && (
                    <span style={{ color: '#999', fontSize: '11px', display: 'block' }}>
                      {f.substring(0, f.lastIndexOf('/'))}
                    </span>
                  )}
                </button>
              ))}
            </div>
            {/* File content */}
            <div style={{ flex: 1, overflow: 'auto' }}>
              <div style={{ padding: '10px 15px', background: '#f0f0f0',
                borderBottom: '1px solid #e0e0e0', fontSize: '13px',
                fontFamily: 'monospace', color: '#666' }}>
                {previewFile}
              </div>
              <pre style={{ margin: 0, padding: '15px', fontSize: '12px',
                lineHeight: '1.5', overflow: 'auto', maxHeight: '500px',
                background: '#1e1e1e', color: '#d4d4d4' }}>
                {previewFile && previewData.files[previewFile]}
              </pre>
            </div>
          </div>
        </div>
      )}

      {/* Deployments History Table */}
      <div className="table-container">
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th><th>Pipeline</th><th>Type</th><th>Region</th>
              <th>VPC / Subnets</th>
              <th>Status</th><th>Started</th><th>Actions</th>
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
                <td>
                  <div className="action-buttons">
                    <button className="btn btn-small btn-secondary"
                      onClick={async () => {
                        try {
                          const ecsParams = d.vpc_id || d.subnet_ids
                            ? `?vpc_id=${encodeURIComponent(d.vpc_id || '')}&subnet_ids=${encodeURIComponent(d.subnet_ids || '')}`
                            : '';
                          const endpoint = d.deploy_type === 'ecs'
                            ? `${API_URL}/api/pipelines/${d.pipeline_id}/download-ecs-deployment${ecsParams}`
                            : `${API_URL}/api/pipelines/${d.pipeline_id}/download-deployment`;
                          const res = await fetch(endpoint);
                          const blob = await res.blob();
                          const url = window.URL.createObjectURL(
                            new Blob([blob], { type: 'application/zip' })
                          );
                          const a = document.createElement('a');
                          a.href = url;
                          a.download = `ecdl-${(d.pipeline_name || 'deployment').toLowerCase().replace(/\s+/g, '-')}.zip`;
                          document.body.appendChild(a);
                          a.click();
                          a.remove();
                          window.URL.revokeObjectURL(url);
                        } catch (e) { alert(`Download error: ${e.message}`); }
                      }}
                      title="Download deployment package">⬇ Download</button>
                    {d.status === 'pending' && (
                      <button className="btn btn-small btn-success"
                        onClick={() => handleStatusChange(d.id, 'running')}>Start</button>
                    )}
                    {d.status === 'running' && (
                      <button className="btn btn-small btn-danger"
                        onClick={() => handleStatusChange(d.id, 'stopped')}>Stop</button>
                    )}
                    {(d.status === 'stopped' || d.status === 'error') && (
                      <button className="btn btn-small btn-success"
                        onClick={() => handleStatusChange(d.id, 'running')}>Restart</button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {deployments.length === 0 && (
              <tr>
                <td colSpan="8" style={{ textAlign: 'center', color: '#999', padding: '30px' }}>
                  No deployments yet. Click "Deploy Pipeline" to create one.
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
