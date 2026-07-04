from __future__ import annotations

from cli_fixtures import FakeTerminalCapabilities
from rich.cells import cell_len

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

    assert text.startswith(
        "Nymeria | Ready | local agent | claude-test-model | "
        "thread Fixture thread | ctx 410/1.0k ["
    )
    assert text.endswith("] 41% | cwd /opt/NymeriaOS")


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
        context=StatusBarContext(connection_label="api ok 24ms"),
        now=1.6,
    )

    assert "Thinking..." in text
    assert "0.6s" in text
    assert "api ok 24ms" in text
    assert "do not show raw thinking" not in text


def test_status_bar_shows_fast_mode_indicator() -> None:
    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(width=120)
    state = create_initial_state(thread_id="thread-1", now=0.0)

    text = renderer.render_text(
        state,
        capabilities=caps,
        context=StatusBarContext(
            connection_label="api ok 24ms",
            model="gpt-fast",
            fast_mode_active=True,
        ),
        now=0.0,
    )

    assert "gpt-fast" in text
    assert "FAST" in text


def test_status_bar_omits_model_when_disconnected() -> None:
    # dev-todo #37: a CLI started before the backend must not present its local
    # Settings default as the active model. The disconnected label carries the
    # state; no model segment is shown.
    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(width=120)
    state = create_initial_state(thread_id="thread-1", now=0.0)

    text = renderer.render_text(
        state,
        capabilities=caps,
        context=StatusBarContext(
            connection_label="disconnected",
            model="claude-sonnet-4-6",
            disconnected=True,
        ),
        now=0.0,
    )

    assert "disconnected" in text
    assert "claude-sonnet-4-6" not in text


def test_status_bar_shows_connected_backend_model() -> None:
    # When connected, the fetched backend model renders even before the first
    # turn streams an active_model.
    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(width=120)
    state = create_initial_state(thread_id="thread-1", now=0.0)

    text = renderer.render_text(
        state,
        capabilities=caps,
        context=StatusBarContext(
            connection_label="api ok 24ms",
            model="gpt-5.5",
            disconnected=False,
        ),
        now=0.0,
    )

    assert "gpt-5.5" in text


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


def test_status_bar_fitting_uses_terminal_cell_width_for_unicode() -> None:
    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(width=42)
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "think", now=1.0)
    state = reduce_stream_event(
        state,
        {"type": "thinking", "content": "private"},
        now=1.1,
    )

    text = renderer.render_text(
        state,
        capabilities=caps,
        context=StatusBarContext(
            connection_label="api ok 24ms",
            thread_label="測試" * 20,
            model="provider/model-with-a-long-name",
            notice=StatusNotice(
                message="Queued follow-up ⠋",
                created_at=1.0,
                ttl_seconds=10.0,
            ),
            cwd="/opt/NymeriaOS",
        ),
        width=42,
        now=1.2,
    )

    assert "\n" not in text
    assert cell_len(text) <= 42


def test_status_bar_fragments_keep_text_fitted_and_semantic_styles() -> None:
    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(width=60)
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "think", now=1.0)
    state = reduce_stream_event(
        state,
        {"type": "thinking", "content": "private"},
        now=1.1,
    )

    rendered = renderer.render(
        state,
        capabilities=caps,
        context=StatusBarContext(
            connection_label="api ok 24ms",
            thread_label="Fixture thread",
            notice=StatusNotice(
                message="Queued follow-up",
                level="warning",
                created_at=1.0,
                ttl_seconds=10.0,
            ),
        ),
        width=60,
        now=1.2,
    )
    fragment_text = "".join(text for _style, text in rendered.fragments)
    fragment_styles = {style for style, _text in rendered.fragments}

    assert fragment_text == rendered.text
    assert cell_len(fragment_text) <= 60
    assert "class:status.spinner" in fragment_styles
    assert "class:status.separator" in fragment_styles
    assert "class:status.notice.warning" in fragment_styles


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

    label = context_usage_label(state)
    assert label.startswith("ctx 25/100 [")
    assert label.endswith("] 25%")
    assert format_duration(4.24) == "4.2s"
    assert format_duration(12.6) == "13s"
    assert format_duration(65.0) == "1m05s"


def test_context_usage_label_caps_bar_at_compact_trigger_by_mode() -> None:
    from types import SimpleNamespace

    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "hello", now=1.0)
    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "context_stats": {"total_tokens": 100, "context_window": 1000},
        },
        now=1.1,
    )

    tokens_settings = SimpleNamespace(
        compact_threshold_mode="tokens",
        compact_threshold_tokens=500,
        compact_threshold=0.8,
    )
    tokens_label = context_usage_label(state, compact_settings=tokens_settings)
    assert tokens_label.startswith("ctx 100/500 [")
    assert tokens_label.endswith("] 10%")

    oversized_settings = SimpleNamespace(
        compact_threshold_mode="tokens",
        compact_threshold_tokens=5_000,
        compact_threshold=0.8,
    )
    clamped_label = context_usage_label(state, compact_settings=oversized_settings)
    assert clamped_label.startswith("ctx 100/1.0k [")

    percent_settings = SimpleNamespace(
        compact_threshold_mode="percentage",
        compact_threshold=0.5,
        compact_threshold_tokens=200_000,
    )
    percent_label = context_usage_label(state, compact_settings=percent_settings)
    assert percent_label.startswith("ctx 100/500 [")

    no_settings_label = context_usage_label(state)
    assert no_settings_label.startswith("ctx 100/1.0k [")


def test_context_usage_label_prefers_backend_resolved_trigger() -> None:
    from types import SimpleNamespace

    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "hello", now=1.0)
    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "context_stats": {
                "total_tokens": 100,
                "context_window": 1000,
                "compact_trigger_tokens": 500,
            },
        },
        now=1.1,
    )

    # Local settings disagree (800); the backend-resolved trigger wins.
    settings = SimpleNamespace(
        compact_threshold_mode="tokens",
        compact_threshold_tokens=800,
        compact_threshold=0.8,
    )
    label = context_usage_label(state, compact_settings=settings)
    assert label.startswith("ctx 100/500 [")


def test_context_usage_label_null_backend_trigger_disables_compact_cap() -> None:
    from types import SimpleNamespace

    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "hello", now=1.0)
    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "context_stats": {
                "total_tokens": 100,
                "context_window": 1000,
                "compact_trigger_tokens": None,
            },
        },
        now=1.1,
    )

    # The key is present but null: auto-compact is off for this thread, so
    # the bar caps at the full limit even though local settings have one.
    settings = SimpleNamespace(
        compact_threshold_mode="tokens",
        compact_threshold_tokens=500,
        compact_threshold=0.8,
    )
    label = context_usage_label(state, compact_settings=settings)
    assert label.startswith("ctx 100/1.0k [")


def test_status_segments_come_from_keyed_registry_in_default_order() -> None:
    renderer = StatusBarRenderer()
    assert renderer.segment_keys == (
        "brand",
        "activity",
        "notice",
        "connection",
        "model",
        "fast",
        "reasoning",
        "thread",
        "context",
        "tps",
        "queued",
        "cwd",
    )


def test_register_segment_supports_append_anchor_override_and_remove() -> None:
    from nymeria.triggers.cli.rendering.status_bar import StatusSegment

    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(width=200)
    state = create_initial_state(thread_id="thread-1", now=0.0)

    def latency_provider(r, s, capabilities, context, now):
        return StatusSegment(text="42 ms", priority=1, min_width=5)

    # Append by default.
    renderer.register_segment("latency", latency_provider)
    assert renderer.segment_keys[-1] == "latency"
    render = renderer.render(state, capabilities=caps, width=200, now=5.0)
    assert "42 ms" in render.segments

    # Re-register in place: order is unchanged.
    def latency_provider_v2(r, s, capabilities, context, now):
        return StatusSegment(text="99 ms", priority=1, min_width=5)

    keys_before = renderer.segment_keys
    renderer.register_segment("latency", latency_provider_v2)
    assert renderer.segment_keys == keys_before
    render = renderer.render(state, capabilities=caps, width=200, now=5.0)
    assert "99 ms" in render.segments

    # Anchored insertion.
    def flag_provider(r, s, capabilities, context, now):
        return StatusSegment(text="flag", priority=1, min_width=4)

    renderer.register_segment("flag", flag_provider, before="thread")
    keys = renderer.segment_keys
    assert keys.index("flag") == keys.index("thread") - 1

    # Removal by key.
    assert renderer.remove_segment("latency") is True
    assert renderer.remove_segment("latency") is False
    render = renderer.render(state, capabilities=caps, width=200, now=5.0)
    assert not any("ms" in segment for segment in render.segments)


def test_tokens_per_second_segment_shows_after_recorded_turn() -> None:
    """The built-in tps segment renders the server-computed rate from the
    done event's context_stats and hides when no rate is known (#63)."""
    renderer = StatusBarRenderer()
    caps = FakeTerminalCapabilities(width=200)
    state = create_initial_state(thread_id="thread-1", now=0.0)

    # No rate yet: segment hidden.
    render = renderer.render(state, capabilities=caps, width=200, now=1.0)
    assert not any("tok/s" in segment for segment in render.segments)

    state = start_turn(state, "hello", now=1.0)
    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "model": "claude-test-model",
            "context_stats": {
                "used_tokens": 410,
                "max_tokens": 1000,
                "output_tokens": 300,
                "turn_recorded": True,
                "turn_llm_seconds": 7.1,
                "tokens_per_second": 42.3,
            },
        },
        now=2.0,
    )
    render = renderer.render(state, capabilities=caps, width=200, now=3.0)
    assert "42 tok/s" in render.segments

    # Sub-10 rates keep one decimal.
    state = start_turn(state, "again", now=4.0)
    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "model": "claude-test-model",
            "context_stats": {
                "output_tokens": 30,
                "turn_recorded": True,
                "tokens_per_second": 6.4,
            },
        },
        now=5.0,
    )
    render = renderer.render(state, capabilities=caps, width=200, now=6.0)
    assert "6.4 tok/s" in render.segments
