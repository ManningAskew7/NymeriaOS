"""Integration tests for the PRE_TOOL_USE / POST_TOOL_USE seam in SafeToolNode.

Drives the real node (concurrent + async paths) with hooks registered, asserting
veto, arg-modify, result-rewrite, note-append, matcher scoping, fault policy, and
that the no-hooks fast path is untouched. Also a coupling guard against the pinned
langgraph-prebuilt internals the override depends on.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.runtime import DEFAULT_RUNTIME
from langgraph._internal._constants import CONFIG_KEY_RUNTIME

from nymeria.core.hooks import HookEvent, PostToolOutcome, PreToolOutcome
from nymeria.vendor.react_agent.nodes import SafeToolNode

dmod = importlib.import_module("nymeria.core.hooks.dispatch")


@tool
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


def _config(thread_id: str = "t-hook"):
    return {"configurable": {
        CONFIG_KEY_RUNTIME: DEFAULT_RUNTIME,
        "thread_id": thread_id,
        "user_id": "u-hook",
    }}


def _msg(tool_calls):
    return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}


def _call(text="hi", cid="c1"):
    return {"name": "echo", "args": {"text": text}, "id": cid}


def _first_msg(result):
    return result["messages"][0]


def test_no_hooks_runs_tool_normally():
    dmod.reset()
    try:
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("world")]), _config())
        assert _first_msg(out).content == "echo:world"
    finally:
        dmod.reset()


def test_pre_deny_blocks_tool():
    dmod.reset()
    try:
        dmod.register(HookEvent.PRE_TOOL_USE, lambda c: PreToolOutcome(decision="deny", reason="nope"))
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("world")]), _config())
        msg = _first_msg(out)
        assert msg.status == "error"
        assert "blocked: nope" in msg.content
        assert "echo:" not in msg.content  # tool never executed
    finally:
        dmod.reset()


def test_pre_modify_rewrites_args():
    dmod.reset()
    try:
        dmod.register(HookEvent.PRE_TOOL_USE, lambda c: PreToolOutcome(decision="modify", updated_args={"text": "modified"}))
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("original")]), _config())
        assert _first_msg(out).content == "echo:modified"
    finally:
        dmod.reset()


def test_post_rewrite_result():
    dmod.reset()
    try:
        dmod.register(HookEvent.POST_TOOL_USE, lambda c: PostToolOutcome(updated_result_text="REDACTED"))
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("secret")]), _config())
        assert _first_msg(out).content == "REDACTED"
    finally:
        dmod.reset()


def test_post_append_note():
    dmod.reset()
    try:
        dmod.register(HookEvent.POST_TOOL_USE, lambda c: PostToolOutcome(additional_context="[note]"))
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("hi")]), _config())
        assert _first_msg(out).content == "echo:hi\n\n[note]"
    finally:
        dmod.reset()


def test_tool_hook_sees_threaded_turn_source():
    """slice D5: graph_run_config stamps hook_is_autonomous/holder/trigger into
    configurable; _build_tool_hook_ctx surfaces them to a PRE/POST tool hook."""
    dmod.reset()
    seen = {}
    try:
        def pre(c):
            seen["autonomous"] = c.is_autonomous
            seen["holder"] = c.holder_kind
            seen["trigger"] = c.trigger_label
            return None
        dmod.register(HookEvent.PRE_TOOL_USE, pre)
        node = SafeToolNode([echo])
        cfg = {"configurable": {
            CONFIG_KEY_RUNTIME: DEFAULT_RUNTIME,
            "thread_id": "t-src",
            "user_id": "u-src",
            "hook_is_autonomous": True,
            "hook_holder_kind": "ticker",
            "hook_trigger_label": "Scheduled TODO",
        }}
        node.invoke(_msg([_call("hi")]), cfg)
        assert seen == {"autonomous": True, "holder": "ticker", "trigger": "Scheduled TODO"}
    finally:
        dmod.reset()


def test_tool_hook_turn_source_defaults_when_absent():
    """No stamped source (e.g. legacy config) -> interactive defaults, no crash."""
    dmod.reset()
    seen = {}
    try:
        def pre(c):
            seen["autonomous"] = c.is_autonomous
            seen["holder"] = c.holder_kind
            return None
        dmod.register(HookEvent.PRE_TOOL_USE, pre)
        node = SafeToolNode([echo])
        node.invoke(_msg([_call("hi")]), _config())
        assert seen == {"autonomous": False, "holder": None}
    finally:
        dmod.reset()


def test_matcher_scopes_hook_out():
    dmod.reset()
    try:
        dmod.register(HookEvent.POST_TOOL_USE, lambda c: PostToolOutcome(additional_context="X"), matcher="Edit|Write")
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("hi")]), _config())
        assert _first_msg(out).content == "echo:hi"  # echo not matched -> unaffected
    finally:
        dmod.reset()


def test_post_sees_tool_result_name_status():
    dmod.reset()
    seen = {}
    try:
        def post(c):
            seen["name"] = c.tool_name
            seen["result"] = c.tool_result_text
            seen["status"] = c.tool_status
            return None
        dmod.register(HookEvent.POST_TOOL_USE, post)
        node = SafeToolNode([echo])
        node.invoke(_msg([_call("data")]), _config())
        assert seen["name"] == "echo"
        assert seen["result"] == "echo:data"
        assert seen["status"] in ("success", None)
    finally:
        dmod.reset()


def test_post_observe_hook_fires_and_is_ignored():
    # An observe-plane POST hook must run (proving the seam activates on observe
    # hooks) but its return is ignored (the tool result is unchanged).
    dmod.reset()
    fired = {}
    try:
        def observe(c):
            fired["name"] = c.tool_name
            fired["result"] = c.tool_result_text
            return PostToolOutcome(updated_result_text="SHOULD BE IGNORED")
        dmod.register(HookEvent.POST_TOOL_USE, observe, observe=True)
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("hi")]), _config())
        dmod.drain_observe()  # observe now runs off-turn (pool path on sync invoke)
        assert fired == {"name": "echo", "result": "echo:hi"}
        assert _first_msg(out).content == "echo:hi"  # observe return not applied
    finally:
        dmod.reset()


def test_async_post_observe_hook_fires():
    dmod.reset()
    fired = {}
    try:
        def observe(c):
            fired["result"] = c.tool_result_text
            return None
        dmod.register(HookEvent.POST_TOOL_USE, observe, observe=True)
        node = SafeToolNode([echo])

        async def _run():
            # Drain inside the same loop: observe now runs as a background task.
            result = await node.ainvoke(_msg([_call("data")]), _config())
            await dmod.adrain_observe()
            return result

        out = asyncio.run(_run())
        assert fired["result"] == "echo:data"
        assert _first_msg(out).content == "echo:data"
    finally:
        dmod.reset()


def test_async_pre_deny():
    dmod.reset()
    try:
        dmod.register(HookEvent.PRE_TOOL_USE, lambda c: PreToolOutcome(decision="deny", reason="async-no"))
        node = SafeToolNode([echo])
        out = asyncio.run(node.ainvoke(_msg([_call("x")]), _config()))
        assert "blocked: async-no" in _first_msg(out).content
    finally:
        dmod.reset()


def test_pre_deny_emits_hook_activity(monkeypatch):
    """The sync tool path forwards meaningful mutate runs as hook_activity events."""
    import nymeria.vendor.react_agent.nodes as nmod

    dmod.reset()
    captured: list = []
    monkeypatch.setattr(
        nmod, "_dispatch_provider_event",
        lambda name, payload, cfg: captured.append((name, payload)),
    )
    try:
        dmod.register(
            HookEvent.PRE_TOOL_USE,
            lambda c: PreToolOutcome(decision="deny", reason="nope"),
            name="guard",
        )
        node = SafeToolNode([echo])
        node.invoke(_msg([_call("world")]), _config())
        acts = [p for (n, p) in captured if n == "hook_activity"]
        assert len(acts) == 1
        assert acts[0]["name"] == "guard"
        assert acts[0]["event"] == "pre_tool_use"
        assert acts[0]["status"] == "ok"
        assert acts[0]["detail"] == "deny: nope"
        assert acts[0]["tool_name"] == "echo"
    finally:
        dmod.reset()


def test_async_post_rewrite_emits_hook_activity(monkeypatch):
    """The async tool path forwards a post_tool_use rewrite as hook_activity."""
    import nymeria.vendor.react_agent.nodes as nmod

    dmod.reset()
    captured: list = []

    async def _fake_emit(name, payload, cfg):
        captured.append((name, payload))

    monkeypatch.setattr(nmod, "_adispatch_provider_event", _fake_emit)
    try:
        dmod.register(
            HookEvent.POST_TOOL_USE,
            lambda c: PostToolOutcome(updated_result_text="REDACTED"),
            name="redactor",
        )
        node = SafeToolNode([echo])
        asyncio.run(node.ainvoke(_msg([_call("secret")]), _config()))
        acts = [p for (n, p) in captured if n == "hook_activity"]
        assert len(acts) == 1
        assert acts[0]["name"] == "redactor"
        assert acts[0]["event"] == "post_tool_use"
        assert "rewrite result" in acts[0]["detail"]
    finally:
        dmod.reset()


def test_pre_allow_emits_no_hook_activity(monkeypatch):
    """A bare allow is inert and must not put a line on every guarded call."""
    import nymeria.vendor.react_agent.nodes as nmod

    dmod.reset()
    captured: list = []
    monkeypatch.setattr(
        nmod, "_dispatch_provider_event",
        lambda name, payload, cfg: captured.append((name, payload)),
    )
    try:
        dmod.register(
            HookEvent.PRE_TOOL_USE, lambda c: PreToolOutcome(decision="allow"), name="allower"
        )
        node = SafeToolNode([echo])
        node.invoke(_msg([_call("world")]), _config())
        assert [p for (n, p) in captured if n == "hook_activity"] == []
    finally:
        dmod.reset()


def test_pre_raise_fails_closed_at_node():
    dmod.reset()
    try:
        dmod.register(
            HookEvent.PRE_TOOL_USE,
            lambda c: (_ for _ in ()).throw(RuntimeError("boom")),
            name="boom",
        )
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("x")]), _config())
        msg = _first_msg(out)
        assert msg.status == "error"
        assert "blocked" in msg.content  # raising PRE hook -> deny (fail closed)
        assert "echo:" not in msg.content
    finally:
        dmod.reset()


def test_post_rewrite_preserves_multimodal_blocks():
    """A text rewrite on a multimodal result keeps image/file blocks."""
    msg = ToolMessage(
        content=[
            {"type": "text", "text": "orig"},
            {"type": "image_url", "image_url": {"url": "data:x"}},
        ],
        name="echo",
        tool_call_id="c1",
    )
    out = SafeToolNode._apply_post_tool_outcome(msg, PostToolOutcome(updated_result_text="REDACTED"))
    types = [b.get("type") for b in out.content if isinstance(b, dict)]
    assert "image_url" in types  # image block preserved
    texts = [b["text"] for b in out.content if isinstance(b, dict) and b.get("type") == "text"]
    assert texts == ["REDACTED"]


def test_post_rewrite_returns_copy_not_mutation():
    """The rewrite returns a fresh ToolMessage; the original is untouched."""
    msg = ToolMessage(content="orig", name="echo", tool_call_id="c1")
    out = SafeToolNode._apply_post_tool_outcome(msg, PostToolOutcome(updated_result_text="new"))
    assert out.content == "new"
    assert msg.content == "orig"  # original not mutated in place


def test_parent_tool_node_internals_present():
    """Coupling guard: a langgraph-prebuilt upgrade must not silently remove the
    internals the PRE/POST override depends on."""
    from langgraph.prebuilt.tool_node import ToolCallRequest, ToolNode
    for name in ("_run_one", "_arun_one", "_execute_tool_sync", "_execute_tool_async"):
        assert hasattr(ToolNode, name), f"ToolNode.{name} missing after upgrade"
    params = inspect.signature(ToolCallRequest).parameters
    for field in ("tool_call", "tool", "state", "runtime"):
        assert field in params, f"ToolCallRequest.{field} missing after upgrade"
