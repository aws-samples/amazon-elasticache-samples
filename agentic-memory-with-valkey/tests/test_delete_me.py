from textwrap import dedent
import pytest

def test_import() -> None:
    import amzn_agentic_shopping_demo  # type: ignore # noqa: F401
