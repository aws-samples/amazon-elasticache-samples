# publish.py
import asyncio
import os

from glide import (
    GlideClusterClient,
    GlideClusterClientConfiguration,
    NodeAddress,
)

async def main():
    endpoint = os.getenv("ELASTICACHE_CFG", "localhost")
    addresses = [ NodeAddress(endpoint, 6379) ]

    client_cfg = GlideClusterClientConfiguration(
        addresses = addresses,
        use_tls   = False,
    )

    client = await GlideClusterClient.create(client_cfg)
    print("[Publisher] Connected to cluster")

    for channel in ["channel1", "channel2", "channel3", "channel4", "channel5"]:
        for i in range(1, 11):
            payload = f"Msg {i} to {channel}"
            # sharded=True → uses SPUBLISH under the hood
            await client.publish(payload, channel, sharded=True)
            print(f"[Publisher] → {channel}: {payload}")
            await asyncio.sleep(1)  # small delay between messages

    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
