# Template: Valkey Glide Cluster
#!/usr/bin/env python3
"""
ECDL Valkey Sink Worker (Glide Cluster)
Consumes CDC events from Kafka and writes to Valkey/ElastiCache
using the Valkey Glide client library for cluster mode.

Configuration, the metrics endpoint, shutdown handling, key naming and the
Kafka consumer come from sink_common.py, which is shipped alongside.
"""

import asyncio
import json
import time

from glide import (
    GlideClusterClientConfiguration,
    GlideClusterClient,
    NodeAddress,
    ServerCredentials,
)

from sink_common import (
    BATCH_SIZE, LWW_ENABLED, VALKEY_AUTH_TOKEN, VALKEY_HOST, VALKEY_PORT,
    VALKEY_SSL, WRITE_MODE, connect_consumer, extract_key,
    install_signal_handlers, metrics, print_startup_banner, record_result,
    running, start_metrics_server, stats_line,
)

install_signal_handlers()


async def create_valkey_client():
    """Create Valkey Glide cluster client connection."""
    addresses = [NodeAddress(host=VALKEY_HOST, port=VALKEY_PORT)]

    config_kwargs = {
        'addresses': addresses,
        'use_tls': VALKEY_SSL,
        'request_timeout': 5000,
    }
    if VALKEY_AUTH_TOKEN:
        config_kwargs['credentials'] = ServerCredentials(password=VALKEY_AUTH_TOKEN)

    config = GlideClusterClientConfiguration(**config_kwargs)
    client = await GlideClusterClient.create(config)

    # Verify connection
    result = await client.custom_command(["PING"])
    print(f"Valkey PING response: {result}")
    return client


async def process_event(valkey_client, topic, event, kafka_key=None):
    """Process a single CDC event and write to Valkey."""
    try:
        topic_parts = topic.split('.')
        table_name = topic_parts[-1] if topic_parts else 'unknown'

        payload = event.get('payload', event)
        op = payload.get('op', 'c')

        if op == 'd':
            key = extract_key(payload, table_name, kafka_key)
            await valkey_client.delete([key])
            return 'delete'

        after = payload.get('after')
        if not after:
            return 'skip'

        key = extract_key(payload, table_name, kafka_key)

        # LWW check - only write if this is newer
        if LWW_ENABLED:
            ts_ms = payload.get('ts_ms', 0)
            try:
                if WRITE_MODE == 'hset':
                    existing_ts = await valkey_client.hget(key, '__ts_ms')
                elif WRITE_MODE == 'json_object':
                    raw = await valkey_client.custom_command(["JSON.GET", key, "$.__ts_ms"])
                    existing_ts = None
                    if raw:
                        try:
                            parsed = json.loads(raw if isinstance(raw, str) else raw.decode('utf-8'))
                            existing_ts = parsed[0] if isinstance(parsed, list) and parsed else parsed
                        except (json.JSONDecodeError, AttributeError):
                            existing_ts = None
                else:
                    raw = await valkey_client.get(key)
                    existing_ts = None
                    if raw:
                        try:
                            existing_obj = json.loads(raw if isinstance(raw, str) else raw.decode('utf-8'))
                            existing_ts = existing_obj.get('__ts_ms') if isinstance(existing_obj, dict) else None
                        except (json.JSONDecodeError, AttributeError):
                            existing_ts = None
            except Exception:
                existing_ts = None
            if existing_ts:
                existing_val = existing_ts if isinstance(existing_ts, (int, float)) else (existing_ts if isinstance(existing_ts, str) else existing_ts.decode('utf-8'))
                if int(existing_val) > ts_ms:
                    return 'skip_lww'

        # Write based on mode
        if WRITE_MODE == 'hset':
            hash_data = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) if v is not None else ''
                        for k, v in after.items()}
            if LWW_ENABLED:
                hash_data['__ts_ms'] = str(payload.get('ts_ms', 0))
            await valkey_client.hset(key, hash_data)
        elif WRITE_MODE == 'json':
            if LWW_ENABLED:
                after['__ts_ms'] = payload.get('ts_ms', 0)
            await valkey_client.set(key, json.dumps(after))
        elif WRITE_MODE == 'json_object':
            if LWW_ENABLED:
                after['__ts_ms'] = payload.get('ts_ms', 0)
            await valkey_client.custom_command(["JSON.SET", key, "$", json.dumps(after)])
        elif WRITE_MODE == 'string':
            await valkey_client.set(key, json.dumps(after))

        return 'write'

    except Exception as e:
        print(f"Error processing event: {e}")
        return 'error'


async def main():
    print_startup_banner("ECDL Valkey Sink Worker (Glide Cluster)")

    start_metrics_server()
    metrics['start_time'] = time.time()

    # Connect to Valkey
    print("Connecting to Valkey (Glide Cluster)...")
    valkey_client = None
    while running() and valkey_client is None:
        try:
            valkey_client = await create_valkey_client()
            print("Connected to Valkey!")
            metrics['status'] = 'connected_valkey'
        except Exception as e:
            print(f"Failed to connect to Valkey: {e}, retrying in 5s...")
            metrics['status'] = 'error_valkey'
            await asyncio.sleep(5)

    if not running():
        return

    # kafka-python is not async; the consumer is created and polled directly.
    consumer = connect_consumer(time.sleep)
    if consumer is None:
        return

    stats = {'write': 0, 'delete': 0, 'skip': 0, 'error': 0}
    batch_count = 0
    metrics['status'] = 'running'

    print("Processing messages...")
    while running():
        try:
            messages = consumer.poll(timeout_ms=1000, max_records=BATCH_SIZE)

            for topic_partition, records in messages.items():
                for record in records:
                    if not running():
                        break
                    # Skip messages that couldn't be deserialized
                    if record.value is None:
                        continue

                    result = await process_event(valkey_client, record.topic, record.value, record.key)
                    record_result(stats, result)
                    batch_count += 1
                    if batch_count >= 1000:
                        print(f"Stats: {stats_line(stats)}")
                        batch_count = 0

        except Exception as e:
            print(f"Error in main loop: {e}")
            await asyncio.sleep(1)

    # Cleanup
    print("Shutting down...")
    metrics['status'] = 'stopped'
    consumer.close()
    print(f"Final stats: {stats_line(stats)}")
    print("Goodbye!")


if __name__ == '__main__':
    asyncio.run(main())
