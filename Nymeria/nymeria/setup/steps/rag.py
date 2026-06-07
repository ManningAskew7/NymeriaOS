"""RAG embedder + reranker selection steps for the first-run wizard.

Two screens: pick an embedding model (and authenticate), then pick a reranker
(authenticating only when the embedding key cannot be reused, e.g. Voyage embed
plus Voyage rerank reuses one key). Options are grouped premium / value / local
with recommendations from an internal retrieval eval on a blended agentic corpus;
the catalog lives in ``setup/rag_catalog.py``. The reranker step only appears once
an embedder is chosen, so skipping RAG (Ctrl+S on the embedder) skips both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.widgets import Input, RadioSet, Static

from ..nav import Step
from ..rag_catalog import (
    EMBEDDERS,
    RERANKERS,
    TIER_LABELS,
    EmbedderOption,
    RerankerOption,
    get_embedder,
)
from .base import (
    CircleRadioButton,
    SelectingRadioSet,
    WizardStep,
    commit_radio_highlight,
)

if TYPE_CHECKING:
    from ..app import SetupWizardApp


def _tagged_label(option) -> str:
    tag = TIER_LABELS.get(option.tier, option.tier.title())
    label = f"[{tag}] {option.label}"
    if getattr(option, "recommended", False):
        label += " (recommended)"
    return label


class EmbedderStep(WizardStep):
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
                "free and private. Recommendations are from a retrieval eval on a "
                "real agentic corpus. Ctrl+S skips RAG setup."
            ),
        )
        self._options: list[EmbedderOption] = list(EMBEDDERS)

    def compose_body(self) -> ComposeResult:
        initial = self.state.embedder or self._options[0].id
        yield SelectingRadioSet(
            *[
                CircleRadioButton(_tagged_label(o), value=(o.id == initial))
                for o in self._options
            ]
        )
        yield Static("", id="choice-desc")
        yield Static("API key", classes="field-label", id="key-label")
        yield Input(
            value=self.state.optional_env.get("EMBEDDING_API_KEY", ""),
            password=True,
            id="rag-key",
        )
        yield Static("", id="key-status")

    def on_mount(self) -> None:
        self.query_one(SelectingRadioSet).focus()
        self.call_after_refresh(self._sync_on_entry)

    def _sync_on_entry(self) -> None:
        radio_set = self.query_one(SelectingRadioSet)
        radio_set.align_cursor_to_selection()
        idx = getattr(radio_set, "_selected", None)
        if not isinstance(idx, int) or idx < 0:
            idx = radio_set.pressed_index
        self._update_for(idx)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._update_for(event.index)

    def _update_for(self, idx: int) -> None:
        if not (0 <= idx < len(self._options)):
            return
        opt = self._options[idx]
        self.query_one("#choice-desc", Static).update(opt.description)
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
        idx = commit_radio_highlight(self.query_one(RadioSet))
        if not (0 <= idx < len(self._options)):
            self.show_error("Select an embedding model, then press Enter.")
            return False
        opt = self._options[idx]
        key = self.query_one("#rag-key", Input).value.strip()
        if opt.requires_key and not key:
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
        return True


class RerankerStep(WizardStep):
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
                "little latency, so it is optional. It only asks for a key when it "
                "cannot reuse the embedding key you just entered."
            ),
        )
        self._options: list[RerankerOption] = list(RERANKERS)

    def compose_body(self) -> ComposeResult:
        initial = self.state.reranker or self._options[0].id
        yield SelectingRadioSet(
            *[
                CircleRadioButton(_tagged_label(o), value=(o.id == initial))
                for o in self._options
            ]
        )
        yield Static("", id="choice-desc")
        yield Static("API key", classes="field-label", id="key-label")
        yield Input(value="", password=True, id="rag-key")
        yield Static("", id="key-status")

    def on_mount(self) -> None:
        self.query_one(SelectingRadioSet).focus()
        self.call_after_refresh(self._sync_on_entry)

    def _sync_on_entry(self) -> None:
        radio_set = self.query_one(SelectingRadioSet)
        radio_set.align_cursor_to_selection()
        idx = getattr(radio_set, "_selected", None)
        if not isinstance(idx, int) or idx < 0:
            idx = radio_set.pressed_index
        self._update_for(idx)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._update_for(event.index)

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

    def _update_for(self, idx: int) -> None:
        if not (0 <= idx < len(self._options)):
            return
        opt = self._options[idx]
        self.query_one("#choice-desc", Static).update(opt.description)
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
        idx = commit_radio_highlight(self.query_one(RadioSet))
        if not (0 <= idx < len(self._options)):
            self.show_error("Select an option, then press Enter.")
            return False
        opt = self._options[idx]
        mode, _ = self._key_mode(opt)
        key = self.query_one("#rag-key", Input).value.strip()
        if mode == "need" and not key:
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
