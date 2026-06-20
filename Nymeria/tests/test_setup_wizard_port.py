"""Alternate API port plumbed through every URL/probe/compose binding.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
from nymeria.onboarding import HostingOption
from nymeria.setup import finalize as finalize_mod
from nymeria.setup.runner import main as setup_main

from _setup_wizard_helpers import (
    _API_PORT_STEP,
    _SECURITY_STEP,
    _env_line,
    _env_report,
    _first_run,
    _stub_llm,
)


# --- alternate API port -------------------------------------------------------


def test_finalize_writes_chosen_api_port_and_prints_urls(
    monkeypatch, tmp_path, capsys
):
    _stub_llm(monkeypatch)
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    rc = setup_main(
        [
            "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
            "--root", str(tmp_path), "--non-interactive", "--skip-llm-test",
            "--port", "8010",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    content = (tmp_path / "config.env").read_text()
    assert _env_line(content, "API_PORT") == "8010"
    # Every printed URL (open-the-browser line, smoke curl recipe) follows it.
    assert "localhost:8010" in out
    assert "localhost:8000" not in out


def test_noninteractive_reconfigure_keeps_port_and_flag_overrides(
    monkeypatch, tmp_path
):
    root = tmp_path / "runtime"
    _first_run(monkeypatch, root, "--port", "8010")

    # An unrelated scripted edit keeps the configured port.
    rc = setup_main(
        [
            "--provider", "anthropic", "--model", "other-model",
            "--root", str(root), "--non-interactive", "--skip-llm-test",
        ]
    )
    assert rc == 0
    assert _env_line((root / "config.env").read_text(), "API_PORT") == "8010"

    # An explicit --port on a reconfigure moves it.
    rc = setup_main(
        [
            "--provider", "anthropic", "--model", "other-model",
            "--root", str(root), "--non-interactive", "--skip-llm-test",
            "--port", "8020",
        ]
    )
    assert rc == 0
    assert _env_line((root / "config.env").read_text(), "API_PORT") == "8020"


def test_hydrate_reads_api_port_and_flag_wins(monkeypatch, tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    _first_run(monkeypatch, root, "--port", "8010")

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.api_port == 8010

    flagged = WizardState(root=root, api_port=8123)
    hydrate_state_from_disk(flagged)
    assert flagged.api_port == 8123


def test_docker_stack_spec_honors_alt_port():
    from nymeria.onboarding import DockerStack
    from nymeria.setup.state import WizardState

    # Default-port specs are byte-identical to the documented short commands.
    default_slim = finalize_mod._docker_stack_spec(WizardState())
    assert default_slim.compose_args == ("-f", finalize_mod.DOCKER_SINGLE_COMPOSE)
    assert default_slim.health_url == "http://localhost:8000/health"

    # A non-default port adds --env-file so compose interpolates API_PORT into
    # the host-side binding, and the health probe follows the host port.
    alt_slim = finalize_mod._docker_stack_spec(WizardState(api_port=8010))
    assert alt_slim.compose_args == (
        "-f", finalize_mod.DOCKER_SINGLE_COMPOSE, "--env-file", ".env.docker",
    )
    assert alt_slim.health_url == "http://localhost:8010/health"

    alt_full = finalize_mod._docker_stack_spec(
        WizardState(docker_stack=DockerStack.FULL, api_port=8010)
    )
    assert alt_full.compose_args == ("--env-file", ".env.docker")
    assert alt_full.health_url == "http://localhost:8010/ready"

    # The printed manual command carries a protective env prefix (a shell with
    # a stale API_PORT would otherwise win over --env-file); the default-port
    # commands stay the documented short form.
    assert default_slim.command_env == ()
    assert alt_slim.command_env == (("API_PORT", "8010"),)
    assert finalize_mod._compose_command_str(alt_slim, "up", "-d") == (
        "API_PORT=8010 docker compose -f docker-compose.single.yml "
        "--env-file .env.docker up -d"
    )


def test_compose_env_pins_api_port_over_stale_process_env(monkeypatch):
    from nymeria.setup.state import WizardState

    # run.py's import-time dotenv load leaves the OLD config's port in the
    # process env; compose resolves ${API_PORT} from there BEFORE --env-file,
    # so the wizard's own `up -d` must pin the freshly chosen port.
    monkeypatch.setenv("API_PORT", "8000")
    alt = finalize_mod._docker_stack_spec(WizardState(api_port=8010))
    assert finalize_mod._compose_env(alt)["API_PORT"] == "8010"

    monkeypatch.setenv("API_PORT", "8010")  # foreign checkout's config
    default = finalize_mod._docker_stack_spec(WizardState())
    assert finalize_mod._compose_env(default)["API_PORT"] == "8000"


def test_hydrate_rejects_out_of_range_api_port(monkeypatch, tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    _first_run(monkeypatch, root)
    config = root / "config.env"
    config.write_text(
        config.read_text().replace("API_PORT=8000", "API_PORT=99999")
    )

    # int("99999") parses fine but overflows the socket probes; hydrate must
    # treat it as junk so `nymeria init` does not crash at detection.
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.api_port is None
    assert "api_port_on_disk" not in state.extras


def test_port_change_with_active_public_url_warns(monkeypatch, tmp_path, capsys):
    root = tmp_path / "runtime"
    _first_run(
        monkeypatch, root,
        "--external-access", "cloudflare",
        "--public-url", "https://nym.example.com",
    )
    capsys.readouterr()

    rc = setup_main(
        [
            "--provider", "anthropic", "--model", "claude-test-model",
            "--root", str(root), "--non-interactive", "--skip-llm-test",
            "--port", "8010",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "changing from 8000 to 8010" in out
    assert "ingress" in out

    # A re-run on the SAME port stays quiet.
    rc = setup_main(
        [
            "--provider", "anthropic", "--model", "claude-test-model",
            "--root", str(root), "--non-interactive", "--skip-llm-test",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "ingress" not in out


def test_port_flag_rejects_out_of_range(tmp_path):
    with pytest.raises(SystemExit) as exc:
        setup_main(
            [
                "--provider", "anthropic", "--model", "m", "--api-key", "k",
                "--root", str(tmp_path), "--non-interactive", "--skip-llm-test",
                "--port", "70000",
            ]
        )
    assert "--port must be between" in str(exc.value)


def test_port_busy_warning_names_chosen_port(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    monkeypatch.setattr(finalize_mod, "port_in_use", lambda port: port == 8010)
    rc = setup_main(
        [
            "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
            "--root", str(tmp_path), "--non-interactive", "--skip-llm-test",
            "--port", "8010",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "port 8010 is already in use" in out


def test_local_smoke_worker_uses_alt_port(monkeypatch, tmp_path, capsys):
    import threading as threading_mod

    seen: dict[str, str] = {}

    def fake_health(url):
        seen["health"] = url
        return True

    monkeypatch.setattr(finalize_mod, "_smoke_health_ok", fake_health)
    monkeypatch.setattr(
        finalize_mod, "_wait_for_host_service_token", lambda *_a, **_k: "nym_token"
    )

    def fake_smoke(*, token, base_url="http://localhost:8000"):
        seen["smoke"] = base_url
        return True, "ok"

    monkeypatch.setattr(finalize_mod, "run_chat_smoke_test", fake_smoke)
    finalize_mod._local_smoke_worker(
        tmp_path, threading_mod.Event(), "http://localhost:8010"
    )
    assert seen["health"] == "http://localhost:8010/health"
    assert seen["smoke"] == "http://localhost:8010"


def test_single_compose_files_interpolate_api_port():
    repo = Path(__file__).resolve().parents[1]
    for name in ("docker-compose.single.yml", "docker-compose.single.published.yml"):
        content = (repo / name).read_text()
        # Host side follows API_PORT; the container side (and its internal
        # healthcheck) stays pinned to 8000.
        assert '"127.0.0.1:${API_PORT:-8000}:8000"' in content, name
        assert "http://localhost:8000/health" in content, name


def test_review_shows_api_port_row():
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.review import _summary_markup

    markup = _summary_markup(
        WizardState(hosting=HostingOption.LOCAL, api_port=8010)
    )
    assert "API port" in markup and "8010" in markup


def test_wizard_pilot_api_port_step_validates_and_stores():
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            await pilot.press("enter")  # welcome -> tier chooser
            await pilot.pause()
            await pilot.press("down")  # focus Full setup
            await pilot.press("enter")  # pick Full setup -> hosting
            await pilot.pause()
            await pilot.press("enter")  # accept default hosting -> api port
            await pilot.pause()
            assert app.nav.current() == _API_PORT_STEP
            field = app.screen.query_one("#api-port", Input)
            assert field.value == "8000"
            field.value = "70000"
            await pilot.press("enter")  # out of range: stays with an error
            await pilot.pause()
            assert app.nav.current() == _API_PORT_STEP
            field.value = "8010"
            await pilot.press("enter")
            await pilot.pause()
            assert app.nav.current() == _SECURITY_STEP
        return state

    state = asyncio.run(drive())
    assert state.api_port == 8010


def test_wizard_pilot_api_port_step_warns_busy_and_refreshes_report(monkeypatch):
    from textual.widgets import Input, Static

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    # steps/port.py binds port_free by value at import; patch THAT module.
    monkeypatch.setattr("nymeria.setup.steps.port.port_free", lambda *_a, **_k: True)

    async def drive() -> WizardState:
        state = WizardState()
        state.env_report = _env_report(
            api_port_free=False, port_owner="uvicorn", suggested_port=8002
        )
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            await pilot.press("enter")  # welcome -> tier chooser
            await pilot.pause()
            await pilot.press("down")  # focus Full setup
            await pilot.press("enter")  # pick Full setup -> hosting
            await pilot.pause()
            await pilot.press("enter")  # accept default hosting -> api port
            await pilot.pause()
            warning = str(app.screen.query_one("#port-warning", Static).render())
            assert "uvicorn" in warning and "8002" in warning
            app.screen.query_one("#api-port", Input).value = "8002"
            await pilot.press("enter")
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.api_port == 8002
    # The cached report follows the chosen port so review checks the right one.
    assert state.env_report is not None
    assert state.env_report.api_port == 8002
    assert state.env_report.api_port_free is True
