import React, { useState } from 'react';
import './App.css';
import Sidebar from './components/Sidebar';
import TokenBar from './components/TokenBar';
import { installAuth } from './authClient';
import Links from './components/Links';
import LinkDeployments from './components/LinkDeployments';
import PipelineMonitor from './components/PipelineMonitor';
import Application from './components/Application';
import Maintenance from './components/Maintenance';

// Attach the API token to every request from one place. Called at module
// load so no component can fire a call before the interceptor exists.
installAuth();

function App() {
  const [activeView, setActiveView] = useState('links');
  // Which link the monitor should open on. Set when a page navigates here ABOUT
  // a specific link, so "I just deployed this, is it working" does not mean
  // switching page and then searching for the name in a dropdown.
  const [watchLink, setWatchLink] = useState('');

  const watchPipeline = (link) => {
    setWatchLink(link || '');
    setActiveView('pipeline-monitor');
  };

  return (
    <div className="App">
      <Sidebar activeView={activeView} setActiveView={setActiveView} />
      <div className="main-content">
        <TokenBar />
        {activeView === 'links' && <Links />}
        {activeView === 'link-deployments' && (
          <LinkDeployments onWatch={watchPipeline} />
        )}
        {activeView === 'pipeline-monitor' && (
          <PipelineMonitor initialLink={watchLink} />
        )}
        {activeView === 'application' && <Application />}
        {activeView === 'maintenance' && <Maintenance />}
      </div>
    </div>
  );
}

export default App;
