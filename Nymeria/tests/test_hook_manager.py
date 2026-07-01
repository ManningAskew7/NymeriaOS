"""Unit tests for the hook record + per-user store (core/hook_manager.py)."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from nymeria.core.hook_manager import HookDefinition, HookLogic, HookManager, HookStore


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
        logic=HookLogic(text="hi"),
    )
    assert h.logic.action == "inject_context"
    assert h.matcher == "Edit"


def test_empty_text_is_rejected():
    with pytest.raises(ValidationError):
        HookLogic(text="")


def test_matcher_dropped_on_non_tool_event():
    # A matcher on prompt_submit would silently never match; normalized to None.
    h = HookDefinition(
        id="a", name="n", event="prompt_submit", matcher="Edit", logic=HookLogic(text="x")
    )
    assert h.matcher is None


def test_matcher_kept_on_post_tool_use():
    h = HookDefinition(
        id="a", name="n", event="post_tool_use", matcher="Edit", logic=HookLogic(text="x")
    )
    assert h.matcher == "Edit"


def test_empty_matcher_normalized_to_none():
    # An empty/whitespace matcher on a tool event would parse to [""] and match
    # nothing; normalize to None (match every tool).
    for bad in ("", "   "):
        h = HookDefinition(
            id="a", name="n", event="post_tool_use", matcher=bad, logic=HookLogic(text="x")
        )
        assert h.matcher is None


def test_text_over_cap_rejected():
    with pytest.raises(ValidationError):
        HookLogic(text="x" * 10_001)


def test_illegal_event_rejected():
    with pytest.raises(ValidationError):
        HookDefinition(id="a", name="n", event="pre_tool_use", logic=HookLogic(text="x"))


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
