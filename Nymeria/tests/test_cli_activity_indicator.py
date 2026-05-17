from __future__ import annotations

from cli_fixtures import FakeTerminalCapabilities

from nymeria.triggers.cli.rendering.indicator import (
    ASCII_FRAMES,
    LABEL_SEGMENT_WIDTH,
    NO_ANIMATION_FRAMES,
    PHASE_LABELS,
    UNICODE_FRAMES,
    ActivityIndicator,
    ActivityState,
    activity_state_from_ui_state,
    spinner_frames,
)
from nymeria.triggers.cli.state import (
    create_initial_state,
    reduce_stream_event,
    start_turn,
)


def test_phase_labels_match_desktop_text_with_ellipses() -> None:
    assert PHASE_LABELS == {
        "processing": "Processing...",
        "thinking": "Thinking...",
        "typing": "Streaming...",
        "formulating": "Formulating...",
        "processing_results": "Processing results...",
        "waiting": "Waiting...",
    }


def test_processing_and_thinking_become_formulating_after_quiet_timeout() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "hello", now=1.0)

    assert activity_state_from_ui_state(state, now=1.9).phase == "processing"
    assert activity_state_from_ui_state(state, now=2.01).phase == "formulating"

    state = reduce_stream_event(
        state,
        {"type": "thinking", "content": "private chain of thought"},
        now=3.0,
    )

    assert activity_state_from_ui_state(state, now=3.9).phase == "thinking"
    assert activity_state_from_ui_state(state, now=4.01).phase == "formulating"


def test_typing_phase_renders_animated_streaming_indicator() -> None:
    caps = FakeTerminalCapabilities()
    indicator = ActivityIndicator()
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "hello", now=1.0)
    state = reduce_stream_event(
        state,
        {"type": "response", "content": "Hello"},
        now=1.1,
    )

    activity = activity_state_from_ui_state(state, now=1.2)
    rendered = indicator.render_from_state(state, capabilities=caps, now=1.2)

    assert activity is not None
    assert activity.phase == "typing"
    assert rendered is not None
    assert rendered.phase == "typing"
    assert rendered.label == "Streaming..."
    assert rendered.frame in UNICODE_FRAMES


def test_queued_state_renders_waiting_without_thinking_content() -> None:
    caps = FakeTerminalCapabilities()
    indicator = ActivityIndicator()
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "hello", now=1.0)
    state = reduce_stream_event(
        state,
        {"type": "thinking", "content": "do not show this"},
        now=1.1,
    )
    state = reduce_stream_event(
        state,
        {"type": "queued", "holder": "worker-1"},
        now=1.2,
    )

    rendered = indicator.render_from_state(state, capabilities=caps, now=1.3)

    assert rendered is not None
    assert rendered.phase == "waiting"
    assert rendered.label == "Waiting..."
    assert "thread lock: worker-1" in rendered.text
    assert "do not show this" not in rendered.text


def test_running_tool_names_are_safe_waiting_details() -> None:
    caps = FakeTerminalCapabilities(supports_unicode=False)
    indicator = ActivityIndicator()
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "use a tool", now=1.0)
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

    rendered = indicator.render_from_state(state, capabilities=caps, now=1.2)

    assert rendered is not None
    assert rendered.phase == "waiting"
    assert rendered.detail == "search_memory"
    assert rendered.frame in ASCII_FRAMES
    assert "private" not in rendered.text


def test_spinner_frames_follow_unicode_ascii_and_off_capabilities() -> None:
    unicode_caps = FakeTerminalCapabilities()
    ascii_caps = FakeTerminalCapabilities(supports_unicode=False)
    off_caps = FakeTerminalCapabilities(supports_animation=False)

    assert spinner_frames(unicode_caps) == UNICODE_FRAMES
    assert spinner_frames(ascii_caps) == ASCII_FRAMES
    assert spinner_frames(off_caps) == NO_ANIMATION_FRAMES


def test_spinner_ticks_independently_from_activity_changes() -> None:
    caps = FakeTerminalCapabilities(supports_unicode=False)
    indicator = ActivityIndicator(frame_interval_seconds=0.1)
    activity = ActivityState(phase="thinking", updated_at=0.0, active=True)

    first = indicator.render(activity, capabilities=caps, now=0.0)
    same_frame = indicator.render(activity, capabilities=caps, now=0.05)
    next_frame = indicator.render(activity, capabilities=caps, now=0.11)

    assert first is not None
    assert same_frame is not None
    assert next_frame is not None
    assert first.frame == "|"
    assert same_frame.frame == "|"
    assert next_frame.frame == "/"


def test_disabled_animation_keeps_label_without_spinner_noise() -> None:
    caps = FakeTerminalCapabilities(supports_animation=False)
    indicator = ActivityIndicator()
    activity = ActivityState(phase="processing", updated_at=0.0, active=True)

    rendered = indicator.render(activity, capabilities=caps, now=0.0)

    assert rendered is not None
    assert rendered.frame == ""
    assert rendered.text.startswith("Processing...")
    assert "|" not in rendered.text
    assert "/" not in rendered.text


def test_label_segment_is_stable_and_long_detail_truncates_to_width() -> None:
    caps = FakeTerminalCapabilities(supports_unicode=False, width=40)
    indicator = ActivityIndicator()
    thinking = ActivityState(phase="thinking", detail="x" * 100, active=True)
    results = ActivityState(phase="processing_results", detail="x" * 100, active=True)

    thinking_render = indicator.render(thinking, capabilities=caps, width=40, now=0.0)
    results_render = indicator.render(results, capabilities=caps, width=40, now=0.0)

    assert thinking_render is not None
    assert results_render is not None
    assert len(thinking_render.label_segment) == LABEL_SEGMENT_WIDTH
    assert len(results_render.label_segment) == LABEL_SEGMENT_WIDTH
    assert len(thinking_render.text) == 40
    assert len(results_render.text) == 40
    assert thinking_render.text.endswith("...")
    assert results_render.text.endswith("...")
