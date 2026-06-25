/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useState, useEffect } from 'react';
import type { ReactNode } from 'react';
import type { ConnectionConfig, ConnectionStatus } from '@/types';
import { useSettings } from '@/contexts/SettingsContext';

interface ConnectionContextType {
  activeConnection: ConnectionConfig | null;
  connectionStatus: ConnectionStatus | null;
  connections: ConnectionConfig[];
  setActiveConnection: (connection: ConnectionConfig) => Promise<boolean>;
  addConnection: (connection: ConnectionConfig) => void;
  updateConnection: (connection: ConnectionConfig) => void;
  deleteConnection: (id: string) => void;
  testConnection: (connection: ConnectionConfig) => Promise<boolean>;
  connectingConnectionId: string | null;
}

const ConnectionContext = createContext<ConnectionContextType | undefined>(undefined);

export function useConnection() {
  const context = useContext(ConnectionContext);
  console.log('=========> ConnectionContext', context);
  if (!context) {
    throw new Error('useConnection must be used within a ConnectionProvider');
  }
  return context;
}

interface ConnectionProviderProps {
  children: ReactNode;
}

export function ConnectionProvider({ children }: ConnectionProviderProps) {
  const { settings } = useSettings();
  const [activeConnection, setActiveConnectionState] = useState<ConnectionConfig | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus | null>(null);
  const [connections, setConnections] = useState<ConnectionConfig[]>([]);
  const [connectingConnectionId, setConnectingConnectionId] = useState<string | null>(null);

  // Default connection (from config)
  // const defaultConfig = getApiConfigWithEnvOverrides();

  // Prefer same-origin for API by default (works with Docker/nginx proxy)
  // const browserOrigin = (typeof window !== 'undefined' && window.location) ? window.location : null;
  // const inferredApiEndpoint = browserOrigin ? browserOrigin.hostname : defaultConfig.apiEndpoint || 'localhost';
  // const inferredApiEndpoint = 'localhost'; // for Running in Docker, no config is needed
  // const inferredApiPort = browserOrigin ? (browserOrigin.port ? parseInt(browserOrigin.port) : (browserOrigin.protocol === 'https:' ? 443 : 80)) : defaultConfig.apiPort;
  // const inferredApiPort = '8000'; // for Running in Docker, no config is needed
  // const inferredApiSsl = browserOrigin ? (browserOrigin.protocol === 'https:') : defaultConfig.apiSsl;
  // const inferredApiSsl = 'http:'; // for Running in Docker, no config is needed

  /*
  const defaultConnection: ConnectionConfig = {
    id: 'default-local',
    name: 'Default Connection from Environment Variables',
    // API Service endpoint (your backend API)
    apiEndpoint: inferredApiEndpoint,
    apiPort: inferredApiPort,
    apiSsl: inferredApiSsl,
    // Redis cluster endpoint
    redisEndpoint: defaultConfig.redisEndpoint,
    redisPort: defaultConfig.redisPort,
    redisTls: defaultConfig.redisTls,
    redisCluster: defaultConfig.redisCluster,
    type: defaultConfig.type,
    region: defaultConfig.region,
    createdAt: new Date('2024-01-01'),
    lastConnected: new Date(),
    // Legacy compatibility
    endpoint: inferredApiEndpoint,
    port: inferredApiPort,
    ssl: inferredApiSsl,
  };

   */

  // Load connections from localStorage on mount
  useEffect(() => {
    const savedConnections = localStorage.getItem('valkey-connections');
    const savedActiveConnection = localStorage.getItem('valkey-active-connection');
    
    // let loadedConnections = [defaultConnection];
    let loadedConnections : ConnectionConfig[] = [];

    if (savedConnections) {
      try {
        const parsed = JSON.parse(savedConnections);
        console.log('🔍 Loading connections from localStorage:', parsed.length, 'saved connections');
        // Convert date strings back to Date objects
        const connectionsWithDates = parsed.map((conn: ConnectionConfig) => ({
          ...conn,
          createdAt: new Date(conn.createdAt),
          lastConnected: conn.lastConnected ? new Date(conn.lastConnected) : undefined,
        }));
        // loadedConnections = [...loadedConnections, ...connectionsWithDates];
        loadedConnections = [...connectionsWithDates];
        console.log('✅ Total connections loaded:', loadedConnections.length, '(1 default + ' + parsed.length + ' saved)');
      } catch (error) {
        console.error('Failed to load connections from localStorage:', error);
      }
    } else {
      console.log('📝 No saved connections found in localStorage, using default connection only');
    }
    
    setConnections(loadedConnections);
    console.log('🔗 Final connections state:', loadedConnections.map(c => ({ id: c.id, name: c.name })));
    
    // Set active connection
    if (savedActiveConnection) {
      try {
        const activeId = JSON.parse(savedActiveConnection);
        // const active = loadedConnections.find(conn => conn.id === activeId) || defaultConnection;
        const active = loadedConnections.find(conn => conn.id === activeId);
        if (active) {
            setActiveConnectionState(active);
        // Test the connection
            testConnectionInternal(active);
        }
      } catch (error) {
        console.error('Failed to load active connection:', error);
        // setActiveConnectionState(defaultConnection);
        // testConnectionInternal(defaultConnection);
      }
    } else {
      console.error('No default connection:');
      // setActiveConnectionState(defaultConnection);
      // testConnectionInternal(defaultConnection);
    }
  }, []);

  // Save connections to localStorage
  useEffect(() => {
    if (connections.length > 0) {
      const connectionsToSave = connections.filter(conn => conn.id !== 'default' && conn.id !== 'default-local'); // changed from 'defaule-ec2'
      console.log('💾 Saving connections to localStorage:', connectionsToSave.length, 'connections');
      localStorage.setItem('valkey-connections', JSON.stringify(connectionsToSave));
    }
  }, [connections]);

  // When settings change, propagate influx settings to all connections
  useEffect(() => {
    setConnections(prev => prev.map(c => ({
      ...c,
      influxToken: settings.influxToken,
      influxBucket: settings.influxBucket,
      influxOrg: settings.influxOrg,
    })));
  }, [settings.influxToken, settings.influxBucket, settings.influxOrg]);

  // Save active connection to localStorage
  useEffect(() => {
    if (activeConnection) {
      console.log('💾 Saving active connection to localStorage:', activeConnection.name, activeConnection.id);
      localStorage.setItem('valkey-active-connection', JSON.stringify(activeConnection.id));
    }
  }, [activeConnection]);

  const testConnectionInternal = async (connection: ConnectionConfig): Promise<boolean> => {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 10000); // 10 second timeout
    
    try {
      // Use new API endpoint fields, with fallback to legacy fields
      const apiEndpoint = connection.apiEndpoint || connection.endpoint || 'localhost';
      const apiPort = connection.apiPort || connection.port || 8000;
      const apiSsl = connection.apiSsl ?? connection.ssl ?? false;
      
      const baseUrl = `http${apiSsl ? 's' : ''}://${apiEndpoint}:${apiPort}`;
      console.log('DEBUG ----> Connection given:', connection);
      console.log(`🔍 Testing connection to API service: ${baseUrl}/health`);
      console.log(`🔍 Redis cluster will be: ${connection.redisEndpoint}:${connection.redisPort} (TLS: ${connection.redisTls})`);

      //const redisTls = connection.redisTls;
      const name = connection.name;
      const redisEndpoint = connection.redisEndpoint;
      const redisPort = connection.redisPort;
      const redisTls = connection.redisTls;
      const redisCluster = connection.redisCluster;

      // InfluxDB
      let influxEndpointUrl = connection.influxEndpointUrl;
      let influxPort = connection.influxPort;
      let influxToken = connection.influxToken;
      let influxBucket = connection.influxBucket;
      let influxOrg = connection.influxOrg;

      if (influxEndpointUrl==null) {
            influxEndpointUrl ="not sure why undefined";
      }
      if (influxPort==null) {
          influxPort = 1;
          console.log("   InfluxPort undefined")
      }
      if (influxToken==null) {
            influxToken = 'no token';
            console.log("   InfluxToken undefined")
      }
      if (influxBucket==null) {
            influxBucket = 'no Bucket';
            console.log("   InfluxBucket undefined")
      }
      if (influxOrg==null) {
            influxOrg = 'no org';
            console.log("   InfluxOrg undefined")
      }

      console.log(`_______----------_________ URL: '${influxEndpointUrl}'`)
      console.log(`_______----------_________ Port: '${influxPort}'`)
      console.log(`_______----------_________ Port: '${influxToken}'`)
      console.log(`_______----------_________ Port: '${influxBucket}'`)
      console.log(`_______----------_________ Port: '${influxOrg}'`)


        /*      const debug_body = JSON.stringify({
                  name,
                  redisEndpoint,
                  redisPort,
                  redisTls,
                  redisCluster,
                  timeout: 30000 // 30 second timeout
              });

              console.log(`DEBUG ----> POST goes to: ${baseUrl}/api/cluster/connect`)
              console.log(`          > ${debug_body}`)


                console.log('testing GET INSTEAD')
                const debug_response = await fetch('http://localhost:8000/api/cluster/connect', {
                    // signal: controller.signal,
                    headers: {
                        'Accept': 'application/json',
                        'Content-Type': 'application/json',
                    }
                });


                console.log(debug_response);
                console.log('END testing GET INSTEAD')
        */

        const debug_body = JSON.stringify({
            name,
            redisEndpoint,
            redisPort,
            redisTls,
            redisCluster,
            // from settings
            influxEndpointUrl,
            influxPort,
            influxToken,
            influxBucket,
            influxOrg,
            timeout: 30000 // 30 second timeout
        });
        console.log(`    BODY: ${debug_body}`);

        const response_con = await fetch(`${baseUrl}/api/cluster/connect`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                name,
                redisEndpoint,
                redisPort,
                redisTls,
                redisCluster,
                // from settings
                influxEndpointUrl,
                influxPort,
                influxToken,
                influxBucket,
                influxOrg,
                timeout: 30000 // 30 second timeout
            }),
      });

     if (response_con.ok) {
         const healthData = await response_con.json();
         console.log(`✅ Connection successful to ${connection.name}:`, healthData);

         setConnectionStatus({
             id: connection.id,
             connected: true,
             info: {
                 version: healthData.version || 'Unknown',
                 mode: healthData.mode || 'Unknown',
                 role: healthData.role || 'Unknown',
             },
         });
         return true;
     } else {
         throw new Error(`HTTP ${response_con.status} ${response_con.statusText}`);
     }

      const response = await fetch(`${baseUrl}/health`, {
        method: 'GET',
        signal: controller.signal,
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json',
        }
      });
      
      clearTimeout(timeoutId);
      
      if (response.ok) {
        const healthData = await response.json();
        console.log(`✅ Connection successful to ${connection.name}:`, healthData);
        
        setConnectionStatus({
          id: connection.id,
          connected: true,
          info: {
            version: healthData.version || 'Unknown',
            mode: healthData.mode || 'Unknown',
            role: healthData.role || 'Unknown',
          },
        });
        return true;
      } else {
        throw new Error(`HTTP ${response.status} ${response.statusText}`);
      }
    } catch (error) {
      clearTimeout(timeoutId);
      
      let errorMessage = 'Connection failed';
      if (error instanceof Error) {
        if (error.name === 'AbortError') {
          errorMessage = 'Connection timeout (10s)';
        } else if (error.message.includes('Failed to fetch')) {
          errorMessage = 'Network error - check endpoint and port';
        } else if (error.message.includes('HTTP 404')) {
          errorMessage = 'Health endpoint not found';
        } else if (error.message.includes('HTTP 500')) {
          errorMessage = 'Server error';
        } else {
          errorMessage = error.message;
        }
      }
      
      console.error(`❌ Connection failed to ${connection.name}:`, errorMessage);
      
      setConnectionStatus({
        id: connection.id,
        connected: false,
        error: errorMessage,
      });
      return false;
    }
  };

  const testConnection = async (connection: ConnectionConfig): Promise<boolean> => {
    setConnectingConnectionId(connection.id);
    try {
      const result = await testConnectionInternal(connection);
      return result;
    } finally {
      setConnectingConnectionId(null);
    }
  };

  const setActiveConnection = async (connection: ConnectionConfig): Promise<boolean> => {
      console.log(`== DEBUG -- setActiveConnection conneciton.id = ${connection.id}:`);
      console.log(`== DEBUG -- setActiveConnection conneciton.name = ${connection.name}:`);
      console.log(`== DEBUG -- setActiveConnection conneciton.redisEndpoint = ${connection.redisEndpoint}:`); //redisEndpoint
      setConnectingConnectionId(connection.id);
    try {
      const connected = await testConnectionInternal(connection);
      if (connected) {
        setActiveConnectionState(connection);
        // Update last connected time
        const updatedConnection = {
          ...connection,
          lastConnected: new Date(),
        };
        setConnections(prev => 
          prev.map(conn => conn.id === connection.id ? updatedConnection : conn)
        );
        return true;
      }
      return false;
    } finally {
      setConnectingConnectionId(null);
    }
  };

  const addConnection = (connection: ConnectionConfig) => {
    console.log(" ---> ADDING CONNECTION -------------")
    console.log(connection);
    setConnections(prev => [...prev, connection]);
  };

  const updateConnection = (connection: ConnectionConfig) => {
    setConnections(prev => 
      prev.map(conn => conn.id === connection.id ? connection : conn)
    );
  };

  const deleteConnection = (id: string) => {
    setConnections(prev => prev.filter(conn => conn.id !== id));
    if (activeConnection?.id === id) {
      console.error('No default connection to fall back to after deleteConnection');

      // setActiveConnectionState(defaultConnection);
      // testConnectionInternal(defaultConnection);
    }
  };

  return (
    <ConnectionContext.Provider
      value={{
        activeConnection,
        connectionStatus,
        connections,
        setActiveConnection,
        addConnection,
        updateConnection,
        deleteConnection,
        testConnection,
        connectingConnectionId,
      }}
    >
      {children}
    </ConnectionContext.Provider>
  );
}
