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


def test_update_rejects_scope_change(store):
    h = store.add_hook("u1", name="n", event="done", text="x", thread_id="t1")
    # Re-scoping on update is forbidden (matches REST PATCH and /hook edit): an
    # update cannot supply the access-gated thread binding, so allowing it could
    # orphan the hook as scope="thread" with thread_id="".
    out = _invoke(
        hook_tools.hook_config,
        {"action": "update", "hook_id": h.id, "scope": "global"},
        _cfg(),
    )
    assert "[Error]" in out
    assert "scope" in out
    fresh = store.get_hook("u1", h.id)
    assert fresh.scope == "thread"
    assert fresh.thread_id == "t1"


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


# --- hook_info: execution log ------------------------------------------------

def test_log_empty(store):
    out = _invoke(hook_tools.hook_info, {"action": "log"}, _cfg())
    assert "[Info]" in out
    assert "No hook executions" in out


def test_log_lists_entries_newest_first(store):
    from nymeria.core.hook_manager import HookExecution

    store.log_execution("u1", HookExecution(
        hook_id="h1", hook_name="guard", event="pre_tool_use", plane="mutate",
        status="ok", detail="deny: nope", tool_name="bash",
    ))
    store.log_execution("u1", HookExecution(
        hook_id="h2", hook_name="notifier", event="done", plane="observe",
        status="error", detail="side effect failed",
    ))
    out = _invoke(hook_tools.hook_info, {"action": "log"}, _cfg())
    assert "2 hook execution(s)" in out
    # Newest first: the done/error entry precedes the pre_tool_use one.
    assert out.index("h2") < out.index("h1")
    assert "[error]" in out
    assert "deny: nope" in out
    assert "tool=bash" in out


def test_log_filters_by_hook_id(store):
    from nymeria.core.hook_manager import HookExecution

    store.log_execution("u1", HookExecution(hook_id="h1", event="done", status="ok"))
    store.log_execution("u1", HookExecution(hook_id="h2", event="done", status="no_op"))
    out = _invoke(hook_tools.hook_info, {"action": "log", "hook_id": "h1"}, _cfg())
    assert "h1" in out
    assert "h2" not in out


# --- hook_config: run_command authoring gate ---------------------------------

def _set_run_command_flag(monkeypatch, enabled: bool):
    """Point the config-level get_settings (used by the gate) at a flag shim."""
    from types import SimpleNamespace

    from nymeria import config as config_module

    monkeypatch.setattr(
        config_module, "get_settings",
        lambda: SimpleNamespace(hooks_run_command_enabled=enabled),
    )


def test_run_command_denied_when_flag_off(store, monkeypatch):
    _set_run_command_flag(monkeypatch, False)
    monkeypatch.setattr(hook_tools, "is_admin", lambda *a, **k: True)
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "rc", "event": "done",
         "hook_action": "run_command", "command": "echo hi"},
        _cfg(),
    )
    assert "[Error]" in out
    assert "HOOKS_RUN_COMMAND_ENABLED" in out
    assert store.get_hooks("u1") == []


def test_run_command_denied_for_non_admin(store, monkeypatch):
    _set_run_command_flag(monkeypatch, True)
    monkeypatch.setattr(hook_tools, "is_admin", lambda *a, **k: False)
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "rc", "event": "done",
         "hook_action": "run_command", "command": "echo hi"},
        _cfg(),
    )
    assert "[Error]" in out
    assert "admin-only" in out
    assert store.get_hooks("u1") == []


def test_run_command_created_for_admin_with_flag_on(store, monkeypatch):
    _set_run_command_flag(monkeypatch, True)
    monkeypatch.setattr(hook_tools, "is_admin", lambda *a, **k: True)
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "rc", "event": "done", "scope": "global",
         "hook_action": "run_command", "command": "echo hi", "timeout_seconds": 12},
        _cfg(),
    )
    assert "[Success]" in out
    h = store.get_hooks("u1")[0]
    assert h.logic.action == "run_command"
    assert h.logic.command == "echo hi"
    assert h.logic.timeout_seconds == 12


def test_run_command_requires_command(store, monkeypatch):
    _set_run_command_flag(monkeypatch, True)
    monkeypatch.setattr(hook_tools, "is_admin", lambda *a, **k: True)
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "rc", "event": "done",
         "hook_action": "run_command"},
        _cfg(),
    )
    assert "[Error]" in out
    assert "run_command requires command" in out


# --- hook_config: run_workflow ------------------------------------------------

def test_run_workflow_created_with_params(store, monkeypatch):
    monkeypatch.setattr(
        "nymeria.core.workflows.tool_runtime.workflow_binding_error",
        lambda workflow_id, params, allow_event=False: None,
    )
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "wf", "event": "pre_tool_use", "scope": "global",
         "hook_action": "run_workflow", "matcher": "Bash",
         "params": {"workflow_id": "wf_guard", "params": {"mode": "strict"},
                    "on_fault": "deny", "timeout_seconds": 45}},
        _cfg(),
    )
    assert "[Success]" in out
    h = store.get_hooks("u1")[0]
    assert h.logic.action == "run_workflow"
    assert h.logic.workflow_id == "wf_guard"
    assert h.logic.params == {"mode": "strict"}
    assert h.logic.on_fault == "deny"
    assert h.logic.timeout_seconds == 45


def test_run_workflow_requires_workflow_id(store):
    out = _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "wf", "event": "done",
         "hook_action": "run_workflow"},
        _cfg(),
    )
    assert "[Error]" in out
    assert "workflow_id" in out
    assert store.get_hooks("u1") == []


def test_run_command_update_command_preserves_timeout(store, monkeypatch):
    """Editing just `command` (no `hook_action`) must reach the store and keep timeout."""
    _set_run_command_flag(monkeypatch, True)
    monkeypatch.setattr(hook_tools, "is_admin", lambda *a, **k: True)
    _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "rc", "event": "done", "scope": "global",
         "hook_action": "run_command", "command": "echo hi", "timeout_seconds": 42},
        _cfg(),
    )
    hook = store.get_hooks("u1")[0]
    out = _invoke(
        hook_tools.hook_config,
        {"action": "update", "hook_id": hook.id, "command": "echo bye"},
        _cfg(),
    )
    assert "[Success]" in out
    updated = store.get_hooks("u1")[0]
    assert updated.logic.command == "echo bye"
    assert updated.logic.timeout_seconds == 42  # sibling preserved, not reset to default


def test_run_command_update_command_denied_for_non_admin(store, monkeypatch):
    """In-place behavior edits re-gate: authoring admin is not a permanent pass."""
    _set_run_command_flag(monkeypatch, True)
    monkeypatch.setattr(hook_tools, "is_admin", lambda *a, **k: True)
    _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "rc", "event": "done", "scope": "global",
         "hook_action": "run_command", "command": "echo hi"},
        _cfg(),
    )
    hook = store.get_hooks("u1")[0]
    monkeypatch.setattr(hook_tools, "is_admin", lambda *a, **k: False)
    out = _invoke(
        hook_tools.hook_config,
        {"action": "update", "hook_id": hook.id, "command": "echo bye"},
        _cfg(),
    )
    assert "[Error]" in out
    assert "admin-only" in out
    assert store.get_hooks("u1")[0].logic.command == "echo hi"


def test_run_command_update_enabled_toggle_allowed_for_non_admin(store, monkeypatch):
    """Enabled/name-only updates stay ungated so the owner can switch a hook off."""
    _set_run_command_flag(monkeypatch, True)
    monkeypatch.setattr(hook_tools, "is_admin", lambda *a, **k: True)
    _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "rc", "event": "done", "scope": "global",
         "hook_action": "run_command", "command": "echo hi"},
        _cfg(),
    )
    hook = store.get_hooks("u1")[0]
    monkeypatch.setattr(hook_tools, "is_admin", lambda *a, **k: False)
    out = _invoke(
        hook_tools.hook_config,
        {"action": "update", "hook_id": hook.id, "enabled": False},
        _cfg(),
    )
    assert "[Success]" in out
    updated = store.get_hooks("u1")[0]
    assert updated.enabled is False
    assert updated.logic.command == "echo hi"


# --- Definition-level fire gate (fire_conditions + once) ----------------------

def test_create_with_fire_gate(store):
    out = _invoke(
        hook_tools.hook_config,
        {
            "action": "create", "name": "advisory", "event": "post_tool_use",
            "text": "wrap up ({context_pct_of_trigger}% of trigger)",
            "fire_conditions": [
                {"field": "context_pct_of_trigger", "operator": "gte", "value": "85"}
            ],
            "once": True, "scope": "global",
        },
        _cfg(),
    )
    assert "[Success]" in out
    h = store.get_hooks("u1")[0]
    assert h.once is True
    assert h.fire_conditions[0].operator == "gte"


def test_create_rejects_non_list_fire_conditions(store):
    # The @tool args schema rejects a non-list at the invoke boundary (the
    # agent sees a tool-arg validation error); nothing reaches the store.
    with pytest.raises(Exception):
        _invoke(
            hook_tools.hook_config,
            {
                "action": "create", "name": "n", "event": "done", "text": "x",
                "fire_conditions": "not-a-list",
            },
            _cfg(),
        )
    assert store.get_hooks("u1") == []


def test_update_fire_gate(store):
    _invoke(
        hook_tools.hook_config,
        {"action": "create", "name": "n", "event": "done", "text": "x"},
        _cfg(),
    )
    hook_id = store.get_hooks("u1")[0].id
    out = _invoke(
        hook_tools.hook_config,
        {
            "action": "update", "hook_id": hook_id, "once": True,
            "fire_conditions": [
                {"field": "final_text", "operator": "contains", "value": "FAIL"}
            ],
        },
        _cfg(),
    )
    assert "[Success]" in out
    h = store.get_hook("u1", hook_id)
    assert h.once is True
    assert h.fire_conditions[0].value == "FAIL"


def test_detail_renders_fire_gate(store):
    _invoke(
        hook_tools.hook_config,
        {
            "action": "create", "name": "n", "event": "done", "text": "x",
            "once": True,
            "fire_conditions": [
                {"field": "context_pct_of_trigger", "operator": "gte", "value": "85"}
            ],
        },
        _cfg(),
    )
    hook_id = store.get_hooks("u1")[0].id
    detail = _invoke(
        hook_tools.hook_info, {"action": "detail", "hook_id": hook_id}, _cfg()
    )
    assert "fire_conditions" in detail
    assert "context_pct_of_trigger" in detail
    assert "once: True" in detail


# --- templates + install (bundled hook-template catalog) --------------------

def test_hook_info_templates_lists_catalog(store):
    out = _invoke(hook_tools.hook_info, {"action": "templates"}, _cfg())
    assert "context-checkpoint-advisory" in out
    assert "install" in out


def test_hook_config_install(store):
    out = _invoke(
        hook_tools.hook_config,
        {"action": "install", "template_id": "context-checkpoint-advisory"},
        _cfg(),
    )
    assert "[Success]" in out
    hooks = store.get_hooks("u1")
    assert len(hooks) == 1
    assert hooks[0].template == "context-checkpoint-advisory"
    assert hooks[0].scope == "global"
    assert hooks[0].created_by == "agent"


def test_hook_config_install_idempotent(store):
    _invoke(
        hook_tools.hook_config,
        {"action": "install", "template_id": "context-checkpoint-advisory"},
        _cfg(),
    )
    out = _invoke(
        hook_tools.hook_config,
        {"action": "install", "template_id": "context-checkpoint-advisory"},
        _cfg(),
    )
    assert "already installed" in out
    assert len(store.get_hooks("u1")) == 1


def test_hook_config_install_thread_scope(store):
    _invoke(
        hook_tools.hook_config,
        {
            "action": "install",
            "template_id": "context-checkpoint-advisory",
            "scope": "thread",
        },
        _cfg(thread_id="mythread"),
    )
    h = store.get_hooks("u1")[0]
    assert h.scope == "thread"
    assert h.thread_id == "mythread"


def test_hook_config_install_requires_template_id(store):
    out = _invoke(hook_tools.hook_config, {"action": "install"}, _cfg())
    assert "[Error]" in out
    assert store.get_hooks("u1") == []


def test_hook_detail_shows_template_provenance(store):
    _invoke(
        hook_tools.hook_config,
        {"action": "install", "template_id": "context-checkpoint-advisory"},
        _cfg(),
    )
    hook_id = store.get_hooks("u1")[0].id
    detail = _invoke(
        hook_tools.hook_info, {"action": "detail", "hook_id": hook_id}, _cfg()
    )
    assert "template: context-checkpoint-advisory" in detail


def test_create_single_use_via_tool(store):
    _invoke(
        hook_tools.hook_config,
        {
            "action": "create", "name": "n", "event": "done", "text": "x",
            "single_use": True,
        },
        _cfg(),
    )
    h = store.get_hooks("u1")[0]
    assert h.single_use is True
    detail = _invoke(
        hook_tools.hook_info, {"action": "detail", "hook_id": h.id}, _cfg()
    )
    assert "single_use: True" in detail
