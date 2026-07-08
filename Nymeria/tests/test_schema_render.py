"""Tests for the compact args-schema renderer used by deferred tool loading."""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool

from nymeria.tools.schema_render import render_tool_args_schema, tool_args_json_schema


@tool
def sr_stub_add(a: int, b: int) -> str:
    """Add two numbers (schema-render stub)."""
    return str(a + b)


@tool
def sr_stub_injected(x: int, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
    """Stub with an injected arg (schema-render stub)."""
    return str(x)


def test_render_excludes_injected_args():
    text = render_tool_args_schema(sr_stub_injected)
    assert '"x"' in text
    # injected args must never reach the model-facing schema
    assert "tool_call_id" not in text


def test_render_no_args_marker():
    @tool
    def sr_noargs() -> str:
        """No args."""
        return "ok"

    assert render_tool_args_schema(sr_noargs) == "(no arguments)"


def test_render_is_capped():
    text = render_tool_args_schema(sr_stub_add, max_chars=40)
    assert len(text) <= 40
    assert text.endswith("...")


def test_render_empty_when_no_schema():
    class _NoSchema:
        name = "x"

    assert render_tool_args_schema(_NoSchema()) == ""


def test_json_schema_dict_shape():
    js = tool_args_json_schema(sr_stub_add)
    assert js is not None
    assert set(js.get("properties", {})) == {"a", "b"}
