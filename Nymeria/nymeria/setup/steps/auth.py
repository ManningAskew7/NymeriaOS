"""LLM auth method step: direct API key (real) vs subscription OAuth (deferred).

The plan defaults LLM auth to a direct, TOS-clean API key and keeps subscription
OAuth via CLIProxy an opt-in advanced path. That OAuth branch is parked in git
history and not wired into this installer, so the step shows it for orientation
but gates selection: choosing it explains why and asks for the API-key path. The
provider/connection/model steps then run only for the API-key path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...onboarding import (
    PROVIDER_AUTH_METHOD_CHOICES,
    PROVIDER_AUTH_METHOD_ORDER,
    ProviderAuthMethod,
)
from ..nav import Step
from ..state import WizardState
from .base import Choice, SingleSelectStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp


def _auth_choices() -> list[Choice]:
    out: list[Choice] = []
    for method in PROVIDER_AUTH_METHOD_ORDER:
        meta = PROVIDER_AUTH_METHOD_CHOICES[method]
        if method is ProviderAuthMethod.API_KEY:
            label = meta.label + " (recommended)"
            description = meta.description
        else:
            label = meta.label + " (not in this installer yet)"
            description = (
                meta.description
                + " Deferred for now; choose Direct API key to continue."
            )
        out.append(Choice(value=method, label=label, description=description))
    return out


class AuthMethodStep(SingleSelectStep):
    """Single-select that accepts only the wired API-key path for now."""

    def collect(self) -> bool:
        buttons = self._buttons()
        sel = next((i for i, b in enumerate(buttons) if b.value), None)
        if sel is None or sel >= len(self._choices):
            self.show_error("Select an option, then press Enter.")
            return False
        value = self._choices[sel].value
        if value is not ProviderAuthMethod.API_KEY:
            # Web-form validation: focus the only valid option and explain.
            api_idx = next(
                (i for i, c in enumerate(self._choices)
                 if c.value is ProviderAuthMethod.API_KEY),
                None,
            )
            if api_idx is not None and api_idx < len(buttons):
                buttons[api_idx].focus()
            self.show_error(
                "Subscription OAuth via CLIProxy is not available in the installer "
                "yet. Choose Direct API key to continue."
            )
            return False
        self._store(self.state, value)
        return True

    def action_skip(self) -> None:
        # Skipping must not leave a deferred OAuth value in state (which would
        # silently disable provider/connection/model via the applies predicate).
        # Pin the only wired method, then advance.
        self.show_error("")
        self._store(self.state, ProviderAuthMethod.API_KEY)
        self._wizard.advance()


def make_auth_method_step() -> Step:
    def get_initial(state: WizardState) -> ProviderAuthMethod:
        return state.auth_method or ProviderAuthMethod.API_KEY

    def store(state: WizardState, value: Any) -> None:
        state.auth_method = value

    def build(wizard: "SetupWizardApp", number: int, total: int) -> AuthMethodStep:
        return AuthMethodStep(
            wizard,
            number,
            total,
            step_id="auth_method",
            title="How should Nymeria reach your LLM provider?",
            choices=_auth_choices(),
            get_initial=get_initial,
            store=store,
            note=(
                "Direct API keys are the simple, terms-of-service-clean default. "
                "Subscription OAuth via CLIProxy is an advanced path that is not "
                "wired into setup yet."
            ),
        )

    return Step(id="auth_method", applies=lambda _state: True, build=build)


__all__ = ["make_auth_method_step", "AuthMethodStep"]
