"""Ratchet for the tool-ordering sort in ``create_llm_with_tools``.

The sort (``vendor/react_agent/providers.py``, longest-name-first with name
tiebreak) reads like a llama.cpp streaming workaround, but it is
INDEPENDENTLY load-bearing for Anthropic prompt caching: part of the tool
set reaches the factory through set iteration
(``agent_graph.select_tools_for_graph`` extras) under live hash
randomization, and ``tools[]`` is the FIRST segment of the prompt-cache
prefix (tools -> system -> messages), so an unsorted list would reshuffle
per process restart and bust the entire cache (measured 2026-08-21,
``shipped/07``). This test makes that invariant enforceable rather than
advisory: deleting the sort, e.g. after the llama.cpp bug is fixed, must
turn the suite red.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from nymeria.vendor.react_agent import providers as providers_mod
from nymeria.vendor.react_agent.config import LLMConfig


class _RecordingLLM:
    """Stands in for the factory-built model; captures the bind order."""

    def __init__(self):
        self.bound = None

    def bind_tools(self, tools):
        self.bound = list(tools)
        return self


def _tool(name: str):
    return SimpleNamespace(name=name)


# Longest-first, then name: the deterministic order the sort must produce.
_EXPECTED = ["nym_todo_list", "bash_execute", "nym_todo", "ab"]


def _bound_names(tools) -> list[str]:
    cfg = LLMConfig(
        provider="anthropic",
        model="claude-opus-5",
        api_key="test-key",
        base_url=None,
    )
    llm = _RecordingLLM()
    with patch.object(providers_mod, "create_llm", return_value=llm):
        result = providers_mod.create_llm_with_tools(cfg, tools)
    assert result is llm
    assert llm.bound is not None
    return [t.name for t in llm.bound]


def test_bind_order_is_deterministic_regardless_of_input_order():
    """Two opposite input orders must bind identically, in sorted order."""
    forward = [_tool(n) for n in ["nym_todo", "bash_execute", "nym_todo_list", "ab"]]
    backward = list(reversed(forward))

    assert _bound_names(forward) == _EXPECTED
    assert _bound_names(backward) == _EXPECTED


def test_prefix_overlapping_names_bind_longest_first():
    """The llama.cpp half of the invariant: longer names precede their prefixes."""
    tools = [_tool("nym_todo"), _tool("nym_todo_list")]
    assert _bound_names(tools) == ["nym_todo_list", "nym_todo"]
