"""Unit tests for the bundled hook-template catalog (core/hook_templates.py).

Loader behavior against a controlled tmp directory, install semantics against
a real ``HookManager``, and a pinning test over the real bundled catalog so a
broken shipped template file fails CI rather than install time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import nymeria
from nymeria.core.hook_manager import HookManager
from nymeria.core.hook_templates import (
    HookTemplate,
    get_template,
    install_template,
    load_templates,
)

BUNDLED_DIR = Path(nymeria.__file__).parent / "hooks_bundled"


@pytest.fixture
def manager(tmp_path):
    return HookManager(tmp_path / "store")


@pytest.fixture
def catalog(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    return d


def _write(directory: Path, template_id: str, **overrides) -> Path:
    data = {
        "id": template_id,
        "title": f"Title {template_id}",
        "description": "a canned recipe",
        "hook": {
            "name": template_id,
            "event": "done",
            "action": "inject_context",
            "params": {"text": "wrap up"},
            "scope": "global",
        },
    }
    data.update(overrides)
    path = directory / f"{template_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- loader -----------------------------------------------------------------

def test_missing_directory_is_empty(tmp_path):
    assert load_templates(tmp_path / "nope") == []


def test_valid_template_loads(catalog):
    _write(catalog, "wrap-up")
    templates = load_templates(catalog)
    assert [t.id for t in templates] == ["wrap-up"]
    assert templates[0].title == "Title wrap-up"


def test_unparseable_file_is_skipped(catalog):
    (catalog / "bad.json").write_text("{not json", encoding="utf-8")
    _write(catalog, "good")
    assert [t.id for t in load_templates(catalog)] == ["good"]


def test_illegal_hook_payload_is_skipped(catalog):
    # notify is not legal on prompt_submit; the dry-run HookDefinition
    # validation must reject the file at scan time.
    _write(
        catalog,
        "illegal",
        hook={
            "name": "illegal",
            "event": "prompt_submit",
            "action": "notify",
            "params": {"text": "x"},
        },
    )
    assert load_templates(catalog) == []


def test_get_template(catalog):
    _write(catalog, "wrap-up")
    assert get_template("wrap-up", catalog) is not None
    assert get_template("unknown", catalog) is None


# --- install ----------------------------------------------------------------

def test_install_creates_hook_with_provenance(manager, catalog):
    _write(catalog, "wrap-up")
    hook, created = install_template(
        manager, "alice", "wrap-up", directory=catalog
    )
    assert created is True
    assert hook is not None
    assert hook.template == "wrap-up"
    assert hook.event == "done"
    assert hook.scope == "global"
    assert hook.logic.text == "wrap up"
    assert manager.get_hooks("alice")[0].id == hook.id


def test_reinstall_is_idempotent(manager, catalog):
    _write(catalog, "wrap-up")
    first, created1 = install_template(manager, "alice", "wrap-up", directory=catalog)
    second, created2 = install_template(manager, "alice", "wrap-up", directory=catalog)
    assert created1 is True and created2 is False
    assert second.id == first.id
    assert len(manager.get_hooks("alice")) == 1


def test_reinstall_with_different_binding_creates_new(manager, catalog):
    _write(catalog, "wrap-up")
    g, _ = install_template(manager, "alice", "wrap-up", directory=catalog)
    t, created = install_template(
        manager, "alice", "wrap-up", scope="thread", thread_id="t1", directory=catalog
    )
    assert created is True
    assert t.id != g.id
    assert t.scope == "thread" and t.thread_id == "t1"


def test_text_and_enabled_overrides(manager, catalog):
    _write(catalog, "wrap-up")
    hook, _ = install_template(
        manager, "alice", "wrap-up", text="custom", enabled=False, directory=catalog
    )
    assert hook.logic.text == "custom"
    assert hook.enabled is False


def test_thread_scope_requires_thread_id(manager, catalog):
    _write(catalog, "wrap-up")
    with pytest.raises(ValueError, match="thread binding"):
        install_template(manager, "alice", "wrap-up", scope="thread", directory=catalog)


def test_unknown_template_raises_with_catalog(manager, catalog):
    _write(catalog, "wrap-up")
    with pytest.raises(ValueError, match="wrap-up"):
        install_template(manager, "alice", "unknown", directory=catalog)


def test_gated_action_template_is_gated(manager, catalog):
    # A run_command template must pass the same authoring gate as create;
    # with HOOKS_RUN_COMMAND_ENABLED off (the default) install is refused.
    _write(
        catalog,
        "gated",
        hook={
            "name": "gated",
            "event": "done",
            "action": "run_command",
            "params": {"command": "echo hi"},
            "scope": "global",
        },
    )
    with pytest.raises(ValueError, match="disabled on this deployment"):
        install_template(manager, "alice", "gated", is_admin=False, directory=catalog)
    assert manager.get_hooks("alice") == []


# --- the real bundled catalog -----------------------------------------------

def test_bundled_catalog_is_valid():
    """Every shipped template file must survive the dry-run validation."""
    files = list(BUNDLED_DIR.glob("*.json"))
    templates = load_templates(BUNDLED_DIR)
    assert len(templates) == len(files), (
        "a shipped hook template failed validation and was skipped"
    )
    assert all(isinstance(t, HookTemplate) for t in templates)


def test_bundled_context_checkpoint_advisory_shape():
    t = get_template("context-checkpoint-advisory", BUNDLED_DIR)
    assert t is not None
    hook = t.hook
    assert hook["event"] == "post_tool_use"
    assert hook["action"] == "inject_context"
    assert hook["once"] is True
    assert hook["scope"] == "global"
    (cond,) = hook["fire_conditions"]
    assert cond == {"field": "context_pct_of_trigger", "operator": "gte", "value": "85"}
    text = hook["params"]["text"]
    # The enriched advisory carries live numbers + concrete wrap-up pointers.
    for needle in (
        "{context_tokens}", "{compact_trigger_tokens}", "{context_pct_of_trigger}",
        "memory_add", "nym_todo", "tool_invoke",
    ):
        assert needle in text


def test_bundled_advisory_installs(manager):
    hook, created = install_template(
        manager, "alice", "context-checkpoint-advisory", directory=BUNDLED_DIR
    )
    assert created is True
    assert hook.once is True
    assert hook.scope == "global"
    assert hook.fire_conditions[0].field == "context_pct_of_trigger"


def test_bundled_turn_end_prompt_shape():
    t = get_template("turn-end-prompt", BUNDLED_DIR)
    assert t is not None
    hook = t.hook
    assert hook["event"] == "done"
    assert hook["action"] == "inject_context"
    assert hook["single_use"] is True
    assert hook["once"] is True
    assert hook["scope"] == "thread"


def test_template_single_use_flows_through_install(manager, catalog):
    _write(
        catalog,
        "oneshot",
        hook={
            "name": "oneshot",
            "event": "done",
            "action": "inject_context",
            "params": {"text": "x"},
            "single_use": True,
            "scope": "thread",
        },
    )
    hook, _ = install_template(
        manager, "alice", "oneshot", thread_id="t1", directory=catalog
    )
    assert hook.single_use is True
    assert hook.scope == "thread" and hook.thread_id == "t1"
