"""memory_add is additive-only; memory_edit owns clearing and replacing.

Regression coverage for the notepad-wipe footgun: a plain
``memory_add(scope="thread", content=...)`` used to overwrite the entire
notepad. It must now append, and deletion/clearing/rewriting must live in
``memory_edit`` (including the empty-``find`` = whole-target convention).
"""

from __future__ import annotations

from types import SimpleNamespace

from nymeria.core.user_profile import UserProfileManager
from nymeria.tools import memory as memory_tools
from nymeria.tools import thread_notes


def _patch_memory_settings(monkeypatch, tmp_path, *, limit: int = 8000) -> SimpleNamespace:
    settings = SimpleNamespace(data_dir=tmp_path, memory_char_limit=limit)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    monkeypatch.setattr(memory_tools, "_profile_manager", None)
    monkeypatch.setattr(thread_notes, "_notes_dir", None)
    return settings


def _config(*, user_id: str = "user", thread_id: str = "thread") -> dict:
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


# ── Thread notepad: memory_add appends, never clobbers ──────────────────────

def test_memory_add_thread_appends_without_clobbering(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path)
    config = _config()

    first = memory_tools.memory_add.func(scope="thread", content="First note.", config=config)
    second = memory_tools.memory_add.func(scope="thread", content="Second note.", config=config)

    assert first.startswith("[Saved]:")
    assert second.startswith("[Saved]:")
    notepad = thread_notes.read_notepad("thread")
    # The original incident: the second add wiped the first. It must survive now.
    assert "First note." in notepad
    assert "Second note." in notepad
    assert notepad.index("First note.") < notepad.index("Second note.")


def test_memory_add_thread_empty_is_noop(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path)
    config = _config()

    memory_tools.memory_add.func(scope="thread", content="Keep me.", config=config)
    result = memory_tools.memory_add.func(scope="thread", content="", config=config)

    assert result.startswith("[Info]:")
    assert "memory_edit" in result
    assert thread_notes.read_notepad("thread") == "Keep me."


# ── Global profile: memory_add never deletes ────────────────────────────────

def test_memory_add_global_empty_is_noop(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path)
    config = _config()

    memory_tools.memory_add.func(scope="global", key="city", content="Sydney", config=config)
    result = memory_tools.memory_add.func(scope="global", key="city", content="", config=config)

    assert result.startswith("[Info]:")
    assert "memory_edit" in result
    profile = UserProfileManager(tmp_path).get_profile("user")
    assert profile.get_memory("city").value == "Sydney"


# ── memory_edit owns clearing/replacing (empty find = whole target) ─────────

def test_memory_edit_thread_empty_find_rewrites_whole_notepad(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path)
    config = _config()

    memory_tools.memory_add.func(scope="thread", content="Old bloated notes.", config=config)
    result = memory_tools.memory_edit.func(
        scope="thread", find="", replace="Tight rewrite.", config=config
    )

    assert result.startswith("[Saved]: Notepad rewritten")
    assert thread_notes.read_notepad("thread") == "Tight rewrite."


def test_memory_edit_thread_empty_find_empty_replace_clears(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path)
    config = _config()

    memory_tools.memory_add.func(scope="thread", content="Some notes.", config=config)
    result = memory_tools.memory_edit.func(scope="thread", find="", replace="", config=config)

    assert result.startswith("[Saved]: Notepad cleared")
    assert thread_notes.read_notepad("thread") is None


def test_memory_edit_thread_empty_find_creates_when_missing(monkeypatch, tmp_path):
    # A full rewrite must land even on an empty/missing notepad (the dream loop
    # uses memory_edit(find="", replace=<rewrite>) for consolidation).
    _patch_memory_settings(monkeypatch, tmp_path)
    config = _config()

    result = memory_tools.memory_edit.func(scope="thread", find="", replace="Created.", config=config)

    assert result.startswith("[Saved]: Notepad rewritten")
    assert thread_notes.read_notepad("thread") == "Created."


def test_memory_edit_thread_clear_when_missing_is_info(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path)
    config = _config()

    result = memory_tools.memory_edit.func(scope="thread", find="", replace="", config=config)

    assert result.startswith("[Info]:")
    assert thread_notes.read_notepad("thread") is None


def test_memory_edit_thread_empty_find_oversized_rewrite_blocked(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path, limit=8)
    config = _config()

    memory_tools.memory_add.func(scope="thread", content="hi", config=config)
    result = memory_tools.memory_edit.func(
        scope="thread", find="", replace="way too long to fit in eight chars", config=config
    )

    assert result.startswith("[Error]: Thread memory is full")
    assert thread_notes.read_notepad("thread") == "hi"


def test_memory_edit_thread_empty_find_shrinking_rewrite_allowed_over_limit(monkeypatch, tmp_path):
    settings = _patch_memory_settings(monkeypatch, tmp_path, limit=100)
    config = _config()

    memory_tools.memory_add.func(scope="thread", content="x" * 50, config=config)
    settings.memory_char_limit = 8  # now the existing notepad is already over limit
    result = memory_tools.memory_edit.func(scope="thread", find="", replace="y" * 20, config=config)

    assert result.startswith("[Saved]: Notepad rewritten")
    assert thread_notes.read_notepad("thread") == "y" * 20


def test_memory_edit_global_empty_find_deletes_only_that_key(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path)
    config = _config()

    memory_tools.memory_add.func(scope="global", key="city", content="Sydney", config=config)
    memory_tools.memory_add.func(scope="global", key="lang", content="Python", config=config)

    result = memory_tools.memory_edit.func(
        scope="global", key="city", find="", replace="", config=config
    )

    assert result.startswith("[Deleted]:")
    profile = UserProfileManager(tmp_path).get_profile("user")
    assert profile.get_memory("city") is None
    assert profile.get_memory("lang").value == "Python"


def test_memory_edit_global_empty_find_sets_value(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path)
    config = _config()

    memory_tools.memory_add.func(scope="global", key="city", content="Sydney", config=config)
    result = memory_tools.memory_edit.func(
        scope="global", key="city", find="", replace="Melbourne", config=config
    )

    assert result.startswith("[Saved]: Updated")
    profile = UserProfileManager(tmp_path).get_profile("user")
    assert profile.get_memory("city").value == "Melbourne"


def test_memory_edit_global_requires_key_no_bulk_wipe(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path)
    config = _config()

    memory_tools.memory_add.func(scope="global", key="city", content="Sydney", config=config)
    # No key -> cannot wipe the whole profile in one call.
    result = memory_tools.memory_edit.func(scope="global", find="", replace="", config=config)

    assert result.startswith("[Error]:")
    profile = UserProfileManager(tmp_path).get_profile("user")
    assert profile.get_memory("city").value == "Sydney"
