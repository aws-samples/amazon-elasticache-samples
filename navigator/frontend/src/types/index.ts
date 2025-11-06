// Connection Types
export interface ConnectionConfig {
  id: string;
  name: string;
  // API Service endpoint (where your backend API runs)
  apiEndpoint: string;
  apiPort: number;
  apiSsl: boolean;
  // Redis/Valkey cluster endpoint (the actual database)
  redisEndpoint: string;
  redisPort: number;
  redisTls: boolean;
  redisCluster: boolean;
  password?: string;
  type: 'elasticache' | 'memorydb';
  region?: string;
  createdAt: Date;
  lastConnected?: Date;
  // Legacy fields for backward compatibility
  endpoint?: string;
  port?: number;
  ssl?: boolean;
  // from SettingsContext for global settings'
  influxEndpointUrl: string;
  influxPort: number;
  influxToken?: string;
  influxBucket?: string;
  influxOrg?: string;
}

export interface ConnectionStatus {
  id: string;
  connected: boolean;
  error?: string;
  info?: {
    version: string;
    mode: string;
    role: string;
  };
}

// Data Browser Types
export interface RedisKey {
  name: string;
  type: 'string' | 'hash' | 'list' | 'set' | 'zset' | 'stream';
  ttl: number;
  size?: number;
}

export interface RedisValue {
  key: string;
  type: string;
  value: any;
  ttl: number;
}

// CLI Types
export interface CommandResult {
  id: string;
  command: string;
  result: any;
  error?: string;
  timestamp: Date;
  executionTime: number;
  isAutoExecuted?: boolean;
}

// Monitoring Types
export interface Metrics {
  memoryUsed: number;
  memoryTotal: number;
  connectedClients: number;
  opsPerSecond: number;
  hitRate: number;
  evictedKeys: number;
  expiredKeys: number;
  timestamp: Date;
}

// Command Log Types
export interface CommandLogEntry {
  id: number;
  timestamp: number;
  execution_time_microseconds: number;
  command: string; // Backend returns string, not array
  client_info?: string;
}

export interface CommandLogResponse {
  entries: CommandLogEntry[];
  count: number;
  requested_count: number;
  timestamp: string;
  log_type: 'slow' | 'large-request' | 'large-reply';
  command_used: string; // Which command was used (SLOWLOG vs COMMANDLOG)
  fallback_used: boolean; // Whether fallback to SLOWLOG was used
  version_supported: boolean; // Whether the log type is supported on current version
}

export type CommandLogType = 'slow' | 'large-request' | 'large-reply';

export interface CommandLogTypeConfig {
  type: CommandLogType;
  label: string;
  description: string;
  minVersion: string;
  supported: boolean;
}

// Parallel Processing & Collection Types
export type MetricCategory = 'server' | 'memory' | 'connections' | 'commands' | 'cluster' | 'performance' | 'keyspace';

export type MetricCategoryStatus = 'pending' | 'loading' | 'complete' | 'failed' | 'timeout';

export interface TaskTiming {
  [key: string]: string; // e.g., "2.15s" or "timeout(8s)"
}

export interface CollectionInfo {
  method: 'parallel' | 'sequential';
  total_tasks: number;
  successful_tasks: number;
  failed_tasks: number;
  task_timings: TaskTiming;
  total_duration_seconds: number;
}

export interface CategoryState {
  status: MetricCategoryStatus;
  timing?: string;
  error?: string;
  lastAttempt?: Date;
  retryCount?: number;
}

export type MetricCollectionState = {
  [K in MetricCategory]: CategoryState;
};

export interface EnhancedMetricsResponse {
  data: any; // The actual metrics data
  collection_info?: CollectionInfo;
  partial_results?: boolean;
  failed_categories?: MetricCategory[];
}

// Performance Tracking Types
export interface CategoryPerformanceHistory {
  category: MetricCategory;
  timings: Array<{
    timestamp: Date;
    duration: number;
    status: 'success' | 'failed' | 'timeout';
  }>;
  averageDuration: number;
  successRate: number;
}

export interface CollectionPerformanceMetrics {
  totalCollections: number;
  averageCollectionTime: number;
  categoryPerformance: CategoryPerformanceHistory[];
  recentCollections: Array<{
    timestamp: Date;
    totalDuration: number;
    successfulTasks: number;
    totalTasks: number;
  }>;
}

// Navigation Types
export interface NavItem {
  title: string;
  href: string;
  icon: string;
  description: string;
}
