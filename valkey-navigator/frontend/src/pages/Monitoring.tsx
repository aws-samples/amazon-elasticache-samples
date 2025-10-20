import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, ResponsiveContainer } from 'recharts';
import { valkeyApi } from '@/services/valkeyApi';
import { useConnection } from '@/contexts/ConnectionContext';
import { Progress } from '@/components/ui/progress';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Skeleton } from '@/components/ui/skeleton';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ScrollArea } from '@/components/ui/scroll-area';
import { 
  Activity, 
  Database, 
  Users, 
  RefreshCw,
  Zap,
  HardDrive,
  Cpu,
  AlertCircle,
  Server,
  Layers,
  Brain,
  Loader2,
  BarChart3,
  Network,
  Hash
} from 'lucide-react';
import { useEnhancedValkeyMetrics, useValkeyHealth, useNodeMetrics, useSlotStats } from '@/hooks/useValkeyMetrics';
import { useRecommendations } from '@/hooks/useRecommendations';
import { ConnectionsModal } from '@/components/ConnectionsModal';
import { NodeMetricsSection } from '@/components/NodeMetricsSection';
import { CollectionOverview } from '@/components/CollectionOverview';
import { MetricProgressGrid } from '@/components/MetricCategoryProgress';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { useState, useEffect } from 'react';

export function Monitoring() {
  const { activeConnection } = useConnection();
  const redisEndpoint = activeConnection?.redisEndpoint;
    console.log('needed for query API >>>>>>> ');
    console.log(activeConnection);
    console.log('<<<<<<<<<<<<<<<<<<<<<<<<<<<<>>>>>>>>>>>>>>>>>>>>>>>>>>>> ');

  const influxEndpointUrl = activeConnection?.influxEndpointUrl;
  const influxPort = activeConnection?.influxPort.toString();
  const influxToken = activeConnection?.influxToken;
  const influxBucket = activeConnection?.influxBucket;
  const influxOrg = activeConnection?.influxOrg;
  // History state
  const now = new Date();
  const defaultStart = new Date(now.getTime() - 12 * 60 * 60 * 1000); // last 2h
  const [startInput, setStartInput] = useState<string>(defaultStart.toISOString().slice(0,16)); // yyyy-MM-ddTHH:mm
  const [endInput, setEndInput] = useState<string>(now.toISOString().slice(0,16));
  const [customMetric, setCustomMetric] = useState<string>('commands.instantaneous_ops_per_sec');
  const [loadingHistory, setLoadingHistory] = useState<boolean>(false);
  const [hist1, setHist1] = useState<any[]>([]); // server.system_cpu_percentage
  const [hist2, setHist2] = useState<any[]>([]); // memory.allocator_active
  const [hist3, setHist3] = useState<any[]>([]); // keyspace.keyspace_info.db0.keys
  const [hist4, setHist4] = useState<any[]>([]); // custom

  const loadHistory = async () => {
    setLoadingHistory(true);
    const startIso = toIso(startInput);
    const endIso = toIso(endInput);
    try {
      let [s1,s2,s3,s4] = await Promise.all([
        valkeyApi.getHistorySeries('server.system_cpu_percentage', startIso, endIso, redisEndpoint, influxEndpointUrl, influxPort, influxToken, influxBucket, influxOrg),
        valkeyApi.getHistorySeries('memory.allocator_active', startIso, endIso, redisEndpoint, influxEndpointUrl, influxPort, influxToken, influxBucket, influxOrg),
        valkeyApi.getHistorySeries('keyspace.keyspace_info.db0.keys', startIso, endIso, redisEndpoint, influxEndpointUrl, influxPort, influxToken, influxBucket, influxOrg),
        valkeyApi.getHistorySeries(customMetric, startIso, endIso, redisEndpoint, influxEndpointUrl, influxPort, influxToken, influxBucket, influxOrg),
      ]);
      if (!s1?.points || s1.points.length === 0) {
        s1 = await valkeyApi.getHistorySeries('server.system_cpu_percent', startIso, endIso, redisEndpoint, influxEndpointUrl, influxPort, influxToken, influxBucket, influxOrg);
      }
      setHist1((s1?.points||[]).map((p:any)=>({ time: p.time, value: p.value })));
      setHist2((s2?.points||[]).map((p:any)=>({ time: p.time, value: p.value })));
      setHist3((s3?.points||[]).map((p:any)=>({ time: p.time, value: p.value })));
      setHist4((s4?.points||[]).map((p:any)=>({ time: p.time, value: p.value })));
    } catch (e) {
      console.error('Failed to load history', e);
      setHist1([]); setHist2([]); setHist3([]); setHist4([]);
    } finally {
      setLoadingHistory(false);
    }
  };

  // Determine InfluxDB UI URL preference: use active connection if defined, else backend/env
  useEffect(() => {
    let mounted = true;

    // Helper to safely set global for backward compatibility with existing button logic
    const setGlobalInfluxUrl = (url?: string) => {
      if (!mounted) return;
      if (url) {
        (window as any).influx_url = url;
        console.log('Influx URL set:', url);
      }
    };

    // If active connection provides an Influx endpoint, prefer it
    const connUrl = activeConnection?.influxEndpointUrl?.trim();
    const connPort = activeConnection?.influxPort;
    if (connUrl) {
      try {
        // Build a proper URL. If connUrl already includes scheme, use it; otherwise default to http.
        let finalUrl: string;
        try {
          // Try as-is
          const u = new URL(connUrl);
          finalUrl = u.toString().replace(/\/$/, '');
        } catch {
          // Not a full URL; assemble with port if present
          const scheme = connUrl.startsWith('http://') || connUrl.startsWith('https://') ? '' : 'https://';
          finalUrl = `${scheme}${connUrl.replace(/\/$/, '')}`;
        }
        // Append port if provided and not already present in the URL
        try {
          const u2 = new URL(finalUrl);
          if (connPort && !u2.port) {
            u2.port = String(connPort);
          }
          setGlobalInfluxUrl(u2.toString().replace(/\/$/, ''));
        } catch {
          // Fallback to the raw connUrl if URL constructor fails
          setGlobalInfluxUrl(finalUrl);
        }
      } catch (e) {
        console.warn('Failed to construct Influx URL from active connection, falling back to backend/env', e);
        // Fall back to backend/env fetch below
      }
    }

    // If no connection URL, or construction failed, fetch from backend/env
    if (!connUrl) {
      (async () => {
        try {
          const res = await valkeyApi.getInfluxUrl();
          setGlobalInfluxUrl(res?.url);
        } catch (e) {
          console.error('Failed to load Influx URL from backend', e);
        }
      })();
    }

    return () => { mounted = false; };
  }, [activeConnection]);

  // Auto-refresh custom metric graph when selection changes
  useEffect(() => {
    if (!customMetric) return;
    // Trigger the same history load used by the Refresh button
    loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [customMetric]);

  const toIso = (local: string) => {
    // local from datetime-local without seconds
    try { return new Date(local).toISOString(); } catch { return new Date().toISOString(); }
  }
  const { 
    data: healthData, 
    isLoading: isLoadingHealth, 
    error: healthError 
  } = useValkeyHealth();

  const {
    recommendation,
    isLoading: isLoadingRecommendations,
    error: recommendationsError,
    getRecommendations,
    clear: clearRecommendations
  } = useRecommendations();

  const {
    data: nodeMetrics,
    clusterInfo,
    isLoading: isLoadingNodes,
    error: nodeMetricsError,
    refresh: refreshNodes
  } = useNodeMetrics({
    autoRefresh: false, // Disabled auto-refresh - node metrics are semi-static
    refreshInterval: 120000 // Changed to 2 minutes to reduce server load
  });

  // Enhanced monitoring with parallel processing support - this replaces useValkeyMetrics
  const {
    data: metrics, // Renamed from enhancedData to metrics for consistency
    collectionInfo,
    categoryStates,
    partialResults,
    isLoading,
    error,
    refresh,
    retryCategory,
    retryFailedCategories,
    lastUpdated,
    performanceMetrics
  } = useEnhancedValkeyMetrics({
    autoRefresh: true, // Enable auto-refresh to replace the old useValkeyMetrics behavior
    refreshInterval: 120000, // Changed to 2 minutes to reduce server load
    trackPerformance: true
  });

  const [isConnectionsModalOpen, setIsConnectionsModalOpen] = useState(false);
  const [startSlot, setStartSlot] = useState('0');
  const [endSlot, setEndSlot] = useState('1000');

  // Slot stats hook - only fetch when explicitly requested
  const {
    data: rawSlotStatsData,
    isLoading: isLoadingSlotStats,
    error: slotStatsError,
    refresh: refreshSlotStats,
    lastUpdated: slotStatsLastUpdated,
    setSlotRange
  } = useSlotStats({
    autoRefresh: false, // Don't auto-refresh, only on demand
    fetchOnMount: false, // Don't fetch on mount
    initialStartSlot: parseInt(startSlot, 10) || 0,
    initialEndSlot: parseInt(endSlot, 10) || 1000
  });

  // Transform the API response to include missing aggregate fields
  const slotStatsData = rawSlotStatsData ? {
    ...rawSlotStatsData,
    total_keys: rawSlotStatsData.slots?.reduce((sum, slot) => sum + slot.key_count, 0) || 0,
    total_cpu_usec: rawSlotStatsData.slots?.reduce((sum, slot) => sum + slot.cpu_usec, 0) || 0,
    total_network_bytes_in: rawSlotStatsData.slots?.reduce((sum, slot) => sum + slot.network_bytes_in, 0) || 0,
    total_network_bytes_out: rawSlotStatsData.slots?.reduce((sum, slot) => sum + slot.network_bytes_out, 0) || 0,
  } : null;

  const formatBytes = (bytes: number) => {
    if (!bytes) return '0 B';
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(2)} ${sizes[i]}`;
  };

  const formatUptime = (seconds: number) => {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    
    if (days > 0) return `${days}d ${hours}h`;
    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
  };

  const handleRefresh = async () => {
    // Refresh both main metrics and node metrics
    await Promise.all([
      refresh(),
      refreshNodes()
    ]);
  };

  // Generate prompt string from current metrics
  const generateRecommendationPrompt = () => {
    if (!metrics) return '';
    
    const memoryUsage = metrics.memory.maxmemory > 0 
      ? (metrics.memory.used_memory / metrics.memory.maxmemory) * 100 
      : 0;
    
    const promptParts = [
      `Memory Usage ${memoryUsage.toFixed(1)}%`,
      `CPU Usage ${(metrics.server?.system_cpu_percent?.toFixed(1) || 0)}%`,
      `Connected Clients ${metrics.connections.connected_clients}`,
      `Hit Rate ${(metrics.performance.hit_rate || 0).toFixed(1)}%`,
      `Operations per second ${((metrics.commands as any)?.instantaneous_ops_per_sec || 0)}`
    ];
    
    return promptParts.join(', ');
  };

  const handleGetRecommendations = async () => {
    const prompt = generateRecommendationPrompt();
    await getRecommendations(prompt);
  };

  // Input handlers
  const handleStartSlotChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    // Allow empty string for better UX
    setStartSlot(value);
  };

  const handleEndSlotChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    // Allow empty string for better UX
    setEndSlot(value);
  };

  // Slot stats handlers
  const handleLoadSlotStats = async () => {
    // Parse and validate slot range with defaults
    const startSlotNum = parseInt(startSlot, 10) || 0;
    const endSlotNum = parseInt(endSlot, 10) || 1000;
    
    // Validate slot range
    if (startSlotNum < 0 || endSlotNum < 0 || startSlotNum > 16383 || endSlotNum > 16383 || startSlotNum > endSlotNum) {
      console.error('Invalid slot range:', { startSlotNum, endSlotNum });
      return; // Invalid range
    }
    
    console.log('Loading slot stats with range:', { startSlotNum, endSlotNum });
    
    // Update the hook's internal slot range state and fetch data
    setSlotRange(startSlotNum, endSlotNum);
    
    // Use the parsed numeric values for the API call
    await refreshSlotStats(startSlotNum, endSlotNum);
  };

  const formatCpuTime = (microseconds: number) => {
    if (!microseconds) return '0ms';
    const ms = microseconds / 1000;
    return ms < 1 ? `${microseconds}μs` : `${ms.toFixed(1)}ms`;
  };

  const getUsageLevel = (keyCount: number, maxKeys: number) => {
    if (!maxKeys) return 'Low';
    const percentage = (keyCount / maxKeys) * 100;
    if (percentage > 70) return 'High';
    if (percentage > 30) return 'Medium';
    return 'Low';
  };

  const MetricCard = ({
    title, 
    value, 
    subtitle,
    icon: Icon, 
    color = "default",
    isLoading = false
  }: {
    title: string;
    value: string | number;
    subtitle?: string;
    icon: any;
    color?: 'default' | 'success' | 'warning' | 'danger';
    isLoading?: boolean;
  }) => (
    <Card>
      <CardContent className="p-6">
        <div className="flex items-center justify-between">
          <div className="flex-1">
            <p className="text-sm text-muted-foreground mb-1">{title}</p>
            {isLoading ? (
              <Skeleton className="h-8 w-20" />
            ) : (
              <p className="text-2xl font-bold">{value}</p>
            )}
            {subtitle && (
              <p className="text-xs text-muted-foreground mt-1">{subtitle}</p>
            )}
          </div>
          <Icon className={`h-8 w-8 ${
            color === 'success' ? 'text-green-600' :
            color === 'warning' ? 'text-yellow-600' :
            color === 'danger' ? 'text-red-600' :
            'text-blue-600'
          }`} />
        </div>
      </CardContent>
    </Card>
  );

  // Show loading state
  if (isLoading && !metrics) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold tracking-tight">Monitoring</h1>
            <p className="text-muted-foreground">
              Loading Valkey cluster metrics...
            </p>
          </div>
        </div>
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Card key={i}>
              <CardContent className="p-6">
                <Skeleton className="h-4 w-24 mb-2" />
                <Skeleton className="h-8 w-16 mb-1" />
                <Skeleton className="h-3 w-20" />
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  // Show error state
  if (error && !metrics) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold tracking-tight">Monitoring</h1>
            <p className="text-muted-foreground">
              Unable to load Valkey cluster metrics
            </p>
          </div>
          <Button variant="outline" onClick={handleRefresh}>
            <RefreshCw className="mr-2 h-4 w-4" />
            Retry
          </Button>
        </div>
        <Alert>
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>
            Failed to connect to Valkey cluster: {error}
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  if (!metrics) return null;

  const memoryUsagePercent = metrics.memory.maxmemory > 0 
    ? (metrics.memory.used_memory / metrics.memory.maxmemory) * 100 
    : 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Valkey Cluster Monitoring</h1>
          <p className="text-muted-foreground">
            Real-time metrics and performance monitoring for your Valkey cluster
          </p>
          {lastUpdated && (
            <p className="text-xs text-muted-foreground mt-1">
              Last updated: {lastUpdated.toLocaleTimeString()}
            </p>
          )}
        </div>
        <div className="flex items-center space-x-2">
          {isLoadingHealth ? (
            <Badge variant="outline">
              <div className="w-2 h-2 bg-gray-400 rounded-full mr-2"></div>
              Loading...
            </Badge>
          ) : healthData?.status ? (
            <Badge variant={healthData.status === 'healthy' ? 'default' : 'destructive'}>
              <div className={`w-2 h-2 rounded-full mr-2 ${
                healthData.status === 'healthy' ? 'bg-green-500' : 'bg-red-500'
              }`}></div>
              {healthData.status}
            </Badge>
          ) : healthError ? (
            <Badge variant="destructive">
              <div className="w-2 h-2 bg-red-500 rounded-full mr-2"></div>
              Error
            </Badge>
          ) : null}
          <Button variant="outline" size="sm" onClick={handleRefresh} disabled={isLoading}>
            <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {/* Key Metrics Grid */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
        <MetricCard
          title="Memory Usage"
          value={`${memoryUsagePercent.toFixed(1)}%`}
          subtitle={`${metrics.memory.used_memory_human} / ${metrics.memory.maxmemory_human}`}
          icon={HardDrive}
          color={memoryUsagePercent > 80 ? 'danger' : memoryUsagePercent > 60 ? 'warning' : 'success'}
          isLoading={isLoading}
        />

        <MetricCard
          title="CPU Usage"
          value={`${metrics.server?.system_cpu_percent?.toFixed(1) || 0}%`}
          subtitle={`${metrics.server?.system_cpu_count || 0} cores available`}
          icon={Cpu}
          color="default"
          isLoading={isLoading}
        />
        
        <Card 
          className="cursor-pointer hover:bg-muted/50 transition-colors"
          onClick={() => setIsConnectionsModalOpen(true)}
        >
          <CardContent className="p-6">
            <div className="flex items-center justify-between">
              <div className="flex-1">
                <p className="text-sm text-muted-foreground mb-1">Connected Clients</p>
                {isLoading ? (
                  <Skeleton className="h-8 w-20" />
                ) : (
                  <p className="text-2xl font-bold">{metrics.connections.connected_clients}</p>
                )}
                <p className="text-xs text-muted-foreground mt-1">Click to view details</p>
              </div>
              <Users className="h-8 w-8 text-blue-600" />
            </div>
          </CardContent>
        </Card>
        
        <MetricCard
          title="Operations/sec"
          value={((metrics.commands as any)?.instantaneous_ops_per_sec || 0).toLocaleString()}
          subtitle={`${((metrics.commands as any)?.total_commands_processed || 0).toLocaleString()} total commands`}
          icon={Zap}
          color="success"
          isLoading={isLoading}
        />
        
        <MetricCard
          title="Hit Rate"
          value={`${((metrics.performance as any)?.hit_ratio_percent || 0).toFixed(1)}%`}
          subtitle={`${(metrics.performance.keyspace_hits || 0).toLocaleString()} hits`}
          icon={Activity}
          color={((metrics.performance as any)?.hit_ratio_percent || 0) > 90 ? 'success' : ((metrics.performance as any)?.hit_ratio_percent || 0) > 70 ? 'warning' : 'danger'}
          isLoading={isLoading}
        />
      </div>

      {/* Server Info Banner */}
      {metrics.server && (
        <Card>
          <CardContent className="p-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-4">
                <Server className="h-6 w-6 text-blue-600" />
                <div>
                  <h3 className="font-semibold">Valkey {metrics.server?.valkey_version || 'Unknown'}</h3>
                  <p className="text-sm text-muted-foreground">
                    {metrics.server.redis_mode} • Uptime: {formatUptime(metrics.server.uptime_in_seconds)} • 
                    Port: {metrics.server.tcp_port} • PID: {metrics.server.process_id}
                  </p>
                </div>
              </div>
              {metrics.cluster && metrics.cluster.cluster_enabled > 0 && (
                <div className="flex items-center space-x-2">
                  <Layers className="h-4 w-4" />
                  <Badge variant="outline">
                    Cluster: {metrics.cluster.cluster_size} nodes
                  </Badge>
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Detailed Metrics */}
      <Tabs defaultValue="overview" className="space-y-4">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="enhanced">Enhanced Monitoring</TabsTrigger>
          <TabsTrigger value="history">History</TabsTrigger>
          <TabsTrigger value="memory">Memory</TabsTrigger>
          <TabsTrigger value="performance">Performance</TabsTrigger>
          <TabsTrigger value="cluster">Cluster</TabsTrigger>
          <TabsTrigger value="keyspace">Keyspace</TabsTrigger>
          <TabsTrigger value="recommendations">Recommendations</TabsTrigger>
        </TabsList>

        <TabsContent value="history" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Historical Metrics</CardTitle>
              <CardDescription>
                Select a time range and metric. Data filtered by active redisEndpoint.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex flex-wrap gap-4 items-end">
                <div>
                  <Label htmlFor="startTime">Start</Label>
                  <Input id="startTime" type="datetime-local" value={startInput} onChange={(e)=>setStartInput(e.target.value)} />
                </div>
                <div>
                  <Label htmlFor="endTime">End</Label>
                  <Input id="endTime" type="datetime-local" value={endInput} onChange={(e)=>setEndInput(e.target.value)} />
                </div>
                <div>
                  <Label>Custom Metric</Label>
                  <Select value={customMetric} onValueChange={setCustomMetric}>
                    <SelectTrigger className="w-80">
                      <SelectValue placeholder="Select metric" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="commands.instantaneous_ops_per_sec">commands.instantaneous_ops_per_sec</SelectItem>
                      <SelectItem value="performance.ops_per_sec">performance.ops_per_sec</SelectItem>
                      <SelectItem value="memory.used_memory">memory.used_memory</SelectItem>
                      <SelectItem value="server.system_cpu_percentage">server.system_cpu_percentage</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    disabled={loadingHistory}
                    onClick={loadHistory}
                  >
                    <RefreshCw className={`mr-2 h-4 w-4 ${loadingHistory ? 'animate-spin' : ''}`} />
                    Refresh
                  </Button>
                  <Button
                    variant="outline"
                    onClick={()=>{
                      const influxUrl = (window as any).influx_url;
                      if (!influxUrl) {
                        alert('InfluxDB URL not loaded yet. Please try again in a moment.');
                        return;
                      }
                      window.open(influxUrl, '_blank');
                    }}
                  >
                    InfluxDB UI
                  </Button>
                </div>
              </div>

              <div className="grid gap-6 md:grid-cols-2">
                <Card>
                  <CardHeader>
                    <CardTitle>server.system_cpu_percentage ({redisEndpoint || 'all'})</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="h-64">
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={hist1}>
                          <CartesianGrid strokeDasharray="3 3" />
                          <XAxis dataKey="time" tick={{ fontSize: 10 }} minTickGap={20} />
                          <YAxis tick={{ fontSize: 10 }} />
                          <Tooltip />
                          <Line type="monotone" dataKey="value" stroke="#2563eb" dot={false} />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  </CardContent>
                </Card>
                <Card>
                  <CardHeader>
                    <CardTitle>memory.allocator_active ({redisEndpoint || 'all'})</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="h-64">
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={hist2}>
                          <CartesianGrid strokeDasharray="3 3" />
                          <XAxis dataKey="time" tick={{ fontSize: 10 }} minTickGap={20} />
                          <YAxis tick={{ fontSize: 10 }} />
                          <Tooltip />
                          <Line type="monotone" dataKey="value" stroke="#16a34a" dot={false} />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  </CardContent>
                </Card>
                <Card>
                  <CardHeader>
                    <CardTitle>keyspace.keyspace_info.db0.keys ({redisEndpoint || 'all'})</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="h-64">
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={hist3}>
                          <CartesianGrid strokeDasharray="3 3" />
                          <XAxis dataKey="time" tick={{ fontSize: 10 }} minTickGap={20} />
                          <YAxis tick={{ fontSize: 10 }} />
                          <Tooltip />
                          <Line type="monotone" dataKey="value" stroke="#9333ea" dot={false} />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  </CardContent>
                </Card>
                <Card>
                  <CardHeader>
                    <CardTitle>{customMetric} ({redisEndpoint || 'all'})</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="h-64">
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={hist4}>
                          <CartesianGrid strokeDasharray="3 3" />
                          <XAxis dataKey="time" tick={{ fontSize: 10 }} minTickGap={20} />
                          <YAxis tick={{ fontSize: 10 }} />
                          <Tooltip />
                          <Line type="monotone" dataKey="value" stroke="#f59e0b" dot={false} />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  </CardContent>
                </Card>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="enhanced" className="space-y-4">
          <div className="space-y-6">
            {/* Collection Overview */}
            <CollectionOverview
              collectionInfo={collectionInfo}
              categoryStates={categoryStates}
              performanceMetrics={performanceMetrics}
              isLoading={isLoading}
              onRetryAll={refresh}
              onRetryFailed={retryFailedCategories}
              partialResults={partialResults}
            />

            {/* Metric Category Progress Grid */}
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center space-x-2">
                  <Activity className="h-5 w-5" />
                  <span>Metric Collection Progress</span>
                </CardTitle>
                <CardDescription>
                  Real-time status of individual metric category collection
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  <div className="flex items-center justify-between">
                    <p className="text-sm text-muted-foreground">
                      Track the status of each metric category with detailed timing information
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={refresh}
                      disabled={isLoading}
                    >
                      <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
                      Test Enhanced Collection
                    </Button>
                  </div>
                  
                  <MetricProgressGrid
                    categoryStates={categoryStates}
                    onRetryCategory={retryCategory}
                    compact={false}
                  />
                </div>
              </CardContent>
            </Card>

            {/* Performance Tracking */}
            {performanceMetrics && (
              <Card>
                <CardHeader>
                  <CardTitle>Collection Performance History</CardTitle>
                  <CardDescription>
                    Historical performance data for metric collection operations
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="grid gap-4 md:grid-cols-3 mb-6">
                    <div className="text-center">
                      <div className="text-2xl font-bold text-blue-600">
                        {performanceMetrics.totalCollections}
                      </div>
                      <p className="text-sm text-muted-foreground">Total Collections</p>
                    </div>
                    <div className="text-center">
                      <div className="text-2xl font-bold text-green-600">
                        {performanceMetrics.averageCollectionTime.toFixed(2)}s
                      </div>
                      <p className="text-sm text-muted-foreground">Average Duration</p>
                    </div>
                    <div className="text-center">
                      <div className="text-2xl font-bold text-purple-600">
                        {collectionInfo ? 
                          (collectionInfo.total_duration_seconds < performanceMetrics.averageCollectionTime ? 
                            '↑ Faster' : '↓ Slower') : 'N/A'
                        }
                      </div>
                      <p className="text-sm text-muted-foreground">vs Average</p>
                    </div>
                  </div>

                  {/* Category Performance Breakdown */}
                  <div className="space-y-3">
                    <h4 className="text-sm font-medium">Category Performance</h4>
                    {performanceMetrics.categoryPerformance.map((cat) => (
                      <div key={cat.category} className="flex items-center justify-between p-3 border rounded-lg">
                        <div className="flex items-center space-x-3">
                          <span className="text-sm font-medium capitalize">{cat.category}</span>
                          <Badge variant={cat.successRate > 90 ? 'default' : cat.successRate > 70 ? 'secondary' : 'destructive'}>
                            {cat.successRate.toFixed(0)}% success
                          </Badge>
                        </div>
                        <div className="text-right">
                          <div className="text-sm font-mono">
                            {cat.averageDuration.toFixed(2)}s avg
                          </div>
                          <div className="text-xs text-muted-foreground">
                            {cat.timings.length} samples
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Current Metrics Display - Now using enhanced data by default */}
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
              <MetricCard
                title="Memory Usage"
                value={`${metrics?.memory.maxmemory > 0 
                  ? ((metrics.memory.used_memory / metrics.memory.maxmemory) * 100).toFixed(1) 
                  : 0}%`}
                subtitle={`${metrics?.memory.used_memory_human} / ${metrics?.memory.maxmemory_human}`}
                icon={HardDrive}
                color="success"
                isLoading={isLoading}
              />
              <MetricCard
                title="CPU Usage"
                value={`${metrics?.server?.system_cpu_percent?.toFixed(1) || 0}%`}
                subtitle={`${metrics?.server?.system_cpu_count || 0} cores`}
                icon={Cpu}
                color="default"
                isLoading={isLoading}
              />
              <MetricCard
                title="Operations/sec"
                value={((metrics?.commands as any)?.instantaneous_ops_per_sec || 0).toLocaleString()}
                subtitle="Real-time operations"
                icon={Zap}
                color="success"
                isLoading={isLoading}
              />
              <MetricCard
                title="Hit Rate"
                value={`${((metrics?.performance as any)?.hit_ratio_percent || 0).toFixed(1)}%`}
                subtitle={`${(metrics?.performance?.keyspace_hits || 0).toLocaleString()} hits`}
                icon={Activity}
                color="success"
                isLoading={isLoading}
              />
            </div>

            {/* Demo Alert */}
            <Alert>
              <Activity className="h-4 w-4" />
              <AlertDescription>
                This Enhanced Monitoring tab demonstrates the parallel processing capabilities of your updated backend. 
                Click "Test Enhanced Collection" to see real-time progress tracking, individual category status, 
                and performance metrics for each of the 7 metric categories.
              </AlertDescription>
            </Alert>
          </div>
        </TabsContent>

        <TabsContent value="overview" className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            {/* Memory Usage Breakdown */}
            <Card>
              <CardHeader>
                <CardTitle>Memory Usage</CardTitle>
                <CardDescription>Current memory utilization</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-2">
                  <div className="flex justify-between text-sm">
                    <span>Used Memory</span>
                    <span>{metrics.memory.used_memory_human}</span>
                  </div>
                  <Progress value={memoryUsagePercent} className="h-2" />
                  <div className="flex justify-between text-xs text-muted-foreground">
                    <span>0</span>
                    <span>{metrics.memory.maxmemory_human}</span>
                  </div>
                </div>
                
                <div className="grid grid-cols-2 gap-4 pt-4">
                  <div className="text-center">
                    <p className="text-2xl font-bold text-blue-600">{metrics.memory.used_memory_human}</p>
                    <p className="text-xs text-muted-foreground">Used</p>
                  </div>
                  <div className="text-center">
                    <p className="text-2xl font-bold text-green-600">{metrics.memory.used_memory_peak_human}</p>
                    <p className="text-xs text-muted-foreground">Peak</p>
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* Key Statistics */}
            <Card>
              <CardHeader>
                <CardTitle>Key Statistics</CardTitle>
                <CardDescription>Database keys and operations</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  <div className="flex justify-between items-center">
                    <span className="text-sm">Total Keys</span>
                    <span className="font-semibold">{(metrics.keyspace.total_keys || 0).toLocaleString()}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-sm">Keys with Expiry</span>
                    <span className="font-semibold text-orange-600">{(metrics.keyspace.total_expires || 0).toLocaleString()}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-sm">Expired Keys</span>
                    <span className="font-semibold text-red-600">{(metrics.performance.expired_keys || 0).toLocaleString()}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-sm">Evicted Keys</span>
                    <span className="font-semibold text-red-600">{(metrics.performance.evicted_keys || 0).toLocaleString()}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-sm">Hit Rate</span>
                    <span className={`font-semibold ${
                      (metrics.performance.hit_rate || 0) > 90 ? 'text-green-600' :
                      (metrics.performance.hit_rate || 0) > 70 ? 'text-yellow-600' : 'text-red-600'
                    }`}>
                      {(metrics.performance.hit_rate || 0).toFixed(1)}%
                    </span>
                  </div>
                </div>
              </CardContent>
            </Card>

          </div>
        </TabsContent>

        <TabsContent value="memory" className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Memory Details</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-3">
                  <div className="flex justify-between">
                    <span className="text-sm">Used Memory</span>
                    <span className="font-mono text-sm">{metrics.memory.used_memory_human}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">RSS Memory</span>
                    <span className="font-mono text-sm">{metrics.memory.used_memory_rss_human}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Peak Memory</span>
                    <span className="font-mono text-sm">{metrics.memory.used_memory_peak_human}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Memory Fragmentation</span>
                    <span className="font-mono text-sm">{metrics.memory.mem_fragmentation_ratio.toFixed(2)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Memory Allocator</span>
                    <span className="font-mono text-sm">{metrics.memory.mem_allocator}</span>
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Memory Limits</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-3">
                  <div className="flex justify-between">
                    <span className="text-sm">Max Memory</span>
                    <span className="font-mono text-sm">{metrics.memory.maxmemory_human}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Usage Percentage</span>
                    <span className="font-mono text-sm">{memoryUsagePercent.toFixed(1)}%</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Lua Memory</span>
                    <span className="font-mono text-sm">{formatBytes(metrics.memory.used_memory_lua)}</span>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="performance" className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            <Card>
              <CardHeader>
                <CardTitle>Operations</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-center space-y-2">
                  <p className="text-3xl font-bold text-green-600">{metrics.performance.instantaneous_ops_per_sec}</p>
                  <p className="text-sm text-muted-foreground">ops/sec</p>
                  <p className="text-xs text-muted-foreground">
                    {(metrics.performance.total_commands_processed || 0).toLocaleString()} total
                  </p>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Cache Performance</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-3">
                  <div className="flex justify-between">
                    <span className="text-sm">Hit Rate</span>
                    <span className="font-semibold text-green-600">{(metrics.performance.hit_rate || 0).toFixed(1)}%</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Hits</span>
                    <span className="font-mono text-sm">{metrics.performance.keyspace_hits.toLocaleString()}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Misses</span>
                    <span className="font-mono text-sm">{metrics.performance.keyspace_misses.toLocaleString()}</span>
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Key Events</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-3">
                  <div className="flex justify-between">
                    <span className="text-sm">Expired</span>
                    <span className="font-mono text-sm text-orange-600">{(metrics.performance.expired_keys || 0).toLocaleString()}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Evicted</span>
                    <span className="font-mono text-sm text-red-600">{(metrics.performance.evicted_keys || 0).toLocaleString()}</span>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="cluster" className="space-y-4">
          {(() => {
            // Unified cluster detection using multiple sources
            const isClusterMode = (
              // Check main metrics cluster enabled flag
              (metrics?.cluster?.cluster_enabled > 0) ||
              // Check if we have multiple nodes from node metrics
              (nodeMetrics && nodeMetrics.length > 1) ||
              // Check cluster info
              (clusterInfo && clusterInfo.totalNodes > 1) ||
              // Check server redis_mode
              (metrics?.server?.redis_mode === 'cluster')
            );
            
            return isClusterMode;
          })() ? (
            <div className="space-y-6">
              <div className="grid gap-4 md:grid-cols-2">
                <Card>
                  <CardHeader>
                    <CardTitle>Cluster Status</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="space-y-4">
                      <div className="flex justify-between">
                        <span className="text-sm">State</span>
                        <Badge variant={(clusterInfo?.clusterState === 'ok' || nodeMetrics?.some(n => n.status === 'online')) ? 'default' : 'destructive'}>
                          {clusterInfo?.clusterState || 'ok'}
                        </Badge>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-sm">Total Nodes</span>
                        <span className="font-mono text-sm">{clusterInfo?.totalNodes || nodeMetrics?.length || 0} nodes</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-sm">Master Nodes</span>
                        <span className="font-mono text-sm">{clusterInfo?.mastersCount || nodeMetrics?.filter(n => n.role === 'primary').length || 0}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-sm">Replica Nodes</span>
                        <span className="font-mono text-sm">{clusterInfo?.slavesCount || nodeMetrics?.filter(n => n.role === 'secondary').length || 0}</span>
                      </div>
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle>Node Distribution</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="space-y-4">
                      <div className="flex justify-between">
                        <span className="text-sm">Online Nodes</span>
                        <span className="font-mono text-sm text-green-600">{nodeMetrics?.filter(n => n.status === 'online').length || 0}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-sm">Offline Nodes</span>
                        <span className="font-mono text-sm text-red-600">{nodeMetrics?.filter(n => n.status === 'offline').length || 0}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-sm">Total Keys</span>
                        <span className="font-mono text-sm">{nodeMetrics?.reduce((sum, node) => sum + (node.keyCount || 0), 0).toLocaleString() || '0'}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-sm">Total Connections</span>
                        <span className="font-mono text-sm">{nodeMetrics?.reduce((sum, node) => sum + (node.connections || 0), 0).toLocaleString() || '0'}</span>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </div>

              {/* Cluster Slot Statistics */}
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <Hash className="h-5 w-5" />
                    Slot Statistics
                  </CardTitle>
                  <CardDescription>Detailed metrics for cluster slots</CardDescription>
                </CardHeader>
                <CardContent className="space-y-6">
                  {/* Controls Row */}
                  <div className="flex gap-4 items-end">
                    <div className="flex-1 max-w-32">
                      <Label htmlFor="startSlot">Start Slot</Label>
                      <Input
                        id="startSlot"
                        type="number"
                        min="0"
                        max="16383"
                        value={startSlot}
                        onChange={handleStartSlotChange}
                        placeholder="0"
                      />
                    </div>
                    <div className="flex-1 max-w-32">
                      <Label htmlFor="endSlot">End Slot</Label>
                      <Input
                        id="endSlot"
                        type="number"
                        min="0"
                        max="16383"
                        value={endSlot}
                        onChange={handleEndSlotChange}
                        placeholder="1000"
                      />
                    </div>
                    <Button 
                      onClick={handleLoadSlotStats}
                      disabled={(() => {
                        const startNum = parseInt(startSlot, 10) || 0;
                        const endNum = parseInt(endSlot, 10) || 1000;
                        return isLoadingSlotStats || startNum < 0 || endNum < 0 || startNum > 16383 || endNum > 16383 || startNum > endNum;
                      })()}
                    >
                      {isLoadingSlotStats && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                      Load Stats
                    </Button>
                  </div>

                  {/* Summary Stats */}
                  {slotStatsData && (
                    <div className="grid gap-4 md:grid-cols-4">
                      <MetricCard
                        title="Slots Analyzed"
                        value={slotStatsData.total_slots.toLocaleString()}
                        subtitle={`Range: ${slotStatsData.start_slot}-${slotStatsData.end_slot}`}
                        icon={Hash}
                        color="default"
                      />
                      <MetricCard
                        title="Total Keys"
                        value={slotStatsData.total_keys.toLocaleString()}
                        subtitle="Across all slots"
                        icon={Database}
                        color="success"
                      />
                      <MetricCard
                        title="Total CPU Time"
                        value={formatCpuTime(slotStatsData.total_cpu_usec)}
                        subtitle="Aggregate usage"
                        icon={Cpu}
                        color="warning"
                      />
                      <MetricCard
                        title="Total Network I/O"
                        value={formatBytes(slotStatsData.total_network_bytes_in + slotStatsData.total_network_bytes_out)}
                        subtitle={`${formatBytes(slotStatsData.total_network_bytes_in)} in / ${formatBytes(slotStatsData.total_network_bytes_out)} out`}
                        icon={Network}
                        color="default"
                      />
                    </div>
                  )}

                  {/* Slot List */}
                  {slotStatsError && (
                    <Alert>
                      <AlertCircle className="h-4 w-4" />
                      <AlertDescription>
                        Failed to load slot statistics: {slotStatsError}
                        <br />
                        <small className="text-xs text-muted-foreground">
                          Debug: Requested range {startSlot}-{endSlot}
                        </small>
                      </AlertDescription>
                    </Alert>
                  )}

                  {!slotStatsData && !isLoadingSlotStats && !slotStatsError && (
                    <div className="text-center py-8">
                      <BarChart3 className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
                      <h3 className="text-lg font-semibold mb-2">No Slot Data</h3>
                      <p className="text-muted-foreground mb-4">Enter a slot range and click "Load Stats" to view detailed metrics.</p>
                    </div>
                  )}

                  {isLoadingSlotStats && (
                    <div className="flex items-center justify-center py-12">
                      <div className="text-center">
                        <Loader2 className="h-8 w-8 animate-spin mx-auto text-blue-600 mb-2" />
                        <p className="text-sm text-muted-foreground">Loading slot statistics...</p>
                      </div>
                    </div>
                  )}

                  {slotStatsData && slotStatsData.slots && slotStatsData.slots.length > 0 && (
                    <div className="space-y-4">
                      <div className="flex items-center justify-between">
                        <h4 className="text-sm font-medium">Individual Slot Metrics</h4>
                        {slotStatsLastUpdated && (
                          <p className="text-xs text-muted-foreground">
                            Updated: {slotStatsLastUpdated.toLocaleTimeString()}
                          </p>
                        )}
                      </div>
                      <ScrollArea className="h-96 w-full border rounded-md">
                        <div className="p-4 space-y-2">
                          {slotStatsData.slots.map((slot) => {
                            const maxKeys = Math.max(...slotStatsData.slots.map(s => s.key_count));
                            const usageLevel = getUsageLevel(slot.key_count, maxKeys);
                            const keyPercentage = maxKeys > 0 ? (slot.key_count / maxKeys) * 100 : 0;

                            return (
                              <div
                                key={slot.slot_id}
                                className="flex items-center justify-between p-3 border rounded-lg hover:bg-muted/50 transition-colors"
                              >
                                <div className="flex items-center space-x-3">
                                  <Badge variant="outline" className="font-mono">
                                    #{slot.slot_id.toString().padStart(5, '0')}
                                  </Badge>
                                  <div className="flex items-center space-x-2">
                                    <div className="text-sm">
                                      <span className="font-semibold">{slot.key_count.toLocaleString()}</span>
                                      <span className="text-muted-foreground ml-1">keys</span>
                                    </div>
                                    <div className="w-20 bg-muted rounded-full h-2">
                                      <div
                                        className={`h-2 rounded-full ${
                                          usageLevel === 'High' ? 'bg-red-500' :
                                          usageLevel === 'Medium' ? 'bg-yellow-500' : 'bg-green-500'
                                        }`}
                                        style={{ width: `${keyPercentage}%` }}
                                      />
                                    </div>
                                  </div>
                                </div>
                                <div className="flex items-center space-x-4">
                                  <div className="text-xs text-center">
                                    <div className="font-mono">{formatCpuTime(slot.cpu_usec)}</div>
                                    <div className="text-muted-foreground">CPU</div>
                                  </div>
                                  <div className="text-xs text-center">
                                    <div className="font-mono">↓{formatBytes(slot.network_bytes_in)}</div>
                                    <div className="font-mono">↑{formatBytes(slot.network_bytes_out)}</div>
                                  </div>
                                  <Badge
                                    variant={
                                      usageLevel === 'High' ? 'destructive' :
                                      usageLevel === 'Medium' ? 'default' : 'secondary'
                                    }
                                    className="text-xs"
                                  >
                                    {usageLevel}
                                  </Badge>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </ScrollArea>
                    </div>
                  )}
                </CardContent>
              </Card>
            </div>
          ) : (
            <div className="space-y-6">
              <Card>
                <CardContent className="p-8 text-center">
                  <Database className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
                  <h3 className="text-lg font-semibold mb-2">Single Node Mode</h3>
                  <p className="text-muted-foreground mb-4">
                    This Valkey instance is running in single-node mode.
                  </p>
                  {nodeMetrics && nodeMetrics.length > 0 && (
                    <div className="bg-muted/50 rounded-lg p-4 mt-4">
                      <h4 className="font-medium mb-3">Node Information</h4>
                      <div className="grid gap-3 text-sm">
                        <div className="flex justify-between">
                          <span>Node Address:</span>
                          <span className="font-mono">{nodeMetrics[0].nodeAddress}</span>
                        </div>
                        <div className="flex justify-between">
                          <span>Role:</span>
                          <Badge variant="secondary">{nodeMetrics[0].role}</Badge>
                        </div>
                        <div className="flex justify-between">
                          <span>Status:</span>
                          <Badge variant={nodeMetrics[0].status === 'online' ? 'default' : 'destructive'}>
                            {nodeMetrics[0].status}
                          </Badge>
                        </div>
                        <div className="flex justify-between">
                          <span>Memory Usage:</span>
                          <span className="font-mono">{nodeMetrics[0].memory.used} / {nodeMetrics[0].memory.max}</span>
                        </div>
                        <div className="flex justify-between">
                          <span>CPU Usage:</span>
                          <span className="font-mono">{nodeMetrics[0].cpu.percent}%</span>
                        </div>
                        <div className="flex justify-between">
                          <span>Keys:</span>
                          <span className="font-mono">{nodeMetrics[0].keyCount.toLocaleString()}</span>
                        </div>
                        <div className="flex justify-between">
                          <span>Operations/sec:</span>
                          <span className="font-mono">{nodeMetrics[0].opsPerSec.toLocaleString()}</span>
                        </div>
                        <div className="flex justify-between">
                          <span>Uptime:</span>
                          <span className="font-mono">{nodeMetrics[0].uptime}</span>
                        </div>
                      </div>
                    </div>
                  )}
                </CardContent>
              </Card>
            </div>
          )}
        </TabsContent>

        <TabsContent value="keyspace" className="space-y-4">
          <div className="grid gap-4">
            {metrics.keyspace.db0 && (
              <Card>
                <CardHeader>
                  <CardTitle>Database 0</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="grid gap-4 md:grid-cols-3">
                    <div className="text-center">
                      <p className="text-2xl font-bold text-blue-600">{metrics.keyspace.db0.keys.toLocaleString()}</p>
                      <p className="text-sm text-muted-foreground">Keys</p>
                    </div>
                    <div className="text-center">
                      <p className="text-2xl font-bold text-orange-600">{metrics.keyspace.db0.expires.toLocaleString()}</p>
                      <p className="text-sm text-muted-foreground">With Expiry</p>
                    </div>
                    <div className="text-center">
                      <p className="text-2xl font-bold text-purple-600">{formatUptime(metrics.keyspace.db0.avg_ttl)}</p>
                      <p className="text-sm text-muted-foreground">Avg TTL</p>
                    </div>
                  </div>
                </CardContent>
              </Card>
            )}

            <Card>
              <CardHeader>
                <CardTitle>Total Keyspace</CardTitle>
                <CardDescription>Aggregate statistics across all databases</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="text-center">
                    <p className="text-3xl font-bold text-blue-600">{(metrics.keyspace.total_keys || 0).toLocaleString()}</p>
                    <p className="text-sm text-muted-foreground">Total Keys</p>
                  </div>
                  <div className="text-center">
                    <p className="text-3xl font-bold text-orange-600">{(metrics.keyspace.total_expires || 0).toLocaleString()}</p>
                    <p className="text-sm text-muted-foreground">Keys with Expiry</p>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="recommendations" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Performance Recommendations</CardTitle>
              <CardDescription>AI-ready prompt with current system metrics</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                <div className="bg-slate-50 dark:bg-slate-900 rounded-lg p-4 border">
                  <h4 className="text-sm font-medium mb-2">Formatted Prompt</h4>
                  <code className="text-sm font-mono bg-white dark:bg-slate-800 p-2 rounded border block">
                    {generateRecommendationPrompt()}
                  </code>
                </div>
                
                <div className="grid gap-4 md:grid-cols-2">
                  <Card>
                    <CardHeader>
                      <CardTitle className="text-lg">Current Metrics</CardTitle>
                    </CardHeader>
                    <CardContent>
                      <div className="space-y-3">
                        <div className="flex justify-between items-center">
                          <span className="text-sm font-medium">Memory Usage</span>
                          <span className={`font-semibold ${
                            memoryUsagePercent > 80 ? 'text-red-600' :
                            memoryUsagePercent > 60 ? 'text-yellow-600' : 'text-green-600'
                          }`}>
                            {memoryUsagePercent.toFixed(1)}%
                          </span>
                        </div>
                        <div className="flex justify-between items-center">
                          <span className="text-sm font-medium">CPU Usage</span>
                          <span className={`font-semibold ${
                            (metrics.server?.system_cpu_percent || 0) > 80 ? 'text-red-600' :
                            (metrics.server?.system_cpu_percent || 0) > 60 ? 'text-yellow-600' : 'text-green-600'
                          }`}>
                            {(metrics.server?.system_cpu_percent?.toFixed(1) || 0)}%
                          </span>
                        </div>
                        <div className="flex justify-between items-center">
                          <span className="text-sm font-medium">Connected Clients</span>
                          <span className="font-semibold">{metrics.connections.connected_clients}</span>
                        </div>
                        <div className="flex justify-between items-center">
                          <span className="text-sm font-medium">Hit Rate</span>
                          <span className={`font-semibold ${
                            (metrics.performance.hit_rate || 0) > 90 ? 'text-green-600' :
                            (metrics.performance.hit_rate || 0) > 70 ? 'text-yellow-600' : 'text-red-600'
                          }`}>
                            {(metrics.performance.hit_rate || 0).toFixed(1)}%
                          </span>
                        </div>
                      </div>
                    </CardContent>
                  </Card>

                  <Card>
                    <CardHeader>
                      <CardTitle className="text-lg flex items-center gap-2">
                        <Brain className="h-5 w-5" />
                        AI Recommendations
                      </CardTitle>
                    </CardHeader>
                    <CardContent>
                      <div className="space-y-4">
                        {!recommendation && !isLoadingRecommendations && (
                          <div className="text-center py-4">
                            <Button 
                              onClick={handleGetRecommendations}
                              disabled={isLoadingRecommendations}
                              className="w-full"
                            >
                              <Brain className="mr-2 h-4 w-4" />
                              Get AI Recommendations
                            </Button>
                            <p className="text-xs text-muted-foreground mt-2">
                              Analyze current metrics with AI
                            </p>
                          </div>
                        )}

                        {isLoadingRecommendations && (
                          <div className="flex items-center justify-center py-8">
                            <div className="text-center">
                              <Loader2 className="h-8 w-8 animate-spin mx-auto text-blue-600 mb-2" />
                              <p className="text-sm text-muted-foreground">Analyzing metrics...</p>
                            </div>
                          </div>
                        )}

                        {recommendationsError && (
                          <div className="space-y-3">
                            <Alert>
                              <AlertCircle className="h-4 w-4" />
                              <AlertDescription>
                                Failed to get recommendations: {recommendationsError}
                              </AlertDescription>
                            </Alert>
                            <Button 
                              variant="outline" 
                              onClick={handleGetRecommendations}
                              disabled={isLoadingRecommendations}
                              size="sm"
                              className="w-full"
                            >
                              <RefreshCw className="mr-2 h-4 w-4" />
                              Try Again
                            </Button>
                          </div>
                        )}

                        {recommendation && (
                          <div className="space-y-3">
                            <div className="bg-white dark:bg-slate-800 rounded-lg p-4 border">
                              <div className="flex items-start justify-between mb-3">
                                <h5 className="text-sm font-medium flex items-center gap-2">
                                  <Brain className="h-4 w-4 text-blue-600" />
                                  AI Analysis
                                </h5>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  onClick={clearRecommendations}
                                  className="h-6 w-6 p-0 text-muted-foreground hover:text-foreground"
                                >
                                  ×
                                </Button>
                              </div>
                              <div className="text-sm whitespace-pre-line text-slate-700 dark:text-slate-300 leading-relaxed text-left">
                                {recommendation}
                              </div>
                            </div>
                            <Button 
                              variant="outline" 
                              onClick={handleGetRecommendations}
                              disabled={isLoadingRecommendations}
                              size="sm"
                              className="w-full"
                            >
                              <RefreshCw className="mr-2 h-4 w-4" />
                              Refresh Analysis
                            </Button>
                          </div>
                        )}
                      </div>
                    </CardContent>
                  </Card>
                </div>
              </div>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* Node Metrics Section with Error Handling */}
      {nodeMetricsError && (
        <Alert>
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>
            Node metrics error: {nodeMetricsError}
            <Button 
              variant="outline" 
              size="sm" 
              onClick={refreshNodes}
              disabled={isLoadingNodes}
              className="ml-2"
            >
              <RefreshCw className={`mr-2 h-4 w-4 ${isLoadingNodes ? 'animate-spin' : ''}`} />
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}
      <ErrorBoundary>
        <NodeMetricsSection
          nodes={nodeMetrics || []}
          isLoading={isLoadingNodes}
          onRefresh={refreshNodes}
        />
      </ErrorBoundary>

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
