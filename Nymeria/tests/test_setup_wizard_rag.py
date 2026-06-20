"""Expanded RAG embedder/reranker catalog.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import asyncio


# --- expanded RAG catalog ---------------------------------------------------


# Providers the production backend can actually drive (memory_index.py embedders /
# rag_quality.py rerankers). An option whose provider is not here would write
# config the backend cannot honor, so the catalog must never offer one.
_BACKEND_EMBED_PROVIDERS = {"openai", "cohere", "gemini", "local"}


_BACKEND_RERANK_PROVIDERS = {"voyage", "cohere", "zeroentropy", "local", "none"}


def test_rag_catalog_expanded_options_are_backend_supported_and_metricked():
    from nymeria.setup.rag_catalog import (
        EMBEDDERS,
        RECOMMENDED_COMBOS,
        RERANKERS,
        get_embedder,
        get_reranker,
        recommended_reranker_for,
    )

    emb_ids = {o.id for o in EMBEDDERS}
    rer_ids = {o.id for o in RERANKERS}
    # The new same-provider options are present.
    assert {"premium-cohere-1536", "premium-voyage-large", "value-openai-small"} <= emb_ids
    assert {"premium-cohere-pro", "local-ettin-32m"} <= rer_ids
    # First options are stable: pilots and the shape-aware defaults rely on them.
    assert EMBEDDERS[0].id == "premium-cohere"
    assert RERANKERS[0].id == "none"
    # Every provider is one the backend can drive (guards against e.g. a Jina
    # reranker, which the eval tested but production has no endpoint for).
    assert all(o.provider in _BACKEND_EMBED_PROVIDERS for o in EMBEDDERS)
    assert all(o.provider in _BACKEND_RERANK_PROVIDERS for o in RERANKERS)
    assert all(o.provider != "jina" for o in RERANKERS)
    # Every option carries a description; every non-baseline one carries a metric.
    for o in EMBEDDERS:
        assert o.description and o.metrics
    for o in RERANKERS:
        assert o.description
        if o.provider != "none":
            assert o.metrics
    # Every non-baseline option also carries the short inline eval verdict.
    for o in EMBEDDERS:
        assert o.eval_tag
    for o in RERANKERS:
        if o.provider != "none":
            assert o.eval_tag
    # Recommended combos reference real ids.
    for _tier, emb_id, rer_id, _blurb in RECOMMENDED_COMBOS:
        assert get_embedder(emb_id) is not None
        assert get_reranker(rer_id) is not None
    # Every embedder maps to a recommended reranker that exists.
    for o in EMBEDDERS:
        rec = recommended_reranker_for(o.id)
        assert rec is not None, o.id
        assert get_reranker(rec[0]) is not None


def test_rag_picker_rows_carry_inline_eval_and_fit_standard_width():
    """The eval verdict rides dim at the end of each picker row (horizontal
    space is free; vertical space is what made these steps scroll). Rows must
    stay inside a 110-column terminal: body padding (2+2), the group box
    border+padding (2+2), the per-button padding (1+1), and ToggleButton's
    glyph plus label pad (3) leave 97 usable label cells."""
    from textual.content import Content

    from nymeria.setup.rag_catalog import EMBEDDERS, RERANKERS
    from nymeria.setup.steps.rag import _name_width, _tagged_label

    for options in (EMBEDDERS, RERANKERS):
        width = _name_width(options)
        tag_columns = set()
        for option in options:
            plain = Content.from_markup(_tagged_label(option, width)).plain
            if option.eval_tag:
                assert option.eval_tag in plain
                tag_columns.add(plain.index(option.eval_tag))
            # No option carries a "(recommended)" tag any more: the row shows the
            # label, tier, and inline eval verdict only.
            assert "(recommended)" not in plain
            assert len(plain) <= 97, f"{option.id} row is {len(plain)}: {plain!r}"
        # The verdicts line up as one table column across the list.
        assert len(tag_columns) == 1


def test_rag_env_for_state_handles_new_embedder_and_reranker():
    from nymeria.setup.rag_catalog import rag_env_for_state
    from nymeria.setup.state import WizardState

    # Voyage 4 large embedder + Cohere rerank-v4.0-pro.
    env = rag_env_for_state(
        WizardState(embedder="premium-voyage-large", reranker="premium-cohere-pro")
    )
    assert env["EMBEDDING_PROVIDER"] == "openai"
    assert env["EMBEDDING_MODEL"] == "voyage-4-large"
    assert env["EMBEDDING_DIMENSIONS"] == "1024"
    assert env["EMBEDDING_BASE_URL"] == "https://api.voyageai.com/v1"
    assert env["EMBEDDING_INPUT_TYPE"] == "voyage"
    assert env["RAG_RERANK_ENABLED"] == "true"
    assert env["RAG_RERANK_PROVIDER"] == "cohere"
    assert env["RAG_RERANK_MODEL"] == "rerank-v4.0-pro"

    # OpenAI text-embedding-3-small uses the default OpenAI endpoint (no base URL).
    env2 = rag_env_for_state(WizardState(embedder="value-openai-small"))
    assert env2["EMBEDDING_PROVIDER"] == "openai"
    assert env2["EMBEDDING_MODEL"] == "text-embedding-3-small"
    assert env2["EMBEDDING_DIMENSIONS"] == "1536"
    assert "EMBEDDING_BASE_URL" not in env2

    # Cohere 1536-d variant emits the full width.
    env3 = rag_env_for_state(WizardState(embedder="premium-cohere-1536"))
    assert env3["EMBEDDING_PROVIDER"] == "cohere"
    assert env3["EMBEDDING_DIMENSIONS"] == "1536"

    # Local Ettin 32m reranker.
    env4 = rag_env_for_state(WizardState(embedder="local-granite", reranker="local-ettin-32m"))
    assert env4["RAG_RERANK_PROVIDER"] == "local"
    assert env4["RAG_RERANK_MODEL"] == "cross-encoder/ettin-reranker-32m-v1"


def test_wizard_pilot_reranker_reuses_cohere_key_for_cohere_pro():
    """Cohere embedder + Cohere rerank-v4.0-pro share one key: the reranker step
    reuses the embedding key (no second prompt) and stores it for the reranker."""
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.rag_catalog import RERANKERS
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.rag import make_reranker_step

    cohere_pro_idx = next(
        i for i, o in enumerate(RERANKERS) if o.id == "premium-cohere-pro"
    )

    async def drive() -> WizardState:
        state = WizardState(
            embedder="premium-cohere",
            optional_env={"EMBEDDING_API_KEY": "co-secret"},
        )
        app = SetupWizardApp(state, steps=[make_reranker_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            for _ in range(cohere_pro_idx):
                await pilot.press("down")
            await pilot.press("space")  # select Cohere rerank-v4.0-pro
            await pilot.pause()
            assert not scr.query_one("#rag-key", Input).display  # key reused, hidden
            assert scr.query_one("#combo-rec")  # the pairing hint rendered
            await pilot.press("enter")
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.reranker == "premium-cohere-pro"
    # The Cohere embedding key was reused for the reranker, not re-prompted.
    assert state.optional_env["RAG_RERANK_API_KEY"] == "co-secret"
