"""Fast model tier command: /fast."""

from __future__ import annotations

from ....config.model_tiers import DEFAULT_FAST_MODELS, default_fast_model
from . import Command, CommandContext, CommandRegistry, CommandResult
from ._tier_common import handle_tier, tier_prompt_payload

__all__ = [
    "DEFAULT_FAST_MODELS",
    "default_fast_model",
    "fast_prompt_payload",
    "register",
]


async def _handle_fast(context: CommandContext, args: list[str]) -> CommandResult:
    return await handle_tier(context, args, tier="fast")


def fast_prompt_payload(result):
    """Back-compat alias for the shared one-shot payload extractor."""
    return tier_prompt_payload(result)


def register(registry: CommandRegistry) -> None:
    registry.register(Command(
        name="fast",
        description="Toggle between default and fast models",
        usage="/fast [on|off|set <model-id>] or /fast <prompt>",
        handler=_handle_fast,
        category="Model",
    ))
