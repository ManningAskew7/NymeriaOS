from __future__ import annotations

import hashlib
from pathlib import Path

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.core.tool_search_index import ToolSearchIndex
from nymeria.core.user_profile import UserProfileManager
from nymeria.tools.metadata import (
    CUSTOM_TOOL_METADATA,
    register_custom_tool_metadata,
    unregister_custom_tool_metadata,
)


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.profile_manager = UserProfileManager(data_dir)
        self.thread_config_manager = ThreadConfigManager(data_dir)

    def _get_team_scoped_callable_threads(
        self,
        *,
        user_id: str,
        caller_thread_id: str,
    ):
        owned = set(self.accounts_repo.list_threads_for_user(user_id))
        return self.thread_config_manager.list_callable_threads(
            owned_thread_ids=owned,
        )


def test_bm25_fallback_without_embedding_key_finds_browser_tools():
    index = ToolSearchIndex(openai_api_key=None)

    response = index.search("browser", user_id="default", top_k=5)

    assert response.mode == "bm25"
    assert response.warning and "EMBEDDING_API_KEY" in response.warning
    assert any(result.name.startswith("browser_") for result in response.results)


def test_cliproxy_gatekeeper_key_does_not_hit_embeddings():
    index = ToolSearchIndex(openai_api_key="cpx-local-test")

    response = index.search("browser", user_id="default", top_k=5)

    assert response.mode != "semantic"
    assert response.warning and "CLIProxy gatekeeper" in response.warning


def test_embedding_client_uses_bounded_timeout_and_no_retries():
    """A slow/unreachable embeddings endpoint must fail fast and degrade to
    keyword search instead of stalling the agent turn for the SDK default
    (~600s x 2 retries). Guards against regressing the client construction.
    """
    import openai

    from nymeria.core.tool_search_index import EMBED_REQUEST_TIMEOUT_SECONDS

    captured: dict[str, object] = {}

    class _RecordingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    original = openai.OpenAI
    openai.OpenAI = _RecordingClient
    try:
        index = ToolSearchIndex(openai_api_key="sk-real-looking-key")
        index._get_openai()
    finally:
        openai.OpenAI = original

    assert captured["timeout"] == EMBED_REQUEST_TIMEOUT_SECONDS
    assert captured["max_retries"] == 0


def test_fuzzy_fallback_handles_typo():
    index = ToolSearchIndex(openai_api_key=None)

    response = index.search("brwoser", user_id="default", top_k=5)

    assert response.mode == "fuzzy"
    assert any(result.name.startswith("browser_") for result in response.results)


def test_changed_tool_metadata_updates_catalog_fingerprint():
    index = ToolSearchIndex(openai_api_key=None)
    tool_id = "zz_price_probe"
    old = CUSTOM_TOOL_METADATA.get(tool_id)
    try:
        register_custom_tool_metadata(tool_id, "Alpha-only lookup")
        first = index.search("alpha", category="custom", user_id="default")
        first_fingerprint = index.catalog_fingerprint

        register_custom_tool_metadata(tool_id, "Beta-only lookup")
        index.mark_dirty()
        second = index.search("beta", category="custom", user_id="default")

        assert first.results[0].name == tool_id
        assert second.results[0].name == tool_id
        assert index.catalog_fingerprint != first_fingerprint
    finally:
        unregister_custom_tool_metadata(tool_id)
        if old is not None:
            CUSTOM_TOOL_METADATA[tool_id] = old


def test_developer_only_tools_are_hidden_from_non_admins():
    index = ToolSearchIndex(openai_api_key=None)

    user_response = index.search("hello_test", user_role="user", user_id="owner")
    admin_response = index.search("hello_test", user_role="admin", user_id="admin")

    assert not any(result.name == "hello_test" for result in user_response.results)
    assert any(result.name == "hello_test" for result in admin_response.results)


def test_thread_visible_callable_tools_are_indexed(tmp_path: Path):
    agent = FakeAgent(tmp_path)
    agent.accounts_repo.create_user("owner", "owner@example.com", "Owner")
    agent.accounts_repo.claim_thread("caller", "owner")
    agent.accounts_repo.claim_thread("helper", "owner")
    agent.thread_config_manager.save_config(ThreadConfig(thread_id="caller"))
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="helper",
            callable=True,
            callable_name="PriceHelper",
            callable_description="Look up supplier prices",
        )
    )
    index = ToolSearchIndex(openai_api_key=None)

    response = index.search(
        "supplier prices",
        user_id="owner",
        user_role="user",
        agent=agent,
        thread_id="caller",
    )

    assert any(
        result.name == "PriceHelper" and result.tool_type == "callable_thread"
        for result in response.results
    )


def _fake_embedder(dim: int = 8):
    """Deterministic offline embedder: text -> a fixed-width vector.

    Drives the semantic path without network calls; the real dimension check in
    ``_embed``/``_embed_texts`` is bypassed because those methods are
    monkeypatched, and in-memory cosine works on any consistent width.
    """

    def embed_one(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[:dim]]

    return embed_one


def test_search_never_triggers_full_catalog_embed():
    """The cold-start hang was search() embedding the whole catalog inline.
    Even with semantic configured but not yet warmed, search must serve keyword
    results and never call the embedder (neither the batch nor the single)."""
    index = ToolSearchIndex(openai_api_key=None)
    index._semantic_available = True  # pretend a real embedder is configured
    calls = {"texts": 0, "single": 0}

    def boom_texts(_texts):
        calls["texts"] += 1
        raise AssertionError("search must never embed the catalog inline")

    def boom_single(_text):
        calls["single"] += 1
        raise AssertionError("search must not embed the query before warm")

    index._embed_texts = boom_texts
    index._embed = boom_single

    response = index.search("browser", user_id="default", top_k=5)

    assert response.mode in {"bm25", "fuzzy", "substring"}
    assert "warming" in (response.warning or "")
    assert calls == {"texts": 0, "single": 0}


def test_semantic_requires_fully_embedded():
    index = ToolSearchIndex(openai_api_key=None)
    index._semantic_available = True
    embed = _fake_embedder()
    index._embed = lambda text: embed(text)
    index._embed_texts = lambda texts: [embed(t) for t in texts]

    # Before the warm: catalog not embedded -> keyword + warming notice.
    before = index.search("browser", user_id="default", top_k=5)
    assert before.mode != "semantic"
    assert "warming" in (before.warning or "")

    # After the warm: semantic ranking over the resident cache.
    index.warm_embeddings()
    assert index._fully_embedded
    after = index.search("browser", user_id="default", top_k=5)
    assert after.mode == "semantic"


def test_warming_warning_distinct_from_no_key_warning():
    no_key = ToolSearchIndex(openai_api_key=None)
    r1 = no_key.search("browser", user_id="default")
    assert "EMBEDDING_API_KEY" in (r1.warning or "")

    warming = ToolSearchIndex(openai_api_key=None)
    warming._semantic_available = True  # configured but not warmed yet
    r2 = warming.search("browser", user_id="default")
    assert "warming" in (r2.warning or "")
    assert "EMBEDDING_API_KEY" not in (r2.warning or "")


def test_mark_dirty_resets_fully_embedded_and_rewarm_is_delta():
    index = ToolSearchIndex(openai_api_key=None)
    index._semantic_available = True
    embed = _fake_embedder()
    index._embed = lambda text: embed(text)
    seen = {"count": 0}

    def embed_texts(texts):
        seen["count"] += len(texts)
        return [embed(t) for t in texts]

    index._embed_texts = embed_texts

    index.warm_embeddings()
    assert index._fully_embedded
    assert seen["count"] > 0  # cold warm embedded the catalog

    tool_id = "zz_delta_probe"
    register_custom_tool_metadata(tool_id, "Delta probe tool")
    try:
        index.mark_dirty()
        assert index._fully_embedded is False
        seen["count"] = 0
        index.warm_embeddings()
        assert index._fully_embedded
        # Only the newly added tool needed embedding, not the whole catalog.
        assert 1 <= seen["count"] <= 3
    finally:
        unregister_custom_tool_metadata(tool_id)
        index.mark_dirty()


def test_embed_failure_does_not_permanently_disable_semantic():
    """A transient embeddings-endpoint error must not latch semantic search off
    for the process lifetime; the warm/heartbeat must stay free to retry."""
    index = ToolSearchIndex(openai_api_key="sk-real-looking-key")
    assert index.is_semantic_available() is True

    class _BoomClient:
        class embeddings:
            @staticmethod
            def create(**_kwargs):
                raise RuntimeError("transient 503 from embeddings endpoint")

        def with_options(self, **_kwargs):
            return self

    index._openai_client = _BoomClient()

    assert index._embed_texts(["a", "b"]) == [None, None]
    assert index._embed("query") is None
    assert index.is_semantic_available() is True  # NOT latched off by the blip
    assert index._last_error  # recorded for diagnostics


def test_transient_warm_failure_recovers_on_next_pass():
    index = ToolSearchIndex(openai_api_key=None)
    index._semantic_available = True
    embed = _fake_embedder()
    state = {"fail": True}

    def embed_texts(texts):
        # Mirror the real method's internal catch: a failed request returns
        # all-None rather than raising.
        if state["fail"]:
            return [None] * len(texts)
        return [embed(t) for t in texts]

    index._embed_texts = embed_texts
    index._embed = lambda text: embed(text)

    index.warm_embeddings()
    assert index._fully_embedded is False  # the failed pass embedded nothing
    assert index.is_semantic_available() is True  # but it is not disabled

    state["fail"] = False
    index.warm_embeddings()  # heartbeat-style retry
    assert index._fully_embedded is True
