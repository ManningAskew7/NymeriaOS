"""Step 2: LLM provider + API key, plus a conditional connection step.

The provider list is the canonical registry (`config/llm_providers.py`), grouped
by backend tier exactly like the desktop global-settings picker: native reasoning
first, then gateways, then unverified. The model is chosen on the next screen
(`ModelStep`), so this screen only collects the provider and its API key. OAuth /
CLIProxy routes are deferred, so only the direct API-key providers from the
registry appear.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape
from textual.app import ComposeResult
from textual.widgets import Input, RadioSet, Static

from ...config.llm_providers import get_llm_provider_spec
from ...onboarding import ProviderAuthMethod
from ..nav import Step
from ..providers import (
    grouped_provider_specs,
    key_prefix_for_spec,
    valid_key_format_for_spec,
)
from ..state import WizardState
from ..widgets import ListItem, SearchableList
from .base import (
    CircleRadioButton,
    SelectingRadioSet,
    WizardStep,
    commit_radio_highlight,
)

if TYPE_CHECKING:
    from ..app import SetupWizardApp

_API_MODE_CHOICES = [
    ("", "Default (inherit global)"),
    ("responses", "Responses API"),
    ("chat_completions", "Chat Completions"),
]


def _provider_items() -> list[ListItem]:
    """Flatten the tier-grouped registry into header + selectable rows.

    Provider notes are prose, so they are NOT row secondaries (those would wrap
    inside the list); the highlighted provider's note renders below the list.
    """
    items: list[ListItem] = []
    for tier_label, specs in grouped_provider_specs():
        items.append(ListItem(value="", primary=tier_label, is_header=True))
        for spec in specs:
            items.append(
                ListItem(
                    value=spec.id,
                    primary=spec.label,
                    search_text=f"{spec.label} {spec.id} {tier_label}",
                )
            )
    return items


def _provider_note(provider_id: str | None) -> str:
    spec = get_llm_provider_spec(provider_id) if provider_id else None
    # Escaped: the note Static parses markup, and registry notes are prose.
    return escape(spec.notes_for_user) if spec else ""


def _provider_signup(provider_id: str | None) -> str:
    """The highlighted provider's "get a key" guidance, or "" when uncurated.

    Sourced from the registry spec (beta-readiness 03) so the TUI and any
    future GUI wizard render from one place. Escaped like the note: guidance
    is prose and the Static parses markup.
    """
    spec = get_llm_provider_spec(provider_id) if provider_id else None
    if spec is None or not (spec.signup_guidance or spec.signup_url):
        return ""
    parts: list[str] = []
    if spec.signup_url:
        parts.append(f"Get a key: {spec.signup_url}")
    if spec.signup_guidance:
        parts.append(spec.signup_guidance)
    return escape("\n".join(parts))


def _first_provider_id() -> str | None:
    groups = grouped_provider_specs()
    if groups and groups[0][1]:
        return groups[0][1][0].id
    return None


def _key_placeholder(provider_id: str | None) -> str:
    spec = get_llm_provider_spec(provider_id) if provider_id else None
    if spec is None:
        return "paste your API key"
    if not spec.requires_api_key:
        return "no key required (local / optional)"
    prefix = key_prefix_for_spec(spec)
    return f"{prefix}..." if prefix else f"paste your {spec.label} API key"


def _provider_key_present(state: WizardState, provider_id: str | None) -> bool:
    """True when this provider's key env var is already set on disk (reconfigure)."""
    spec = get_llm_provider_spec(provider_id) if provider_id else None
    return bool(
        spec
        and spec.api_key_env_vars
        and spec.api_key_env_vars[0] in getattr(state, "present_env_keys", set())
    )


class ProviderStep(WizardStep):
    """Searchable provider picker + API key in one screen."""

    def compose_body(self) -> ComposeResult:
        initial = self.state.provider or _first_provider_id()
        yield Static("Provider", classes="field-label")
        yield SearchableList(
            _provider_items(),
            placeholder="Type to filter providers...",
            initial_value=initial,
            search_id="provider-search",
            list_id="provider-options",
        )
        note_text = _provider_note(initial)
        note = Static(note_text, id="provider-note")
        note.display = bool(note_text)
        yield note
        signup_text = _provider_signup(initial)
        signup = Static(signup_text, id="provider-signup")
        signup.display = bool(signup_text)
        yield signup
        yield Static("API key", classes="field-label")
        yield Input(
            value=self.state.api_key,
            password=True,
            placeholder=self._key_field_placeholder(initial),
            id="api-key",
        )

    def _key_field_placeholder(self, provider_id: str | None) -> str:
        # Reconfigure: when a key for this provider is already on disk, a blank
        # field keeps it; advertise that instead of asking for a fresh paste.
        if _provider_key_present(self.state, provider_id):
            return "leave blank to keep the existing key"
        return _key_placeholder(provider_id)

    def on_mount(self) -> None:
        self.query_one(SearchableList).focus()

    def on_searchable_list_highlighted(self, event: SearchableList.Highlighted) -> None:
        # Update the key-field hint and the provider note to match the
        # highlighted provider. We do not write state.provider here, so
        # skipping the step leaves it unset.
        if event.value:
            self.query_one("#api-key", Input).placeholder = self._key_field_placeholder(
                event.value
            )
        # value is None when the filter has no matches: clear the note so it
        # cannot describe a provider that is no longer shown. Hidden when
        # empty, so providers without notes do not leave a gap.
        note_text = _provider_note(event.value)
        note = self.query_one("#provider-note", Static)
        note.update(note_text)
        note.display = bool(note_text)
        # The "get a key" panel follows the highlight the same way.
        signup_text = _provider_signup(event.value)
        signup = self.query_one("#provider-signup", Static)
        signup.update(signup_text)
        signup.display = bool(signup_text)

    def on_searchable_list_selected(self, event: SearchableList.Selected) -> None:
        # Mouse click on a provider: update the hint and move to the key field.
        self.query_one("#api-key", Input).placeholder = self._key_field_placeholder(
            event.value
        )
        self.query_one("#api-key", Input).focus()

    def action_next(self) -> None:
        # Enter inside the picker moves into the key field (so selecting a
        # provider never skips key entry). Enter from the key field advances.
        self.show_error("")
        if self.query_one(SearchableList).has_focus_within:
            self.query_one("#api-key", Input).focus()
            return
        if self.collect():
            self._wizard.advance()

    def collect(self) -> bool:
        picker = self.query_one(SearchableList)
        provider_id = picker.selected_value or self.state.provider
        spec = get_llm_provider_spec(provider_id) if provider_id else None
        if spec is None:
            self.show_error("Select a provider.")
            return False
        api_key = self.query_one("#api-key", Input).value.strip()
        # Reconfigure: a blank field keeps the key already on disk for this
        # provider, so do not force a re-paste (finalize preserves the line).
        keep_existing = not api_key and _provider_key_present(self.state, spec.id)
        if spec.requires_api_key and not api_key and not keep_existing:
            self.show_error("Enter an API key, or press Ctrl+S to skip this step.")
            return False
        if api_key:
            ok, prefix = valid_key_format_for_spec(spec, api_key)
            if not ok and prefix:
                self.show_error(f"That key should start with `{prefix}`.")
                return False
        if self.state.provider != spec.id:
            # Connection details belong to the previously chosen provider. On a
            # switch, clear them so a back-nav that skips the (now inapplicable)
            # connection step cannot leak a stale api_mode/base_url into config.
            self.state.api_mode = ""
            self.state.base_url = ""
        self.state.provider = spec.id
        self.state.api_key = api_key
        return True


class ConnectionStep(WizardStep):
    """Optional API mode + base URL, shown when the provider needs them."""

    def compose_body(self) -> ComposeResult:
        spec = get_llm_provider_spec(self.state.provider)
        if spec is not None and spec.supports_responses:
            yield Static("API mode", classes="field-label")
            buttons = [
                CircleRadioButton(label, value=(value == self.state.api_mode))
                for value, label in _API_MODE_CHOICES
            ]
            yield SelectingRadioSet(*buttons, id="api-mode-set")
        placeholder = (spec.default_base_url if spec else None) or "Provider default"
        yield Static("API base URL (optional)", classes="field-label")
        yield Input(
            value=self.state.base_url,
            placeholder=placeholder,
            id="base-url",
        )

    def on_mount(self) -> None:
        api_mode_sets = list(self.query("#api-mode-set").results(SelectingRadioSet))
        if api_mode_sets:
            radio_set = api_mode_sets[0]
            radio_set.focus()
            # Land the highlight on the stored API mode so the dot follows from
            # the first keypress (see SelectingRadioSet).
            self.call_after_refresh(radio_set.align_cursor_to_selection)
        else:
            self.query_one("#base-url", Input).focus()

    def collect(self) -> bool:
        api_mode_sets = list(self.query("#api-mode-set").results(RadioSet))
        if api_mode_sets:
            idx = commit_radio_highlight(api_mode_sets[0])
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
            note="Type to filter, arrow keys to pick, Enter to continue. Paste the API key, then Enter. Ctrl+S skips this step.",
            hint="type filter   down to list   enter next   ctrl+s skip   esc back",
        )

    def applies(state: WizardState) -> bool:
        # Only the direct API-key path collects a provider here. The auth step
        # gates the deferred OAuth methods, so this is true in practice today,
        # but the predicate keeps the future OAuth branch correct.
        return state.auth_method is ProviderAuthMethod.API_KEY

    return Step(id="provider", applies=applies, build=build)


def make_connection_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> ConnectionStep:
        return ConnectionStep(
            wizard,
            number,
            total,
            step_id="connection",
            title="Connection options",
            note="Connection details for the provider you chose.",
        )

    def applies(state: WizardState) -> bool:
        spec = get_llm_provider_spec(state.provider)
        if not state.provider or spec is None:
            return False
        return bool(
            spec.supports_responses
            or spec.requires_base_url
            or not spec.requires_api_key
            or spec.default_base_url is None
        )

    return Step(id="connection", applies=applies, build=build)


__all__ = ["make_provider_step", "make_connection_step", "ProviderStep", "ConnectionStep"]
