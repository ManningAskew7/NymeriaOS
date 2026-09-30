"""#101 entry 23b: a running backend says which code it booted from, and
whether newer code has landed on disk since (``nymeria/_provenance.py``)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from nymeria import _provenance as prov

SHA_A = "a" * 40
SHA_B = "b" * 40


def _package(root: Path, *, files: int = 3) -> Path:
    """A ``<root>/Nymeria/nymeria`` package with a few source files, a
    prompt read at boot, and the ``run.py`` entry point beside it."""
    pkg = root / "Nymeria" / "nymeria"
    (pkg / "core").mkdir(parents=True)
    for i in range(files):
        (pkg / "core" / f"mod{i}.py").write_text(f"X = {i}\n")
    (pkg / "__init__.py").write_text("")
    (pkg / "config").mkdir()
    (pkg / "config" / "soul.md").write_text("You are Nymeria.\n")
    (root / "Nymeria" / "run.py").write_text("def main(): ...\n")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "mod0.cpython-312.pyc").write_bytes(b"\0")
    return pkg


def _git(root: Path, head: str, refs: dict[str, str] | None = None, packed: str = "") -> Path:
    git = root / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text(head + "\n")
    for ref, sha in (refs or {}).items():
        (git / ref).parent.mkdir(parents=True, exist_ok=True)
        (git / ref).write_text(sha + "\n")
    if packed:
        (git / "packed-refs").write_text(packed)
    return git


# -- reading the commit without git -------------------------------------------


def test_a_branch_head_resolves_through_its_loose_ref(tmp_path):
    pkg = _package(tmp_path)
    _git(tmp_path, "ref: refs/heads/main", {"refs/heads/main": SHA_A})
    assert prov.checkout_commit(pkg) == SHA_A


def test_a_branch_head_resolves_through_packed_refs(tmp_path):
    pkg = _package(tmp_path)
    _git(
        tmp_path,
        "ref: refs/heads/main",
        packed=(
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{SHA_B} refs/heads/other\n"
            f"{SHA_A} refs/heads/main\n"
            f"^{SHA_B}\n"
        ),
    )
    assert prov.checkout_commit(pkg) == SHA_A


def test_a_detached_head_is_its_own_commit(tmp_path):
    pkg = _package(tmp_path)
    _git(tmp_path, SHA_B)
    assert prov.checkout_commit(pkg) == SHA_B


def test_a_sha256_repository_reads_like_a_sha1_one(tmp_path):
    sha256 = "c" * 64
    pkg = _package(tmp_path)
    _git(tmp_path, "ref: refs/heads/main", {"refs/heads/main": sha256})
    assert prov.checkout_commit(pkg) == sha256


def test_a_linked_worktree_reads_its_own_head_and_the_shared_refs(tmp_path):
    main = tmp_path / "main"
    git = _git(main, "ref: refs/heads/main", {"refs/heads/main": SHA_A,
                                              "refs/heads/feature": SHA_B})
    wt_git = git / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feature\n")
    (wt_git / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    pkg = _package(wt)
    (wt / ".git").write_text(f"gitdir: {wt_git}\n")
    assert prov.checkout_commit(pkg) == SHA_B
    # git writes a relative gitdir too (worktree.useRelativePaths).
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")
    assert prov.checkout_commit(pkg) == SHA_B


@pytest.mark.parametrize(
    "head, refs",
    [
        ("ref: refs/heads/main", {}),  # dangling ref
        ("ref: refs/heads/main", {"refs/heads/main": "not-a-sha"}),
        ("garbage", {}),
        ("", {}),
    ],
)
def test_an_unreadable_head_is_none(tmp_path, head, refs):
    pkg = _package(tmp_path)
    _git(tmp_path, head, refs)
    assert prov.checkout_commit(pkg) is None


def test_a_git_dir_above_the_repo_layout_is_not_borrowed(tmp_path):
    # <outer>/.git is the great-grandparent of the package: some unrelated
    # repository (a dotfiles repo in the home dir), not this checkout.
    _git(tmp_path, SHA_A)
    pkg = _package(tmp_path / "unpacked")
    assert prov.checkout_commit(pkg) is None


def test_an_installed_package_never_reports_a_commit(tmp_path):
    site = tmp_path / "lib" / "site-packages"
    pkg = site / "nymeria"
    pkg.mkdir(parents=True)
    _git(site, SHA_A)
    assert prov.checkout_commit(pkg) is None


def test_the_reader_agrees_with_git_on_this_checkout():
    probe = subprocess.run(
        ["git", "-C", str(Path(prov.__file__).parent), "rev-parse", "HEAD"],
        capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")},
    )
    if probe.returncode != 0:
        pytest.skip("not running from a git checkout")
    assert prov.checkout_commit() == probe.stdout.strip()


# -- drift: is newer code on disk? --------------------------------------------


def _touch(path: Path, delta_ns: int) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + delta_ns))


def test_an_untouched_tree_has_no_drift(tmp_path):
    pkg = _package(tmp_path)
    _git(tmp_path, "ref: refs/heads/main", {"refs/heads/main": SHA_A})
    record = prov.capture(pkg)
    assert record.commit == SHA_A
    assert prov.drift(record, pkg) is None


def _same_mtime_new_size(path: Path) -> None:
    st = path.stat()
    path.write_text(path.read_text() + "# grown\n")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


@pytest.mark.parametrize(
    "change",
    [
        lambda pkg: _touch(pkg / "core" / "mod1.py", 5_000_000_000),  # edited
        # uv links an upgrade's files from its cache: mtimes can go BACKWARDS.
        lambda pkg: _touch(pkg / "core" / "mod1.py", -5_000_000_000),
        lambda pkg: _same_mtime_new_size(pkg / "core" / "mod1.py"),
        lambda pkg: (pkg / "core" / "new.py").write_text("Y = 1\n"),
        lambda pkg: (pkg / "core" / "mod2.py").unlink(),
        # Read once at boot, like code: the soul prompt and the entry point.
        lambda pkg: _touch(pkg / "config" / "soul.md", 1_000_000_000),
        lambda pkg: _touch(pkg.parent / "run.py", 1_000_000_000),
    ],
    ids=["edited", "mtime-backwards", "same-mtime-new-size", "added", "removed",
         "prompt", "entry-point"],
)
def test_changed_source_files_are_drift(tmp_path, change):
    pkg = _package(tmp_path)
    record = prov.capture(pkg)
    change(pkg)
    assert prov.drift(record, pkg) == "source files changed on disk since this process started"


def test_bytecode_is_not_drift(tmp_path):
    # The interpreter writes it on import; it is never newer code.
    pkg = _package(tmp_path)
    record = prov.capture(pkg)
    (pkg / "__pycache__" / "mod1.cpython-312.pyc").write_bytes(b"\0")
    (pkg / "core" / "__pycache__").mkdir()
    (pkg / "core" / "__pycache__" / "mod0.cpython-312.pyc").write_bytes(b"\0")
    assert prov.drift(record, pkg) is None


def test_a_package_gone_from_disk_is_drift(tmp_path):
    pkg = _package(tmp_path)
    record = prov.capture(pkg)
    (pkg.parent / "run.py").unlink()
    pkg.rename(tmp_path / "moved-away")
    assert prov.drift(record, pkg) == (
        "the package's files are gone from disk since this process started"
    )


def test_without_a_fingerprint_a_moved_checkout_still_reads_as_drift(tmp_path):
    # The boot walk failed: the commit alone is the only evidence left.
    pkg = _package(tmp_path)
    _git(tmp_path, "ref: refs/heads/main", {"refs/heads/main": SHA_A})
    record = prov.capture(pkg)
    record = prov.BootRecord(**{**record.__dict__, "fingerprint": None})
    assert prov.drift(record, pkg) is None
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text(SHA_B + "\n")
    assert prov.drift(record, pkg) == (
        "the checkout moved to bbbbbbbb since this process started"
    )


def test_a_pull_that_changed_the_code_names_the_new_commit(tmp_path):
    pkg = _package(tmp_path)
    _git(tmp_path, "ref: refs/heads/main", {"refs/heads/main": SHA_A})
    record = prov.capture(pkg)
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text(SHA_B + "\n")
    _touch(pkg / "core" / "mod0.py", 1_000_000_000)
    assert prov.drift(record, pkg) == (
        "the checkout moved to bbbbbbbb since this process started"
    )


def test_a_commit_that_did_not_change_the_code_is_not_drift(tmp_path):
    # Edit, restart, then commit: the process already runs what HEAD holds.
    pkg = _package(tmp_path)
    _git(tmp_path, "ref: refs/heads/main", {"refs/heads/main": SHA_A})
    record = prov.capture(pkg)
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text(SHA_B + "\n")
    assert prov.drift(record, pkg) is None


def test_an_upgraded_install_names_the_version_now_on_disk(tmp_path, monkeypatch):
    pkg = tmp_path / "venv" / "lib" / "site-packages" / "nymeria"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    installed = iter(["0.2.0b6", "0.2.0b7"])
    monkeypatch.setattr(prov, "installed_dist_version", lambda: next(installed))
    record = prov.capture(pkg)
    assert record.installed and record.dist_version == "0.2.0b6"
    _touch(pkg / "__init__.py", 1_000_000_000)
    assert prov.drift(record, pkg) == "NymeriaOS 0.2.0b7 is installed; this process runs 0.2.0b6"


def test_drift_never_raises(tmp_path, monkeypatch):
    pkg = _package(tmp_path)
    record = prov.capture(pkg)

    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(prov, "source_fingerprint", boom)
    assert prov.drift(record, pkg) is None


# -- what an operator sees ----------------------------------------------------


@pytest.mark.parametrize(
    "seconds, text",
    [(0, "under a minute"), (59, "under a minute"), (60, "1m"), (3599, "59m"),
     (3600, "1h 0m"), (7980, "2h 13m"), (86400 + 4 * 3600 + 59, "1d 4h")],
)
def test_uptime_reads_naturally(seconds, text):
    assert prov.format_uptime(seconds) == text


def _record(**overrides) -> prov.BootRecord:
    fields = dict(version="0.2.0-beta.6", started_at=1_790_000_000.0, commit=SHA_A,
                  installed=False, dist_version=None, fingerprint=None,
                  started_mono=500.0)
    fields.update(overrides)
    return prov.BootRecord(**fields)


def test_status_lines_name_version_commit_start_and_uptime():
    # Uptime runs on the monotonic clock, not the wall clock.
    lines = prov.status_lines(_record(), now_mono=500.0 + 7980)
    assert lines == ["0.2.0-beta.6 @ aaaaaaaa | started 2026-09-21 14:13 UTC (up 2h 13m)"]


def test_status_lines_add_the_restart_hint_only_on_drift():
    lines = prov.status_lines(_record(commit=None), now_mono=600.0,
                              drift_reason="source files changed on disk since this process started")
    assert lines == [
        "0.2.0-beta.6 | started 2026-09-21 14:13 UTC (up 1m)",
        "restart to load newer code: source files changed on disk since this process started",
    ]


@pytest.mark.parametrize(
    "record, origin",
    [
        (_record(), "checkout commit aaaaaaaa"),
        (_record(commit=None, installed=True), "an installed package"),
        (_record(commit=None), "a source tree with no git metadata"),
    ],
)
def test_the_boot_log_line_names_where_the_code_came_from(record, origin):
    assert prov.boot_log_line(record) == f"Booted NymeriaOS 0.2.0-beta.6 from {origin}"
