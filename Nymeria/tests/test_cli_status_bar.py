from __future__ import annotations

from cli_fixtures import FakeTerminalCapabilities

from nymeria.triggers.cli.rendering.status_bar import (
    StatusBarContext,
    StatusBarRenderer,
    StatusNotice,
    context_usage_label,
    format_duration,
)
from nymeria.triggers.cli.state import (
    create_initial_state,
    reduce_stream_event,
    start_turn,
)


def test_ready_status_bar_includes_operational_context() -> None:
    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(width=160)
    state = create_initial_state(thread_id="thread-1", user_id="alice", now=0.0)
    state = start_turn(state, "hello", now=1.0)
    state = reduce_stream_event(
        state,
        {"type": "response", "content": "Hello"},
        now=1.1,
    )
    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "model": "claude-test-model",
            "context_stats": {"used_tokens": 410, "max_tokens": 1000},
        },
        now=1.2,
    )

    text = renderer.render_text(
        state,
        capabilities=caps,
        context=StatusBarContext(
            connection_label="local agent",
            thread_label="Fixture thread",
            cwd="/opt/NymeriaOS",
        ),
        now=2.0,
    )

    assert text == (
        "Nymeria | Ready | local agent | claude-test-model | "
        "thread Fixture thread | ctx 41% | cwd /opt/NymeriaOS"
    )


def test_activity_status_uses_desktop_label_duration_and_safe_detail() -> None:
    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(supports_unicode=False, width=120)
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "think", now=1.0)
    state = reduce_stream_event(
        state,
        {"type": "thinking", "content": "do not show raw thinking"},
        now=1.2,
    )

    text = renderer.render_text(
        state,
        capabilities=caps,
        context=StatusBarContext(connection_label="api http://localhost:8000"),
        now=1.6,
    )

    assert "Thinking..." in text
    assert "0.6s" in text
    assert "api http://localhost:8000" in text
    assert "do not show raw thinking" not in text


def test_quiet_activity_status_becomes_formulating() -> None:
    renderer = StatusBarRenderer()
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "think", now=1.0)
    state = reduce_stream_event(
        state,
        {"type": "thinking", "content": "private"},
        now=1.2,
    )

    text = renderer.render_text(
        state,
        capabilities=FakeTerminalCapabilities(supports_unicode=False, width=100),
        now=2.3,
    )

    assert "Formulating..." in text
    assert "private" not in text


def test_waiting_and_processing_results_labels_match_activity_indicator() -> None:
    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(supports_unicode=False, width=120)
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "tool", now=1.0)
    state = reduce_stream_event(
        state,
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "search_memory",
            "args": {"query": "private"},
        },
        now=1.1,
    )

    waiting = renderer.render_text(state, capabilities=caps, now=1.2)
    assert "Waiting..." in waiting
    assert "search_memory" in waiting
    assert "private" not in waiting

    state = reduce_stream_event(
        state,
        {
            "type": "tool_result",
            "id": "call-1",
            "name": "search_memory",
            "result": "done",
        },
        now=1.3,
    )

    results = renderer.render_text(state, capabilities=caps, now=1.4)
    assert "Processing results..." in results


def test_status_notice_uses_ttl_and_prefixes_warning_or_error() -> None:
    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(width=120)
    state = create_initial_state(thread_id="thread-1", now=0.0)
    notice = StatusNotice(
        message="Backend unavailable",
        level="warning",
        created_at=10.0,
        ttl_seconds=5.0,
    )

    visible = renderer.render_text(
        state,
        capabilities=caps,
        context=StatusBarContext(notice=notice),
        now=14.0,
    )
    expired = renderer.render_text(
        state,
        capabilities=caps,
        context=StatusBarContext(notice=notice),
        now=16.0,
    )

    assert "Warning: Backend unavailable" in visible
    assert "Backend unavailable" not in expired

    state = reduce_stream_event(
        state,
        {
            "type": "error",
            "content": "Stream disconnected",
            "code": "connection_failed",
        },
        now=20.0,
    )

    error_visible = renderer.render_text(state, capabilities=caps, now=23.0)
    error_expired = renderer.render_text(state, capabilities=caps, now=27.0)

    assert "Error: Stream disconnected" in error_visible
    assert "Stream disconnected" not in error_expired


def test_long_thread_titles_truncate_instead_of_overflowing_width() -> None:
    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(width=70)
    state = create_initial_state(thread_id="thread-1", now=0.0)
    long_thread = "A very long thread title " + ("x" * 100)
    long_model = "provider/" + ("model-" * 20)

    text = renderer.render_text(
        state,
        capabilities=caps,
        context=StatusBarContext(
            connection_label="local agent",
            thread_label=long_thread,
            model=long_model,
            cwd="/opt/NymeriaOS/some/deep/path",
        ),
        width=70,
        now=0.0,
    )

    assert len(text) <= 70
    assert "thread" in text
    assert text.endswith("...")


def test_context_usage_and_duration_format_helpers() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "hello", now=1.0)
    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "context_stats": {"total_tokens": 25, "context_window": 100},
        },
        now=1.1,
    )

    assert context_usage_label(state) == "ctx 25%"
    assert format_duration(4.24) == "4.2s"
    assert format_duration(12.6) == "13s"
    assert format_duration(65.0) == "1m05s"
