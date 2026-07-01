"""Tests for the PROMPT_SUBMIT lifecycle-hook seam pieces.

Covers the strippable-sentinel round trip and the exact dispatch+wrap pipeline
the ``chat``/``astream`` seams run, without standing up a full graph.
"""

from __future__ import annotations

import asyncio

from nymeria.core.agent import NymeriaAgent
from nymeria.core.agent_history import strip_prompt_context, wrap_hook_context
from nymeria.core.hooks import HookEvent, PromptOutcome
import importlib

dmod = importlib.import_module("nymeria.core.hooks.dispatch")


def _bare_agent() -> NymeriaAgent:
    return NymeriaAgent.__new__(NymeriaAgent)


def test_wrap_and_strip_roundtrip():
    user = "please refactor foo()"
    prefix = "[Time: X]\n[Trigger: Y]\n\n"
    injected = wrap_hook_context("codehints: see bar.py")
    full = f"{prefix}{user}\n\n{injected}"
    stripped = strip_prompt_context(full)
    assert stripped == user
    assert "hook_context" not in stripped
    assert "codehints" not in stripped


def test_strip_leaves_plain_text_untouched():
    assert strip_prompt_context("just text") == "just text"


def test_prompt_submit_context_fields():
    agent = _bare_agent()
    ctx = agent._prompt_submit_context(
        thread_id="t1", user_id="u1", message="hi",
        is_autonomous=True, holder_kind="trigger", trigger_label="GitHub",
    )
    assert ctx.event is HookEvent.PROMPT_SUBMIT
    assert (ctx.thread_id, ctx.user_id) == ("t1", "u1")
    assert ctx.is_autonomous is True
    assert ctx.holder_kind == "trigger"
    assert ctx.trigger_label == "GitHub"
    assert ctx.prompt == "hi"


def test_wrap_prompt_injection_empty_and_text():
    agent = _bare_agent()
    assert agent._wrap_prompt_injection(None) == ""
    assert agent._wrap_prompt_injection(PromptOutcome(inject_context=None)) == ""
    wrapped = agent._wrap_prompt_injection(PromptOutcome(inject_context="hello"))
    assert wrapped.startswith("<hook_context>")
    assert "hello" in wrapped


def test_seam_sync_pipeline_injects_and_strips():
    dmod.reset()
    try:
        dmod.register(
            HookEvent.PROMPT_SUBMIT,
            lambda c: PromptOutcome(inject_context=f"src={c.holder_kind}"),
        )
        agent = _bare_agent()
        ctx = agent._prompt_submit_context(
            thread_id="t1", user_id="u1", message="hi",
            is_autonomous=False, holder_kind="user", trigger_label=None,
        )
        out = dmod.dispatch(HookEvent.PROMPT_SUBMIT, ctx)
        injected = agent._wrap_prompt_injection(out)
        base = "[Time: X]\n[Trigger: Y]\n\nhi"
        final = f"{base}\n\n{injected}" if injected else base
        assert "src=user" in final           # reaches the model in-turn
        assert strip_prompt_context(final) == "hi"  # stripped from history/RAG
    finally:
        dmod.reset()


def test_seam_async_pipeline():
    dmod.reset()
    try:
        dmod.register(HookEvent.PROMPT_SUBMIT, lambda c: PromptOutcome(inject_context="X"))
        agent = _bare_agent()
        ctx = agent._prompt_submit_context(
            thread_id="t1", user_id="u1", message="hi",
            is_autonomous=False, holder_kind="user", trigger_label=None,
        )
        out = asyncio.run(dmod.adispatch(HookEvent.PROMPT_SUBMIT, ctx))
        assert agent._wrap_prompt_injection(out) == wrap_hook_context("X")
    finally:
        dmod.reset()


def test_seam_no_hook_no_injection():
    dmod.reset()
    try:
        agent = _bare_agent()
        ctx = agent._prompt_submit_context(
            thread_id="t1", user_id="u1", message="hi",
            is_autonomous=False, holder_kind="user", trigger_label=None,
        )
        out = dmod.dispatch(HookEvent.PROMPT_SUBMIT, ctx)
        assert out is None
        assert agent._wrap_prompt_injection(out) == ""
    finally:
        dmod.reset()
