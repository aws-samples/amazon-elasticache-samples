#!/usr/bin/env python3
"""
Aerospike to Valkey Mapping Engine
Reads mapping configuration and applies transformations based on rules
"""

import yaml
import json
import logging
import re
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass

# T4: the mapping configuration controls destination key names, Valkey commands
# and connection endpoints, so it must be verified rather than trusted.
from config_security import ConfigSecurityError, load_trusted_config

@dataclass
class MappingResult:
    """Result of a mapping operation"""
    valkey_type: str
    valkey_command: str
    key: str
    value: Any
    ttl: Optional[int] = None
    additional_params: Dict[str, Any] = None

class MappingEngine:
    """Engine that applies mapping rules to convert Aerospike data to Valkey format"""
    
    def __init__(self, config_file: str = "mapping_config.yaml",
                 expected_sha256: Optional[str] = None):
        """Initialize the mapping engine with a verified configuration.

        ``expected_sha256`` pins the config's contents; see
        :mod:`config_security` for the full set of trust checks (threat T4).
        """
        self.logger = logging.getLogger(__name__)
        self.config_path: Optional[str] = None
        self.config_sha256: Optional[str] = None
        self.config = self._load_config(config_file, expected_sha256)

    def _load_config(self, config_file: str,
                     expected_sha256: Optional[str] = None) -> Dict[str, Any]:
        """Load the mapping configuration after verifying it can be trusted.

        The config decides where data is written and under what key names, so a
        tampered or attacker-supplied file is a data-redirection vector (threat
        T4). Ownership, permissions, integrity pin and schema are all checked
        before any value here is used.
        """
        try:
            config, path, digest, warnings = load_trusted_config(
                config_file, expected_sha256
            )
        except ConfigSecurityError as e:
            self.logger.error(f"Refusing to use mapping config {config_file}: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Failed to load mapping config: {e}")
            raise

        for warning in warnings:
            self.logger.warning(warning)

        self.config_path = path
        self.config_sha256 = digest
        self.logger.info(
            f"Loaded verified mapping configuration from {path} (sha256={digest})"
        )
        return config

    def get_connection_settings(self) -> Dict[str, Any]:
        """Get connection settings from configuration"""
        return self.config.get('connection_settings', {})
    
    def map_record(self, record: Tuple, prefix: str) -> List[MappingResult]:
        """
        Map an Aerospike record to Valkey operations
        Returns list of MappingResult objects
        """
        key, meta, bins = record
        record_id = self._extract_key(key)
        namespace = key[0] if len(key) > 0 else "default"
        set_name = key[1] if len(key) > 1 else "default"
        ttl = meta.get('ttl')
        
        # Determine mapping strategy
        strategy = self._get_mapping_strategy(namespace, set_name, bins)
        
        results = []
        
        if strategy == "direct" and len(bins) == 1:
            # Single bin record - direct mapping
            results.extend(self._map_single_bin_direct(record_id, bins, ttl, namespace, set_name))
            
        elif strategy == "json_document":
            # Store entire record as JSON document
            results.extend(self._map_as_json_document(record_id, bins, ttl, namespace, set_name))
            
        elif strategy == "separate_keys":
            # Store each bin as separate key
            results.extend(self._map_separate_keys(record_id, bins, ttl, namespace, set_name))
            
        elif strategy == "hybrid":
            # Mix of strategies based on data types
            results.extend(self._map_hybrid(record_id, bins, ttl, namespace, set_name))
        
        return results
    
    def _get_mapping_strategy(self, namespace: str, set_name: str, bins: Dict[str, Any]) -> str:
        """Determine which mapping strategy to use"""
        
        # Check namespace-specific settings
        ns_config = self.config.get('namespace_mappings', {}).get(namespace, {})
        set_config = ns_config.get('sets', {}).get(set_name, {})
        
        if 'strategy' in set_config:
            return set_config['strategy']
        
        if 'strategy' in ns_config:
            return ns_config['strategy']
        
        # Check global settings
        if len(bins) == 1:
            return self.config['global_settings']['single_bin_strategy']
        else:
            return self.config['global_settings']['multi_bin_strategy']
    
    def _map_single_bin_direct(self, record_id: str, bins: Dict[str, Any], 
                              ttl: Optional[int], namespace: str, set_name: str) -> List[MappingResult]:
        """Map single bin record directly (no bin name in key)"""
        field_name, field_value = next(iter(bins.items()))
        
        key = self._format_key(namespace, set_name, record_id)
        mapping = self._determine_value_mapping(field_name, field_value, namespace, set_name)
        
        return [MappingResult(
            valkey_type=mapping['valkey_type'],
            valkey_command=mapping['valkey_command'],
            key=key,
            value=self._transform_value(field_value, mapping),
            ttl=ttl,
            additional_params=mapping.get('params', {})
        )]
    
    def _map_separate_keys(self, record_id: str, bins: Dict[str, Any], 
                          ttl: Optional[int], namespace: str, set_name: str) -> List[MappingResult]:
        """Map each bin to separate Valkey key"""
        results = []
        
        for field_name, field_value in bins.items():
            key = self._format_multi_key(namespace, set_name, record_id, field_name)
            mapping = self._determine_value_mapping(field_name, field_value, namespace, set_name)
            
            results.append(MappingResult(
                valkey_type=mapping['valkey_type'],
                valkey_command=mapping['valkey_command'],
                key=key,
                value=self._transform_value(field_value, mapping),
                ttl=ttl,
                additional_params=mapping.get('params', {})
            ))
        
        return results
    
    def _map_as_json_document(self, record_id: str, bins: Dict[str, Any], 
                             ttl: Optional[int], namespace: str, set_name: str) -> List[MappingResult]:
        """Map entire record as single JSON document"""
        key = self._format_key(namespace, set_name, record_id)
        
        # Transform all values according to their mappings
        transformed_bins = {}
        for field_name, field_value in bins.items():
            mapping = self._determine_value_mapping(field_name, field_value, namespace, set_name)
            transformed_bins[field_name] = self._transform_value(field_value, mapping)
        
        return [MappingResult(
            valkey_type="json",
            valkey_command="JSON.SET",
            key=key,
            value=transformed_bins,
            ttl=ttl,
            additional_params={"path": "$"}
        )]
    
    def _map_hybrid(self, record_id: str, bins: Dict[str, Any], 
                   ttl: Optional[int], namespace: str, set_name: str) -> List[MappingResult]:
        """Hybrid mapping - scalars separate, complex as JSON"""
        results = []
        complex_bins = {}
        
        for field_name, field_value in bins.items():
            if self._is_scalar_type(field_value):
                # Store scalar as separate key
                key = self._format_multi_key(namespace, set_name, record_id, field_name)
                mapping = self._determine_value_mapping(field_name, field_value, namespace, set_name)
                
                results.append(MappingResult(
                    valkey_type=mapping['valkey_type'],
                    valkey_command=mapping['valkey_command'],
                    key=key,
                    value=self._transform_value(field_value, mapping),
                    ttl=ttl
                ))
            else:
                # Collect complex fields for JSON document
                complex_bins[field_name] = field_value
        
        # Store complex fields as JSON document if any exist
        if complex_bins:
            json_key = self._format_multi_key(namespace, set_name, record_id, "complex")
            results.append(MappingResult(
                valkey_type="json",
                valkey_command="JSON.SET",
                key=json_key,
                value=complex_bins,
                ttl=ttl,
                additional_params={"path": "$"}
            ))
        
        return results
    
    def _determine_value_mapping(self, field_name: str, field_value: Any, 
                                namespace: str, set_name: str) -> Dict[str, Any]:
        """Determine how to map a specific field value"""
        
        # Check field-specific mappings first (highest priority)
        field_mapping = self._get_field_mapping(field_name)
        if field_mapping:
            return field_mapping
        
        # Check namespace/set specific mappings
        ns_mapping = self._get_namespace_field_mapping(namespace, set_name, field_name)
        if ns_mapping:
            return ns_mapping
        
        # Determine by data type (field_name is required for opt-in HLL detection)
        return self._get_data_type_mapping(field_value, field_name)
    
    def _get_field_mapping(self, field_name: str) -> Optional[Dict[str, Any]]:
        """Get mapping based on field name patterns"""
        field_patterns = self.config.get('field_mappings', {}).get('field_name_patterns', {})
        
        for pattern, mapping in field_patterns.items():
            if self._matches_pattern(field_name, pattern):
                return mapping
        
        return None
    
    def _get_namespace_field_mapping(self, namespace: str, set_name: str, field_name: str) -> Optional[Dict[str, Any]]:
        """Get mapping from namespace/set specific configuration"""
        ns_config = self.config.get('namespace_mappings', {}).get(namespace, {})
        set_config = ns_config.get('sets', {}).get(set_name, {})
        bin_mappings = set_config.get('bin_mappings', {})
        
        return bin_mappings.get(field_name)
    
    def _get_data_type_mapping(self, value: Any, field_name: Optional[str] = None) -> Dict[str, Any]:
        """Get mapping based on data type analysis"""
        
        if self._is_scalar_type(value):
            return self._get_scalar_mapping(value)
        elif isinstance(value, list):
            # Only coerce a list to HyperLogLog when the field has explicitly
            # opted in (threat T13). Otherwise preserve it as a real list.
            if self._is_hyperloglog(value, field_name):
                return self._get_hyperloglog_mapping()
            return self._get_list_mapping(value)
        elif isinstance(value, dict):
            return self._get_map_mapping(value)
        elif self._is_geojson(value):
            return self._get_geojson_mapping(value)
        else:
            return self._get_fallback_mapping()
    
    def _get_scalar_mapping(self, value: Any) -> Dict[str, Any]:
        """Get mapping for scalar values"""
        scalar_mappings = self.config['data_type_mappings']['scalar_types']
        
        if isinstance(value, int):
            return scalar_mappings['integer']
        elif isinstance(value, float):
            return scalar_mappings['float']
        elif isinstance(value, str):
            return scalar_mappings['string']
        elif isinstance(value, bool):
            return scalar_mappings['boolean']
        elif isinstance(value, (bytes, bytearray)):
            return scalar_mappings['binary']
        else:
            return self._get_fallback_mapping()
    
    def _get_list_mapping(self, lst: List[Any]) -> Dict[str, Any]:
        """Get mapping for list values"""
        list_mappings = self.config['data_type_mappings']['collection_types']['list']
        
        if self._is_simple_list(lst):
            return list_mappings['simple_list']
        elif self._has_nested_structures(lst):
            return list_mappings['nested_list']
        else:
            return list_mappings['complex_list']
    
    def _get_map_mapping(self, dct: Dict[str, Any]) -> Dict[str, Any]:
        """Get mapping for map/dict values"""
        map_mappings = self.config['data_type_mappings']['collection_types']['map']
        
        if self._is_simple_map(dct):
            return map_mappings['simple_map']
        elif self._has_nested_structures(list(dct.values())):
            return map_mappings['nested_map']
        else:
            return map_mappings['complex_map']
    
    def _get_geojson_mapping(self, geojson: Dict[str, Any]) -> Dict[str, Any]:
        """Get mapping for GeoJSON values"""
        geojson_mappings = self.config['data_type_mappings']['special_types']['geojson']
        
        geojson_type = geojson.get('type', '').lower()
        if geojson_type == 'point':
            return geojson_mappings['point']
        elif geojson_type == 'polygon':
            return geojson_mappings['polygon']
        else:
            return geojson_mappings['other']
    
    def _get_hyperloglog_mapping(self) -> Dict[str, Any]:
        """Get mapping for HyperLogLog values"""
        return self.config['data_type_mappings']['special_types']['hyperloglog']
    
    def _get_fallback_mapping(self) -> Dict[str, Any]:
        """Get fallback mapping for unknown types"""
        return {
            'valkey_type': 'string',
            'valkey_command': 'SET',
            'encoding': 'json'
        }
    
    def _transform_value(self, value: Any, mapping: Dict[str, Any]) -> Any:
        """Transform value according to mapping rules"""
        
        if mapping['valkey_type'] == 'native':
            return value  # Keep native type
        
        elif mapping['valkey_type'] == 'string':
            if mapping.get('encoding') == 'base64' and isinstance(value, (bytes, bytearray)):
                import base64
                encoded = base64.b64encode(value).decode('utf-8')
                prefix = mapping.get('prefix', '')
                return f"{prefix}{encoded}"
            elif mapping.get('encoding') == 'json':
                return json.dumps(value)
            else:
                return str(value)
        
        elif mapping['valkey_type'] == 'list':
            if isinstance(value, list):
                convert_items = mapping.get('convert_items', 'string')
                if convert_items == 'string':
                    return [str(item) for item in value]
                return value
            return [str(value)]
        
        elif mapping['valkey_type'] == 'hash':
            if isinstance(value, dict):
                convert_values = mapping.get('convert_values', 'string')
                if convert_values == 'string':
                    return {k: str(v) for k, v in value.items()}
                return value
            return {'value': str(value)}
        
        elif mapping['valkey_type'] == 'json':
            return value  # Will be JSON serialized later
        
        return value
    
    def _format_key(self, namespace: str, set_name: str, record_id: str) -> str:
        """Format key for single bin records"""
        template = self.config['global_settings']['key_format']
        return template.format(
            namespace=namespace,
            set=set_name,
            primary_key=record_id
        )
    
    def _format_multi_key(self, namespace: str, set_name: str, record_id: str, bin_name: str) -> str:
        """Format key for multi-bin records"""
        template = self.config['global_settings']['multi_key_format']
        return template.format(
            namespace=namespace,
            set=set_name,
            primary_key=record_id,
            bin_name=bin_name
        )
    
    def _extract_key(self, key: Tuple) -> str:
        """Extract primary key from Aerospike key tuple"""
        if len(key) > 2 and key[2] is not None:
            return str(key[2])
        elif len(key) > 3 and key[3] is not None:
            if isinstance(key[3], (bytes, bytearray)):
                return key[3].hex()
            else:
                return str(key[3])
        else:
            import uuid
            return str(uuid.uuid4())
    
    def _is_scalar_type(self, value: Any) -> bool:
        """Check if value is a scalar type"""
        return isinstance(value, (int, float, str, bool, bytes, bytearray))
    
    def _is_simple_list(self, lst: List[Any]) -> bool:
        """Check if list contains only scalar values"""
        detection_rules = self.config['detection_rules']['simple_list']
        allowed_types = detection_rules['allowed_types']
        
        type_map = {'int': int, 'float': float, 'str': str, 'bool': bool}
        allowed_python_types = tuple(type_map[t] for t in allowed_types if t in type_map)
        
        return all(isinstance(item, allowed_python_types) for item in lst)
    
    def _is_simple_map(self, dct: Dict[str, Any]) -> bool:
        """Check if map contains only scalar values"""
        detection_rules = self.config['detection_rules']['simple_map']
        allowed_types = detection_rules['allowed_value_types']
        
        type_map = {'int': int, 'float': float, 'str': str, 'bool': bool}
        allowed_python_types = tuple(type_map[t] for t in allowed_types if t in type_map)
        
        return all(isinstance(value, allowed_python_types) for value in dct.values())
    
    def _has_nested_structures(self, values: List[Any]) -> bool:
        """Check if values contain nested lists or dicts"""
        return any(isinstance(v, (list, dict)) for v in values)
    
    def _is_geojson(self, value: Any) -> bool:
        """Check if value is GeoJSON"""
        if not isinstance(value, dict):
            return False
        
        detection_rules = self.config['detection_rules']['geojson']
        required_fields = detection_rules['required_fields']
        supported_types = detection_rules['supported_types']
        
        # Check required fields
        if not all(field in value for field in required_fields):
            return False
        
        # Check if type is supported
        return value.get('type') in supported_types
    
    def _is_hyperloglog(self, value: Any, field_name: Optional[str] = None) -> bool:
        """Check if value should be treated as HyperLogLog.

        HyperLogLog conversion is lossy (values are replaced by a probabilistic
        cardinality estimate). To avoid silently and irreversibly destroying data
        (threat T13), conversion now requires an explicit field-level opt-in:
        the field name must match one of the configured
        ``detection_rules.hyperloglog.field_name_patterns``. Size/duplicate
        heuristics alone are no longer sufficient.
        """
        if not isinstance(value, list):
            return False

        detection_rules = self.config['detection_rules']['hyperloglog']

        # Require explicit opt-in via field name pattern.
        patterns = detection_rules.get('field_name_patterns', [])
        if not field_name or not any(self._matches_pattern(field_name, p) for p in patterns):
            return False

        # Still enforce the minimum size as a sanity check.
        if len(value) < detection_rules.get('min_list_size', 0):
            return False

        return True
    
    def _matches_pattern(self, text: str, pattern: str) -> bool:
        """Check if text matches a pattern (supports wildcards)"""
        # Convert wildcard pattern to regex
        regex_pattern = pattern.replace('*', '.*')
        return re.match(f"^{regex_pattern}$", text, re.IGNORECASE) is not None

# Example usage
if __name__ == "__main__":
    # Test the mapping engine
    engine = MappingEngine()
    
    # Test record
    test_record = (
        ('test', 'million_data', '1'),  # key
        {'ttl': 3600, 'gen': 1},        # meta
        {                               # bins
            'bin1': 123456789,
            'bin2': 'sample_string',
            'bin3': 3.14159,
            'bin4': [42, 84],
            'bin5': {'key1': 1, 'key2': 2}
        }
    )
    
    results = engine.map_record(test_record, "test")
    
    print("Mapping Results:")
    for i, result in enumerate(results, 1):
        print(f"{i}. {result.valkey_command} {result.key} → {result.value}")
        print(f"   Type: {result.valkey_type}, TTL: {result.ttl}")
