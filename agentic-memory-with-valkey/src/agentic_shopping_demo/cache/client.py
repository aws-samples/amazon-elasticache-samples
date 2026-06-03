"""
Semantic cache client for ShopNow AI.

Supports two cache modes:
- SUBAGENT: Cache in front of the knowledge sub-agent (lookup_knowledge tool)
- FULL: Cache in front of the main agent (entire response)

Uses ElastiCache (Valkey) with HNSW vector search and Titan embeddings.
"""

import json
import logging
import os
import struct
import time
import uuid
from enum import Enum
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

ELASTICACHE_ENDPOINT = os.environ.get("SHOPNOW_CACHE_ENDPOINT", "localhost")
ELASTICACHE_PORT = int(os.environ.get("SHOPNOW_CACHE_PORT", "6379"))
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
VECTOR_DIM = 1024
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Similarity thresholds (cosine similarity: 0.0-1.0, higher = more strict)
THRESHOLD_SUBAGENT = 0.70
THRESHOLD_FULL = 0.65

# Key prefixes — hot (pre-loaded, never flushed) and temp (demo, flushable)
PREFIX_VECTOR_SUBAGENT_HOT  = "shopnow:subagent:hot:vec:"
PREFIX_RR_SUBAGENT_HOT      = "shopnow:subagent:hot:rr:"
PREFIX_VECTOR_SUBAGENT_TEMP = "shopnow:subagent:temp:vec:"
PREFIX_RR_SUBAGENT_TEMP     = "shopnow:subagent:temp:rr:"

PREFIX_VECTOR_FULL_HOT      = "shopnow:full:hot:vec:"
PREFIX_RR_FULL_HOT          = "shopnow:full:hot:rr:"
PREFIX_VECTOR_FULL_TEMP     = "shopnow:full:temp:vec:"
PREFIX_RR_FULL_TEMP         = "shopnow:full:temp:rr:"

INDEX_SUBAGENT_HOT  = "idx:shopnow:subagent:hot"
INDEX_SUBAGENT_TEMP = "idx:shopnow:subagent:temp"
INDEX_FULL_HOT      = "idx:shopnow:full:hot"
INDEX_FULL_TEMP     = "idx:shopnow:full:temp"

ALL_INDEXES = [
    (INDEX_SUBAGENT_HOT,  PREFIX_VECTOR_SUBAGENT_HOT),
    (INDEX_SUBAGENT_TEMP, PREFIX_VECTOR_SUBAGENT_TEMP),
    (INDEX_FULL_HOT,      PREFIX_VECTOR_FULL_HOT),
    (INDEX_FULL_TEMP,     PREFIX_VECTOR_FULL_TEMP),
]


class CacheMode(str, Enum):
    OFF = "off"
    SUBAGENT = "subagent"
    FULL = "full"


class CacheTemp(str, Enum):
    HOT  = "hot"   # use hot index (pre-loaded, never flushed)
    COLD = "cold"  # use temp index (flushable)


# ── Lazy clients ─────────────────────────────────────────────────────────────

_cache_client = None
_bedrock_client = None


def _get_cache_client():
    global _cache_client
    if _cache_client is not None:
        return _cache_client
    try:
        from glide_sync import (
            GlideClient, GlideClientConfiguration, NodeAddress,
            TlsAdvancedConfiguration, AdvancedGlideClientConfiguration,
        )
        tls_config = TlsAdvancedConfiguration(use_insecure_tls=True)
        advanced = AdvancedGlideClientConfiguration(tls_config=tls_config)
        config = GlideClientConfiguration(
            addresses=[NodeAddress(host=ELASTICACHE_ENDPOINT, port=ELASTICACHE_PORT)],
            client_name="shopnow-semantic-cache",
            use_tls=True,
            advanced_config=advanced,
        )
        _cache_client = GlideClient.create(config)
        print(f"[CACHE] Connected to {ELASTICACHE_ENDPOINT}:{ELASTICACHE_PORT}")
        return _cache_client
    except Exception as e:
        # Don't cache None — retry on next request so tunnel reconnects are picked up
        print(f"[CACHE] *** CONNECTION FAILED *** {ELASTICACHE_ENDPOINT}:{ELASTICACHE_PORT} — {e}")
        print(f"[CACHE] Is the SSM tunnel running? Both read and write will be skipped.")
        return None

def _get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock_client


# ── Core operations ───────────────────────────────────────────────────────────

def generate_embedding(text: str) -> Optional[list[float]]:
    """Generate 1024-dim Titan embedding for the given text."""
    try:
        client = _get_bedrock_client()
        response = client.invoke_model(
            modelId=EMBEDDING_MODEL,
            body=json.dumps({
                "inputText": text,
                "dimensions": VECTOR_DIM,
                "embeddingTypes": ["float"],
            }),
        )
        return json.loads(response["body"].read())["embedding"]
    except Exception as e:
        logger.error(f"[CACHE] Embedding generation failed: {e}")
        return None


def _search(client, index_name: str, embedding: list[float], threshold: float,
            tag_filters: Optional[list[str]] = None,
            numeric_filters: Optional[list[tuple[str, float, float]]] = None) -> tuple[Optional[str], float]:
    """
    Search vector index using FT.SEARCH with optional TAG and NUMERIC pre-filters.

    tag_filters: list of raw FT filter expressions, e.g. ["@state_tags:{slot_color:white}"]
    numeric_filters: list of (field, min, max) tuples, e.g. [("slot_budget_usd", 0, 150)]
    """
    try:
        import struct as _struct
        query_vec = _struct.pack(f"{len(embedding)}f", *embedding)

        # Build pre-filter expression
        filter_parts = []
        if tag_filters:
            filter_parts.extend(tag_filters)
        if numeric_filters:
            for field, lo, hi in numeric_filters:
                filter_parts.append(f"@{field}:[{lo} {hi}]")

        if filter_parts:
            pre_filter = "(" + " ".join(filter_parts) + ")"
            query_str = f"{pre_filter}=>[KNN 1 @embedding $vec AS score]"
        else:
            query_str = "*=>[KNN 1 @embedding $vec AS score]"

        result = client.custom_command([
            "FT.SEARCH", index_name, query_str,
            "PARAMS", "2", "vec", query_vec,
            "RETURN", "2", "request_id", "score",
            "DIALECT", "2",
        ])
        if not result or (isinstance(result, list) and int(result[0]) == 0):
            print(f"[CACHE] Search: 0 results in {index_name}")
            return None, 0.0
        # Glide returns: [total, {key: {field: value, ...}, ...}]
        hits_dict = result[1]
        if not hits_dict:
            return None, 0.0
        first_val = next(iter(hits_dict.values()))
        doc = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in first_val.items()
        }
        similarity = 1.0 - float(doc["score"])  # cosine distance → similarity
        print(f"[CACHE] Search: similarity={similarity:.4f} threshold={threshold} index={index_name} filters={filter_parts}")
        if similarity >= threshold:
            return doc["request_id"], similarity
        print(f"[CACHE] Search: below threshold ({similarity:.4f} < {threshold})")
    except Exception as e:
        print(f"[CACHE] Search failed: {e}")
    return None, 0.0


def _get_cached(client, rr_prefix: str, request_id: str) -> Optional[str]:
    """Retrieve cached response text by request_id."""
    try:
        key = f"{rr_prefix}{request_id}"
        result = client.hgetall(key)
        if result:
            data = {k.decode(): v.decode() for k, v in result.items()}
            return data.get("response_text")
    except Exception as e:
        logger.error(f"[CACHE] Get failed: {e}")
    return None


def _build_lookup_embedding_input(
    text: str,
    state: Optional[dict],
    prior_user_msg: Optional[str] = None,
) -> str:
    """
    Build a deterministic embedding input string from user text + pre-turn state.
    Only uses fields available before the agent runs.

    When prior_user_msg is provided (SESSION-scoped entries), it is prepended so
    that context-dependent fragments like bare zip codes embed with their intent.
    """
    if not state and not prior_user_msg:
        return text

    slots = (state or {}).get("slots") or {}
    entities = (state or {}).get("entities") or {}

    parts = {"q": text}

    # Prepend prior turn context for SESSION-scoped entries so that
    # "98121" after "find stores near me" embeds differently from standalone "98121"
    if prior_user_msg:
        parts["prior"] = prior_user_msg[:200]  # cap to avoid embedding drift

    if state:
        # Pre-turn slots
        for k in ("category", "color", "style", "size", "budget_usd", "zip", "radius_miles", "delivery_mode"):
            v = slots.get(k)
            if v is not None:
                parts[k] = v

        # Pre-turn user-typed entities only (order_id and explicit sku)
        for k in ("order_id", "sku"):
            v = entities.get(k)
            if v is not None:
                parts[k] = v

        turn_stage = state.get("turn_stage")
        if turn_stage:
            parts["turn_stage"] = turn_stage

    return json.dumps(parts, separators=(",", ":"), sort_keys=True)


def _build_lookup_filters(
    state: Optional[dict],
    session_id: Optional[str],
    cache_scope: str = "global",  # "global" | "session" — from intent classifier
) -> tuple[list[str], list[tuple]]:
    """
    Build TAG and NUMERIC filters from pre-turn state only.
    Never uses post-turn fields (domain, task, last_action, selected_item_id).

    cache_scope is provided by the intent classifier and takes precedence over
    the turn_stage heuristic for determining session vs global scoping.

    Returns (tag_filters, numeric_filters).
    """
    if not state:
        return [], []

    tag_filters: list[str] = []
    numeric_filters: list[tuple] = []

    slots = state.get("slots") or {}
    entities = state.get("entities") or {}
    turn_stage = state.get("turn_stage", "new")

    # Scope filter — classifier signal takes precedence; fall back to turn_stage heuristic
    is_session_scoped = (
        cache_scope == "session"
        or (cache_scope == "global" and turn_stage == "follow_up")
    )
    if is_session_scoped and session_id:
        tag_filters.append("@state_tags:{cache_scope:session}")
        safe_sid = session_id.replace("-", "\\-")
        tag_filters.append(f"@state_tags:{{session_id:{safe_sid}}}")
    else:
        tag_filters.append("@state_tags:{cache_scope:global}")

    # Exact slot filters — only for non-null pre-turn slots
    for slot_key in ("color", "category", "style"):
        v = slots.get(slot_key)
        if v is not None:
            safe_v = str(v).lower().replace(" ", "_").replace("-", "\\-")
            tag_filters.append(f"@state_tags:{{slot_{slot_key}:{safe_v}}}")

    # Explicit entity filters — only user-typed entities
    for ent_key in ("order_id", "sku"):
        v = entities.get(ent_key)
        if v is not None:
            safe_v = str(v).lower().replace("-", "\\-")
            tag_filters.append(f"@state_tags:{{entity_{ent_key}:{safe_v}}}")

    # Numeric range filters
    budget = slots.get("budget_usd")
    if budget is not None:
        numeric_filters.append(("slot_budget_usd", 0, float(budget)))

    radius = slots.get("radius_miles")
    if radius is not None:
        numeric_filters.append(("slot_radius_miles", 0, float(radius)))

    return tag_filters, numeric_filters


def _build_state_tags(
    state: Optional[dict],
    session_id: Optional[str] = None,
    cache_scope_override: Optional[str] = None,  # "session" | "global" from classifier
) -> str:
    """
    Build comma-separated namespaced state tokens from post-turn conversation state.
    Includes cache_scope: session (if session_id present) or global.
    Only includes non-null values.

    cache_scope_override from the intent classifier takes precedence over the
    turn_stage heuristic when provided.
    """
    if not state:
        return ""
    tokens = []

    slots = state.get("slots") or {}
    for k, v in slots.items():
        if v is not None and k not in ("budget_usd", "radius_miles"):
            tokens.append(f"slot_{k}:{str(v).lower().replace(' ', '_')}")

    entities = state.get("entities") or {}
    for k, v in entities.items():
        if v is not None:
            tokens.append(f"entity_{k}:{str(v).lower()}")

    if state.get("active_domain"):
        tokens.append(f"domain:{state['active_domain']}")
    if state.get("active_task"):
        tokens.append(f"task:{state['active_task']}")
    if state.get("turn_stage"):
        tokens.append(f"turn_stage:{state['turn_stage']}")

    refs = state.get("references") or {}
    if refs.get("last_action"):
        tokens.append(f"last_action:{refs['last_action']}")

    # Scope: classifier override > turn_stage heuristic
    if cache_scope_override == "session":
        is_session_scoped = True
    elif cache_scope_override == "global":
        is_session_scoped = False
    else:
        is_session_scoped = state.get("turn_stage") == "follow_up"

    if is_session_scoped and session_id:
        tokens.append(f"session_id:{session_id}")
        tokens.append("cache_scope:session")
    else:
        tokens.append("cache_scope:global")

    return ",".join(tokens)


def _store(client, vec_prefix: str, rr_prefix: str, index_name: str,
           text: str, response: str, embedding: list[float],
           state: Optional[dict] = None, session_id: Optional[str] = None,
           prior_user_msg: Optional[str] = None,
           cache_scope_override: Optional[str] = None) -> None:
    """Store request-response pair with embedding and optional state tags."""
    try:
        request_id = str(uuid.uuid4())
        vec_key = f"{vec_prefix}{request_id}"
        rr_key = f"{rr_prefix}{request_id}"
        embedding_bytes = struct.pack(f"{len(embedding)}f", *embedding)
        now = time.time()

        vec_fields: dict = {
            "request_id": request_id,
            "embedding": embedding_bytes,
            "timestamp": str(now),
        }

        # IMPORTANT: ElastiCache Valkey bug P379892069 — schema fields missing from hash keys
        # cause silent indexing failure (num_docs stays 0). Always write ALL schema fields,
        # using sentinel values (-1 for numerics, "" for tags) when not applicable.
        if state is not None:
            state_tags = _build_state_tags(state, session_id, cache_scope_override=cache_scope_override)
            vec_fields["state_tags"] = state_tags if state_tags else ""

            slots = state.get("slots") or {}
            vec_fields["slot_budget_usd"] = str(float(slots["budget_usd"])) if slots.get("budget_usd") is not None else "-1"
            vec_fields["slot_radius_miles"] = str(float(slots["radius_miles"])) if slots.get("radius_miles") is not None else "-1"
        else:
            # Sentinel values so the hash satisfies the full index schema
            vec_fields["state_tags"] = ""
            vec_fields["slot_budget_usd"] = "-1"
            vec_fields["slot_radius_miles"] = "-1"

        client.hset(vec_key, vec_fields)
        client.hset(rr_key, {
            "request_text": text,
            "response_text": response,
            "created_at": str(now),
        })
        logger.info(f"[CACHE] Stored {request_id} in {index_name} scope={cache_scope_override or 'auto'}")
    except Exception as e:
        logger.error(f"[CACHE] Store failed: {e}")


def _ensure_index(client, index_name: str, vec_prefix: str) -> None:
    """Create HNSW COSINE vector index via raw FT.CREATE custom_command.

    Schema includes state_tags (TAG) and numeric slot fields so that
    pre-filter queries work correctly for FULL cache mode.
    All fields must be present in every stored hash (use sentinel values
    when not applicable) to avoid the Valkey silent-indexing-failure bug.
    """
    try:
        client.custom_command([
            "FT.CREATE", index_name,
            "ON", "HASH",
            "PREFIX", "1", vec_prefix,
            "SCHEMA",
            "embedding", "VECTOR", "HNSW", "6",
            "TYPE", "FLOAT32",
            "DIM", str(VECTOR_DIM),
            "DISTANCE_METRIC", "COSINE",
            "request_id", "TAG",
            "state_tags", "TAG", "SEPARATOR", ",",
            "slot_budget_usd", "NUMERIC",
            "slot_radius_miles", "NUMERIC",
        ])
        logger.info(f"[CACHE] Created index {index_name}")
    except Exception as e:
        if "already exists" not in str(e).lower():
            logger.warning(f"[CACHE] Index creation: {e}")


# ── Public interface ──────────────────────────────────────────────────────────

class SemanticCache:
    """
    Semantic cache for ShopNow AI.

    Four indexes: subagent:hot, subagent:temp, full:hot, full:temp
    - hot  = pre-loaded, never flushed, used when CacheTemp.HOT
    - temp = demo cache, flushable, used when CacheTemp.COLD
    """

    def __init__(self):
        self._initialized = False
        self._indexes_ensured = False

    def initialize(self) -> bool:
        """Initialize all four cache indexes."""
        client = _get_cache_client()
        if client is None:
            return False
        try:
            for index_name, vec_prefix in ALL_INDEXES:
                _ensure_index(client, index_name, vec_prefix)
            self._initialized = True
            self._indexes_ensured = True
            logger.info("[CACHE] Initialized successfully (4 indexes)")
            return True
        except Exception as e:
            logger.error(f"[CACHE] Initialization failed: {e}")
            return False

    def _lazy_ensure_indexes(self, client) -> None:
        """Ensure indexes exist on first successful connection (handles tunnel-up-after-startup)."""
        if self._indexes_ensured:
            return
        try:
            for index_name, vec_prefix in ALL_INDEXES:
                _ensure_index(client, index_name, vec_prefix)
            self._indexes_ensured = True
            print("[CACHE] Lazy index initialization complete")
        except Exception as e:
            print(f"[CACHE] Lazy index init failed: {e}")

    def get(self, text: str, mode: CacheMode, temp: str = "hot", threshold: Optional[float] = None,
            state: Optional[dict] = None, session_id: Optional[str] = None,
            cache_scope: str = "global",
            prior_user_msg: Optional[str] = None) -> Optional[dict]:
        """
        Check cache for a semantically similar entry.

        For FULL mode, accepts pre-turn state and session_id to:
        - build a richer embedding input (text + slots + entities + turn_stage)
        - apply TAG/NUMERIC pre-filters (scope, slots, entities, TTL)

        cache_scope: "global" | "session" — from intent classifier, used to override
                     the turn_stage heuristic in _build_lookup_filters.
        prior_user_msg: prepended to embedding input for SESSION-scoped lookups so
                        context-dependent fragments embed with their intent.

        temp="hot"  → search hot index only
        temp="cold" → search temp index only

        Returns dict with {response, similarity, request_id} on hit, None on miss.
        """
        if mode == CacheMode.OFF:
            return None

        client = _get_cache_client()
        if client is None:
            print(f"[CACHE] get() — no client, skipping read (tunnel down?)")
            return None

        self._lazy_ensure_indexes(client)

        threshold = threshold if threshold is not None else (THRESHOLD_SUBAGENT if mode == CacheMode.SUBAGENT else THRESHOLD_FULL)

        if mode == CacheMode.SUBAGENT:
            index     = INDEX_SUBAGENT_HOT  if temp == "hot" else INDEX_SUBAGENT_TEMP
            rr_prefix = PREFIX_RR_SUBAGENT_HOT if temp == "hot" else PREFIX_RR_SUBAGENT_TEMP
            embed_input = text
            tag_filters, numeric_filters = [], []
        else:
            index     = INDEX_FULL_HOT  if temp == "hot" else INDEX_FULL_TEMP
            rr_prefix = PREFIX_RR_FULL_HOT if temp == "hot" else PREFIX_RR_FULL_TEMP
            # Use context-enriched embedding for SESSION-scoped lookups only.
            # GLOBAL lookups use raw text to match what was stored at write time.
            embed_input = _build_lookup_embedding_input(
                text, state,
                prior_user_msg=prior_user_msg if cache_scope == "session" else None,
            ) if cache_scope == "session" else text
            # TAG pre-filters require state_tags to be in the index schema.
            # Only apply them when the index was created with state_tags field.
            tag_filters, numeric_filters = [], []

        embedding = generate_embedding(embed_input)
        if embedding is None:
            return None

        request_id, similarity = _search(client, index, embedding, threshold,
                                         tag_filters=tag_filters or None,
                                         numeric_filters=numeric_filters or None)
        if request_id:
            response = _get_cached(client, rr_prefix, request_id)
            if response:
                logger.info(f"[CACHE HIT] mode={mode} temp={temp} similarity={similarity:.4f}")
                return {"response": response, "similarity": similarity, "request_id": request_id}

        logger.info(f"[CACHE MISS] mode={mode} temp={temp} similarity={similarity:.4f}")
        return None

    def put(self, text: str, response: str, mode: CacheMode, temp: str = "hot",
            embedding: Optional[list[float]] = None,
            state: Optional[dict] = None, session_id: Optional[str] = None,
            prior_user_msg: Optional[str] = None,
            cache_scope_override: Optional[str] = None) -> None:
        """
        Store a response in the cache.

        Always writes to the index matching the current temp setting.
        state and session_id are only applied to FULL cache writes.

        prior_user_msg: if provided and cache_scope_override=="session", the embedding
                        is built from context-enriched input (prior + current message).
        cache_scope_override: "session" | "global" from intent classifier — overrides
                              the turn_stage heuristic in _build_state_tags.
        """
        if mode == CacheMode.OFF:
            return

        client = _get_cache_client()
        if client is None:
            print(f"[CACHE] put() — no client, skipping write (tunnel down?)")
            return

        self._lazy_ensure_indexes(client)

        # For SESSION-scoped writes, enrich the embedding with prior context
        embed_text = text
        if mode == CacheMode.FULL and cache_scope_override == "session" and prior_user_msg:
            embed_text = _build_lookup_embedding_input(text, state, prior_user_msg=prior_user_msg)

        if embedding is None:
            embedding = generate_embedding(embed_text)
        if embedding is None:
            return

        if mode == CacheMode.SUBAGENT:
            vec_prefix = PREFIX_VECTOR_SUBAGENT_HOT  if temp == "hot" else PREFIX_VECTOR_SUBAGENT_TEMP
            rr_prefix  = PREFIX_RR_SUBAGENT_HOT      if temp == "hot" else PREFIX_RR_SUBAGENT_TEMP
            index      = INDEX_SUBAGENT_HOT           if temp == "hot" else INDEX_SUBAGENT_TEMP
            _store(client, vec_prefix, rr_prefix, index, text, response, embedding)
        else:
            vec_prefix = PREFIX_VECTOR_FULL_HOT  if temp == "hot" else PREFIX_VECTOR_FULL_TEMP
            rr_prefix  = PREFIX_RR_FULL_HOT      if temp == "hot" else PREFIX_RR_FULL_TEMP
            index      = INDEX_FULL_HOT           if temp == "hot" else INDEX_FULL_TEMP
            _store(client, vec_prefix, rr_prefix, index, text, response, embedding,
                   state=state, session_id=session_id,
                   prior_user_msg=prior_user_msg,
                   cache_scope_override=cache_scope_override)

    def flush_temp(self, mode: Optional[CacheMode] = None) -> None:
        """Flush temp indexes only. Hot indexes are never touched."""
        client = _get_cache_client()
        if client is None:
            return
        try:
            prefixes = []
            if mode is None or mode == CacheMode.SUBAGENT:
                prefixes += [PREFIX_VECTOR_SUBAGENT_TEMP, PREFIX_RR_SUBAGENT_TEMP]
            if mode is None or mode == CacheMode.FULL:
                prefixes += [PREFIX_VECTOR_FULL_TEMP, PREFIX_RR_FULL_TEMP]

            total = 0
            for prefix in prefixes:
                cursor = "0"
                while True:
                    result = client.custom_command(["SCAN", cursor, "MATCH", f"{prefix}*", "COUNT", "100"])
                    cursor = result[0].decode() if isinstance(result[0], bytes) else str(result[0])
                    keys = result[1]
                    if keys:
                        client.custom_command(["DEL"] + list(keys))
                        total += len(keys)
                    if cursor == "0":
                        break
            logger.info(f"[CACHE] Flushed {total} temp keys (mode={mode})")
        except Exception as e:
            logger.error(f"[CACHE] Flush failed: {e}")

# Singleton
_cache = SemanticCache()


def get_cache() -> SemanticCache:
    """Get the global cache instance."""
    return _cache
