"""Final review step: summarize collected answers, then write on Enter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape
from textual.app import ComposeResult
from textual.widgets import Static

from ...config.llm_providers import get_llm_provider_spec
from ...onboarding import (
    DOCKER_STACK_CHOICES,
    EXTERNAL_ACCESS_CHOICES,
    HOSTING_CHOICES,
    PROVIDER_AUTH_METHOD_CHOICES,
    SECURITY_PROFILE_CHOICES,
    HostingOption,
    NextAction,
)
from ..nav import Step
from ..rag_catalog import get_embedder, get_reranker
from ..state import WizardState
from ..tool_keys import BACKEND_KEY_SPECS
from ..tool_seed import default_thread_tools_for_state
from .base import ACCENT, WizardStep
from .placeholders import seeded_global_skills, seeded_tool_names, unmet_fetch_dependency

if TYPE_CHECKING:
    from ..app import SetupWizardApp


class ReviewStep(WizardStep):
    def compose_body(self) -> ComposeResult:
        yield Static(_summary_markup(self.state))

    def collect(self) -> bool:
        # Advancing past review finishes the wizard; finalize runs after exit.
        return True


def _row(label: str, value: str) -> str:
    """One aligned summary row: grey label column, white value."""
    return f"[#aab4c3]{label:<10}[/]{value}"


def _heading(text: str) -> str:
    return f"[bold {ACCENT}]{text}[/]"


def _summary_markup(state: WizardState) -> str:
    lines: list[str] = []

    if state.hosting is not None:
        lines.append(_row("Hosting", HOSTING_CHOICES[state.hosting].label))
    if state.docker_stack is not None and state.hosting is HostingOption.DOCKER:
        lines.append(_row("Stack", DOCKER_STACK_CHOICES[state.docker_stack].label))
    if state.security_profile is not None:
        lines.append(
            _row("Security", SECURITY_PROFILE_CHOICES[state.security_profile].label)
        )
    if state.auth_method is not None:
        lines.append(
            _row("LLM auth", PROVIDER_AUTH_METHOD_CHOICES[state.auth_method].label)
        )

    spec = get_llm_provider_spec(state.provider)
    if spec is not None:
        lines.append(_row("Provider", f"{spec.label}  ({spec.tier})"))
        model = state.model or spec.default_model or "(choose on next run)"
        lines.append(_row("Model", escape(model)))
        if state.api_key:
            lines.append(_row("API key", "set"))
        if state.api_mode:
            lines.append(_row("API mode", state.api_mode))
        if state.base_url:
            lines.append(_row("Base URL", state.base_url))

    # Tools: the default (seed) set plus the concrete family members seeded at
    # init (web_search_* / fetch_url_* / image_gen_*), written to the bootstrap
    # admin's default_thread_tools, with the still-placeholder capabilities below.
    total_default = len(default_thread_tools_for_state(state))
    tool_lines: list[str] = [
        f"Default thread tools: {total_default} (core set + your picks)"
    ]
    seeded = seeded_tool_names(state)
    if seeded:
        tool_lines.append(f"Picked backends: {', '.join(seeded)}")
    from ..tool_keys import VOICE_KEY_SPECS

    collected_keys = [
        spec.env_var
        for spec in BACKEND_KEY_SPECS.values()
        if state.optional_env.get(spec.env_var)
    ] + [
        spec.env_var
        for specs in VOICE_KEY_SPECS.values()
        for spec in specs
        if state.optional_env.get(spec.env_var)
    ]
    if collected_keys:
        tool_lines.append(f"Backend keys set: {', '.join(sorted(set(collected_keys)))}")
    if unmet_fetch_dependency(state):
        tool_lines.append(
            "[yellow]Heads up: a non-Perplexity search backend is selected with no "
            "fetch backend; results will be links only.[/yellow]"
        )
    kits = seeded_global_skills(state)
    if kits:
        tool_lines.append(f"Skill kits: {', '.join(kits)} (self-improve stays on)")
    from .. import voice_catalog

    for name, value, label_fn in (
        ("TTS", state.extras.get("tts"), voice_catalog.tts_label),
        ("STT", state.extras.get("stt"), voice_catalog.stt_label),
    ):
        if isinstance(value, str) and value not in ("__skip__", "none"):
            tool_lines.append(f"{name}: {label_fn(value)}")
    from ..tuning_catalog import tuning_summary_lines

    tool_lines.extend(tuning_summary_lines(state))
    lines.append("")
    lines.append(_heading("Tools and capabilities"))
    for line in tool_lines:
        lines.append(f"  {line}")

    emb = get_embedder(state.embedder)
    if emb is not None:
        rer = get_reranker(state.reranker)
        lines.append("")
        lines.append(_heading("Semantic memory (RAG)"))
        lines.append(f"  Embedder: {emb.label}")
        if rer is not None:
            lines.append(f"  Reranker: {rer.label}")
        mode = "vector-only" if state.rag_retrieval_mode == "vector" else "hybrid (BM25 + vector)"
        lines.append(f"  Retrieval: {mode}")

    if state.external_access is not None:
        lines.append("")
        external = EXTERNAL_ACCESS_CHOICES[state.external_access].label
        from ..finalize import active_public_url

        if active_public_url(state):
            from ..external_access import public_origin

            verified = "verified" if state.public_url_verified else "unverified"
            external += f" at {public_origin(active_public_url(state))} ({verified})"
        lines.append(_row("External", external))

    # Post-setup handoff (set by the start-now step or the --start/--next-action
    # flags). Surfaced so the final Enter's effect is no surprise.
    if state.hosting is not None:
        if state.next_action is NextAction.START_API_OPEN_FRONTEND:
            nxt = "start the backend now"
        elif state.next_action is NextAction.CLI:
            nxt = "enter the CLI chat"
        else:
            nxt = "print the start command"
        lines.append("")
        lines.append(_row("Next", nxt))

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
