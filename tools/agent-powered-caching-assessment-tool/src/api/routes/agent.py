"""
The chat agent route: POST /agent/chat, one conversation turn as server-sent events.

Additive: it reads through the same route functions the UI calls and changes nothing on
its own. Tools that change something run in the browser, after the user confirms there.

Calls Amazon Bedrock with the server's own AWS credentials on every turn, like use_llm
does, so it inherits the loopback gate and needs authentication before any non-local
use (see the module docstring of src/api/main.py). Spec: .kiro/specs/chat-agent.
"""

import json
import logging
import os
import threading
from collections.abc import Iterator

from botocore.config import Config as BotoConfig
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from src.agents.chat.loop import DEFAULT_CHAT_MODEL_ID, ModelClient, run_turn
from src.api import agent_tools, audit
from src.api.common import MAX_CONCURRENT_AGENT_TURNS
from src.api.errors import ApiError, error_responses
from src.contracts.agent_chat import AgentChatRequest

logger = logging.getLogger(__name__)
router = APIRouter()

_slots = threading.BoundedSemaphore(MAX_CONCURRENT_AGENT_TURNS)
CHAT_REGION = os.environ.get("AWS_REGION", "us-east-1")


class EventStreamResponse(StreamingResponse):
    media_type = "text/event-stream"


def bedrock_client() -> ModelClient:
    """The Bedrock runtime client. Replaced in tests."""
    import boto3

    return boto3.client("bedrock-runtime", region_name=CHAT_REGION,
                        config=BotoConfig(connect_timeout=10, read_timeout=120,
                                          retries={"max_attempts": 2, "mode": "standard"}))


class _Catalog:
    """The tool catalog as the turn loop sees it."""

    def specs(self) -> list[dict]:
        return agent_tools.tool_specs()

    def executor(self, name: str) -> str:
        t = agent_tools.BY_NAME.get(name)
        return t.executor if t else "unknown"

    def kind(self, name: str) -> str:
        return agent_tools.BY_NAME[name].kind

    def run(self, name: str, raw: dict) -> tuple[str, dict]:
        return agent_tools.run_server_tool(agent_tools.BY_NAME[name], raw)


class _Slot:
    """A concurrency slot released exactly once, however the stream ends."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held = True

    def release(self) -> None:
        with self._lock:
            if self._held:
                self._held = False
                _slots.release()


def _sse(events: Iterator[dict], slot: _Slot) -> Iterator[str]:
    try:
        for e in events:
            yield f"event: {e['type']}\ndata: {json.dumps(e, separators=(',', ':'), default=str)}\n\n"
    finally:
        slot.release()


@router.post(
    "/agent/chat",
    response_class=EventStreamResponse,
    responses={
        200: {"description": "Server-sent events, one AgentEvent per event",
              "content": {"text/event-stream": {
                  "schema": {"$ref": "#/components/schemas/AgentEvent"}}}},
        **error_responses(413, 429),
    },
)
def agent_chat(req: AgentChatRequest) -> EventStreamResponse:
    """One turn of the chat agent, streamed as server-sent events.

    Send the whole conversation, ending with a user message. Each event's `data` is one
    AgentEvent. Read-only tools run on the server; a tool that changes something ends
    the turn with `action_required`, and its result comes back with the next turn after
    the user has confirmed it in the browser.

    422 when the conversation does not end with a user message. 429 when four turns are
    already running. A model failure is an `error` event with fixed text, then `done`.
    """
    if req.messages[-1].role != "user":
        raise ApiError(422, "invalid_conversation", "The conversation must end with a user message")
    if not _slots.acquire(blocking=False):
        raise ApiError(429, "agent_busy", "Too many chat turns are running. Try again shortly.",
                       headers={"Retry-After": "5"})
    slot = _Slot()
    try:
        client = bedrock_client()
    except Exception:  # noqa: BLE001 - reported in the stream, like any model failure
        client = None
    # Counts only: never the conversation, which can hold query text.
    audit.record("agent_turn", assessment_id=req.assessment_id, database_id=req.database_id,
                 messages=len(req.messages))

    def events() -> Iterator[dict]:
        if client is None:
            yield {"type": "start", "version": "1.0", "model_id": req.model_id or DEFAULT_CHAT_MODEL_ID}
            yield {"type": "error", "code": "model_unavailable",
                   "detail": "The model could not be reached. Answers without a model are "
                             "still available."}
            yield {"type": "done", "reason": "error"}
            return
        yield from run_turn(req, client, _Catalog(), model_id=req.model_id or DEFAULT_CHAT_MODEL_ID)

    return EventStreamResponse(
        _sse(events(), slot), background=BackgroundTask(slot.release),
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
