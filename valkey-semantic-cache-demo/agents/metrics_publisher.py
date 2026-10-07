"""
AgentCore Metrics Publisher with async task management.

Handles high-throughput metric emission by batching and publishing asynchronously
using AgentCore's async task API. Safe for persistent sessions where background
threads survive across multiple invocations.
"""

import logging
import time
from collections import deque
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class AgentCoreMetricsPublisher:
    """
    Batched async metrics publisher for AgentCore runtime.
    
    Buffers metrics in memory and publishes in batches to CloudWatch using
    AgentCore's async task management. Thread-safe and optimized for high
    throughput (100+ req/s).
    """
    
    def __init__(self, app, cloudwatch_client, namespace: str, max_buffer_size: int = 100):
        """
        Initialize the metrics publisher.
        
        Args:
            app: BedrockAgentCoreApp instance for async task management
            cloudwatch_client: Boto3 CloudWatch client
            namespace: CloudWatch namespace for metrics
            max_buffer_size: Maximum metrics to buffer before dropping oldest
        """
        self.app = app
        self.cloudwatch = cloudwatch_client
        self.namespace = namespace
        self.metrics_buffer = deque(maxlen=max_buffer_size)
        self.buffer_lock = Lock()
        self.last_flush = time.time()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="metrics")
        
    def add_metric(self, metric_name: str, value: float, unit: str = 'None',
                   dimensions: list | None = None, timestamp: float | None = None):
        """
        Add a metric to the buffer for async publication.
        
        Args:
            metric_name: CloudWatch metric name
            value: Metric value
            unit: CloudWatch unit (None, Count, Milliseconds, etc.)
            dimensions: List of dimension dicts [{"Name": "...", "Value": "..."}]
            timestamp: Unix timestamp (defaults to current time)
        """
        metric_data = {
            'MetricName': metric_name,
            'Value': value,
            'Unit': unit,
            'Timestamp': timestamp or time.time()
        }
        
        if dimensions:
            metric_data['Dimensions'] = dimensions
        
        should_flush = False
        with self.buffer_lock:
            self.metrics_buffer.append(metric_data)
            
            # Check flush conditions (don't flush inside lock)
            if len(self.metrics_buffer) >= 20 or (time.time() - self.last_flush) > 10:
                should_flush = True
        
        # Flush outside lock to prevent blocking
        if should_flush:
            self._flush_async()
    
    def _flush_async(self):
        """Flush buffered metrics asynchronously via thread pool"""
        with self.buffer_lock:
            if not self.metrics_buffer:
                return
            
            # Pop up to 20 metrics (CloudWatch batch limit)
            batch = []
            for _ in range(min(20, len(self.metrics_buffer))):
                batch.append(self.metrics_buffer.popleft())
            
            self.last_flush = time.time()
        
        # Submit to executor with AgentCore async task tracking
        if batch:
            self.executor.submit(self._publish_batch, batch)
    
    def _publish_batch(self, metrics_batch: list):
        """
        Publish a batch of metrics to CloudWatch (runs in background thread).
        
        Uses AgentCore's async task API to ensure proper lifecycle management
        in persistent sessions.
        """
        task_id = self.app.add_async_task("metrics_batch", {"count": len(metrics_batch)})
        
        try:
            logger.info(f"[METRICS] Publishing batch of {len(metrics_batch)} metrics to {self.namespace}")
            self.cloudwatch.put_metric_data(
                Namespace=self.namespace,
                MetricData=metrics_batch
            )
            logger.info("[METRICS] Successfully published batch")
        except Exception as e:
            logger.error(f"[METRICS] Failed to publish batch: {e}", exc_info=True)
        finally:
            self.app.complete_async_task(task_id)
    
    def flush_remaining(self):
        """Synchronously flush any remaining buffered metrics (call on shutdown)"""
        with self.buffer_lock:
            if not self.metrics_buffer:
                return
            
            batch = list(self.metrics_buffer)
            self.metrics_buffer.clear()
        
        if batch:
            # Direct synchronous call (no async task needed for shutdown)
            try:
                logger.info(f"[METRICS] Flushing remaining {len(batch)} metrics")
                self.cloudwatch.put_metric_data(
                    Namespace=self.namespace,
                    MetricData=batch
                )
            except Exception as e:
                logger.error(f"[METRICS] Failed to flush remaining metrics: {e}")

