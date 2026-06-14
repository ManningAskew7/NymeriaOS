"""Tests for auto-installing the local-rag extra during `nymeria init`.

Covers the resolution helpers in ``setup/local_rag_install`` (which install
command for which install shape) and the ``finalize._maybe_install_local_rag``
flow (Docker hint, unattended skip, confirm + install, decline, failure). No real
install runs: the subprocess and the importability probe are stubbed.
"""

from __future__ import annotations

import importlib.util
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
        "--force",
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
        "--force",
        "nymeriaos[local-rag]",
    ]


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
        "--force",
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
        "--force",
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


def test_accept_default_yes_runs_install(monkeypatch, force_missing, tty):
    cmd = ["uv", "tool", "install", "--force", "nymeriaos[local-rag]"]
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
