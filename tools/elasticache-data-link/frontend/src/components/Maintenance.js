import React, { useRef, useState } from 'react';
import axios from 'axios';
import API_URL from '../apiUrl';


function Maintenance() {
  const fileInputRef = useRef(null);
  const [restoring, setRestoring] = useState(false);

  const handleBackup = async () => {
    try {
      const response = await axios.get(`${API_URL}/api/maintenance/backup`, { responseType: 'blob' });
      const now = new Date();
      const ts = now.toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const filename = `ecdl-backup-${ts}.db`;
      const url = window.URL.createObjectURL(new Blob([response.data]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', filename);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      alert('Failed to download backup: ' + (error.response?.data?.error || error.message));
    }
  };

  const handleRestore = () => { fileInputRef.current.click(); };

  const handleFileSelected = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    if (!window.confirm(`Restore from "${file.name}"? This replaces the current database. Refresh after restore.`)) {
      fileInputRef.current.value = '';
      return;
    }
    setRestoring(true);
    try {
      const formData = new FormData();
      formData.append('file', file);
      const response = await axios.post(`${API_URL}/api/maintenance/restore`, formData, {
        headers: { 'Content-Type': 'multipart/form-data' }
      });
      if (response.data.success) alert('Database restored. Please refresh the page.');
      else alert('Restore failed: ' + (response.data.error || 'Unknown error'));
    } catch (error) {
      alert('Failed to restore: ' + (error.response?.data?.error || error.message));
    } finally {
      setRestoring(false);
      fileInputRef.current.value = '';
    }
  };

  return (
    <div style={{ maxWidth: '800px' }}>
      <h1 style={{ fontSize: '28px', color: '#2c3e50', marginBottom: '20px' }}>Maintenance</h1>
      <div style={{ background: 'white', borderRadius: '8px', boxShadow: '0 2px 4px rgba(0,0,0,0.1)', padding: '25px' }}>
        <h2 style={{ fontSize: '20px', color: '#2c3e50', marginBottom: '8px' }}>Backup</h2>
        <p style={{ color: '#666', fontSize: '14px', marginBottom: '20px' }}>
          Download or restore the ECDL configuration database.
        </p>
        <div style={{ display: 'flex', gap: '12px' }}>
          <button onClick={handleBackup} style={{
            display: 'flex', alignItems: 'center', gap: '8px',
            padding: '10px 20px', background: '#FF9900', color: '#232F3E',
            border: 'none', borderRadius: '5px', cursor: 'pointer', fontSize: '14px', fontWeight: 600
          }}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <polyline points="7 10 12 15 17 10"></polyline>
              <line x1="12" y1="15" x2="12" y2="3"></line>
            </svg>
            Backup
          </button>
          <button onClick={handleRestore} disabled={restoring} style={{
            display: 'flex', alignItems: 'center', gap: '8px',
            padding: '10px 20px', background: '#e67e22', color: 'white',
            border: 'none', borderRadius: '5px', cursor: 'pointer', fontSize: '14px',
            opacity: restoring ? 0.6 : 1
          }}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <polyline points="17 8 12 3 7 8"></polyline>
              <line x1="12" y1="3" x2="12" y2="15"></line>
            </svg>
            {restoring ? 'Restoring...' : 'Restore'}
          </button>
          <input type="file" ref={fileInputRef} onChange={handleFileSelected}
            accept=".db,.sqlite,.sqlite3" style={{ display: 'none' }} />
        </div>
      </div>
    </div>
  );
}

export default Maintenance;
