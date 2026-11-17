"""
Which Bedrock model the analysis and the chat use, and which a caller may choose.

Any model id or inference profile shaped like one is accepted: the list below is a hint
for a user interface, not a limit. A model the account cannot call shows up as a
fallback (analysis) or an error event (chat), never as a crash.
"""

import os
import re

DEFAULT_MODEL_ID = os.environ.get("PGCA_BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-5-5")

_SUGGESTED = ("global.anthropic.claude-sonnet-5-5", "global.anthropic.claude-sonnet-5",
              "global.anthropic.claude-sonnet-4-6")


def model_suggestions() -> list[str]:
    """PGCA_MODEL_SUGGESTIONS (comma-separated) or the built-in list, default first."""
    raw = os.environ.get("PGCA_MODEL_SUGGESTIONS", "")
    listed = [m.strip() for m in raw.split(",") if m.strip()] or list(_SUGGESTED)
    return [DEFAULT_MODEL_ID] + [m for m in listed if m != DEFAULT_MODEL_ID]


# A model id (anthropic.claude-...), an inference profile (global.anthropic...) or an ARN.
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:/_-]{0,199}$")
MODEL_ID_MAX_LENGTH = 200

# Newer Anthropic models reject `temperature` ("deprecated for this model"). Checked on
# Bedrock: Sonnet 5 and 5.5 reject it, Sonnet 4.6 accepts it.
_NO_TEMPERATURE = re.compile(r"claude-(sonnet|opus|haiku)-[5-9]")


def takes_temperature(model_id: str) -> bool:
    """Whether to send a temperature to this model."""
    return not _NO_TEMPERATURE.search(model_id)


def forces_tool_choice(model_id: str) -> bool:
    """Whether the model accepts a forced tool choice ("any" or a named tool).

    Sonnet 5 and 5.5 reject it on Bedrock ("tool_choice: type tool and any are not
    supported for this model"); the analysis then asks for the output tool with "auto".
    """
    return not _NO_TEMPERATURE.search(model_id)
