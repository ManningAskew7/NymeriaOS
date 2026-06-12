"""Step: which port the API listens on (written as API_PORT)."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.widgets import Input, Static

from ..environment import port_free
from ..nav import Step
from .base import FormStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp


class ApiPortStep(FormStep):
    """One numeric input, prefilled with the current port (default 8000).

    A busy port warns (with the owning process and a suggested free port when
    detection saw them) but never vetoes: the operator may be re-running setup
    over an install that is currently serving that very port.
    """

    def compose_body(self) -> ComposeResult:
        report = self.state.env_report
        if report is not None and not report.api_port_free:
            owner = f" by {report.port_owner}" if report.port_owner else ""
            suggestion = (
                f" Port {report.suggested_port} looks free."
                if report.suggested_port is not None
                else ""
            )
            yield Static(
                f"[yellow]Port {report.api_port} is already in use{owner}."
                f"{suggestion}[/yellow]",
                id="port-warning",
            )
        yield Static("API port", classes="field-label")
        yield Input(value=str(self.state.resolved_api_port()), id="api-port")

    def on_mount(self) -> None:
        self.query_one("#api-port", Input).focus()
        self.call_after_refresh(self._refresh_focus_view)

    def collect(self) -> bool:
        widget = self.query_one("#api-port", Input)
        raw = widget.value.strip()
        if not raw:
            # Cleared field keeps the current value (hydrated or default).
            return True
        if not raw.isdigit() or not 1 <= int(raw) <= 65535:
            self.show_error("Enter a port between 1 and 65535.")
            widget.focus()
            return False
        port = int(raw)
        self.state.api_port = port
        self._refresh_report(port)
        return True

    def _refresh_report(self, port: int) -> None:
        # Keep the cached report consistent with the chosen port so the review
        # heads-up checks the right one (one cheap socket probe; the owner and
        # suggestion are not re-resolved here).
        report = self.state.env_report
        if report is None or report.api_port == port:
            return
        self.state.env_report = replace(
            report,
            api_port=port,
            api_port_free=port_free(port),
            port_owner="",
            suggested_port=None,
        )


def make_api_port_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> ApiPortStep:
        return ApiPortStep(
            wizard,
            number,
            total,
            step_id="api_port",
            title="Which port should the API listen on?",
            note=(
                "Default 8000. The choice is written as API_PORT; printed URLs, "
                "health checks, and remote access all follow it."
            ),
            hint="enter next   esc back   ctrl+s skip   ctrl+q quit",
        )

    return Step(id="api_port", applies=lambda _state: True, build=build)


__all__ = ["make_api_port_step", "ApiPortStep"]
