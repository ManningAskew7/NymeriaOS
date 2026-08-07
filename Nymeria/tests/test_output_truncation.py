"""Tests for output-truncation detection (the silent-failure backstop).

A turn that hits its output cap inside the thinking block returns a message
with a lone thinking block: no text, no tool calls. ``should_continue`` sees no
tool_calls and routes to "end", so the turn "succeeds" while delivering
nothing, and the bots drop an empty text buffer without error. Three separate
things made that invisible in production:

1. The detector only knew the OpenAI spelling (``finish_reason == "length"``).
   Anthropic sets ``stop_reason == "max_tokens"`` and never sets a
   finish_reason at all, so the check was dead code on that provider.
2. Nothing else in the runtime inspected ``stop_reason``.
3. The INFO log measured ``str(response.content)``, which renders thinking
   blocks and their base64 signatures, so a turn that produced zero visible
   text logged "Response: 19626 chars, final answer".

Both node paths are exercised. That is not symmetry for its own sake: real
interactive turns STREAM, and a streamed ``response_metadata`` is assembled
from chunks, so "stop_reason survives accumulation" is an assumption worth
pinning rather than inferring from the sync path.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from nymeria.vendor.react_agent import nodes as nodes_module
from nymeria.vendor.react_agent.config import LLMConfig, LLMFallbackConfig
from nymeria.vendor.react_agent.nodes import (
    _visible_text_of,
    _with_empty_turn_notice,
    create_agent_node,
)


def _anthropic_llm_config() -> LLMConfig:
    return LLMConfig(provider="anthropic", model="claude-sonnet-5", api_key="test-key")


class TestVisibleTextOf:
    def test_plain_string_content(self):
        assert _visible_text_of("hello") == "hello"

    def test_text_blocks_only(self):
        content = [
            {"type": "text", "text": "hel"},
            {"type": "text", "text": "lo"},
        ]
        assert _visible_text_of(content) == "hello"

    def test_thinking_blocks_are_not_visible_text(self):
        # The whole point: a thinking block is not something the user sees, and
        # its signature is multiple KB of base64 that must never be counted.
        content = [
            {
                "type": "thinking",
                "thinking": "a long internal deliberation",
                "signature": "x" * 4000,
            }
        ]
        assert _visible_text_of(content) == ""

    def test_mixed_blocks_return_only_text(self):
        content = [
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "text", "text": "the answer"},
            {"type": "tool_use", "name": "t", "input": {}},
        ]
        assert _visible_text_of(content) == "the answer"

    def test_bare_string_blocks(self):
        assert _visible_text_of(["a", "b"]) == "ab"

    def test_unknown_shapes_are_empty_not_stringified(self):
        assert _visible_text_of(None) == ""
        assert _visible_text_of({"type": "text", "text": "x"}) == ""


class TestTruncationNotice:
    def test_appends_text_block_to_block_content(self):
        msg = AIMessage(content=[{"type": "thinking", "thinking": "...", "signature": "s"}])
        patched = _with_empty_turn_notice(msg)
        assert _visible_text_of(patched.content) != ""
        # A copy, so the caller's own reference is untouched.
        assert _visible_text_of(msg.content) == ""

    def test_thinking_blocks_stay_first(self):
        # Anthropic validates thinking-block signatures on replay and rejects
        # MODIFIED blocks. Appending keeps them first and byte-identical;
        # prepending the notice would reorder the block list.
        msg = AIMessage(content=[{"type": "thinking", "thinking": "t", "signature": "s"}])
        patched = _with_empty_turn_notice(msg)
        assert patched.content[0]["type"] == "thinking"
        assert patched.content[0]["signature"] == "s"
        assert patched.content[-1]["type"] == "text"

    def test_preserves_usage_and_metadata(self):
        msg = AIMessage(
            content=[{"type": "thinking", "thinking": "...", "signature": "s"}],
            response_metadata={"stop_reason": "max_tokens"},
            usage_metadata={"input_tokens": 1, "output_tokens": 4096, "total_tokens": 4097},
        )
        patched = _with_empty_turn_notice(msg)
        assert patched.response_metadata == msg.response_metadata
        assert patched.usage_metadata == msg.usage_metadata

    def test_fills_empty_string_content(self):
        patched = _with_empty_turn_notice(AIMessage(content=""))
        assert isinstance(patched.content, str)
        assert patched.content != ""

    def test_leaves_non_empty_string_content_alone(self):
        msg = AIMessage(content="a real answer")
        assert _with_empty_turn_notice(msg).content == "a real answer"


def _thinking_only_response(metadata: dict) -> AIMessage:
    """The exact shape the outage produced."""
    return AIMessage(
        content=[
            {
                "type": "thinking",
                "thinking": "Looking at the available age",  # cut mid-sentence
                "signature": "x" * 4000,
            }
        ],
        response_metadata=metadata,
        usage_metadata={"input_tokens": 90829, "output_tokens": 4096, "total_tokens": 94925},
    )


class _FakeLLM:
    """Sync stand-in that records the per-call kwargs it was handed."""

    def __init__(self, response: AIMessage):
        self._response = response
        self.calls: list[dict] = []

    def invoke(self, _messages, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeStreamLLM:
    """Async stand-in: yields the response as chunks, records call kwargs.

    Mirrors how a real streamed turn arrives, so `stop_reason` has to survive
    chunk accumulation to be seen.
    """

    def __init__(self, response: AIMessage):
        self._response = response
        self.calls: list[dict] = []

    async def astream(self, _messages, **kwargs):
        self.calls.append(kwargs)
        yield AIMessageChunk(
            content=self._response.content,
            response_metadata=self._response.response_metadata,
            usage_metadata=self._response.usage_metadata,
            tool_calls=self._response.tool_calls,
        )


@pytest.fixture()
def events(monkeypatch):
    """Capture dispatched custom events (see tests/test_tool_node_sequential.py).

    Both spellings: _finish_response dispatches synchronously even on the
    async node, but the in-loop retry/swap events on the streaming path go
    through the async twin (adispatch_custom_event).
    """
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        nodes_module,
        "dispatch_custom_event",
        lambda name, payload, config=None: captured.append((name, payload)),
    )

    async def _acapture(name, payload, config=None):
        captured.append((name, payload))

    monkeypatch.setattr(nodes_module, "adispatch_custom_event", _acapture)
    return captured


def _run(response: AIMessage) -> AIMessage:
    node = create_agent_node(_FakeLLM(response), "system prompt")
    result = node.invoke({"messages": [HumanMessage(content="hi")]}, {"configurable": {}})
    return result["messages"][0]


async def _arun(response: AIMessage) -> AIMessage:
    node = create_agent_node(_FakeStreamLLM(response), "system prompt")
    result = await node.ainvoke(
        {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
    )
    return result["messages"][0]


class TestDeadTurnDetection:
    @pytest.mark.parametrize(
        "metadata",
        [
            {"stop_reason": "max_tokens", "model_name": "claude-sonnet-5"},  # Anthropic
            {"finish_reason": "length", "model_name": "gpt-5.5"},            # OpenAI
        ],
    )
    def test_dead_turn_gets_a_visible_body(self, metadata, caplog):
        with caplog.at_level("ERROR", logger="nymeria"):
            out = _run(_thinking_only_response(metadata))
        assert "DEAD TURN" in caplog.text
        # Silence is what made this a mystery. The turn must now say something.
        assert _visible_text_of(out.content) != ""

    def test_anthropic_spelling_was_the_dead_code(self, caplog):
        # Pins the actual defect: before the fix this response produced no log
        # line at all, because it carries no finish_reason key whatsoever.
        response = _thinking_only_response({"stop_reason": "max_tokens"})
        assert "finish_reason" not in response.response_metadata
        with caplog.at_level("ERROR", logger="nymeria"):
            _run(response)
        assert "DEAD TURN" in caplog.text

    def test_log_reports_visible_chars_not_stringified_blocks(self, caplog):
        # The old log said "19626 chars, final answer" for a turn with zero
        # visible text, because it measured str(content) including the ~4KB
        # base64 thinking signature.
        with caplog.at_level("INFO", logger="nymeria"):
            _run(_thinking_only_response({"stop_reason": "max_tokens"}))
        assert "[LLM] Response: 0 chars" in caplog.text

    @pytest.mark.asyncio
    async def test_dead_turn_detected_on_the_streaming_path(self, caplog):
        # Interactive turns stream. If stop_reason did not survive chunk
        # accumulation the detector would be dead code on the ONLY path real
        # users take, which is precisely how the original bug hid.
        with caplog.at_level("ERROR", logger="nymeria"):
            out = await _arun(_thinking_only_response({"stop_reason": "max_tokens"}))
        assert "DEAD TURN" in caplog.text
        assert _visible_text_of(out.content) != ""


class TestOutputTruncatedEvent:
    def test_dead_turn_emits_the_event(self, events):
        _run(_thinking_only_response({"stop_reason": "max_tokens"}))
        emitted = [(n, p) for n, p in events if n == "output_truncated"]
        assert len(emitted) == 1
        payload = emitted[0][1]
        assert payload["produced_output"] is False
        assert payload["reason"] == "max_tokens"
        assert payload["output_tokens"] == 4096

    def test_partial_truncation_marks_produced_output(self, events):
        _run(
            AIMessage(
                content=[{"type": "text", "text": "a partial answer"}],
                response_metadata={"stop_reason": "max_tokens"},
                usage_metadata={"input_tokens": 10, "output_tokens": 4096, "total_tokens": 4106},
            )
        )
        emitted = [(n, p) for n, p in events if n == "output_truncated"]
        assert len(emitted) == 1
        assert emitted[0][1]["produced_output"] is True

    def test_healthy_turn_emits_nothing(self, events):
        _run(AIMessage(content="fine", response_metadata={"stop_reason": "end_turn"}))
        assert [n for n, _ in events if n == "output_truncated"] == []

    @pytest.mark.asyncio
    async def test_streaming_path_emits_the_event(self, events):
        await _arun(_thinking_only_response({"stop_reason": "max_tokens"}))
        assert [n for n, _ in events if n == "output_truncated"] == ["output_truncated"]

    def test_event_is_mirrored_to_autonomous_consumers(self):
        # An autonomous briefing dying inside its thinking block is the exact
        # outage. If the type is not registered, event_bus drops it and the
        # turn is silent again for everyone not watching an interactive stream.
        from nymeria.core.event_bus import AGENT_STREAM_AUTONOMOUS_EVENT_TYPES

        assert "output_truncated" in AGENT_STREAM_AUTONOMOUS_EVENT_TYPES


def _refused_thinking_only_response(metadata: dict) -> AIMessage:
    return AIMessage(
        content=[
            {
                "type": "thinking",
                "thinking": "I'm going to test the bash tool by examining",
                "signature": "x" * 4000,
            }
        ],
        response_metadata=metadata,
        usage_metadata={"input_tokens": 65291, "output_tokens": 311, "total_tokens": 65602},
    )


class TestRefusedTurnDetection:
    """Pins the provider-refusal backstop (slim dogfood, 2026-07-24).

    Measured shape on claude-fable-5 via CLIProxy: a benign-sounding "try to
    break what the bash tool claims" prompt returned a lone thinking block,
    no text, no tool calls, stop_reason="refusal". `should_continue` read it
    as a clean finish and the turn delivered silence that was reported as a
    client bug.
    """

    @pytest.mark.parametrize(
        "metadata",
        [
            {"stop_reason": "refusal", "model_name": "claude-fable-5"},        # Anthropic
            {"finish_reason": "content_filter", "model_name": "gpt-5.5"},      # OpenAI
        ],
    )
    def test_refused_turn_gets_a_visible_body(self, metadata, caplog):
        with caplog.at_level("WARNING", logger="nymeria"):
            out = _run(_refused_thinking_only_response(metadata))
        assert "REFUSED TURN" in caplog.text
        assert _visible_text_of(out.content) != ""

    @pytest.mark.parametrize(
        "metadata",
        [
            {"stop_reason": "refusal", "model_name": "claude-fable-5"},
            {"finish_reason": "content_filter", "model_name": "gpt-5.5"},
        ],
    )
    def test_empty_refusal_stamps_the_rewind_marker(self, metadata):
        """The marker is what the post-turn rewind keys on (backlog #105)."""
        out = _run(_refused_thinking_only_response(metadata))
        assert out.additional_kwargs.get("empty_turn_refusal") is True

    def test_partial_refusal_does_not_stamp_the_rewind_marker(self):
        """The user already saw content; a partial refusal must never rewind."""
        out = _run(
            AIMessage(
                content=[{"type": "text", "text": "a partial answer"}],
                response_metadata={"stop_reason": "refusal"},
                usage_metadata={
                    "input_tokens": 10, "output_tokens": 50, "total_tokens": 60,
                },
            )
        )
        assert "empty_turn_refusal" not in out.additional_kwargs

    def test_truncated_dead_turn_does_not_stamp_the_rewind_marker(self):
        """max_tokens dead turns keep their notice-only treatment."""
        out = _run(
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "budget gone", "signature": "s"}
                ],
                response_metadata={"stop_reason": "max_tokens"},
                usage_metadata={
                    "input_tokens": 10, "output_tokens": 4096, "total_tokens": 4106,
                },
            )
        )
        assert "empty_turn_refusal" not in out.additional_kwargs

    @pytest.mark.asyncio
    async def test_refused_turn_detected_on_the_streaming_path(self, caplog):
        with caplog.at_level("WARNING", logger="nymeria"):
            out = await _arun(
                _refused_thinking_only_response({"stop_reason": "refusal"})
            )
        assert "REFUSED TURN" in caplog.text
        assert _visible_text_of(out.content) != ""

    def test_refused_turn_emits_the_event(self, events):
        _run(_refused_thinking_only_response({"stop_reason": "refusal"}))
        emitted = [(n, p) for n, p in events if n == "response_refused"]
        assert len(emitted) == 1
        payload = emitted[0][1]
        assert payload["produced_output"] is False
        assert payload["output_tokens"] == 311

    def test_partial_refusal_marks_produced_output_and_keeps_text(self, events):
        out = _run(
            AIMessage(
                content=[{"type": "text", "text": "a partial answer"}],
                response_metadata={"stop_reason": "refusal"},
                usage_metadata={"input_tokens": 10, "output_tokens": 50, "total_tokens": 60},
            )
        )
        emitted = [(n, p) for n, p in events if n == "response_refused"]
        assert len(emitted) == 1
        assert emitted[0][1]["produced_output"] is True
        # The user already saw the text; no notice is appended to it.
        assert _visible_text_of(out.content) == "a partial answer"

    def test_healthy_turn_emits_nothing(self, events):
        _run(AIMessage(content="fine", response_metadata={"stop_reason": "end_turn"}))
        assert [n for n, _ in events if n == "response_refused"] == []

    def test_event_is_mirrored_to_autonomous_consumers(self):
        from nymeria.core.event_bus import AGENT_STREAM_AUTONOMOUS_EVENT_TYPES

        assert "response_refused" in AGENT_STREAM_AUTONOMOUS_EVENT_TYPES


class TestStreamingOptUpWiring:
    """Pins the CALL SITE, not the helper.

    `streaming_call_kwargs` is tested as a pure function in
    test_provider_max_tokens.py. That proves it computes the right override and
    proves nothing about whether the node actually passes it, which is the half
    that silently breaks. Stub the helper to a sentinel and assert the sentinel
    reaches the model: if someone drops `**_streaming_kwargs(candidate)` from
    the astream call, every non-streaming caller keeps working and only the
    user-facing turn quietly loses 6x its output budget.
    """

    @pytest.mark.asyncio
    async def test_astream_receives_the_streaming_override(self, monkeypatch):
        monkeypatch.setattr(
            nodes_module, "streaming_call_kwargs", lambda candidate, resolved: {"max_tokens": 128000}
        )
        llm = _FakeStreamLLM(AIMessage(content="ok"))
        node = create_agent_node(llm, "system prompt", _anthropic_llm_config())
        await node.ainvoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )
        assert llm.calls == [{"max_tokens": 128000}]

    @pytest.mark.asyncio
    async def test_astream_passes_nothing_when_nothing_was_clamped(self, monkeypatch):
        monkeypatch.setattr(
            nodes_module, "streaming_call_kwargs", lambda candidate, resolved: {}
        )
        llm = _FakeStreamLLM(AIMessage(content="ok"))
        node = create_agent_node(llm, "system prompt", _anthropic_llm_config())
        await node.ainvoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )
        assert llm.calls == [{}]

    @pytest.mark.asyncio
    async def test_resolver_failure_does_not_break_the_turn(self, monkeypatch):
        # Discovery is best-effort. A resolver that explodes must cost the turn
        # its opt-up, never the turn itself.
        def boom(*a, **k):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(nodes_module, "resolve_max_output_tokens", boom)
        llm = _FakeStreamLLM(AIMessage(content="ok"))
        node = create_agent_node(llm, "system prompt", _anthropic_llm_config())
        out = await node.ainvoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )
        assert out["messages"][0].content == "ok"
        assert llm.calls == [{}]

    @pytest.mark.asyncio
    async def test_no_llm_config_is_a_quiet_noop(self):
        # Direct create_agent_node callers may pass no config at all.
        llm = _FakeStreamLLM(AIMessage(content="ok"))
        node = create_agent_node(llm, "system prompt")
        await node.ainvoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )
        assert llm.calls == [{}]


class TestStreamingCeilingIsPerCandidate:
    """A fallback candidate is a DIFFERENT model with a different ceiling.

    Resolving against the primary's config and handing that number to the
    fallback would send e.g. sonnet-5's 128000 to haiku-4-5, whose real cap is
    64000: a 400 on the rescue attempt, at exactly the moment the turn is
    already in trouble. Tested against the module-level helper because the
    fallback branch is otherwise only reachable by driving a real stream
    failure.
    """

    @staticmethod
    def _config_with_fallback():
        cfg = _anthropic_llm_config()
        cfg.fallbacks = [
            LLMFallbackConfig(model="claude-haiku-4-5", provider="anthropic")
        ]
        return cfg

    def _record(self, monkeypatch):
        seen: list = []
        monkeypatch.setattr(
            nodes_module,
            "resolve_max_output_tokens",
            lambda cfg, probe=False: (seen.append(cfg.model), 64000)[1],
        )
        monkeypatch.setattr(
            nodes_module, "streaming_call_kwargs", lambda candidate, resolved: {}
        )
        return seen

    def test_primary_candidate_uses_the_primary_model(self, monkeypatch):
        seen = self._record(monkeypatch)
        nodes_module._streaming_max_tokens_kwargs(
            object(), self._config_with_fallback(), 0
        )
        assert seen == ["claude-sonnet-5"]

    def test_fallback_candidate_uses_the_fallback_model(self, monkeypatch):
        seen = self._record(monkeypatch)
        nodes_module._streaming_max_tokens_kwargs(
            object(), self._config_with_fallback(), 1
        )
        assert seen == ["claude-haiku-4-5"]

    def test_out_of_range_candidate_is_a_noop(self, monkeypatch):
        seen = self._record(monkeypatch)
        assert (
            nodes_module._streaming_max_tokens_kwargs(
                object(), self._config_with_fallback(), 99
            )
            == {}
        )
        assert seen == []

    def test_no_config_is_a_noop(self):
        assert nodes_module._streaming_max_tokens_kwargs(object(), None, 0) == {}

    def test_resolution_is_memoized_per_candidate(self, monkeypatch):
        """Not an optimisation: this bounds blocking HTTP on the event loop.

        Resolution can do a blocking openrouter fetch (10s) whose FAILURE cache
        is only 60s, so on a host that cannot reach openrouter.ai an unmemoized
        call would stall the whole event loop on every astream attempt, forever.
        """
        seen = self._record(monkeypatch)
        cfg = self._config_with_fallback()
        cache: dict = {}
        for _ in range(3):
            nodes_module._streaming_max_tokens_kwargs(object(), cfg, 0, cache=cache)
        assert seen == ["claude-sonnet-5"]  # resolved once, not three times

        # A different candidate is a different model: it must resolve on its own.
        nodes_module._streaming_max_tokens_kwargs(object(), cfg, 1, cache=cache)
        assert seen == ["claude-sonnet-5", "claude-haiku-4-5"]

    def test_does_not_memoize_an_unknown_ceiling(self, monkeypatch):
        """An unknown ceiling must NOT stick to the graph.

        This asserts the OPPOSITE of what it once did, deliberately. The old
        rationale was "not caching a None re-probes every attempt, which is the
        stall the cache exists to prevent" -- true only while the probe could
        block the event loop. It cannot any more: it never does HTTP on a
        running loop, and it caches its own answer for a day, so re-asking is a
        dict lookup.

        What changed the verdict is that this cache is per GRAPH BUILD. A None
        here does not mean "this model has no ceiling", it means the probe was
        merely COLD and is warming off-loop right now. Memoizing that pins the
        graph to the degraded 4096 for its entire life instead of for one turn,
        turning a momentary miss into exactly the dead-turn bug this change
        exists to kill. Re-ask; a real ceiling still memoizes (see the sibling).
        """
        calls: list = []
        monkeypatch.setattr(
            nodes_module,
            "resolve_max_output_tokens",
            lambda cfg, probe=False: (calls.append(cfg.model), None)[1],
        )
        cache: dict = {}
        for _ in range(3):
            nodes_module._streaming_max_tokens_kwargs(
                object(), _anthropic_llm_config(), 0, cache=cache
            )
        assert len(calls) == 3
        assert cache == {}, "a cold miss must leave the cache empty, not poison it"

    def test_sync_path_sends_no_override(self):
        # The sync node rides the instance's own clamped max_tokens. The
        # override is a streaming-only concession.
        llm = _FakeLLM(AIMessage(content="ok"))
        node = create_agent_node(llm, "system prompt")
        node.invoke({"messages": [HumanMessage(content="hi")]}, {"configurable": {}})
        assert llm.calls == [{}]


class TestPartialTruncation:
    def test_truncated_but_productive_turn_warns_without_notice(self, caplog):
        response = AIMessage(
            content=[{"type": "text", "text": "a partial answer"}],
            response_metadata={"stop_reason": "max_tokens"},
            usage_metadata={"input_tokens": 10, "output_tokens": 4096, "total_tokens": 4106},
        )
        with caplog.at_level("WARNING", logger="nymeria"):
            out = _run(response)
        assert "TRUNCATED" in caplog.text
        assert "DEAD TURN" not in caplog.text
        # It said something real; do not bolt an apology onto it.
        assert _visible_text_of(out.content) == "a partial answer"

    def test_truncated_tool_call_turn_is_not_dead(self, caplog):
        response = AIMessage(
            content=[{"type": "thinking", "thinking": "...", "signature": "s"}],
            response_metadata={"stop_reason": "max_tokens"},
            tool_calls=[{"name": "trigger_info", "args": {}, "id": "call_1"}],
        )
        with caplog.at_level("WARNING", logger="nymeria"):
            _run(response)
        assert "DEAD TURN" not in caplog.text


class TestHealthyTurns:
    def test_normal_completion_is_untouched_and_quiet(self, caplog):
        response = AIMessage(
            content="the full answer",
            response_metadata={"stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 10, "output_tokens": 478, "total_tokens": 488},
        )
        with caplog.at_level("WARNING", logger="nymeria"):
            out = _run(response)
        assert out.content == "the full answer"
        assert "TRUNCATED" not in caplog.text
        assert "DEAD TURN" not in caplog.text

    def test_empty_response_without_truncation_is_not_flagged(self, caplog):
        # An empty message that did NOT hit the cap is a different problem and
        # must not be mislabelled as truncation.
        response = AIMessage(content="", response_metadata={"stop_reason": "end_turn"})
        with caplog.at_level("ERROR", logger="nymeria"):
            _run(response)
        assert "DEAD TURN" not in caplog.text

    def test_missing_metadata_is_safe(self):
        _run(AIMessage(content="ok"))


class _TransientStreamLLM:
    """Fails the first attempt (retryable 5xx), streams fine afterwards."""

    class _Transient(RuntimeError):
        status_code = 500

    def __init__(self, fail_times: int = 1):
        self._fail_times = fail_times
        self.attempts = 0

    async def astream(self, _messages, **kwargs):
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise self._Transient("boom")
        yield AIMessageChunk(content="ok")


class TestAstreamCallSiteArguments:
    """Pins the ARGUMENTS at the astream call site, not just the helper.

    The gap these close was found by mutation: `candidate_index -> 0`,
    `probe=True -> probe=False`, and dropping `cache=` from the call ALL
    survived the previous suite, because the per-candidate tests call the helper
    with literal indices and the wiring test stubs the helper out entirely. So
    the whole per-candidate rationale was protected everywhere except where it
    actually runs.
    """

    def _config_with_fallback(self):
        cfg = _anthropic_llm_config()
        cfg.stream_max_retries = 1
        cfg.fallbacks = [
            LLMFallbackConfig(provider="anthropic", model="claude-haiku-4-5")
        ]
        return cfg

    @pytest.mark.asyncio
    async def test_the_node_passes_the_live_candidate_index_and_one_shared_cache(
        self, monkeypatch
    ):
        """Mid-fallback, the node must ask about the FALLBACK, not the primary.

        Hardcoding 0 here sends the primary's ceiling to a different model, which
        is how you 400 the rescue attempt that was meant to save the turn.
        """
        seen: list[tuple[int, int]] = []

        def _recorder(candidate, llm_config, candidate_index=0, cache=None):
            # Record whether a cache arrived at all AND its identity, so both
            # "cache= dropped from the call site" (None) and "a fresh dict each
            # attempt" (differing ids) are distinguishable from one shared dict.
            # Storing id(cache) alone cannot catch the first case: id(None) is a
            # perfectly good int, so a `None not in ids` check silently passes.
            seen.append((candidate_index, cache is None, id(cache)))
            return {}

        monkeypatch.setattr(nodes_module, "_streaming_max_tokens_kwargs", _recorder)
        monkeypatch.setattr(
            nodes_module, "_llm_candidate", lambda primary, **kw: primary
        )

        llm = _TransientStreamLLM(fail_times=2)
        node = create_agent_node(llm, "system prompt", self._config_with_fallback())
        await node.ainvoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )

        indices = [i for i, _, _ in seen]
        assert indices == [0, 0, 1], f"call-site candidate_index sequence was {indices}"
        assert not any(missing for _, missing, _ in seen), (
            "the node dropped cache= at the call site, so the per-graph "
            "memoization is silently gone"
        )
        cache_ids = {cid for _, _, cid in seen}
        assert len(cache_ids) == 1, "every attempt must share one memoization cache"

    @pytest.mark.asyncio
    async def test_the_node_resolves_the_ceiling_with_the_probe_enabled(
        self, monkeypatch
    ):
        """probe=False here silently kills discovery through CLIProxy.

        That is the one deployment where no catalog knows the model, so the
        probe is the only source of the ceiling: flipping this costs ~6x the
        output budget and nothing fails loudly.
        """
        probes: list[bool] = []

        def _recording_resolve(cfg, probe=False):
            probes.append(probe)
            return 128000

        monkeypatch.setattr(
            nodes_module, "resolve_max_output_tokens", _recording_resolve
        )
        monkeypatch.setattr(
            nodes_module, "streaming_call_kwargs", lambda candidate, resolved: {}
        )
        llm = _FakeStreamLLM(AIMessage(content="ok"))
        node = create_agent_node(llm, "system prompt", _anthropic_llm_config())
        await node.ainvoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )
        assert probes, "the node never resolved a ceiling at all"
        assert all(probes), f"the ceiling was resolved with probe disabled: {probes}"


class TestColdStartCeiling:
    """The regression the loop-safety fix nearly introduced.

    Refusing to probe on the event loop is right, but the Anthropic factory
    resolves the ceiling DURING construction, and construction happens inside
    the async node, i.e. on the loop. Left alone that meant a cold start baked
    langchain's invented 4096 into an LLM the graph then caches: the original
    dead-turn bug, restored by its own fix. The node warms off-loop first.
    """

    @pytest.mark.asyncio
    async def test_the_node_actually_warms_before_constructing_the_llm(
        self, monkeypatch
    ):
        """Pins the CALL, not the helper.

        Testing `_warm_max_output_ceiling` directly proves it works and proves
        nothing about whether the node calls it. Deleting the call is invisible
        to every other test here, and costs a cold-start turn its whole ceiling.
        Order matters too: warming AFTER construction is the same bug, because
        the factory has already resolved by then.
        """
        order: list[str] = []

        async def _fake_warm(llm_config, candidate_index):
            order.append("warm")

        def _fake_candidate(primary, **kwargs):
            order.append("construct")
            return primary

        monkeypatch.setattr(nodes_module, "_warm_max_output_ceiling", _fake_warm)
        monkeypatch.setattr(nodes_module, "_llm_candidate", _fake_candidate)
        monkeypatch.setattr(
            nodes_module, "streaming_call_kwargs", lambda candidate, resolved: {}
        )
        llm = _FakeStreamLLM(AIMessage(content="ok"))
        node = create_agent_node(llm, "system prompt", _anthropic_llm_config())
        await node.ainvoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )
        assert order == ["warm", "construct"], (
            f"the node must warm the ceiling off-loop BEFORE building the LLM: {order}"
        )

    @pytest.mark.asyncio
    async def test_the_warm_resolves_off_the_event_loop_thread(self, monkeypatch):
        import asyncio as _asyncio

        loop_threads: list[str] = []
        import threading

        main_thread = threading.current_thread().name

        def _recording_resolve(cfg, probe=False):
            loop_threads.append(threading.current_thread().name)
            return 128000

        monkeypatch.setattr(
            nodes_module, "resolve_max_output_tokens", _recording_resolve
        )
        await nodes_module._warm_max_output_ceiling(_anthropic_llm_config(), 0)
        assert loop_threads, "the warm never resolved anything"
        assert main_thread not in loop_threads, (
            "the ceiling was resolved on the event loop thread; blocking probe "
            f"HTTP would stall every request in the process: {loop_threads}"
        )
        assert _asyncio.get_event_loop_policy() is not None  # sanity

    @pytest.mark.asyncio
    async def test_the_warm_targets_the_fallback_model_mid_fallback(self, monkeypatch):
        seen: list[str] = []

        def _recording_resolve(cfg, probe=False):
            seen.append(cfg.model)
            return 128000

        monkeypatch.setattr(
            nodes_module, "resolve_max_output_tokens", _recording_resolve
        )
        cfg = _anthropic_llm_config()
        cfg.fallbacks = [
            LLMFallbackConfig(provider="anthropic", model="claude-haiku-4-5")
        ]
        await nodes_module._warm_max_output_ceiling(cfg, 1)
        assert seen == ["claude-haiku-4-5"], (
            f"the warm resolved the wrong model's ceiling: {seen}"
        )

    @pytest.mark.asyncio
    async def test_a_warm_failure_never_breaks_the_turn(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(nodes_module, "resolve_max_output_tokens", boom)
        await nodes_module._warm_max_output_ceiling(_anthropic_llm_config(), 0)


def _silent_empty_response(metadata: dict | None = None) -> AIMessage:
    """The incident shape (2026-08-06, thread 1a1aaee2): a NORMAL stop with
    no text and no tool calls. gemini-3.6-flash via CLIProxy leaked its
    intended tool call into the thinking channel as `call:default_api:`
    prose and stopped "successfully" having emitted nothing."""
    return AIMessage(
        content="",
        additional_kwargs={
            "reasoning_content": (
                "I've just initiated a file read operation using the "
                "`call:default_api:file_read` function"
            )
        },
        response_metadata=metadata
        or {"finish_reason": "stop", "model_name": "gemini-3.6-flash-high"},
        usage_metadata={
            "input_tokens": 100000,
            "output_tokens": 204,
            "total_tokens": 100204,
        },
    )


class _SequencedLLM:
    """Sync stand-in that returns a different response per call."""

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)
        self.calls = 0

    def invoke(self, _messages, **kwargs):
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response


class _SequencedStreamLLM:
    """Async stand-in that streams a different response per call."""

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)
        self.calls = 0

    async def astream(self, _messages, **kwargs):
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        yield AIMessageChunk(
            content=response.content,
            additional_kwargs=response.additional_kwargs,
            response_metadata=response.response_metadata,
            usage_metadata=response.usage_metadata,
            tool_calls=response.tool_calls,
        )


class TestSilentStopPredicate:
    """Rows for _is_silent_stop_response: normal-stop empties only."""

    def test_normal_stop_empty_shapes_are_silent(self):
        pred = nodes_module._is_silent_stop_response
        assert pred(_silent_empty_response())
        # Missing metadata counts as a normal stop (some providers send none).
        assert pred(AIMessage(content=""))
        # Reasoning-only content is not visible output.
        assert pred(
            AIMessage(
                content=[{"type": "thinking", "thinking": "t", "signature": "s"}]
            )
        )

    def test_owned_causes_and_healthy_shapes_are_not_silent(self):
        pred = nodes_module._is_silent_stop_response
        assert not pred(AIMessage(content="x"))
        assert not pred(
            AIMessage(content="", response_metadata={"finish_reason": "length"})
        )
        assert not pred(
            AIMessage(
                content="", response_metadata={"finish_reason": "content_filter"}
            )
        )
        assert not pred(
            AIMessage(content="", response_metadata={"stop_reason": "max_tokens"})
        )
        assert not pred(
            AIMessage(content="", response_metadata={"stop_reason": "refusal"})
        )
        assert not pred(
            AIMessage(
                content="",
                tool_calls=[{"name": "t", "args": {}, "id": "c", "type": "tool_call"}],
            )
        )

    def test_provider_spellings_are_case_blind(self):
        """langchain-google-genai reports finish_reason as the UPPERCASE enum
        name (STOP/MAX_TOKENS/SAFETY/...), and the Responses API reports
        truncation as status="incomplete" with no finish_reason at all.
        Case-sensitive lowercase compares were dead code on the native
        Gemini route: a MAX_TOKENS dead turn was retried twice at full cost
        and mislabeled with the wrong notice (cold review, 2026-08-07)."""
        pred = nodes_module._is_silent_stop_response
        # Owned causes, Gemini spellings: never retried as silent stops.
        for reason in ("MAX_TOKENS", "SAFETY", "PROHIBITED_CONTENT", "RECITATION"):
            assert not pred(
                AIMessage(content="", response_metadata={"finish_reason": reason})
            ), reason
        # Responses-API truncation spelling.
        assert not pred(
            AIMessage(
                content="",
                response_metadata={
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            )
        )
        # A normal Gemini stop IS the silent-stop shape.
        assert pred(AIMessage(content="", response_metadata={"finish_reason": "STOP"}))
        # MALFORMED_FUNCTION_CALL is transient: retrying is the point.
        assert pred(
            AIMessage(
                content="",
                response_metadata={"finish_reason": "MALFORMED_FUNCTION_CALL"},
            )
        )

    def test_gemini_uppercase_dead_turn_gets_truncation_treatment(self, events):
        """A Gemini MAX_TOKENS dead turn must reach the truncation branch
        (right notice, output_truncated event), not the retry loop."""
        llm = _SequencedLLM(
            [
                AIMessage(
                    content="",
                    response_metadata={"finish_reason": "MAX_TOKENS"},
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": 4096,
                        "total_tokens": 4106,
                    },
                ),
                AIMessage(content="never reached"),
            ]
        )
        node = create_agent_node(llm, "system prompt")
        result = node.invoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )
        assert llm.calls == 1
        assert [n for n, _ in events if n == "output_truncated"] == ["output_truncated"]
        assert [n for n, _ in events if n == "empty_turn_retry"] == []
        assert nodes_module._TRUNCATION_NOTICE in _visible_text_of(
            result["messages"][0].content
        )

    def test_gemini_safety_block_gets_refusal_treatment(self, events):
        """A Gemini SAFETY block is a deterministic filter: retrying re-refuses.
        It must take the refusal branch (event + rewind marker), not retry."""
        llm = _SequencedLLM(
            [
                AIMessage(content="", response_metadata={"finish_reason": "SAFETY"}),
                AIMessage(content="never reached"),
            ]
        )
        node = create_agent_node(llm, "system prompt")
        result = node.invoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )
        assert llm.calls == 1
        assert [n for n, _ in events if n == "response_refused"] == ["response_refused"]
        assert [n for n, _ in events if n == "empty_turn_retry"] == []
        assert result["messages"][0].additional_kwargs.get("empty_turn_refusal") is True


class TestEmptyRoundRetry:
    """Third empty-turn cause: normal stop, nothing in it (2026-08-06).

    Unlike truncation and refusal, this shape is retried IN PLACE before any
    notice: the round executed nothing, so re-asking is inherently
    replay-safe, and Gemini's reasoning-only stops are probabilistic enough
    that a plain re-invoke usually recovers. The notice is the last resort,
    and doubles as the guarantee that the checkpointed message is never
    empty (an empty model turn 400s Gemini via CLIProxy on history replay,
    permanently wedging the thread)."""

    def test_empty_round_retries_in_place_and_recovers(self, events, caplog):
        llm = _SequencedLLM(
            [_silent_empty_response(), AIMessage(content="the answer")]
        )
        node = create_agent_node(llm, "system prompt")
        with caplog.at_level("WARNING", logger="nymeria"):
            result = node.invoke(
                {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
            )
        out = result["messages"][0]
        assert llm.calls == 2
        # The recovered answer, with NO notice bolted onto it and no trace of
        # the discarded empty attempt in graph state.
        assert _visible_text_of(out.content) == "the answer"
        assert "EMPTY ROUND" in caplog.text
        assert [p["retry"] for n, p in events if n == "empty_turn_retry"] == [1]
        assert [n for n, _ in events if n == "empty_turn"] == []

    @pytest.mark.asyncio
    async def test_empty_round_retry_on_the_streaming_path(self, events):
        # Interactive turns stream; the guard must live on that path too.
        llm = _SequencedStreamLLM(
            [_silent_empty_response(), AIMessage(content="the answer")]
        )
        node = create_agent_node(llm, "system prompt")
        result = await node.ainvoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )
        assert llm.calls == 2
        assert _visible_text_of(result["messages"][0].content) == "the answer"
        assert [p["retry"] for n, p in events if n == "empty_turn_retry"] == [1]

    def test_exhausted_retries_attach_notice_and_event(self, events, caplog):
        llm = _SequencedLLM([_silent_empty_response()])  # empty every time
        node = create_agent_node(llm, "system prompt")
        with caplog.at_level("WARNING", logger="nymeria"):
            result = node.invoke(
                {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
            )
        out = result["messages"][0]
        assert llm.calls == 1 + nodes_module._EMPTY_ROUND_RETRY_LIMIT
        # The checkpointed message must carry THIS cause's notice: silence
        # for the user otherwise, and asserting the exact text pins that the
        # truncation notice was not attached in its place.
        assert nodes_module._EMPTY_ROUND_NOTICE in _visible_text_of(out.content)
        assert "EMPTY TURN" in caplog.text
        assert [p["retry"] for n, p in events if n == "empty_turn_retry"] == [1, 2]
        emitted = [p for n, p in events if n == "empty_turn"]
        assert len(emitted) == 1
        assert emitted[0]["produced_output"] is False
        assert emitted[0]["retries"] == nodes_module._EMPTY_ROUND_RETRY_LIMIT

    @pytest.mark.asyncio
    async def test_exhausted_retries_on_the_streaming_path(self, events):
        llm = _SequencedStreamLLM([_silent_empty_response()])
        node = create_agent_node(llm, "system prompt")
        result = await node.ainvoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )
        assert llm.calls == 1 + nodes_module._EMPTY_ROUND_RETRY_LIMIT
        assert nodes_module._EMPTY_ROUND_NOTICE in _visible_text_of(
            result["messages"][0].content
        )
        assert [p["retry"] for n, p in events if n == "empty_turn_retry"] == [1, 2]
        assert [n for n, _ in events if n == "empty_turn"] == ["empty_turn"]

    def test_truncated_empty_round_is_not_retried(self, events):
        # max_tokens dead turns keep their own treatment: retrying would just
        # reproduce the cap, and the truncation notice tells the user how to
        # actually fix it.
        llm = _SequencedLLM(
            [
                _thinking_only_response({"stop_reason": "max_tokens"}),
                AIMessage(content="never reached"),
            ]
        )
        node = create_agent_node(llm, "system prompt")
        node.invoke({"messages": [HumanMessage(content="hi")]}, {"configurable": {}})
        assert llm.calls == 1
        assert [n for n, _ in events if n == "empty_turn_retry"] == []

    def test_refused_empty_round_is_not_retried(self, events):
        # Refusals have their own machinery (swap + rewind); retrying the same
        # model on the same context tends to re-refuse and would double-bill.
        llm = _SequencedLLM(
            [
                _refused_thinking_only_response({"stop_reason": "refusal"}),
                AIMessage(content="never reached"),
            ]
        )
        node = create_agent_node(llm, "system prompt")
        node.invoke({"messages": [HumanMessage(content="hi")]}, {"configurable": {}})
        assert llm.calls == 1
        assert [n for n, _ in events if n == "empty_turn_retry"] == []

    def test_tool_call_round_is_not_retried(self, events):
        llm = _SequencedLLM(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "t", "args": {}, "id": "c1", "type": "tool_call"}
                    ],
                )
            ]
        )
        node = create_agent_node(llm, "system prompt")
        node.invoke({"messages": [HumanMessage(content="hi")]}, {"configurable": {}})
        assert llm.calls == 1
        assert [n for n, _ in events if n in ("empty_turn_retry", "empty_turn")] == []

    def test_healthy_round_is_untouched(self, events):
        llm = _SequencedLLM([AIMessage(content="fine")])
        node = create_agent_node(llm, "system prompt")
        result = node.invoke(
            {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
        )
        assert llm.calls == 1
        assert result["messages"][0].content == "fine"
        assert [n for n, _ in events if n in ("empty_turn_retry", "empty_turn")] == []

    def test_events_are_mirrored_to_autonomous_consumers(self):
        # Autonomous turns are exactly the ones nobody watches; see the
        # output_truncated mirror rationale.
        from nymeria.core.event_bus import AGENT_STREAM_AUTONOMOUS_EVENT_TYPES

        assert "empty_turn_retry" in AGENT_STREAM_AUTONOMOUS_EVENT_TYPES
        assert "empty_turn" in AGENT_STREAM_AUTONOMOUS_EVENT_TYPES


def test_visible_text_accepts_both_spellings_of_a_text_block():
    """`output_text` is the OpenAI-Responses spelling and must count as visible.

    Every other extractor in the codebase accepts both, including one in this
    same module. Reading only "text" would classify a productive truncated turn
    as a DEAD TURN: it would log an error, report produced_output=false, and
    append the "ran out of output budget" notice to an answer the user could see
    perfectly well.
    """
    assert nodes_module._visible_text_of([{"type": "text", "text": "hello"}]) == "hello"
    assert (
        nodes_module._visible_text_of([{"type": "output_text", "text": "hello"}])
        == "hello"
    )
    # Thinking blocks still must not count as visible output.
    assert (
        nodes_module._visible_text_of(
            [{"type": "thinking", "thinking": "long private reasoning", "signature": "x"}]
        )
        == ""
    )
