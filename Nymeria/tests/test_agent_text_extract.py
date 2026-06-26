"""Tests for the agent_text_extract leaf module (slice 02 F5).

These cover the move invariants (re-export identity, the no-cycle leaf guard) and
direct behavior of the primitives now imported straight from the new module.
The pre-existing tests in ``test_agent_history.py`` exercise the same functions
through the ``agent_history`` re-export surface and stay green unchanged.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

from nymeria.core import agent_history as agent_history_module
from nymeria.core import agent_text_extract as ate

# Every symbol relocated from agent_history.py into agent_text_extract.py.
MOVED_SYMBOLS = [
    "extract_mime_from_data_url",
    "_walk_reasoning_value",
    "extract_reasoning_text_from_block",
    "extract_reasoning_text_from_details",
    "THINK_OPEN",
    "THINK_CLOSE",
    "strip_inline_thinking_text",
    "trailing_marker_prefix_length",
    "InlineThinkingTextStripper",
    "extract_content_parts",
    "extract_reasoning_parts",
]


# --- Move invariants -------------------------------------------------------


def test_reexport_identity_for_all_moved_symbols():
    """agent_history re-exports the exact same objects as agent_text_extract."""
    for name in MOVED_SYMBOLS:
        assert hasattr(ate, name), f"{name} missing from agent_text_extract"
        assert hasattr(agent_history_module, name), f"{name} not re-exported by agent_history"
        assert getattr(agent_history_module, name) is getattr(ate, name), (
            f"re-export identity broken for {name}"
        )


def test_agent_text_extract_is_a_leaf_module():
    """The new module must stay a leaf: no relative (agent-family) imports and
    no third-party deps, so it can never close an import cycle. Future stdlib
    additions are allowed."""
    source = Path(ate.__file__).read_text()
    tree = ast.parse(source)
    relative = []
    top_level = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                relative.append(node.module or "(package)")
            elif node.module:
                top_level.append(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            top_level.extend(alias.name.split(".")[0] for alias in node.names)
    assert not relative, f"leaf module must not use relative imports: {relative}"
    external = [m for m in top_level if m not in sys.stdlib_module_names]
    assert not external, f"leaf module imported non-stdlib modules: {external}"


# --- extract_mime_from_data_url -------------------------------------------


def test_extract_mime_from_data_url_parses_and_falls_back():
    assert ate.extract_mime_from_data_url("data:image/png;base64,AAAA") == "image/png"
    assert ate.extract_mime_from_data_url("data:image/jpeg,xxx") == "image/jpeg"
    assert ate.extract_mime_from_data_url("https://x/y.png") == "application/octet-stream"


# --- reasoning extractors --------------------------------------------------


def test_extract_reasoning_text_from_block_joins_content_then_summary():
    block = {"reasoning": "think-", "content": "more", "summary": ["s1", "s2"]}
    assert ate.extract_reasoning_text_from_block(block) == ["think-more\n\ns1\n\ns2"]


def test_extract_reasoning_text_from_block_drops_encrypted():
    # The encrypted-skip fires on nested dicts encountered during the walk.
    block = {"reasoning": [{"type": "reasoning.encrypted", "text": "secret"}]}
    assert ate.extract_reasoning_text_from_block(block) == []


def test_extract_reasoning_text_from_details_concatenates():
    details = [{"text": "alpha"}, {"text": "beta"}]
    assert ate.extract_reasoning_text_from_details(details) == ["alphabeta"]


def test_walk_reasoning_value_recurses_and_skips_empty():
    collected: list[str] = []
    ate._walk_reasoning_value(["", None, "x", {"text": "y"}], collected.append)
    assert collected == ["x", "y"]


def test_extract_reasoning_parts_dedupes_and_orders_summary_first():
    msg = SimpleNamespace(
        additional_kwargs={
            "reasoning_content": [{"summary": ["s"]}, {"reasoning": "r"}, "r"],
        }
    )
    # summary 's' first (its precedence), then 'r' once (seen-set dedupe).
    assert ate.extract_reasoning_parts(msg) == ["s", "r"]


# --- inline-think sanitizer ------------------------------------------------


def test_strip_inline_thinking_text_removes_tagged_spans():
    assert ate.strip_inline_thinking_text("a<think>hidden</think>b") == "ab"
    assert ate.strip_inline_thinking_text("plain") == "plain"


def test_trailing_marker_prefix_length_detects_partial_marker():
    assert ate.trailing_marker_prefix_length("hello<th") == 3
    assert ate.trailing_marker_prefix_length("hello") == 0


def test_inline_thinking_stripper_streams_across_chunks():
    stripper = ate.InlineThinkingTextStripper()
    out = stripper.process_text("before<thi")
    out += stripper.process_text("nk>secret</think>after")
    out += stripper.flush()
    assert "secret" not in out
    assert out.startswith("before")
    assert out.endswith("after")


# --- content parts ---------------------------------------------------------


def test_extract_content_parts_splits_text_and_thinking():
    content = [
        {"type": "text", "text": "visible"},
        {"type": "thinking", "thinking": "pondering"},
    ]
    text, thinking = ate.extract_content_parts(content)
    assert text == "visible"
    assert thinking == ["pondering"]


def test_extract_content_parts_strips_inline_think_in_string():
    text, thinking = ate.extract_content_parts("hi<think>x</think>there")
    assert text == "hithere"
    assert thinking == []
