"""RAG embedder + reranker selection steps for the first-run wizard.

Two screens: pick an embedding model (and authenticate), then pick a reranker
(authenticating only when the embedding key cannot be reused, e.g. Voyage embed
plus Voyage rerank reuses one key). Options are grouped premium / value / local
with recommendations from an internal retrieval eval on a blended agentic corpus;
the catalog lives in ``setup/rag_catalog.py``. The reranker step only appears once
an embedder is chosen, so skipping RAG (Ctrl+S on the embedder) skips both.

Both screens are `FormStep`s: arrows move focus across the model list, the API key
field, and (embedder) the Hybrid / Vector-only retrieval choice; Space selects the
focused option; Enter locks it in and, when a chosen cloud model/reranker has an
empty key, focuses that key field with an error, else advances. Key-field
visibility and the description follow the SELECTED model / FOCUSED option.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, RadioButton, Static

from ..nav import Step
from ..rag_catalog import (
    EMBEDDERS,
    RERANKERS,
    TIER_LABELS,
    EmbedderOption,
    RerankerOption,
    get_embedder,
)
from .base import CircleRadioButton, FormStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp


def _tagged_label(option) -> str:
    tag = TIER_LABELS.get(option.tier, option.tier.title())
    label = f"[{tag}] {option.label}"
    if getattr(option, "recommended", False):
        label += " (recommended)"
    return label


class EmbedderStep(FormStep):
    """Pick the embedding model for semantic memory, and authenticate it."""

    def __init__(self, wizard: "SetupWizardApp", number: int, total: int) -> None:
        super().__init__(
            wizard,
            number,
            total,
            step_id="embedder",
            title="Semantic memory: embedding model",
            note=(
                "rag_search embeds your conversations, tool results, and notes for "
                "recall. Premium = best quality, Value = best per dollar, Local = "
                "free and private. Arrow through the fields, Space to pick, Enter to "
                "confirm. Ctrl+S skips RAG setup."
            ),
        )
        self._options: list[EmbedderOption] = list(EMBEDDERS)

    def compose_body(self) -> ComposeResult:
        initial = self.state.embedder or self._options[0].id
        yield Static("Embedding model", classes="field-label")
        with Vertical(classes="radio-group", id="model-group"):
            for o in self._options:
                yield CircleRadioButton(_tagged_label(o), value=(o.id == initial))
        yield Static("", id="choice-desc")
        yield Static("API key", classes="field-label", id="key-label")
        yield Input(
            value=self.state.optional_env.get("EMBEDDING_API_KEY", ""),
            password=True,
            id="rag-key",
        )
        yield Static("", id="key-status")
        yield Static("Retrieval", classes="field-label")
        with Vertical(classes="radio-group", id="retrieval-group"):
            yield CircleRadioButton(
                "Hybrid search (BM25 + vector)",
                value=(self.state.rag_retrieval_mode != "vector"),
            )
            yield CircleRadioButton(
                "Vector-only", value=(self.state.rag_retrieval_mode == "vector")
            )
        yield Static(
            "Hybrid keeps a BM25 keyword failsafe. Vector-only is often higher "
            "quality but relies entirely on the embedder.",
            id="retrieval-help",
        )

    def _model_buttons(self) -> list[RadioButton]:
        return list(self.query_one("#model-group").query(RadioButton))

    def _retrieval_buttons(self) -> list[RadioButton]:
        return list(self.query_one("#retrieval-group").query(RadioButton))

    def on_mount(self) -> None:
        buttons = self._model_buttons()
        selected = next((b for b in buttons if b.value), buttons[0] if buttons else None)
        if selected is not None:
            i = buttons.index(selected)
            if 0 <= i < len(self._options):
                self._apply_model_visibility(self._options[i])
            selected.focus()
        self.call_after_refresh(self._refresh_focus_view)

    def _sync_description(self, focused: object) -> None:
        buttons = self._model_buttons()
        if focused in buttons:
            i = buttons.index(focused)  # type: ignore[arg-type]
            if 0 <= i < len(self._options):
                self.query_one("#choice-desc", Static).update(self._options[i].description)

    def _on_single_select(self, group: object, button: RadioButton) -> None:
        if getattr(group, "id", None) != "model-group":
            return
        buttons = self._model_buttons()
        if button in buttons:
            i = buttons.index(button)
            if 0 <= i < len(self._options):
                self._apply_model_visibility(self._options[i])

    def _apply_model_visibility(self, opt: EmbedderOption) -> None:
        key_input = self.query_one("#rag-key", Input)
        label = self.query_one("#key-label", Static)
        status = self.query_one("#key-status", Static)
        if opt.requires_key:
            key_input.display = True
            label.display = True
            label.update(opt.key_label)
            key_input.placeholder = f"Paste your {opt.key_label}"
            status.update("")
        else:
            key_input.display = False
            label.display = False
            status.update(
                "No API key needed: runs on this machine "
                "(install the optional local-rag extra)."
            )

    def collect(self) -> bool:
        buttons = self._model_buttons()
        sel = next((i for i, b in enumerate(buttons) if b.value), None)
        if sel is None or sel >= len(self._options):
            self.show_error("Select an embedding model, then press Enter.")
            return False
        opt = self._options[sel]
        key = self.query_one("#rag-key", Input).value.strip()
        if opt.requires_key and not key:
            self.query_one("#rag-key", Input).focus()
            self.show_error(
                f"Enter your {opt.key_label}, or press Ctrl+S to skip RAG setup."
            )
            return False
        self.state.embedder = opt.id
        if opt.requires_key:
            self.state.optional_env["EMBEDDING_API_KEY"] = key
        else:
            # Local embedder needs no key; drop any stale one from a prior choice.
            self.state.optional_env.pop("EMBEDDING_API_KEY", None)
        rbtns = self._retrieval_buttons()
        vector = len(rbtns) > 1 and bool(rbtns[1].value)
        self.state.rag_retrieval_mode = "vector" if vector else "hybrid"
        return True


class RerankerStep(FormStep):
    """Pick a reranker; ask for a key only when the embedding key can't be reused."""

    def __init__(self, wizard: "SetupWizardApp", number: int, total: int) -> None:
        super().__init__(
            wizard,
            number,
            total,
            step_id="reranker",
            title="Semantic memory: reranker (optional)",
            note=(
                "A reranker reorders rag_search results for accuracy. It adds a "
                "little latency, so it is optional. Arrow to one, Space to pick, "
                "Enter to confirm; it only asks for a key when it cannot reuse the "
                "embedding key you just entered."
            ),
        )
        self._options: list[RerankerOption] = list(RERANKERS)

    def compose_body(self) -> ComposeResult:
        initial = self.state.reranker or self._options[0].id
        with Vertical(classes="radio-group", id="reranker-group"):
            for o in self._options:
                yield CircleRadioButton(_tagged_label(o), value=(o.id == initial))
        yield Static("", id="choice-desc")
        yield Static("API key", classes="field-label", id="key-label")
        yield Input(value="", password=True, id="rag-key")
        yield Static("", id="key-status")

    def _reranker_buttons(self) -> list[RadioButton]:
        return list(self.query_one("#reranker-group").query(RadioButton))

    def on_mount(self) -> None:
        buttons = self._reranker_buttons()
        selected = next((b for b in buttons if b.value), buttons[0] if buttons else None)
        if selected is not None:
            i = buttons.index(selected)
            if 0 <= i < len(self._options):
                self._apply_visibility(self._options[i])
            selected.focus()
        self.call_after_refresh(self._refresh_focus_view)

    def _sync_description(self, focused: object) -> None:
        buttons = self._reranker_buttons()
        if focused in buttons:
            i = buttons.index(focused)  # type: ignore[arg-type]
            if 0 <= i < len(self._options):
                self.query_one("#choice-desc", Static).update(self._options[i].description)

    def _on_single_select(self, group: object, button: RadioButton) -> None:
        if getattr(group, "id", None) != "reranker-group":
            return
        buttons = self._reranker_buttons()
        if button in buttons:
            i = buttons.index(button)
            if 0 <= i < len(self._options):
                self._apply_visibility(self._options[i])

    def _key_mode(self, opt: RerankerOption) -> tuple[str, str]:
        """Return (mode, detail): 'none' (no key), 'reuse' (share embedding key),
        or 'need' (prompt for a key)."""
        if not opt.requires_key:
            return ("none", "")
        emb = get_embedder(self.state.embedder)
        if (
            emb is not None
            and emb.key_vendor
            and emb.key_vendor == opt.key_vendor
            and self.state.optional_env.get("EMBEDDING_API_KEY")
        ):
            return ("reuse", emb.key_vendor)
        return ("need", opt.key_label)

    def _apply_visibility(self, opt: RerankerOption) -> None:
        mode, detail = self._key_mode(opt)
        key_input = self.query_one("#rag-key", Input)
        label = self.query_one("#key-label", Static)
        status = self.query_one("#key-status", Static)
        if mode == "need":
            key_input.display = True
            label.display = True
            label.update(opt.key_label)
            key_input.placeholder = f"Paste your {opt.key_label}"
            status.update("")
        else:
            key_input.display = False
            label.display = False
            if mode == "reuse":
                status.update(f"Reuses your {detail} API key from the previous step.")
            elif opt.provider == "none":
                status.update("Vector-only: no reranker, lowest latency.")
            else:
                status.update(
                    "No API key needed: runs on this machine "
                    "(install the optional local-rag extra)."
                )

    def collect(self) -> bool:
        buttons = self._reranker_buttons()
        sel = next((i for i, b in enumerate(buttons) if b.value), None)
        if sel is None or sel >= len(self._options):
            self.show_error("Select an option, then press Enter.")
            return False
        opt = self._options[sel]
        mode, _ = self._key_mode(opt)
        key = self.query_one("#rag-key", Input).value.strip()
        if mode == "need" and not key:
            self.query_one("#rag-key", Input).focus()
            self.show_error(
                f"Enter your {opt.key_label}, or press Ctrl+S to skip the reranker."
            )
            return False
        self.state.reranker = opt.id
        if mode == "reuse":
            self.state.optional_env["RAG_RERANK_API_KEY"] = self.state.optional_env[
                "EMBEDDING_API_KEY"
            ]
        elif mode == "need":
            self.state.optional_env["RAG_RERANK_API_KEY"] = key
        else:
            self.state.optional_env.pop("RAG_RERANK_API_KEY", None)
        return True


def make_embedder_step() -> Step:
    """Embedding-model picker (always applies)."""
    return Step(
        id="embedder",
        applies=lambda _state: True,
        build=lambda wizard, number, total: EmbedderStep(wizard, number, total),
    )


def make_reranker_step() -> Step:
    """Reranker picker; only shown once an embedder has been chosen."""
    return Step(
        id="reranker",
        applies=lambda state: state.embedder is not None,
        build=lambda wizard, number, total: RerankerStep(wizard, number, total),
    )


__all__ = ["make_embedder_step", "make_reranker_step"]
