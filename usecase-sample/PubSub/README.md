# Sharded Pub/Sub Demo with Valkey GLIDE

This repository demonstrates a **sharded** publisher/subscriber pattern using **Valkey GLIDE** against a Redis Cluster (Redis 7+ or AWS MemoryDB). It contains two standalone Python scripts:

* **`subscribe.py`** — Connects with GLIDE, auto-`SSUBSCRIBE`s to five channels, and prints incoming messages.
* **`publish.py`** — Connects with GLIDE, uses **sharded publish** (`SPUBLISH`), and sends 10 messages per channel with a delay.

---

## 🚀 Quick Start

1. **Clone this repo**

   ```bash
   git clone 
   cd valkey-glide-pubsub-demo
   ```

2. **Install dependencies**

   ```bash
   pip install valkey-glide
   ```

3. **Set your cluster endpoint**

   ```bash
   export ELASTICACHE_CFG="<your-cluster-endpoint>"
   ```

   * Replace with your ElastiCache/MemoryDB valkey cluster configuration endpoint.

4. **Run the subscriber** (in one terminal)

   ```bash
   python3 subscribe.py
   ```

   * Subscriber connects, auto-`SSUBSCRIBE`s to `channel1`–`channel5`, and blocks waiting for messages.

5. **Run the publisher** (in another terminal)

   ```bash
   python3 publish.py
   ```

   * Publisher connects, then sends 10 messages per channel with a 1‑second delay.

6. **Observe output**

   * Publisher terminal shows each `SPUBLISH`.
   * Subscriber terminal prints every message received.

---

## 🛠️ Script Details

### `subscribe.py`

* Imports `GlideClusterClient` and builds a `GlideClusterClientConfiguration` with `pubsub_subscriptions`.
* Defines `handle_message()` callback to decode and print each `smessage` event.
* Calls `GlideClusterClient.create()` which issues `SSUBSCRIBE` on the correct shard for each channel.
* Runs indefinitely to receive and log messages.

### `publish.py`

* Imports `GlideClusterClient` and builds a simple `GlideClusterClientConfiguration`.
* Connects via `GlideClusterClient.create()`, then publishes with `await client.publish(..., sharded=True)`.
* Uses a 1-second `asyncio.sleep()` between messages to simulate load without flooding.

---



## 🔧 Testing Reliability & Failover

## 🤝 Contributing

Feel free to open issues or pull requests to improve examples, add more usage patterns, or support additional languages.

