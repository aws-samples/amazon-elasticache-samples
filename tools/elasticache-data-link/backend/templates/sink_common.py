"""Code shared by the Python sink worker templates in templates/sink/.

The generated deployment packages ship this file next to sink_worker.py as
sink_common.py, so each template keeps only what differs between them: the
Valkey client and how it writes. Everything here is the same for every
template: configuration, the metrics endpoint, shutdown handling, key naming,
and the Kafka consumer setup.
"""
import json
import os
import re
import signal
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

from kafka import KafkaConsumer

# Configuration from environment
KAFKA_BOOTSTRAP_SERVERS = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
KAFKA_CONSUMER_GROUP = os.getenv('KAFKA_CONSUMER_GROUP', 'ecdl-sink')
KAFKA_TOPIC_PATTERN = os.getenv('KAFKA_TOPIC_PATTERN', 'ecdl.*')
KAFKA_SECURITY_PROTOCOL = os.getenv('KAFKA_SECURITY_PROTOCOL', 'PLAINTEXT')
KAFKA_SASL_MECHANISM = os.getenv('KAFKA_SASL_MECHANISM', '')
VALKEY_HOST = os.getenv('VALKEY_HOST', 'localhost')
VALKEY_PORT = int(os.getenv('VALKEY_PORT', '6379'))
VALKEY_SSL = os.getenv('VALKEY_SSL', 'false').lower() == 'true'
VALKEY_AUTH_TOKEN = os.getenv('VALKEY_AUTH_TOKEN', '')
KEY_PREFIX = os.getenv('KEY_PREFIX', '')
KEY_PATTERN = os.getenv('KEY_PATTERN', '{table}:{pk}')
WRITE_MODE = os.getenv('WRITE_MODE', 'hset')
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '100'))
LWW_ENABLED = os.getenv('LWW_ENABLED', 'true').lower() == 'true'
METRICS_PORT = int(os.getenv('METRICS_PORT', '9090'))
# Loopback by default. A generated docker-compose sets METRICS_BIND to accept
# its 127.0.0.1 publish; nothing else needs to reach the metrics server.
METRICS_BIND = os.getenv('METRICS_BIND', '127.0.0.1')

# Shared metrics dict — updated by the main loop, read by the metrics server.
metrics = {
    'messages_consumed': 0,
    'writes': 0,
    'deletes': 0,
    'skips': 0,
    'errors': 0,
    'start_time': None,
    'last_event_time': None,
    'consumer_group': KAFKA_CONSUMER_GROUP,
    'topics': [],
    'kafka_bootstrap': KAFKA_BOOTSTRAP_SERVERS,
    'valkey_host': VALKEY_HOST,
    'valkey_port': VALKEY_PORT,
    'write_mode': WRITE_MODE,
    'status': 'starting',
}

# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

_stop = threading.Event()


def running():
    """True until SIGINT or SIGTERM is received."""
    return not _stop.is_set()


def _on_signal(sig, frame):
    print("Shutdown signal received, stopping...")
    _stop.set()


def install_signal_handlers():
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)


# ---------------------------------------------------------------------------
# Metrics endpoint
# ---------------------------------------------------------------------------

class MetricsHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP handler that serves /metrics as JSON."""
    def do_GET(self):
        if self.path == '/metrics' or self.path == '/':
            uptime = None
            if metrics['start_time']:
                uptime = round(time.time() - metrics['start_time'], 1)
            body = json.dumps({**metrics, 'uptime_seconds': uptime}, default=str)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress request logs


def start_metrics_server():
    """Start the metrics HTTP server in a daemon thread."""
    try:
        server = HTTPServer((METRICS_BIND, METRICS_PORT), MetricsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"  Metrics server listening on {METRICS_BIND}:{METRICS_PORT}")
    except Exception as e:
        print(f"  Warning: Could not start metrics server on port {METRICS_PORT}: {e}")


def record_result(stats, result):
    """Count one processed event in the loop's stats and the shared metrics."""
    stats[result] = stats.get(result, 0) + 1
    metrics['messages_consumed'] += 1
    metrics['last_event_time'] = time.time()
    if result == 'write':
        metrics['writes'] += 1
    elif result == 'delete':
        metrics['deletes'] += 1
    elif result == 'error':
        metrics['errors'] += 1
    else:
        metrics['skips'] += 1


def stats_line(stats):
    return (f"writes={stats['write']}, deletes={stats['delete']}, "
            f"skips={stats['skip']}, errors={stats['error']}")


# ---------------------------------------------------------------------------
# Key naming
# ---------------------------------------------------------------------------

def extract_key(event, table_name, kafka_key=None):
    """Extract the key for Valkey based on the configured pattern."""
    # Prefer the Kafka message key (Debezium puts PK fields there)
    key_data = None
    if kafka_key:
        if isinstance(kafka_key, dict):
            key_data = kafka_key.get('payload', kafka_key)
        elif isinstance(kafka_key, (str, int, float)):
            key_data = kafka_key

    if not key_data:
        key_data = event.get('key', {})

    # Build primary key from key payload
    if isinstance(key_data, dict):
        pk_parts = [str(v) for v in key_data.values()]
        pk = ':'.join(pk_parts) if pk_parts else 'unknown'
    else:
        pk = str(key_data) if key_data else 'unknown'

    key = KEY_PATTERN.replace('{table}', table_name).replace('{pk}', pk)
    if KEY_PREFIX:
        key = f"{KEY_PREFIX}:{key}"
    return key


# ---------------------------------------------------------------------------
# Kafka consumer
# ---------------------------------------------------------------------------

def _safe_json_deserialize(m):
    if not m:
        return None
    try:
        return json.loads(m.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _consumer_kwargs():
    kafka_kwargs = {
        'bootstrap_servers': KAFKA_BOOTSTRAP_SERVERS.split(','),
        'group_id': KAFKA_CONSUMER_GROUP,
        'auto_offset_reset': 'earliest',
        'enable_auto_commit': True,
        'metadata_max_age_ms': 10000,
        'value_deserializer': _safe_json_deserialize,
        'key_deserializer': _safe_json_deserialize,
    }
    # MSK IAM authentication
    if KAFKA_SECURITY_PROTOCOL == 'SASL_SSL' and KAFKA_SASL_MECHANISM == 'AWS_MSK_IAM':
        from aws_msk_iam_sasl_signer import MSKAuthTokenProvider
        from kafka.sasl.oauth import AbstractTokenProvider

        class MSKTokenProvider(AbstractTokenProvider):
            def token(self):
                token, _ = MSKAuthTokenProvider.generate_auth_token(
                    region=os.getenv('AWS_DEFAULT_REGION', 'us-east-1')
                )
                return token

        kafka_kwargs['security_protocol'] = 'SASL_SSL'
        kafka_kwargs['sasl_mechanism'] = 'OAUTHBEARER'
        kafka_kwargs['sasl_oauth_token_provider'] = MSKTokenProvider()
        print("  Using MSK IAM authentication")
    return kafka_kwargs


def _is_data_topic(topic, pattern_re):
    """CDC data topics only (prefix.pipeline.database.table): not Connect's
    internal configs/offsets/status topics, and not schema history."""
    internal_suffixes = ('_connect_configs', '_connect_offsets', '_connect_status')
    return (pattern_re.match(topic)
            and not topic.endswith(internal_suffixes)
            and 'schema-history' not in topic
            and len(topic.split('.')) >= 4)


def connect_consumer(sleep):
    """Create the Kafka consumer and subscribe it, retrying every 5s until it
    works or shutdown is requested. `sleep` is time.sleep for the threaded
    template and a blocking wrapper for the asyncio one. Returns None on
    shutdown."""
    print("Connecting to Kafka...")
    print(f"  Consumer group: {KAFKA_CONSUMER_GROUP}")
    while running():
        try:
            consumer = KafkaConsumer(**_consumer_kwargs())
            # Discover matching topics, then subscribe explicitly (pattern
            # subscribe can be unreliable with MSK IAM auth).
            pattern_re = re.compile(KAFKA_TOPIC_PATTERN)
            matched = [t for t in consumer.topics() if _is_data_topic(t, pattern_re)]
            if matched:
                consumer.subscribe(topics=matched)
                print(f"Subscribed to {len(matched)} topic(s): {matched}")
                metrics['topics'] = matched
            else:
                # Fall back to pattern subscribe and wait for a metadata refresh.
                print(f"  No topics matched '{KAFKA_TOPIC_PATTERN}' yet, using pattern subscribe...")
                consumer.subscribe(pattern=KAFKA_TOPIC_PATTERN)
                print(f"Subscribed to topics matching: {KAFKA_TOPIC_PATTERN}")
                metrics['topics'] = [KAFKA_TOPIC_PATTERN]
            return consumer
        except Exception as e:
            print(f"Failed to connect to Kafka: {e}, retrying in 5s...")
            metrics['status'] = 'error_kafka'
            sleep(5)
    return None


def print_startup_banner(title):
    print(f"{title} starting...")
    print(f"  Kafka: {KAFKA_BOOTSTRAP_SERVERS}")
    print(f"  Topic Pattern: {KAFKA_TOPIC_PATTERN}")
    print(f"  Valkey: {VALKEY_HOST}:{VALKEY_PORT}")
    print(f"  Write Mode: {WRITE_MODE}")
    print(f"  LWW Enabled: {LWW_ENABLED}")
