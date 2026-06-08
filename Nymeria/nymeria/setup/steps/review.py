"""Final review step: summarize collected answers, then write on Enter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.widgets import Static

from ...config.llm_providers import get_llm_provider_spec
from ...onboarding import (
    EXTERNAL_ACCESS_CHOICES,
    HOSTING_CHOICES,
    IMAGE_TIER_CHOICES,
    PROVIDER_AUTH_METHOD_CHOICES,
    SECURITY_PROFILE_CHOICES,
    HostingOption,
)
from ..nav import Step
from ..rag_catalog import get_embedder, get_reranker
from ..state import WizardState
from ..tool_keys import BACKEND_KEY_SPECS
from ..tool_seed import default_thread_tools_for_state
from .base import WizardStep
from .placeholders import seeded_tool_names, unmet_fetch_dependency

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
    if state.image_tier is not None and state.hosting is HostingOption.DOCKER:
        lines.append(
            f"[bold]Image[/bold]     {IMAGE_TIER_CHOICES[state.image_tier].label}"
        )
    if state.security_profile is not None:
        lines.append(
            "[bold]Security[/bold]  "
            f"{SECURITY_PROFILE_CHOICES[state.security_profile].label}"
        )
    if state.auth_method is not None:
        lines.append(
            "[bold]LLM auth[/bold]  "
            f"{PROVIDER_AUTH_METHOD_CHOICES[state.auth_method].label}"
        )

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

    # Tools: the always-on core set plus the concrete family members seeded at
    # init (web_search_* / fetch_url_* / image_gen_*), written to the bootstrap
    # admin's default_thread_tools, with the still-placeholder capabilities below.
    total_default = len(default_thread_tools_for_state(state))
    tool_lines: list[str] = [
        f"Default thread tools: {total_default} (core set + your picks)"
    ]
    seeded = seeded_tool_names(state)
    if seeded:
        tool_lines.append(f"Picked backends: {', '.join(seeded)}")
    collected_keys = [
        spec.env_var
        for spec in BACKEND_KEY_SPECS.values()
        if state.optional_env.get(spec.env_var)
    ]
    if collected_keys:
        tool_lines.append(f"Backend keys set: {', '.join(sorted(set(collected_keys)))}")
    if unmet_fetch_dependency(state):
        tool_lines.append(
            "[yellow]Heads up: a non-Perplexity search backend is selected with no "
            "fetch backend; results will be links only.[/yellow]"
        )
    placeholder_caps = [
        ("Skill kits", state.extras.get("skill_kits")),
        ("TTS", state.extras.get("tts")),
        ("STT", state.extras.get("stt")),
        ("Agent settings", state.extras.get("agent_settings")),
    ]
    for name, value in placeholder_caps:
        if value and value != "__skip__":
            tool_lines.append(f"{name}: {value}")
    lines.append("")
    lines.append("[bold]Tools and capabilities[/bold]")
    for line in tool_lines:
        lines.append(f"  {line}")

    emb = get_embedder(state.embedder)
    if emb is not None:
        rer = get_reranker(state.reranker)
        lines.append("")
        lines.append("[bold]Semantic memory (RAG)[/bold]")
        lines.append(f"  Embedder: {emb.label}")
        if rer is not None:
            lines.append(f"  Reranker: {rer.label}")
        mode = "vector-only" if state.rag_retrieval_mode == "vector" else "hybrid (BM25 + vector)"
        lines.append(f"  Retrieval: {mode}")

    if state.external_access is not None:
        lines.append("")
        lines.append(
            "[bold]External[/bold]  "
            f"{EXTERNAL_ACCESS_CHOICES[state.external_access].label}"
        )

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
