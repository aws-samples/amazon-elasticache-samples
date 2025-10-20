import { BrowserRouter, Routes, Route, Navigate } from 'react-router';
import { Layout } from './components/layout/Layout';
import { Home } from './pages/Home';
import { Connections } from './pages/Connections';
import { CLI } from './pages/CLI';
import { CommandLine } from './pages/CommandLine';
import { CommandLog } from './pages/CommandLog';
import { Monitoring } from './pages/Monitoring';
import { Chat } from './pages/Chat';
import { HelpDocs } from './pages/HelpDocs';
import { ConnectionProvider } from './contexts/ConnectionContext';
import { SettingsProvider } from './contexts/SettingsContext';
import { AuthProvider } from './contexts/AuthContext';
import './App.css';

function App() {
  return (
    <SettingsProvider>
      <AuthProvider>
        <ConnectionProvider>
          <BrowserRouter>
            <Routes>
              <Route path="/" element={<Layout />}>
              <Route index element={<Home />} />
              <Route path="connections" element={<Connections />} />
              <Route path="cli" element={<CLI />} />
              <Route path="cmdline" element={<CommandLine />} />
              <Route path="commandlog" element={<CommandLog />} />
              {/* Backward compatibility redirect */}
              <Route path="slowlog" element={<Navigate to="/commandlog" replace />} />
              <Route path="monitoring" element={<Monitoring />} />
              <Route path="chat" element={<Chat />} />
              <Route path="help" element={<HelpDocs />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </ConnectionProvider>
      </AuthProvider>
    </SettingsProvider>
  );
}

export default App;
