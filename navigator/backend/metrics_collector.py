import logging
import time
import concurrent.futures
from typing import Dict, Any, Optional
from datetime import datetime
import psutil
from valkey_client import ValkeyClient

logger = logging.getLogger(__name__)

class MetricsCollector:
    def __init__(self, valkey_client: ValkeyClient, individual_timeout: float = 5.0):
        self.valkey_client = valkey_client
        self.individual_timeout = individual_timeout
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        
    def _safe_get_info_with_timeout(self, section: Optional[str] = None, timeout: float = None) -> Dict[str, Any]:
        """
        Get Redis/Valkey info with timeout and enhanced error handling
        """
        timeout = timeout or self.individual_timeout
        method_name = f"get_info('{section}')"
        
        try:
            logger.debug(f"⏰ Starting {method_name} with timeout {timeout}s")
            start_time = time.time()
            
            # Use concurrent.futures for timeout control
            future = self.executor.submit(self.valkey_client.get_info, section)
            info_result = future.result(timeout=timeout)
            
            elapsed = time.time() - start_time
            logger.info(f"✅ {method_name} completed in {elapsed:.2f}s, got {len(info_result)} fields")
            return info_result
            
        except concurrent.futures.TimeoutError:
            elapsed = time.time() - start_time
            logger.error(f"⏱️  TIMEOUT: {method_name} exceeded {timeout}s (actual: {elapsed:.2f}s)")
            return {}
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"❌ FAILED: {method_name} after {elapsed:.2f}s - {str(e)}")
            logger.error(f"Connection status - Connected: {self.valkey_client.is_connected()}")
            logger.error(f"Redis client type: {type(self.valkey_client.client)}")
            logger.error(f"Exception type: {type(e).__name__}")
            
            # Quick ping test for debugging
            try:
                ping_future = self.executor.submit(self.valkey_client.client.ping)
                ping_result = ping_future.result(timeout=1.0)
                logger.error(f"PING test result: {ping_result}")
            except Exception as ping_e:
                logger.error(f"PING test also failed: {ping_e}")
            
            return {}

    def _safe_get_client_list_with_timeout(self, timeout: float = None) -> list:
        """
        Safely get client list with timeout
        """
        timeout = timeout or self.individual_timeout
        try:
            logger.debug(f"⏰ Starting get_client_list with timeout {timeout}s")
            start_time = time.time()
            
            future = self.executor.submit(self.valkey_client.get_client_list)
            client_list = future.result(timeout=timeout)
            
            elapsed = time.time() - start_time
            logger.info(f"✅ get_client_list completed in {elapsed:.2f}s, found {len(client_list)} clients")
            return client_list
            
        except concurrent.futures.TimeoutError:
            elapsed = time.time() - start_time
            logger.error(f"⏱️  TIMEOUT: get_client_list exceeded {timeout}s (actual: {elapsed:.2f}s)")
            return []
        except Exception as e:
            elapsed = time.time() - start_time
            logger.warning(f"❌ Error getting client list after {elapsed:.2f}s: {str(e)}")
            return []
    
    def _get_system_metrics_with_timeout(self, timeout: float = 2.0) -> Dict[str, Any]:
        """
        Safely get system metrics with timeout
        """
        try:
            logger.debug(f"⏰ Starting system metrics collection with timeout {timeout}s")
            start_time = time.time()
            
            # Use executor for system calls to prevent blocking
            def get_system_info():
                cpu_percent = psutil.cpu_percent(interval=0.1)
                cpu_count = psutil.cpu_count()
                memory = psutil.virtual_memory()
                return {
                    "system_cpu_percent": cpu_percent,
                    "system_cpu_count": cpu_count,
                    "system_memory_total": memory.total,
                    "system_memory_available": memory.available,
                    "system_memory_percent": memory.percent,
                    "system_memory_used": memory.used,
                    "system_memory_free": memory.free,
                }
            
            future = self.executor.submit(get_system_info)
            result = future.result(timeout=timeout)
            
            elapsed = time.time() - start_time
            logger.info(f"✅ System metrics collected in {elapsed:.2f}s")
            return result
            
        except concurrent.futures.TimeoutError:
            elapsed = time.time() - start_time
            logger.error(f"⏱️  TIMEOUT: System metrics exceeded {timeout}s (actual: {elapsed:.2f}s)")
            return self._get_fallback_system_metrics()
        except Exception as e:
            elapsed = time.time() - start_time
            logger.warning(f"❌ Error getting system metrics after {elapsed:.2f}s: {str(e)}")
            return self._get_fallback_system_metrics()
    
    def _get_fallback_system_metrics(self) -> Dict[str, Any]:
        """Get fallback system metrics when main collection fails"""
        return {
            "system_cpu_percent": 0.0,
            "system_cpu_count": 0,
            "system_memory_total": 0,
            "system_memory_available": 0,
            "system_memory_percent": 0.0,
            "system_memory_used": 0,
            "system_memory_free": 0,
        }

    def _collect_metric_with_timing(self, metric_name: str, collect_func, fallback_data: dict) -> Dict[str, Any]:
        """
        Collect a metric with timing and error handling
        """
        logger.info(f"🚀 Starting {metric_name} collection")
        start_time = time.time()
        
        try:
            result = collect_func()
            elapsed = time.time() - start_time
            logger.info(f"✅ {metric_name} completed successfully in {elapsed:.2f}s")
            return result
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"❌ {metric_name} failed after {elapsed:.2f}s: {str(e)}")
            # Add timing info to fallback data
            fallback_data.update({
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(e),
                "collection_time_seconds": elapsed
            })
            return fallback_data

    def get_server_metrics(self) -> Dict[str, Any]:
        """Get server-related metrics including CPU and system info"""
        def collect():
            info = self._safe_get_info_with_timeout("server")
            system_metrics = self._get_system_metrics_with_timeout()
            
            return {
                "redis_version": info.get("redis_version", "unknown"),
                "redis_mode": info.get("redis_mode", "unknown"),
                "os": info.get("os", "unknown"),
                "arch_bits": info.get("arch_bits", 0),
                "multiplexing_api": info.get("multiplexing_api", "unknown"),
                "process_id": info.get("process_id", 0),
                "uptime_in_seconds": info.get("uptime_in_seconds", 0),
                "uptime_in_days": info.get("uptime_in_days", 0),
                "hz": info.get("hz", 0),
                "configured_hz": info.get("configured_hz", 0),
                "lru_clock": info.get("lru_clock", 0),
                "executable": info.get("executable", ""),
                "config_file": info.get("config_file", ""),
                **system_metrics,
                "timestamp": datetime.utcnow().isoformat()
            }
        
        fallback = {
            "redis_version": "unknown", "redis_mode": "unknown", "os": "unknown",
            "arch_bits": 0, "multiplexing_api": "unknown", "process_id": 0,
            "uptime_in_seconds": 0, "uptime_in_days": 0, "hz": 0, "configured_hz": 0,
            "lru_clock": 0, "executable": "", "config_file": "",
            **self._get_fallback_system_metrics()
        }
        
        return self._collect_metric_with_timing("server_metrics", collect, fallback)

    def get_memory_metrics(self) -> Dict[str, Any]:
        """Get memory-related metrics"""
        def collect():
            info = self._safe_get_info_with_timeout("memory")
            system_metrics = self._get_system_metrics_with_timeout()
            
            return {
                "used_memory": info.get("used_memory", 0),
                "used_memory_human": info.get("used_memory_human", "0B"),
                "used_memory_rss": info.get("used_memory_rss", 0),
                "used_memory_rss_human": info.get("used_memory_rss_human", "0B"),
                "used_memory_peak": info.get("used_memory_peak", 0),
                "used_memory_peak_human": info.get("used_memory_peak_human", "0B"),
                "used_memory_peak_perc": info.get("used_memory_peak_perc", "0.00%"),
                "used_memory_overhead": info.get("used_memory_overhead", 0),
                "used_memory_startup": info.get("used_memory_startup", 0),
                "used_memory_dataset": info.get("used_memory_dataset", 0),
                "used_memory_dataset_perc": info.get("used_memory_dataset_perc", "0.00%"),
                "allocator_allocated": info.get("allocator_allocated", 0),
                "allocator_active": info.get("allocator_active", 0),
                "allocator_resident": info.get("allocator_resident", 0),
                "total_system_memory": info.get("total_system_memory", 0),
                "total_system_memory_human": info.get("total_system_memory_human", "0B"),
                "used_memory_lua": info.get("used_memory_lua", 0),
                "used_memory_lua_human": info.get("used_memory_lua_human", "0B"),
                "used_memory_scripts": info.get("used_memory_scripts", 0),
                "used_memory_scripts_human": info.get("used_memory_scripts_human", "0B"),
                "number_of_cached_scripts": info.get("number_of_cached_scripts", 0),
                "maxmemory": info.get("maxmemory", 0),
                "maxmemory_human": info.get("maxmemory_human", "0B"),
                "maxmemory_policy": info.get("maxmemory_policy", "unknown"),
                "allocator_frag_ratio": info.get("allocator_frag_ratio", 0.0),
                "allocator_frag_bytes": info.get("allocator_frag_bytes", 0),
                "allocator_rss_ratio": info.get("allocator_rss_ratio", 0.0),
                "allocator_rss_bytes": info.get("allocator_rss_bytes", 0),
                "rss_overhead_ratio": info.get("rss_overhead_ratio", 0.0),
                "rss_overhead_bytes": info.get("rss_overhead_bytes", 0),
                "mem_fragmentation_ratio": info.get("mem_fragmentation_ratio", 0.0),
                "mem_fragmentation_bytes": info.get("mem_fragmentation_bytes", 0),
                "mem_not_counted_for_evict": info.get("mem_not_counted_for_evict", 0),
                "mem_replication_backlog": info.get("mem_replication_backlog", 0),
                "mem_clients_slaves": info.get("mem_clients_slaves", 0),
                "mem_clients_normal": info.get("mem_clients_normal", 0),
                "mem_aof_buffer": info.get("mem_aof_buffer", 0),
                "system_memory_total": system_metrics.get("system_memory_total", 0),
                "system_memory_available": system_metrics.get("system_memory_available", 0),
                "system_memory_percent": system_metrics.get("system_memory_percent", 0.0),
                "system_memory_used": system_metrics.get("system_memory_used", 0),
                "system_memory_free": system_metrics.get("system_memory_free", 0),
                "timestamp": datetime.utcnow().isoformat()
            }
        
        fallback = {
            "used_memory": 0, "used_memory_human": "0B", "used_memory_rss": 0,
            "used_memory_rss_human": "0B", "used_memory_peak": 0, "used_memory_peak_human": "0B",
            "used_memory_peak_perc": "0.00%", "used_memory_overhead": 0, "used_memory_startup": 0,
            "used_memory_dataset": 0, "used_memory_dataset_perc": "0.00%", "allocator_allocated": 0,
            "allocator_active": 0, "allocator_resident": 0, "total_system_memory": 0,
            "total_system_memory_human": "0B", "used_memory_lua": 0, "used_memory_lua_human": "0B",
            "used_memory_scripts": 0, "used_memory_scripts_human": "0B", "number_of_cached_scripts": 0,
            "maxmemory": 0, "maxmemory_human": "0B", "maxmemory_policy": "unknown",
            "allocator_frag_ratio": 0.0, "allocator_frag_bytes": 0, "allocator_rss_ratio": 0.0,
            "allocator_rss_bytes": 0, "rss_overhead_ratio": 0.0, "rss_overhead_bytes": 0,
            "mem_fragmentation_ratio": 0.0, "mem_fragmentation_bytes": 0, "mem_not_counted_for_evict": 0,
            "mem_replication_backlog": 0, "mem_clients_slaves": 0, "mem_clients_normal": 0,
            "mem_aof_buffer": 0, **self._get_fallback_system_metrics()
        }
        
        return self._collect_metric_with_timing("memory_metrics", collect, fallback)

    def get_connection_metrics(self) -> Dict[str, Any]:
        """Get connection-related metrics"""
        def collect():
            info = self._safe_get_info_with_timeout("clients")
            client_list = self._safe_get_client_list_with_timeout()
            
            return {
                "connected_clients": info.get("connected_clients", 0),
                "client_recent_max_input_buffer": info.get("client_recent_max_input_buffer", 0),
                "client_recent_max_output_buffer": info.get("client_recent_max_output_buffer", 0),
                "blocked_clients": info.get("blocked_clients", 0),
                "tracking_clients": info.get("tracking_clients", 0),
                "clients_in_timeout_table": info.get("clients_in_timeout_table", 0),
                "total_client_connections": len(client_list),
                "client_list_sample": client_list[:5] if client_list else [],
                "timestamp": datetime.utcnow().isoformat()
            }
        
        fallback = {
            "connected_clients": 0, "client_recent_max_input_buffer": 0,
            "client_recent_max_output_buffer": 0, "blocked_clients": 0,
            "tracking_clients": 0, "clients_in_timeout_table": 0,
            "total_client_connections": 0, "client_list_sample": []
        }
        
        return self._collect_metric_with_timing("connection_metrics", collect, fallback)

    def get_command_stats(self) -> Dict[str, Any]:
        """Get command statistics and performance metrics"""
        def collect():
            info = self._safe_get_info_with_timeout("stats")
            
            return {
                "total_connections_received": info.get("total_connections_received", 0),
                "total_commands_processed": info.get("total_commands_processed", 0),
                "instantaneous_ops_per_sec": info.get("instantaneous_ops_per_sec", 0),
                "total_net_input_bytes": info.get("total_net_input_bytes", 0),
                "total_net_output_bytes": info.get("total_net_output_bytes", 0),
                "instantaneous_input_kbps": info.get("instantaneous_input_kbps", 0.0),
                "instantaneous_output_kbps": info.get("instantaneous_output_kbps", 0.0),
                "rejected_connections": info.get("rejected_connections", 0),
                "sync_full": info.get("sync_full", 0),
                "sync_partial_ok": info.get("sync_partial_ok", 0),
                "sync_partial_err": info.get("sync_partial_err", 0),
                "expired_keys": info.get("expired_keys", 0),
                "expired_stale_perc": info.get("expired_stale_perc", 0.0),
                "expired_time_cap_reached_count": info.get("expired_time_cap_reached_count", 0),
                "expire_cycle_cpu_milliseconds": info.get("expire_cycle_cpu_milliseconds", 0),
                "evicted_keys": info.get("evicted_keys", 0),
                "keyspace_hits": info.get("keyspace_hits", 0),
                "keyspace_misses": info.get("keyspace_misses", 0),
                "pubsub_channels": info.get("pubsub_channels", 0),
                "pubsub_patterns": info.get("pubsub_patterns", 0),
                "latest_fork_usec": info.get("latest_fork_usec", 0),
                "migrate_cached_sockets": info.get("migrate_cached_sockets", 0),
                "slave_expires_tracked_keys": info.get("slave_expires_tracked_keys", 0),
                "active_defrag_hits": info.get("active_defrag_hits", 0),
                "active_defrag_misses": info.get("active_defrag_misses", 0),
                "active_defrag_key_hits": info.get("active_defrag_key_hits", 0),
                "active_defrag_key_misses": info.get("active_defrag_key_misses", 0),
                "tracking_total_keys": info.get("tracking_total_keys", 0),
                "tracking_total_items": info.get("tracking_total_items", 0),
                "tracking_total_prefixes": info.get("tracking_total_prefixes", 0),
                "unexpected_error_replies": info.get("unexpected_error_replies", 0),
                "total_reads_processed": info.get("total_reads_processed", 0),
                "total_writes_processed": info.get("total_writes_processed", 0),
                "io_threaded_reads_processed": info.get("io_threaded_reads_processed", 0),
                "io_threaded_writes_processed": info.get("io_threaded_writes_processed", 0),
                "timestamp": datetime.utcnow().isoformat()
            }
        
        fallback = {
            "total_connections_received": 0, "total_commands_processed": 0,
            "instantaneous_ops_per_sec": 0, "total_net_input_bytes": 0,
            "total_net_output_bytes": 0, "instantaneous_input_kbps": 0.0,
            "instantaneous_output_kbps": 0.0, "rejected_connections": 0,
            "sync_full": 0, "sync_partial_ok": 0, "sync_partial_err": 0,
            "expired_keys": 0, "expired_stale_perc": 0.0, "expired_time_cap_reached_count": 0,
            "expire_cycle_cpu_milliseconds": 0, "evicted_keys": 0, "keyspace_hits": 0,
            "keyspace_misses": 0, "pubsub_channels": 0, "pubsub_patterns": 0,
            "latest_fork_usec": 0, "migrate_cached_sockets": 0, "slave_expires_tracked_keys": 0,
            "active_defrag_hits": 0, "active_defrag_misses": 0, "active_defrag_key_hits": 0,
            "active_defrag_key_misses": 0, "tracking_total_keys": 0, "tracking_total_items": 0,
            "tracking_total_prefixes": 0, "unexpected_error_replies": 0, "total_reads_processed": 0,
            "total_writes_processed": 0, "io_threaded_reads_processed": 0, "io_threaded_writes_processed": 0
        }
        
        return self._collect_metric_with_timing("command_stats", collect, fallback)

    def get_cluster_metrics(self) -> Dict[str, Any]:
        """Get cluster-specific metrics"""
        def collect():
            info = self._safe_get_info_with_timeout("cluster")
            replication_info = self._safe_get_info_with_timeout("replication")
            
            return {
                "cluster_enabled": info.get("cluster_enabled", 0),
                "role": replication_info.get("role", "unknown"),
                "connected_slaves": replication_info.get("connected_slaves", 0),
                "master_failover_state": replication_info.get("master_failover_state", "no-failover"),
                "master_replid": replication_info.get("master_replid", ""),
                "master_replid2": replication_info.get("master_replid2", ""),
                "master_repl_offset": replication_info.get("master_repl_offset", 0),
                "second_repl_offset": replication_info.get("second_repl_offset", -1),
                "repl_backlog_active": replication_info.get("repl_backlog_active", 0),
                "repl_backlog_size": replication_info.get("repl_backlog_size", 0),
                "repl_backlog_first_byte_offset": replication_info.get("repl_backlog_first_byte_offset", 0),
                "repl_backlog_histlen": replication_info.get("repl_backlog_histlen", 0),
                "timestamp": datetime.utcnow().isoformat()
            }
        
        fallback = {
            "cluster_enabled": 0, "role": "unknown", "connected_slaves": 0,
            "master_failover_state": "no-failover", "master_replid": "", "master_replid2": "",
            "master_repl_offset": 0, "second_repl_offset": -1, "repl_backlog_active": 0,
            "repl_backlog_size": 0, "repl_backlog_first_byte_offset": 0, "repl_backlog_histlen": 0
        }
        
        return self._collect_metric_with_timing("cluster_metrics", collect, fallback)

    def get_performance_metrics(self) -> Dict[str, Any]:
        """Get performance-related metrics"""
        def collect():
            stats_info = self._safe_get_info_with_timeout("stats")
            commandstats_info = self._safe_get_info_with_timeout("commandstats")
            
            # Calculate hit ratio
            hits = stats_info.get("keyspace_hits", 0)
            misses = stats_info.get("keyspace_misses", 0)
            total_requests = hits + misses
            hit_ratio = (hits / total_requests * 100) if total_requests > 0 else 0
            
            return {
                "hit_ratio_percent": round(hit_ratio, 2),
                "keyspace_hits": hits,
                "keyspace_misses": misses,
                "total_requests": total_requests,
                "ops_per_sec": stats_info.get("instantaneous_ops_per_sec", 0),
                "input_kbps": stats_info.get("instantaneous_input_kbps", 0.0),
                "output_kbps": stats_info.get("instantaneous_output_kbps", 0.0),
                "used_cpu_sys": stats_info.get("used_cpu_sys", 0.0),
                "used_cpu_user": stats_info.get("used_cpu_user", 0.0),
                "used_cpu_sys_children": stats_info.get("used_cpu_sys_children", 0.0),
                "used_cpu_user_children": stats_info.get("used_cpu_user_children", 0.0),
                "command_stats_sample": dict(list(commandstats_info.items())[:10]) if commandstats_info else {},
                "timestamp": datetime.utcnow().isoformat()
            }
        
        fallback = {
            "hit_ratio_percent": 0.0, "keyspace_hits": 0, "keyspace_misses": 0,
            "total_requests": 0, "ops_per_sec": 0, "input_kbps": 0.0, "output_kbps": 0.0,
            "used_cpu_sys": 0.0, "used_cpu_user": 0.0, "used_cpu_sys_children": 0.0,
            "used_cpu_user_children": 0.0, "command_stats_sample": {}
        }
        
        return self._collect_metric_with_timing("performance_metrics", collect, fallback)

    def get_keyspace_metrics(self) -> Dict[str, Any]:
        """Get keyspace information"""
        def collect():
            info = self._safe_get_info_with_timeout("keyspace")
            return {
                "keyspace_info": info,
                "timestamp": datetime.utcnow().isoformat()
            }
        
        fallback = {"keyspace_info": {}}
        
        return self._collect_metric_with_timing("keyspace_metrics", collect, fallback)

    def _collect_task_group(self, tasks: list, results: dict, collection_info: dict, max_workers: int = 3):
        """
        Collect a group of tasks with limited parallelism to avoid overwhelming the cluster
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as group_executor:
            # Submit all tasks in the group
            future_to_name = {}
            for name, func in tasks:
                future = group_executor.submit(func)
                future_to_name[future] = name
            
            # Collect results with timeout
            task_timeout = 8.0  # 8 seconds per task
            group_timeout = len(tasks) * task_timeout + 5.0  # Group timeout with buffer
            
            try:
                for future in concurrent.futures.as_completed(future_to_name, timeout=group_timeout):
                    task_name = future_to_name[future]
                    try:
                        task_start = time.time()
                        result = future.result(timeout=task_timeout)
                        task_duration = time.time() - task_start
                        
                        results[task_name] = result
                        collection_info["successful_tasks"] += 1
                        collection_info["task_timings"][task_name] = f"{task_duration:.2f}s"
                        logger.info(f"✅ Task '{task_name}' completed in {task_duration:.2f}s")
                        
                    except concurrent.futures.TimeoutError:
                        logger.error(f"⏱️  Task '{task_name}' timed out after {task_timeout}s")
                        results[task_name] = {"error": f"Task timed out after {task_timeout}s", "timestamp": datetime.utcnow().isoformat()}
                        collection_info["failed_tasks"] += 1
                        collection_info["task_timings"][task_name] = f"timeout({task_timeout}s)"
                    except Exception as e:
                        logger.error(f"❌ Task '{task_name}' failed: {str(e)}")
                        results[task_name] = {"error": str(e), "timestamp": datetime.utcnow().isoformat()}
                        collection_info["failed_tasks"] += 1
                        collection_info["task_timings"][task_name] = f"error"
                        
            except concurrent.futures.TimeoutError:
                logger.error(f"⏱️  Task group timed out after {group_timeout}s")
                # Fill missing results for tasks that didn't complete
                for future, task_name in future_to_name.items():
                    if task_name not in results:
                        results[task_name] = {"error": "Task group timeout", "timestamp": datetime.utcnow().isoformat()}
                        collection_info["failed_tasks"] += 1
                        collection_info["task_timings"][task_name] = "group_timeout"

    def get_all_metrics_parallel(self) -> Dict[str, Any]:
        """
        Get comprehensive metrics from all categories using optimized parallel collection
        """
        logger.info("🚀 Starting optimized parallel metrics collection")
        overall_start = time.time()
        
        # Define all metric collection tasks with priorities
        # High priority tasks are collected first to reduce cluster load
        high_priority_tasks = [
            ("server", self.get_server_metrics),
            ("memory", self.get_memory_metrics),
            ("connections", self.get_connection_metrics),
        ]
        
        low_priority_tasks = [
            ("commands", self.get_command_stats),
            ("cluster", self.get_cluster_metrics),
            ("performance", self.get_performance_metrics),
            ("keyspace", self.get_keyspace_metrics)
        ]
        
        all_tasks = high_priority_tasks + low_priority_tasks
        
        results = {}
        collection_info = {
            "method": "parallel_optimized",
            "total_tasks": len(all_tasks),
            "successful_tasks": 0,
            "failed_tasks": 0,
            "task_timings": {},
            "optimization_notes": ["Reduced parallel workers", "Prioritized essential metrics", "Added throttling"]
        }
        
        try:
            # Process high priority tasks first with reduced parallelism
            logger.info(f"Processing {len(high_priority_tasks)} high-priority tasks")
            self._collect_task_group(high_priority_tasks, results, collection_info, max_workers=2)
            
            # Small delay to prevent overwhelming the cluster
            time.sleep(0.1)
            
            # Process remaining tasks
            logger.info(f"Processing {len(low_priority_tasks)} low-priority tasks")
            self._collect_task_group(low_priority_tasks, results, collection_info, max_workers=3)
        
        except concurrent.futures.TimeoutError:
            logger.error("⏱️  Overall metrics collection timed out after 25s")
            collection_info["overall_timeout"] = True
            # Fill missing results with timeout errors
            for name, _ in all_tasks:
                if name not in results:
                    results[name] = {"error": "Overall collection timeout", "timestamp": datetime.utcnow().isoformat()}
                    collection_info["failed_tasks"] += 1
                    collection_info["task_timings"][name] = "overall_timeout"
        
        except Exception as e:
            logger.error(f"❌ Overall metrics collection failed: {str(e)}")
            collection_info["overall_error"] = str(e)
        
        overall_duration = time.time() - overall_start
        collection_info["total_duration_seconds"] = round(overall_duration, 2)
        
        logger.info(f"🏁 Parallel metrics collection completed in {overall_duration:.2f}s "
                   f"({collection_info['successful_tasks']}/{collection_info['total_tasks']} successful)")
        
        return {
            **results,
            "collection_info": collection_info,
            "collected_at": datetime.utcnow().isoformat()
        }

    def get_all_metrics(self) -> Dict[str, Any]:
        """
        Get comprehensive metrics from all categories (backward compatibility)
        This method now uses parallel collection by default
        """
        return self.get_all_metrics_parallel()

    def get_all_metrics_sequential(self) -> Dict[str, Any]:
        """
        Get comprehensive metrics from all categories using sequential collection (legacy)
        """
        logger.info("🐌 Starting sequential metrics collection (legacy mode)")
        overall_start = time.time()
        
        try:
            result = {
                "server": self.get_server_metrics(),
                "memory": self.get_memory_metrics(),
                "connections": self.get_connection_metrics(),
                "commands": self.get_command_stats(),
                "cluster": self.get_cluster_metrics(),
                "performance": self.get_performance_metrics(),
                "keyspace": self.get_keyspace_metrics(),
                "collected_at": datetime.utcnow().isoformat(),
                "collection_info": {
                    "method": "sequential",
                    "total_duration_seconds": round(time.time() - overall_start, 2)
                }
            }
            
            overall_duration = time.time() - overall_start
            logger.info(f"🏁 Sequential metrics collection completed in {overall_duration:.2f}s")
            return result
            
        except Exception as e:
            overall_duration = time.time() - overall_start
            logger.error(f"❌ Sequential metrics collection failed after {overall_duration:.2f}s: {str(e)}")
            # Return partial results with error info
            return {
                "server": self.get_server_metrics(),
                "memory": self.get_memory_metrics(),
                "connections": self.get_connection_metrics(),
                "commands": self.get_command_stats(),
                "cluster": self.get_cluster_metrics(),
                "performance": self.get_performance_metrics(),
                "keyspace": self.get_keyspace_metrics(),
                "collected_at": datetime.utcnow().isoformat(),
                "collection_info": {
                    "method": "sequential",
                    "total_duration_seconds": round(overall_duration, 2),
                    "error": str(e)
                }
            }

    def close(self):
        """Clean up resources"""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)
            logger.info("MetricsCollector thread pool executor shutdown")
