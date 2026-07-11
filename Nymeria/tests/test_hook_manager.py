"""Unit tests for the hook record + per-user store (core/hook_manager.py)."""

from __future__ import annotations

import os
import threading
import time

import pytest
from pydantic import ValidationError

from nymeria.core.hook_manager import (
    MAX_HOOK_EXECUTION_LOG,
    BlockIfMatchesLogic,
    CreateTodoLogic,
    HookDefinition,
    HookExecution,
    HookManager,
    HookStore,
    InjectContextLogic,
    NotifyLogic,
    RewriteArgLogic,
    WebhookLogic,
    build_logic,
    make_execution_recorder,
)


@pytest.fixture
def manager(tmp_path):
    return HookManager(tmp_path)


# --- record validation ------------------------------------------------------

def test_valid_definition_roundtrips():
    h = HookDefinition(
        id="abc123",
        name="n",
        event="post_tool_use",
        matcher="Edit",
        logic=InjectContextLogic(text="hi"),
    )
    assert h.logic.action == "inject_context"
    assert h.matcher == "Edit"


def test_empty_text_is_rejected():
    with pytest.raises(ValidationError):
        InjectContextLogic(text="")


def test_matcher_dropped_on_non_tool_event():
    # A matcher on prompt_submit would silently never match; normalized to None.
    h = HookDefinition(
        id="a", name="n", event="prompt_submit", matcher="Edit", logic=InjectContextLogic(text="x")
    )
    assert h.matcher is None


def test_matcher_kept_on_post_tool_use():
    h = HookDefinition(
        id="a", name="n", event="post_tool_use", matcher="Edit", logic=InjectContextLogic(text="x")
    )
    assert h.matcher == "Edit"


def test_empty_matcher_normalized_to_none():
    # An empty/whitespace matcher on a tool event would parse to [""] and match
    # nothing; normalize to None (match every tool).
    for bad in ("", "   "):
        h = HookDefinition(
            id="a", name="n", event="post_tool_use", matcher=bad, logic=InjectContextLogic(text="x")
        )
        assert h.matcher is None


def test_text_over_cap_rejected():
    with pytest.raises(ValidationError):
        InjectContextLogic(text="x" * 10_001)


def test_illegal_event_rejected():
    with pytest.raises(ValidationError):
        HookDefinition(id="a", name="n", event="pre_tool_use", logic=InjectContextLogic(text="x"))


# --- CRUD -------------------------------------------------------------------

def test_add_and_get_hook(manager):
    h = manager.add_hook("u1", name="n", event="done", text="finish check")
    assert h is not None
    fetched = manager.get_hook("u1", h.id)
    assert fetched is not None
    assert fetched.logic.text == "finish check"


def test_add_hook_defaults_thread_scope(manager):
    h = manager.add_hook("u1", name="n", event="done", text="x", thread_id="t9")
    assert h.scope == "thread"
    assert h.thread_id == "t9"


def test_get_hooks_lists_all_for_user(manager):
    manager.add_hook("u1", name="a", event="done", text="1")
    manager.add_hook("u1", name="b", event="prompt_submit", text="2")
    assert len(manager.get_hooks("u1")) == 2
    assert manager.get_hooks("u2") == []


def test_update_hook_changes_text(manager):
    h = manager.add_hook("u1", name="n", event="done", text="old")
    assert manager.update_hook("u1", h.id, text="new") is True
    assert manager.get_hook("u1", h.id).logic.text == "new"


def test_update_hook_toggles_enabled(manager):
    h = manager.add_hook("u1", name="n", event="done", text="x")
    assert h.enabled is True
    manager.update_hook("u1", h.id, enabled=False)
    assert manager.get_hook("u1", h.id).enabled is False


def test_update_revalidates_illegal_combo(manager):
    # Moving a matcher'd hook to a non-tool event normalizes the matcher away.
    h = manager.add_hook("u1", name="n", event="post_tool_use", text="x", matcher="Edit")
    manager.update_hook("u1", h.id, event="prompt_submit")
    assert manager.get_hook("u1", h.id).matcher is None


def test_update_missing_hook_returns_false(manager):
    assert manager.update_hook("u1", "nope", text="x") is False


def test_delete_hook(manager):
    h = manager.add_hook("u1", name="n", event="done", text="x")
    assert manager.delete_hook("u1", h.id) is True
    assert manager.get_hook("u1", h.id) is None
    assert manager.delete_hook("u1", h.id) is False


def test_delete_hooks_for_thread(manager):
    a = manager.add_hook("u1", name="a", event="done", text="1", scope="thread", thread_id="t1")
    manager.add_hook("u1", name="b", event="done", text="2", scope="thread", thread_id="t2")
    g = manager.add_hook("u1", name="g", event="done", text="3", scope="global")
    deleted = manager.delete_hooks_for_thread("u1", "t1")
    assert deleted == [a.id]
    remaining = {h.id for h in manager.get_hooks("u1")}
    assert g.id in remaining
    assert a.id not in remaining


def test_max_hooks_cap(manager):
    cap = HookStore().MAX_HOOKS
    for i in range(cap):
        assert manager.add_hook("u1", name=f"h{i}", event="done", text="x") is not None
    # The (cap + 1)th returns None and is not stored.
    assert manager.add_hook("u1", name="overflow", event="done", text="x") is None
    assert len(manager.get_hooks("u1")) == cap


# --- persistence robustness -------------------------------------------------

def test_persists_across_manager_instances(manager, tmp_path):
    h = manager.add_hook("u1", name="n", event="done", text="persist me")
    fresh = HookManager(tmp_path)
    assert fresh.get_hook("u1", h.id).logic.text == "persist me"


def test_load_never_raises_on_corrupt_file(manager, tmp_path):
    path = manager._path_for("u1")
    path.write_text("{ this is not json", encoding="utf-8")
    # A bad file must not raise; it degrades to an empty store.
    assert manager.get_hooks("u1") == []


def test_corrupt_file_is_quarantined_not_destroyed(manager, tmp_path):
    path = manager._path_for("u1")
    corrupt_bytes = "{ this is not json"
    path.write_text(corrupt_bytes, encoding="utf-8")
    assert manager.get_hooks("u1") == []
    # The original bytes survive under a quarantine name for manual recovery.
    quarantined = list((path.parent / "quarantine").glob(f"{path.stem}.corrupt-*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == corrupt_bytes
    assert not path.exists()


def test_save_after_corrupt_load_does_not_clobber_quarantine(manager, tmp_path):
    path = manager._path_for("u1")
    corrupt_bytes = '{"hooks": "not a list"'
    path.write_text(corrupt_bytes, encoding="utf-8")
    # A write after the corrupt load lands in a fresh store file; the
    # quarantined original keeps its bytes.
    h = manager.add_hook("u1", name="n", event="done", text="fresh")
    assert manager.get_hook("u1", h.id) is not None
    quarantined = list((path.parent / "quarantine").glob(f"{path.stem}.corrupt-*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == corrupt_bytes


def test_corrupt_load_waits_for_user_lock(manager):
    """Quarantine serializes on the per-user lock: it cannot race a locked writer.

    Without the lock in _load, an unlocked reader (get_hooks) could parse a
    stale corrupt file while a locked writer repairs it, then quarantine-rename
    the freshly written valid store aside.
    """
    path = manager._path_for("u1")
    path.write_text("{ not json", encoding="utf-8")
    results: list = []
    reader = threading.Thread(target=lambda: results.append(manager.get_hooks("u1")))
    lock = manager._get_lock("u1")
    lock.acquire()
    try:
        reader.start()
        time.sleep(0.2)
        # The reader is blocked on the lock: no quarantine has happened yet.
        assert path.exists()
        assert not list((path.parent / "quarantine").glob(f"{path.stem}.corrupt-*.json"))
    finally:
        lock.release()
    reader.join(timeout=5)
    assert results == [[]]
    assert not path.exists()
    assert len(list((path.parent / "quarantine").glob(f"{path.stem}.corrupt-*.json"))) == 1


def test_mtime_cache_refreshes_on_write(manager):
    manager.add_hook("u1", name="a", event="done", text="1")
    assert len(manager.get_hooks_cached("u1")) == 1
    manager.add_hook("u1", name="b", event="done", text="2")
    # Force a distinct mtime so the assertion does not depend on the filesystem's
    # mtime granularity (two back-to-back writes can share an mtime tick, which
    # would flake under load). The cache keys on st_mtime_ns; bump it explicitly.
    path = manager._path_for("u1")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert len(manager.get_hooks_cached("u1")) == 2


def test_cached_read_matches_fresh_read(manager):
    manager.add_hook("u1", name="a", event="done", text="1")
    assert [h.id for h in manager.get_hooks_cached("u1")] == [h.id for h in manager.get_hooks("u1")]


# --- discriminated union (Pass 3 actions) -----------------------------------

def test_legacy_inject_context_json_still_loads():
    # A record persisted before the union migration must load unchanged.
    h = HookDefinition.model_validate(
        {"id": "a", "name": "n", "event": "post_tool_use",
         "matcher": "Edit", "logic": {"action": "inject_context", "text": "hi"}}
    )
    assert isinstance(h.logic, InjectContextLogic)
    assert h.logic.text == "hi"


def test_block_if_matches_roundtrips_and_keeps_matcher():
    h = HookDefinition.model_validate(
        {"id": "b", "name": "guard", "event": "pre_tool_use", "matcher": "bash",
         "logic": {"action": "block_if_matches",
                   "conditions": [{"field": "command", "operator": "contains", "value": "rm -rf"}],
                   "reason": "no"}}
    )
    assert isinstance(h.logic, BlockIfMatchesLogic)
    assert h.matcher == "bash"  # matcher kept on pre_tool_use (a tool event)
    assert len(h.logic.conditions) == 1


def test_rewrite_arg_roundtrips():
    h = HookDefinition.model_validate(
        {"id": "r", "name": "clamp", "event": "pre_tool_use",
         "logic": {"action": "rewrite_arg", "updates": {"command": "echo hi"}}}
    )
    assert isinstance(h.logic, RewriteArgLogic)
    assert h.logic.updates == {"command": "echo hi"}


def test_event_action_legality_rejects_block_on_non_pre():
    for event in ("prompt_submit", "post_tool_use", "done"):
        with pytest.raises(ValidationError):
            HookDefinition.model_validate(
                {"id": "x", "name": "n", "event": event,
                 "logic": {"action": "block_if_matches"}}
            )


def test_event_action_legality_rejects_inject_on_pre():
    with pytest.raises(ValidationError):
        HookDefinition.model_validate(
            {"id": "x", "name": "n", "event": "pre_tool_use",
             "logic": {"action": "inject_context", "text": "x"}}
        )


def test_build_logic_unknown_action_raises_valueerror():
    with pytest.raises(ValueError):
        build_logic("teleport", {})


def test_build_logic_invalid_params_raises_valueerror():
    # min_length=1 on inject_context text -> ValidationError mapped to ValueError.
    with pytest.raises(ValueError):
        build_logic("inject_context", {"text": ""})


def test_add_hook_with_action_and_params(manager):
    h = manager.add_hook(
        "u1", name="guard", event="pre_tool_use", action="block_if_matches",
        params={"conditions": [{"field": "command", "operator": "contains", "value": "rm"}],
                "reason": "denied"},
        matcher="bash",
    )
    assert h is not None
    got = manager.get_hook("u1", h.id)
    assert isinstance(got.logic, BlockIfMatchesLogic)
    assert got.logic.reason == "denied"
    assert got.matcher == "bash"


def test_add_hook_rejects_illegal_action_for_event(manager):
    with pytest.raises(ValueError):
        manager.add_hook(
            "u1", name="bad", event="prompt_submit", action="block_if_matches", params={}
        )


def test_update_hook_switches_action(manager):
    h = manager.add_hook(
        "u1", name="g", event="pre_tool_use", action="block_if_matches",
        params={"conditions": [], "reason": "x"},
    )
    assert manager.update_hook(
        "u1", h.id, action="rewrite_arg", params={"updates": {"command": "ls"}}
    ) is True
    got = manager.get_hook("u1", h.id)
    assert isinstance(got.logic, RewriteArgLogic)
    assert got.logic.updates == {"command": "ls"}


def test_update_hook_replaces_params(manager):
    h = manager.add_hook(
        "u1", name="g", event="pre_tool_use", action="block_if_matches",
        params={"conditions": [{"field": "command", "operator": "contains", "value": "a"}]},
    )
    manager.update_hook("u1", h.id, params={"conditions": [], "reason": "always"})
    got = manager.get_hook("u1", h.id)
    assert got.logic.conditions == []
    assert got.logic.reason == "always"


# --- observe-plane actions (Pass 3 slice B) ---------------------------------

def test_notify_and_create_todo_roundtrip():
    n = HookDefinition.model_validate(
        {"id": "n", "name": "ping", "event": "done",
         "logic": {"action": "notify", "text": "done: {final_text}"}}
    )
    assert isinstance(n.logic, NotifyLogic)
    t = HookDefinition.model_validate(
        {"id": "t", "name": "td", "event": "post_tool_use",
         "logic": {"action": "create_todo", "text": "follow up"}}
    )
    assert isinstance(t.logic, CreateTodoLogic)


def test_webhook_roundtrip_requires_url():
    w = HookDefinition.model_validate(
        {"id": "w", "name": "wh", "event": "done",
         "logic": {"action": "webhook", "url": "https://x.test/h", "text": "body"}}
    )
    assert isinstance(w.logic, WebhookLogic)
    assert w.logic.url == "https://x.test/h"
    with pytest.raises(ValueError):  # build_logic maps ValidationError -> ValueError
        build_logic("webhook", {"text": "no url"})  # url is required


def test_observe_actions_legal_on_post_and_done_only():
    for action in ("notify", "create_todo", "webhook"):
        params = {"url": "https://x.test"} if action == "webhook" else {"text": "x"}
        # legal on post_tool_use and done
        for event in ("post_tool_use", "done"):
            assert manager_add(event, action, params) is not None
        # illegal on prompt_submit and pre_tool_use
        for event in ("prompt_submit", "pre_tool_use"):
            with pytest.raises(ValueError):
                manager_add(event, action, params)


def manager_add(event, action, params):
    # Helper using a throwaway manager so legality (not persistence) is under test.
    import tempfile
    from pathlib import Path
    m = HookManager(Path(tempfile.mkdtemp()))
    return m.add_hook("u", name="n", event=event, action=action, params=params)


# --- run_command (pass 5) ----------------------------------------------------

def test_run_command_roundtrips_on_all_four_events():
    from nymeria.core.hook_manager import RunCommandLogic
    for event in ("prompt_submit", "pre_tool_use", "post_tool_use", "done"):
        h = HookDefinition.model_validate(
            {"id": "rc", "name": "cmd", "event": event,
             "logic": {"action": "run_command", "command": "echo hi", "timeout_seconds": 12}}
        )
        assert isinstance(h.logic, RunCommandLogic)
        assert h.logic.command == "echo hi"
        assert h.logic.timeout_seconds == 12


def test_run_command_timeout_bounds_enforced():
    with pytest.raises(ValueError):
        build_logic("run_command", {"command": "x", "timeout_seconds": 0})  # < 1
    with pytest.raises(ValueError):
        build_logic("run_command", {"command": "x", "timeout_seconds": 999})  # > 300
    with pytest.raises(ValueError):
        build_logic("run_command", {"command": ""})  # command required


def test_run_command_flat_fields_map_to_params():
    from nymeria.core.hook_manager import params_from_fields
    params = params_from_fields("run_command", command="ls -la", timeout_seconds=42.0)
    assert params == {"command": "ls -la", "timeout_seconds": 42.0}
    assert params_from_fields("run_command", command=None, timeout_seconds=None) is None


def test_run_command_authoring_gate():
    from nymeria.core.hook_manager import run_command_authoring_error
    from unittest.mock import patch
    # Non-gated action is always allowed regardless of flag/admin.
    assert run_command_authoring_error("inject_context", is_admin=False) is None
    # Flag off -> error mentioning the env var (even for an admin).
    with patch("nymeria.config.get_settings",
               return_value=type("S", (), {"hooks_run_command_enabled": False})()):
        msg = run_command_authoring_error("run_command", is_admin=True)
        assert msg and "HOOKS_RUN_COMMAND_ENABLED" in msg
    # Flag on but not admin -> admin-only error.
    with patch("nymeria.config.get_settings",
               return_value=type("S", (), {"hooks_run_command_enabled": True})()):
        assert "admin-only" in run_command_authoring_error("run_command", is_admin=False)
        # Flag on + admin -> allowed. None (trusted local) is also allowed.
        assert run_command_authoring_error("run_command", is_admin=True) is None
        assert run_command_authoring_error("run_command", is_admin=None) is None


# --- execution log (write-behind) --------------------------------------------

def _exec(hook_id="h1", **kw):
    base = dict(hook_id=hook_id, hook_name="n", event="done", plane="mutate", status="ok")
    base.update(kw)
    return HookExecution(**base)


def test_execution_log_round_trip_newest_first(manager):
    manager.log_execution("u1", _exec(detail="first"))
    manager.log_execution("u1", _exec(detail="second"))
    entries = manager.get_executions("u1")
    assert [e["detail"] for e in entries] == ["second", "first"]
    e = entries[0]
    assert e["hook_id"] == "h1"
    assert e["event"] == "done"
    assert e["plane"] == "mutate"
    assert e["status"] == "ok"


def test_execution_log_filter_by_hook_and_limit(manager):
    for i in range(5):
        manager.log_execution("u1", _exec(hook_id="a", detail=f"a{i}"))
        manager.log_execution("u1", _exec(hook_id="b", detail=f"b{i}"))
    only_a = manager.get_executions("u1", hook_id="a")
    assert {e["hook_id"] for e in only_a} == {"a"}
    assert len(only_a) == 5
    limited = manager.get_executions("u1", limit=3)
    assert len(limited) == 3
    assert limited[0]["detail"] == "b4"  # newest first


def test_execution_log_caps_entries(manager):
    for i in range(MAX_HOOK_EXECUTION_LOG + 25):
        manager.log_execution("u1", _exec(detail=str(i)))
    manager.flush_execution_log("u1")
    entries = manager.get_executions("u1", limit=MAX_HOOK_EXECUTION_LOG + 25)
    assert len(entries) == MAX_HOOK_EXECUTION_LOG
    # The oldest entries were dropped, the newest kept.
    assert entries[0]["detail"] == str(MAX_HOOK_EXECUTION_LOG + 24)


def test_execution_log_purged_on_hook_delete(manager):
    h = manager.add_hook("u1", name="n", event="done", text="x")
    manager.log_execution("u1", _exec(hook_id=h.id))
    manager.log_execution("u1", _exec(hook_id="other"))
    assert manager.delete_hook("u1", h.id) is True
    assert manager.get_executions("u1", hook_id=h.id) == []
    # Unrelated entries survive.
    assert len(manager.get_executions("u1")) == 1


def test_execution_log_purged_on_thread_delete(manager):
    h = manager.add_hook("u1", name="n", event="done", text="x", thread_id="t9")
    manager.log_execution("u1", _exec(hook_id=h.id))
    deleted = manager.delete_hooks_for_thread("u1", "t9")
    assert deleted == [h.id]
    assert manager.get_executions("u1", hook_id=h.id) == []


def test_make_execution_recorder_duck_types_engine_objects(manager):
    from types import SimpleNamespace

    recorder = make_execution_recorder(manager, "u1")
    reg = SimpleNamespace(definition_id="abcd1234", name="guard", observe=False)
    ctx = SimpleNamespace(
        event=SimpleNamespace(value="pre_tool_use"),
        thread_id="t1",
        tool_name="bash",
    )
    recorder(reg, ctx, status="ok", detail="deny: nope", duration=0.0123)
    entries = manager.get_executions("u1")
    assert len(entries) == 1
    e = entries[0]
    assert e["hook_id"] == "abcd1234"
    assert e["hook_name"] == "guard"
    assert e["event"] == "pre_tool_use"
    assert e["plane"] == "mutate"
    assert e["tool_name"] == "bash"
    assert e["detail"] == "deny: nope"


def test_make_execution_recorder_never_raises(manager):
    from types import SimpleNamespace

    recorder = make_execution_recorder(manager, "u1")
    reg = SimpleNamespace(definition_id="x", name="n", observe=False)
    ctx = SimpleNamespace(event=None, thread_id="t", tool_name=None)
    # An invalid status fails pydantic validation; the recorder swallows it.
    recorder(reg, ctx, status="bogus-status", detail="", duration=0.0)
    assert manager.get_executions("u1") == []


# --- Definition-level fire gate: fire_conditions + once ------------------------

def test_fire_gate_fields_default_and_roundtrip(manager):
    # Legacy shape (no gate fields) parses with inert defaults.
    legacy = HookDefinition.model_validate({
        "id": "old1", "name": "n", "event": "done",
        "logic": {"action": "inject_context", "text": "hi"},
    })
    assert legacy.fire_conditions == []
    assert legacy.once is False
    # Authored gate persists through the store and reloads.
    hook = manager.add_hook(
        "u1", name="advisory", event="post_tool_use", text="wrap up",
        fire_conditions=[
            {"field": "context_pct_of_trigger", "operator": "gte", "value": "85"}
        ],
        once=True, scope="global",
    )
    reloaded = manager.get_hook("u1", hook.id)
    assert reloaded.once is True
    assert len(reloaded.fire_conditions) == 1
    cond = reloaded.fire_conditions[0]
    assert (cond.field, cond.operator, cond.value) == (
        "context_pct_of_trigger", "gte", "85"
    )


def test_fire_gate_updates_via_update_hook(manager):
    hook = manager.add_hook("u1", name="n", event="done", text="x", scope="global")
    assert manager.update_hook(
        "u1", hook.id,
        fire_conditions=[{"field": "final_text", "operator": "contains", "value": "error"}],
        once=True,
    )
    updated = manager.get_hook("u1", hook.id)
    assert updated.once is True
    assert updated.fire_conditions[0].field == "final_text"
    # Clearing the gate works too.
    assert manager.update_hook("u1", hook.id, fire_conditions=[], once=False)
    cleared = manager.get_hook("u1", hook.id)
    assert cleared.fire_conditions == []
    assert cleared.once is False


def test_fire_gate_invalid_operator_rejected(manager):
    with pytest.raises((ValidationError, ValueError)):
        manager.add_hook(
            "u1", name="n", event="done", text="x", scope="global",
            fire_conditions=[{"field": "x", "operator": "sideways", "value": "1"}],
        )


# --- single_use (delete after first successful run) -------------------------

def test_single_use_field_defaults_false_and_roundtrips(manager):
    h = manager.add_hook("u", name="n", event="done", text="x")
    assert h.single_use is False
    h2 = manager.add_hook("u", name="n2", event="done", text="x", single_use=True)
    assert h2.single_use is True
    assert manager.get_hook("u", h2.id).single_use is True


def test_legacy_store_without_single_use_loads(manager):
    # Old stored JSON has no single_use/template keys; defaults must apply.
    h = HookDefinition(
        id="leg1", name="n", event="done", logic=InjectContextLogic(text="x")
    )
    data = h.model_dump(mode="json")
    data.pop("single_use", None)
    data.pop("template", None)
    loaded = HookDefinition.model_validate(data)
    assert loaded.single_use is False
    assert loaded.template == ""


def test_delete_hook_purge_log_false_keeps_executions(manager):
    h = manager.add_hook("u", name="n", event="done", text="x")
    manager.log_execution(
        "u", HookExecution(hook_id=h.id, hook_name="n", event="done", status="ok")
    )
    assert manager.delete_hook("u", h.id, purge_log=False) is True
    entries = manager.get_executions("u")
    assert any(e["hook_id"] == h.id for e in entries)


def _reg_for(hook, *, single_use=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        definition_id=hook.id,
        name=hook.name,
        observe=False,
        single_use=hook.single_use if single_use is None else single_use,
    )


def _ctx_stub():
    from types import SimpleNamespace

    return SimpleNamespace(event="done", thread_id="t1", tool_name=None)


def test_recorder_deletes_single_use_on_ok_synchronously(manager):
    h = manager.add_hook("u", name="n", event="done", text="x", single_use=True)
    recorder = make_execution_recorder(manager, "u")
    recorder(_reg_for(h), _ctx_stub(), status="ok", detail="fired", duration=0.1)
    # Synchronous: the definition is gone immediately (no flush needed), so
    # "absent" reliably means "fired" for claim-by-delete callers.
    assert manager.get_hook("u", h.id) is None
    # The log entry survives the self-cleanup.
    assert any(e["hook_id"] == h.id for e in manager.get_executions("u"))


def test_recorder_keeps_single_use_on_non_ok(manager):
    h = manager.add_hook("u", name="n", event="done", text="x", single_use=True)
    recorder = make_execution_recorder(manager, "u")
    for status in ("no_op", "error", "timeout", "saturated", "illegal"):
        recorder(_reg_for(h), _ctx_stub(), status=status, detail="", duration=0.0)
        assert manager.get_hook("u", h.id) is not None, status


def test_recorder_keeps_non_single_use_on_ok(manager):
    h = manager.add_hook("u", name="n", event="done", text="x")
    recorder = make_execution_recorder(manager, "u")
    recorder(_reg_for(h), _ctx_stub(), status="ok", detail="fired", duration=0.1)
    assert manager.get_hook("u", h.id) is not None
