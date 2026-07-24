"""Tests for the provider-refusal rewind (backlog #105).

A pre-output refusal (Fable 5's safety classifiers) leaves a poisoned tail:
Anthropic's guidance is that the refused turn must be reset or refusals tend
to repeat. ``maybe_rewind_refused_turn`` is the backend-authoritative reset,
keyed on the ``empty_turn_refusal`` marker ``_finish_response`` stamps
(vendor/react_agent/nodes.py). The rewind is GATED to exchanges that produced
nothing besides the refusal: any tool activity or earlier output in the
exchange means the user already saw content (and side effects already ran),
so the turn stays in place and the in-message notice, delivered live as a
trailing response chunk, is the recovery surface.
"""

from __future__ import annotations

from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.core.agent_context import (
    RewindResult,
    maybe_rewind_refused_turn,
    refusal_gated_content,
    refusal_rewind_content,
    refused_turn_tail,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOTICE = "notice text attached by the graph"


def _refused_ai(model: str = "claude-fable-5") -> AIMessage:
    """The measured refused shape after _finish_response patched it."""
    return AIMessage(
        content=[
            {"type": "thinking", "thinking": "hm", "signature": "s"},
            {"type": "text", "text": NOTICE},
        ],
        response_metadata={"stop_reason": "refusal", "model_name": model},
        additional_kwargs={"empty_turn_refusal": True},
    )


def _refused_exchange(prompt: str = "poke the sandbox", anchor: str = "anchor-1"):
    return [
        HumanMessage(content=prompt, id=anchor),
        _refused_ai(),
    ]


def _tool_exchange_refused():
    """Mid-turn refusal: the exchange ran a tool before the classifier fired."""
    return [
        HumanMessage(content="audit the sandbox", id="anchor-t"),
        AIMessage(
            content="",
            tool_calls=[{"name": "bash_execute", "args": {}, "id": "tc-1"}],
        ),
        ToolMessage(content="uid=1000", tool_call_id="tc-1"),
        _refused_ai(),
    ]


class FakeAgent:
    """Just enough agent for maybe_rewind_refused_turn: the rewind facade."""

    def __init__(
        self,
        result: Optional[RewindResult] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.result = result if result is not None else RewindResult(2, 1)
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def rewind_thread(self, thread_id: str, **kwargs: Any) -> RewindResult:
        self.calls.append((thread_id, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


# ---------------------------------------------------------------------------
# refused_turn_tail — detection + the rewindable gate
# ---------------------------------------------------------------------------


class TestRefusedTurnTail:
    def test_detects_clean_exchange_as_rewindable(self):
        info = refused_turn_tail(_refused_exchange())
        assert info is not None
        assert info["rewindable"] is True
        assert info["steps"] == 1
        assert info["to_message_id"] == "anchor-1"
        assert info["prompt"] == "poke the sandbox"
        assert info["model"] == "claude-fable-5"
        assert info["notice"] == NOTICE

    def test_prompt_strips_time_trigger_prefix(self):
        prompt = (
            "[Time: 2026-07-24 10:00 (Sydney)]\n[Trigger: user_message]\n\n"
            "poke the sandbox"
        )
        info = refused_turn_tail(_refused_exchange(prompt=prompt))
        assert info is not None
        assert info["prompt"] == "poke the sandbox"

    def test_prompt_extracts_text_from_multimodal_blocks(self):
        messages = [
            HumanMessage(
                content=[
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
                id="anchor-2",
            ),
            _refused_ai(),
        ]
        info = refused_turn_tail(messages)
        assert info is not None
        assert info["rewindable"] is True
        assert info["prompt"] == "describe this"
        assert info["to_message_id"] == "anchor-2"

    def test_queued_batch_anchors_on_first_prompt_of_the_run(self):
        # A refused turn that absorbed a queued-prompt batch has one
        # HumanMessage per drained prompt; the whole run must rewind or the
        # earlier prompts dangle unanswered at the tail.
        messages = [
            HumanMessage(content="earlier turn", id="old-1"),
            AIMessage(content="answered"),
            HumanMessage(content="first queued", id="q-1"),
            HumanMessage(content="second queued", id="q-2"),
            _refused_ai(),
        ]
        info = refused_turn_tail(messages)
        assert info is not None
        assert info["rewindable"] is True
        assert info["steps"] == 2
        assert info["to_message_id"] == "q-1"
        assert info["prompt"] == "first queued\n\nsecond queued"

    def test_tool_activity_gates_the_rewind(self):
        # The exchange ran a tool: side effects happened and the user watched
        # the tool cards stream. Never rewound.
        info = refused_turn_tail(_tool_exchange_refused())
        assert info is not None
        assert info["rewindable"] is False
        assert info["notice"] == NOTICE

    def test_earlier_ai_output_gates_the_rewind(self):
        # DONE-continue shape: the exchange already delivered visible text
        # before the re-drive refused.
        messages = [
            HumanMessage(content="hi", id="a"),
            AIMessage(content="a real answer the user saw"),
            _refused_ai(),
        ]
        info = refused_turn_tail(messages)
        assert info is not None
        assert info["rewindable"] is False

    def test_resume_shape_gates_instead_of_anchoring_an_old_prompt(self):
        # A /resume re-drive has no fresh HumanMessage; the old anchor would
        # rewind a healthy prior exchange and restore a stale prompt.
        messages = [
            HumanMessage(content="the pre-halt prompt", id="old-anchor"),
            AIMessage(
                content="",
                tool_calls=[{"name": "bash_execute", "args": {}, "id": "tc-9"}],
            ),
            ToolMessage(content="result", tool_call_id="tc-9"),
            _refused_ai(),
        ]
        info = refused_turn_tail(messages)
        assert info is not None
        assert info["rewindable"] is False
        assert info["to_message_id"] is None
        assert info["prompt"] == ""

    def test_no_marker_means_no_detection(self):
        messages = [
            HumanMessage(content="hi", id="a"),
            AIMessage(
                content="a normal answer",
                response_metadata={"stop_reason": "end_turn"},
            ),
        ]
        assert refused_turn_tail(messages) is None

    def test_partial_refusal_without_marker_not_detected(self):
        # _finish_response never stamps the marker on partial-output refusals.
        messages = [
            HumanMessage(content="hi", id="a"),
            AIMessage(
                content="partial answer",
                response_metadata={"stop_reason": "refusal"},
            ),
        ]
        assert refused_turn_tail(messages) is None

    def test_non_ai_tail_not_detected(self):
        messages = _refused_exchange() + [
            ToolMessage(content="result", tool_call_id="t1"),
        ]
        assert refused_turn_tail(messages) is None

    def test_empty_messages_not_detected(self):
        assert refused_turn_tail([]) is None

    def test_missing_human_message_is_gated_not_rewound(self):
        # Degenerate state (compaction ate the prompt): a steps rewind here
        # would cut back to some OLDER HumanMessage, so it is gated.
        info = refused_turn_tail([_refused_ai()])
        assert info is not None
        assert info["rewindable"] is False


# ---------------------------------------------------------------------------
# content helpers — wording
# ---------------------------------------------------------------------------


class TestRefusalContent:
    def test_rewind_content_mentions_model_and_recovery_options(self):
        content = refusal_rewind_content("claude-fable-5")
        assert "claude-fable-5" in content
        assert "rewound" in content
        assert "different model" in content.lower()

    def test_rewind_content_unknown_model_uses_generic_label(self):
        assert refusal_rewind_content("").startswith("The model")

    def test_gated_content_offers_both_recovery_routes(self):
        content = refusal_gated_content("claude-fable-5").lower()
        assert "rewind" in content
        assert "different model" in content

    def test_never_mentions_an_input_box(self):
        # Delivered verbatim by bots, which have no composer.
        for content in (
            refusal_rewind_content("claude-fable-5"),
            refusal_gated_content("claude-fable-5"),
        ):
            assert "input box" not in content.lower()
            assert "composer" not in content.lower()


# ---------------------------------------------------------------------------
# maybe_rewind_refused_turn — rewind + payload
# ---------------------------------------------------------------------------


class TestMaybeRewindRefusedTurn:
    def test_rewinds_clean_exchange_and_returns_event_payload(self):
        agent = FakeAgent(result=RewindResult(2, 1))
        payload = maybe_rewind_refused_turn(agent, "t1", _refused_exchange())
        assert agent.calls == [("t1", {"steps": 1})]
        assert payload is not None
        assert payload["rewound"] is True
        assert payload["reason"] == "refusal"
        assert payload["removed"] == 2
        assert payload["to_message_id"] == "anchor-1"
        assert payload["prompt"] == "poke the sandbox"
        assert payload["model"] == "claude-fable-5"
        assert payload["content"] == refusal_rewind_content("claude-fable-5")

    def test_queued_batch_rewinds_the_whole_run(self):
        agent = FakeAgent(result=RewindResult(3, 2))
        messages = [
            HumanMessage(content="first queued", id="q-1"),
            HumanMessage(content="second queued", id="q-2"),
            _refused_ai(),
        ]
        payload = maybe_rewind_refused_turn(agent, "t1", messages)
        assert agent.calls == [("t1", {"steps": 2})]
        assert payload is not None
        assert payload["rewound"] is True
        assert payload["removed"] == 3
        assert payload["to_message_id"] == "q-1"
        assert payload["prompt"] == "first queued\n\nsecond queued"

    def test_tool_activity_returns_gated_payload_without_rewinding(self):
        agent = FakeAgent()
        payload = maybe_rewind_refused_turn(
            agent, "t1", _tool_exchange_refused()
        )
        assert agent.calls == []
        assert payload is not None
        assert payload["rewound"] is False
        assert payload["reason"] == "refusal"
        # The live-delivered content is the in-message notice itself, so the
        # streamed text byte-matches what history reload shows.
        assert payload["content"] == NOTICE

    def test_gated_payload_falls_back_when_notice_extraction_is_empty(self):
        agent = FakeAgent()
        bare = AIMessage(
            content=[{"type": "thinking", "thinking": "hm", "signature": "s"}],
            response_metadata={"stop_reason": "refusal", "model_name": "m1"},
            additional_kwargs={"empty_turn_refusal": True},
        )
        payload = maybe_rewind_refused_turn(agent, "t1", [bare])
        assert payload is not None
        assert payload["rewound"] is False
        assert payload["content"] == refusal_gated_content("m1")

    def test_clean_tail_never_touches_the_rewind(self):
        agent = FakeAgent()
        messages = [
            HumanMessage(content="hi", id="a"),
            AIMessage(content="fine"),
        ]
        assert maybe_rewind_refused_turn(agent, "t1", messages) is None
        assert agent.calls == []

    def test_rewind_failure_returns_gated_payload(self):
        # Fallback contract: the refused message stays in place carrying the
        # in-message notice, and the notice is still delivered live.
        agent = FakeAgent(error=RuntimeError("checkpointer unavailable"))
        payload = maybe_rewind_refused_turn(agent, "t1", _refused_exchange())
        assert payload is not None
        assert payload["rewound"] is False
        assert payload["content"] == NOTICE

    def test_zero_removed_returns_gated_payload(self):
        agent = FakeAgent(result=RewindResult(0, 0))
        payload = maybe_rewind_refused_turn(agent, "t1", _refused_exchange())
        assert payload is not None
        assert payload["rewound"] is False


# ---------------------------------------------------------------------------
# Event registration
# ---------------------------------------------------------------------------


def test_turn_rewound_is_mirrored_to_autonomous_consumers():
    from nymeria.core.event_bus import AGENT_STREAM_AUTONOMOUS_EVENT_TYPES

    assert "turn_rewound" in AGENT_STREAM_AUTONOMOUS_EVENT_TYPES
