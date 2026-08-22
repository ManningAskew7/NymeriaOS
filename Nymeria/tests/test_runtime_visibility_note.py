"""#234: a not-found error says when the path is outside this runtime's view.

The Docker agent, handed a host path, searched everywhere and said "does not
exist"; the file existed on the host, unmounted. "No such file" and "that
path is outside the filesystem this runtime can see" are different facts, and
only the second tells the operator the next step, so the not-found errors
teach it. The slim shape shares the host filesystem, so there a miss stays a
plain miss, and a RELATIVE request resolves inside this runtime's own tree by
definition, so it never gets the note either.
"""

from __future__ import annotations

from types import SimpleNamespace

import nymeria.tools.filesystem as fs_mod
from nymeria.tools.file_edit import file_edit
from nymeria.tools.filesystem import file_read, file_write, runtime_visibility_note


def _containerized(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(
        fs_mod,
        "detect_execution_environment",
        lambda: SimpleNamespace(in_container=value),
    )


def test_a_deep_missing_host_path_names_the_boundary(monkeypatch, tmp_path):
    _containerized(monkeypatch, True)
    missing = tmp_path / "NymeriaOS" / "tmp" / "brief.md"
    content, _ = file_read.func(str(missing))
    assert "File not found" in content
    assert f"only '{tmp_path}' exists here" in content
    assert "inside a container" in content
    assert "Paste the file's content" in content, "the error must teach the next step"


def test_a_missing_filename_with_a_live_parent_stays_a_plain_miss(monkeypatch, tmp_path):
    # The parent directory exists, so this is an ordinary wrong filename:
    # blaming the container boundary here would misdirect every typo.
    _containerized(monkeypatch, True)
    content, _ = file_read.func(str(tmp_path / "nope.md"))
    assert "File not found" in content
    assert "inside a container" not in content


def test_a_non_container_runtime_keeps_the_plain_error(monkeypatch, tmp_path):
    # Slim shares the host filesystem: the boundary story would be false there.
    _containerized(monkeypatch, False)
    missing = tmp_path / "a" / "b" / "c.md"
    content, _ = file_read.func(str(missing))
    assert "File not found" in content
    assert "inside a container" not in content


def test_a_relative_request_never_gets_the_note(monkeypatch, tmp_path):
    # The gate is the CALLER's raw path, judged before resolution: every call
    # site resolves to an absolute Path, so gating on the resolved path would
    # be inert (review catch), and a relative request resolves inside this
    # runtime's own tree, where the boundary story is false.
    _containerized(monkeypatch, True)
    monkeypatch.chdir(tmp_path)
    content, _ = file_read.func("no_such_dir/deeper/file.md")
    assert "File not found" in content
    assert "inside a container" not in content
    # Helper-level pin of the same rule.
    assert runtime_visibility_note(tmp_path / "a" / "b" / "c.md", "a/b/c.md") == ""


def test_file_edit_not_found_carries_the_same_note(monkeypatch, tmp_path):
    _containerized(monkeypatch, True)
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    missing = tmp_path / "gone" / "sub" / "doc.md"
    out = file_edit.func(
        str(missing), edits=[{"operation": "replace", "target": "a", "replacement": "b"}]
    )
    assert "File not found" in out
    assert "inside a container" in out


def test_file_write_single_missing_directory_stays_a_plain_miss(monkeypatch, tmp_path):
    # For a WRITE the file itself is expected to be missing and one missing
    # directory is an ordinary "create it" case (review catch: counting from
    # the file path made a single missing dir look like a boundary problem).
    _containerized(monkeypatch, True)
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    out = file_write.func(
        str(tmp_path / "newdir" / "f.md"), content="x", create_directories=False
    )
    assert "Directory does not exist" in out
    assert "inside a container" not in out


def test_file_write_deep_missing_directories_name_the_boundary(monkeypatch, tmp_path):
    _containerized(monkeypatch, True)
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    out = file_write.func(
        str(tmp_path / "gone" / "sub" / "f.md"), content="x", create_directories=False
    )
    assert "Directory does not exist" in out
    assert "inside a container" in out
