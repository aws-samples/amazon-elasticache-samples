"""
Lightweight spaCy rule engine for ShopNow cache routing.
No LLM calls — PhraseMatcher (exact phrases) + Matcher (token patterns).

Usage:
    from agentic_shopping_demo.intent_classifier import classify_intent, CacheScope

    result = classify_intent(
        text="98121",
        has_prior_context=True,
    )
    # result.cache_scope  -> CacheScope.SESSION
    # result.fired_rules  -> ["token:ZIP_FRAGMENT:98121"]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache

import spacy
from spacy.lang.en import English
from spacy.matcher import Matcher, PhraseMatcher

from agentic_shopping_demo.intent_rules_config import (
    CONFIRMATION_LABELS,
    CONSTRAINT_FRAGMENT_LABELS,
    CONTINUATION_LABELS,
    DEICTIC_LABELS,
    LIVE_LOCATION_LABELS,
    PHRASE_RULES,
    STANDALONE_INTENT_LABELS,
    TOKEN_PATTERNS,
)

logger = logging.getLogger(__name__)


class CacheScope(str, Enum):
    GLOBAL = "global"   # safe to serve to any session
    SESSION = "session" # scope to this session — prior turn is part of the key
    SKIP = "skip"       # do not read from or write to cache


@dataclass
class IntentSignals:
    # Independent boolean signals
    is_confirmation: bool = False
    is_continuation: bool = False
    is_live_location: bool = False
    is_standalone_intent: bool = False
    has_deictic_reference: bool = False
    has_constraint_fragment: bool = False
    is_context_dependent: bool = False  # derived

    # Routing output
    cache_scope: CacheScope = CacheScope.GLOBAL

    # Debug — every rule that fired, format "phrase|token:LABEL:matched_text"
    fired_rules: list[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def _build_matchers() -> tuple[English, PhraseMatcher, Matcher]:
    nlp = English()  # tokenizer-only, fast

    # PhraseMatcher — case-insensitive exact phrases
    pm = PhraseMatcher(nlp.vocab, attr="LOWER")
    for label, phrases in PHRASE_RULES.items():
        pm.add(label, [nlp.make_doc(p) for p in phrases])

    # Token Matcher — sequence patterns
    tm = Matcher(nlp.vocab)
    for entry in TOKEN_PATTERNS:
        tm.add(entry["label"], [entry["pattern"]])

    return nlp, pm, tm


def classify_intent(
    text: str,
    has_prior_context: bool = False,
) -> IntentSignals:
    """
    Run rule matchers against text and derive cache routing signal.

    Args:
        text: raw user message
        has_prior_context: True if the session has at least one prior turn
    """
    sig = IntentSignals()
    nlp, pm, tm = _build_matchers()
    doc = nlp(text)

    # ── PhraseMatcher pass ────────────────────────────────────────────────────
    for match_id, start, end in pm(doc):
        label = nlp.vocab.strings[match_id]
        span_text = doc[start:end].text
        sig.fired_rules.append(f"phrase:{label}:{span_text}")
        _apply_label(sig, label)

    # ── Token Matcher pass ────────────────────────────────────────────────────
    for match_id, start, end in tm(doc):
        label = nlp.vocab.strings[match_id]
        span_text = doc[start:end].text
        sig.fired_rules.append(f"token:{label}:{span_text}")
        _apply_label(sig, label)

    # ── Derive is_context_dependent ──────────────────────────────────────────
    # A standalone intent (e.g. "find red shoes") is self-contained even with
    # constraint fragments, so it should NOT be marked context-dependent.
    sig.is_context_dependent = (
        sig.is_confirmation
        or sig.is_continuation
        or sig.has_deictic_reference
        or (sig.has_constraint_fragment and has_prior_context and not sig.is_standalone_intent)
    )

    # ── Derive cache_scope ────────────────────────────────────────────────────
    if sig.is_live_location:
        sig.cache_scope = CacheScope.SKIP
    elif sig.is_context_dependent:
        sig.cache_scope = CacheScope.SESSION
    else:
        sig.cache_scope = CacheScope.GLOBAL

    if sig.fired_rules:
        logger.debug("[INTENT] %r fired=%s scope=%s", text, sig.fired_rules, sig.cache_scope)
    else:
        logger.debug("[INTENT] %r no rules fired scope=%s", text, sig.cache_scope)

    return sig


def _apply_label(sig: IntentSignals, label: str) -> None:
    if label in CONFIRMATION_LABELS:
        sig.is_confirmation = True
    if label in CONTINUATION_LABELS:
        sig.is_continuation = True
    if label in LIVE_LOCATION_LABELS:
        sig.is_live_location = True
    if label in STANDALONE_INTENT_LABELS:
        sig.is_standalone_intent = True
    if label in DEICTIC_LABELS:
        sig.has_deictic_reference = True
    if label in CONSTRAINT_FRAGMENT_LABELS:
        sig.has_constraint_fragment = True


def debug_match(text: str) -> list[tuple[str, str, str]]:
    """Return all fired rules for a text — useful for tuning."""
    nlp, pm, tm = _build_matchers()
    doc = nlp(text)
    hits = []
    for match_id, start, end in pm(doc):
        hits.append(("PHRASE", nlp.vocab.strings[match_id], doc[start:end].text))
    for match_id, start, end in tm(doc):
        hits.append(("TOKEN", nlp.vocab.strings[match_id], doc[start:end].text))
    return hits
