"""Pin the autonomous-fanout contract.

P1 bug (caught during refactor review): worker-relayed TODO/trigger turns
that queued behind a busy thread would yield only ``prompt_absorbed`` on
the worker's HTTP stream and lose the model's response, because
``observes_stream`` excluded autonomous sources. The fix promotes
``ticker``/``trigger``/``watchdog`` to fanout observers so the holder's
re-drive events are mirrored to the worker.

These tests pin:
1. The source classification at ``agent.astream``: autonomous sources
   observe the holder's fanout.
2. The ``task_started`` gate at ticker/trigger/chat skips every queue-meta
   event, so the first ``task_started`` reflects real model work.
"""

from __future__ import annotations

import inspect

from nymeria.core.pending_prompt_queue import PENDING_QUEUE_META_EVENT_TYPES


# Autonomous + interactive sources that ``agent.astream`` MUST treat as
# fanout observers. If the set in agent.py:astream drifts, the worker
# regression will reappear: see Nymeria/nymeria/core/agent.py:1446.
EXPECTED_OBSERVING_SOURCES = frozenset(
    {"user", "callable", "mcp", "trigger", "ticker", "watchdog"}
)

EXPECTED_QUEUE_META_EVENTS = frozenset(
    {
        "queued",
        "prompt_queued",
        "prompt_injected",
        "prompt_absorbed",
        "turn_halted",
        "fanout_dropped",
    }
)


def test_observes_stream_includes_autonomous_sources():
    """Worker-relayed sources MUST observe the holder's fanout.

    Without this, autonomous astream() callers fall into the
    fire-and-forget branch (``pending.notify_event.wait``) and the holder's
    re-drive output is yielded on the holder's stream only — the worker
    receives only ``prompt_absorbed`` and publishes ``task_completed``
    with empty content.
    """
    import nymeria.core.agent as agent_mod

    src = inspect.getsource(agent_mod.NymeriaAgent.astream)
    # The check uses a frozenset literal in the source; assert each
    # required source name appears in the observes_stream literal.
    # We re-read the source rather than executing astream to avoid
    # standing up a full agent + checkpointer in a unit test.
    marker = "observes_stream = source in {"
    assert marker in src, (
        "expected observes_stream classification block missing from "
        "NymeriaAgent.astream — fanout contract may have been refactored"
    )
    start = src.index(marker) + len(marker)
    end = src.index("}", start)
    fragment = src[start:end]
    for name in EXPECTED_OBSERVING_SOURCES:
        assert f'"{name}"' in fragment, (
            f"source {name!r} missing from observes_stream — autonomous "
            "queuers would lose their fanout"
        )


def test_pending_queue_meta_event_set_matches_protocol():
    """The shared meta-event set must cover every queue-state event.

    If a new queue event is added to the protocol but not added here,
    ticker/trigger/chat will fire ``task_started`` on it and publish an
    empty completion before the real response chunks arrive.
    """
    assert PENDING_QUEUE_META_EVENT_TYPES == EXPECTED_QUEUE_META_EVENTS


def test_ticker_task_started_gate_uses_shared_meta_set():
    """ticker.py's on_chunk must reject every queue-meta event before
    firing task_started."""
    import nymeria.core.ticker as ticker_mod

    src = inspect.getsource(ticker_mod.Ticker._stream_todo_execution)
    assert "PENDING_QUEUE_META_EVENT_TYPES" in src, (
        "ticker task_started gate must use the shared meta-event set"
    )


def test_trigger_manager_task_started_gate_uses_shared_meta_set():
    """trigger_manager.py's _stream_live must reject every queue-meta event
    before firing task_started."""
    import nymeria.core.trigger_manager as trigger_mod

    src = inspect.getsource(trigger_mod.TriggerManager._stream_live)
    assert "PENDING_QUEUE_META_EVENT_TYPES" in src, (
        "trigger_manager task_started gate must use the shared meta-event set"
    )


def test_chat_router_task_started_gate_uses_shared_meta_set():
    """api/routers/chat.py's autonomous-mirror branch must reject every
    queue-meta event before firing task_started."""
    import nymeria.api.routers.chat as chat_mod

    src = inspect.getsource(chat_mod.create_chat_router)
    assert "PENDING_QUEUE_META_EVENT_TYPES" in src, (
        "chat router task_started gate must use the shared meta-event set"
    )
