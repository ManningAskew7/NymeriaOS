from __future__ import annotations

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
