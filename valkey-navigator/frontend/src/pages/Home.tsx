import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { 
  Database, 
  Terminal, 
  Activity,
  ArrowRight,
  Server,
  Users,
  AlertCircle,
  RefreshCw,
  Zap,
  HardDrive,
  Cpu
} from 'lucide-react';
import { Link } from 'react-router';
import { useValkeyMetrics } from '@/hooks/useValkeyMetrics';
import { ConnectionsModal } from '@/components/ConnectionsModal';
import { useState } from 'react';

export function Home() {
  const { 
    data: metrics, 
    isLoading, 
    error, 
    refresh 
  } = useValkeyMetrics({ 
    fetchOnMount: true 
  });

  const [isConnectionsModalOpen, setIsConnectionsModalOpen] = useState(false);


  const formatUptime = (seconds: number) => {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    
    if (days > 0) return `${days}d ${hours}h`;
    if (hours > 0) return `${hours}h`;
    return `${Math.floor((seconds % 3600) / 60)}m`;
  };

  return (
    <div className="space-y-6">
      {/* Welcome Section */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Welcome to ValkeyNavigator</h1>
          <p className="text-muted-foreground">
            Monitor and manage your Valkey cluster with real-time insights and performance metrics.
          </p>
        </div>
      </div>

      {/* Error State */}
      {error && !metrics && (
        <Alert>
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>
            Failed to connect to Valkey cluster: {error}
            <Button variant="outline" size="sm" className="ml-2" onClick={refresh}>
              <RefreshCw className="h-4 w-4 mr-1" />
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {/* Quick Stats */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
        <Card 
          className="cursor-pointer hover:bg-muted/50 transition-colors"
          onClick={() => setIsConnectionsModalOpen(true)}
        >
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Active Connections</CardTitle>
            <Users className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <Skeleton className="h-8 w-16" />
            ) : (
              <div className="text-2xl font-bold">{metrics?.connections?.connected_clients || 0}</div>
            )}
            <p className="text-xs text-muted-foreground">
              Click to view connection details
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Total Keys</CardTitle>
            <Database className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <Skeleton className="h-8 w-16" />
            ) : (
              <div className="text-2xl font-bold">
                {(() => {
                  // Use pre-calculated total_keys from API first
                  if (metrics?.keyspace?.total_keys !== undefined) {
                    return metrics.keyspace.total_keys.toLocaleString();
                  }
                  
                  // Fallback to manual calculation if total_keys not available
                  const keyspaceInfo = metrics?.keyspace?.keyspace_info;
                  if (!keyspaceInfo) return 0;
                  return Object.values(keyspaceInfo).reduce((sum: number, db: any) => sum + (db?.keys || 0), 0).toLocaleString();
                })()}
              </div>
            )}
            <p className="text-xs text-muted-foreground">
              {(() => {
                const keyspaceInfo = metrics?.keyspace?.keyspace_info;
                if (!keyspaceInfo) return 0;
                return Object.values(keyspaceInfo).reduce((sum: number, db: any) => sum + (db?.expires || 0), 0).toLocaleString();
              })()} with expiry
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Memory Usage</CardTitle>
            <HardDrive className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <Skeleton className="h-8 w-16" />
            ) : (
              <div className="text-2xl font-bold">{metrics?.memory?.used_memory_human || '0B'}</div>
            )}
            <p className="text-xs text-muted-foreground">
              {metrics?.memory?.maxmemory_human || 'No limit'} max
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">CPU Usage</CardTitle>
            <Cpu className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <Skeleton className="h-8 w-16" />
            ) : (
              <div className="text-2xl font-bold">{metrics?.server?.system_cpu_percent?.toFixed(1) || 0}%</div>
            )}
            <p className="text-xs text-muted-foreground">
              {metrics?.server?.system_cpu_count || 0} cores available
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Operations/sec</CardTitle>
            <Zap className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <Skeleton className="h-8 w-16" />
            ) : (
              <div className="text-2xl font-bold">{metrics?.commands?.instantaneous_ops_per_sec?.toLocaleString() || 0}</div>
            )}
            <p className="text-xs text-muted-foreground">
              {metrics?.performance?.hit_ratio_percent?.toFixed(1) || 0}% hit rate
            </p>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        {/* Cluster Info */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between">
              Cluster Status
              <Link to="/monitoring">
                <Button variant="outline" size="sm">
                  View Details
                  <ArrowRight className="ml-2 h-4 w-4" />
                </Button>
              </Link>
            </CardTitle>
            <CardDescription>
              Current Valkey cluster information
            </CardDescription>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <div className="space-y-4">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-4 w-1/2" />
              </div>
            ) : metrics ? (
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center space-x-3">
                    <Server className="h-4 w-4 text-muted-foreground" />
                    <div>
                      <p className="text-sm font-medium">Valkey {metrics.server?.valkey_version || 'Unknown'}</p>
                      <p className="text-xs text-muted-foreground">
                        {metrics.server?.redis_mode || 'Unknown'} • Port {metrics.server?.tcp_port || 'Unknown'}
                      </p>
                    </div>
                  </div>
                  <Badge variant="outline">
                    Up {metrics.server?.uptime_in_seconds ? formatUptime(metrics.server.uptime_in_seconds) : 'Unknown'}
                  </Badge>
                </div>

                {metrics.cluster && metrics.cluster.cluster_enabled > 0 ? (
                  <div className="flex items-center justify-between">
                    <div className="flex items-center space-x-3">
                      <Database className="h-4 w-4 text-muted-foreground" />
                      <div>
                        <p className="text-sm font-medium">Cluster Mode</p>
                        <p className="text-xs text-muted-foreground">
                          {(metrics.cluster.connected_slaves || 0) + 1} nodes • Active
                        </p>
                      </div>
                    </div>
                    <Badge variant="default">
                      Active
                    </Badge>
                  </div>
                ) : (
                  <div className="flex items-center space-x-3">
                    <Database className="h-4 w-4 text-muted-foreground" />
                    <div>
                      <p className="text-sm font-medium">Standalone Mode</p>
                      <p className="text-xs text-muted-foreground">Single instance configuration</p>
                    </div>
                  </div>
                )}

                <div className="flex items-center justify-between">
                  <div className="flex items-center space-x-3">
                    <Activity className="h-4 w-4 text-muted-foreground" />
                    <div>
                      <p className="text-sm font-medium">Performance</p>
                      <p className="text-xs text-muted-foreground">
                        {metrics.commands?.instantaneous_ops_per_sec || 0} ops/sec • 
                        {metrics.performance?.hit_ratio_percent?.toFixed(1) || 0}% hit rate
                      </p>
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              <div className="text-center text-muted-foreground">
                <AlertCircle className="h-8 w-8 mx-auto mb-2" />
                <p className="text-sm">Unable to load cluster information</p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Quick Actions */}
        <Card>
          <CardHeader>
            <CardTitle>Quick Actions</CardTitle>
            <CardDescription>
              Navigate to common tasks and features
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3">
              <Link to="/monitoring">
                <Button variant="outline" className="w-full justify-start">
                  <Activity className="mr-2 h-4 w-4" />
                  View Real-time Monitoring
                </Button>
              </Link>
              
              
              <Link to="/cli">
                <Button variant="outline" className="w-full justify-start">
                  <Terminal className="mr-2 h-4 w-4" />
                  Open Command Interface
                </Button>
              </Link>

              <Button 
                variant="outline" 
                className="w-full justify-start" 
                onClick={refresh}
                disabled={isLoading}
              >
                <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
                Refresh Metrics
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Performance Summary */}
      {metrics && (
        <Card>
          <CardHeader>
            <CardTitle>Performance Summary</CardTitle>
            <CardDescription>Key metrics at a glance</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
              <div className="text-center">
                <p className="text-2xl font-bold text-blue-600">
                  {metrics.memory?.used_memory && metrics.memory?.maxmemory 
                    ? ((metrics.memory.used_memory / metrics.memory.maxmemory) * 100).toFixed(1)
                    : '0'}%
                </p>
                <p className="text-sm text-muted-foreground">Memory Usage</p>
              </div>
              <div className="text-center">
                <p className="text-2xl font-bold text-green-600">
                  {metrics.commands?.total_commands_processed?.toLocaleString() || '0'}
                </p>
                <p className="text-sm text-muted-foreground">Commands Processed</p>
              </div>
              <div className="text-center">
                <p className="text-2xl font-bold text-purple-600">
                  {metrics.commands?.keyspace_hits?.toLocaleString() || '0'}
                </p>
                <p className="text-sm text-muted-foreground">Cache Hits</p>
              </div>
              <div className="text-center">
                <p className="text-2xl font-bold text-orange-600">
                  {((metrics.commands?.expired_keys || 0) + (metrics.commands?.evicted_keys || 0)).toLocaleString()}
                </p>
                <p className="text-sm text-muted-foreground">Keys Expired/Evicted</p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      <ConnectionsModal
        isOpen={isConnectionsModalOpen}
        onClose={() => setIsConnectionsModalOpen(false)}
        connections={metrics?.connections?.client_list_sample}
        totalConnections={metrics?.connections?.connected_clients || 0}
        onRefresh={refresh}
        isLoading={isLoading}
      />
    </div>
  );
}
