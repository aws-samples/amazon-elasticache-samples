import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { 
  Server, 
  Users, 
  HardDrive, 
  Cpu, 
  Zap,
  Circle
} from 'lucide-react';

export interface NodeMetrics {
  nodeId: string;
  nodeAddress: string;
  role: 'primary' | 'secondary';
  status: 'online' | 'offline';
  memory: { 
    used: string; 
    max: string; 
    usedBytes: number;
    maxBytes: number;
    percent: number 
  };
  cpu: { 
    percent: number; 
    cores: number 
  };
  connections: number;
  opsPerSec: number;
  uptime: string;
  keyCount: number;
}

interface NodeMetricsCardProps {
  node: NodeMetrics;
}

export function NodeMetricsCard({ node }: NodeMetricsCardProps) {
  // Defensive programming - ensure we have safe defaults for all properties
  const safeNode = {
    nodeId: node?.nodeId || 'unknown',
    nodeAddress: node?.nodeAddress || 'unknown',
    role: node?.role || 'unknown',
    status: node?.status || 'unknown',
    memory: {
      used: node?.memory?.used || '0B',
      max: node?.memory?.max || '0B',
      usedBytes: node?.memory?.usedBytes || 0,
      maxBytes: node?.memory?.maxBytes || 0,
      percent: node?.memory?.percent || 0
    },
    cpu: {
      percent: node?.cpu?.percent || 0,
      cores: node?.cpu?.cores || 0
    },
    connections: node?.connections || 0,
    opsPerSec: node?.opsPerSec || 0,
    uptime: node?.uptime || '0m',
    keyCount: node?.keyCount || 0
  };

  const formatUptime = (uptime: string) => {
    // uptime is already formatted like "2d 14h"
    return uptime || '0m';
  };

  const getStatusColor = (status: string) => {
    if (status === 'online') return 'text-green-500';
    if (status === 'offline') return 'text-red-500';
    return 'text-yellow-500'; // For unknown status
  };

  const getRoleBadgeVariant = (role: string) => {
    if (role === 'primary') return 'default';
    if (role === 'secondary') return 'secondary';
    return 'outline'; // For unknown role
  };

  const getMemoryColor = (percent: number) => {
    if (!percent || isNaN(percent)) return 'text-gray-500';
    if (percent > 80) return 'text-red-600';
    if (percent > 60) return 'text-yellow-600';
    return 'text-green-600';
  };

  const getCpuColor = (percent: number) => {
    if (!percent || isNaN(percent)) return 'text-gray-500';
    if (percent > 80) return 'text-red-600';
    if (percent > 60) return 'text-yellow-600';
    return 'text-blue-600';
  };

  return (
    <Card className="transition-all duration-200 hover:shadow-md border-l-4 border-l-primary/20">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base flex items-center gap-2">
            <Server className="h-4 w-4" />
            {safeNode.nodeAddress}
          </CardTitle>
          <div className="flex items-center gap-2">
            <Badge variant={getRoleBadgeVariant(safeNode.role)} className="text-xs">
              {safeNode.role.toUpperCase()}
            </Badge>
            <div className="flex items-center gap-1">
              <Circle className={`h-2 w-2 fill-current ${getStatusColor(safeNode.status)}`} />
              <span className={`text-xs font-medium ${getStatusColor(safeNode.status)}`}>
                {safeNode.status.toUpperCase()}
              </span>
            </div>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">
          Uptime: {formatUptime(safeNode.uptime)} • {safeNode.keyCount.toLocaleString()} keys
        </p>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          {/* Memory Usage */}
          <div className="flex items-center space-x-2">
            <HardDrive className="h-4 w-4 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <p className="text-xs font-medium text-muted-foreground">Memory</p>
              <p className={`text-sm font-bold ${getMemoryColor(safeNode.memory.percent)}`}>
                {safeNode.memory.percent}%
              </p>
              <p className="text-xs text-muted-foreground truncate">
                {safeNode.memory.used}/{safeNode.memory.max}
              </p>
            </div>
          </div>

          {/* CPU Usage */}
          <div className="flex items-center space-x-2">
            <Cpu className="h-4 w-4 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <p className="text-xs font-medium text-muted-foreground">CPU</p>
              <p className={`text-sm font-bold ${getCpuColor(safeNode.cpu.percent)}`}>
                {safeNode.cpu.percent.toFixed(1)}%
              </p>
              <p className="text-xs text-muted-foreground">
                {safeNode.cpu.cores} cores
              </p>
            </div>
          </div>

          {/* Connections */}
          <div className="flex items-center space-x-2">
            <Users className="h-4 w-4 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <p className="text-xs font-medium text-muted-foreground">Connections</p>
              <p className="text-sm font-bold text-purple-600">
                {safeNode.connections.toLocaleString()}
              </p>
              <p className="text-xs text-muted-foreground">active</p>
            </div>
          </div>

          {/* Operations per Second */}
          <div className="flex items-center space-x-2">
            <Zap className="h-4 w-4 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <p className="text-xs font-medium text-muted-foreground">Ops/sec</p>
              <p className="text-sm font-bold text-orange-600">
                {safeNode.opsPerSec.toLocaleString()}
              </p>
              <p className="text-xs text-muted-foreground">current</p>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
