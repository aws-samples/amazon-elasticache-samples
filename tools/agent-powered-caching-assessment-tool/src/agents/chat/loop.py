"""
One chat turn: call the model, run its server tools, stop at a client tool.

Talks to Amazon Bedrock through boto3's ConverseStream, which streams text and tool calls.
The model client is passed in, so tests use a fake and no test reaches AWS.

What the turn yields, in order: start, then text pieces as they arrive, a message event
for each finished message, tool_call and tool_result for each tool, action_required for
each client tool, and done. Error text is fixed; the provider's own message is logged,
never sent, because it can name the server's role and account.
"""

import json
import logging
import os
from collections.abc import Iterator
from typing import Any, Protocol

from src.agents.chat.prompts import system_prompt
from src.contracts.agent_chat import (
    MAX_TOOL_ROUNDS,
    ActionRequiredEvent,
    AgentChatRequest,
    ChatMessage,
    DoneEvent,
    ErrorEvent,
    MessageEvent,
    StartEvent,
    TextBlock,
    TextEvent,
    ToolCallEvent,
    ToolResultBlock,
    ToolResultEvent,
    ToolUseBlock,
)
from src.contracts.models import DEFAULT_MODEL_ID, takes_temperature

logger = logging.getLogger(__name__)

DEFAULT_CHAT_MODEL_ID = os.environ.get("PGCA_CHAT_MODEL_ID", DEFAULT_MODEL_ID)
MAX_TOKENS = 4096
TEMPERATURE = 0.2


class ModelClient(Protocol):
    def converse_stream(self, **kwargs: Any) -> dict: ...


class ToolRunner(Protocol):
    """How the turn sees the catalog. Implemented over src/api/agent_tools."""

    def specs(self) -> list[dict]: ...
    def executor(self, name: str) -> str: ...  # "server", "client" or "unknown"
    def kind(self, name: str) -> str: ...      # "confirm" or "form" for a client tool
    def run(self, name: str, raw: dict) -> tuple[str, dict]: ...


def to_bedrock(m: ChatMessage) -> dict:
    """A conversation message in Converse's shape."""
    blocks = []
    for b in m.content:
        if isinstance(b, TextBlock):
            blocks.append({"text": b.text})
        elif isinstance(b, ToolUseBlock):
            blocks.append({"toolUse": {"toolUseId": b.tool_use_id, "name": b.name,
                                       "input": b.input}})
        else:
            # Converse knows success and error; a declined action is an error the model reads.
            status = "success" if b.status == "success" else "error"
            content = b.content if b.status != "declined" else {
                "declined": True, "detail": "The user did not confirm this action.", **b.content}
            blocks.append({"toolResult": {"toolUseId": b.tool_use_id, "status": status,
                                          "content": [{"json": content}]}})
    return {"role": m.role, "content": blocks}


def _read_stream(stream: Any, out: dict) -> Iterator[str]:
    """Assemble one ConverseStream response, yielding text as it arrives.

    Fills out["blocks"] (TextBlock and ToolUseBlock) and out["stop"] when the stream ends.
    """
    blocks: list = []
    text: list[str] = []
    tool: dict | None = None
    stop = "end_turn"
    for event in stream:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                tool = {"id": start["toolUse"]["toolUseId"], "name": start["toolUse"]["name"],
                        "input": []}
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"]["delta"]
            if "text" in delta:
                text.append(delta["text"])
                yield delta["text"]
            elif "toolUse" in delta and tool is not None:
                tool["input"].append(delta["toolUse"].get("input", ""))
        elif "contentBlockStop" in event:
            if tool is not None:
                raw = "".join(tool["input"]) or "{}"
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {"_unparsed": raw[:2000]}
                blocks.append(ToolUseBlock(tool_use_id=tool["id"], name=tool["name"],
                                           input=parsed if isinstance(parsed, dict) else {}))
                tool = None
            elif text:
                blocks.append(TextBlock(text="".join(text)))
                text = []
        elif "messageStop" in event:
            stop = event["messageStop"].get("stopReason", "end_turn")
    if text:
        blocks.append(TextBlock(text="".join(text)))
    out["blocks"], out["stop"] = blocks, stop


def run_turn(req: AgentChatRequest, client: ModelClient, tools: ToolRunner,
             model_id: str = DEFAULT_CHAT_MODEL_ID) -> Iterator[dict]:
    """Run one turn and yield its events as plain dicts, ready to send."""
    yield StartEvent(model_id=model_id).model_dump()
    messages = [to_bedrock(m) for m in req.messages]
    system = [{"text": system_prompt(req.assessment_id,
                                     req.focus.model_dump() if req.focus else None,
                                     req.database_id)}]
    for _round in range(MAX_TOOL_ROUNDS):
        out: dict = {}
        try:
            inference = {"maxTokens": MAX_TOKENS,
                         **({"temperature": TEMPERATURE} if takes_temperature(model_id) else {})}
            response = client.converse_stream(
                modelId=model_id, system=system, messages=messages,
                toolConfig={"tools": tools.specs()}, inferenceConfig=inference)
            for piece in _read_stream(response["stream"], out):
                yield TextEvent(text=piece).model_dump()
        except Exception as e:  # noqa: BLE001 - every model failure ends the turn the same way
            logger.warning("Chat model call failed: %s", type(e).__name__)
            yield ErrorEvent(code="model_unavailable",
                             detail=f"The model could not be reached ({type(e).__name__}). "
                             "Answers without a model are still available.").model_dump()
            yield DoneEvent(reason="error").model_dump()
            return
        blocks, stop = out["blocks"], out["stop"]
        if not blocks:
            blocks = [TextBlock(text="(no reply)")]
        assistant = ChatMessage(role="assistant", content=blocks)
        messages.append(to_bedrock(assistant))
        yield MessageEvent(message=assistant).model_dump()

        uses = [b for b in blocks if isinstance(b, ToolUseBlock)]
        if stop != "tool_use" or not uses:
            yield DoneEvent(reason="end_turn").model_dump()
            return

        results: list[ToolResultBlock] = []
        pending = False
        for u in uses:
            executor = tools.executor(u.name)
            yield ToolCallEvent(tool_use_id=u.tool_use_id, name=u.name, input=u.input,
                                executor=executor).model_dump()  # type: ignore[arg-type]
            if executor == "client":
                pending = True
                yield ActionRequiredEvent(tool_use_id=u.tool_use_id, name=u.name, input=u.input,
                                          kind=tools.kind(u.name)).model_dump()  # type: ignore[arg-type]
                continue
            if executor == "server":
                status, content = tools.run(u.name, u.input)
            else:
                status, content = "error", {"code": "unknown_tool",
                                            "detail": f"There is no tool named {u.name}"}
            yield ToolResultEvent(tool_use_id=u.tool_use_id, name=u.name, status=status,  # type: ignore[arg-type]
                                  content=content).model_dump()
            results.append(ToolResultBlock(tool_use_id=u.tool_use_id, status=status,  # type: ignore[arg-type]
                                           content=content))
        if pending:
            # The browser adds the server results above to its own, in one user message.
            yield DoneEvent(reason="action_required").model_dump()
            return
        user = ChatMessage(role="user", content=results)
        messages.append(to_bedrock(user))
        yield MessageEvent(message=user).model_dump()
    yield DoneEvent(reason="max_tool_rounds").model_dump()
