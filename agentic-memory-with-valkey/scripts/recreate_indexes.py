"""Drop and recreate all four cache indexes with COSINE distance metric."""
from glide_sync import (
    GlideClient, GlideClientConfiguration, NodeAddress,
    TlsAdvancedConfiguration, AdvancedGlideClientConfiguration,
)

tls_config = TlsAdvancedConfiguration(use_insecure_tls=True)
advanced = AdvancedGlideClientConfiguration(tls_config=tls_config)
config = GlideClientConfiguration(
    addresses=[NodeAddress(host="localhost", port=6379)],
    client_name="cache-admin",
    use_tls=True,
    advanced_config=advanced,
)
c = GlideClient.create(config)

INDEXES = [
    ("idx:shopnow:full:hot",      "shopnow:full:hot:vec:",      True),
    ("idx:shopnow:full:temp",     "shopnow:full:temp:vec:",     True),
    ("idx:shopnow:subagent:hot",  "shopnow:subagent:hot:vec:",  False),
    ("idx:shopnow:subagent:temp", "shopnow:subagent:temp:vec:", False),
]

BASE_SCHEMA = [
    "embedding", "VECTOR", "HNSW", "6",
    "TYPE", "FLOAT32",
    "DIM", "1024",
    "DISTANCE_METRIC", "COSINE",
    "request_id", "TAG",
]

FULL_EXTRA_SCHEMA = [
    "state_tags",        "TAG",     "SEPARATOR", ",",
    "slot_budget_usd",   "NUMERIC",
    "slot_radius_miles", "NUMERIC",
]

for name, prefix, _ in INDEXES:
    try:
        c.custom_command(["FT.DROPINDEX", name])
        print(f"Dropped  {name}")
    except Exception as e:
        print(f"Drop     {name}: {e}")

for name, prefix, is_full in INDEXES:
    schema = BASE_SCHEMA + (FULL_EXTRA_SCHEMA if is_full else [])
    try:
        c.custom_command([
            "FT.CREATE", name,
            "ON", "HASH",
            "PREFIX", "1", prefix,
            "SCHEMA",
            *schema,
        ])
        tag = "COSINE + state_tags" if is_full else "COSINE"
        print(f"Created  {name} ({tag})")
    except Exception as e:
        print(f"Create   {name}: {e}")
