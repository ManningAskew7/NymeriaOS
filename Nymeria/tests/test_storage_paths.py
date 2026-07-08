"""Tests for core.storage_paths.safe_path_segment and its storage-manager wiring.

safe_path_segment is the canonical owner (slice 06 / F6) of the path-segment
sanitization idiom previously inlined across ~10 core storage modules. These
tests lock the helper's behavior, prove byte-equivalence to the retired inline
idiom, and assert that each migrated storage manager still resolves a hostile
identifier to a path inside its own directory (no traversal escape).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nymeria.core.storage_paths import (
    mtime_sort_key,
    safe_path_segment,
    write_text_atomic,
)


def _legacy(value: str, default: str = "default") -> str:
    """The exact inline idiom this helper replaced, for differential testing."""
    return "".join(c for c in value if c.isalnum() or c in "-_") or default


class TestSafePathSegment:
    def test_alnum_passthrough(self):
        assert safe_path_segment("user1") == "user1"

    def test_keeps_hyphen_and_underscore(self):
        assert safe_path_segment("a-b_c") == "a-b_c"

    def test_drops_separators_and_dots(self):
        # The dropped "/" "\\" and "." are exactly what prevents traversal.
        assert safe_path_segment("../../etc/passwd") == "etcpasswd"
        assert safe_path_segment("a/b\\c.d") == "abcd"

    def test_empty_input_returns_default(self):
        assert safe_path_segment("") == "default"

    def test_all_punctuation_returns_default(self):
        assert safe_path_segment("...") == "default"
        assert safe_path_segment("/././") == "default"

    def test_custom_default(self):
        assert safe_path_segment("", default="anon") == "anon"
        assert safe_path_segment("..", default="anon") == "anon"

    def test_empty_default_preserves_legacy_empty_contract(self):
        # Non-core callers that historically returned "" opt out of the fallback.
        assert safe_path_segment("...", default="") == ""
        assert safe_path_segment("", default="") == ""

    def test_unicode_alphanumerics_are_kept(self):
        # str.isalnum() is True for unicode letters/digits, matching the old idiom.
        assert safe_path_segment("déjà-vu") == "déjà-vu"
        assert safe_path_segment("用户42") == "用户42"

    def test_no_stripping_of_leading_trailing_allowed_chars(self):
        assert safe_path_segment("_-user-_") == "_-user-_"

    def test_whitespace_is_dropped(self):
        assert safe_path_segment("U S E R") == "USER"


class TestMtimeSortKey:
    """mtime_sort_key guards stat() so a store file removed between the glob and
    the sort (a resolve/sweep/prune/claim race) ranks oldest instead of raising
    FileNotFoundError out of sorted()."""

    def test_returns_mtime_ns_and_name(self, tmp_path: Path):
        f = tmp_path / "rec.json"
        f.write_text("{}", encoding="utf-8")
        assert mtime_sort_key(f) == (f.stat().st_mtime_ns, "rec.json")

    def test_missing_file_ranks_oldest_without_raising(self, tmp_path: Path):
        ghost = tmp_path / "gone.json"  # never created
        assert mtime_sort_key(ghost) == (0, "gone.json")

    def test_newest_first_with_ghost_sorted_last(self, tmp_path: Path):
        import os

        old = tmp_path / "old.json"
        old.write_text("{}", encoding="utf-8")
        new = tmp_path / "new.json"
        new.write_text("{}", encoding="utf-8")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        ghost = tmp_path / "ghost.json"  # missing -> (0, name), i.e. oldest

        # The exact shape of every store's listing: glob yields a since-deleted
        # path, and the sort must place it last (where the read loop skips it).
        ordered = sorted([old, new, ghost], key=mtime_sort_key, reverse=True)
        assert ordered == [new, old, ghost]


@pytest.mark.parametrize(
    "value",
    [
        "user1",
        "default",
        "../etc/passwd",
        "../../../",
        "",
        "...",
        "a/b\\c",
        "user.name",
        "déjà-vu",
        "___",
        "-_-",
        "U S E R",
        "user@host:1234",
        "%2e%2e%2f",
        "线程-1",
    ],
)
def test_equivalence_with_legacy_idiom(value):
    assert safe_path_segment(value) == _legacy(value)
    assert safe_path_segment(value, default="") == _legacy(value, "")
    assert safe_path_segment(value, default="x") == _legacy(value, "x")


class TestWriteTextAtomic:
    def test_writes_content_and_returns_path(self, tmp_path: Path):
        target = tmp_path / "store.json"
        result = write_text_atomic(target, '{"a": 1}')
        assert result == target
        assert target.read_text(encoding="utf-8") == '{"a": 1}'

    def test_overwrite_replaces_prior_contents(self, tmp_path: Path):
        target = tmp_path / "store.json"
        write_text_atomic(target, "old")
        write_text_atomic(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

    def test_leaves_no_temp_files_behind(self, tmp_path: Path):
        target = tmp_path / "store.json"
        write_text_atomic(target, "x")
        assert [p.name for p in tmp_path.iterdir()] == ["store.json"]

    def test_failed_write_preserves_old_file_and_cleans_temp(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "store.json"
        write_text_atomic(target, "good")

        # Simulate a crash after the temp is written but before the rename.
        import pathlib

        original_replace = pathlib.Path.replace

        def boom(self, *args, **kwargs):
            if self.name.endswith(".tmp"):
                raise OSError("simulated crash before rename")
            return original_replace(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "replace", boom)
        with pytest.raises(OSError):
            write_text_atomic(target, "bad")

        # Old file intact (never torn), and the temp was cleaned up.
        assert target.read_text(encoding="utf-8") == "good"
        assert [p.name for p in tmp_path.iterdir()] == ["store.json"]


# (manager class, path-builder method name, dir attribute) for each migrated
# storage manager. UserProfileManager nests under a per-user directory, the rest
# write a flat file, but all must keep the result inside their dir attribute.
_MANAGERS = [
    ("nymeria.core.goal_manager", "GoalManager", "_path_for", "goals_dir"),
    ("nymeria.core.trigger_manager", "TriggerManager", "_path_for", "triggers_dir"),
    ("nymeria.core.trigger_manager", "TriggerManager", "_executions_path", "triggers_dir"),
    ("nymeria.core.todo_manager", "TodoManager", "_get_todos_path", "todos_dir"),
    ("nymeria.core.activity_log", "ActivityLog", "_get_activity_path", "activity_dir"),
    ("nymeria.core.thread_metadata", "ThreadMetadataManager", "_get_path", "metadata_dir"),
    ("nymeria.core.thread_config", "ThreadConfigManager", "_get_config_path", "configs_dir"),
    ("nymeria.core.user_profile", "UserProfileManager", "_get_profile_path", "users_dir"),
    ("nymeria.core.notifications", "NotificationStore", "_get_notifications_path", "notifications_dir"),
]


def _build(module_name: str, cls_name: str, data_dir: Path):
    import importlib

    cls = getattr(importlib.import_module(module_name), cls_name)
    return cls(data_dir)


@pytest.mark.parametrize("module_name,cls_name,method,dir_attr", _MANAGERS)
class TestManagerWiringResistsTraversal:
    def test_traversal_id_stays_inside_manager_dir(
        self, tmp_path: Path, module_name, cls_name, method, dir_attr
    ):
        manager = _build(module_name, cls_name, tmp_path)
        base = getattr(manager, dir_attr).resolve()
        path = getattr(manager, method)("../../etc/passwd").resolve()
        # The hostile id must not escape the manager's own directory.
        assert base == path or base in path.parents
        assert "etcpasswd" in path.name or "etcpasswd" in path.parent.name
        assert ".." not in path.parts

    def test_empty_id_uses_default_segment(
        self, tmp_path: Path, module_name, cls_name, method, dir_attr
    ):
        manager = _build(module_name, cls_name, tmp_path)
        path = getattr(manager, method)("").resolve()
        assert "default" in (path.name + "/" + path.parent.name)


class TestThreadDeletionSafeFile:
    """thread_deletion._safe_thread_file is a module-level helper (not a manager
    method), so it gets its own coverage rather than the _MANAGERS table above."""

    def test_traversal_id_stays_inside_folder(self, tmp_path: Path):
        from nymeria.core.thread_deletion import _safe_thread_file

        base = (tmp_path / "thread_notes").resolve()
        path = _safe_thread_file(tmp_path, "thread_notes", "../../etc/passwd", ".md").resolve()
        assert base in path.parents
        assert path.name == "etcpasswd.md"
        assert ".." not in path.parts

    def test_empty_id_uses_default_segment(self, tmp_path: Path):
        from nymeria.core.thread_deletion import _safe_thread_file

        path = _safe_thread_file(tmp_path, "thread_notes", "", ".md").resolve()
        assert path.name == "default.md"

    def test_alnum_id_passthrough(self, tmp_path: Path):
        from nymeria.core.thread_deletion import _safe_thread_file

        path = _safe_thread_file(tmp_path, "thread_notes", "thread-1_abc", ".md")
        assert path == tmp_path / "thread_notes" / "thread-1_abc.md"
