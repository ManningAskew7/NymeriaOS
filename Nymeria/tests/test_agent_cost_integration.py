"""Integration tests for the agent's cost-recording facades.

Exercises ``NymeriaAgent._compute_turn_cost``, ``_record_turn_cost``, and
``_is_cost_unavailable_for_thread`` against a stub agent built from the
unbound methods. Avoids the cost of constructing a full ``NymeriaAgent``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from nymeria.core.agent import NymeriaAgent
from nymeria.core.thread_metadata import ThreadMetadata, ThreadMetadataManager
from nymeria.core.token_tracker import TokenTracker


def _ai(input_tokens: int, output_tokens: int):
    return AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


@pytest.fixture
def stub_agent(monkeypatch):
    """Lightweight stub exposing the attributes the cost facades need."""
    tmp = Path(tempfile.mkdtemp())
    manager = ThreadMetadataManager(tmp)
    tracker = TokenTracker()

    # Seed a thread for the persistence path to find.
    with manager.atomic_update("user-1") as store:
        store.threads["thread-1"] = ThreadMetadata(thread_id="thread-1")

    agent = SimpleNamespace(
        _token_tracker=tracker,
        thread_metadata_manager=manager,
    )
    # Bind the OAuth/local detection facade so _compute_turn_cost can call
    # it through ``self`` -- mirrors how monkeypatch test seams work on the
    # real NymeriaAgent.
    agent._is_cost_unavailable_for_thread = (
        lambda cfg: NymeriaAgent._is_cost_unavailable_for_thread(agent, cfg)
    )
    return agent


def _llm_config(*, provider: str = "openai", model: str = "gpt-4o", base_url: str = ""):
    return SimpleNamespace(provider=provider, model=model, base_url=base_url)


class TestIsCostUnavailable:
    def test_paid_provider(self, stub_agent):
        cfg = _llm_config(provider="openai", base_url="https://api.openai.com/v1")
        assert NymeriaAgent._is_cost_unavailable_for_thread(stub_agent, cfg) is False

    def test_cliproxy_url(self, stub_agent):
        cfg = _llm_config(provider="openai", base_url="http://cli-proxy-api:8317/v1")
        assert NymeriaAgent._is_cost_unavailable_for_thread(stub_agent, cfg) is True

    def test_cliproxy_by_port(self, stub_agent):
        cfg = _llm_config(provider="anthropic", base_url="http://localhost:8317")
        assert NymeriaAgent._is_cost_unavailable_for_thread(stub_agent, cfg) is True

    def test_local_llm_url(self, stub_agent):
        cfg = _llm_config(provider="openai", base_url="http://127.0.0.1:11434/v1")
        assert NymeriaAgent._is_cost_unavailable_for_thread(stub_agent, cfg) is True

    def test_local_provider_id(self, stub_agent):
        cfg = _llm_config(provider="ollama")
        assert NymeriaAgent._is_cost_unavailable_for_thread(stub_agent, cfg) is True


class TestComputeTurnCost:
    def test_sums_across_ai_messages_in_turn(self, stub_agent):
        cfg = _llm_config(provider="openai", model="gpt-4o")
        msgs = [
            HumanMessage(content="hi"),
            _ai(100, 50),
            _ai(50, 30),
            _ai(20, 10),
        ]
        cost, na = NymeriaAgent._compute_turn_cost(stub_agent, "thread-1", msgs, cfg)
        assert na is False
        assert cost is not None
        assert cost > 0

    def test_unknown_model_returns_none(self, stub_agent):
        cfg = _llm_config(provider="openai", model="unknown-xyz-model")
        msgs = [HumanMessage(content="hi"), _ai(100, 50)]
        cost, na = NymeriaAgent._compute_turn_cost(stub_agent, "thread-1", msgs, cfg)
        assert na is False
        assert cost is None

    def test_oauth_subscription_returns_na(self, stub_agent):
        cfg = _llm_config(
            provider="openai",
            model="claude-opus-4-7",
            base_url="http://cli-proxy-api:8317/v1",
        )
        msgs = [HumanMessage(content="hi"), _ai(100, 50)]
        cost, na = NymeriaAgent._compute_turn_cost(stub_agent, "thread-1", msgs, cfg)
        assert na is True
        assert cost is None

    def test_no_double_counting_across_turns(self, stub_agent):
        cfg = _llm_config(provider="openai", model="gpt-4o")
        msgs = [HumanMessage(content="hi"), _ai(100, 50)]
        cost1, _ = NymeriaAgent._compute_turn_cost(stub_agent, "thread-1", msgs, cfg)
        assert cost1 is not None and cost1 > 0
        # Same message list re-passed: should now find no NEW AIMessages.
        cost2, _ = NymeriaAgent._compute_turn_cost(stub_agent, "thread-1", msgs, cfg)
        assert cost2 is None

    def test_new_messages_after_high_water_mark(self, stub_agent):
        cfg = _llm_config(provider="openai", model="gpt-4o")
        first = [HumanMessage(content="hi"), _ai(100, 50)]
        cost1, _ = NymeriaAgent._compute_turn_cost(stub_agent, "thread-1", first, cfg)
        assert cost1 is not None
        extended = first + [HumanMessage(content="more"), _ai(200, 100)]
        cost2, _ = NymeriaAgent._compute_turn_cost(stub_agent, "thread-1", extended, cfg)
        assert cost2 is not None and cost2 > 0


class TestRecordTurnCost:
    def test_persists_to_thread_metadata(self, stub_agent):
        NymeriaAgent._record_turn_cost(stub_agent, "thread-1", "user-1", 0.0042, False)
        meta = stub_agent.thread_metadata_manager.get_thread("user-1", "thread-1")
        assert meta is not None
        # 0.0042 USD = 4200 micros
        assert meta.total_cost_usd_micros == 4200

    def test_accumulates_across_calls(self, stub_agent):
        NymeriaAgent._record_turn_cost(stub_agent, "thread-1", "user-1", 0.0010, False)
        NymeriaAgent._record_turn_cost(stub_agent, "thread-1", "user-1", 0.0025, False)
        meta = stub_agent.thread_metadata_manager.get_thread("user-1", "thread-1")
        assert meta.total_cost_usd_micros == 3500

    def test_na_skips_persistence(self, stub_agent):
        NymeriaAgent._record_turn_cost(stub_agent, "thread-1", "user-1", 0.01, True)
        meta = stub_agent.thread_metadata_manager.get_thread("user-1", "thread-1")
        assert meta.total_cost_usd_micros == 0

    def test_none_cost_skips_persistence(self, stub_agent):
        NymeriaAgent._record_turn_cost(stub_agent, "thread-1", "user-1", None, False)
        meta = stub_agent.thread_metadata_manager.get_thread("user-1", "thread-1")
        assert meta.total_cost_usd_micros == 0

    def test_missing_thread_is_silent(self, stub_agent):
        # Should not raise even when the thread metadata row does not exist.
        NymeriaAgent._record_turn_cost(stub_agent, "no-such-thread", "user-1", 0.01, False)


class TestThreadMetadataBackwardCompat:
    def test_loads_legacy_json_without_cost_field(self):
        """Existing per-user JSON files written before this feature must still load."""
        legacy = {
            "thread_id": "t1",
            "title": "old",
            "pinned": False,
            "platform": "desktop",
            "title_source": "user",
        }
        meta = ThreadMetadata.model_validate(legacy)
        assert meta.total_cost_usd_micros == 0
