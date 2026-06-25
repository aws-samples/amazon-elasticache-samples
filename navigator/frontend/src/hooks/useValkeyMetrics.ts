import { useState, useEffect, useCallback, useRef } from 'react';
import { valkeyApi } from '@/services/valkeyApi';
import { useConnection } from '@/contexts/ConnectionContext';
import type { 
  ValkeyAllMetrics,
    // NORBERT 9-10 //  ValkeyNodeMetrics,
  ClusterSlotStatsResponse
} from '@/services/valkeyApi';

// Raw API response interface that matches your actual API response
interface RawValkeyNodeMetrics {
  nodeId: string;
  nodeAddress: string;
  role: 'master' | 'slave';
  available?: boolean;
  status?: 'online' | 'offline';
  slots?: {
    count: number;
    ranges: Array<{ start: number; end: number }>;
    raw: string;
  };
  memory?: {
    used_memory: number;
    used_memory_human: string;
    used_memory_rss: number;
    used_memory_peak: number;
    used_memory_peak_human: string;
    mem_fragmentation_ratio: number;
  };
  cpu?: {
    used_cpu_sys: number;
    used_cpu_user: number;
    used_cpu_sys_children: number;
    used_cpu_user_children: number;
  };
  connections?: {
    connected_clients: number;
    blocked_clients: number;
    tracking_clients: number;
    total_connections_received: number;
    rejected_connections: number;
  };
  keyspace?: {
    [key: string]: {
      keys: number;
      expires: number;
      avg_ttl: number;
    };
  };
  commands?: {
    total_commands_processed: number;
    instantaneous_ops_per_sec: number;
  };
  replication?: any;
  server_info?: {
    redis_version: string;
    redis_mode: string;
    os: string;
    arch_bits: number;
    uptime_in_seconds: number;
    uptime_in_days: number;
  };
  flags?: string[];
  masterNodeId?: string;
}
import type { NodeMetrics } from '@/components/NodeMetricsCard';
import type { 
  MetricCategory, 
  MetricCollectionState, 
  CollectionInfo,
  CategoryPerformanceHistory,
  CollectionPerformanceMetrics 
} from '@/types';

interface UseValkeyMetricsResult {
  data: ValkeyAllMetrics | null;
  isLoading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  lastUpdated: Date | null;
}

interface UseValkeyMetricsOptions {
  autoRefresh?: boolean;
  refreshInterval?: number; // in milliseconds
  fetchOnMount?: boolean;
}

export function useValkeyMetrics(options: UseValkeyMetricsOptions = {}): UseValkeyMetricsResult {
  const {
    autoRefresh = false,
    refreshInterval = 30000, // 30 seconds default
    fetchOnMount = true
  } = options;

  const { activeConnection } = useConnection();
  const [data, setData] = useState<ValkeyAllMetrics | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  // Update API service connection whenever active connection changes
  useEffect(() => {
    if (activeConnection) {
      console.log('🔗 useValkeyMetrics: Updating API service with new connection:', activeConnection.name);
      valkeyApi.setConnection(activeConnection);
    }
  }, [activeConnection]);

  const fetchMetrics = useCallback(async () => {
    if (!activeConnection) {
      console.warn('⚠️ No active connection available for metrics fetch');
      setError('No active connection available');
      return;
    }

    try {
      setIsLoading(true);
      setError(null);
      
      // Ensure API service has the latest connection
      valkeyApi.setConnection(activeConnection);
      
      const metrics = await valkeyApi.getAllMetrics();
      setData(metrics);
      setLastUpdated(new Date());
    } catch (err) {
      console.error('Error fetching Valkey metrics:', err);
      setError(err instanceof Error ? err.message : 'Failed to fetch metrics');
    } finally {
      setIsLoading(false);
    }
  }, [activeConnection]);

  const refresh = useCallback(async () => {
    await fetchMetrics();
  }, [fetchMetrics]);

  // Initial fetch
  useEffect(() => {
    if (fetchOnMount) {
      fetchMetrics();
    }
  }, [fetchOnMount]); // Removed fetchMetrics to prevent infinite re-renders

  // Auto-refresh setup
  useEffect(() => {
    if (!autoRefresh) return;

    const interval = setInterval(() => {
      fetchMetrics();
    }, refreshInterval);

    return () => clearInterval(interval);
  }, [autoRefresh, refreshInterval]); // Removed fetchMetrics to prevent infinite re-renders

  return {
    data,
    isLoading,
    error,
    refresh,
    lastUpdated
  };
}

// Enhanced hook with parallel processing support
interface UseEnhancedValkeyMetricsResult {
  data: ValkeyAllMetrics | null;
  collectionInfo: CollectionInfo | null;
  categoryStates: MetricCollectionState;
  partialResults: boolean;
  failedCategories: string[];
  isLoading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  retryCategory: (category: MetricCategory) => Promise<void>;
  retryFailedCategories: () => Promise<void>;
  lastUpdated: Date | null;
  performanceMetrics: CollectionPerformanceMetrics | null;
}

interface UseEnhancedValkeyMetricsOptions {
  autoRefresh?: boolean;
  refreshInterval?: number;
  fetchOnMount?: boolean;
  trackPerformance?: boolean;
}

export function useEnhancedValkeyMetrics(options: UseEnhancedValkeyMetricsOptions = {}): UseEnhancedValkeyMetricsResult {
  const {
    autoRefresh = false,
    refreshInterval = 30000,
    fetchOnMount = true,
    trackPerformance = true
  } = options;

  const { activeConnection } = useConnection();
  const [data, setData] = useState<ValkeyAllMetrics | null>(null);
  const [collectionInfo, setCollectionInfo] = useState<CollectionInfo | null>(null);
  const [categoryStates, setCategoryStates] = useState<MetricCollectionState>({
    server: { status: 'pending' },
    memory: { status: 'pending' },
    connections: { status: 'pending' },
    commands: { status: 'pending' },
    cluster: { status: 'pending' },
    performance: { status: 'pending' },
    keyspace: { status: 'pending' }
  });
  const [partialResults, setPartialResults] = useState(false);
  const [failedCategories, setFailedCategories] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [performanceMetrics, setPerformanceMetrics] = useState<CollectionPerformanceMetrics | null>(null);
  
  // Performance tracking
  const performanceHistoryRef = useRef<CategoryPerformanceHistory[]>([]);
  const collectionHistoryRef = useRef<Array<{
    timestamp: Date;
    totalDuration: number;
    successfulTasks: number;
    totalTasks: number;
  }>>([]);

  const updateCategoryStates = useCallback((taskTimings: { [key: string]: string }, failedCats: string[]) => {
    const newStates: MetricCollectionState = { ...categoryStates };
    
    // Reset all to pending first
    Object.keys(newStates).forEach(category => {
      newStates[category as MetricCategory] = { status: 'pending' };
    });

    // Update based on task timings
    Object.entries(taskTimings).forEach(([category, timing]) => {
      const cat = category as MetricCategory;
      if (timing.includes('timeout')) {
        newStates[cat] = { 
          status: 'timeout', 
          timing, 
          error: 'Request timed out',
          lastAttempt: new Date()
        };
      } else if (failedCats.includes(category)) {
        newStates[cat] = { 
          status: 'failed', 
          timing, 
          error: 'Collection failed',
          lastAttempt: new Date()
        };
      } else {
        newStates[cat] = { 
          status: 'complete', 
          timing,
          lastAttempt: new Date()
        };
      }
    });

    setCategoryStates(newStates);
  }, [categoryStates]);

  function wait(ms: number){
        var start = new Date().getTime();
        var end = start;
        while(end < start + ms) {
            end = new Date().getTime();
        }
  }

  const updatePerformanceMetrics = useCallback((collectionInfo: CollectionInfo) => {
    if (!trackPerformance) return;

    // Update collection history
    collectionHistoryRef.current.push({
      timestamp: new Date(),
      totalDuration: collectionInfo.total_duration_seconds,
      successfulTasks: collectionInfo.successful_tasks,
      totalTasks: collectionInfo.total_tasks
    });

    // Keep only last 50 collections
    if (collectionHistoryRef.current.length > 50) {
      collectionHistoryRef.current = collectionHistoryRef.current.slice(-50);
    }

    // Update category performance history
    Object.entries(collectionInfo.task_timings).forEach(([category, timing]) => {
      const cat = category as MetricCategory;
      const duration = parseFloat(timing.replace('s', ''));
      const status = timing.includes('timeout') ? 'timeout' as const : 
                    timing.includes('failed') ? 'failed' as const : 'success' as const;

      let categoryHistory = performanceHistoryRef.current.find(h => h.category === cat);
      if (!categoryHistory) {
        categoryHistory = {
          category: cat,
          timings: [],
          averageDuration: 0,
          successRate: 0
        };
        performanceHistoryRef.current.push(categoryHistory);
      }

      categoryHistory.timings.push({
        timestamp: new Date(),
        duration: isNaN(duration) ? 0 : duration,
        status
      });

      // Keep only last 20 timings per category
      if (categoryHistory.timings.length > 20) {
        categoryHistory.timings = categoryHistory.timings.slice(-20);
      }

      // Recalculate averages
      const successfulTimings = categoryHistory.timings.filter(t => t.status === 'success');
      categoryHistory.averageDuration = successfulTimings.length > 0 
        ? successfulTimings.reduce((sum, t) => sum + t.duration, 0) / successfulTimings.length 
        : 0;
      categoryHistory.successRate = (successfulTimings.length / categoryHistory.timings.length) * 100;
    });

    // Update performance metrics state
    const avgCollectionTime = collectionHistoryRef.current.length > 0 
      ? collectionHistoryRef.current.reduce((sum, c) => sum + c.totalDuration, 0) / collectionHistoryRef.current.length 
      : 0;

    setPerformanceMetrics({
      totalCollections: collectionHistoryRef.current.length,
      averageCollectionTime: avgCollectionTime,
      categoryPerformance: [...performanceHistoryRef.current],
      recentCollections: [...collectionHistoryRef.current]
    });
  }, [trackPerformance]);

  // Update API service connection whenever active connection changes
  useEffect(() => {
    if (activeConnection) {
      console.log('🔗 useEnhancedValkeyMetrics: Updating API service with new connection:', activeConnection.name);
      valkeyApi.setConnection(activeConnection);
    }
  }, [activeConnection]);

  const fetchEnhancedMetrics = useCallback(async () => {
    if (!activeConnection) {
      console.warn('⚠️ No active connection available for enhanced metrics fetch');
      setError('No active connection available');
    //  return;
      await wait(5000);
      console.log("Waited 5s");
    }

    if (!activeConnection) {
      console.warn('⚠️ No active connection available for enhanced metrics fetch');
      setError('No active connection available');
      return;
    }

      try {
      setIsLoading(true);
      setError(null);
      
      // Ensure API service has the latest connection
      valkeyApi.setConnection(activeConnection);
      
      // Set all categories to loading
      setCategoryStates(prev => {
        const newStates = { ...prev };
        Object.keys(newStates).forEach(category => {
          newStates[category as MetricCategory] = { 
            ...newStates[category as MetricCategory],
            status: 'loading' 
          };
        });
        return newStates;
      });

      const enhanced = await valkeyApi.getEnhancedMetrics();
      
      setData(enhanced.metrics);
      setCollectionInfo(enhanced.collection_info || null);
      setPartialResults(enhanced.partial_results || false);
      setFailedCategories(enhanced.failed_categories || []);
      setLastUpdated(new Date());

      // Update category states based on collection info
      if (enhanced.collection_info) {
        updateCategoryStates(enhanced.collection_info.task_timings, enhanced.failed_categories || []);
        updatePerformanceMetrics(enhanced.collection_info);
      }
      
    } catch (err) {
      console.error('Error fetching enhanced Valkey metrics:', err);
      setError(err instanceof Error ? err.message : 'Failed to fetch metrics');
      
      // Mark all categories as failed
      setCategoryStates(prev => {
        const newStates = { ...prev };
        Object.keys(newStates).forEach(category => {
          newStates[category as MetricCategory] = { 
            status: 'failed', 
            error: err instanceof Error ? err.message : 'Unknown error',
            lastAttempt: new Date()
          };
        });
        return newStates;
      });
    } finally {
      setIsLoading(false);
    }
  }, [activeConnection, updateCategoryStates, updatePerformanceMetrics]);

  const refresh = useCallback(async () => {
    await fetchEnhancedMetrics();
  }, [fetchEnhancedMetrics]);

  const retryCategory = useCallback(async (category: MetricCategory) => {
    try {
      console.log(`🔄 Retrying category: ${category}`);
      
      // Mark specific category as loading
      setCategoryStates(prev => ({
        ...prev,
        [category]: { 
          ...prev[category],
          status: 'loading',
          retryCount: (prev[category].retryCount || 0) + 1
        }
      }));

      const enhanced = await valkeyApi.retryMetricCategories([category]);
      
      if (enhanced.collection_info) {
        updateCategoryStates(enhanced.collection_info.task_timings, enhanced.failed_categories || []);
        updatePerformanceMetrics(enhanced.collection_info);
      }
      
      // Update metrics if successful
      if (enhanced.metrics) {
        setData(enhanced.metrics);
        setLastUpdated(new Date());
      }
      
    } catch (err) {
      console.error(`Error retrying category ${category}:`, err);
      setCategoryStates(prev => ({
        ...prev,
        [category]: { 
          ...prev[category],
          status: 'failed',
          error: err instanceof Error ? err.message : 'Retry failed',
          lastAttempt: new Date()
        }
      }));
    }
  }, [updateCategoryStates, updatePerformanceMetrics]);

  const retryFailedCategories = useCallback(async () => {
    const failedCats = Object.entries(categoryStates)
      .filter(([_, state]) => state.status === 'failed' || state.status === 'timeout')
      .map(([category, _]) => category);

    if (failedCats.length === 0) return;

    try {
      console.log(`🔄 Retrying failed categories: ${failedCats.join(', ')}`);
      
      // Mark failed categories as loading
      setCategoryStates(prev => {
        const newStates = { ...prev };
        failedCats.forEach(category => {
          newStates[category as MetricCategory] = {
            ...newStates[category as MetricCategory],
            status: 'loading',
            retryCount: (newStates[category as MetricCategory].retryCount || 0) + 1
          };
        });
        return newStates;
      });

      const enhanced = await valkeyApi.retryMetricCategories(failedCats);
      
      if (enhanced.collection_info) {
        updateCategoryStates(enhanced.collection_info.task_timings, enhanced.failed_categories || []);
        updatePerformanceMetrics(enhanced.collection_info);
      }
      
      // Update metrics if successful
      if (enhanced.metrics) {
        setData(enhanced.metrics);
        setLastUpdated(new Date());
      }
      
    } catch (err) {
      console.error('Error retrying failed categories:', err);
      // Mark retried categories as failed again
      setCategoryStates(prev => {
        const newStates = { ...prev };
        failedCats.forEach(category => {
          newStates[category as MetricCategory] = {
            ...newStates[category as MetricCategory],
            status: 'failed',
            error: err instanceof Error ? err.message : 'Retry failed',
            lastAttempt: new Date()
          };
        });
        return newStates;
      });
    }
  }, [categoryStates, updateCategoryStates, updatePerformanceMetrics]);

  // Initial fetch
  useEffect(() => {
    if (fetchOnMount) {
      fetchEnhancedMetrics();
    }
  }, [fetchOnMount]); // Removed fetchEnhancedMetrics to prevent infinite re-renders

  // Auto-refresh setup
  useEffect(() => {
    if (!autoRefresh) return;

    const interval = setInterval(() => {
      fetchEnhancedMetrics();
    }, refreshInterval);

    return () => clearInterval(interval);
  }, [autoRefresh, refreshInterval]); // Removed fetchEnhancedMetrics to prevent infinite re-renders

  return {
    data,
    collectionInfo,
    categoryStates,
    partialResults,
    failedCategories,
    isLoading,
    error,
    refresh,
    retryCategory,
    retryFailedCategories,
    lastUpdated,
    performanceMetrics
  };
}

// Hook for individual metric endpoints
interface UseValkeySpecificMetricResult<T> {
  data: T | null;
  isLoading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  lastUpdated: Date | null;
}

export function useValkeyHealth(): UseValkeySpecificMetricResult<{ status: string; timestamp: string }> {
  const { activeConnection } = useConnection();
  const [data, setData] = useState<{ status: string; timestamp: string } | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  // Update API service connection whenever active connection changes
  useEffect(() => {
    if (activeConnection) {
      valkeyApi.setConnection(activeConnection);
    }
  }, [activeConnection]);

  const fetchHealth = useCallback(async () => {
    if (!activeConnection) {
      console.warn('⚠️ No active connection available for health check');
      setError('No active connection available');
      return;
    }

    try {
      setIsLoading(true);
      setError(null);
      
      // Ensure API service has the latest connection
      valkeyApi.setConnection(activeConnection);
      
      const health = await valkeyApi.getHealth();
      setData(health);
      setLastUpdated(new Date());
    } catch (err) {
      console.error('Error fetching Valkey health:', err);
      setError(err instanceof Error ? err.message : 'Failed to fetch health status');
    } finally {
      setIsLoading(false);
    }
  }, [activeConnection]);

  const refresh = useCallback(async () => {
    await fetchHealth();
  }, [fetchHealth]);

  useEffect(() => {
    fetchHealth();
  }, []); // Removed fetchHealth dependency to prevent continuous re-calls

  return {
    data,
    isLoading,
    error,
    refresh,
    lastUpdated
  };
}

// Hook for cache operations
interface UseCacheOperationsResult {
  keys: string[] | null;
  isLoadingKeys: boolean;
  keysError: string | null;
  refreshKeys: () => Promise<void>;
  getKey: (key: string) => Promise<any>;
  setKey: (key: string, value: any, ttl?: number) => Promise<void>;
  deleteKey: (key: string) => Promise<void>;
  isOperating: boolean;
  operationError: string | null;
}

export function useCacheOperations(): UseCacheOperationsResult {
  const { activeConnection } = useConnection();
  const [keys, setKeys] = useState<string[] | null>(null);
  const [isLoadingKeys, setIsLoadingKeys] = useState(false);
  const [keysError, setKeysError] = useState<string | null>(null);
  const [isOperating, setIsOperating] = useState(false);
  const [operationError, setOperationError] = useState<string | null>(null);

  // Update API service connection whenever active connection changes
  useEffect(() => {
    if (activeConnection) {
      valkeyApi.setConnection(activeConnection);
    }
  }, [activeConnection]);

  const fetchKeys = useCallback(async () => {
    if (!activeConnection) {
      console.warn('⚠️ No active connection available for cache operations');
      setKeysError('No active connection available');
      return;
    }

    try {
      setIsLoadingKeys(true);
      setKeysError(null);
      
      // Ensure API service has the latest connection
      valkeyApi.setConnection(activeConnection);
      
      const cacheKeysResponse = await valkeyApi.getCacheKeys();
      
      // Handle API response format: {pattern: "*", keys: [...], count: 0}
      if (cacheKeysResponse && typeof cacheKeysResponse === 'object' && 'keys' in cacheKeysResponse) {
        setKeys((cacheKeysResponse as any).keys || []);
      } else if (Array.isArray(cacheKeysResponse)) {
        // Fallback if it's already an array
        setKeys(cacheKeysResponse);
      } else {
        setKeys([]);
      }
    } catch (err) {
      console.error('Error fetching cache keys:', err);
      setKeysError(err instanceof Error ? err.message : 'Failed to fetch cache keys');
    } finally {
      setIsLoadingKeys(false);
    }
  }, [activeConnection]);

  const refreshKeys = useCallback(async () => {
    await fetchKeys();
  }, [fetchKeys]);

  const getKey = useCallback(async (key: string) => {
    try {
      setOperationError(null);
      return await valkeyApi.getCacheKey(key);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to get cache key';
      setOperationError(errorMessage);
      throw err;
    }
  }, []);

  const setKey = useCallback(async (key: string, value: any, ttl?: number) => {
    try {
      setIsOperating(true);
      setOperationError(null);
      
      await valkeyApi.setCacheKey(key, value, ttl);
      // Refresh keys list to include new key
      await fetchKeys();
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to set cache key';
      setOperationError(errorMessage);
      throw err;
    } finally {
      setIsOperating(false);
    }
  }, [fetchKeys]);

  const deleteKey = useCallback(async (key: string) => {
    try {
      setIsOperating(true);
      setOperationError(null);
      
      await valkeyApi.deleteCacheKey(key);
      // Refresh keys list to remove deleted key
      await fetchKeys();
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to delete cache key';
      setOperationError(errorMessage);
      throw err;
    } finally {
      setIsOperating(false);
    }
  }, [fetchKeys]);

  useEffect(() => {
    fetchKeys();
  }, []); // Removed fetchKeys to prevent infinite re-renders

  return {
    keys,
    isLoadingKeys,
    keysError,
    refreshKeys,
    getKey,
    setKey,
    deleteKey,
    isOperating,
    operationError
  };
}

// Hook for node metrics
interface UseNodeMetricsResult {
  data: NodeMetrics[] | null;
  clusterInfo: any | null;
  isLoading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  lastUpdated: Date | null;
}

interface UseNodeMetricsOptions {
  autoRefresh?: boolean;
  refreshInterval?: number; // in milliseconds
  fetchOnMount?: boolean;
}

export function useNodeMetrics(options: UseNodeMetricsOptions = {}): UseNodeMetricsResult {
  const {
    autoRefresh = false,
    refreshInterval = 30000, // 30 seconds default
    fetchOnMount = true
  } = options;

  const [data, setData] = useState<NodeMetrics[] | null>(null);
  const [clusterInfo, setClusterInfo] = useState<any | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  // Transform raw API response to NodeMetrics format
  const transformNodeMetrics = useCallback((rawNodes: RawValkeyNodeMetrics[]): NodeMetrics[] => {
    return rawNodes.map(node => {
      console.log('🔄 Transforming node:', node.nodeId, node);
      
      // Determine status from available boolean or other indicators
      let status: 'online' | 'offline' = 'offline';
      
      if (node.available !== undefined) {
        // Use the available boolean from API
        status = node.available ? 'online' : 'offline';
      } else if (node.status === 'online' || node.status === 'offline') {
        // Use the provided status if it's valid
        status = node.status;
      } else {
        // Try to determine status from other indicators
        if (node.flags && Array.isArray(node.flags)) {
          // Check Redis cluster node flags
          const hasFailFlags = node.flags.some(flag => 
            flag.includes('fail') || flag.includes('noaddr') || flag.includes('disconnected')
          );
          status = hasFailFlags ? 'offline' : 'online';
        } else if (node.server_info && node.server_info.uptime_in_seconds > 0) {
          // If there's uptime data, assume online
          status = 'online';
        }
        // Otherwise defaults to 'offline'
      }

      // Extract connection count from connections object
      const connections = node.connections?.connected_clients || 0;

      // Extract and transform memory data
      const memory = node.memory ? {
        used: node.memory.used_memory_human || '0B',
        max: node.memory.used_memory_peak_human || '0B', // Use peak as max fallback
        usedBytes: node.memory.used_memory || 0,
        maxBytes: node.memory.used_memory_peak || 0,
        percent: node.memory.used_memory && node.memory.used_memory_peak 
          ? Math.round(((node.memory.used_memory / node.memory.used_memory_peak) * 100) * 100) / 100
          : 0
      } : {
        used: '0B',
        max: '0B',
        usedBytes: 0,
        maxBytes: 0,
        percent: 0
      };

      // Calculate CPU percentage from CPU usage data
      // Redis/Valkey CPU usage is cumulative, so we use a simple approximation
      const cpu = node.cpu ? {
        percent: Math.min(100, Math.max(0, 
          // Simple approximation: use system CPU if available, otherwise derive from total usage
          (node.cpu.used_cpu_sys + node.cpu.used_cpu_user) / 10000 || 0
        )),
        cores: 1 // Default to 1 core since this info isn't in the API response
      } : {
        percent: 0,
        cores: 1
      };

      // Extract operations per second from commands object
      const opsPerSec = node.commands?.instantaneous_ops_per_sec || 0;

      // Format uptime from seconds to readable format
      const formatUptime = (seconds: number): string => {
        if (!seconds || seconds === 0) return '0m';
        
        const days = Math.floor(seconds / 86400);
        const hours = Math.floor((seconds % 86400) / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        
        if (days > 0) return `${days}d ${hours}h`;
        if (hours > 0) return `${hours}h ${minutes}m`;
        return `${minutes}m`;
      };

      const uptime = node.server_info?.uptime_in_seconds 
        ? formatUptime(node.server_info.uptime_in_seconds)
        : '0m';

      // Calculate total key count from keyspace databases
      const keyCount = node.keyspace ? 
        Object.values(node.keyspace).reduce((total, db: any) => {
          return total + (db?.keys || 0);
        }, 0) : 0;

      // Map role from master/slave to primary/secondary
      const role = node.role === 'master' ? 'primary' : 'secondary' as 'primary' | 'secondary';

      const transformedNode: NodeMetrics = {
        nodeId: node.nodeId || 'unknown',
        nodeAddress: node.nodeAddress || 'unknown',
        role,
        status,
        memory,
        cpu,
        connections,
        opsPerSec,
        uptime,
        keyCount
      };

      console.log('✅ Transformed node result:', transformedNode);
      return transformedNode;
    });
  }, []);

  const fetchNodeMetrics = useCallback(async () => {
    try {
      setIsLoading(true);
      setError(null);
      
      console.log('🔍 Fetching node metrics from API...');
      
      // Add timeout protection for the API call
      const timeoutController = new AbortController();
      const timeoutId = setTimeout(() => {
        timeoutController.abort();
        console.warn('⏰ Node metrics API call timed out after 15 seconds');
      }, 15000); // 15 second timeout
      
      try {
        const response = await valkeyApi.getAllNodeMetrics();
        clearTimeout(timeoutId);
        
        console.log('📊 Raw node metrics response:', response);
        console.log('📊 Response nodes array:', response.nodes);
        console.log('📊 Response cluster info:', response.clusterInfo);
        
        // Defensive programming - ensure response has expected structure
        if (!response || typeof response !== 'object') {
          throw new Error('Invalid response format from node metrics API');
        }
        
        const nodes = Array.isArray(response.nodes) ? response.nodes : [];
        console.log('🔍 Raw nodes from API before transformation:', nodes);
        const transformedNodes = transformNodeMetrics(nodes as unknown as RawValkeyNodeMetrics[]);
        console.log('🔄 Transformed nodes after transformation:', transformedNodes);
        
        // Check for duplicates in raw data
        if (nodes.length > 0) {
          const nodeIds = nodes.map(n => n?.nodeId).filter(Boolean);
          const uniqueNodeIds = [...new Set(nodeIds)];
          if (nodeIds.length !== uniqueNodeIds.length) {
            console.warn('⚠️ Duplicate node IDs found in RAW API response!', { 
              total: nodeIds.length, 
              unique: uniqueNodeIds.length,
              allIds: nodeIds,
              duplicates: nodeIds.filter((id, index) => nodeIds.indexOf(id) !== index)
            });
          }
        }
        
        setData(transformedNodes);
        setClusterInfo(response.clusterInfo || null);
        setLastUpdated(new Date());
        
        if (transformedNodes.length === 0) {
          console.warn('⚠️ No node metrics found in response');
        } else {
          console.log(`✅ Successfully loaded ${transformedNodes.length} node metrics`);
        }
      } catch (timeoutError) {
        clearTimeout(timeoutId);
        if (timeoutError instanceof Error && timeoutError.name === 'AbortError') {
          throw new Error('Node metrics request timed out - this may indicate backend performance issues');
        }
        throw timeoutError;
      }
    } catch (err) {
      console.error('❌ Error fetching node metrics:', err);
      console.error('❌ Error details:', {
        message: err instanceof Error ? err.message : String(err),
        stack: err instanceof Error ? err.stack : undefined
      });
      
      // Provide more specific error messages for different scenarios
      let errorMessage = 'Failed to fetch node metrics';
      if (err instanceof Error) {
        if (err.message.includes('timeout')) {
          errorMessage = 'Node metrics request timed out - backend may be slow or unresponsive';
        } else if (err.message.includes('network') || err.message.includes('fetch')) {
          errorMessage = 'Network error while fetching node metrics - check backend connectivity';
        } else if (err.message.includes('404')) {
          errorMessage = 'Node metrics endpoint not found - backend may not support cluster operations';
        } else if (err.message.includes('500')) {
          errorMessage = 'Backend error while fetching node metrics - check server logs';
        } else {
          errorMessage = `Node metrics error: ${err.message}`;
        }
      }
      
      setError(errorMessage);
      
      // Don't let node metrics errors crash the entire page
      // Set empty data instead of leaving it in an undefined state
      setData([]);
      setClusterInfo(null);
    } finally {
      setIsLoading(false);
    }
  }, [transformNodeMetrics]);

  const refresh = useCallback(async () => {
    await fetchNodeMetrics();
  }, [fetchNodeMetrics]);

  // Initial fetch
  useEffect(() => {
    if (fetchOnMount) {
      fetchNodeMetrics();
    }
  }, [fetchOnMount]); // Removed fetchNodeMetrics to prevent infinite re-renders

  // Auto-refresh setup
  useEffect(() => {
    if (!autoRefresh) return;

    const interval = setInterval(() => {
      fetchNodeMetrics();
    }, refreshInterval);

    return () => clearInterval(interval);
  }, [autoRefresh, refreshInterval]); // Removed fetchNodeMetrics to prevent infinite re-renders

  return {
    data,
    clusterInfo,
    isLoading,
    error,
    refresh,
    lastUpdated
  };
}

// Hook for cluster slot stats
interface UseSlotStatsResult {
  data: ClusterSlotStatsResponse | null;
  isLoading: boolean;
  error: string | null;
  refresh: (startSlot?: number, endSlot?: number) => Promise<void>;
  lastUpdated: Date | null;
  startSlot: number;
  endSlot: number;
  setSlotRange: (start: number, end: number) => void;
}

interface UseSlotStatsOptions {
  autoRefresh?: boolean;
  refreshInterval?: number; // in milliseconds
  fetchOnMount?: boolean;
  initialStartSlot?: number;
  initialEndSlot?: number;
}

export function useSlotStats(options: UseSlotStatsOptions = {}): UseSlotStatsResult {
  const {
    autoRefresh = false,
    refreshInterval = 30000, // 30 seconds default
    fetchOnMount = true,
    initialStartSlot = 0,
    initialEndSlot = 1000
  } = options;

  const [data, setData] = useState<ClusterSlotStatsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [startSlot, setStartSlot] = useState(initialStartSlot);
  const [endSlot, setEndSlot] = useState(initialEndSlot);

  const fetchSlotStats = useCallback(async (start?: number, end?: number) => {
    const actualStart = start ?? startSlot;
    const actualEnd = end ?? endSlot;
    
    try {
      setIsLoading(true);
      setError(null);
      
      const response = await valkeyApi.getClusterSlotStats(actualStart, actualEnd);
      setData(response);
      setLastUpdated(new Date());
    } catch (err) {
      console.error('Error fetching slot stats:', err);
      setError(err instanceof Error ? err.message : 'Failed to fetch slot stats');
    } finally {
      setIsLoading(false);
    }
  }, [startSlot, endSlot]);

  const refresh = useCallback(async (start?: number, end?: number) => {
    await fetchSlotStats(start, end);
  }, [fetchSlotStats]);

  const setSlotRange = useCallback((start: number, end: number) => {
    setStartSlot(start);
    setEndSlot(end);
  }, []);

  // Initial fetch
  useEffect(() => {
    if (fetchOnMount) {
      fetchSlotStats();
    }
  }, [fetchOnMount]); // Removed fetchSlotStats to prevent infinite re-renders

  // Auto-refresh setup
  useEffect(() => {
    if (!autoRefresh) return;

    const interval = setInterval(() => {
      fetchSlotStats();
    }, refreshInterval);

    return () => clearInterval(interval);
  }, [autoRefresh, refreshInterval]); // Removed fetchSlotStats to prevent infinite re-renders

  // Remove the problematic useEffect that causes race conditions
  // Manual loading via the "Load Stats" button is preferred

  return {
    data,
    isLoading,
    error,
    refresh,
    lastUpdated,
    startSlot,
    endSlot,
    setSlotRange
  };
}
