"""Final review step: summarize collected answers, then write on Enter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.widgets import Static

from ...config.llm_providers import get_llm_provider_spec
from ...onboarding import HOSTING_CHOICES
from ..nav import Step
from ..state import WizardState
from .base import WizardStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp


class ReviewStep(WizardStep):
    def compose_body(self) -> ComposeResult:
        yield Static(_summary_markup(self.state))

    def collect(self) -> bool:
        # Advancing past review finishes the wizard; finalize runs after exit.
        return True


def _summary_markup(state: WizardState) -> str:
    lines: list[str] = []

    if state.hosting is not None:
        lines.append(f"[bold]Hosting[/bold]   {HOSTING_CHOICES[state.hosting].label}")

    spec = get_llm_provider_spec(state.provider)
    if spec is not None:
        lines.append(f"[bold]Provider[/bold]  {spec.label}  ({spec.tier})")
        model = state.model or spec.default_model or "(choose on next run)"
        lines.append(f"[bold]Model[/bold]     {model}")
        if state.api_key:
            lines.append("[bold]API key[/bold]   set")
        if state.api_mode:
            lines.append(f"[bold]API mode[/bold]  {state.api_mode}")
        if state.base_url:
            lines.append(f"[bold]Base URL[/bold]  {state.base_url}")

    capabilities = [
        ("Web search", state.extras.get("web_search")),
        ("Embeddings", state.extras.get("embeddings")),
        ("Image gen", state.extras.get("image_gen")),
        ("TTS", state.extras.get("tts")),
        ("STT", state.extras.get("stt")),
        ("Agent settings", state.extras.get("agent_settings")),
    ]
    chosen = [
        f"{name}: {value}"
        for name, value in capabilities
        if value and value != "__skip__"
    ]
    if chosen:
        lines.append("")
        lines.append("[bold]Capabilities[/bold]")
        for line in chosen:
            lines.append(f"  {line}")

    if state.tools:
        lines.append("")
        lines.append(f"[bold]Tools[/bold]     {', '.join(state.tools)}")

    if not lines:
        lines.append("Nothing selected yet.")
    return "\n".join(lines)


def make_review_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> ReviewStep:
        return ReviewStep(
            wizard,
            number,
            total,
            step_id="review",
            title="Review and finish",
            note="Press Enter to write your configuration.",
            hint="enter write config and finish   esc back   ctrl+q quit",
        )

    return Step(id="review", applies=lambda _state: True, build=build)


__all__ = ["make_review_step", "ReviewStep"]
