"""Step 0: welcome + environment detection.

The plan's first step is "detect environment": read the host (OS, Docker, port
8000) and recommend a hosting shape before the operator chooses one. This screen
is informational, so Enter advances and nothing is stored; the detected
recommendation is shown here and the hosting step still owns the actual choice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.widgets import Static

from ...onboarding import HOSTING_CHOICES
from ..environment import EnvironmentReport, detect_environment
from ..nav import Step
from .base import WizardStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp


def _ok(flag: bool) -> str:
    return "[#86efac]yes[/#86efac]" if flag else "[#fca5a5]no[/#fca5a5]"


def _report_markup(report: EnvironmentReport) -> str:
    lines = [
        "[bold]Detected environment[/bold]",
        f"  Operating system   {report.os_label}",
        f"  Docker available   {_ok(report.docker_available)}",
        f"  Port 8000 free     {_ok(report.port_8000_free)}",
        "",
        (
            "[bold]Recommended hosting[/bold]   "
            f"{HOSTING_CHOICES[report.recommended_hosting].label}"
        ),
    ]
    for note in report.notes:
        lines.append("")
        lines.append(f"[#fcd34d]Note:[/#fcd34d] {note}")
    return "\n".join(lines)


class WelcomeStep(WizardStep):
    """Informational front door: show what we detected and the recommendation."""

    def compose_body(self) -> ComposeResult:
        yield Static(_report_markup(detect_environment()))

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


__all__ = ["make_welcome_step", "WelcomeStep"]
