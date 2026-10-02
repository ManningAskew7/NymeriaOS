"""Regression tests for run.py command dispatch and bot-runner helpers.

Covers the COMMANDS registry (single source of truth for dispatch + config
validation) and the shared bot-runner helpers extracted from the eight
near-identical bot launchers.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

import run


# ---------------------------------------------------------------------------
# COMMANDS registry (F2)
# ---------------------------------------------------------------------------


def _subcommand_names(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def test_every_subparser_resolves_to_a_command():
    """No registered subcommand may exist without a COMMANDS entry, and vice
    versa, so dispatch and the parser can never drift apart."""
    names = _subcommand_names(run.build_parser())
    assert names, "build_parser exposed no subcommands"
    assert names == set(run.COMMANDS), names ^ set(run.COMMANDS)


def test_full_validation_set_matches_server_and_bot_commands():
    """The validate_config gate set is derived from the table; lock its members
    so a future edit cannot silently add or drop config validation."""
    assert run._FULL_VALIDATION_COMMANDS == frozenset(
        {
            "api",
            "slim",
            "mcp",
            "worker",
            "discord-bot",
            "telegram-bot",
            "slack-bot",
            "twitch-bot",
        }
    )
    # The conditionally-validated commands are deliberately NOT in the set.
    for name in ("cli", "service", "users", "snapshot", "upgrade"):
        assert name not in run._FULL_VALIDATION_COMMANDS


def test_thin_client_commands_are_the_relays():
    """Exactly the commands that relay to the API skip the server-dependency
    warnings; every agent-running command keeps them."""
    thin = {name for name, command in run.COMMANDS.items() if command.thin_client}
    assert thin == {"mcp", "discord-bot", "telegram-bot", "slack-bot", "twitch-bot"}


@pytest.mark.parametrize(
    "command,thin",
    [("mcp", True), ("twitch-bot", True), ("api", False), ("worker", False)],
)
def test_main_passes_the_thin_client_flag_to_validation(monkeypatch, command, thin):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(run, "validate_config", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(run, "_load_environment", lambda: None)
    monkeypatch.setattr(run, "_require_launch_mode_service_token", lambda *a, **k: None)
    monkeypatch.setattr(run, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(run, run.COMMANDS[command].runner.__name__, lambda args: None)
    monkeypatch.setattr(sys, "argv", ["run.py", command])

    run.main()

    assert seen.get("thin_client", False) is thin


@pytest.mark.parametrize(
    "command,logged",
    [("api", True), ("worker", True), ("telegram-bot", True), ("claude-code-runner", True),
     ("cli", False), ("reembed", False)],
)
def test_server_roles_log_the_code_they_booted_from(monkeypatch, caplog, command, logged):
    """#101 entry 23b: the journal answers "which code is this running"."""
    import contextlib
    import logging

    from nymeria import _provenance

    monkeypatch.setattr(_provenance, "_BOOT", _provenance.BootRecord(
        version="9.9.9", started_at=0.0, commit="c" * 40, installed=False,
        dist_version=None, fingerprint=None,
    ))
    monkeypatch.setattr(run, "validate_config", lambda *a, **k: None)
    monkeypatch.setattr(run, "_load_environment", lambda: None)
    monkeypatch.setattr(run, "_require_launch_mode_service_token", lambda *a, **k: None)
    monkeypatch.setattr(run, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(run, run.COMMANDS[command].runner.__name__, lambda args: None)
    monkeypatch.setattr(sys, "argv", ["run.py", command])
    monkeypatch.setattr(logging.getLogger("nymeria"), "propagate", True)

    with caplog.at_level(logging.INFO, logger="nymeria"), contextlib.suppress(SystemExit):
        run.main()  # exit-returning commands (reembed) end in sys.exit

    booted = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Booted ")]
    assert booted == (["Booted NymeriaOS 9.9.9 from checkout commit cccccccc"] if logged else [])


def test_every_command_takes_its_code_baseline_at_boot(monkeypatch):
    """A quiet role still needs the baseline: the fat CLI (`--transport local`)
    serves /status from its own process, and a baseline taken lazily at the
    first /status would miss every change made before it."""
    from nymeria import _provenance

    monkeypatch.setattr(_provenance, "_BOOT", None)
    monkeypatch.setattr(run, "validate_config", lambda *a, **k: None)
    monkeypatch.setattr(run, "_load_environment", lambda: None)
    monkeypatch.setattr(run, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(run, "run_cli", lambda args: None)
    monkeypatch.setattr(sys, "argv", ["run.py", "cli"])

    run.main()

    assert _provenance._BOOT is not None
    assert _provenance._BOOT.version == _provenance.__version__


@pytest.mark.parametrize("thin_client", [True, False])
def test_validate_config_maps_thin_client_to_a_non_server_validation(monkeypatch, thin_client):
    import nymeria.config

    seen: dict[str, Any] = {}

    class FakeSettings:
        def validate_runtime(self, **kwargs):
            seen.update(kwargs)
            return [], []

    monkeypatch.setattr(nymeria.config, "get_settings", lambda: FakeSettings())

    run.validate_config(thin_client=thin_client)

    assert seen == {"server_process": not thin_client}


def test_exit_returning_commands():
    """Only the commands that return an exit code are marked exits=True."""
    exits = {name for name, command in run.COMMANDS.items() if command.exits}
    assert exits == {"init", "doctor", "reembed", "users", "snapshot", "browser", "upgrade"}


def test_dispatch_resolves_runner_through_module_namespace(monkeypatch):
    """main() must resolve the runner by name at call time, not call the
    reference captured in COMMANDS at import time, so monkeypatching a runner
    (as many tests and the old if/elif seam rely on) still intercepts dispatch.
    Regression guard: capturing the function object in COMMANDS silently
    bypasses the patch and runs the real runner."""
    called: dict[str, object] = {}
    monkeypatch.setattr(run, "run_api", lambda args: called.setdefault("api", args))
    monkeypatch.setattr(run, "validate_config", lambda *a, **k: None)
    # This test is about dispatch, not startup: stub the dotenv load that
    # main() performs since #294, so it does not merge the host's real
    # deployment config mid-test.
    monkeypatch.setattr(run, "_load_environment", lambda: None)
    monkeypatch.setattr(sys, "argv", ["run.py", "api"])

    run.main()

    assert "api" in called, "dispatch did not call the monkeypatched runner"


def test_run_users_delegates_to_users_cli(monkeypatch):
    captured = {}

    def fake_dispatch(args):
        captured["args"] = args
        return 7

    import nymeria.cli.users as users_cli

    monkeypatch.setattr(users_cli, "dispatch", fake_dispatch)
    args = argparse.Namespace(action="list")
    assert run.run_users(args) == 7
    assert captured["args"] is args


# ---------------------------------------------------------------------------
# Bot-runner helpers (F1)
# ---------------------------------------------------------------------------


def test_resolve_api_url_prefers_explicit():
    assert (
        run._resolve_api_url(argparse.Namespace(api_url="http://localhost:9000"))
        == "http://localhost:9000"
    )


@pytest.fixture
def host_process(monkeypatch):
    """A bot launched on the host (not in a container) with no URL configured."""
    from nymeria.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "_in_container", lambda: False)
    monkeypatch.delenv("NYMERIA_API_URL", raising=False)
    monkeypatch.delenv("API_PORT", raising=False)


def test_resolve_api_url_on_the_host_dials_this_installs_own_port(host_process, monkeypatch):
    # #101 entry 11b: `nymeria telegram-bot` on a slim or service install used
    # to dial the Docker-only http://nymeria-api:8000.
    monkeypatch.setenv("API_PORT", "8010")
    assert run._resolve_api_url(argparse.Namespace()) == "http://localhost:8010"
    assert run._resolve_api_url(argparse.Namespace(api_url=None)) == "http://localhost:8010"

    monkeypatch.delenv("API_PORT")
    assert run._resolve_api_url(argparse.Namespace()) == "http://localhost:8000"
    monkeypatch.setenv("API_PORT", "not-a-port")
    assert run._resolve_api_url(argparse.Namespace()) == "http://localhost:8000"


def test_resolve_api_url_honours_nymeria_api_url(host_process, monkeypatch):
    monkeypatch.setenv("API_PORT", "8010")
    monkeypatch.setenv("NYMERIA_API_URL", "http://backend.lan:9000/")
    assert run._resolve_api_url(argparse.Namespace()) == "http://backend.lan:9000"
    # The flag still wins over the environment.
    assert (
        run._resolve_api_url(argparse.Namespace(api_url="http://localhost:9001"))
        == "http://localhost:9001"
    )


def test_resolve_api_url_in_a_container_keeps_the_compose_service(monkeypatch):
    from nymeria.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "_in_container", lambda: True)
    monkeypatch.delenv("NYMERIA_API_URL", raising=False)
    # API_PORT is the HOST side of the mapping; the container side stays 8000.
    monkeypatch.setenv("API_PORT", "8010")
    assert run._resolve_api_url(argparse.Namespace()) == "http://nymeria-api:8000"


def test_add_api_url_arg_default_help_and_parsing():
    parser = argparse.ArgumentParser()
    run._add_api_url_arg(parser)
    assert parser.parse_args([]).api_url is None
    assert parser.parse_args(["--api-url", "http://x:1"]).api_url == "http://x:1"


def test_install_exit_handlers_runs_on_stop_without_hard_exit(monkeypatch, capsys):
    registered: dict[int, Callable[[int, Any], None]] = {}
    monkeypatch.setattr(run.signal, "signal", lambda sig, handler: registered.__setitem__(sig, handler))

    calls: list[str] = []
    run._install_exit_handlers("byebye", on_stop=lambda: calls.append("stopped"), hard_exit=False)

    assert signal.SIGINT in registered and signal.SIGTERM in registered
    # Invoking the handler must print the message and run on_stop, but not exit.
    registered[signal.SIGINT](signal.SIGINT, None)
    assert "byebye" in capsys.readouterr().out
    assert calls == ["stopped"]


def test_install_exit_handlers_hard_exit(monkeypatch):
    registered: dict[int, Callable[[int, Any], None]] = {}
    monkeypatch.setattr(run.signal, "signal", lambda sig, handler: registered.__setitem__(sig, handler))
    exits: list[int] = []
    monkeypatch.setattr(run.os, "_exit", lambda code: exits.append(code))

    run._install_exit_handlers("bye", hard_exit=True)
    registered[signal.SIGTERM](signal.SIGTERM, None)
    assert exits == [0]


# ---------------------------------------------------------------------------
# Import-time environment side effects (#294)
# ---------------------------------------------------------------------------


_IMPORT_PROBE = """
import os, sys

sys.path.insert(0, sys.argv[1])
import run

print("AFTER-IMPORT", os.environ.get("NYMERIA_294_IMPORT_PROBE", "<unset>"))
run._load_environment()
print("AFTER-LOAD", os.environ.get("NYMERIA_294_IMPORT_PROBE", "<unset>"))
"""


def test_importing_run_does_not_load_the_deployment_env(tmp_path):
    """Importing ``run`` must not reconfigure the interpreter (#294).

    ``_load_environment`` merges the deployment dotenv with ``override=True``.
    As an import side effect that reached every importer, including pytest,
    which imports test modules during COLLECTION: the operator's real
    ``.env.docker`` landed in ``os.environ`` before the suite's first test and
    silently overrode 65 Settings fields.

    Asserts both halves, so neither deleting the call nor deleting the loader
    passes: the import is inert, and the loader still works when called.
    """
    (tmp_path / ".env").write_text(
        "NYMERIA_294_IMPORT_PROBE=leaked\n", encoding="utf-8"
    )
    repo_root = Path(run.__file__).resolve().parent

    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE, str(repo_root)],
        capture_output=True,
        text=True,
        timeout=120,
        # Inherit, overriding only NYMERIA_PROJECT_ROOT, which is the one thing
        # this test needs to control: it points run.py's dotenv resolution at
        # the probe root instead of this checkout. A hand-built minimal env
        # would not start a child interpreter on Windows (no SystemRoot), and
        # the probe variable is absent from the parent anyway. Same shape as
        # tests/test_cli_startup_imports.py's subprocess probes.
        env={**os.environ, "NYMERIA_PROJECT_ROOT": str(tmp_path)},
    )

    detail = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr[-3000:]}"
    assert result.returncode == 0, detail
    assert "AFTER-IMPORT <unset>" in result.stdout, detail
    # Guard against a vacuous pass: the loader must actually read that file.
    assert "AFTER-LOAD leaked" in result.stdout, detail


def test_main_loads_the_deployment_env_before_parsing_args(monkeypatch):
    """The deferred load still happens on the real entry path (#294).

    An unparseable argv would make ``main()`` raise ``SystemExit`` from
    ``parse_args``; seeing the loader's sentinel instead proves the load ran,
    and ran before argument parsing (hence before any subcommand).
    """

    class _Loaded(Exception):
        pass

    def _sentinel() -> None:
        raise _Loaded

    monkeypatch.setattr(run, "_load_environment", _sentinel)
    monkeypatch.setattr(sys, "argv", ["run.py", "--not-a-real-flag"])

    with pytest.raises(_Loaded):
        run.main()


# ---------------------------------------------------------------------------
# One root per process, chosen before any env file loads (#101 entry 6, #451)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["init", "--root", "/r/b"], (None, "/r/b")),
        (["init", "--root=/r/b"], (None, "/r/b")),
        (["--root", "/r/g", "init"], ("/r/g", None)),
        (["--root=/r/g", "doctor"], ("/r/g", None)),
        (["service", "install", "--root", "/r/s"], (None, "/r/s")),
        (["service", "--root", "/r/s", "install"], (None, "/r/s")),
        (["browser", "configure", "--root", "/r/c", "--token-stdin"], (None, "/r/c")),
        (["-l", "DEBUG", "--root", "/r/g", "slim"], ("/r/g", None)),
        (["--log-level=DEBUG", "--root", "/r/g", "cli"], ("/r/g", None)),
        (["--root", "/r/g", "init", "--root", "/r/g"], ("/r/g", "/r/g")),
        (["init", "--root", "/r/a", "--root", "/r/b"], (None, "/r/b")),  # last wins, as argparse
        # Not a root flag of the scanned shape: the value of another option,
        # an unscoped command's argument, after `--`, empty, or missing.
        (["cli", "-m", "--root /r/x"], (None, None)),
        (["cli", "--root", "/r/x"], (None, None)),
        (["init", "--", "--root", "/r/x"], (None, None)),
        (["-l", "--root", "init"], (None, None)),
        (["init", "--root="], (None, None)),
        (["init", "--root"], (None, None)),
        (["--not-a-real-flag"], (None, None)),
        ([], (None, None)),
    ],
)
def test_the_root_scan_reads_only_the_two_full_spellings(argv, expected):
    assert run._root_flags_from_argv(argv) == expected


@pytest.fixture
def _restore_root_selection(monkeypatch):
    """main() selects the process root; put it back after the test."""
    from nymeria import _runtime_paths

    monkeypatch.setattr(run, "_project_root", run._project_root)
    monkeypatch.setattr(_runtime_paths, "_SELECTED_PROJECT_ROOT", None)


def _record_load(monkeypatch) -> dict[str, Any]:
    from nymeria import _runtime_paths

    seen: dict[str, Any] = {}

    def _load() -> None:
        seen["project_root"] = run._project_root
        seen["env"] = os.environ.get("NYMERIA_PROJECT_ROOT")
        seen["explicit"] = _runtime_paths.explicit_project_root()

    monkeypatch.setattr(run, "_load_environment", _load)
    return seen


@pytest.mark.parametrize(
    "argv,command",
    [
        (["init", "--root", "{root}"], "init"),
        (["--root", "{root}", "init"], "init"),
        (["--root={root}", "service", "status"], "service"),
        (["browser", "status", "--root", "{root}"], "browser"),
        (["--root", "{root}", "doctor"], "doctor"),
    ],
)
def test_a_root_flag_is_the_process_root_before_any_env_file_loads(
    monkeypatch, tmp_path, _restore_root_selection, argv, command
):
    root = tmp_path / "target"
    seen = _record_load(monkeypatch)
    dispatched: list[argparse.Namespace] = []
    monkeypatch.setattr(run, "validate_config", lambda *a, **k: None)
    monkeypatch.setattr(
        run, run.COMMANDS[command].runner.__name__, lambda args: dispatched.append(args) or 0
    )
    monkeypatch.setattr(sys, "argv", ["run.py", *(a.format(root=root) for a in argv)])

    with pytest.raises(SystemExit) if run.COMMANDS[command].exits else _no_raise():
        run.main()

    assert seen == {"project_root": root, "env": str(root), "explicit": root}
    (args,) = dispatched
    # Commands with their own --root get the global one: one meaning everywhere.
    if hasattr(args, "root"):
        assert Path(args.root).resolve() == root


class _no_raise:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_without_a_root_flag_the_launch_root_stays(monkeypatch, _restore_root_selection):
    from nymeria import _runtime_paths

    seen = _record_load(monkeypatch)
    before = (run._project_root, os.environ.get("NYMERIA_PROJECT_ROOT"))
    monkeypatch.setattr(run, "run_doctor", lambda args: 0)
    monkeypatch.setattr(sys, "argv", ["run.py", "doctor"])

    with pytest.raises(SystemExit):
        run.main()

    assert (seen["project_root"], seen["env"]) == before
    assert _runtime_paths._SELECTED_PROJECT_ROOT is None


def test_two_different_roots_exit_2_before_anything_loads(
    monkeypatch, tmp_path, capsys, _restore_root_selection
):
    monkeypatch.setattr(
        run, "_load_environment", lambda: pytest.fail("nothing may load on a clash")
    )
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(sys, "argv", ["run.py", "--root", str(a), "init", "--root", str(b)])

    with pytest.raises(SystemExit) as exc:
        run.main()

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert str(a) in err and str(b) in err


def test_the_same_root_in_both_places_is_fine(monkeypatch, tmp_path, _restore_root_selection):
    root = tmp_path / "r"
    seen = _record_load(monkeypatch)
    dispatched: list[argparse.Namespace] = []
    monkeypatch.setattr(run, "run_init", lambda args: dispatched.append(args) or 0)
    monkeypatch.setattr(
        sys, "argv", ["run.py", "--root", str(root), "init", "--root", f"{root}/./"]
    )

    with pytest.raises(SystemExit) as exc:
        run.main()

    assert exc.value.code == 0
    assert seen["project_root"] == root
    assert len(dispatched) == 1


@pytest.mark.parametrize("spelling", ["--roo", "--ro"])
def test_an_abbreviated_root_flag_is_refused_after_parsing(
    monkeypatch, tmp_path, capsys, _restore_root_selection, spelling
):
    # argparse maps `--roo` to --root, but the env files were already loaded
    # from the root the scan saw (none): running would split the root.
    seen = _record_load(monkeypatch)
    monkeypatch.setattr(run, "run_init", lambda args: pytest.fail("must not dispatch"))
    monkeypatch.setattr(sys, "argv", ["run.py", "init", spelling, str(tmp_path / "b")])

    with pytest.raises(SystemExit) as exc:
        run.main()

    assert exc.value.code == 2
    assert "write the root flag in full" in capsys.readouterr().err
    assert seen["explicit"] != tmp_path / "b"


_ROOT_PROBE = """
import os, sys

sys.path.insert(0, sys.argv[1])
import run

def probe(args):
    from nymeria import _runtime_paths
    from nymeria.config import settings
    print("A", os.environ.get("IT32_A_SENTINEL", "<unset>"))
    print("B", os.environ.get("IT32_B_SENTINEL", "<unset>"))
    print("SETTINGS_ROOT", settings.PROJECT_ROOT)
    print("EXPLICIT", _runtime_paths.explicit_project_root())
    print("API_URL", settings.thin_client_api_url())
    print("ARGS_ROOT", getattr(args, "root", "<none>"))
    return 0

run.run_init = run.run_doctor = run.run_cli = probe
sys.argv = ["run.py", *sys.argv[2:]]
run.main()
"""


@pytest.mark.parametrize(
    "argv",
    [
        ["init", "--root", "{b}"],
        ["--root", "{b}", "init"],
        ["--root", "{b}", "doctor"],
        ["--root", "{b}", "cli"],
    ],
)
def test_a_root_flag_run_never_sees_the_launch_roots_config(tmp_path, argv):
    """E2: a fresh interpreter launched at root A, told `--root B`, holds B's
    env and none of A's. Before #101 entry 6 it loaded A's files first, which
    is how a new install got A's vault key (#451) and a start-now server got
    A's LLM route."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / ".env").write_text("IT32_A_SENTINEL=launch\nAPI_PORT=8111\n", encoding="utf-8")
    (b / ".env").write_text("IT32_B_SENTINEL=target\nAPI_PORT=8222\n", encoding="utf-8")
    repo_root = Path(run.__file__).resolve().parent
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("IT32_A_SENTINEL", "IT32_B_SENTINEL", "API_PORT", "NYMERIA_API_URL")
    }
    env["NYMERIA_PROJECT_ROOT"] = str(a)  # the launch root

    result = subprocess.run(
        [sys.executable, "-c", _ROOT_PROBE, str(repo_root), *(x.format(b=b) for x in argv)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(tmp_path),
    )

    detail = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr[-3000:]}"
    assert result.returncode == 0, detail
    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
    assert lines["A"] == "<unset>", detail
    assert lines["B"] == "target", detail
    assert lines["SETTINGS_ROOT"] == str(b.resolve()), detail
    assert lines["EXPLICIT"] == str(b.resolve()), detail
    assert lines["API_URL"] == "http://localhost:8222", detail
    if argv[0] == "--root" and argv[2] == "init":
        assert lines["ARGS_ROOT"] == str(b), detail  # copied into init's own flag


def test_version_flag_prints_the_package_version_and_exits_zero(capsys):
    # #101 entry 37: `nymeria --version` was "unrecognized arguments".
    from nymeria import __version__

    with pytest.raises(SystemExit) as exc_info:
        run.build_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"nymeria {__version__}"


# ---------------------------------------------------------------------------
# Missing bot SDK guidance (#101 entry 15 of 2026-08-23)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("in_container", [False, True])
def test_a_missing_bot_sdk_names_the_fix_for_where_it_runs(monkeypatch, capsys, in_container):
    # A pip install inside a container dies with the next recreate, so a
    # containerized bot is told to rebuild the image, not to pip install.
    import types

    import nymeria.config.settings as settings_mod

    monkeypatch.setattr(settings_mod, "_in_container", lambda: in_container)
    with pytest.raises(SystemExit) as exc:
        run._require_bot_sdk(types.SimpleNamespace(SDK_AVAILABLE=False), "Telegram", "telegram")
    assert exc.value.code == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "Telegram support is not installed" in out
    assert ("pip install 'nymeriaos[telegram]'" in out) is not in_container
    assert ("add --build to the same docker compose up command" in out) is in_container


def test_an_installed_bot_sdk_passes_the_guard(capsys):
    import types

    run._require_bot_sdk(types.SimpleNamespace(SDK_AVAILABLE=True), "Telegram", "telegram")
    assert capsys.readouterr().out == ""
