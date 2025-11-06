/**
 * API Configuration
 * 
 * Centralized configuration for backend API endpoints and connection settings.
 * This file contains all hardcoded server URLs and connection parameters.
 */

export interface ApiEndpointConfig {
  apiEndpoint: string;
  apiPort: number;
  apiSsl: boolean;
  redisEndpoint: string;
  redisPort: number;
  redisTls: boolean;
  redisCluster: boolean;
  type: 'elasticache' | 'memorydb';
  region: string;
}

export const API_CONFIG = {
  // Default backend server configuration
  default: {
    apiEndpoint: 'ec2-52-27-97-17.us-west-2.compute.amazonaws.com',
    apiPort: 8000,
    apiSsl: false,
    redisEndpoint: 'funknor-devtest-cluster-0001-001.funknor-devtest-cluster.iaospb.use1.cache.amazonaws.com', //'ec2-52-27-97-17.us-west-2.compute.amazonaws.com',
    redisPort: 6379,
    redisTls: true,
    redisCluster: true,
    type: 'elasticache' as const,
    region: 'us-west-2'
  },
  
  // Environment-specific overrides
  development: {
    // Override for local development if needed
    // apiEndpoint: 'localhost',
    // apiPort: 8000,
    // apiSsl: false,
  },
  
  production: {
    // Override for production if needed
  },
  
  // You can add more preset configurations here
  local: {
    apiEndpoint: 'localhost',
    apiPort: 8000,
    apiSsl: false,
    redisEndpoint: '3.80.25.62', // 'localhost',
    redisPort: 6379,
    redisTls: true,
    type: 'elasticache' as const,
    region: 'local'
  }
} as const;

/**
 * Get the current API configuration based on environment
 * Uses Vite's import.meta.env.MODE to determine environment
 */
export const getApiConfig = (): ApiEndpointConfig => {
  const env = import.meta.env.MODE || 'development';
  const envConfig = API_CONFIG[env as keyof typeof API_CONFIG] || {};
  
  return {
    ...API_CONFIG.default,
    ...envConfig
  };
};

/**
 * Get the base URL for API requests
 */
export const getDefaultBaseUrl = (): string => {
  // 1) If VITE_BASE_URL provided at build time, use it
  if (import.meta.env.VITE_BASE_URL) {
    return String(import.meta.env.VITE_BASE_URL);
  }

  // 2) When running in a browser, construct the backend URL using the host IP/hostname
  //    and the configured backend port/ssl. This is important for Docker where the
  //    frontend runs on a different port (e.g., 5173) than the backend (e.g., 8000).
  if (typeof window !== 'undefined' && window.location) {
    const envEndpoint = import.meta.env.VITE_API_ENDPOINT as string | undefined;
    const envPort = import.meta.env.VITE_API_PORT as string | undefined;
    const envSsl = import.meta.env.VITE_API_SSL as string | undefined;

    const host = (envEndpoint && envEndpoint.trim()) ? envEndpoint.trim() : window.location.hostname;
    const port = envPort ? parseInt(envPort, 10) : 8000;
    const ssl = envSsl ? envSsl === 'true' : window.location.protocol === 'https:';

    // Handle IPv6 literals by wrapping in brackets
    const needsBrackets = host.includes(':') && !host.startsWith('[');
    const formattedHost = needsBrackets ? `[${host}]` : host;

    return `http${ssl ? 's' : ''}://${formattedHost}:${port}`;
  }

  // 3) Fallback to static config (useful for SSR or non-browser contexts)
  const config = getApiConfig();
  return `http${config.apiSsl ? 's' : ''}://${config.apiEndpoint}:${config.apiPort}`;
};

/**
 * Environment variable overrides
 * These can be set in .env files if needed
 */
export const getEnvOverrides = (): Partial<ApiEndpointConfig> => {
  const overrides: Partial<ApiEndpointConfig> = {};
  
  if (import.meta.env.VITE_API_ENDPOINT) {
    overrides.apiEndpoint = import.meta.env.VITE_API_ENDPOINT || 'localhost';
  }
  
  if (import.meta.env.VITE_API_PORT) {
    overrides.apiPort = parseInt(import.meta.env.VITE_API_PORT);
  }
  
  if (import.meta.env.VITE_API_SSL) {
    overrides.apiSsl = import.meta.env.VITE_API_SSL === 'true';
  }
  
  if (import.meta.env.VITE_REDIS_ENDPOINT) {
    overrides.redisEndpoint = import.meta.env.VITE_REDIS_ENDPOINT;
  }
  
  if (import.meta.env.VITE_REDIS_PORT) {
    overrides.redisPort = parseInt(import.meta.env.VITE_REDIS_PORT);
  }
  
  if (import.meta.env.VITE_REDIS_TLS) {
    overrides.redisTls = import.meta.env.VITE_REDIS_TLS === 'true';
  }
  
  return overrides;
};

/**
 * Get API config with environment variable overrides applied
 */
export const getApiConfigWithEnvOverrides = (): ApiEndpointConfig => {
  const baseConfig = getApiConfig();
  const envOverrides = getEnvOverrides();
  
  return {
    ...baseConfig,
    ...envOverrides
  };
};
