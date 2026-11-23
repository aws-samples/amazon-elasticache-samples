// ═══════════════════════════════════════════════════════════════════════════
// Sidebar — navigation, organised around the link model.
//
// WHAT CHANGED AND WHY. This used to lead with five pages that each stored a
// separate object: a Data Source, a Connector, a Broker, a Valkey Target, and a
// Pipeline that referenced them. That is a second way to describe the same
// deployment, and it drifted from the one the pipeline actually runs on -- the
// generated package even described a different architecture (Kafka/MSK and the
// Python sink) from the one every benchmark measures.
//
// In the link model none of those are things a customer defines:
//   * the source and the target are the CUSTOMER'S OWN resources, referenced by
//     ARN, so a page that creates them implies ECDL manages their database
//   * the broker is created BY ECDL and is an implementation detail
//   * the connector configuration is derived from the link, not authored
//   * the pipeline IS the link
// So one page defines a link, and everything else reports on it.
//
// The old pages were removed (review finding H10). They deployed nothing, and
// the data-source endpoints behind them connected to any host a user typed
// in, with a secret the control plane's role read.
// ═══════════════════════════════════════════════════════════════════════════
import React, { useState } from 'react';
import './Sidebar.css';

function Sidebar({ activeView, setActiveView }) {
  const [adminExpanded, setAdminExpanded] = useState(false);

  const item = (view, label) => (
    <button
      className={`sidebar-item ${activeView === view ? 'active' : ''}`}
      onClick={() => setActiveView(view)}
    >
      {label}
    </button>
  );

  return (
    <div className="sidebar">
      <h2 className="sidebar-title">
        ElastiCache Datalink
        <span className="sidebar-subtitle">RDBMS → Valkey CDC Pipeline</span>
      </h2>

      <div className="sidebar-group">
        <h3 className="group-title">1. Define</h3>
        {item('links', 'Build Pipeline')}
      </div>

      <div className="sidebar-group">
        <h3 className="group-title">2. Deploy</h3>
        {item('link-deployments', 'Deployments')}
      </div>

      <div className="sidebar-group">
        <h3 className="group-title">3. Observe</h3>
        {item('pipeline-monitor', 'Pipeline Monitor')}
      </div>

      {/* "4. Test" (the Randomizer, a data generator that writes to the source
          database) is hidden until it is ready. App.wiring.test.js keeps it out. */}

      <div className="sidebar-group">
        <div
          className="group-title-collapsible"
          onClick={() => setAdminExpanded(!adminExpanded)}
        >
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"></path>
            <circle cx="12" cy="12" r="3"></circle>
          </svg>
          <span className={`expand-arrow ${adminExpanded ? 'expanded' : ''}`}>▶</span>
        </div>
        {adminExpanded && (
          <>
            {item('application', 'Application')}
            {item('maintenance', 'Maintenance')}
          </>
        )}
      </div>
    </div>
  );
}

export default Sidebar;
