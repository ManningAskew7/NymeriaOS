"""Step 3: choose the model, as a searchable list fetched live from the provider.

There is no backend running during `nymeria init`, so the model list is fetched
directly from the provider using the API key entered on the previous screen. The
search box doubles as a free-text field: if the fetch fails, returns nothing, or
the provider has no listable models, whatever the user types is used as the model
id. The provider's default model is pre-highlighted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.widgets import Static

from ...config.llm_providers import get_llm_provider_spec
from ..nav import Step
from ..providers import ModelChoice, fetch_models_for_spec
from ..widgets import ListItem, SearchableList
from .base import WizardStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp


def _context_label(context_length: int | None) -> str:
    if not context_length or context_length <= 0:
        return ""
    if context_length >= 1_000_000:
        return f"{context_length / 1_000_000:.1f}M ctx"
    if context_length >= 1_000:
        return f"{context_length // 1000}K ctx"
    return f"{context_length} ctx"


class ModelStep(WizardStep):
    """Searchable model list, fetched live using the provider + key just entered."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._models_by_id: dict[str, ModelChoice] = {}

    def compose_body(self) -> ComposeResult:
        spec = get_llm_provider_spec(self.state.provider)
        default_model = self.state.model or (spec.default_model if spec else None)
        yield Static("Model", classes="field-label")
        yield SearchableList(
            [],
            placeholder="Loading models...",
            initial_value=default_model,
            search_id="model-search",
            list_id="model-options",
            empty_text="No models listed - type the exact model id",
        )
        yield Static("", id="model-hint")

    def on_mount(self) -> None:
        self.query_one(SearchableList).focus()
        self._load_models()

    @work(exclusive=True)
    async def _load_models(self) -> None:
        spec = get_llm_provider_spec(self.state.provider)
        if spec is None:
            return
        models = await fetch_models_for_spec(
            spec, api_key=self.state.api_key, base_url=self.state.base_url or None
        )
        if not self.is_mounted:
            return
        picker = self.query_one(SearchableList)
        if models:
            self._models_by_id = {model.id: model for model in models}
            picker.set_items(
                [
                    ListItem(
                        value=model.id,
                        primary=model.id,
                        secondary=_context_label(model.context_length),
                    )
                    for model in models
                ]
            )
            picker.set_placeholder("Type to filter models...")
        else:
            self._models_by_id = {}
            picker.set_placeholder("type the exact model id")
            self.show_error(
                "Could not list models for this provider. "
                "Type the exact model id and press Enter."
            )

    def on_searchable_list_highlighted(self, event: SearchableList.Highlighted) -> None:
        choice = self._models_by_id.get(event.value or "")
        hint = _context_label(choice.context_length) if choice else ""
        self.query_one("#model-hint", Static).update(hint)

    def collect(self) -> bool:
        picker = self.query_one(SearchableList)
        spec = get_llm_provider_spec(self.state.provider)
        model = (picker.selected_value or picker.search_value or "").strip()
        if not model and spec is not None:
            model = (spec.default_model or "").strip()
        if not model:
            self.show_error("Enter a model id.")
            return False
        self.state.model = model
        return True


def make_model_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> ModelStep:
        return ModelStep(
            wizard,
            number,
            total,
            step_id="model",
            title="Choose a model",
            note="Models are listed live from the provider. Type to filter, or type an exact model id.",
            hint="type filter   down to list   enter next   ctrl+s skip   esc back",
        )

    return Step(id="model", applies=lambda state: bool(state.provider), build=build)


__all__ = ["make_model_step", "ModelStep"]
