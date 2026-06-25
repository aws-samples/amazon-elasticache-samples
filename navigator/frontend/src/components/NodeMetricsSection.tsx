import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { 
  ChevronDown,
  ChevronUp,
  Server,
  RefreshCw
} from 'lucide-react';
import { NodeMetricsCard, type NodeMetrics } from './NodeMetricsCard';
import { useState } from 'react';

interface NodeMetricsSectionProps {
  nodes: NodeMetrics[];
  isLoading?: boolean;
  onRefresh?: () => void;
}

export function NodeMetricsSection({ nodes, isLoading = false, onRefresh }: NodeMetricsSectionProps) {
  const [isExpanded, setIsExpanded] = useState(true);

  // Defensive programming - ensure nodes is always an array
  const safeNodes = Array.isArray(nodes) ? nodes : [];

  // Debug logging to identify duplicate issues
  console.log('🔍 NodeMetricsSection - Raw nodes:', nodes);
  console.log('🔍 NodeMetricsSection - Safe nodes count:', safeNodes.length);
  if (safeNodes.length > 0) {
    console.log('🔍 NodeMetricsSection - Node IDs:', safeNodes.map(n => n?.nodeId || 'undefined'));
    console.log('🔍 NodeMetricsSection - Node addresses:', safeNodes.map(n => n?.nodeAddress || 'undefined'));
    
    // Check for duplicates
    const nodeIds = safeNodes.map(n => n?.nodeId).filter(Boolean);
    const uniqueNodeIds = [...new Set(nodeIds)];
    if (nodeIds.length !== uniqueNodeIds.length) {
      console.warn('⚠️ Duplicate node IDs detected!', { 
        total: nodeIds.length, 
        unique: uniqueNodeIds.length,
        duplicates: nodeIds.filter((id, index) => nodeIds.indexOf(id) !== index)
      });
    }
  }

  const onlineNodes = safeNodes.filter(node => node?.status === 'online');
  const offlineNodes = safeNodes.filter(node => node?.status === 'offline');
  const primaryNodes = safeNodes.filter(node => node?.role === 'primary');
  const secondaryNodes = safeNodes.filter(node => node?.role === 'secondary');

  // Sort nodes so Primary nodes appear first, then Secondary nodes
  const sortedNodes = [...safeNodes].sort((a, b) => {
    // Handle potential null/undefined nodes
    if (!a || !b) return 0;
    
    // Primary nodes first
    if (a.role === 'primary' && b.role === 'secondary') return -1;
    if (a.role === 'secondary' && b.role === 'primary') return 1;
    
    // Within same role, sort by nodeAddress for consistency
    return (a.nodeAddress || '').localeCompare(b.nodeAddress || '');
  });

  const getTotalMetrics = () => {
    if (!nodes || nodes.length === 0) {
      return {
        totalConnections: 0,
        totalOpsPerSec: 0,
        avgCpuUsage: '0.0',
        avgMemoryUsage: '0.0'
      };
    }

    // Filter out null/undefined nodes for calculations
    const validNodes = nodes.filter(node => node != null);
    
    if (validNodes.length === 0) {
      return {
        totalConnections: 0,
        totalOpsPerSec: 0,
        avgCpuUsage: '0.0',
        avgMemoryUsage: '0.0'
      };
    }

    const totalConnections = validNodes.reduce((sum, node) => sum + (node.connections || 0), 0);
    const totalOpsPerSec = validNodes.reduce((sum, node) => sum + (node.opsPerSec || 0), 0);
    const avgCpuUsage = validNodes.reduce((sum, node) => sum + (node.cpu?.percent || 0), 0) / validNodes.length;
    const avgMemoryUsage = validNodes.reduce((sum, node) => sum + (node.memory?.percent || 0), 0) / validNodes.length;
    
    return {
      totalConnections,
      totalOpsPerSec,
      avgCpuUsage: isNaN(avgCpuUsage) ? '0.0' : avgCpuUsage.toFixed(1),
      avgMemoryUsage: isNaN(avgMemoryUsage) ? '0.0' : avgMemoryUsage.toFixed(1)
    };
  };

  const totals = getTotalMetrics();

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Server className="h-5 w-5 text-muted-foreground" />
            <div>
              <CardTitle className="flex items-center gap-2">
                Cluster Nodes
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setIsExpanded(!isExpanded)}
                  className="h-6 w-6 p-0"
                >
                  {isExpanded ? (
                    <ChevronUp className="h-4 w-4" />
                  ) : (
                    <ChevronDown className="h-4 w-4" />
                  )}
                </Button>
              </CardTitle>
              <CardDescription>
                Individual node performance and health metrics
              </CardDescription>
            </div>
          </div>
          <div className="flex items-center gap-3">
              <div className="flex items-center gap-4 text-sm">
                <div className="flex items-center gap-2">
                  <Badge variant="default" className="text-xs">
                    {primaryNodes.length} Primary
                  </Badge>
                  <Badge variant="secondary" className="text-xs">
                    {secondaryNodes.length} Secondary
                  </Badge>
                </div>
              <div className="flex items-center gap-1">
                <div className="h-2 w-2 bg-green-500 rounded-full"></div>
                <span className="text-green-600 font-medium">{onlineNodes.length} Online</span>
                {offlineNodes.length > 0 && (
                  <>
                    <div className="h-2 w-2 bg-red-500 rounded-full ml-2"></div>
                    <span className="text-red-600 font-medium">{offlineNodes.length} Offline</span>
                  </>
                )}
              </div>
            </div>
            <Button 
              variant="outline" 
              size="sm"
              onClick={onRefresh}
              disabled={isLoading}
            >
              <RefreshCw className={`h-4 w-4 mr-1 ${isLoading ? 'animate-spin' : ''}`} />
              Refresh
            </Button>
          </div>
        </div>
      </CardHeader>
      {isExpanded && (
        <CardContent className="space-y-4">
          {/* Quick Summary */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 p-4 bg-muted/30 rounded-lg">
            <div className="text-center">
              <p className="text-lg font-bold text-purple-600">{totals.totalConnections.toLocaleString()}</p>
              <p className="text-xs text-muted-foreground">Total Connections</p>
            </div>
            <div className="text-center">
              <p className="text-lg font-bold text-orange-600">{totals.totalOpsPerSec.toLocaleString()}</p>
              <p className="text-xs text-muted-foreground">Total Ops/sec</p>
            </div>
            <div className="text-center">
              <p className="text-lg font-bold text-blue-600">{totals.avgCpuUsage}%</p>
              <p className="text-xs text-muted-foreground">Avg CPU Usage</p>
            </div>
            <div className="text-center">
              <p className="text-lg font-bold text-green-600">{totals.avgMemoryUsage}%</p>
              <p className="text-xs text-muted-foreground">Avg Memory Usage</p>
            </div>
          </div>

          {/* Node Cards */}
          <div className="space-y-3">
            {sortedNodes.length > 0 ? (
              sortedNodes.map((node) => (
                <NodeMetricsCard key={node.nodeId} node={node} />
              ))
            ) : (
              <div className="text-center py-8">
                <Server className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
                <h3 className="text-lg font-semibold mb-2">No Node Data Available</h3>
                <p className="text-muted-foreground mb-4">
                  {isLoading 
                    ? 'Loading cluster nodes...' 
                    : 'Unable to retrieve cluster node metrics. This could mean:'
                  }
                </p>
                {!isLoading && (
                  <ul className="text-sm text-muted-foreground mb-4 text-left max-w-md mx-auto">
                    <li>• The Valkey instance is running in single-node mode</li>
                    <li>• The cluster nodes API endpoint is not available</li>
                    <li>• There was an error fetching node metrics</li>
                    <li>• The backend cluster detection needs configuration</li>
                  </ul>
                )}
                <Button 
                  variant="outline" 
                  onClick={onRefresh}
                  disabled={isLoading}
                  className="mt-2"
                >
                  <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
                  {isLoading ? 'Loading...' : 'Try Again'}
                </Button>
              </div>
            )}
          </div>
        </CardContent>
      )}
    </Card>
  );
}
