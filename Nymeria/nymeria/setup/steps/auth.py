"""LLM auth method step: direct API key vs subscription OAuth via CLIProxy.

The direct, TOS-clean API key stays the recommended default. Choosing the
subscription branch routes through the CLIProxy steps that follow (disclaimer,
endpoint, provider, login, model); the provider/connection/model trio of the
API-key path drops out via its `applies` predicate, and vice versa.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...onboarding import (
    PROVIDER_AUTH_METHOD_CHOICES,
    PROVIDER_AUTH_METHOD_ORDER,
    ProviderAuthMethod,
    legacy_cliproxy_provider,
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
        else:
            label = meta.label + " (advanced)"
        out.append(Choice(value=method, label=label, description=meta.description))
    return out


class AuthMethodStep(SingleSelectStep):
    """Single-select between the API-key path and the CLIProxy OAuth branch."""

    def action_skip(self) -> None:
        # Skipping pins the recommended method so downstream `applies`
        # predicates see a definite branch, then advances.
        self.show_error("")
        self._store(self.state, ProviderAuthMethod.API_KEY)
        self._wizard.advance()


def make_auth_method_step() -> Step:
    def get_initial(state: WizardState) -> ProviderAuthMethod:
        method = state.auth_method or ProviderAuthMethod.API_KEY
        # Legacy per-provider values render as the generic branch.
        if legacy_cliproxy_provider(method) is not None:
            return ProviderAuthMethod.CLIPROXY_OAUTH
        return method

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
                "Direct API keys are the simple, terms-of-service-clean "
                "default. Subscription OAuth routes an existing AI "
                "subscription through a CLIProxy deployment; it is an "
                "advanced path behind a disclaimer."
            ),
        )

    return Step(id="auth_method", applies=lambda _state: True, build=build)


def is_cliproxy_auth(state: WizardState) -> bool:
    """True when the wizard is on the CLIProxy subscription branch."""
    return state.auth_method_is_cliproxy()


__all__ = ["make_auth_method_step", "AuthMethodStep", "is_cliproxy_auth"]
