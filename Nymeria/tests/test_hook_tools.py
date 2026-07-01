"""Unit tests for the hook authoring @tool pair (tools/hooks.py).

The tools read identity/thread from an injected RunnableConfig and write to a
per-user HookManager. Tests point the module singleton at a tmp store and drive
the tools through ``.invoke(input, config=...)`` so the InjectedToolArg config
resolves the way it does at runtime.
"""

from __future__ import annotations

import pytest

from nymeria.core.hook_manager import HookManager
from nymeria.tools import hooks as hook_tools


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = HookManager(tmp_path)
    monkeypatch.setattr(hook_tools, "_hook_manager", mgr)
    return mgr


def _cfg(user_id="u1", thread_id="t1"):
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


def _invoke(tool, args, cfg):
    return tool.invoke(args, config=cfg)


# --- hook_config ------------------------------------------------------------

def test_create_binds_current_thread(store):
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "n", "event": "done", "text": "check"},
        _cfg(thread_id="mythread"),
    )
    assert "[Success]" in out
    hooks = store.get_hooks("u1")
    assert len(hooks) == 1
    assert hooks[0].scope == "thread"
    assert hooks[0].thread_id == "mythread"


def test_create_global_scope_has_no_thread(store):
    _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "n", "event": "done", "text": "x", "scope": "global"},
        _cfg(thread_id="ignored"),
    )
    h = store.get_hooks("u1")[0]
    assert h.scope == "global"
    assert h.thread_id == ""


def test_create_requires_fields(store):
    out = _invoke(hook_tools.hook_config, {"action": "create", "name": "n"}, _cfg())
    assert "[Error]" in out
    assert store.get_hooks("u1") == []


def test_create_rejects_bad_event(store):
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "n", "event": "pre_tool_use", "text": "x"},
        _cfg(),
    )
    assert "[Error]" in out


def test_update_changes_text(store):
    h = store.add_hook("u1", name="n", event="done", text="old")
    out = _invoke(
        hook_tools.hook_config,
        {"action": "update", "hook_id": h.id, "text": "new"},
        _cfg(),
    )
    assert "[Success]" in out
    assert store.get_hook("u1", h.id).logic.text == "new"


def test_update_missing_hook(store):
    out = _invoke(
        hook_tools.hook_config,
        {"action": "update", "hook_id": "nope", "text": "x"},
        _cfg(),
    )
    assert "[Error]" in out


def test_delete_hook(store):
    h = store.add_hook("u1", name="n", event="done", text="x")
    out = _invoke(hook_tools.hook_config, {"action": "delete", "hook_id": h.id}, _cfg())
    assert "[Success]" in out
    assert store.get_hook("u1", h.id) is None


def test_unknown_action(store):
    out = _invoke(hook_tools.hook_config, {"action": "frobnicate"}, _cfg())
    assert "[Error]" in out


# --- hook_config: PRE guardrail actions -------------------------------------

def test_create_block_if_matches(store):
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "guard", "event": "pre_tool_use",
         "hook_action": "block_if_matches", "matcher": "bash",
         "params": {"conditions": [{"field": "command", "operator": "contains", "value": "rm -rf"}],
                    "reason": "no"}},
        _cfg(),
    )
    assert "[Success]" in out
    h = store.get_hooks("u1")[0]
    assert h.logic.action == "block_if_matches"
    assert h.matcher == "bash"
    assert h.logic.reason == "no"


def test_create_rewrite_arg(store):
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "clamp", "event": "pre_tool_use",
         "hook_action": "rewrite_arg", "params": {"updates": {"command": "echo hi"}}},
        _cfg(),
    )
    assert "[Success]" in out
    assert store.get_hooks("u1")[0].logic.updates == {"command": "echo hi"}


def test_create_block_requires_params(store):
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "g", "event": "pre_tool_use",
         "hook_action": "block_if_matches"},
        _cfg(),
    )
    assert "[Error]" in out
    assert store.get_hooks("u1") == []


def test_create_rejects_illegal_action_for_event(store):
    # block_if_matches is not legal on done.
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "g", "event": "done",
         "hook_action": "block_if_matches", "params": {}},
        _cfg(),
    )
    assert "[Error]" in out


def test_detail_renders_block_variant(store):
    h = store.add_hook(
        "u1", name="g", event="pre_tool_use", action="block_if_matches",
        params={"conditions": [{"field": "command", "operator": "contains", "value": "rm"}],
                "reason": "denied"},
        matcher="bash",
    )
    out = _invoke(hook_tools.hook_info, {"action": "detail", "hook_id": h.id}, _cfg())
    assert "block_if_matches" in out
    assert "denied" in out


def test_test_describes_block_variant(store):
    h = store.add_hook(
        "u1", name="g", event="pre_tool_use", action="block_if_matches",
        params={"conditions": [{"field": "command", "operator": "contains", "value": "rm"}]},
        matcher="bash",
    )
    out = _invoke(hook_tools.hook_info, {"action": "test", "hook_id": h.id}, _cfg())
    assert "block_if_matches" in out or "denies" in out


# --- hook_config: observe-plane actions -------------------------------------

def test_create_notify_via_text(store):
    # notify is a text action -> a bare `text` arg (no params) is accepted.
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "ping", "event": "done",
         "hook_action": "notify", "text": "finished: {final_text}"},
        _cfg(),
    )
    assert "[Success]" in out
    h = store.get_hooks("u1")[0]
    assert h.logic.action == "notify"
    assert h.logic.text == "finished: {final_text}"


def test_create_webhook_via_params(store):
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "wh", "event": "done", "hook_action": "webhook",
         "params": {"url": "https://x.test/h", "text": "body"}},
        _cfg(),
    )
    assert "[Success]" in out
    h = store.get_hooks("u1")[0]
    assert h.logic.action == "webhook"
    assert h.logic.url == "https://x.test/h"


def test_create_webhook_requires_params(store):
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "wh", "event": "done", "hook_action": "webhook"},
        _cfg(),
    )
    assert "[Error]" in out
    assert store.get_hooks("u1") == []


def test_create_notify_illegal_on_prompt_submit(store):
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "n", "event": "prompt_submit",
         "hook_action": "notify", "text": "x"},
        _cfg(),
    )
    assert "[Error]" in out


def test_detail_renders_webhook_variant(store):
    h = store.add_hook(
        "u1", name="wh", event="done", action="webhook",
        params={"url": "https://x.test/h", "text": "body"},
    )
    out = _invoke(hook_tools.hook_info, {"action": "detail", "hook_id": h.id}, _cfg())
    assert "webhook" in out
    assert "https://x.test/h" in out


# --- hook_info --------------------------------------------------------------

def test_list_empty(store):
    out = _invoke(hook_tools.hook_info, {"action": "list"}, _cfg())
    assert "No hooks" in out


def test_list_shows_hooks(store):
    store.add_hook("u1", name="alpha", event="done", text="x")
    out = _invoke(hook_tools.hook_info, {"action": "list"}, _cfg())
    assert "alpha" in out


def test_list_current_thread_only_filters(store):
    store.add_hook("u1", name="here", event="done", text="x", scope="thread", thread_id="t1")
    store.add_hook("u1", name="elsewhere", event="done", text="x", scope="thread", thread_id="t2")
    store.add_hook("u1", name="global", event="done", text="x", scope="global")
    out = _invoke(
        hook_tools.hook_info,
        {"action": "list", "current_thread_only": True},
        _cfg(thread_id="t1"),
    )
    assert "here" in out
    assert "global" in out
    assert "elsewhere" not in out


def test_detail(store):
    h = store.add_hook("u1", name="n", event="post_tool_use", text="note", matcher="Edit")
    out = _invoke(hook_tools.hook_info, {"action": "detail", "hook_id": h.id}, _cfg())
    assert "post_tool_use" in out
    assert "Edit" in out


def test_test_renders_template(store):
    h = store.add_hook("u1", name="n", event="post_tool_use", text="ran {tool_name}")
    out = _invoke(hook_tools.hook_info, {"action": "test", "hook_id": h.id}, _cfg())
    # Sample tool_name is "Edit" in the tool's sample var map.
    assert "ran Edit" in out


def test_detail_missing_hook(store):
    out = _invoke(hook_tools.hook_info, {"action": "detail", "hook_id": "nope"}, _cfg())
    assert "[Error]" in out


def test_hook_tools_grouped_for_catalog():
    names = {t.name for t in hook_tools.HOOK_TOOLS}
    assert names == {"hook_config", "hook_info"}
