# API Configuration

This directory contains the centralized API configuration for the ElastiCache Navigator frontend application.

## Files

### `api.ts`
Contains all backend server connection configuration, replacing hardcoded URLs throughout the application.

## Configuration Structure

The configuration supports multiple environments and can be overridden with environment variables.

### Default Configuration

```typescript
{
  apiEndpoint: 'ec2-52-27-97-17.us-west-2.compute.amazonaws.com',
  apiPort: 8000,
  apiSsl: false,
  redisEndpoint: 'ec2-52-27-97-17.us-west-2.compute.amazonaws.com',
  redisPort: 6389,
  redisTls: true,
  type: 'elasticache',
  region: 'us-west-2'
}
```

### Environment-Specific Overrides

You can add environment-specific configurations:

- `development`: Local development settings
- `production`: Production environment settings
- `local`: Local Redis instance settings

### Environment Variables

You can override any configuration value using environment variables in your `.env` file:

```bash
# API Service Configuration
VITE_API_ENDPOINT=your-api-server.com
VITE_API_PORT=8000
VITE_API_SSL=false

# Redis Cluster Configuration
VITE_REDIS_ENDPOINT=your-redis-cluster.amazonaws.com
VITE_REDIS_PORT=6380
VITE_REDIS_TLS=true
```

## Usage

### In React Components

```typescript
import { getApiConfigWithEnvOverrides } from '@/config/api';

const config = getApiConfigWithEnvOverrides();
console.log(config.apiEndpoint); // 'ec2-52-27-97-17.us-west-2.compute.amazonaws.com'
```

### Getting Base URL

```typescript
import { getDefaultBaseUrl } from '@/config/api';

const baseUrl = getDefaultBaseUrl();
console.log(baseUrl); // 'http://ec2-52-27-97-17.us-west-2.compute.amazonaws.com:8000'
```

## Migration from Hardcoded URLs

This configuration system replaces hardcoded URLs that were previously in:

- `src/contexts/ConnectionContext.tsx` - Default connection configuration
- `src/services/valkeyApi.ts` - Fallback API URL

## Benefits

1. **Single Source of Truth**: All API endpoints are defined in one place
2. **Environment Support**: Easy switching between development, staging, and production
3. **Environment Variables**: Runtime configuration without code changes
4. **Maintainability**: Easier to update endpoints across the entire application
5. **Flexibility**: Support for different API and Redis endpoints

## Example .env File

Create a `.env` file in the project root to override default settings:

```bash
# Override API endpoint for local development
VITE_API_ENDPOINT=localhost
VITE_API_PORT=3001
VITE_API_SSL=false

# Override Redis endpoint for local testing
VITE_REDIS_ENDPOINT=localhost
VITE_REDIS_PORT=6379
VITE_REDIS_TLS=false
