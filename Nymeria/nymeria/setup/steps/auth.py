"""LLM auth method step: API key, subscription login via CLIProxy, or local model.

Three equal, capability-gated paths (beta-readiness 03): no path is labeled
"recommended" or "advanced". Choosing the subscription branch routes through
the CLIProxy steps that follow (disclaimer, endpoint, provider, login, model);
choosing the local branch pins the provider to Ollama and skips the provider
picker (there is no key to collect). Capability gaps (no Docker for a CLIProxy
self-deploy, no Ollama install) append a one-line warning to the choice rather
than disabling it: the CLIProxy endpoint step accepts an existing remote proxy
URL, and Ollama may legitimately run remotely or in a container.
"""

from __future__ import annotations

import shutil
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

# The provider the local-model branch pins. Kept a module constant so the
# store, the initial-selection inference, and the tests agree on one value.
LOCAL_MODEL_PROVIDER = "ollama"


def _capability_warning(method: ProviderAuthMethod, state: WizardState) -> str:
    """One-line capability note appended to a choice description, or ""."""
    if method is ProviderAuthMethod.CLIPROXY_OAUTH:
        from ..environment import docker_available

        # A known management endpoint (hydrated reconfigure or a
        # --cliproxy-management-url flag) means no local deploy is needed.
        if not docker_available() and not state.cliproxy_management_url:
            return (
                "No Docker detected here: you will need an existing "
                "CLIProxy endpoint URL."
            )
    if method is ProviderAuthMethod.LOCAL_MODEL:
        if shutil.which("ollama") is None:
            return "Ollama not detected: install it from ollama.com first."
    return ""


def _auth_choices(state: WizardState) -> list[Choice]:
    out: list[Choice] = []
    for method in PROVIDER_AUTH_METHOD_ORDER:
        meta = PROVIDER_AUTH_METHOD_CHOICES[method]
        description = meta.description
        warning = _capability_warning(method, state)
        if warning:
            description = f"{description} Warning: {warning}"
        out.append(Choice(value=method, label=meta.label, description=description))
    return out


class AuthMethodStep(SingleSelectStep):
    """Single-select between the API-key path, the CLIProxy branch, and local."""

    def action_skip(self) -> None:
        # Skipping pins the API-key method so downstream `applies`
        # predicates see a definite branch, then advances.
        self.show_error("")
        self._store(self.state, ProviderAuthMethod.API_KEY)
        self._wizard.advance()


def store_auth_method(state: WizardState, value: Any) -> None:
    """Record the branch; the local branch also pins its provider.

    The local branch never shows the provider picker, so the pinning that
    `ProviderStep.collect` would do happens here, including its
    provider-switch hygiene: connection details and the API key belong to the
    previously chosen provider, and leaking a stale key would write it into
    OLLAMA_API_KEY at finalize.
    """
    state.auth_method = value
    if value is ProviderAuthMethod.LOCAL_MODEL:
        # Clear only on a switch from a DIFFERENT, previously-set provider.
        # With no provider chosen yet, flag-provided values are the user's
        # intent for THIS branch (e.g. `--base-url http://box:11434` naming a
        # remote Ollama) and must survive the pick.
        if state.provider is not None and state.provider != LOCAL_MODEL_PROVIDER:
            state.api_key = ""
            state.api_mode = ""
            state.base_url = ""
        state.provider = LOCAL_MODEL_PROVIDER


def make_auth_method_step() -> Step:
    def get_initial(state: WizardState) -> ProviderAuthMethod:
        method = state.auth_method or ProviderAuthMethod.API_KEY
        # Legacy per-provider values render as the generic branch.
        if legacy_cliproxy_provider(method) is not None:
            return ProviderAuthMethod.CLIPROXY_OAUTH
        return method

    def build(wizard: "SetupWizardApp", number: int, total: int) -> AuthMethodStep:
        # Built per-show so the choices see the hydrated state (the CLIProxy
        # capability warning consults the recorded management endpoint).
        return AuthMethodStep(
            wizard,
            number,
            total,
            step_id="auth_method",
            title="How should Nymeria reach your LLM provider?",
            choices=_auth_choices(wizard.state),
            get_initial=get_initial,
            store=store_auth_method,
            note=(
                "Direct API keys are the simple, terms-of-service-clean "
                "path, and several providers have free tiers. Subscription "
                "login routes an existing AI subscription through a "
                "CLIProxy deployment (a disclaimer follows). A local model "
                "needs no account at all."
            ),
        )

    return Step(id="auth_method", applies=lambda _state: True, build=build)


def is_cliproxy_auth(state: WizardState) -> bool:
    """True when the wizard is on the CLIProxy subscription branch."""
    return state.auth_method_is_cliproxy()


__all__ = [
    "make_auth_method_step",
    "AuthMethodStep",
    "is_cliproxy_auth",
    "store_auth_method",
    "LOCAL_MODEL_PROVIDER",
]
