"""Tests for the hook execution recorder seam (dispatch -> registry.recorder).

The recorder is an opaque callable the product layer attaches to a per-turn
registry; dispatch reports each hook run through it (both planes, both
sync/async forms) and recording must never change dispatch semantics or raise
into a turn.
"""

from __future__ import annotations

import asyncio
import importlib
import time

from nymeria.core.hooks import (
    DoneOutcome,
    HookContext,
    HookEvent,
    HookRegistry,
    PreToolOutcome,
    PromptOutcome,
)

dmod = importlib.import_module("nymeria.core.hooks.dispatch")


def ctx(event: HookEvent, **kw) -> HookContext:
    base = dict(event=event, thread_id="t1", user_id="u1", is_autonomous=False)
    base.update(kw)
    return HookContext(**base)


def run(coro):
    return asyncio.run(coro)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, reg, ctx, *, status, detail, duration):
        self.calls.append(
            {
                "name": reg.name,
                "definition_id": reg.definition_id,
                "observe": reg.observe,
                "status": status,
                "detail": detail,
                "duration": duration,
            }
        )


def _registry(recorder=None):
    reg = HookRegistry()
    reg.recorder = recorder
    return reg


# --- mutate plane -------------------------------------------------------------

def test_records_ok_with_outcome_detail():
    rec = Recorder()
    reg = _registry(rec)
    reg.register(
        HookEvent.PROMPT_SUBMIT,
        lambda c: PromptOutcome(inject_context="hi"),
        name="inject",
        definition_id="abc123",
    )
    out = dmod.dispatch(HookEvent.PROMPT_SUBMIT, ctx(HookEvent.PROMPT_SUBMIT), registry=reg)
    assert out is not None and out.inject_context == "hi"
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["status"] == "ok"
    assert call["detail"] == "inject 2 chars"
    assert call["definition_id"] == "abc123"
    assert call["duration"] >= 0.0


def test_records_no_op_when_hook_returns_none():
    rec = Recorder()
    reg = _registry(rec)
    reg.register(HookEvent.DONE, lambda c: None, name="noop")
    assert dmod.dispatch(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg) is None
    assert rec.calls[0]["status"] == "no_op"
    assert rec.calls[0]["detail"] == ""


def test_records_error_and_pre_still_fails_closed():
    rec = Recorder()
    reg = _registry(rec)

    def boom(c):
        raise ValueError("kaput")

    reg.register(HookEvent.PRE_TOOL_USE, boom, name="guard")
    out = dmod.dispatch(
        HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="bash"), registry=reg
    )
    # Fault policy unchanged: a raising PRE hook denies.
    assert isinstance(out, PreToolOutcome) and out.decision == "deny"
    assert rec.calls[0]["status"] == "error"
    assert "kaput" in rec.calls[0]["detail"]


def test_records_timeout_on_execution_overrun():
    rec = Recorder()
    reg = _registry(rec)

    def slow(c):
        time.sleep(0.5)

    reg.register(HookEvent.DONE, slow, name="slow")
    dmod.dispatch(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg, timeout=0.05)
    assert rec.calls[0]["status"] == "timeout"


def test_records_illegal_outcome_dropped():
    rec = Recorder()
    reg = _registry(rec)
    reg.register(HookEvent.DONE, lambda c: PreToolOutcome(decision="deny"), name="wrong")
    assert dmod.dispatch(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg) is None
    assert rec.calls[0]["status"] == "illegal"
    assert "PreToolOutcome" in rec.calls[0]["detail"]


def test_deny_detail_carries_reason():
    rec = Recorder()
    reg = _registry(rec)
    reg.register(
        HookEvent.PRE_TOOL_USE,
        lambda c: PreToolOutcome(decision="deny", reason="nope"),
        name="guard",
    )
    dmod.dispatch(
        HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="bash"), registry=reg
    )
    assert rec.calls[0]["status"] == "ok"
    assert rec.calls[0]["detail"] == "deny: nope"


def test_recorder_exception_never_breaks_dispatch():
    def bad_recorder(reg, ctx, **kw):
        raise RuntimeError("recorder broken")

    reg = _registry(bad_recorder)
    reg.register(HookEvent.PROMPT_SUBMIT, lambda c: PromptOutcome(inject_context="x"))
    out = dmod.dispatch(HookEvent.PROMPT_SUBMIT, ctx(HookEvent.PROMPT_SUBMIT), registry=reg)
    assert out is not None and out.inject_context == "x"


def test_recorder_exception_on_pre_does_not_deny():
    """A recording fault is not a hook fault: PRE must not fail closed on it."""

    def bad_recorder(reg, ctx, **kw):
        raise RuntimeError("recorder broken")

    reg = _registry(bad_recorder)
    reg.register(HookEvent.PRE_TOOL_USE, lambda c: None, name="guard")
    out = dmod.dispatch(
        HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="bash"), registry=reg
    )
    # The hook itself passed (returned None); a broken recorder must not veto.
    assert out is None


def test_detail_summarization_fault_on_pre_does_not_deny(monkeypatch):
    """Detail is computed inside _record's own guard: a summarizer bug cannot veto.

    Recording happens outside the dispatch fault handling; if it ran inside,
    a raise here would become a fail-closed deny on the PRE seam.
    """
    rec = Recorder()
    reg = _registry(rec)
    reg.register(
        HookEvent.PRE_TOOL_USE, lambda c: PreToolOutcome(decision="allow"), name="guard"
    )

    def boom(outcome):
        raise RuntimeError("summarizer broken")

    monkeypatch.setattr(dmod, "_outcome_detail", boom)
    out = dmod.dispatch(
        HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="bash"), registry=reg
    )
    assert isinstance(out, PreToolOutcome) and out.decision == "allow"


def test_no_recorder_is_the_default_and_records_nothing():
    reg = HookRegistry()
    assert reg.recorder is None
    reg.register(HookEvent.DONE, lambda c: DoneOutcome(continue_=True, reason="go"))
    out = dmod.dispatch(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg)
    assert out is not None and out.continue_ is True


def test_async_mutate_records():
    rec = Recorder()
    reg = _registry(rec)

    async def hook(c):
        return PromptOutcome(inject_context="async")

    reg.register(HookEvent.PROMPT_SUBMIT, hook, name="a")
    out = run(dmod.adispatch(HookEvent.PROMPT_SUBMIT, ctx(HookEvent.PROMPT_SUBMIT), registry=reg))
    assert out is not None
    assert rec.calls[0]["status"] == "ok"


# --- observe plane -------------------------------------------------------------

def test_observe_records_ok_and_error():
    rec = Recorder()
    reg = _registry(rec)
    reg.register(HookEvent.DONE, lambda c: None, name="fine", observe=True)

    def boom(c):
        raise ValueError("side effect failed")

    reg.register(HookEvent.DONE, boom, name="broken", observe=True)
    dmod.dispatch_observe(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg)
    by_name = {c["name"]: c for c in rec.calls}
    assert by_name["fine"]["status"] == "ok"
    assert by_name["fine"]["observe"] is True
    assert by_name["broken"]["status"] == "error"


def test_async_observe_records():
    rec = Recorder()
    reg = _registry(rec)
    reg.register(HookEvent.DONE, lambda c: None, name="fine", observe=True)
    run(dmod.adispatch_observe(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg))
    assert rec.calls[0]["status"] == "ok"


# --- status mapping -------------------------------------------------------------

def test_fault_status_mapping():
    assert dmod._fault_status(dmod._HookTimeout("h", started=True)) == "timeout"
    assert dmod._fault_status(dmod._HookTimeout("h", started=False)) == "saturated"
    assert dmod._fault_status(ValueError("x")) == "error"
