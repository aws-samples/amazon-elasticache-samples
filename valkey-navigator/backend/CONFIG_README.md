# Configuration Management System

This document describes the YAML-based configuration system implemented for the Valkey Cluster API Server.

## Overview

The configuration system replaces hardcoded variables with a flexible, hierarchical YAML-based configuration that supports:

- **Environment-specific configurations** (dev, staging, prod)
- **Environment variable overrides** for secure deployment
- **Type validation** using Pydantic models
- **Backward compatibility** with existing environment variables
- **Centralized configuration management**

## Configuration Files

### Directory Structure

```
config/
├── config.yaml          # Main configuration file
├── config.dev.yaml      # Development environment overrides
└── config.prod.yaml     # Production environment overrides (optional)
```

### Main Configuration File (`config/config.yaml`)

```yaml
# Main configuration file for Valkey Cluster API Server
valkey:
  host: "clustercfg.redis-demo-cache.a1oizt.usw2.cache.amazonaws.com"
  port: 6379
  use_tls: true
  use_cluster: false

server:
  host: "0.0.0.0"
  port: 8000
  debug_mode: false

logging:
  level: "INFO"
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

app:
  name: "Valkey Cluster API Server"
  version: "1.0.0"
  description: "Python API server for React applications to interact with Valkey cluster and retrieve metrics"
```

### Environment-Specific Configuration

Create `config.dev.yaml` for development settings:

```yaml
# Development environment overrides
server:
  debug_mode: true
  port: 8001

logging:
  level: "DEBUG"

valkey:
  host: "localhost"
  use_tls: false
```

## Configuration Priority

Configuration values are applied in the following order (highest to lowest priority):

1. **Environment Variables** (highest priority)
2. **Environment-specific config file** (e.g., `config.dev.yaml`)
3. **Main config file** (`config.yaml`)
4. **Default values** (lowest priority)

## Environment Variables

The system maintains backward compatibility with existing environment variables:

| Environment Variable | Configuration Path | Description |
|---------------------|-------------------|-------------|
| `VALKEY_HOST` | `valkey.host` | Valkey/Redis host |
| `VALKEY_PORT` | `valkey.port` | Valkey/Redis port |
| `VALKEY_USE_TLS` | `valkey.use_tls` | Enable TLS connection |
| `VALKEY_USE_CLUSTER` | `valkey.use_cluster` | Enable cluster mode |
| `SERVER_HOST` | `server.host` | Server bind address |
| `SERVER_PORT` | `server.port` | Server port |
| `DEBUG_MODE` | `server.debug_mode` | Enable debug mode |
| `LOG_LEVEL` | `logging.level` | Logging level |

## Usage Examples

### Basic Usage

```python
from config_manager import get_config, get_valkey_config

# Get full configuration
config = get_config()

# Get specific configuration sections
valkey_config = get_valkey_config()
server_config = get_server_config()

# Access configuration values
print(f"Connecting to {valkey_config.host}:{valkey_config.port}")
```

### Environment-Specific Deployment

#### Development Environment
```bash
export APP_ENV=dev
python app.py
# Uses config.yaml + config.dev.yaml
```

#### Production Environment
```bash
export APP_ENV=prod
export VALKEY_HOST=prod-redis.example.com
export VALKEY_USE_TLS=true
python app.py
# Uses config.yaml + config.prod.yaml + environment overrides
```

### Docker Deployment

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY . .

RUN pip install -r requirements.txt

# Set environment for production
ENV APP_ENV=prod
ENV VALKEY_HOST=prod-redis.example.com
ENV VALKEY_USE_TLS=true

CMD ["python", "app.py"]
```

## Configuration Management Module

### ConfigManager Class

The `ConfigManager` class provides the core configuration functionality:

```python
from config_manager import ConfigManager

# Initialize with custom environment
config_manager = ConfigManager(environment="staging")
config = config_manager.get_config()

# Reload configuration
config = config_manager.reload_config()
```

### Configuration Models

Configuration is validated using Pydantic models:

- `ValkeyConfig`: Valkey/Redis connection settings
- `ServerConfig`: Server configuration
- `LoggingConfig`: Logging settings
- `AppConfig`: Application metadata

### Type Validation

The system automatically validates configuration types:

```python
# Invalid port number will raise validation error
valkey:
  port: "invalid"  # ValidationError: ensure this value is greater than 0
```

## Migration from Hardcoded Variables

### Before (Hardcoded)
```python
VALKEY_HOST = os.getenv("VALKEY_HOST", "default-host")
VALKEY_PORT = int(os.getenv("VALKEY_PORT", "6379"))
```

### After (Configuration System)
```python
from config_manager import get_valkey_config

valkey_config = get_valkey_config()
print(f"Host: {valkey_config.host}")
print(f"Port: {valkey_config.port}")
```

## Best Practices

1. **Environment Variables for Secrets**: Use environment variables for sensitive data like passwords and API keys
2. **Configuration Files for Structure**: Use YAML files for complex configuration structures
3. **Environment-Specific Files**: Create separate config files for different environments
4. **Validation**: Always validate configuration using Pydantic models
5. **Documentation**: Document all configuration options and their purposes

## Troubleshooting

### Configuration Loading Issues

Check configuration loading with debug logging:

```python
import logging
logging.basicConfig(level=logging.DEBUG)

from config_manager import get_config
config = get_config()
```

### Environment Variable Overrides

Verify environment variable names and values:

```bash
# Check current environment variables
env | grep VALKEY

# Test configuration loading
python -c "from config_manager import get_valkey_config; print(get_valkey_config())"
```

### Validation Errors

Configuration validation errors provide detailed information:

```python
# Example validation error
pydantic.ValidationError: 1 validation error for ValkeyConfig
port
  ensure this value is greater than 0 (type=value_error.number.not_gt; limit_value=0)
```

## Adding New Configuration Options

1. **Update Configuration Model**:
```python
class ValkeyConfig(BaseModel):
    host: str = Field(default="localhost")
    port: int = Field(default=6379, ge=1, le=65535)
    # Add new field
    timeout: int = Field(default=30, ge=1, le=300)
```

2. **Update YAML Configuration**:
```yaml
valkey:
  host: "localhost"
  port: 6379
  timeout: 30
```

3. **Add Environment Variable Mapping** (optional):
```python
env_mappings = {
    'VALKEY_TIMEOUT': ['valkey', 'timeout'],
    # ... other mappings
}
```

## Dependencies

- `PyYAML==6.0.1`: YAML parsing
- `pydantic==2.5.0`: Configuration validation
- `python-dotenv==1.0.0`: Environment variable loading
