# subscribe.py
import asyncio
import os
import signal

from glide import (
    GlideClusterClient,
    GlideClusterClientConfiguration,
    NodeAddress,
)

# pull the nested Pub/Sub types off the config class
PubSubSubscriptions = GlideClusterClientConfiguration.PubSubSubscriptions
PubSubChannelModes  = GlideClusterClientConfiguration.PubSubChannelModes

def handle_message(msg, context):
    channel = msg.channel.decode() if isinstance(msg.channel, bytes) else msg.channel
    data    = msg.message.decode() if isinstance(msg.message, bytes) else msg.message
    print(f"[Subscriber] ← {channel}: {data}")

async def main():
    endpoint = os.getenv("ELASTICACHE_CFG", "localhost")
    addresses = [ NodeAddress(endpoint, 6379) ]

    subs_cfg = PubSubSubscriptions(
        channels_and_patterns = {
            PubSubChannelModes.Sharded: {
                "channel1", "channel2", "channel3", "channel4", "channel5"
            }
        },
        callback = handle_message,
        context  = None
    )

    client_cfg = GlideClusterClientConfiguration(
        addresses            = addresses,
        use_tls              = False,
        pubsub_subscriptions = subs_cfg
    )

    client = await GlideClusterClient.create(client_cfg)
    print("[Subscriber] Connected with sharded subscriptions")

    # Wait until cancelled (e.g. Ctrl+C), then clean up
    stop_event = asyncio.Event()

    def _on_shutdown():
        stop_event.set()

    # Register SIGINT/SIGTERM to trigger shutdown
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _on_shutdown)
    loop.add_signal_handler(signal.SIGTERM, _on_shutdown)

    try:
        await stop_event.wait()
    finally:
        print("\n[Subscriber] Shutting down and closing client...")
        await client.close()
        print("[Subscriber] Closed.")

if __name__ == "__main__":
    asyncio.run(main())
