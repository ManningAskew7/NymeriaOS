"""Host environment detection, detection-driven wiring, and headless hosting gates.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import pytest
from nymeria.onboarding import HostingOption
from nymeria.setup import finalize as finalize_mod
from nymeria.setup import runner as runner_mod
from nymeria.setup.runner import main as setup_main

from _setup_wizard_helpers import (  # type: ignore[import-not-found]
    _env_report,
    _first_run,
    _stub_llm,
)


# --- environment detection --------------------------------------------------


def test_recommend_hosting_prefers_container_on_windows_with_docker():
    from nymeria.setup.environment import recommend_hosting

    assert (
        recommend_hosting(is_windows=True, has_docker=True) is HostingOption.DOCKER
    )
    assert (
        recommend_hosting(is_windows=True, has_docker=False) is HostingOption.LOCAL
    )
    assert (
        recommend_hosting(is_windows=False, has_docker=True) is HostingOption.LOCAL
    )


def test_detect_environment_returns_a_report(monkeypatch):
    from nymeria.setup import environment as env_mod

    monkeypatch.setattr(env_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(env_mod, "port_free", lambda *_a, **_k: False)
    report = env_mod.detect_environment()

    assert report.docker_available is False
    assert report.port_8000_free is False
    assert report.recommended_hosting in tuple(HostingOption)
    # A busy port is surfaced as a note rather than silently dropped.
    assert any("8000" in note for note in report.notes)


def test_detect_environment_light_skips_subprocess_probes(monkeypatch):
    from nymeria.setup import environment as env_mod

    def _no_subprocess(*_a, **_k):
        raise AssertionError("light detection must not spawn subprocesses")

    monkeypatch.setattr(env_mod.subprocess, "run", _no_subprocess)
    monkeypatch.setattr(env_mod, "port_free", lambda *_a, **_k: True)
    report = env_mod.detect_environment()
    assert report.deep is False
    assert report.docker_daemon_running is None
    assert report.docker_compose_available is None
    assert report.nymeria_containers == ()
    assert report.mcp_port_free is None
    assert report.port_owner == ""


def test_detect_environment_deep_probes_docker(monkeypatch):
    from nymeria.setup import environment as env_mod

    monkeypatch.setattr(env_mod.shutil, "which", lambda _name: "/usr/bin/mock")
    monkeypatch.setattr(env_mod, "port_free", lambda *_a, **_k: True)
    monkeypatch.setattr(env_mod, "docker_daemon_running", lambda **_k: True)
    monkeypatch.setattr(env_mod, "docker_compose_available", lambda **_k: True)
    monkeypatch.setattr(
        env_mod, "list_nymeria_containers", lambda **_k: ("nymeria-api",)
    )
    report = env_mod.detect_environment(deep=True)
    assert report.deep is True
    assert report.docker_daemon_running is True
    assert report.docker_compose_available is True
    assert report.nymeria_containers == ("nymeria-api",)
    assert any("existing install" in note for note in report.notes)
    assert report.mcp_port_free is True


def test_detect_environment_deep_survives_probe_failures(monkeypatch):
    from nymeria.setup import environment as env_mod

    monkeypatch.setattr(env_mod.shutil, "which", lambda _name: "/usr/bin/mock")
    monkeypatch.setattr(env_mod, "port_free", lambda *_a, **_k: False)

    def _boom(*_a, **_k):
        raise FileNotFoundError("docker vanished mid-probe")

    monkeypatch.setattr(env_mod.subprocess, "run", _boom)
    report = env_mod.detect_environment(deep=True)
    # FileNotFoundError is an OSError: every probe degrades to unknown/empty
    # instead of raising.
    assert report.docker_daemon_running is None
    assert report.docker_compose_available is None
    assert report.nymeria_containers == ()
    assert report.port_owner == ""
    assert any("already in use" in note for note in report.notes)


def test_docker_probes_handle_timeouts(monkeypatch):
    import subprocess as sp

    from nymeria.setup import environment as env_mod

    monkeypatch.setattr(env_mod.shutil, "which", lambda _name: "/usr/bin/mock")

    def _slow(cmd, **_k):
        raise sp.TimeoutExpired(cmd, 0.1)

    monkeypatch.setattr(env_mod.subprocess, "run", _slow)
    assert env_mod.docker_daemon_running() is None
    assert env_mod.docker_compose_available() is None
    assert env_mod.list_nymeria_containers() == ()
    assert env_mod.identify_port_owner(8000) == ""


def test_identify_port_owner_parses_ss_and_falls_back_to_lsof(monkeypatch):
    from nymeria.setup import environment as env_mod

    monkeypatch.setattr(env_mod.shutil, "which", lambda _name: "/usr/bin/mock")

    class _Result:
        def __init__(self, stdout: str):
            self.returncode = 0
            self.stdout = stdout

    ss_line = (
        'LISTEN 0 2048 127.0.0.1:8000 0.0.0.0:* users:(("uvicorn",pid=4242,fd=13))'
    )

    def via_ss(cmd, **_k):
        assert cmd[0] == "ss", "ss answered; lsof must not run"
        return _Result(ss_line + "\n")

    monkeypatch.setattr(env_mod.subprocess, "run", via_ss)
    assert env_mod.identify_port_owner(8000) == "uvicorn"

    def via_lsof(cmd, **_k):
        if cmd[0] == "ss":
            return _Result("")  # owner not visible to ss without root
        assert cmd[0] == "lsof"
        return _Result("p4242\ncuvicorn\n")

    monkeypatch.setattr(env_mod.subprocess, "run", via_lsof)
    assert env_mod.identify_port_owner(8000) == "uvicorn"

    def nothing_visible(cmd, **_k):
        return _Result("")

    monkeypatch.setattr(env_mod.subprocess, "run", nothing_visible)
    assert env_mod.identify_port_owner(8000) == ""


def test_suggest_free_port_returns_next_free(monkeypatch):
    from nymeria.setup import environment as env_mod

    monkeypatch.setattr(env_mod, "port_free", lambda p, **_k: p >= 8003)
    assert env_mod.suggest_free_port(8000) == 8003
    monkeypatch.setattr(env_mod, "port_free", lambda *_a, **_k: False)
    assert env_mod.suggest_free_port(8000, tries=5) is None


def test_suggest_free_port_skips_the_mcp_port(monkeypatch):
    """8001 only looks free: the full stack publishes the MCP server there."""
    from nymeria.setup import environment as env_mod

    monkeypatch.setattr(env_mod, "port_free", lambda *_a, **_k: True)
    assert env_mod.suggest_free_port(8000) == 8002
    assert env_mod.suggest_free_port(9000) == 9001


def test_ram_and_disk_probes_never_raise(monkeypatch, tmp_path):
    from nymeria.setup import environment as env_mod

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        8000000 kB\n")
    monkeypatch.setattr(env_mod, "_MEMINFO_PATH", meminfo)
    ram = env_mod.total_ram_gb()
    assert ram is not None and 7.0 < ram < 8.0

    # Unreadable meminfo falls back to os.sysconf (real values) or None;
    # either way it must not raise.
    monkeypatch.setattr(env_mod, "_MEMINFO_PATH", tmp_path / "missing")
    env_mod.total_ram_gb()

    assert env_mod.free_disk_gb(tmp_path) is not None
    assert env_mod.free_disk_gb(tmp_path / "nope") is None


def test_service_manager_block_matrix(monkeypatch, tmp_path):
    from nymeria import service_install as si
    from nymeria.setup import environment as env_mod

    _label, reason = env_mod.service_manager_block(platform_name="win32")
    assert reason == "not automated on Windows"
    assert env_mod.service_manager_block(platform_name="darwin") == (
        "launchd agent",
        "",
    )

    monkeypatch.delenv("container", raising=False)
    marker = tmp_path / "dockerenv"
    marker.write_text("")
    monkeypatch.setattr(si, "_CONTAINER_MARKERS", (marker,))
    _label, reason = env_mod.service_manager_block(platform_name="linux")
    assert reason == "running inside a container"

    monkeypatch.setattr(si, "_CONTAINER_MARKERS", (tmp_path / "absent",))
    monkeypatch.setattr(si, "_SYSTEMD_MARKER", tmp_path / "no-systemd")
    _label, reason = env_mod.service_manager_block(platform_name="linux")
    assert reason == "systemd is not running"

    systemd = tmp_path / "systemd"
    systemd.mkdir()
    monkeypatch.setattr(si, "_SYSTEMD_MARKER", systemd)
    assert env_mod.service_manager_block(platform_name="linux") == (
        "systemd user service",
        "",
    )


def test_recommend_hosting_in_container_prefers_local():
    from nymeria.setup.environment import recommend_hosting

    assert (
        recommend_hosting(is_windows=True, has_docker=True, in_container=True)
        is HostingOption.LOCAL
    )


def test_recommend_hosting_prefers_docker_when_native_deps_missing():
    from nymeria.setup.environment import recommend_hosting

    assert (
        recommend_hosting(
            is_windows=False, has_docker=True, native_deps_missing=True
        )
        is HostingOption.DOCKER
    )
    # Without docker there is nothing better to recommend than LOCAL.
    assert (
        recommend_hosting(
            is_windows=False, has_docker=False, native_deps_missing=True
        )
        is HostingOption.LOCAL
    )
    # In-container still wins: the foreground process is the only shape there.
    assert (
        recommend_hosting(
            is_windows=False,
            has_docker=True,
            in_container=True,
            native_deps_missing=True,
        )
        is HostingOption.LOCAL
    )


def test_recommend_hosting_prefers_service_when_manager_available():
    from nymeria.setup.environment import recommend_hosting

    # Linux/macOS with a working service manager and deps present: the
    # background service survives the terminal closing and starts on login,
    # which is what a non-technical install actually wants.
    assert (
        recommend_hosting(is_windows=False, has_docker=False, service_blocked=False)
        is HostingOption.SERVICE
    )
    # Docker being present does not outrank the service on a healthy native
    # host (Docker is only preferred where the native install is painful).
    assert (
        recommend_hosting(is_windows=False, has_docker=True, service_blocked=False)
        is HostingOption.SERVICE
    )
    # A blocked service manager (and the conservative default for direct
    # callers without detection data) keeps the old LOCAL recommendation.
    assert (
        recommend_hosting(is_windows=False, has_docker=False, service_blocked=True)
        is HostingOption.LOCAL
    )
    assert recommend_hosting(is_windows=False, has_docker=False) is HostingOption.LOCAL
    # Missing runtime deps degrade both native shapes: never nudge toward a
    # background service that would fail at launch.
    assert (
        recommend_hosting(
            is_windows=False,
            has_docker=False,
            native_deps_missing=True,
            service_blocked=False,
        )
        is HostingOption.LOCAL
    )
    # In-container and Windows behavior are unchanged by the nudge.
    assert (
        recommend_hosting(
            is_windows=False, has_docker=True, in_container=True, service_blocked=False
        )
        is HostingOption.LOCAL
    )
    assert (
        recommend_hosting(is_windows=True, has_docker=True, service_blocked=False)
        is HostingOption.DOCKER
    )
    assert (
        recommend_hosting(is_windows=True, has_docker=False, service_blocked=False)
        is HostingOption.LOCAL
    )


def test_detect_environment_feeds_service_verdict_into_recommendation(monkeypatch):
    from nymeria.setup import environment as env_mod

    monkeypatch.setattr(env_mod.shutil, "which", lambda _name: None)  # no docker
    monkeypatch.setattr(env_mod, "port_free", lambda *_a, **_k: True)
    monkeypatch.setattr(env_mod, "_detect_in_container", lambda: False)
    monkeypatch.setattr(env_mod, "missing_python_deps", lambda *_a, **_k: ())
    monkeypatch.setattr(
        env_mod, "service_manager_block", lambda **_kw: ("systemd user service", "")
    )
    if env_mod.sys.platform.startswith("win"):  # pragma: no cover (posix CI)
        pytest.skip("service recommendation is a posix behavior")
    assert (
        env_mod.detect_environment().recommended_hosting is HostingOption.SERVICE
    )

    monkeypatch.setattr(
        env_mod,
        "service_manager_block",
        lambda **_kw: ("systemd user service", "systemd is not running"),
    )
    assert env_mod.detect_environment().recommended_hosting is HostingOption.LOCAL


def test_browser_launch_blocked_reason_matrix(monkeypatch):
    from nymeria.setup import environment as env_mod

    for var in ("SSH_CLIENT", "SSH_TTY", "SSH_CONNECTION", "BROWSER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(env_mod, "_detect_in_container", lambda: False)
    monkeypatch.setattr(env_mod, "_detect_wsl", lambda: False)

    # Desktop OSes are assumed to have a default browser.
    assert env_mod.browser_launch_blocked_reason(platform_name="win32") == ""
    assert env_mod.browser_launch_blocked_reason(platform_name="darwin") == ""

    # SSH always blocks, regardless of platform: the browser would open on
    # the wrong machine.
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 50000 10.0.0.2 22")
    assert (
        env_mod.browser_launch_blocked_reason(platform_name="darwin")
        == "SSH session"
    )
    monkeypatch.delenv("SSH_CONNECTION")

    # A container has nowhere to open a browser.
    monkeypatch.setattr(env_mod, "_detect_in_container", lambda: True)
    assert (
        env_mod.browser_launch_blocked_reason(platform_name="linux")
        == "running inside a container"
    )
    monkeypatch.setattr(env_mod, "_detect_in_container", lambda: False)

    # Display-less Linux without WSL interop is headless.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(env_mod.shutil, "which", lambda _name: None)
    assert (
        env_mod.browser_launch_blocked_reason(platform_name="linux")
        == "no graphical session"
    )

    # WSL interop opens URLs in the Windows-side browser, display or not
    # (marker-based: wslview-on-PATH misfires both ways).
    monkeypatch.setattr(env_mod, "_detect_wsl", lambda: True)
    assert env_mod.browser_launch_blocked_reason(platform_name="linux") == ""
    monkeypatch.setattr(env_mod, "_detect_wsl", lambda: False)

    # A graphical session still needs some browser or opener binary.
    monkeypatch.setenv("DISPLAY", ":0")
    assert (
        env_mod.browser_launch_blocked_reason(platform_name="linux")
        == "no web browser found"
    )
    # An explicit $BROWSER launcher counts even with no known binary on PATH.
    monkeypatch.setenv("BROWSER", "my-flatpak-wrapper")
    assert env_mod.browser_launch_blocked_reason(platform_name="linux") == ""
    monkeypatch.delenv("BROWSER")
    monkeypatch.setattr(
        env_mod.shutil,
        "which",
        lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
    )
    assert env_mod.browser_launch_blocked_reason(platform_name="linux") == ""


def test_missing_python_deps_locates_without_importing():
    from nymeria.setup import environment as env_mod

    assert env_mod.missing_python_deps(modules=("os", "json")) == ()
    assert env_mod.missing_python_deps(
        modules=("os", "definitely_not_a_real_module_xyz")
    ) == ("definitely_not_a_real_module_xyz",)
    # The real marker set is importable wherever the suite runs (the tests
    # need those packages themselves).
    assert env_mod.missing_python_deps() == ()


def test_detect_environment_reports_browser_and_deps_signals(monkeypatch):
    from nymeria.setup import environment as env_mod

    monkeypatch.setattr(env_mod, "_detect_in_container", lambda: False)
    monkeypatch.setattr(env_mod, "port_free", lambda *_a, **_k: True)
    monkeypatch.setattr(
        env_mod, "browser_launch_blocked_reason", lambda **_k: "SSH session"
    )
    monkeypatch.setattr(env_mod, "missing_python_deps", lambda: ("langgraph",))
    monkeypatch.setattr(env_mod, "docker_available", lambda: True)

    report = env_mod.detect_environment()
    assert report.browser_blocked_reason == "SSH session"
    assert report.missing_python_deps == ("langgraph",)
    # Missing native deps push the recommendation to the container shape.
    assert report.recommended_hosting is HostingOption.DOCKER
    assert any(
        "langgraph" in note and "Docker" in note for note in report.notes
    )

    monkeypatch.setattr(env_mod, "docker_available", lambda: False)
    report = env_mod.detect_environment()
    assert report.recommended_hosting is HostingOption.LOCAL
    assert any(
        "install the project's Python dependencies" in note
        for note in report.notes
    )


def test_hosting_gates_matrix():
    from nymeria.setup.environment import hosting_gates

    gates = hosting_gates(_env_report(docker_available=False))
    assert gates[HostingOption.DOCKER].disabled
    assert "not installed" in gates[HostingOption.DOCKER].reason
    assert HostingOption.LOCAL not in gates

    gates = hosting_gates(_env_report(docker_daemon_running=False))
    assert not gates[HostingOption.DOCKER].disabled
    assert "daemon" in gates[HostingOption.DOCKER].warning

    gates = hosting_gates(_env_report(docker_compose_available=False))
    assert "compose" in gates[HostingOption.DOCKER].warning

    gates = hosting_gates(
        _env_report(service_blocked_reason="systemd is not running")
    )
    assert gates[HostingOption.SERVICE].disabled

    assert hosting_gates(_env_report()) == {}


def test_hosting_gates_warn_on_missing_python_deps():
    from nymeria.setup.environment import hosting_gates

    gates = hosting_gates(
        _env_report(missing_python_deps=("fastapi", "langgraph"))
    )
    # Both native shapes run this interpreter: warn, never disable (LOCAL
    # must stay an enabled choice).
    assert not gates[HostingOption.LOCAL].disabled
    assert "fastapi, langgraph" in gates[HostingOption.LOCAL].warning
    assert not gates[HostingOption.SERVICE].disabled
    assert gates[HostingOption.SERVICE].warning == gates[HostingOption.LOCAL].warning
    assert HostingOption.DOCKER not in gates

    # A disabled SERVICE gate is not clobbered by the deps warning.
    gates = hosting_gates(
        _env_report(
            missing_python_deps=("fastapi",),
            service_blocked_reason="systemd is not running",
        )
    )
    assert gates[HostingOption.SERVICE].disabled
    assert gates[HostingOption.LOCAL].warning


def test_stack_resource_warnings_thresholds():
    from nymeria.onboarding import DockerStack
    from nymeria.setup.environment import stack_resource_warnings

    tight = _env_report(total_ram_gb=2.0, free_disk_gb=5.0, mcp_port_free=False)
    full = stack_resource_warnings(tight, DockerStack.FULL)
    assert len(full) == 3
    slim = stack_resource_warnings(tight, DockerStack.SLIM)
    assert len(slim) == 1 and "GB of disk" in slim[0]
    healthy = _env_report(total_ram_gb=16.0, free_disk_gb=100.0, mcp_port_free=True)
    assert stack_resource_warnings(healthy, DockerStack.FULL) == []


def test_stack_resource_warnings_never_round_up_to_the_threshold():
    """A sub-threshold value floors in the message: 9.96 GB free must not
    read as "10 GB free; needs roughly 10 GB"."""
    from nymeria.onboarding import DockerStack
    from nymeria.setup.environment import stack_resource_warnings

    nearly = _env_report(total_ram_gb=3.97, free_disk_gb=9.96)
    slim = stack_resource_warnings(nearly, DockerStack.SLIM)
    assert len(slim) == 1
    assert "9.9 GB of disk" in slim[0] and "roughly 10 GB" in slim[0]

    nearly_full = _env_report(total_ram_gb=3.97, free_disk_gb=24.96)
    full = stack_resource_warnings(nearly_full, DockerStack.FULL)
    ram = [w for w in full if "RAM" in w]
    assert ram and "3.9 GB RAM" in ram[0] and "4 GB or more" in ram[0]
    disk = [w for w in full if "disk" in w]
    assert disk and "24.9 GB of disk" in disk[0] and "roughly 25 GB" in disk[0]


# --- detection-driven wizard wiring -------------------------------------------


def test_hosting_choices_disable_impossible_and_tag_recommended():
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.hosting import hosting_choices_for

    state = WizardState()
    state.env_report = _env_report(
        docker_available=False,
        service_blocked_reason="running inside a container",
    )
    choices = {c.value: c for c in hosting_choices_for(state)}
    assert choices[HostingOption.DOCKER].disabled
    assert "unavailable: docker is not installed" in choices[HostingOption.DOCKER].label
    assert choices[HostingOption.SERVICE].disabled
    assert "running inside a container" in choices[HostingOption.SERVICE].label
    assert not choices[HostingOption.LOCAL].disabled
    assert "(recommended)" in choices[HostingOption.LOCAL].label


def test_hosting_choices_warn_on_degraded_docker():
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.hosting import hosting_choices_for

    state = WizardState()
    state.env_report = _env_report(docker_daemon_running=False)
    choices = {c.value: c for c in hosting_choices_for(state)}
    assert not choices[HostingOption.DOCKER].disabled
    assert "Warning:" in choices[HostingOption.DOCKER].description
    assert "(unavailable" not in choices[HostingOption.DOCKER].label


def test_hosting_choices_without_report_match_legacy_behavior():
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.hosting import hosting_choices_for

    choices = {c.value: c for c in hosting_choices_for(WizardState())}
    assert all(not c.disabled for c in choices.values())
    assert "(recommended)" in choices[HostingOption.LOCAL].label


def test_hosting_recommendation_follows_detection():
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.hosting import hosting_choices_for

    state = WizardState()
    state.env_report = _env_report(
        is_windows=True, recommended_hosting=HostingOption.DOCKER
    )
    choices = {c.value: c for c in hosting_choices_for(state)}
    assert "(recommended)" in choices[HostingOption.DOCKER].label
    assert "(recommended)" not in choices[HostingOption.LOCAL].label


def test_welcome_report_markup_shows_deep_rows():
    from nymeria.setup.steps.welcome import _report_markup

    markup = _report_markup(
        _env_report(
            docker_daemon_running=True,
            docker_compose_available=False,
            service_manager_label="systemd user service",
            total_ram_gb=7.8,
            free_disk_gb=120.0,
            api_port_free=False,
            port_owner="uvicorn",
            notes=["Port 8000 is already in use (held by uvicorn)."],
        )
    )
    assert "Docker daemon running" in markup
    assert "Compose plugin" in markup
    assert "systemd user service" in markup
    assert "7.8 GB" in markup
    assert "held by uvicorn" in markup
    assert "Note:" in markup


def test_welcome_report_markup_light_hides_unprobed_rows():
    from nymeria.setup.steps.welcome import _report_markup

    markup = _report_markup(_env_report())
    assert "Docker daemon running" not in markup
    assert "Compose plugin" not in markup
    assert "Port 8000 free" in markup


def test_welcome_report_markup_flags_browser_and_deps_only_when_bad():
    """The browser/deps rows render only in the bad case so the common
    desktop run keeps the screen compact (it must fit without scrolling)."""
    from nymeria.setup.steps.welcome import _report_markup

    healthy = _report_markup(_env_report())
    assert "Local browser" not in healthy
    assert "Python packages" not in healthy

    degraded = _report_markup(
        _env_report(
            browser_blocked_reason="SSH session",
            missing_python_deps=("fastapi",),
        )
    )
    assert "Local browser" in degraded and "SSH session" in degraded
    assert "Python packages" in degraded and "missing: fastapi" in degraded


def test_review_heads_up_lines_for_degraded_conditions():
    from nymeria.onboarding import DockerStack
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.review import _summary_markup

    state = WizardState()
    state.hosting = HostingOption.DOCKER
    state.docker_stack = DockerStack.FULL
    state.env_report = _env_report(
        api_port_free=False,
        port_owner="uvicorn",
        docker_daemon_running=False,
        total_ram_gb=2.0,
    )
    markup = _summary_markup(state)
    assert "Heads up: port 8000 is already in use (held by uvicorn)." in markup
    assert "daemon is not running" in markup
    assert "2.0 GB RAM" in markup


def test_review_has_no_heads_up_on_healthy_host():
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.review import _summary_markup

    state = WizardState()
    state.hosting = HostingOption.LOCAL
    state.env_report = _env_report()
    assert "Heads up: port" not in _summary_markup(state)


# --- headless hosting gates ---------------------------------------------------


def test_headless_blocks_docker_hosting_without_docker(tmp_path, monkeypatch):
    _stub_llm(monkeypatch)
    monkeypatch.setattr(
        runner_mod,
        "detect_environment",
        lambda **_kw: _env_report(docker_available=False),
    )
    with pytest.raises(SystemExit) as excinfo:
        setup_main(
            [
                "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
                "--root", str(tmp_path), "--non-interactive", "--skip-llm-test",
                "--hosting", "docker",
            ]
        )
    assert "docker is not installed" in str(excinfo.value)
    assert not (tmp_path / ".env.docker").exists()


def test_headless_blocks_service_hosting_in_container(tmp_path, monkeypatch):
    _stub_llm(monkeypatch)
    monkeypatch.setattr(
        runner_mod,
        "detect_environment",
        lambda **_kw: _env_report(
            service_blocked_reason="running inside a container"
        ),
    )
    with pytest.raises(SystemExit) as excinfo:
        setup_main(
            [
                "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
                "--root", str(tmp_path), "--non-interactive", "--skip-llm-test",
                "--hosting", "service",
            ]
        )
    assert "running inside a container" in str(excinfo.value)


def test_headless_warns_only_on_hydrated_impossible_hosting(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "runtime"
    _first_run(monkeypatch, root, "--hosting", "docker", "--docker-stack", "slim")
    capsys.readouterr()

    # Reconfigure an unrelated setting with docker now missing: the hydrated
    # hosting shape only warns (a scripted edit must not die over it).
    monkeypatch.setattr(
        runner_mod,
        "detect_environment",
        lambda **_kw: _env_report(docker_available=False),
    )
    rc = setup_main(
        [
            "--model", "claude-other-model", "--root", str(root),
            "--non-interactive", "--skip-llm-test", "--provider", "anthropic",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "unavailable on this machine" in out
    assert "Continuing anyway" in out


def test_headless_warns_on_low_resources_for_full_stack(
    tmp_path, monkeypatch, capsys
):
    _stub_llm(monkeypatch)
    monkeypatch.setattr(
        runner_mod,
        "detect_environment",
        lambda **_kw: _env_report(total_ram_gb=2.0, free_disk_gb=5.0),
    )
    rc = setup_main(
        [
            "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
            "--root", str(tmp_path), "--non-interactive", "--skip-llm-test",
            "--hosting", "docker", "--docker-stack", "full",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "GB RAM" in out and "GB of disk" in out


def test_headless_warns_on_missing_python_deps_for_native_hosting(
    tmp_path, monkeypatch, capsys
):
    _stub_llm(monkeypatch)
    monkeypatch.setattr(
        runner_mod,
        "detect_environment",
        lambda **_kw: _env_report(missing_python_deps=("langgraph",)),
    )
    rc = setup_main(
        [
            "--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
            "--root", str(tmp_path), "--non-interactive", "--skip-llm-test",
            "--hosting", "local",
        ]
    )
    out = capsys.readouterr().out
    # Warn-only: a pip install between now and launch fixes it.
    assert rc == 0
    assert "langgraph" in out and "not importable" in out


def test_doctor_runs_against_the_just_written_config(monkeypatch, tmp_path):
    """run.py loads the pre-wizard config into os.environ at import time
    (override=True), and pydantic-settings reads the live env over the env
    files, so doctor would validate stale values after a reconfigure (the old
    API_PORT was the visible symptom). finalize must replay the freshly
    written files around the doctor call and restore the env afterwards."""
    import os

    from rich.console import Console

    import nymeria.doctor as doctor_mod
    from nymeria.setup.state import WizardState

    root = tmp_path / "init"
    root.mkdir()
    (root / "config.env").write_text("API_PORT=8010\n", encoding="utf-8")
    monkeypatch.setenv("API_PORT", "8000")  # the stale pre-wizard value
    # A key the rewrite REMOVED (e.g. leaving the CLIProxy branch drops
    # LLM_BASE_URL): a replay alone cannot clear it, so finalize passes the
    # pre-write file-key snapshot and doctor must not see it.
    monkeypatch.setenv("LLM_BASE_URL", "http://old-proxy:8318/v1")

    captured = {}

    def fake_run_doctor(_args):
        captured["api_port"] = os.environ.get("API_PORT")
        captured["llm_base_url"] = os.environ.get("LLM_BASE_URL")
        return 0

    monkeypatch.setattr(doctor_mod, "run_doctor", fake_run_doctor)

    state = WizardState()
    state.run_doctor = True
    rc = finalize_mod._maybe_run_doctor(
        state,
        root=root,
        console=Console(),
        provider_auth_validated=True,
        stale_env_keys=frozenset({"API_PORT", "LLM_BASE_URL"}),
    )
    assert rc == 0
    assert captured["api_port"] == "8010"
    assert captured["llm_base_url"] is None
    # The replay is scoped to the doctor call; nothing leaks past it.
    assert os.environ.get("API_PORT") == "8000"
    assert os.environ.get("LLM_BASE_URL") == "http://old-proxy:8318/v1"


def test_file_defined_env_keys_covers_target_root(tmp_path):
    """The pre-write snapshot must include the target root's file keys (it
    also unions the runtime root's, which may carry the host's own config)."""
    root = tmp_path / "init"
    root.mkdir()
    (root / "config.env").write_text(
        "API_PORT=8010\nLLM_BASE_URL=http://proxy/v1\n# COMMENT=x\n",
        encoding="utf-8",
    )
    keys = finalize_mod._file_defined_env_keys(root)
    assert {"API_PORT", "LLM_BASE_URL"} <= keys
    assert "COMMENT" not in keys
