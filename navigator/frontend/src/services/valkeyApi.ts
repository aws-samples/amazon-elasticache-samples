import { getDefaultBaseUrl } from '@/config/api';

// API Response Types
export interface ValkeyServerMetrics {
  redis_version: string;
  valkey_version?: string;
  uptime_in_seconds: number;
  uptime_in_days: number;
  redis_mode: string;
  os: string;
  arch_bits: number;
  process_id: number;
  tcp_port?: number;
  hz: number;
  configured_hz: number;
  lru_clock: number;
  executable: string;
  config_file: string;
  system_cpu_percent: number;
  system_cpu_count: number;
  timestamp: string;
}

export interface ValkeyMemoryMetrics {
  used_memory: number;
  used_memory_human: string;
  used_memory_rss: number;
  used_memory_rss_human: string;
  used_memory_peak: number;
  used_memory_peak_human: string;
  used_memory_peak_perc: string;
  used_memory_overhead: number;
  used_memory_startup: number;
  used_memory_dataset: number;
  used_memory_dataset_perc: string;
  used_memory_lua: number;
  used_memory_lua_human: string;
  used_memory_scripts: number;
  used_memory_scripts_human: string;
  number_of_cached_scripts: number;
  maxmemory: number;
  maxmemory_human: string;
  maxmemory_policy: string;
  mem_fragmentation_ratio: number;
  mem_fragmentation_bytes: number;
  allocator_allocated: number;
  allocator_active: number;
  allocator_resident: number;
  allocator_frag_ratio: number;
  allocator_frag_bytes: number;
  allocator_rss_ratio: number;
  allocator_rss_bytes: number;
  rss_overhead_ratio: number;
  rss_overhead_bytes: number;
  total_system_memory: number;
  total_system_memory_human: string;
  system_memory_total: number;
  system_memory_available: number;
  system_memory_percent: number;
  system_memory_used: number;
  system_memory_free: number;
  mem_clients_slaves: number;
  mem_clients_normal: number;
  mem_aof_buffer: number;
  mem_replication_backlog: number;
  mem_not_counted_for_evict: number;
  timestamp: string;
  // Computed fields for UI compatibility
  mem_allocator?: string;
}

export interface ValkeyConnectionMetrics {
  connected_clients: number;
  client_recent_max_input_buffer: number;
  client_recent_max_output_buffer: number;
  blocked_clients: number;
  tracking_clients: number;
  clients_in_timeout_table: number;
  total_client_connections: number;
  client_list_sample?: Array<{
    id: string;
    addr: string;
    laddr: string;
    fd: string;
    name: string;
    age: string;
    idle: string;
    flags: string;
    db: string;
    sub: string;
    psub: string;
    ssub: string;
    multi: string;
    watch: string;
    qbuf: string;
    "qbuf-free": string;
    "argv-mem": string;
    "multi-mem": string;
    rbs: string;
    rbp: string;
    obl: string;
    oll: string;
    omem: string;
    "tot-mem": string;
    events: string;
    cmd: string;
    user: string;
    redir: string;
    resp: string;
    "lib-name": string;
    "lib-ver": string;
    "tot-net-in": string;
    "tot-net-out": string;
    "tot-cmds": string;
  }>;
  timestamp: string;
}

export interface ValkeyCommandMetrics {
  total_connections_received: number;
  total_commands_processed: number;
  instantaneous_ops_per_sec: number;
  total_net_input_bytes: number;
  total_net_output_bytes: number;
  instantaneous_input_kbps: number;
  instantaneous_output_kbps: number;
  rejected_connections: number;
  sync_full: number;
  sync_partial_ok: number;
  sync_partial_err: number;
  expired_keys: number;
  expired_stale_perc: number;
  expired_time_cap_reached_count: number;
  expire_cycle_cpu_milliseconds: number;
  evicted_keys: number;
  keyspace_hits: number;
  keyspace_misses: number;
  pubsub_channels: number;
  pubsub_patterns: number;
  latest_fork_usec: number;
  migrate_cached_sockets: number;
  slave_expires_tracked_keys: number;
  active_defrag_hits: number;
  active_defrag_misses: number;
  active_defrag_key_hits: number;
  active_defrag_key_misses: number;
  tracking_total_keys: number;
  tracking_total_items: number;
  tracking_total_prefixes: number;
  unexpected_error_replies: number;
  total_reads_processed: number;
  total_writes_processed: number;
  io_threaded_reads_processed: number;
  io_threaded_writes_processed: number;
  timestamp: string;
}

export interface ValkeyClusterMetrics {
  cluster_enabled: number;
  role: string;
  connected_slaves: number;
  master_failover_state: string;
  master_replid: string;
  master_replid2: string;
  master_repl_offset: number;
  second_repl_offset: number;
  repl_backlog_active: number;
  repl_backlog_size: number;
  repl_backlog_first_byte_offset: number;
  repl_backlog_histlen: number;
  timestamp: string;
  // Computed fields for UI compatibility
  cluster_state?: string;
  cluster_size?: number;
  cluster_slots_assigned?: number;
  cluster_slots_ok?: number;
  cluster_slots_pfail?: number;
  cluster_slots_fail?: number;
  cluster_known_nodes?: number;
  cluster_current_epoch?: number;
  cluster_my_epoch?: number;
}

export interface ValkeyPerformanceMetrics {
  hit_ratio_percent: number;
  keyspace_hits: number;
  keyspace_misses: number;
  total_requests: number;
  ops_per_sec: number;
  input_kbps: number;
  output_kbps: number;
  used_cpu_sys: number;
  used_cpu_user: number;
  used_cpu_sys_children: number;
  used_cpu_user_children: number;
  command_stats_sample: {
    [key: string]: {
      calls: number;
      usec: number;
      usec_per_call: number;
      rejected_calls: number;
      failed_calls: number;
    };
  };
  timestamp: string;
  // Computed fields for UI compatibility
  instantaneous_ops_per_sec?: number;
  total_commands_processed?: number;
  expired_keys?: number;
  evicted_keys?: number;
  hit_rate?: number;
}

export interface ValkeyKeyspaceMetrics {
  keyspace_info: {
    db0?: {
      keys: number;
      expires: number;
      avg_ttl: number;
    };
    db1?: {
      keys: number;
      expires: number;
      avg_ttl: number;
    };
  };
  timestamp: string;
  // Computed fields for UI compatibility
  db0?: {
    keys: number;
    expires: number;
    avg_ttl: number;
  };
  db1?: {
    keys: number;
    expires: number;
    avg_ttl: number;
  };
  total_keys?: number;
  total_expires?: number;
}

export interface ValkeyNodeMetrics {
  nodeId: string;
  nodeAddress: string;
  role: 'master' | 'slave';
  status: 'online' | 'offline';
  memory: {
    used: string;
    max: string;
    usedBytes: number;
    maxBytes: number;
    percent: number;
  };
  cpu: {
    percent: number;
    cores: number;
  };
  connections: number;
  opsPerSec: number;
  uptime: string;
  keyCount: number;
  slots?: string;
  flags?: string[];
  masterNodeId?: string;
}

export interface ValkeyClusterNodesResponse {
  nodes: ValkeyNodeMetrics[];
  clusterInfo?: {
    totalNodes: number;
    mastersCount: number;
    slavesCount: number;
    clusterState: string;
  };
  timestamp: string;
}

export interface ClusterSlotStats {
  slot_id: number;
  key_count: number;
  cpu_usec: number;
  network_bytes_in: number;
  network_bytes_out: number;
}

export interface ClusterSlotStatsResponse {
  slots: ClusterSlotStats[];
  total_slots: number;
  start_slot: number;
  end_slot: number;
  cluster_mode?: boolean;
  command_executed?: string;
  // These fields might be missing from API and calculated on frontend
  total_keys?: number;
  total_cpu_usec?: number;
  total_network_bytes_in?: number;
  total_network_bytes_out?: number;
  timestamp?: string;
}

export interface ValkeyAllMetrics {
  server: ValkeyServerMetrics;
  memory: ValkeyMemoryMetrics;
  connections: ValkeyConnectionMetrics;
  commands: ValkeyCommandMetrics;
  cluster: ValkeyClusterMetrics;
  performance: ValkeyPerformanceMetrics;
  keyspace: ValkeyKeyspaceMetrics;
  timestamp: string;
}

// Enhanced response with collection metadata
export interface EnhancedValkeyMetrics {
  metrics: ValkeyAllMetrics;
  collection_info?: {
    method: 'parallel' | 'sequential';
    total_tasks: number;
    successful_tasks: number;
    failed_tasks: number;
    task_timings: {
      [key: string]: string; // e.g., "server": "2.15s", "connections": "timeout(8s)"
    };
    total_duration_seconds: number;
  };
  partial_results?: boolean;
  failed_categories?: string[];
}

export interface ExecuteCommandResponse {
  command: string;
  success: boolean;
  return_code: number;
  stdout: string;
  stderr: string;
  execution_time: number;
  timestamp: string;
  message: string;
}

export interface ScanResponse {
  pattern: string;
  keys: string[];
  count: number;
  scan_method: "SCAN" | "KEYS";
  scan_parameters?: {
    count: number;
    max_iterations: number;
  };
  timestamp: string;
}

export interface PaginatedScanResponse {
  cursor: string;
  keys: string[];
  count: number;
  complete: boolean;
  scan_method: "SCAN";
  paginated: true;
  timestamp: string;
}

export interface PageCache {
  [pageNumber: number]: {
    keys: string[];
    cursor: string;
    nextCursor?: string;
    timestamp: number;
    complete: boolean;
  };
}

export interface ApiError {
  error: string;
  message?: string;
}

export interface KeyTypeResponse {
  type: string;
}

class ValkeyApiService {
  private currentConnection: any = null;

  // Set the current connection for API calls
  setConnection(connection: any): void {
    this.currentConnection = connection;
    console.log('🔗 API Service connection updated:', {
      id: connection?.id,
      name: connection?.name,
      apiEndpoint: connection?.apiEndpoint || connection?.endpoint,
      apiPort: connection?.apiPort || connection?.port
    });
  }

  private getBaseUrl(): string {
    // First try to use the current connection set via setConnection
    if (this.currentConnection) {
      const apiEndpoint = this.currentConnection.apiEndpoint || this.currentConnection.endpoint || 'localhost';
      const apiPort = this.currentConnection.apiPort || this.currentConnection.port;
      const apiSsl = this.currentConnection.apiSsl ?? this.currentConnection.ssl;
      
      if (apiEndpoint) {
        console.log(`DEBUG ---------------- apiEndpoint ${apiEndpoint}`);
        const baseUrl = `http${apiSsl ? 's' : ''}://${apiEndpoint}:${apiPort}`;
        console.log('🔗 Using current connection for API calls:', baseUrl);
        return baseUrl;
      }
    }

    // Fallback to localStorage for backward compatibility
    const activeConnectionId = localStorage.getItem('valkey-active-connection');
    if (activeConnectionId) {
      const connections = localStorage.getItem('valkey-connections');
      if (connections) {
        try {
          const parsed = JSON.parse(connections);
          const activeId = JSON.parse(activeConnectionId);
          const connection = parsed.find((conn: any) => conn.id === activeId);
          if (connection) {
            // Use new API endpoint fields with fallback to legacy fields
            const apiEndpoint = connection.apiEndpoint || connection.endpoint;
            const apiPort = connection.apiPort || connection.port;
            const apiSsl = connection.apiSsl ?? connection.ssl;
            const baseUrl = `http${apiSsl ? 's' : ''}://${apiEndpoint}:${apiPort}`;
            console.log('🔗 Using localStorage connection for API calls:', baseUrl);
            return baseUrl;
          }
        } catch (error) {
          console.error('Failed to get active connection:', error);
        }
      }
    }
    
    // Default fallback from config
    const fallbackUrl = getDefaultBaseUrl();
    console.log('🔗 Using fallback connection for API calls:', fallbackUrl);
    return fallbackUrl;
  }

  private parseClusterError(errorText: string): { type: string; slot: number; node: string } | null {
    // Parse MOVED or ASK responses: "MOVED 8387 redis-node:6379" or "ASK 8387 redis-node:6379"
    const movedMatch = errorText.match(/(MOVED|ASK)\s+(\d+)\s+([^\s]+)/);
    if (movedMatch) {
      return {
        type: movedMatch[1],
        slot: parseInt(movedMatch[2]),
        node: movedMatch[3]
      };
    }
    return null;
  }

  private async fetchWithErrorHandling<T>(endpoint: string): Promise<T> {
    // Gate all backend calls based on login status
    try {
      const loggedIn = typeof window !== 'undefined' && localStorage.getItem('valkey-logged-in') === 'true';
      if (!loggedIn) {
        // Do not call backend when logged out
        throw new Error('Please log in');
      }
    } catch {
      // If localStorage not available, treat as logged out
      throw new Error('Please log in');
    }
    console.log(`DEBUG --------------- fetchWithErrorHandling::${endpoint}`);
    const timeoutMs = 30000; // 30 second timeout for slow backend responses
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    
    try {
      const baseUrl = this.getBaseUrl();
      console.log(`    --> baseUrl: ${baseUrl}`);
      const fullUrl = `${baseUrl}${endpoint}`;
      console.log(`🔍 API Request: ${fullUrl}`);
      
      const response = await fetch(fullUrl, {
        signal: controller.signal,
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json',
        }
      });
      
      clearTimeout(timeoutId);
      
      console.log(`📡 Response Status: ${response.status} ${response.statusText} for ${endpoint}`);
      
      if (!response.ok) {
        // Try to get more detailed error information
        let errorDetails = `${response.status} ${response.statusText}`;
        try {
          const errorText = await response.text();
          if (errorText) {
            console.error(`❌ Backend Error Response:`, errorText);
            errorDetails += ` - ${errorText}`;
            
            // Check for cluster-related errors
            if (errorText.includes('MOVED') || errorText.includes('ASK')) {
              const clusterError = this.parseClusterError(errorText);
              if (clusterError) {
                throw new Error(`Cluster Redirection Error: ${clusterError.type} ${clusterError.slot} ${clusterError.node} - Your backend needs to handle Redis cluster redirections. The key is located on a different cluster node.`);
              }
            }
          }
        } catch (e) {
          // If it's already our custom error, re-throw it
          if (e instanceof Error && e.message.includes('Cluster Redirection Error')) {
            throw e;
          }
          // Ignore other parsing errors
        }
        throw new Error(`HTTP Error: ${errorDetails}`);
      }
      
      const responseText = await response.text();
      console.log(`📄 Raw Response Text for ${endpoint}:`, responseText.substring(0, 500) + (responseText.length > 500 ? '...' : ''));
      
      let responseData;
      try {
        responseData = JSON.parse(responseText);
      } catch (parseError) {
        console.error(`❌ JSON Parse Error for ${endpoint}:`, parseError);
        console.error(`❌ Response text that failed to parse:`, responseText);
        throw new Error(`Invalid JSON response from ${endpoint}: ${parseError}`);
      }
      
      console.log(`✅ Parsed API Response for ${endpoint}:`, responseData);
      
      // Handle wrapped API response format
      if (responseData.status && responseData.status !== 'success') {
        throw new Error(`API Error: ${responseData.message || 'Unknown error'}`);
      }
      
      // Return the data property if it exists, otherwise return the full response
      return responseData.data || responseData;
    } catch (error) {
      clearTimeout(timeoutId);
      
      if (error instanceof Error && error.name === 'AbortError') {
        console.error(`⏰ Request timeout for ${endpoint} after ${timeoutMs}ms`);
        throw new Error(`Request timeout: ${endpoint} took longer than ${timeoutMs}ms to respond`);
      }
      
      console.error(`❌ Error fetching ${endpoint}:`, error);
      throw error;
    }
  }

  async getHealth(): Promise<{ status: string; timestamp: string }> {
    return this.fetchWithErrorHandling('/health');
  }

  async getServerMetrics(): Promise<ValkeyServerMetrics> {
    return this.fetchWithErrorHandling('/api/metrics/server');
  }

  async getMemoryMetrics(): Promise<ValkeyMemoryMetrics> {
    return this.fetchWithErrorHandling('/api/metrics/memory');
  }

  async getConnectionMetrics(): Promise<ValkeyConnectionMetrics> {
    return this.fetchWithErrorHandling('/api/metrics/connections');
  }

  async getCommandMetrics(): Promise<ValkeyCommandMetrics> {
    return this.fetchWithErrorHandling('/api/metrics/commands');
  }

  async getClusterMetrics(): Promise<ValkeyClusterMetrics> {
    return this.fetchWithErrorHandling('/api/metrics/cluster');
  }

  async getPerformanceMetrics(): Promise<ValkeyPerformanceMetrics> {
    return this.fetchWithErrorHandling('/api/metrics/performance');
  }

  async getKeyspaceMetrics(): Promise<ValkeyKeyspaceMetrics> {
    return this.fetchWithErrorHandling('/api/metrics/keyspace');
  }

  private transformMetrics(rawData: any): ValkeyAllMetrics {
    console.log('📊 Raw API Response (Full Structure):', JSON.stringify(rawData, null, 2));
    console.log('📊 Raw API Response Keys:', Object.keys(rawData || {}));
    
    // Debug each section
    console.log('🔧 Debug - Server section:', rawData.server);
    console.log('🔧 Debug - Memory section:', rawData.memory);
    console.log('🔧 Debug - Connections section:', rawData.connections);
    console.log('🔧 Debug - Commands section:', rawData.commands);
    console.log('🔧 Debug - Cluster section:', rawData.cluster);
    console.log('🔧 Debug - Performance section:', rawData.performance);
    console.log('🔧 Debug - Keyspace section:', rawData.keyspace);

    // Safely handle keyspace_info with null checks
    const keyspaceInfo = rawData.keyspace?.keyspace_info || {};
    let totalKeys = 0;
    let totalExpires = 0;
    
    try {
      // Calculate totals safely
      if (keyspaceInfo && typeof keyspaceInfo === 'object') {
        console.log('🔧 Debug - Processing keyspace_info:', keyspaceInfo);
        totalKeys = Object.values(keyspaceInfo).reduce((sum: number, db: any) => {
          const keys = db && typeof db === 'object' && typeof db.keys === 'number' ? db.keys : 0;
          console.log('🔧 Debug - DB keys:', db, 'extracted keys:', keys);
          return sum + keys;
        }, 0);
        totalExpires = Object.values(keyspaceInfo).reduce((sum: number, db: any) => {
          const expires = db && typeof db === 'object' && typeof db.expires === 'number' ? db.expires : 0;
          console.log('🔧 Debug - DB expires:', db, 'extracted expires:', expires);
          return sum + expires;
        }, 0);
      }
    } catch (error) {
      console.warn('⚠️ Error calculating keyspace totals:', error);
    }

    // Determine cluster mode with better detection
    const isClusterEnabled = rawData.cluster?.cluster_enabled === 1 || 
                            rawData.cluster?.cluster_enabled === '1' ||
                            rawData.server?.redis_mode === 'cluster';

    console.log('🔍 Cluster Detection:', {
      cluster_enabled: rawData.cluster?.cluster_enabled,
      redis_mode: rawData.server?.redis_mode,
      isClusterEnabled,
      connected_slaves: rawData.cluster?.connected_slaves
    });

    const transformedData: ValkeyAllMetrics = {
      server: {
        ...rawData.server,
        tcp_port: rawData.server?.tcp_port || 6379,
        // Standardize version field
        valkey_version: rawData.server?.valkey_version || 
                       rawData.server?.redis_version || 
                       rawData.server?.server_version ||
                       rawData.server?.version || 
                       'Unknown',
      },
      memory: {
        ...rawData.memory,
        mem_allocator: rawData.memory?.mem_allocator || 'jemalloc',
      },
      connections: {
        ...rawData.connections,
        total_connections_received: rawData.commands?.total_connections_received || 
                                  rawData.connections?.total_connections_received || 0,
      },
      commands: {
        ...rawData.commands,
        // Ensure all required fields exist
        instantaneous_ops_per_sec: rawData.commands?.instantaneous_ops_per_sec || 0,
        total_commands_processed: rawData.commands?.total_commands_processed || 0,
        keyspace_hits: rawData.commands?.keyspace_hits || 0,
        keyspace_misses: rawData.commands?.keyspace_misses || 0,
        expired_keys: rawData.commands?.expired_keys || 0,
        evicted_keys: rawData.commands?.evicted_keys || 0,
      },
      cluster: {
        ...rawData.cluster,
        cluster_enabled: isClusterEnabled ? 1 : 0,
        // Improved cluster size calculation
        cluster_size: isClusterEnabled ? 
          Math.max(1, (rawData.cluster?.connected_slaves || 0) + 1) : 1,
        cluster_state: isClusterEnabled ? 'ok' : 'disabled',
        cluster_slots_assigned: isClusterEnabled ? 16384 : 0,
        cluster_slots_ok: isClusterEnabled ? 16384 : 0,
        cluster_slots_pfail: 0,
        cluster_slots_fail: 0,
        cluster_known_nodes: isClusterEnabled ? 
          Math.max(1, (rawData.cluster?.connected_slaves || 0) + 1) : 1,
        cluster_current_epoch: rawData.cluster?.cluster_current_epoch || 0,
        cluster_my_epoch: rawData.cluster?.cluster_my_epoch || 0,
      } as any,
      performance: {
        hit_ratio_percent: rawData.performance?.hit_ratio_percent || 
                          this.calculateHitRatio(rawData.commands?.keyspace_hits, rawData.commands?.keyspace_misses),
        keyspace_hits: rawData.commands?.keyspace_hits || rawData.performance?.keyspace_hits || 0,
        keyspace_misses: rawData.commands?.keyspace_misses || rawData.performance?.keyspace_misses || 0,
        total_requests: rawData.performance?.total_requests || 
                       rawData.commands?.total_commands_processed || 0,
        ops_per_sec: rawData.performance?.ops_per_sec || 
                    rawData.commands?.instantaneous_ops_per_sec || 0,
        input_kbps: rawData.performance?.input_kbps || 
                   rawData.commands?.instantaneous_input_kbps || 0,
        output_kbps: rawData.performance?.output_kbps || 
                    rawData.commands?.instantaneous_output_kbps || 0,
        used_cpu_sys: rawData.performance?.used_cpu_sys || 0,
        used_cpu_user: rawData.performance?.used_cpu_user || 0,
        used_cpu_sys_children: rawData.performance?.used_cpu_sys_children || 0,
        used_cpu_user_children: rawData.performance?.used_cpu_user_children || 0,
        command_stats_sample: rawData.performance?.command_stats_sample || {},
        timestamp: rawData.performance?.timestamp || rawData.timestamp || '',
        // Backward compatibility fields
        instantaneous_ops_per_sec: rawData.commands?.instantaneous_ops_per_sec || 0,
        total_commands_processed: rawData.commands?.total_commands_processed || 0,
        expired_keys: rawData.commands?.expired_keys || 0,
        evicted_keys: rawData.commands?.evicted_keys || 0,
        hit_rate: rawData.performance?.hit_ratio_percent || 
                 this.calculateHitRatio(rawData.commands?.keyspace_hits, rawData.commands?.keyspace_misses),
      } as any,
      keyspace: {
        keyspace_info: keyspaceInfo,
        timestamp: rawData.keyspace?.timestamp || rawData.timestamp || '',
        // Backward compatibility fields
        db0: keyspaceInfo.db0 || null,
        db1: keyspaceInfo.db1 || null,
        total_keys: totalKeys,
        total_expires: totalExpires,
      } as any,
      timestamp: rawData.timestamp || rawData.collected_at || new Date().toISOString(),
    };

    console.log('✅ Transformed Metrics:', {
      totalKeys,
      totalExpires,
      clusterEnabled: transformedData.cluster.cluster_enabled,
      clusterSize: transformedData.cluster.cluster_size,
      hitRate: transformedData.performance.hit_ratio_percent,
      opsPerSec: transformedData.commands.instantaneous_ops_per_sec
    });

    return transformedData;
  }

  private calculateHitRatio(hits?: number, misses?: number): number {
    if (!hits && !misses) return 0;
    const totalRequests = (hits || 0) + (misses || 0);
    return totalRequests > 0 ? ((hits || 0) / totalRequests) * 100 : 0;
  }

  async getAllMetrics(): Promise<ValkeyAllMetrics> {
    const rawData = await this.fetchWithErrorHandling('/api/api/metrics/all');
    return this.transformMetrics(rawData);
  }

  // Enhanced method that returns both metrics and collection metadata
  async getEnhancedMetrics(): Promise<EnhancedValkeyMetrics> {
    const rawResponse = await this.fetchWithErrorHandling<any>('/api/api/metrics/all');
    
    console.log('📊 Enhanced Metrics Raw Response:', rawResponse);
    
    // Check if response includes collection_info (new parallel backend format)
    if (rawResponse.collection_info) {
      console.log('✅ Detected parallel processing response format');
      return {
        metrics: this.transformMetrics(rawResponse),
        collection_info: rawResponse.collection_info,
        partial_results: rawResponse.partial_results || false,
        failed_categories: rawResponse.failed_categories || []
      };
    } else {
      console.log('📝 Using legacy response format');
      // Legacy format - just return the metrics
      return {
        metrics: this.transformMetrics(rawResponse),
        collection_info: undefined,
        partial_results: false,
        failed_categories: []
      };
    }
  }

  // Method to retry specific metric categories
  async retryMetricCategories(categories: string[]): Promise<EnhancedValkeyMetrics> {
    const params = new URLSearchParams({
      retry_categories: categories.join(',')
    });
    
    console.log(`🔄 Retrying metric categories: ${categories.join(', ')}`);
    
    const rawResponse = await this.fetchWithErrorHandling<any>(`/api/api/metrics/all?${params}`);
    
    if (rawResponse.collection_info) {
      return {
        metrics: this.transformMetrics(rawResponse),
        collection_info: rawResponse.collection_info,
        partial_results: rawResponse.partial_results || false,
        failed_categories: rawResponse.failed_categories || []
      };
    } else {
      return {
        metrics: this.transformMetrics(rawResponse),
        collection_info: undefined,
        partial_results: false,
        failed_categories: []
      };
    }
  }

  // Cache operations
  async getCacheKey(key: string): Promise<any> {
    return this.fetchWithErrorHandling(`/api/cache/get/${key}`);
  }

  async setCacheKey(key: string, value: any, ttl?: number): Promise<any> {
    const body: any = { key, value };
    if (ttl) body.ttl = ttl;
    
    const baseUrl = this.getBaseUrl();
    const response = await fetch(`${baseUrl}/api/cache/set`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });
    
    if (!response.ok) {
      // Enhanced error handling for cluster redirections
      let errorDetails = `${response.status} ${response.statusText}`;
      try {
        const errorText = await response.text();
        if (errorText) {
          console.error(`❌ SET Backend Error Response:`, errorText);
          errorDetails += ` - ${errorText}`;
          
          // Check for cluster-related errors
          if (errorText.includes('MOVED') || errorText.includes('ASK')) {
            const clusterError = this.parseClusterError(errorText);
            if (clusterError) {
              throw new Error(`Cluster Redirection Error: ${clusterError.type} ${clusterError.slot} ${clusterError.node} - Your backend needs to handle Redis cluster redirections. The key "${key}" should be stored on a different cluster node.`);
            }
          }
        }
      } catch (e) {
        // If it's already our custom error, re-throw it
        if (e instanceof Error && e.message.includes('Cluster Redirection Error')) {
          throw e;
        }
        // Ignore other parsing errors
      }
      throw new Error(`API Error: ${errorDetails}`);
    }
    
    return response.json();
  }

  async deleteCacheKey(key: string): Promise<any> {
    const baseUrl = this.getBaseUrl();
    const response = await fetch(`${baseUrl}/api/cache/${key}`, {
      method: 'DELETE',
    });
    
    if (!response.ok) {
      // Enhanced error handling for cluster redirections
      let errorDetails = `${response.status} ${response.statusText}`;
      try {
        const errorText = await response.text();
        if (errorText) {
          console.error(`❌ DELETE Backend Error Response:`, errorText);
          errorDetails += ` - ${errorText}`;
          
          // Check for cluster-related errors
          if (errorText.includes('MOVED') || errorText.includes('ASK')) {
            const clusterError = this.parseClusterError(errorText);
            if (clusterError) {
              throw new Error(`Cluster Redirection Error: ${clusterError.type} ${clusterError.slot} ${clusterError.node} - Your backend needs to handle Redis cluster redirections. The key "${key}" is located on a different cluster node.`);
            }
          }
        }
      } catch (e) {
        // If it's already our custom error, re-throw it
        if (e instanceof Error && e.message.includes('Cluster Redirection Error')) {
          throw e;
        }
        // Ignore other parsing errors
      }
      throw new Error(`API Error: ${errorDetails}`);
    }
    
    return response.json();
  }

  async getCacheKeys(
    pattern: string = '*', 
    count: number = 100, 
    maxIterations: number = 10000
  ): Promise<ScanResponse> {
    const params = new URLSearchParams({
      pattern,
      count: count.toString(),
      max_iterations: maxIterations.toString(),
      use_scan: 'true'
    });
    
    return this.fetchWithErrorHandling(`/api/cache/keys?${params}`);
  }

  // New paginated key fetching method
  async getPaginatedKeys(
    pattern: string = '*',
    cursor: string = '0',
    pageSize: number = 25
  ): Promise<PaginatedScanResponse> {
    const params = new URLSearchParams({
      pattern,
      cursor,
      count: pageSize.toString(),
      paginated: 'true'
    });
    
    console.log(`🔍 Fetching paginated keys: pattern="${pattern}", cursor="${cursor}", pageSize=${pageSize}`);
    
    const result = await this.fetchWithErrorHandling<PaginatedScanResponse>(`/api/cache/keys?${params}`);
    
    console.log(`✅ Paginated keys response:`, {
      cursor: result.cursor,
      count: result.count,
      complete: result.complete,
      keysPreview: result.keys.slice(0, 3).join(', ') + (result.keys.length > 3 ? '...' : '')
    });
    
    return result;
  }

  // Get Redis key type
  async getKeyType(key: string): Promise<{ type: string; error?: string; debugInfo?: any }> {
    const startTime = Date.now();
    let debugInfo: any = { key, attempts: [] };
    
    try {
      console.log(`🔍 Getting type for key: "${key}"`);
      
      // First try the REST endpoint with detailed logging
      debugInfo.attempts.push({ method: 'REST', endpoint: `/api/cache/type/${key}`, timestamp: Date.now() });
      
      try {
        const result = await this.fetchWithErrorHandling<KeyTypeResponse>(`/api/cache/type/${key}`);
        const duration = Date.now() - startTime;
        
        console.log(`✅ REST TYPE successful for "${key}": ${result.type} (${duration}ms)`);
        debugInfo.attempts[0].success = true;
        debugInfo.attempts[0].result = result;
        debugInfo.attempts[0].duration = duration;
        
        return { type: result.type, debugInfo };
      } catch (restError) {
        const duration = Date.now() - startTime;
        debugInfo.attempts[0].success = false;
        debugInfo.attempts[0].error = restError;
        debugInfo.attempts[0].duration = duration;
        
        console.warn(`⚠️ REST endpoint failed for TYPE ${key} (${duration}ms):`, restError);
        
        // Check if it's a connection error vs other error
        if (restError instanceof Error) {
          if (restError.message.includes('fetch') || restError.message.includes('network')) {
            console.warn(`🌐 Network-related error for TYPE ${key}, will retry with execute command`);
          } else if (restError.message.includes('404')) {
            console.warn(`🔍 REST endpoint not found for TYPE ${key}, falling back to execute command`);
          } else {
            console.warn(`❓ Unknown REST error for TYPE ${key}:`, restError.message);
          }
        }
      }
      
      // Fallback to using the execute endpoint with enhanced debugging
      console.log(`🔄 Falling back to execute command for key: "${key}"`);
      debugInfo.attempts.push({ method: 'EXECUTE', command: `TYPE ${key}`, timestamp: Date.now() });
      
      try {
        const result = await this.executeRedisCommand(`TYPE ${key}`);
        const duration = Date.now() - startTime;
        debugInfo.attempts[1].result = result;
        debugInfo.attempts[1].duration = duration;
        
        if (result.success && result.stdout) {
          const type = result.stdout.trim().toLowerCase();
          
          console.log(`✅ EXECUTE TYPE successful for "${key}": ${type} (${duration}ms)`);
          console.log(`📊 Command result details:`, {
            stdout: result.stdout,
            stderr: result.stderr,
            return_code: result.return_code,
            execution_time: result.execution_time
          });
          
          debugInfo.attempts[1].success = true;
          debugInfo.attempts[1].parsedType = type;
          
          return { type: type, debugInfo };
        } else {
          console.error(`❌ TYPE command failed for key "${key}":`, {
            success: result.success,
            stdout: result.stdout,
            stderr: result.stderr,
            return_code: result.return_code,
            message: result.message
          });
          
          debugInfo.attempts[1].success = false;
          debugInfo.attempts[1].failureReason = 'Command execution failed or empty result';
          
          return { 
            type: 'unknown', 
            error: `TYPE command failed: ${result.stderr || result.message || 'Unknown error'}`,
            debugInfo 
          };
        }
      } catch (executeError) {
        const duration = Date.now() - startTime;
        debugInfo.attempts[1].success = false;
        debugInfo.attempts[1].error = executeError;
        debugInfo.attempts[1].duration = duration;
        
        console.error(`❌ EXECUTE command failed for TYPE ${key} (${duration}ms):`, executeError);
        
        return { 
          type: 'unknown', 
          error: `Both REST and execute failed: ${executeError instanceof Error ? executeError.message : String(executeError)}`,
          debugInfo 
        };
      }
      
    } catch (unexpectedError) {
      const duration = Date.now() - startTime;
      console.error(`💥 Unexpected error in getKeyType for "${key}" (${duration}ms):`, unexpectedError);
      
      return { 
        type: 'unknown', 
        error: `Unexpected error: ${unexpectedError instanceof Error ? unexpectedError.message : String(unexpectedError)}`,
        debugInfo 
      };
    }
  }

  // Execute Redis command via /api/execute endpoint
  async executeRedisCommand(command: string): Promise<ExecuteCommandResponse> {
    const baseUrl = this.getBaseUrl();
    const response = await fetch(`${baseUrl}/api/execute`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ 
        command,
        timeout: 30000 // 30 second timeout
      }),
    });
    
    if (!response.ok) {
      // Enhanced error handling for cluster redirections
      let errorDetails = `${response.status} ${response.statusText}`;
      try {
        const errorText = await response.text();
        if (errorText) {
          console.error(`❌ EXECUTE Backend Error Response:`, errorText);
          errorDetails += ` - ${errorText}`;
          
          // Check for cluster-related errors
          if (errorText.includes('MOVED') || errorText.includes('ASK')) {
            const clusterError = this.parseClusterError(errorText);
            if (clusterError) {
              throw new Error(`Cluster Redirection Error: ${clusterError.type} ${clusterError.slot} ${clusterError.node} - Your backend needs to handle Redis cluster redirections. The command "${command}" should be executed on a different cluster node.`);
            }
          }
        }
      } catch (e) {
        // If it's already our custom error, re-throw it
        if (e instanceof Error && e.message.includes('Cluster Redirection Error')) {
          throw e;
        }
        // Ignore other parsing errors
      }
      throw new Error(`Redis Command Error: ${errorDetails}`);
    }
    
    const result = await response.json();
    console.log(`✅ Redis Command executed: ${command}`, result);
    return result;
  }

  // Legacy CLI Command execution (for backward compatibility)
  async executeCommand(command: string): Promise<any> {
    const cmd = command.trim().toUpperCase();
    const parts = command.trim().split(/\s+/);
    
    try {
      if (cmd.startsWith('GET ') && parts.length >= 2) {
        const key = parts.slice(1).join(' ');
        const result = await this.getCacheKey(key);
        return result;
      }
      
      if (cmd.startsWith('SET ') && parts.length >= 3) {
        const key = parts[1];
        const value = parts.slice(2).join(' ');
        const result = await this.setCacheKey(key, value);
        return result;
      }
      
      if (cmd.startsWith('DEL ') && parts.length >= 2) {
        const key = parts.slice(1).join(' ');
        const result = await this.deleteCacheKey(key);
        return result;
      }
      
      if (cmd.startsWith('KEYS') || cmd === 'KEYS *') {
        const pattern = parts.length > 1 ? parts.slice(1).join(' ') : '*';
        const result = await this.getCacheKeys(pattern);
        return result; // Return the full ScanResponse for compatibility
      }
      
      if (cmd === 'PING') {
        const result = await this.getHealth();
        return result.status === 'healthy' ? 'PONG' : 'Connection failed';
      }
      
      if (cmd.startsWith('INFO')) {
        if (cmd.includes('SERVER')) {
          return await this.getServerMetrics();
        } else if (cmd.includes('MEMORY')) {
          return await this.getMemoryMetrics();
        } else if (cmd.includes('CLIENTS')) {
          return await this.getConnectionMetrics();
        } else {
          return await this.getAllMetrics();
        }
      }
      
      if (cmd.startsWith('TYPE ') && parts.length >= 2) {
        const key = parts.slice(1).join(' ');
        const result = await this.getKeyType(key);
        return result;
      }
      
      if (cmd.startsWith('HGETALL ') && parts.length >= 2) {
        const key = parts.slice(1).join(' ');
        // Since there's no specific HGETALL endpoint, try to get the key
        const result = await this.getCacheKey(key);
        return result;
      }
      
      // Default case for unsupported commands
      throw new Error(`Command not supported: ${parts[0]}. Available commands: GET, SET, DEL, KEYS, PING, INFO, HGETALL, TYPE`);
      
    } catch (error) {
      throw error;
    }
  }

  // Recommendations API
  async getRecommendations(prompt: string): Promise<any> {
    const baseUrl = this.getBaseUrl();
    const response = await fetch(`${baseUrl}/api/cache/recommend`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ prompt }),
    });
    
    if (!response.ok) {
      let errorDetails = `${response.status} ${response.statusText}`;
      try {
        const errorText = await response.text();
        if (errorText) {
          console.error(`❌ Recommendations Backend Error Response:`, errorText);
          errorDetails += ` - ${errorText}`;
        }
      } catch (e) {
        // Ignore parsing errors
      }
      throw new Error(`Recommendations API Error: ${errorDetails}`);
    }
    
    const result = await response.json();
    console.log(`✅ Recommendations API response:`, result);
    return result;
  }

  // Chat API
  async sendChatMessage(prompt: string): Promise<any> {
    const baseUrl = this.getBaseUrl();
    const response = await fetch(`${baseUrl}/api/chat/converse`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ prompt }),
    });
    
    if (!response.ok) {
      let errorDetails = `${response.status} ${response.statusText}`;
      try {
        const errorText = await response.text();
        if (errorText) {
          console.error(`❌ Chat Backend Error Response:`, errorText);
          errorDetails += ` - ${errorText}`;
        }
      } catch (e) {
        // Ignore parsing errors
      }
      throw new Error(`Chat API Error: ${errorDetails}`);
    }
    
    const result = await response.json();
    console.log(`✅ Chat API response:`, result);
    return result;
  }

  // Command Log operations
  // GET /api/commandlog/{log_type}?count=N
  // DELETE /api/commandlog/{log_type}
  // GET /api/commandlog/{log_type}/count
  async getCommandLogEntries(logType: 'slow' | 'large-request' | 'large-reply', count?: number): Promise<any> {
    // Build endpoint URL with count parameter if provided
    const endpoint = count ? `/api/commandlog/${logType}?count=${count}` : `/api/commandlog/${logType}`;
    return this.fetchWithErrorHandling(endpoint);
  }

  async clearCommandLog(logType: 'slow' | 'large-request' | 'large-reply'): Promise<any> {
    const baseUrl = this.getBaseUrl();
    const response = await fetch(`${baseUrl}/api/commandlog/${logType}`, {
      method: 'DELETE',
    });
    
    if (!response.ok) {
      let errorDetails = `${response.status} ${response.statusText}`;
      try {
        const errorText = await response.text();
        if (errorText) {
          console.error(`❌ Clear CommandLog Backend Error Response:`, errorText);
          errorDetails += ` - ${errorText}`;
        }
      } catch (e) {
        // Ignore parsing errors
      }
      throw new Error(`Clear CommandLog API Error: ${errorDetails}`);
    }
    
    return response.json();
  }

  async getCommandLogLength(logType: 'slow' | 'large-request' | 'large-reply'): Promise<any> {
    return this.fetchWithErrorHandling(`/api/commandlog/${logType}/count`);
  }

  // Legacy slowlog methods for backward compatibility
  async getSlowlogEntries(count?: number): Promise<any> {
    console.warn('getSlowlogEntries is deprecated, use getCommandLogEntries instead');
    return this.getCommandLogEntries('slow', count);
  }

  async clearSlowlog(): Promise<any> {
    console.warn('clearSlowlog is deprecated, use clearCommandLog instead');
    return this.clearCommandLog('slow');
  }

  async getSlowlogLength(): Promise<any> {
    console.warn('getSlowlogLength is deprecated, use getCommandLogLength instead');
    return this.getCommandLogLength('slow');
  }

  // Cluster Node operations
  async getClusterNodes(): Promise<ValkeyNodeMetrics[]> {
    const response = await this.fetchWithErrorHandling<ValkeyClusterNodesResponse>('/api/cluster/nodes');
    return response.nodes || [];
  }

  async getAllNodeMetrics(): Promise<ValkeyClusterNodesResponse> {
    return this.fetchWithErrorHandling('/api/cluster/nodes/metrics');
  }

  async getNodeMetrics(nodeId: string): Promise<ValkeyNodeMetrics> {
    return this.fetchWithErrorHandling(`/api/nodes/${nodeId}/metrics`);
  }

  // Cluster Slot Stats operations
  async getClusterSlotStats(startSlot: number = 0, endSlot: number = 1000): Promise<ClusterSlotStatsResponse> {
    const params = new URLSearchParams();
    params.append('start_slot', startSlot.toString());
    params.append('end_slot', endSlot.toString());
    
    console.log(`🔍 Fetching slot stats for range ${startSlot}-${endSlot}`);
    
    const response = await this.fetchWithErrorHandling<any>(`/api/cluster/slot-stats?${params.toString()}`);
    
    console.log('📊 Raw slot stats API response:', response);
    
    // Transform and validate the response
    const transformedResponse: ClusterSlotStatsResponse = {
      slots: response?.slots || [],
      total_slots: response?.total_slots || 0,
      start_slot: response?.start_slot || startSlot,
      end_slot: response?.end_slot || endSlot,
      cluster_mode: response?.cluster_mode,
      command_executed: response?.command_executed,
      // Calculate totals from slots if not provided by API
      total_keys: response?.total_keys || (response?.slots || []).reduce((sum: number, slot: any) => sum + (slot.key_count || 0), 0),
      total_cpu_usec: response?.total_cpu_usec || (response?.slots || []).reduce((sum: number, slot: any) => sum + (slot.cpu_usec || 0), 0),
      total_network_bytes_in: response?.total_network_bytes_in || (response?.slots || []).reduce((sum: number, slot: any) => sum + (slot.network_bytes_in || 0), 0),
      total_network_bytes_out: response?.total_network_bytes_out || (response?.slots || []).reduce((sum: number, slot: any) => sum + (slot.network_bytes_out || 0), 0),
      timestamp: response?.timestamp || new Date().toISOString()
    };
    
    console.log('🔄 Transformed slot stats response:', transformedResponse);
    
    return transformedResponse;
  }

  // History series from InfluxDB via backend
  async getHistorySeries(
      metric: string,
      start: string,
      end?: string,
      redisEndpoint?: string,
      influxEndpointUrl?: string,
      influxPort?: string,
      influxToken?: string,
      influxBucket?: string,
      influxOrg?: string
  ): Promise<{ metric: string; redisEndpoint?: string; points: { time: string; value: number }[] }> {
    const params = new URLSearchParams();
    params.append('metric', metric);
    params.append('start', start);
    if (end) params.append('end', end);
    if (redisEndpoint) params.append('redisEndpoint', redisEndpoint);
    if (influxEndpointUrl) params.append('influxEndpointUrl', influxEndpointUrl);
      if (influxPort) params.append('influxPort', influxPort);
      if (influxToken) params.append('influxToken', influxToken);
      if (influxBucket) params.append('influxBucket', influxBucket);
      if (influxOrg) params.append('influxOrg', influxOrg);
    return this.fetchWithErrorHandling(`/api/history/series?${params.toString()}`);
  }

  // Fetch InfluxDB URL (scheme://endpoint:port) from backend env
  async getInfluxUrl(): Promise<{ url: string }> {
    return this.fetchWithErrorHandling(`/api/influx-url`);
  }
}

export const valkeyApi = new ValkeyApiService();
