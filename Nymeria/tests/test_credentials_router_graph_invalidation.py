"""Graph-cache invalidation routes through the canonical agent rebuild.

``nymeria/api/routers/credentials.py::_invalidate_llm_graphs`` must delegate to
``agent._rebuild_default_graphs()`` (the single lock-correct evict-and-rebuild)
instead of reaching into private graph-cache internals, and only for LLM
credentials. A best-effort rebuild failure must never propagate to fail the
credential write.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from nymeria.api.routers.credentials import _invalidate_llm_graphs


def _llm_record() -> SimpleNamespace:
    # Registry-independent: the ``llm_provider:`` target prefix classifies this
    # as an LLM credential regardless of the provider registry contents.
    return SimpleNamespace(provider="custom", allowed_targets=["llm_provider:custom"])


def _non_llm_record() -> SimpleNamespace:
    return SimpleNamespace(
        provider="definitely_not_an_llm_provider_xyz",
        allowed_targets=["service:github"],
    )


def test_invalidate_delegates_to_rebuild_for_llm_credential():
    agent = MagicMock()

    _invalidate_llm_graphs(lambda: agent, _llm_record())

    agent._rebuild_default_graphs.assert_called_once_with()
    # No reach-in to private cache internals from the router layer.
    agent._user_graphs.clear.assert_not_called()
    agent._async_user_graphs.clear.assert_not_called()
    agent._build_graph_with_prompt.assert_not_called()


def test_invalidate_skips_non_llm_credential():
    agent = MagicMock()

    _invalidate_llm_graphs(lambda: agent, _non_llm_record())

    agent._rebuild_default_graphs.assert_not_called()


def test_invalidate_swallows_rebuild_failure():
    agent = MagicMock()
    agent._rebuild_default_graphs.side_effect = RuntimeError("boom")

    # Best-effort: a rebuild failure must not propagate to the credential write.
    _invalidate_llm_graphs(lambda: agent, _llm_record())

    agent._rebuild_default_graphs.assert_called_once_with()
