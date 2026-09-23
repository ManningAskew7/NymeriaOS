"""Tests for ``file_list`` (#257): the read-only directory listing beside the
three file tools, so an agent can rediscover what it wrote into its
workspace without being granted a shell.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nymeria.tools.execution_environment import (
    ExecutionEnvironment,
    configure_environment_aware_tool_descriptions,
)
from nymeria.tools.filesystem import file_list


@pytest.fixture()
def workspace(tmp_path, monkeypatch) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(ws))
    return ws


def _populate(ws: Path) -> None:
    (ws / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    (ws / "notes.txt").write_text("one\ntwo\n", encoding="utf-8")
    (ws / "images").mkdir()
    (ws / "images" / "chart.png").write_bytes(b"\x89PNG fake")
    (ws / "images" / "nested").mkdir()
    (ws / "images" / "nested" / "deep.txt").write_text("x", encoding="utf-8")


def _lines(result: str) -> list[str]:
    return [line for line in result.splitlines() if line.strip()]


def test_default_path_lists_the_workspace_sorted_with_dirs_marked(workspace):
    _populate(workspace)

    result = file_list.func(path="")

    assert result.startswith(f"Listing of {workspace}")
    names = [line.split()[0] for line in _lines(result)[1:-1]]
    assert names == ["images/", "notes.txt", "report.pdf"]
    assert "notes.txt  8 bytes  " in result
    assert result.rstrip().endswith("[3 entries]")


def test_relative_path_resolves_from_the_default_tool_cwd(workspace, monkeypatch, tmp_path):
    from nymeria.tools import execution_environment as env_module

    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_text("a", encoding="utf-8")
    monkeypatch.setattr(env_module, "default_tool_cwd", lambda: root)

    result = file_list.func(path="sub")

    assert result.startswith(f"Listing of {root / 'sub'}")
    assert "a.txt  1 bytes" in result


def test_glob_filters_by_name(workspace):
    _populate(workspace)

    result = file_list.func(path=str(workspace), glob="*.txt")

    assert [line.split()[0] for line in _lines(result)[1:-1]] == ["notes.txt"]
    assert result.rstrip().endswith("[1 entry]")


def test_glob_with_no_match_says_so(workspace):
    _populate(workspace)

    result = file_list.func(path=str(workspace), glob="*.docx")

    assert "[No entries match '*.docx']" in result


def test_empty_directory(workspace):
    assert "[Empty directory]" in file_list.func(path=str(workspace))


def test_recursive_lists_relative_paths_and_matches_glob_against_them(workspace):
    _populate(workspace)

    result = file_list.func(path=str(workspace), recursive=True)
    names = [line.split()[0] for line in _lines(result)[1:-1]]
    assert names == [
        "images/",
        "images/chart.png",
        "images/nested/",
        "images/nested/deep.txt",
        "notes.txt",
        "report.pdf",
    ]
    assert "(recursive)" in _lines(result)[0]

    filtered = file_list.func(path=str(workspace), recursive=True, glob="images/*.png")
    assert [line.split()[0] for line in _lines(filtered)[1:-1]] == ["images/chart.png"]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlinks_are_marked_and_never_followed(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    (workspace / "loop").symlink_to(workspace, target_is_directory=True)

    result = file_list.func(path=str(workspace), recursive=True)

    names = [line.split()[0] for line in _lines(result)[1:-1]]
    assert names == ["link@", "loop@"]
    assert "secret.txt" not in result


def test_max_entries_truncates_with_a_note(workspace):
    for i in range(12):
        (workspace / f"f{i:02d}.txt").write_text("x", encoding="utf-8")

    result = file_list.func(path=str(workspace), max_entries=5)

    names = [line.split()[0] for line in _lines(result)[1:-1]]
    assert names == ["f00.txt", "f01.txt", "f02.txt", "f03.txt", "f04.txt"]
    assert "[Truncated at 5 entries; more exist." in result


def test_max_entries_out_of_range_is_an_error(workspace):
    assert file_list.func(path=str(workspace), max_entries=0).startswith("[Error]: max_entries")
    assert file_list.func(path=str(workspace), max_entries=5000).startswith("[Error]: max_entries")


def test_missing_directory_is_an_error(workspace):
    result = file_list.func(path=str(workspace / "nope"))
    assert result.startswith(f"[Error]: Directory not found: {workspace / 'nope'}")


def test_a_file_path_is_refused_and_points_at_file_read(workspace):
    _populate(workspace)
    result = file_list.func(path=str(workspace / "notes.txt"))
    assert result.startswith("[Error]: Not a directory:")
    assert "file_read" in result


def test_credential_store_root_is_refused(workspace, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nymeria.tools import filesystem as fs_module

    data_dir = tmp_path / "data"
    (data_dir / "auth_tokens").mkdir(parents=True)
    (data_dir / "auth_tokens" / "tok.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fs_module, "get_settings", lambda: SimpleNamespace(data_dir=data_dir))

    result = file_list.func(path=str(data_dir / "auth_tokens"))

    assert result.startswith("[Error]: Cannot access credential storage")
    assert "tok.json" not in result


def test_recursive_walk_marks_a_credential_store_and_does_not_descend(workspace, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nymeria.tools import filesystem as fs_module

    data_dir = tmp_path / "data"
    (data_dir / "auth_tokens").mkdir(parents=True)
    (data_dir / "auth_tokens" / "tok.json").write_text("{}", encoding="utf-8")
    (data_dir / "threads").mkdir()
    (data_dir / "threads" / "t1.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fs_module, "get_settings", lambda: SimpleNamespace(data_dir=data_dir))

    result = file_list.func(path=str(data_dir), recursive=True)

    assert "auth_tokens/  (excluded from the file tools)" in result
    assert "tok.json" not in result
    assert "threads/t1.json" in result


def test_unreadable_subdirectory_is_counted_not_fatal(workspace):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("permission bits are not enforced here")
    _populate(workspace)
    locked = workspace / "images" / "nested"
    locked.chmod(0o000)
    try:
        result = file_list.func(path=str(workspace), recursive=True)
    finally:
        locked.chmod(0o755)

    assert "images/nested/" in result
    assert "deep.txt" not in result
    assert "[1 directory could not be read]" in result


def _env(**overrides):
    fields = dict(
        platform_label="TestOS 1",
        default_cwd="/repo/root",
        process_cwd="/service",
        shell_executable="/bin/sh",
        available_shells=("bash",),
        in_container=True,
        path_separator="/",
    )
    fields.update(overrides)
    return ExecutionEnvironment(**fields)


def test_description_carries_the_path_context_and_the_workspace(workspace, monkeypatch):
    monkeypatch.setattr(file_list, "description", file_list.description)
    configure_environment_aware_tool_descriptions([file_list], _env(workspace_dir=str(workspace)))

    description = file_list.description or ""
    assert "Relative path arguments (file_path, path) resolve from: /repo/root" in description
    assert f"Agent workspace (your own files; file_list's default root, and the only root attachments deliver from): {workspace}." in description


def test_description_says_when_no_workspace_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(file_list, "description", file_list.description)
    missing = tmp_path / "no-such-workspace"

    configure_environment_aware_tool_descriptions([file_list], _env(workspace_dir=str(missing)))

    description = file_list.description or ""
    assert f"Agent workspace: none on this deployment ({missing} does not exist" in description
    assert "file_list with no path lists the default cwd" in description


def test_configuring_the_description_twice_is_idempotent(workspace, monkeypatch):
    monkeypatch.setattr(file_list, "description", file_list.description)
    env = _env(workspace_dir=str(workspace))
    configure_environment_aware_tool_descriptions([file_list], env)
    once = file_list.description
    configure_environment_aware_tool_descriptions([file_list], env)
    assert file_list.description == once
    assert once.count("Runtime path context:") == 1


def test_no_workspace_falls_back_to_the_default_cwd_with_a_note(tmp_path, monkeypatch):
    from nymeria.tools import execution_environment as env_module

    missing = tmp_path / "no-such-workspace"
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(missing))
    cwd = tmp_path / "project"
    cwd.mkdir()
    (cwd / "run.py").write_text("x", encoding="utf-8")
    monkeypatch.setattr(env_module, "default_tool_cwd", lambda: cwd)

    result = file_list.func(path="")

    first, header = result.splitlines()[:2]
    assert first.startswith(f"[Note]: No workspace directory exists at {missing}")
    assert f"lists the default tool cwd {cwd} instead" in first
    assert header == f"Listing of {cwd}"
    assert "run.py  1 bytes" in result


@pytest.mark.skipif(not Path("/proc/1").exists(), reason="Linux procfs only")
def test_proc_is_refused_as_a_root_and_marked_in_a_listing(workspace):
    refused = file_list.func(path="/proc")
    assert refused.startswith("[Error]: Cannot access /proc")

    listing = file_list.func(path="/", glob="proc")
    assert "proc/  (excluded from the file tools)" in listing
    assert "[1 entry]" in listing


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlink_to_a_credential_store_is_shown_as_a_link_and_not_entered(workspace, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nymeria.tools import filesystem as fs_module

    data_dir = tmp_path / "data"
    (data_dir / "auth_tokens").mkdir(parents=True)
    (data_dir / "auth_tokens" / "tok.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fs_module, "get_settings", lambda: SimpleNamespace(data_dir=data_dir))
    (workspace / "tokens").symlink_to(data_dir / "auth_tokens", target_is_directory=True)

    result = file_list.func(path=str(workspace), recursive=True)

    assert [line.split()[0] for line in _lines(result)[1:-1]] == ["tokens@"]
    assert "tok.json" not in result
    assert file_list.func(path=str(workspace / "tokens")).startswith(
        "[Error]: Cannot access credential storage"
    )


def test_secret_files_are_marked_without_size_in_a_flat_listing(workspace, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nymeria.tools import filesystem as fs_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "accounts.db").write_bytes(b"sqlite")
    (data_dir / "accounts.db-wal").write_bytes(b"wal")
    (data_dir / "auth_tokens").mkdir()
    (data_dir / "README.md").write_text("map", encoding="utf-8")
    monkeypatch.setattr(fs_module, "get_settings", lambda: SimpleNamespace(data_dir=data_dir))

    result = file_list.func(path=str(data_dir))

    assert _lines(result)[1:-1] == [
        "README.md  3 bytes  " + _lines(result)[1].split("  ", 2)[2],
        "accounts.db  (excluded from the file tools)",
        "accounts.db-wal  (excluded from the file tools)",
        "auth_tokens/  (excluded from the file tools)",
    ]
    assert "sqlite" not in result


def test_permission_denied_on_the_root_is_an_error(workspace):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("permission bits are not enforced here")
    locked = workspace / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        result = file_list.func(path=str(locked))
    finally:
        locked.chmod(0o755)
    assert result.startswith(f"[Error]: Permission denied: {locked}")


def test_a_subdirectory_vanishing_mid_walk_is_counted_not_fatal(workspace, monkeypatch):
    _populate(workspace)
    real_scandir = os.scandir

    def flaky(path):
        if str(path).endswith(os.sep + "nested"):
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky)

    result = file_list.func(path=str(workspace), recursive=True)

    assert "images/nested/" in result
    assert "deep.txt" not in result
    assert "notes.txt" in result
    assert "[1 directory could not be read]" in result


def test_recursive_truncation_stops_descending(workspace, monkeypatch):
    for i in range(3):
        d = workspace / f"d{i}"
        d.mkdir()
        (d / "f.txt").write_text("x", encoding="utf-8")
    real_scandir = os.scandir
    opened: list[str] = []

    def counting(path):
        opened.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting)

    result = file_list.func(path=str(workspace), recursive=True, max_entries=2)

    assert [line.split()[0] for line in _lines(result)[1:-1]] == ["d0/", "d0/f.txt"]
    assert "[Truncated at 2 entries" in result
    # The root and d0 were opened; d1 and d2 never were.
    assert opened == [str(workspace), str(workspace / "d0")]


def test_scan_budget_bounds_a_walk_under_a_non_matching_glob(workspace, monkeypatch):
    from nymeria.tools import filesystem as fs_module

    for i in range(30):
        (workspace / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(fs_module, "_FILE_LIST_SCAN_BUDGET", 10)

    result = file_list.func(path=str(workspace), recursive=True, glob="*.zzz")

    assert "[No entries match '*.zzz']" in result
    assert "[Scan stopped after visiting 10 entries" in result


def test_depth_cap_stops_descending(workspace, monkeypatch):
    from nymeria.tools import filesystem as fs_module

    deep = workspace / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "leaf.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(fs_module, "_FILE_LIST_MAX_DEPTH", 2)

    result = file_list.func(path=str(workspace), recursive=True)

    names = [line.split()[0] for line in _lines(result)[1:-1]]
    assert names == ["a/", "a/b/", "a/b/c/"]
    assert "leaf.txt" not in result


@pytest.mark.skipif(os.name == "nt", reason="POSIX filename bytes")
def test_undecodable_filename_is_escaped_and_utf8_safe(workspace):
    os.makedirs(workspace, exist_ok=True)
    raw = os.fsencode(str(workspace)) + b"/bad\xffname.txt"
    with open(raw, "wb") as fh:
        fh.write(b"x")

    result = file_list.func(path=str(workspace))

    result.encode("utf-8")
    assert "bad\\xffname.txt  1 bytes" in result


def test_file_list_is_a_safe_seed_tool():
    from nymeria.tools import SEED_TOOLS
    from nymeria.tools.metadata import get_tool_metadata

    assert [t.name for t in SEED_TOOLS][:5] == [
        "bash_execute", "file_read", "file_write", "file_edit", "file_list",
    ]
    meta = get_tool_metadata("file_list")
    assert meta is not None
    assert meta.security_level.value == "safe"
