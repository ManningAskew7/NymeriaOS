"""Tests for auto-installing the local-rag extra during `nymeria init`.

Covers the resolution helpers in ``setup/local_rag_install`` (which install
command for which install shape) and the ``finalize._maybe_install_local_rag``
flow (Docker hint, unattended skip, confirm + install, decline, failure). No real
install runs: the subprocess and the importability probe are stubbed.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from nymeria.setup import finalize as finalize_mod
from nymeria.setup import local_rag_install as lri
from nymeria.setup.rag_catalog import apply_quickstart_rag, rag_env_for_state
from nymeria.setup.state import WizardState


# --- requires_local_rag -----------------------------------------------------


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"EMBEDDING_PROVIDER": "local"}, True),
        ({"RAG_RERANK_PROVIDER": "local"}, True),
        ({"EMBEDDING_PROVIDER": "local", "RAG_RERANK_PROVIDER": "voyage"}, True),
        ({"EMBEDDING_PROVIDER": "cohere", "RAG_RERANK_PROVIDER": "voyage"}, False),
        ({}, False),
    ],
)
def test_requires_local_rag(env, expected):
    assert lri.requires_local_rag(env) is expected


def test_requires_local_rag_matches_quickstart_default():
    # The Ctrl+S / quickstart path equips granite + Ettin: both local, so the
    # extra is required even though the user never explicitly picked local.
    state = WizardState()
    apply_quickstart_rag(state)
    assert lri.requires_local_rag(rag_env_for_state(state)) is True


def test_requires_local_rag_false_for_cloud_pick():
    state = WizardState()
    state.embedder = "premium-cohere"  # cohere embedder, no reranker
    assert lri.requires_local_rag(rag_env_for_state(state)) is False


# --- local_rag_importable ---------------------------------------------------


def test_local_rag_importable_reflects_find_spec(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert lri.local_rag_importable() is True
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert lri.local_rag_importable() is False


# --- build_install_command --------------------------------------------------


def _uv_prefix(tmp_path, receipt_body: str | None):
    """A uv-tool-shaped prefix (path contains uv/tools) with an optional receipt."""
    prefix = tmp_path / "uv" / "tools" / "nymeriaos"
    prefix.mkdir(parents=True)
    if receipt_body is not None:
        (prefix / "uv-receipt.toml").write_text(receipt_body, encoding="utf-8")
    return prefix


def test_build_command_uv_editable(tmp_path, monkeypatch):
    monkeypatch.setattr(lri.shutil, "which", lambda name: "/usr/bin/uv")
    prefix = _uv_prefix(
        tmp_path,
        '[tool]\nrequirements = [{ name = "nymeriaos", editable = "/src/Nymeria" }]\n',
    )
    assert lri.build_install_command(prefix=str(prefix)) == [
        "uv",
        "tool",
        "install",
        "--editable",
        "/src/Nymeria[local-rag]",
    ]


def test_build_command_uv_published(tmp_path, monkeypatch):
    monkeypatch.setattr(lri.shutil, "which", lambda name: "/usr/bin/uv")
    prefix = _uv_prefix(
        tmp_path, '[tool]\nrequirements = [{ name = "nymeriaos" }]\n'
    )
    assert lri.build_install_command(prefix=str(prefix)) == [
        "uv",
        "tool",
        "install",
        "nymeriaos[local-rag]",
    ]


def test_in_process_uv_command_never_forces_a_rebuild(tmp_path, monkeypatch):
    """`--force` makes uv delete and recreate the tool environment, which is the
    environment the wizard itself is running from; without it uv syncs the
    existing environment in place and still records the extra in the receipt
    (verified on uv 0.11: `uv tool upgrade` keeps it, a repeat is a no-op)."""
    monkeypatch.setattr(lri.shutil, "which", lambda name: "/usr/bin/uv")
    for receipt in (
        '[tool]\nrequirements = [{ name = "nymeriaos" }]\n',
        '[tool]\nrequirements = [{ name = "nymeriaos", editable = "/src/Nymeria" }]\n',
    ):
        prefix = tmp_path / "uv" / "tools" / "nymeriaos"
        prefix.mkdir(parents=True, exist_ok=True)
        (prefix / "uv-receipt.toml").write_text(receipt, encoding="utf-8")
        command = lri.build_install_command(prefix=str(prefix))
        assert command is not None
        assert command[:3] == ["uv", "tool", "install"]
        assert "--force" not in command


def test_build_command_uv_published_preserves_custom_index(tmp_path, monkeypatch):
    # install.sh's private-index track records the index under [tool.options];
    # the reinstall must re-emit it or the private package will not resolve.
    monkeypatch.setattr(lri.shutil, "which", lambda name: "/usr/bin/uv")
    prefix = _uv_prefix(
        tmp_path,
        '[tool]\n'
        'requirements = [{ name = "nymeriaos" }]\n'
        '[tool.options]\n'
        'index = [{ url = "https://pypi.example/simple/", default = false }]\n',
    )
    assert lri.build_install_command(prefix=str(prefix)) == [
        "uv",
        "tool",
        "install",
        "--index",
        "https://pypi.example/simple/",
        "nymeriaos[local-rag]",
    ]


def test_build_command_uv_default_index_uses_default_index_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(lri.shutil, "which", lambda name: "/usr/bin/uv")
    prefix = _uv_prefix(
        tmp_path,
        '[tool]\n'
        'requirements = [{ name = "nymeriaos", editable = "/src/Nymeria" }]\n'
        '[tool.options]\n'
        'index = [{ url = "https://pypi.example/simple/", default = true }]\n',
    )
    assert lri.build_install_command(prefix=str(prefix)) == [
        "uv",
        "tool",
        "install",
        "--default-index",
        "https://pypi.example/simple/",
        "--editable",
        "/src/Nymeria[local-rag]",
    ]


def test_build_command_uv_missing_binary_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(lri.shutil, "which", lambda name: None)
    prefix = _uv_prefix(
        tmp_path, '[tool]\nrequirements = [{ name = "nymeriaos" }]\n'
    )
    assert lri.build_install_command(prefix=str(prefix)) is None


def test_build_command_uv_unreadable_receipt_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(lri.shutil, "which", lambda name: "/usr/bin/uv")
    prefix = _uv_prefix(tmp_path, None)  # no receipt file
    assert lri.build_install_command(prefix=str(prefix)) is None


def test_build_command_uv_receipt_without_nymeriaos_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(lri.shutil, "which", lambda name: "/usr/bin/uv")
    prefix = _uv_prefix(
        tmp_path, '[tool]\nrequirements = [{ name = "ruff" }]\n'
    )
    assert lri.build_install_command(prefix=str(prefix)) is None


def test_build_command_pip_venv(tmp_path, monkeypatch):
    # A non-uv prefix with pip adds sentence-transformers to the interpreter
    # directly (works for both editable and wheel pip installs).
    monkeypatch.setattr(lri, "_pip_available", lambda: True)
    assert lri.build_install_command(
        prefix=str(tmp_path / "venv"), executable="/venv/bin/python"
    ) == [
        "/venv/bin/python",
        "-m",
        "pip",
        "install",
        "sentence-transformers>=3.0.0",
    ]


def test_build_command_pip_less_venv_uses_uv(tmp_path, monkeypatch):
    # A uv-managed venv (uv venv / uv sync) ships no pip; uv installs into it.
    monkeypatch.setattr(lri, "_pip_available", lambda: False)
    monkeypatch.setattr(lri.shutil, "which", lambda name: "/usr/bin/uv")
    assert lri.build_install_command(
        prefix=str(tmp_path / "venv"), executable="/venv/bin/python"
    ) == [
        "uv",
        "pip",
        "install",
        "--python",
        "/venv/bin/python",
        "sentence-transformers>=3.0.0",
    ]


def test_build_command_pip_less_no_uv_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(lri, "_pip_available", lambda: False)
    monkeypatch.setattr(lri.shutil, "which", lambda name: None)
    assert (
        lri.build_install_command(
            prefix=str(tmp_path / "venv"), executable="/venv/bin/python"
        )
        is None
    )


# --- manual_install_hint ----------------------------------------------------


def test_manual_hint_quotes_extra_brackets():
    hint = lri.manual_install_hint(
        ["uv", "tool", "install", "--force", "/src/Nymeria[local-rag]"]
    )
    # shlex.join quotes the [local-rag] so a shell does not glob it away.
    assert "[local-rag]" in hint
    assert "'/src/Nymeria[local-rag]'" in hint


def test_manual_hint_uv_tool_shape(tmp_path):
    # A uv tool env cannot be pip-installed into, so the unresolved-command hint
    # must be the uv tool command, not pip.
    prefix = tmp_path / "uv" / "tools" / "nymeriaos"
    assert (
        lri.manual_install_hint(None, prefix=str(prefix))
        == 'uv tool install --force "nymeriaos[local-rag]"'
    )


def test_manual_hint_adds_force_to_the_uv_tool_command():
    # The manual command runs from a fresh shell where nothing is locked, so
    # --force is safe there, and it is what repairs a half-deleted environment
    # left by a failed in-process attempt. Never doubled, never added to pip.
    hint = lri.manual_install_hint(
        ["uv", "tool", "install", "nymeriaos[local-rag]"], windows=False
    )
    assert hint == "uv tool install --force 'nymeriaos[local-rag]'"
    forced = lri.manual_install_hint(
        ["uv", "tool", "install", "--force", "nymeriaos[local-rag]"], windows=False
    )
    assert forced.count("--force") == 1
    pip = lri.manual_install_hint(
        ["/venv/bin/python", "-m", "pip", "install", "sentence-transformers>=3.0.0"],
        windows=False,
    )
    assert "--force" not in pip


def test_manual_hint_quotes_for_cmd_and_powershell_on_windows():
    # shlex.join's single quotes are literal in cmd.exe, so a Windows user
    # pasting them gets a resolution error; double quotes work in both shells.
    hint = lri.manual_install_hint(
        ["uv", "tool", "install", "nymeriaos[local-rag]"], windows=True
    )
    assert hint == 'uv tool install --force "nymeriaos[local-rag]"'
    editable = lri.manual_install_hint(
        ["uv", "tool", "install", "--editable", "C:\\src\\Nymeria[local-rag]"],
        windows=True,
    )
    assert editable == 'uv tool install --force --editable "C:\\src\\Nymeria[local-rag]"'
    assert "'" not in hint and "'" not in editable


def test_manual_hint_quotes_version_pins_and_spaced_paths_on_windows():
    # `>` is a redirection to both shells and a bare space splits the path.
    pip = lri.manual_install_hint(
        ["C:\\venv\\Scripts\\python.exe", "-m", "pip", "install", "sentence-transformers>=3.0.0"],
        windows=True,
    )
    assert pip == 'C:\\venv\\Scripts\\python.exe -m pip install "sentence-transformers>=3.0.0"'
    uv_pip = lri.manual_install_hint(
        ["uv", "pip", "install", "--python", "C:\\My Tools\\python.exe", "sentence-transformers>=3.0.0"],
        windows=True,
    )
    assert '"C:\\My Tools\\python.exe"' in uv_pip
    assert '"sentence-transformers>=3.0.0"' in uv_pip
    assert "--force" not in uv_pip


def test_build_command_keeps_the_receipts_existing_extras(tmp_path, monkeypatch):
    """uv syncs the environment exactly to the new requirement and rewrites the
    receipt, so a `nymeriaos[discord]` install reinstalled as plain
    `nymeriaos[local-rag]` would lose discord.py. Existing extras ride along;
    one already present is not duplicated."""
    monkeypatch.setattr(lri.shutil, "which", lambda name: "/usr/bin/uv")
    prefix = _uv_prefix(
        tmp_path, '[tool]\nrequirements = [{ name = "nymeriaos", extras = ["discord"] }]\n'
    )
    assert lri.build_install_command(prefix=str(prefix))[-1] == "nymeriaos[discord,local-rag]"
    (prefix / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "nymeriaos", editable = "/src/Nymeria", extras = ["local-rag"] }]\n',
        encoding="utf-8",
    )
    assert lri.build_install_command(prefix=str(prefix))[-2:] == [
        "--editable",
        "/src/Nymeria[local-rag]",
    ]


def test_extra_install_hint_shares_the_receipt_and_platform_quoting(tmp_path):
    # The voice-local hint must not drop a local-rag extra already installed,
    # and must quote for the platform like the local-rag hint does.
    prefix = _uv_prefix(
        tmp_path, '[tool]\nrequirements = [{ name = "nymeriaos", extras = ["local-rag"] }]\n'
    )
    assert (
        lri.extra_install_hint("voice-local", prefix=str(prefix), windows=False)
        == "uv tool install --force 'nymeriaos[local-rag,voice-local]'"
    )
    assert (
        lri.extra_install_hint("voice-local", prefix=str(prefix), windows=True)
        == 'uv tool install --force "nymeriaos[local-rag,voice-local]"'
    )
    # No receipt: the plain published shape; a venv: pip.
    bare = tmp_path / "uv" / "tools" / "other"
    bare.mkdir(parents=True)
    assert (
        lri.extra_install_hint("voice-local", prefix=str(bare))
        == 'uv tool install --force "nymeriaos[voice-local]"'
    )
    assert (
        lri.extra_install_hint("voice-local", prefix=str(tmp_path / "venv"))
        == 'pip install "nymeriaos[voice-local]"'
    )


# --- in_process_install_blocked ---------------------------------------------


@pytest.mark.parametrize(
    "platform,uv_tool,blocked",
    [
        ("win32", True, True),
        ("win32", False, False),
        ("linux", True, False),
        ("darwin", True, False),
    ],
)
def test_in_process_install_blocked_only_for_a_windows_uv_tool(
    tmp_path, platform, uv_tool, blocked
):
    """Windows keeps the running nymeria's launcher and loaded extensions locked
    and uv rebuilds (or, on the in-place path, deletes on failure) the tool
    environment they live in, so the wizard must never run the install from
    inside itself there. POSIX in-place syncs are safe; a plain venv install
    adds a dependency without touching nymeriaos at all."""
    prefix = tmp_path / ("uv/tools/nymeriaos" if uv_tool else "venv")
    prefix.mkdir(parents=True)
    reason = lri.in_process_install_blocked(prefix=str(prefix), platform=platform)
    assert (reason is not None) is blocked
    if reason is not None:
        assert "Windows" in reason


# --- importability sees packages installed after startup --------------------


def test_module_importable_sees_a_package_installed_after_startup(tmp_path, monkeypatch):
    """The capability summary runs in the same interpreter that just spawned the
    installer; FileFinder caches a directory listing, so without invalidating
    the import caches a freshly installed extra still reads as missing."""
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.syspath_prepend(str(site))
    name = "nymeria_test_fake_extra_pkg"
    assert lri._module_importable(name) is False
    cached_mtime = site.stat().st_mtime
    (site / name).mkdir()
    (site / name / "__init__.py").write_text("", encoding="utf-8")
    # FileFinder re-lists a directory only when its mtime moved; pin it back so
    # the fresh package is visible ONLY through importlib.invalidate_caches()
    # (a same-second write or a coarse-timestamp filesystem looks like this).
    os.utime(site, (cached_mtime, cached_mtime))
    assert lri._module_importable(name) is True


def test_manual_hint_pip_shape_for_plain_venv(tmp_path):
    assert (
        lri.manual_install_hint(None, prefix=str(tmp_path / "venv"))
        == 'pip install "nymeriaos[local-rag]"'
    )


def test_sentence_transformers_pin_matches_pyproject():
    # Guard against the module's pin drifting from pyproject's local-rag extra.
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    extra = data["project"]["optional-dependencies"]["local-rag"]
    # Exact single-element match: if the extra ever grows (e.g. adds
    # optimum[onnxruntime]), the bare-pin pip path would under-install, so fail
    # loudly here to force a revisit of build_install_command's pip branch.
    assert extra == [lri.SENTENCE_TRANSFORMERS_REQUIREMENT]


# --- finalize._maybe_install_local_rag --------------------------------------


class _FakeConsole:
    def __init__(self, answer: str = "", input_exc: BaseException | None = None):
        self.printed: list[str] = []
        self._answer = answer
        self._input_exc = input_exc
        self.input_prompts: list[str] = []

    def print(self, *args, **kwargs):
        self.printed.append(" ".join(str(a) for a in args))

    def input(self, prompt: str = "") -> str:
        self.input_prompts.append(prompt)
        if self._input_exc is not None:
            raise self._input_exc
        return self._answer

    @property
    def text(self) -> str:
        return "\n".join(self.printed)


class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode


@pytest.fixture
def force_missing(monkeypatch):
    """Pretend the extra is not yet installed for the install-flow tests."""
    monkeypatch.setattr(lri, "local_rag_importable", lambda: False)


@pytest.fixture
def tty(monkeypatch):
    """Pretend stdin is an interactive terminal so the confirm prompt is reached
    (pytest's captured stdin reports isatty() False, which would skip it)."""
    monkeypatch.setattr(
        sys, "stdin", type("_Tty", (), {"isatty": lambda self: True})()
    )


def _run(console, **kw):
    finalize_mod._maybe_install_local_rag(
        kw.pop("env", {"EMBEDDING_PROVIDER": "local"}),
        console,
        for_docker=kw.pop("for_docker", False),
        non_interactive=kw.pop("non_interactive", False),
    )


def test_noop_when_stack_not_local(monkeypatch):
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole()
    _run(console, env={"EMBEDDING_PROVIDER": "cohere"})
    assert console.printed == []
    assert calls == []


def test_noop_when_already_installed(monkeypatch):
    monkeypatch.setattr(lri, "local_rag_importable", lambda: True)
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole()
    _run(console)
    assert console.printed == []
    assert calls == []


def test_docker_prints_hint_only(monkeypatch, force_missing):
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole()
    _run(console, for_docker=True)
    assert calls == []
    assert "image" in console.text.lower()


def test_non_interactive_skips_with_hint(monkeypatch, force_missing):
    monkeypatch.setattr(lri, "build_install_command", lambda **k: ["uv", "x"])
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole()
    _run(console, non_interactive=True)
    assert calls == []
    assert console.input_prompts == []  # never prompts unattended
    assert "install it with" in console.text.lower()


def test_no_tty_skips_with_hint(monkeypatch, force_missing):
    # non_interactive=False but stdin is not a tty (the default pytest harness):
    # the prompt cannot be answered, so it surfaces the command and never blocks.
    monkeypatch.setattr(lri, "build_install_command", lambda **k: ["uv", "x"])
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole()
    _run(console)
    assert calls == []
    assert console.input_prompts == []
    assert "install it with" in console.text.lower()


def test_unknown_command_prints_manual_hint(monkeypatch, force_missing):
    monkeypatch.setattr(lri, "build_install_command", lambda **k: None)
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole()
    _run(console)
    assert calls == []
    assert console.input_prompts == []
    assert "local-rag" in console.text


def test_windows_uv_tool_defers_the_install_with_the_manual_command(
    monkeypatch, force_missing, tty
):
    """On a Windows uv tool install the wizard neither prompts nor spawns uv: it
    says why, and names the exact command to run after setup with Nymeria not
    running. Setup itself carries on (the function returns normally)."""
    monkeypatch.setattr(
        lri, "build_install_command", lambda **k: ["uv", "tool", "install", "nymeriaos[local-rag]"]
    )
    monkeypatch.setattr(
        lri, "in_process_install_blocked", lambda **k: "Windows keeps the files locked."
    )
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole(answer="")
    _run(console)
    assert calls == []
    assert console.input_prompts == []
    assert "Windows keeps the files locked." in console.text
    assert "not running" in console.text
    assert "uv tool install --force" in console.text
    assert "[local-rag]" in console.text


def test_accept_default_yes_runs_install(monkeypatch, force_missing, tty):
    cmd = ["uv", "tool", "install", "nymeriaos[local-rag]"]
    monkeypatch.setattr(lri, "build_install_command", lambda **k: cmd)
    captured = {}

    def fake_run(command, *a, **k):
        captured["command"] = command
        return _FakeProc(returncode=0)

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    console = _FakeConsole(answer="")  # bare Enter == yes
    _run(console)
    assert captured["command"] == cmd
    assert "installed" in console.text.lower()


def test_decline_skips_install(monkeypatch, force_missing, tty):
    monkeypatch.setattr(lri, "build_install_command", lambda **k: ["uv", "x"])
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole(answer="n")
    _run(console)
    assert calls == []
    assert "skipped" in console.text.lower()


def test_decline_noish_answer_skips_install(monkeypatch, force_missing, tty):
    # nope / nah (anything starting with n) declines, not just exact n/no.
    monkeypatch.setattr(lri, "build_install_command", lambda **k: ["uv", "x"])
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole(answer="nope")
    _run(console)
    assert calls == []
    assert "skipped" in console.text.lower()


def test_eof_at_prompt_declines_without_crash(monkeypatch, force_missing, tty):
    # Ctrl+D / Ctrl+C at the prompt must not crash a finished init.
    monkeypatch.setattr(lri, "build_install_command", lambda **k: ["uv", "x"])
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole(input_exc=EOFError())
    _run(console)  # must not raise
    assert calls == []
    assert "skipped" in console.text.lower()


def test_nonzero_exit_reports_failure(monkeypatch, force_missing, tty):
    monkeypatch.setattr(lri, "build_install_command", lambda **k: ["uv", "x"])
    monkeypatch.setattr(
        finalize_mod.subprocess, "run", lambda *a, **k: _FakeProc(returncode=1)
    )
    console = _FakeConsole(answer="")
    _run(console)
    assert "did not finish cleanly" in console.text


def test_oserror_reports_failure(monkeypatch, force_missing, tty):
    monkeypatch.setattr(lri, "build_install_command", lambda **k: ["uv", "x"])

    def boom(*a, **k):
        raise OSError("no uv")

    monkeypatch.setattr(finalize_mod.subprocess, "run", boom)
    console = _FakeConsole(answer="")
    _run(console)
    assert "could not run the installer" in console.text.lower()


def test_docker_full_stack_hint_names_the_build_flag(monkeypatch, force_missing):
    """The full stack has an image hook (the NYMERIA_LOCAL_RAG build arg), so its
    hint names the flag write_config already wrote and the `--build` an existing
    image needs, instead of the generic 'build an image' note."""
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole()
    finalize_mod._maybe_install_local_rag(
        {"EMBEDDING_PROVIDER": "local"},
        console,
        for_docker=True,
        full_stack=True,
        non_interactive=False,
    )
    assert calls == []
    assert f"{lri.DOCKER_LOCAL_RAG_ENV}=1" in console.text
    assert "--build" in console.text


# --- #422: one command that closes NymeriaOS around the install -------------


def test_a_relocated_uv_tool_dir_is_still_a_uv_tool_install(tmp_path, monkeypatch):
    # UV_TOOL_DIR=D:\tools leaves no `uv/tools` in the path; the receipt does.
    monkeypatch.setattr(lri.shutil, "which", lambda name: "/usr/bin/uv")
    prefix = tmp_path / "D-tools" / "nymeriaos"
    prefix.mkdir(parents=True)
    assert not lri._is_uv_tool_prefix(prefix)
    (prefix / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "nymeriaos" }]\n', encoding="utf-8"
    )
    assert lri._is_uv_tool_prefix(prefix)
    assert lri.build_install_command(prefix=str(prefix)) == [
        "uv", "tool", "install", "nymeriaos[local-rag]",
    ]


def test_the_receipt_target_takes_several_extras_and_keeps_existing_ones(tmp_path):
    prefix = _uv_prefix(
        tmp_path, '[tool]\nrequirements = [{ name = "nymeriaos", extras = ["discord"] }]\n'
    )
    assert lri.uv_tool_install_target(prefix, ["local-rag", "discord", "voice-local"]) == [
        "nymeriaos[discord,local-rag,voice-local]"
    ]


def test_windows_defer_names_the_one_step_command(monkeypatch, force_missing, tty, tmp_path):
    """A Windows uv tool install gets `nymeria upgrade --add-extra local-rag`,
    which closes NymeriaOS itself, with the raw uv line kept as the fallback."""
    prefix = _uv_prefix(tmp_path, '[tool]\nrequirements = [{ name = "nymeriaos" }]\n')
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(lri.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(
        lri, "in_process_install_blocked", lambda **k: "Windows keeps the files locked."
    )
    calls = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    console = _FakeConsole(answer="")
    _run(console)
    assert calls == [] and console.input_prompts == []
    assert "nymeria upgrade --add-extra local-rag" in console.text
    assert "uv tool install --force" in console.text  # the by-hand fallback


def test_the_in_process_install_keeps_uv_settings_and_drops_secrets(
    monkeypatch, force_missing, tty
):
    # Without UV_TOOL_DIR uv would install a second copy in its default dir.
    monkeypatch.setattr(lri, "build_install_command", lambda **k: ["uv", "tool", "install", "x"])
    monkeypatch.setenv("UV_TOOL_DIR", "/opt/uv-tools")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", "vault-key")
    captured = {}

    def fake_run(command, *a, **k):
        captured["env"] = k["env"]
        return _FakeProc(returncode=0)

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    _run(_FakeConsole(answer=""))
    assert captured["env"]["UV_TOOL_DIR"] == "/opt/uv-tools"
    assert captured["env"]["HTTPS_PROXY"] == "http://proxy:3128"
    assert "NYMERIA_SECRETS_KEY" not in captured["env"]


@pytest.mark.parametrize("uv_tool", [True, False])
def test_the_voice_hint_offers_the_one_step_command_on_a_uv_tool_install(
    monkeypatch, tmp_path, uv_tool
):
    import nymeria.core.voice_local as voice_local

    monkeypatch.setattr(voice_local, "local_tts_importable", lambda: False)
    if uv_tool:
        prefix = _uv_prefix(tmp_path, '[tool]\nrequirements = [{ name = "nymeriaos" }]\n')
    else:
        prefix = tmp_path / "venv"
        prefix.mkdir()
    monkeypatch.setattr(sys, "prefix", str(prefix))
    state = WizardState()
    state.extras["tts"] = "kokoro"
    console = _FakeConsole()
    finalize_mod._print_voice_hints(state, console)
    assert ("nymeria upgrade --add-extra voice-local" in console.text) is uv_tool
    assert ("pip install" in console.text) is (not uv_tool)


def test_the_receipt_target_carries_with_packages_pins_and_python(tmp_path):
    # The shape uv 0.11.10 writes for `uv tool install nymeriaos==0.2.0b6
    # --with cowsay==6.1 --with-editable ./plug --python 3.11`: dropping any
    # of it would uninstall the plugin, lift the pin, or forget the Python.
    prefix = _uv_prefix(
        tmp_path,
        "[tool]\n"
        "requirements = [\n"
        '    { name = "nymeriaos", extras = ["discord"], specifier = "==0.2.0b6" },\n'
        '    { name = "cowsay", specifier = "==6.1" },\n'
        '    { name = "plugx", editable = "/src/plug" },\n'
        "]\n"
        'python = "3.11"\n',
    )
    assert lri.uv_tool_install_target(prefix, ["local-rag"]) == [
        "--python", "3.11",
        "--with", "cowsay==6.1",
        "--with-editable", "/src/plug",
        "nymeriaos[discord,local-rag]==0.2.0b6",
    ]


@pytest.mark.parametrize(
    "receipt",
    [
        # A local (non-editable) source: rebuilt by name it would become the index wheel.
        '[tool]\nrequirements = [{ name = "nymeriaos", directory = "/src/Nymeria" }]\n',
        '[tool]\nrequirements = [{ name = "nymeriaos" }, { name = "plug", git = "https://g/p" }]\n',
        '[tool]\nrequirements = [{ name = "nymeriaos" }, { name = "x", marker = "sys_platform == \'win32\'" }]\n',
        '[tool]\nrequirements = [{ name = "nymeriaos" }]\nconstraints = [{ name = "httpx", specifier = "<1" }]\n',
        '[tool]\nrequirements = [{ name = "nymeriaos" }]\n[tool.options]\nprerelease = "allow"\n',
    ],
)
def test_a_receipt_setting_that_cannot_be_carried_refuses_the_rebuild(tmp_path, receipt):
    prefix = _uv_prefix(tmp_path, receipt)
    assert lri.uv_tool_install_target(prefix, ["local-rag"]) is None


def test_add_extra_command_is_offered_only_for_a_uv_tool_install(tmp_path):
    relocated = tmp_path / "D-tools" / "nymeriaos"
    relocated.mkdir(parents=True)
    (relocated / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "nymeriaos" }]\n', encoding="utf-8"
    )
    assert lri.add_extra_command("local-rag", prefix=str(relocated)) == "nymeria upgrade --add-extra local-rag"
    assert lri.add_extra_command("local-rag", prefix=str(tmp_path / "venv")) is None
