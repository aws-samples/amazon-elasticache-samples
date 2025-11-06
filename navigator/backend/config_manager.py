import os
import yaml
import logging
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, validator
from pathlib import Path

logger = logging.getLogger(__name__)

class ValkeyConfig(BaseModel):
    """Valkey/Redis configuration settings"""
    host: str = Field(default="clustercfg.redis-demo-cache.a1oizt.usw2.cache.amazonaws.com")
    port: int = Field(default=6379, ge=1, le=65535)
    use_tls: bool = Field(default=True)
    use_cluster: bool = Field(default=False)
    influxEndpointUrl: str = Field(default="UNDEFINED INFLUX URL")
    influxPort: int = Field(default=8086, ge=1, le=65535)
    influxToken: str = Field(default="UNDEFINED INFLUX TOKEN")
    influxBucket: str = Field(default="UNDEFINED INFLUX BUCKET")
    influxOrg: str = Field(default="UNDEFINED INFLUX ORG")

class ServerConfig(BaseModel):
    """Server configuration settings"""
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000, ge=1, le=65535)
    debug_mode: bool = Field(default=False)

class LoggingConfig(BaseModel):
    """Logging configuration settings"""
    level: str = Field(default="INFO")
    format: str = Field(default="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    
    @validator('level')
    def validate_log_level(cls, v):
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if v.upper() not in valid_levels:
            raise ValueError(f'Invalid log level: {v}. Must be one of: {valid_levels}')
        return v.upper()

class ExecuteAllowlistConfig(BaseModel):
    """Execute allowlist configuration"""
    enabled: bool = Field(default=True)
    mode: str = Field(default="exact")  # exact, prefix, regex
    commands: list = Field(default_factory=list)
    patterns: list = Field(default_factory=list)
    prefixes: list = Field(default_factory=list)
    
    @validator('mode')
    def validate_mode(cls, v):
        valid_modes = ['exact', 'prefix', 'regex']
        if v not in valid_modes:
            raise ValueError(f'Invalid mode: {v}. Must be one of: {valid_modes}')
        return v

class ExecuteConfig(BaseModel):
    """Execute configuration settings"""
    allowlist: ExecuteAllowlistConfig = Field(default_factory=ExecuteAllowlistConfig)

class AppConfig(BaseModel):
    """Application metadata configuration"""
    name: str = Field(default="Valkey Cluster API Server")
    version: str = Field(default="1.0.0")
    description: str = Field(default="Python API server for React applications to interact with Valkey cluster and retrieve metrics")

class Config(BaseModel):
    """Main configuration model"""
    valkey: ValkeyConfig = Field(default_factory=ValkeyConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    execute: ExecuteConfig = Field(default_factory=ExecuteConfig)
    app: AppConfig = Field(default_factory=AppConfig)

class ConfigManager:
    """Configuration manager that loads and manages application configuration"""
    
    def __init__(self, config_dir: str = "config", environment: Optional[str] = None):
        self.config_dir = Path(config_dir)
        self.environment = environment or os.getenv('APP_ENV', 'default')
        self._config: Optional[Config] = None
        
    def load_config(self) -> Config:
        """Load configuration from YAML files and environment variables"""
        if self._config is not None:
            return self._config
            
        # Start with default configuration
        config_data = {}
        
        # Load base configuration
        base_config_path = self.config_dir / "config.yaml"
        if base_config_path.exists():
            logger.info(f"Loading base configuration from {base_config_path}")
            with open(base_config_path, 'r') as f:
                config_data = yaml.safe_load(f) or {}
        else:
            logger.warning(f"Base configuration file not found at {base_config_path}")
            
        # Load environment-specific configuration if it exists
        if self.environment != 'default':
            env_config_path = self.config_dir / f"config.{self.environment}.yaml"
            if env_config_path.exists():
                logger.info(f"Loading environment-specific configuration from {env_config_path}")
                with open(env_config_path, 'r') as f:
                    env_config = yaml.safe_load(f) or {}
                    config_data = self._deep_merge(config_data, env_config)
        
        # Override with environment variables
        config_data = self._apply_env_overrides(config_data)
        
        # Validate and create configuration object
        try:
            self._config = Config(**config_data)
            logger.info("Configuration loaded successfully")
        except Exception as e:
            logger.error(f"Configuration validation failed: {e}")
            # Fall back to default configuration
            self._config = Config()
            logger.info("Using default configuration")
            
        return self._config
    
    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Deep merge two dictionaries"""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result
    
    def _apply_env_overrides(self, config_data: Dict[str, Any]) -> Dict[str, Any]:
        """Apply environment variable overrides to configuration"""
        
        # Environment variable mappings (for backward compatibility)
        env_mappings = {
            'VALKEY_HOST': ['valkey', 'host'],
            'VALKEY_PORT': ['valkey', 'port'],
            'VALKEY_USE_TLS': ['valkey', 'use_tls'],
            'VALKEY_USE_CLUSTER': ['valkey', 'use_cluster'],
            'SERVER_HOST': ['server', 'host'],
            'SERVER_PORT': ['server', 'port'],
            'DEBUG_MODE': ['server', 'debug_mode'],
            'LOG_LEVEL': ['logging', 'level'],
        }
        
        for env_var, config_path in env_mappings.items():
            env_value = os.getenv(env_var)
            if env_value is not None:
                # Convert string values to appropriate types
                converted_value = self._convert_env_value(env_value, config_path)
                
                # Set the value in the config dictionary
                current_dict = config_data
                for key in config_path[:-1]:
                    if key not in current_dict:
                        current_dict[key] = {}
                    current_dict = current_dict[key]
                current_dict[config_path[-1]] = converted_value
                
                logger.info(f"Environment override: {env_var} = {converted_value}")
        
        return config_data
    
    def _convert_env_value(self, value: str, config_path: list) -> Any:
        """Convert environment variable string to appropriate type"""
        # Port numbers should be integers
        if config_path[-1] == 'port':
            try:
                return int(value)
            except ValueError:
                logger.warning(f"Invalid port number: {value}, using default")
                return None
        
        # Boolean values
        if config_path[-1] in ['use_tls', 'use_cluster', 'debug_mode']:
            return value.lower() in ['true', '1', 'yes', 'on']
        
        # Return as string for other values
        return value
    
    def get_config(self) -> Config:
        """Get the current configuration, loading it if necessary"""
        if self._config is None:
            return self.load_config()
        return self._config
    
    def reload_config(self) -> Config:
        """Reload configuration from files"""
        self._config = None
        return self.load_config()

# Global configuration manager instance
_config_manager = ConfigManager()

def get_config() -> Config:
    """Get the global configuration instance"""
    return _config_manager.get_config()

def reload_config() -> Config:
    """Reload the global configuration"""
    return _config_manager.reload_config()

# Convenience function for backward compatibility
def get_valkey_config() -> ValkeyConfig:
    """Get Valkey configuration"""
    return get_config().valkey

def get_server_config() -> ServerConfig:
    """Get server configuration"""
    return get_config().server

def get_logging_config() -> LoggingConfig:
    """Get logging configuration"""
    return get_config().logging

def get_execute_config() -> ExecuteConfig:
    """Get execute configuration"""
    return get_config().execute

def get_app_config() -> AppConfig:
    """Get application configuration"""
    return get_config().app
