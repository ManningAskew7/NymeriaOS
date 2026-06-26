"""Slice 27 F10: NymeriaAgent public lifecycle / busy accessors.

``stop_ticker()`` and ``is_thread_busy(thread_id)`` are narrow public methods
that replace cross-module private-attribute reach-ins (``agent._ticker``,
``agent._thread_locks``), matching the agent facade-seam convention (cf. slice
07 F10's ``get_llm_config_for_thread``). These tests construct a bare agent
(``__init__`` patched to a no-op) and assert each accessor delegates to the
underlying primitive without changing its behavior.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from nymeria.core.agent import NymeriaAgent


def _bare_agent() -> NymeriaAgent:
    with patch.object(NymeriaAgent, "__init__", lambda self: None):
        return NymeriaAgent()


# --------------------------------------------------------------------------- #
# is_thread_busy
# --------------------------------------------------------------------------- #
def test_is_thread_busy_delegates_to_thread_locks():
    agent = _bare_agent()
    seen: list[str] = []
    agent._thread_locks = cast(
        Any, SimpleNamespace(is_thread_busy=lambda tid: (seen.append(tid) or True))
    )

    assert agent.is_thread_busy("thread-7") is True
    assert seen == ["thread-7"]


def test_is_thread_busy_returns_false_when_free():
    agent = _bare_agent()
    agent._thread_locks = cast(Any, SimpleNamespace(is_thread_busy=lambda tid: False))

    assert agent.is_thread_busy("thread-7") is False


# --------------------------------------------------------------------------- #
# stop_ticker
# --------------------------------------------------------------------------- #
def test_stop_ticker_returns_false_when_no_ticker():
    agent = _bare_agent()
    agent._ticker = None

    assert agent.stop_ticker() is False


def test_stop_ticker_stops_running_ticker():
    agent = _bare_agent()
    stopped: list[bool] = []
    agent._ticker = cast(Any, SimpleNamespace(stop=lambda: stopped.append(True)))

    assert agent.stop_ticker() is True
    assert stopped == [True]


def test_stop_ticker_propagates_stop_errors():
    agent = _bare_agent()

    def boom():
        raise RuntimeError("ticker boom")

    agent._ticker = cast(Any, SimpleNamespace(stop=boom))

    with pytest.raises(RuntimeError, match="ticker boom"):
        agent.stop_ticker()
