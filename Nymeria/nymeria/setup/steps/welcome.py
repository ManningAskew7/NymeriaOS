"""Step 0: welcome + environment detection.

The plan's first step is "detect environment": read the host (OS, Docker
daemon, service manager, ports, RAM/disk) and recommend a hosting shape before
the operator chooses one. The runner detects once (deep, with subprocess
probes) and caches the report on state; this screen only renders it. It is
informational, so Enter advances and nothing is stored; the hosting step owns
the actual choice (and greys out shapes the report says cannot work here).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.widgets import Static

from ...onboarding import HOSTING_CHOICES
from ..environment import EnvironmentReport, _floor1, detect_environment
from ..nav import Step
from .base import ACCENT, SECONDARY, WizardStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp


def _ok(flag: bool) -> str:
    return "[#86efac]yes[/#86efac]" if flag else "[#fca5a5]no[/#fca5a5]"


def _report_markup(report: EnvironmentReport) -> str:
    rows: list[tuple[str, str]] = [
        ("Operating system", report.os_label),
        ("Docker available", _ok(report.docker_available)),
    ]
    # Deep-probe rows render only when the probe actually ran (None = light
    # mode or docker absent), so the screen never claims more than it checked.
    if report.docker_daemon_running is not None:
        rows.append(("Docker daemon running", _ok(report.docker_daemon_running)))
    if report.docker_compose_available is not None:
        rows.append(("Compose plugin", _ok(report.docker_compose_available)))
    if report.service_blocked_reason:
        rows.append(
            (
                "Background service",
                f"[#fca5a5]unavailable: {report.service_blocked_reason}[/#fca5a5]",
            )
        )
    elif report.service_manager_label:
        rows.append(("Background service", report.service_manager_label))
    port_value = _ok(report.api_port_free)
    if not report.api_port_free and report.port_owner:
        port_value += f" [{SECONDARY}](held by {report.port_owner})[/]"
    rows.append((f"Port {report.api_port} free", port_value))
    # Light-signal rows render only in the bad case so the common desktop run
    # stays compact (the screen must fit without scrolling).
    if report.browser_blocked_reason:
        rows.append(
            (
                "Local browser",
                f"[#fca5a5]no ({report.browser_blocked_reason})[/#fca5a5]",
            )
        )
    if report.missing_python_deps:
        rows.append(
            (
                "Python packages",
                f"[#fca5a5]missing: {', '.join(report.missing_python_deps)}[/#fca5a5]",
            )
        )
    # Floored to one decimal like the stack warnings, so this screen and a
    # later "only N GB free" warning can never contradict each other.
    if report.total_ram_gb is not None:
        rows.append(("Memory", f"{_floor1(report.total_ram_gb):.1f} GB"))
    if report.free_disk_gb is not None:
        rows.append(("Free disk", f"{_floor1(report.free_disk_gb):.1f} GB"))
    rows.append(
        (
            "Recommended hosting",
            f"[bold {ACCENT}]{HOSTING_CHOICES[report.recommended_hosting].label}[/]",
        )
    )
    lines = [f"[{SECONDARY}]{label:<22}[/]  {value}" for label, value in rows]
    for note in report.notes:
        lines.append("")
        lines.append(f"[#fcd34d]Note:[/#fcd34d] {note}")
    return "\n".join(lines)


class WelcomeStep(WizardStep):
    """Informational front door: show what we detected and the recommendation."""

    def compose_body(self) -> ComposeResult:
        # The runner caches a deep report before the app starts; the light
        # fallback keeps directly-constructed screens (tests) working.
        detected = self.state.env_report or detect_environment()
        report = Static(_report_markup(detected), id="env-report")
        report.border_title = "Detected environment"
        yield report

    def collect(self) -> bool:
        # Nothing to store; the hosting step owns the deployment-target choice.
        return True


def make_welcome_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> WelcomeStep:
        return WelcomeStep(
            wizard,
            number,
            total,
            step_id="welcome",
            title="Welcome to Nymeria setup",
            note=(
                "This wizard writes your first-run configuration. Press Enter to "
                "begin. Esc goes back a step, Ctrl+Q quits."
            ),
            hint="enter begin   ctrl+q quit",
        )

    return Step(id="welcome", applies=lambda _state: True, build=build)


__all__ = ["make_welcome_step", "WelcomeStep", "_report_markup"]
