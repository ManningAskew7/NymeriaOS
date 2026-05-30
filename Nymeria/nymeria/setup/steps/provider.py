"""Step 2: LLM provider, API key, and model, plus a conditional connection step.

The provider list mirrors how nymeria-desktop groups providers by backend tier
(Native reasoning first, then Gateway). OAuth is deferred, so only the direct
API-key providers appear here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.widgets import Input, RadioButton, RadioSet, Static

from ..nav import Step
from ..providers import (
    API_MODE_PROVIDERS,
    PROVIDER_GROUPS,
    PROVIDERS,
    valid_key_format,
)
from ..state import WizardState
from .base import WizardStep, commit_radio_highlight

if TYPE_CHECKING:
    from ..app import SetupWizardApp

# Providers flattened in tier order, each tagged with its tier label.
_ORDERED_PROVIDERS: list[tuple[str, str]] = [
    (name, tier_label)
    for tier_label, names in PROVIDER_GROUPS
    for name in names
]

_API_MODE_CHOICES = [
    ("", "Default (inherit global)"),
    ("responses", "Responses API"),
    ("chat_completions", "Chat Completions"),
]


class ProviderStep(WizardStep):
    """Provider radio group + API key + model in one screen."""

    def compose_body(self) -> ComposeResult:
        initial = self.state.provider or _ORDERED_PROVIDERS[0][0]
        buttons = [
            RadioButton(f"{PROVIDERS[name].label}  -  {tier}", value=(name == initial))
            for name, tier in _ORDERED_PROVIDERS
        ]
        yield Static("Provider", classes="field-label")
        yield RadioSet(*buttons, id="provider-set")

        provider = PROVIDERS[initial]
        yield Static("API key", classes="field-label")
        yield Input(
            value=self.state.api_key,
            password=True,
            placeholder=f"{provider.key_prefix}...",
            id="api-key",
        )
        yield Static("Model", classes="field-label")
        yield Input(
            value=self.state.model,
            placeholder=f"blank uses {provider.default_model}",
            id="model",
        )

    def on_mount(self) -> None:
        self.query_one("#provider-set", RadioSet).focus()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if not (0 <= event.index < len(_ORDERED_PROVIDERS)):
            return
        provider = PROVIDERS[_ORDERED_PROVIDERS[event.index][0]]
        try:
            key_input = self.query_one("#api-key", Input)
            model_input = self.query_one("#model", Input)
        except Exception:
            return  # widgets not mounted yet during initial compose
        key_input.placeholder = f"{provider.key_prefix}..."
        model_input.placeholder = f"blank uses {provider.default_model}"

    def action_next(self) -> None:
        # Enter on the provider radios commits the choice and moves into the key
        # field instead of advancing, so selecting a provider never skips past
        # key entry. Enter from a field then advances. Ctrl+S skips the step.
        self.show_error("")
        radio_set = self.query_one("#provider-set", RadioSet)
        if self.app.focused is radio_set:
            commit_radio_highlight(radio_set)
            self.query_one("#api-key", Input).focus()
            return
        if self.collect():
            self._wizard.advance()

    def collect(self) -> bool:
        api_key = self.query_one("#api-key", Input).value.strip()
        if not api_key:
            self.show_error("Enter an API key, or press Ctrl+S to skip this step.")
            return False
        idx = commit_radio_highlight(self.query_one("#provider-set", RadioSet))
        if idx is None or idx < 0 or idx >= len(_ORDERED_PROVIDERS):
            self.show_error("Select a provider.")
            return False
        name = _ORDERED_PROVIDERS[idx][0]
        provider = PROVIDERS[name]
        if not valid_key_format(provider, api_key):
            self.show_error(f"That key should start with `{provider.key_prefix}`.")
            return False
        model = self.query_one("#model", Input).value.strip() or provider.default_model
        self.state.provider = name
        self.state.api_key = api_key
        self.state.model = model
        return True


class ConnectionStep(WizardStep):
    """Optional API mode + base URL, shown only for OpenAI-compatible providers."""

    def compose_body(self) -> ComposeResult:
        yield Static("API mode", classes="field-label")
        buttons = [
            RadioButton(label, value=(value == self.state.api_mode))
            for value, label in _API_MODE_CHOICES
        ]
        yield RadioSet(*buttons, id="api-mode-set")
        yield Static("API base URL (optional)", classes="field-label")
        yield Input(
            value=self.state.base_url,
            placeholder="Provider default",
            id="base-url",
        )

    def on_mount(self) -> None:
        self.query_one("#api-mode-set", RadioSet).focus()

    def collect(self) -> bool:
        idx = commit_radio_highlight(self.query_one("#api-mode-set", RadioSet))
        if idx is None or idx < 0 or idx >= len(_API_MODE_CHOICES):
            idx = 0
        self.state.api_mode = _API_MODE_CHOICES[idx][0]
        self.state.base_url = self.query_one("#base-url", Input).value.strip()
        return True


def make_provider_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> ProviderStep:
        return ProviderStep(
            wizard,
            number,
            total,
            step_id="provider",
            title="Choose your LLM provider",
            note="Pick a provider and press Enter, paste its API key, then Enter to continue. Press Ctrl+S to skip and set a provider later.",
            hint="up/down provider   tab fields   enter next   ctrl+s skip   esc back",
        )

    return Step(id="provider", applies=lambda _state: True, build=build)


def make_connection_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> ConnectionStep:
        return ConnectionStep(
            wizard,
            number,
            total,
            step_id="connection",
            title="Connection options",
            note="Only needed for OpenAI-compatible providers.",
        )

    def applies(state: WizardState) -> bool:
        return state.provider in API_MODE_PROVIDERS

    return Step(id="connection", applies=applies, build=build)


__all__ = ["make_provider_step", "make_connection_step", "ProviderStep", "ConnectionStep"]
