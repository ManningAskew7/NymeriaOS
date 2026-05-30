"""Shared answer model for the first-run wizard.

`WizardState` is the single source of truth for everything the wizard collects.
Every step reads its initial value from here and writes its answer back here, so
navigating back and forward is lossless even though Textual recreates screens.
The headless finalize path consumes the same object, so the interactive and
non-interactive flows converge on one shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..onboarding import HostingOption, NextAction
from .providers import PROVIDERS


@dataclass
class WizardState:
    # Step 1: hosting
    hosting: HostingOption | None = None

    # Step 2: provider + credentials
    provider: str | None = None
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    api_mode: str = ""  # "", "responses", or "chat_completions"

    # Optional capability keys (EMBEDDING/OPENAI/GEMINI/PERPLEXITY).
    optional_env: dict[str, str] = field(default_factory=dict)

    # Placeholder steps store their selection here, keyed by step id.
    extras: dict[str, Any] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)

    # Paths and post-setup behavior (driven by flags, not wizard screens yet).
    root: Path | None = None
    data_dir: Path | None = None
    next_action: NextAction = NextAction.PRINT_COMMANDS
    skip_llm_test: bool = False
    force: bool = False
    run_doctor: bool = False
    full_doctor: bool = False

    def provider_option(self):
        """Return the ProviderOption for the chosen provider, or None."""
        if self.provider is None:
            return None
        return PROVIDERS.get(self.provider)


__all__ = ["WizardState"]
