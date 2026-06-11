"""Consolidated credential step for the selected tool backends.

After the web_search / fetch_url / image_gen family pickers, this single screen
collects the API key (or base URL) each chosen backend needs, so a picked tool
can actually run. The set of fields is derived from the selections via
``tool_keys.required_backend_credentials`` (which already drops keyless backends
like ``fetch_url_nymeria``, anything whose env var is already provided, and the
primary provider's own key). The step only appears when something needs
collecting, and any field may be left blank to add later (Ctrl+S skips the lot).
Modeled on ``EmbedderStep``'s key-input handling.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Input, Static

from ..nav import Step
from ..tool_keys import KeySpec, backend_keys_needed, required_backend_credentials
from .base import FormStep


def _field_id(spec: KeySpec) -> str:
    return f"backend-key-{spec.env_var.lower()}"


class BackendKeysStep(FormStep):
    """One labeled input per credential the chosen backends still need.

    Arrows move between the key inputs (FormStep); Enter steps to the next input
    and advances from the last one. Keys are optional, so nothing is required.
    """

    def _current_specs(self) -> list[KeySpec]:
        return required_backend_credentials(self.state)

    def compose_body(self) -> ComposeResult:
        specs = self._current_specs()
        if not specs:
            # Reachable only if selections changed after this step was scheduled.
            yield Static("No backend keys needed for your selections.")
            return
        yield Static(
            "Enter the API key for each selected backend. Leave any blank to add "
            "it later in settings."
        )
        for spec in specs:
            yield Static(spec.label, classes="field-label")
            yield Input(
                value=self.state.optional_env.get(spec.env_var, ""),
                password=(spec.kind not in ("url", "text")),
                placeholder=spec.placeholder or (
                    "https://your-searxng.example" if spec.kind == "url"
                    else f"Paste your {spec.label}"
                ),
                id=_field_id(spec),
            )
            if spec.note:
                yield Static(spec.note, classes="field-note")

    def on_mount(self) -> None:
        inputs = list(self.query(Input))
        if inputs:
            inputs[0].focus()
        self.call_after_refresh(self._refresh_focus_view)

    def action_next(self) -> None:
        # Enter steps to the next key field; advances from the last one. Keys are
        # optional, so there is nothing to validate.
        self.show_error("")
        inputs = list(self.query(Input))
        focused = self.focused
        if isinstance(focused, Input) and focused in inputs:
            idx = inputs.index(focused)
            if idx < len(inputs) - 1:
                inputs[idx + 1].focus()
                self._update_scroll_hint()
                return
        if self.collect():
            self._wizard.advance()

    def collect(self) -> bool:
        for spec in self._current_specs():
            try:
                value = self.query_one(f"#{_field_id(spec)}", Input).value.strip()
            except Exception:
                continue
            if value:
                self.state.optional_env[spec.env_var] = value
            else:
                # Blank clears a previously-entered value (e.g. from a flag).
                self.state.optional_env.pop(spec.env_var, None)
        return True


def make_backend_keys_step() -> Step:
    """Backend-credentials step; only shown when a selected backend needs a key."""
    return Step(
        id="backend_keys",
        applies=backend_keys_needed,
        build=lambda wizard, number, total: BackendKeysStep(
            wizard,
            number,
            total,
            step_id="backend_keys",
            title="Backend API keys",
            note="Keys for the search, fetch, image, and voice backends you selected.",
        ),
    )


__all__ = ["BackendKeysStep", "make_backend_keys_step"]
