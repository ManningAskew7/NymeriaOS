"""Placeholder capability steps.

The architecture (navigation, styling, store-into-state) is real; the option
content is intentionally stubbed until each capability is designed for real.
Single-select capabilities store into `state.extras[step_id]`; tool selection
is multi-select and stores into `state.tools`.
"""

from __future__ import annotations

from ..nav import Step
from ..state import WizardState
from .base import Choice, multi_select_step, placeholder_step


def make_web_search_step() -> Step:
    return placeholder_step(
        step_id="web_search",
        title="Web search",
        note="Placeholder. Search provider options are not wired up yet.",
        options=[
            Choice("tavily", "Tavily", "Hosted search API (key required)."),
            Choice("brave", "Brave Search", "Hosted search API (key required)."),
            Choice("ddg", "DuckDuckGo", "No key required."),
            Choice("searxng", "SearXNG", "Self-hosted search endpoint."),
        ],
    )


def make_embeddings_step() -> Step:
    return placeholder_step(
        step_id="embeddings",
        title="Semantic memory / RAG embeddings",
        note="Placeholder. Embedding model options are not wired up yet.",
        options=[
            Choice("openai-small", "OpenAI text-embedding-3-small", "Hosted, low cost."),
            Choice("openai-large", "OpenAI text-embedding-3-large", "Hosted, higher quality."),
            Choice("local", "Local embeddings", "Runs on this machine."),
        ],
    )


def make_image_gen_step() -> Step:
    return placeholder_step(
        step_id="image_gen",
        title="Image generation",
        note="Placeholder. Image model options are not wired up yet.",
        options=[
            Choice("openai", "OpenAI gpt-image", "Hosted (key required)."),
            Choice("fal", "FAL.ai", "Hosted (key required)."),
            Choice("xai", "xAI Grok Imagine", "Hosted (key required)."),
        ],
    )


def make_tts_step() -> Step:
    return placeholder_step(
        step_id="tts",
        title="Text-to-speech",
        note="Placeholder. Voice synthesis options are not wired up yet.",
        options=[
            Choice("cartesia", "Cartesia Sonic", "Hosted (key required)."),
            Choice("openai", "OpenAI TTS", "Hosted (key required)."),
            Choice("gemini", "Gemini TTS", "Hosted (key required)."),
            Choice("qwen3", "Qwen3-TTS", "Runs locally."),
        ],
    )


def make_stt_step() -> Step:
    return placeholder_step(
        step_id="stt",
        title="Speech-to-text",
        note="Placeholder. Transcription options are not wired up yet.",
        options=[
            Choice("openai", "OpenAI", "Hosted (key required)."),
            Choice("faster-whisper", "Faster-Whisper", "Runs locally."),
        ],
    )


def make_tools_step() -> Step:
    """Multi-select tool categories (boxy checkboxes); stored on state.tools."""

    choices = [
        Choice("web", "Web search and scraping", ""),
        Choice("files", "File operations", ""),
        Choice("shell", "Terminal and processes", ""),
        Choice("code", "Code execution", ""),
        Choice("vision", "Vision", ""),
        Choice("memory", "Memory and RAG", ""),
        Choice("todos", "Task planning (TODOs)", ""),
        Choice("triggers", "Triggers and scheduling", ""),
    ]

    def get_initial(state: WizardState) -> list[str]:
        return list(state.tools)

    def store(state: WizardState, value: list[str]) -> None:
        state.tools = list(value)

    return multi_select_step(
        step_id="tools",
        title="Tool selection",
        note="Placeholder. The core tool list is still being decided.",
        choices=choices,
        get_initial=get_initial,
        store=store,
    )


__all__ = [
    "make_web_search_step",
    "make_embeddings_step",
    "make_image_gen_step",
    "make_tts_step",
    "make_stt_step",
    "make_tools_step",
]
