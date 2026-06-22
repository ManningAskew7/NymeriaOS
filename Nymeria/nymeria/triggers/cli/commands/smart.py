"""Smart model tier command: /smart."""

from __future__ import annotations

from . import Command, CommandContext, CommandRegistry, CommandResult
from ._tier_common import handle_tier, tier_prompt_payload

__all__ = ["smart_prompt_payload", "register"]


async def _handle_smart(context: CommandContext, args: list[str]) -> CommandResult:
    return await handle_tier(context, args, tier="smart")


def smart_prompt_payload(result):
    """Extract a one-shot (prompt, model) payload for /smart, if present."""
    return tier_prompt_payload(result)


def register(registry: CommandRegistry) -> None:
    registry.register(Command(
        name="smart",
        description="Toggle between default and smart models",
        usage="/smart [on|off|set <model-id>] or /smart <prompt>",
        handler=_handle_smart,
        category="Model",
    ))
