import { useState } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Separator } from '@/components/ui/separator';
import { 
  Plus, 
  Server, 
  Trash2, 
  Edit,
  Play,
  Square,
  WifiOff,
  Loader2,
  AlertCircle,
  CheckCircle,
  Wifi,
  // Globe, // * DISABLED AS API DIALOG BELOW IS DISABLED
  Database
} from 'lucide-react';
import { useConnection } from '@/contexts/ConnectionContext';
import { useSettings } from '@/contexts/SettingsContext';
import { toast } from 'sonner';
import type { ConnectionConfig } from '@/types';

export function Connections() {
  const { 
    connections, 
    activeConnection, 
    connectionStatus, 
    setActiveConnection, 
    addConnection, 
    updateConnection, 
    deleteConnection, 
    connectingConnectionId 
  } = useConnection();

  const activeSettings = useSettings();
  console.log(activeSettings);

  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [editingConnection, setEditingConnection] = useState<ConnectionConfig | null>(null);
  const [connectionForm, setConnectionForm] = useState({
    name: '',
    apiEndpoint: activeSettings.settings.dockerEndpoint,
    apiPort: 8000,
    apiSsl: false,
    redisEndpoint: '',
    redisPort: 6380,
    redisTls: true,
    redisCluster: true,
    password: '',
    type: 'elasticache' as 'elasticache' | 'memorydb',
    region: 'us-east-1'
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const newConnection: ConnectionConfig = {
      id: editingConnection?.id || Date.now().toString(),
      ...connectionForm,
      createdAt: editingConnection?.createdAt || new Date(),
      lastConnected: editingConnection?.lastConnected,
      // Legacy compatibility
      endpoint: connectionForm.apiEndpoint,
      port: connectionForm.apiPort,
      ssl: connectionForm.apiSsl,
      // from global settings
      influxEndpointUrl: activeSettings.settings.influxUrl,
      influxPort: activeSettings.settings.influxPort
    };

    if (editingConnection) {
      updateConnection(newConnection);
    } else {
      addConnection(newConnection);
    }

    setIsDialogOpen(false);
    resetForm();
  };

  const resetForm = () => {
    setConnectionForm({
      name: '',
      apiEndpoint: activeSettings.settings.dockerEndpoint,
      apiPort: 8000,
      apiSsl: false,
      redisEndpoint: '',
      redisPort: 6380,
      redisTls: true,
      redisCluster: true,
      password: '',
      type: 'elasticache',
      region: 'us-east-1'
    });
    setEditingConnection(null);
  };

  const handleEdit = (connection: ConnectionConfig) => {
    setEditingConnection(connection);
    setConnectionForm({
      name: connection.name,
      apiEndpoint: connection.apiEndpoint || connection.endpoint || '',
      apiPort: connection.apiPort || connection.port || 8000,
      apiSsl: connection.apiSsl ?? connection.ssl ?? false,
      redisEndpoint: connection.redisEndpoint || connection.endpoint || '',
      redisPort: connection.redisPort || (connection.redisTls ? 6380 : 6379),
      redisTls: connection.redisTls ?? true,
      redisCluster: connection.redisCluster ?? true,
      password: connection.password || '',
      type: connection.type,
      region: connection.region || 'us-east-1'
    });
    setIsDialogOpen(true);
  };

  const handleDelete = (id: string) => {
    deleteConnection(id);
  };

  const handleConnect = async (connection: ConnectionConfig) => {
    try {
      const haveSettings = await(activeSettings);
      if (!haveSettings) {
          const influxURL = activeSettings.settings.influxUrl;
          console.log(`================> INFLUX in settings >> ${influxURL}`);
      }
      else {
          console.log(` hmm, something is wrong in settings`)
      }
      const success = await setActiveConnection(connection);
      if (success) {
        const influxURL = activeSettings.settings.influxUrl;
        console.log(`================> INFLUX >> ${influxURL}`);
        const apiEndpoint = connection.apiEndpoint || connection.endpoint || 'localhost';
        const apiPort = connection.apiPort || connection.port || 8000;
        toast.success(`Connected to ${connection.name}`, {
          description: `Successfully connected to API service at ${apiEndpoint}:${apiPort}`,
          duration: 3000,
        });
      } else {
        const errorMsg = connectionStatus?.error || 'Connection failed';
        toast.error(`Failed to connect to ${connection.name}`, {
          description: errorMsg,
          duration: 5000,
        });
      }
    } catch (error) {
      toast.error(`Connection Error`, {
        description: error instanceof Error ? error.message : 'Unknown error occurred',
        duration: 5000,
      });
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Connections</h1>
          <p className="text-muted-foreground">
            Manage your ElastiCache and MemoryDB connections
          </p>
        </div>
        <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
          <DialogTrigger asChild>
            <Button onClick={resetForm}>
              <Plus className="mr-2 h-4 w-4" />
              Add Connection
            </Button>
          </DialogTrigger>
          <DialogContent className="sm:max-w-[600px] max-h-[80vh] overflow-y-auto">
            <form onSubmit={handleSubmit}>
              <DialogHeader>
                <DialogTitle>
                  {editingConnection ? 'Edit Connection' : 'Add New Connection'}
                </DialogTitle>
                <DialogDescription>
                  Configure your Valkey cluster connection details
                </DialogDescription>
              </DialogHeader>
              <div className="grid gap-6 py-4">
                {/* Basic Info */}
                <div className="space-y-4">
                  <div className="grid grid-cols-4 items-center gap-4">
                    <Label htmlFor="name" className="text-right">
                      Name
                    </Label>
                    <Input
                      id="name"
                      value={connectionForm.name}
                      onChange={(e) => setConnectionForm(prev => ({ ...prev, name: e.target.value }))}
                      className="col-span-3"
                      placeholder="Production Valkey"
                      required
                    />
                  </div>
                  <div className="grid grid-cols-4 items-center gap-4">
                    <Label htmlFor="type" className="text-right">
                      Type
                    </Label>
                    <Select 
                      value={connectionForm.type} 
                      onValueChange={(value: 'elasticache' | 'memorydb') => 
                        setConnectionForm(prev => ({ ...prev, type: value }))
                      }
                    >
                      <SelectTrigger className="col-span-3">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="elasticache">ElastiCache</SelectItem>
                        <SelectItem value="memorydb">MemoryDB</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="grid grid-cols-4 items-center gap-4">
                    <Label htmlFor="region" className="text-right">
                      Region
                    </Label>
                    <Select 
                      value={connectionForm.region} 
                      onValueChange={(value) => setConnectionForm(prev => ({ ...prev, region: value }))}
                    >
                      <SelectTrigger className="col-span-3">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="us-east-1">US East (N. Virginia)</SelectItem>
                        <SelectItem value="us-west-2">US West (Oregon)</SelectItem>
                        <SelectItem value="eu-west-1">EU (Ireland)</SelectItem>
                        <SelectItem value="ap-southeast-1">Asia Pacific (Singapore)</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                <Separator />

                {/* API Service Configuration */}
                  {// -------------- DISABLED FOR NOW ....
                      // THESE SETTINGS ARE DRIVEN BY SETTING DIALOG ...
                      // ONLY ENABLE AGAIN IF WANT TO OVERWRITE DOCKER PER CONNECTION
                      /*
                <div className="space-y-4">
                  <div className="flex items-center space-x-2">
                    <Globe className="h-5 w-5 text-blue-500" />
                    <h3 className="text-lg font-semibold">API Service Configuration</h3>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Your backend API service that handles Redis connections
                  </p>
                  
                  <div className="grid grid-cols-4 items-center gap-4">
                    <Label htmlFor="apiEndpoint" className="text-right">
                      Docker Endpoint
                    </Label>
                    <Input
                      id="apiEndpoint"
                      value={connectionForm.apiEndpoint || "localhost"}
                      onChange={(e) => setConnectionForm(prev => ({ ...prev, apiEndpoint: e.target.value }))}
                      className="col-span-3"
                      placeholder="your-docker-server.com"
                      required
                    />
                  </div>
                  <div className="grid grid-cols-4 items-center gap-4">
                    <Label htmlFor="apiPort" className="text-right">
                      API Port
                    </Label>
                    <Input
                      id="apiPort"
                      type="number"
                      value={connectionForm.apiPort}
                      onChange={(e) => setConnectionForm(prev => ({ ...prev, apiPort: parseInt(e.target.value) }))}
                      className="col-span-3"
                      placeholder="8000"
                      required
                    />
                  </div>
                  <div className="grid grid-cols-4 items-center gap-4">
                    <Label htmlFor="apiSsl" className="text-right">
                      API HTTPS
                    </Label>
                    <div className="col-span-3 flex items-center space-x-2">
                      <Switch
                        id="apiSsl"
                        checked={connectionForm.apiSsl}
                        onCheckedChange={(checked) => setConnectionForm(prev => ({ ...prev, apiSsl: checked }))}
                      />
                      <span className="text-sm text-muted-foreground">
                        {connectionForm.apiSsl ? 'HTTPS' : 'HTTP'}
                      </span>
                    </div>
                  </div>
                </div>

                <Separator />
                ------------------------------ END DISABLED */}

                {/* Redis Cluster Configuration */}
                <div className="space-y-4">
                  <div className="flex items-center space-x-2">
                    <Database className="h-5 w-5 text-red-500" />
                    <h3 className="text-lg font-semibold">Valkey Cluster Configuration</h3>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    The actual ElastiCache cluster endpoint
                  </p>
                  
                  <div className="grid grid-cols-4 items-center gap-4">
                    <Label htmlFor="redisEndpoint" className="text-right">
                      Endpoint
                    </Label>
                    <Input
                      id="redisEndpoint"
                      value={connectionForm.redisEndpoint}
                      onChange={(e) => setConnectionForm(prev => ({ ...prev, redisEndpoint: e.target.value }))}
                      className="col-span-3"
                      placeholder="cluster.cache.amazonaws.com"
                      required
                    />
                  </div>
                  <div className="grid grid-cols-4 items-center gap-4">
                    <Label htmlFor="redisPort" className="text-right">
                      Port
                    </Label>
                    <Input
                      id="redisPort"
                      type="number"
                      value={connectionForm.redisPort}
                      onChange={(e) => setConnectionForm(prev => ({ ...prev, redisPort: parseInt(e.target.value) }))}
                      className="col-span-3"
                      placeholder="6380"
                      required
                    />
                  </div>
                  <div className="grid grid-cols-4 items-center gap-4">
                    <Label htmlFor="redisTls" className="text-right">
                      TLS
                    </Label>
                    <div className="col-span-3 flex items-center space-x-2">
                      <Switch
                        id="redisTls"
                        checked={connectionForm.redisTls}
                        onCheckedChange={(checked) => setConnectionForm(prev => ({ ...prev, redisTls: checked, redisPort: checked ? 6380 : 6379 }))}
                      />
                      <span className="text-sm text-muted-foreground">
                        {connectionForm.redisTls ? 'TLS enabled (port 6380)' : 'No TLS (port 6379)'}
                      </span>
                    </div>
                  </div>
                    <div className="grid grid-cols-4 items-center gap-4">
                        <Label htmlFor="redisCluster" className="text-right">
                            Use Cluster
                        </Label>
                        <div className="col-span-3 flex items-center space-x-2">
                            <Switch
                                id="redisCluster"
                                checked={connectionForm.redisCluster}
                                onCheckedChange={(checked) => setConnectionForm(prev => ({ ...prev, redisCluster: checked /*, redisPort: checked ? 6380 : 6379 */ }))}
                            />
                            <span className="text-sm text-muted-foreground">
                        {connectionForm.redisCluster ? 'Cluster' : 'Single Instance'}
                      </span>
                        </div>
                    </div>
                  <div className="grid grid-cols-4 items-center gap-4">
                    <Label htmlFor="password" className="text-right">
                      Auth Token
                    </Label>
                    <Input
                      id="password"
                      type="password"
                      value={connectionForm.password}
                      onChange={(e) => setConnectionForm(prev => ({ ...prev, password: e.target.value }))}
                      className="col-span-3"
                      placeholder="Optional"
                    />
                  </div>
                </div>
              </div>
              <DialogFooter>
                <Button type="submit">
                  {editingConnection ? 'Update Connection' : 'Add Connection'}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      </div>

      <div className="grid gap-4">
        {connections.map((connection) => (
          <Card key={connection.id}>
            <CardHeader>
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                  <Server className="h-5 w-5 text-muted-foreground" />
                  <div>
                    <CardTitle className="text-lg">{connection.name}</CardTitle>
                    <CardDescription className="flex items-center space-x-4">
                      <span>
                        API: {connection.apiEndpoint || connection.endpoint}:{connection.apiPort || connection.port}
                      </span>
                      <span>
                        Redis: {connection.redisEndpoint}:{connection.redisPort}
                      </span>
                    </CardDescription>
                  </div>
                </div>
                <div className="flex items-center space-x-2">
                  {activeConnection?.id === connection.id ? (
                    <Badge variant="default" className="text-xs">
                      <div className="w-2 h-2 bg-green-500 rounded-full mr-1"></div>
                      Active
                    </Badge>
                  ) : connectionStatus?.id === connection.id && connectionStatus.connected ? (
                    <Badge variant="outline" className="text-xs">
                      <div className="w-2 h-2 bg-green-500 rounded-full mr-1"></div>
                      Connected
                    </Badge>
                  ) : (
                    <Badge variant="outline" className="text-xs">
                      <div className="w-2 h-2 bg-gray-500 rounded-full mr-1"></div>
                      Disconnected
                    </Badge>
                  )}

                  {connectingConnectionId === connection.id ? (
                    <Button variant="outline" size="sm" disabled>
                      <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                      Connecting
                    </Button>
                  ) : activeConnection?.id === connection.id ? (
                    <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleConnect(connection)}
                        disabled
                    >
                      <Square className="h-4 w-4 mr-1" />
                      Active
                    </Button>
                  ) : (
                    <Button 
                      variant="outline" 
                      size="sm"
                      onClick={() => handleConnect(connection)}
                    >
                      <Play className="h-4 w-4 mr-1" />
                      Connect
                    </Button>
                  )}

                  <Button variant="outline" size="sm" onClick={() => handleEdit(connection)}>
                    <Edit className="h-4 w-4" />
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => handleDelete(connection.id)}>
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm mb-4">
                <div>
                  <p className="text-muted-foreground">Type</p>
                  <div className="flex items-center space-x-2">
                    <Badge variant="outline">
                      {connection.type === 'elasticache' ? 'ElastiCache' : 'MemoryDB'}
                    </Badge>
                  </div>
                </div>
                <div>
                  <p className="text-muted-foreground">Encryption</p>
                  <div className="flex items-center space-x-1">
                    <Badge variant={connection.redisTls ? 'default' : 'secondary'} className="text-xs">
                      {connection.redisTls ? 'TLS' : 'No TLS'}
                    </Badge>
                    <Badge variant={connection.apiSsl || connection.ssl ? 'default' : 'secondary'} className="text-xs">
                      {connection.apiSsl || connection.ssl ? 'HTTPS' : 'HTTP'}
                    </Badge>
                  </div>
                </div>
                <div>
                  <p className="text-muted-foreground">Cluster</p>
                  <div className="flex items-center space-x-1">
                      <Badge variant={connection.redisCluster ? 'default' : 'secondary'} className="text-xs">
                          {connection.redisCluster ? 'Cluster' : 'Instance'}
                      </Badge>
                  </div>
                </div>
                <div>
                  <p className="text-muted-foreground">Region</p>
                  <p className="font-medium">{connection.region}</p>
                </div>
                <div>
                  <p className="text-muted-foreground">Status</p>
                  <div className="flex items-center space-x-1">
                    {connectionStatus?.id === connection.id && connectionStatus.connected ? (
                      <>
                        <Wifi className="h-3 w-3 text-green-500" />
                        <p className="font-medium text-green-500">Connected</p>
                      </>
                    ) : (
                      <>
                        <WifiOff className="h-3 w-3 text-gray-500" />
                        <p className="font-medium text-gray-500">Disconnected</p>
                      </>
                    )}
                  </div>
                </div>
              </div>
              
              {/* Error Display */}
              {connectionStatus?.id === connection.id && connectionStatus.error && (
                <Alert className="mt-3 border-red-200 bg-red-50">
                  <AlertCircle className="h-4 w-4 text-red-600" />
                  <AlertDescription className="text-red-800">
                    <strong>Connection Failed:</strong> {connectionStatus.error}
                  </AlertDescription>
                </Alert>
              )}
              
              {/* Success Display */}
              {connectionStatus?.id === connection.id && connectionStatus.connected && connectionStatus.info && (
                <Alert className="mt-3 border-green-200 bg-green-50">
                  <CheckCircle className="h-4 w-4 text-green-600" />
                  <AlertDescription className="text-green-800">
                    <strong>Connected Successfully</strong>
                    {connectionStatus.info.version !== 'Unknown' && (
                      <span> • Version: {connectionStatus.info.version}</span>
                    )}
                    {connectionStatus.info.mode !== 'Unknown' && (
                      <span> • Mode: {connectionStatus.info.mode}</span>
                    )}
                  </AlertDescription>
                </Alert>
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      {connections.length === 0 && (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12">
            <Server className="h-12 w-12 text-muted-foreground mb-4" />
            <h3 className="text-lg font-semibold mb-2">No connections configured</h3>
            <p className="text-muted-foreground text-center mb-4">
              Get started by adding your first ElastiCache or MemoryDB connection
            </p>
              <p>
                  <h3 className="text-lg font-semibold mb-2">Note: Make sure you setup default settings with the gear symbol from the header first</h3>
              </p>
            <Button onClick={() => setIsDialogOpen(true)}>
              <Plus className="mr-2 h-4 w-4" />
              Add Your First Connection
            </Button>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
