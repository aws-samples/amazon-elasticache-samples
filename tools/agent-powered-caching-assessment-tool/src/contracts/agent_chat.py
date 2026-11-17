"""
The agent chat contract: one conversation turn in, a stream of events out.

POST /api/v1/agent/chat takes the conversation so far and streams server-sent events.
The server keeps no conversation state: the client sends the whole conversation with
every turn and appends the messages the stream gives back.

The model acts only through tools. Read-only tools run on the server. Tools that change
something, or that need values the model must not see (connection details), run in the
browser: the stream stops with `action_required` and the client sends their results
with the next turn, after the user has confirmed. Spec: .kiro/specs/chat-agent.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.databases import DATABASE_ID_PATTERN
from src.contracts.models import MODEL_ID_MAX_LENGTH, MODEL_ID_PATTERN
from src.orchestrator.job_store import JOB_ID_PATTERN

AGENT_CHAT_VERSION = "1.0"

MAX_MESSAGES = 80
MAX_BLOCKS_PER_MESSAGE = 32
MAX_TEXT_CHARS = 20_000
# Per turn: how many times the model may call tools before the server stops it.
MAX_TOOL_ROUNDS = 8


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextBlock(_Model):
    type: Literal["text"] = "text"
    text: str = Field(max_length=MAX_TEXT_CHARS)


class ToolUseBlock(_Model):
    """The model asked for a tool. Kept in the conversation as the model sent it."""

    type: Literal["tool_use"] = "tool_use"
    tool_use_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(_Model):
    """What a tool returned. `declined` when the user did not confirm a client tool."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str = Field(min_length=1, max_length=128)
    status: Literal["success", "error", "declined"]
    content: dict[str, Any] = Field(default_factory=dict)


ContentBlock = Annotated[TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")]


class ChatMessage(_Model):
    role: Literal["user", "assistant"]
    content: list[ContentBlock] = Field(min_length=1, max_length=MAX_BLOCKS_PER_MESSAGE)


class ChatFocus(_Model):
    """A report item the user is asking about."""

    kind: Literal["query", "table"]
    id: str = Field(min_length=1, max_length=256)


class AgentChatRequest(_Model):
    """One turn: the conversation so far, ending with a user message."""

    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    assessment_id: str | None = Field(
        default=None, pattern=JOB_ID_PATTERN.pattern, max_length=64,
        description="The assessment the user has open, if any")
    database_id: str | None = Field(
        default=None, pattern=DATABASE_ID_PATTERN, max_length=64,
        description="The saved database the user has open, if any. An id only: the model "
        "never gets its connection")
    focus: ChatFocus | None = Field(default=None, description="The report item in view, if any")
    model_id: str | None = Field(
        default=None, pattern=MODEL_ID_PATTERN.pattern, max_length=MODEL_ID_MAX_LENGTH,
        description="The Bedrock model for this turn. Omit for the server's default")


# ---------------------------------------------------------------------------
# Events, in the order a turn sends them: start, then text, message, tool_call,
# tool_result and action_required as they happen, then done. error comes before done.
# ---------------------------------------------------------------------------


class StartEvent(_Model):
    type: Literal["start"] = "start"
    version: str = AGENT_CHAT_VERSION
    model_id: str


class TextEvent(_Model):
    """A piece of the assistant's reply, as it streams."""

    type: Literal["text"] = "text"
    text: str


class MessageEvent(_Model):
    """A finished message. Append it to the conversation."""

    type: Literal["message"] = "message"
    message: ChatMessage


class ToolCallEvent(_Model):
    type: Literal["tool_call"] = "tool_call"
    tool_use_id: str
    name: str
    input: dict[str, Any]
    executor: Literal["server", "client", "unknown"]


class ToolResultEvent(_Model):
    """A server tool's result. Kept by the client in case the turn stops for an action."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    name: str
    status: Literal["success", "error"]
    content: dict[str, Any]


class ActionRequiredEvent(_Model):
    """A client tool the user must confirm (or fill in) before it runs."""

    type: Literal["action_required"] = "action_required"
    tool_use_id: str
    name: str
    input: dict[str, Any]
    kind: Literal["confirm", "form"]


class ErrorEvent(_Model):
    """The turn could not finish. Fixed text: never the model provider's own message."""

    type: Literal["error"] = "error"
    code: str
    detail: str


class DoneEvent(_Model):
    type: Literal["done"] = "done"
    reason: Literal["end_turn", "action_required", "max_tool_rounds", "error"]


AgentEvent = Annotated[
    StartEvent | TextEvent | MessageEvent | ToolCallEvent | ToolResultEvent
    | ActionRequiredEvent | ErrorEvent | DoneEvent,
    Field(discriminator="type"),
]
