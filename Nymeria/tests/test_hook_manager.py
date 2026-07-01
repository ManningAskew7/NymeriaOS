"""Unit tests for the hook record + per-user store (core/hook_manager.py)."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from nymeria.core.hook_manager import (
    BlockIfMatchesLogic,
    HookDefinition,
    HookManager,
    HookStore,
    InjectContextLogic,
    RewriteArgLogic,
    build_logic,
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
