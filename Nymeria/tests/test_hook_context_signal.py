"""Context-usage signal for lifecycle hooks: fire points + the advisory recipe.

Covers ``hook_context_stats`` (the agent-side best-effort reader), the
PROMPT_SUBMIT/DONE context builders, the ``graph_run_config`` stamps, the tool
node's fresh-occupancy extraction, and an end-to-end run of the
checkpoint-advisory recipe (a once-gated ``inject_context`` on
``post_tool_use`` with a ``context_pct_of_trigger`` fire condition) through the
real ``SafeToolNode``.
"""

from __future__ import annotations

from types import SimpleNamespace

import importlib

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.runtime import DEFAULT_RUNTIME
from langgraph._internal._constants import CONFIG_KEY_RUNTIME

from nymeria.core.agent import NymeriaAgent
from nymeria.core.agent_compaction import hook_context_stats
from nymeria.core.agent_safety import graph_run_config
from nymeria.core.conditions import HookCondition
from nymeria.core.hook_manager import InjectContextLogic
from nymeria.core.hooks import HookEvent
from nymeria.core.hooks.bridge import build_registry
from nymeria.core.token_tracker import TokenTracker
from nymeria.vendor.react_agent.nodes import SafeToolNode

dmod = importlib.import_module("nymeria.core.hooks.dispatch")


# --- hook_context_stats (agent-side reader) -----------------------------------

def _fake_agent(*, context_tokens=0, mode="auto_compact", limit=400_000, trigger=200_000):
    tracker = TokenTracker()
    if context_tokens:
        tracker.set_context_estimate("t1", context_tokens, context_model="m")
    return SimpleNamespace(
        _token_tracker=tracker,
        _get_llm_config_for_thread=lambda tid: SimpleNamespace(model="m"),
        settings=SimpleNamespace(context_management=mode),
        _compaction=SimpleNamespace(
            _resolve_threshold_config=lambda tid: ("tokens", 0.8, trigger)
        ),
        _compact_trigger_tokens=(
            lambda model_limit, pct, mode="tokens", tokens=200_000: min(tokens, model_limit)
        ),
        _limit=limit,
    )


def test_hook_context_stats_reads_tracker_and_trigger(monkeypatch):
    agent = _fake_agent(context_tokens=150_000)
    monkeypatch.setattr(
        "nymeria.core.agent_compaction.get_context_limit", lambda m: 400_000
    )
    stats = hook_context_stats(agent, "t1")
    assert stats == {
        "context_tokens": 150_000,
        "context_limit": 400_000,
        "compact_trigger_tokens": 200_000,
    }


def test_hook_context_stats_no_trigger_when_compaction_off(monkeypatch):
    agent = _fake_agent(context_tokens=150_000, mode="none")
    monkeypatch.setattr(
        "nymeria.core.agent_compaction.get_context_limit", lambda m: 400_000
    )
    stats = hook_context_stats(agent, "t1")
    assert stats["compact_trigger_tokens"] is None
    assert stats["context_tokens"] == 150_000


def test_hook_context_stats_zero_occupancy_is_unknown(monkeypatch):
    agent = _fake_agent(context_tokens=0)
    monkeypatch.setattr(
        "nymeria.core.agent_compaction.get_context_limit", lambda m: 400_000
    )
    assert hook_context_stats(agent, "t1")["context_tokens"] is None


def test_hook_context_stats_never_raises():
    broken = SimpleNamespace()  # no tracker, no settings
    assert hook_context_stats(broken, "t1") == {
        "context_tokens": None,
        "context_limit": None,
        "compact_trigger_tokens": None,
    }


# --- PROMPT_SUBMIT / DONE context builders carry the signal --------------------

def _seam_agent():
    agent = NymeriaAgent.__new__(NymeriaAgent)
    agent._hook_context_stats = lambda tid: {  # type: ignore[method-assign]
        "context_tokens": 170_000,
        "context_limit": 400_000,
        "compact_trigger_tokens": 200_000,
    }
    return agent


def test_prompt_submit_context_carries_stats():
    ctx = _seam_agent()._prompt_submit_context(
        thread_id="t1", user_id="u1", message="hi",
        is_autonomous=False, holder_kind="interactive", trigger_label=None,
        registry=object(),
    )
    assert ctx.context_tokens == 170_000
    assert ctx.context_limit == 400_000
    assert ctx.compact_trigger_tokens == 200_000


def test_done_context_carries_stats():
    ctx = _seam_agent()._done_context(
        thread_id="t1", user_id="u1", is_autonomous=False, holder_kind=None,
        completed_normally=True, final_text="done",
        registry=object(),
    )
    assert ctx.context_tokens == 170_000
    assert ctx.compact_trigger_tokens == 200_000


def test_context_builders_skip_stats_without_a_registry():
    """The zero-hook hot path never pays the stats resolve (vault reads)."""
    agent = NymeriaAgent.__new__(NymeriaAgent)
    calls = []

    def _stats(tid):
        calls.append(tid)
        return {"context_tokens": 1, "context_limit": 2, "compact_trigger_tokens": 3}

    agent._hook_context_stats = _stats  # type: ignore[method-assign]
    ctx = agent._prompt_submit_context(
        thread_id="t1", user_id="u1", message="hi",
        is_autonomous=False, holder_kind="interactive", trigger_label=None,
    )
    done = agent._done_context(
        thread_id="t1", user_id="u1", is_autonomous=False, holder_kind=None,
        completed_normally=True, final_text="done",
    )
    assert calls == []
    assert ctx.context_tokens is None and done.context_tokens is None


# --- graph_run_config stamps (tool-event fallback signal) ----------------------

def _config_agent(registry):
    return SimpleNamespace(
        _max_iterations_for_thread=lambda tid: 10,
        _recursion_limit_for_iterations=lambda n: 2 * n + 1,
        settings=SimpleNamespace(
            sequential_tool_execution=False, tool_timing_in_results=False
        ),
        thread_config_manager=None,
        _hook_registry_for_turn=lambda tid, uid: registry,
        _hook_context_stats=lambda tid: {
            "context_tokens": 160_000,
            "context_limit": 400_000,
            "compact_trigger_tokens": 200_000,
        },
    )


def test_graph_run_config_stamps_signal_only_with_hooks():
    registry = build_registry([])
    stamped = graph_run_config(_config_agent(registry), "t1", "u1")
    assert stamped["configurable"]["hook_context_tokens"] == 160_000
    assert stamped["configurable"]["hook_context_limit"] == 400_000
    assert stamped["configurable"]["hook_compact_trigger_tokens"] == 200_000
    # No registry (zero enabled hooks): the config stays byte-identical.
    bare = graph_run_config(_config_agent(None), "t1", "u1")
    assert "hook_context_tokens" not in bare["configurable"]
    assert "hook_registry" not in bare["configurable"]


# --- SafeToolNode: fresh occupancy + the advisory recipe end to end ------------

@tool
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


def _cfg(registry=None, **stamps):
    configurable = {
        CONFIG_KEY_RUNTIME: DEFAULT_RUNTIME,
        "thread_id": "t-ctx",
        "user_id": "u-ctx",
    }
    if registry is not None:
        configurable["hook_registry"] = registry
    configurable.update(stamps)
    return {"configurable": configurable}


def _call(text="hi", cid="c1"):
    return {"name": "echo", "args": {"text": text}, "id": cid}


def _state(tool_calls, input_tokens=None):
    usage = (
        {"input_tokens": input_tokens, "output_tokens": 5,
         "total_tokens": input_tokens + 5}
        if input_tokens
        else None
    )
    return {"messages": [
        AIMessage(content="", tool_calls=tool_calls, usage_metadata=usage)
    ]}


def _first_msg(result):
    return result["messages"][0]


def test_tool_hook_sees_fresh_occupancy_over_the_stamp():
    dmod.reset()
    seen = {}
    try:
        def pre(c):
            seen["tokens"] = c.context_tokens
            seen["limit"] = c.context_limit
            seen["trigger"] = c.compact_trigger_tokens
            return None
        dmod.register(HookEvent.PRE_TOOL_USE, pre)
        node = SafeToolNode([echo])
        cfg = _cfg(
            hook_context_tokens=111_000,
            hook_context_limit=400_000,
            hook_compact_trigger_tokens=200_000,
        )
        # The state's last AIMessage usage (fresh, mid-turn) wins over the stamp.
        node.invoke(_state([_call()], input_tokens=155_000), cfg)
        assert seen == {"tokens": 155_000, "limit": 400_000, "trigger": 200_000}
        # No usage in state: the turn-entry stamp is the fallback.
        node.invoke(_state([_call(cid="c2")]), cfg)
        assert seen["tokens"] == 111_000
    finally:
        dmod.reset()


def test_context_checkpoint_advisory_recipe_end_to_end():
    """The backlog #72 recipe: once-gated inject_context on post_tool_use.

    Fires once when occupancy crosses 85% of the compaction trigger, stays
    silent while above, re-arms when occupancy drops (compaction), then fires
    again on the next crossing.
    """
    dmod.reset()
    try:
        defn = SimpleNamespace(
            id="ckpt0001",
            name="context checkpoint advisory",
            event="post_tool_use",
            matcher=None,
            fire_conditions=[
                HookCondition(field="context_pct_of_trigger", operator="gte", value="85")
            ],
            once=True,
            logic=InjectContextLogic(
                text="[advisory {context_tokens}/{compact_trigger_tokens}]"
            ),
        )
        registry = build_registry([defn])
        node = SafeToolNode([echo])
        cfg = _cfg(
            registry=registry,
            hook_context_limit=400_000,
            hook_compact_trigger_tokens=200_000,
        )

        def run(tokens, cid):
            out = node.invoke(_state([_call(cid=cid)], input_tokens=tokens), cfg)
            return _first_msg(out).content

        assert "[advisory" not in run(160_000, "c1")          # below the line
        assert "[advisory 172000/200000]" in run(172_000, "c2")  # crossing: fires
        assert "[advisory" not in run(180_000, "c3")          # still above: once
        assert "[advisory" not in run(100_000, "c4")          # dropped: re-arms
        assert "[advisory 190000/200000]" in run(190_000, "c5")  # fires again
    finally:
        dmod.reset()


def test_once_gate_persists_on_the_observe_plane(monkeypatch):
    """A once-gated observe hook fires its side effect once (sequential case).

    The observe dispatchers ignore outcomes but must still apply the gate's
    scratch patches, or the sentinel never sets and the side effect repeats.
    Pins the sequential guarantee only; concurrent off-turn observe dispatches
    remain best-effort (documented).
    """
    dmod.reset()
    calls = []
    try:
        from nymeria.core.hooks import actions as actions_module
        monkeypatch.setitem(
            actions_module.ACTIONS, "notify", lambda ctx, params: calls.append(1)
        )
        defn = SimpleNamespace(
            id="ob1", name="notify once", event="post_tool_use", matcher=None,
            fire_conditions=None, once=True,
            logic=SimpleNamespace(
                action="notify", model_dump=lambda **kw: {"text": "hi"}
            ),
        )
        registry = build_registry([defn])
        ctx = actions_module.HookContext(
            event=HookEvent.POST_TOOL_USE, thread_id="t-obs", user_id="u1",
            is_autonomous=False, tool_name="echo",
        )
        dmod.dispatch_observe(HookEvent.POST_TOOL_USE, ctx, registry=registry)
        dmod.dispatch_observe(HookEvent.POST_TOOL_USE, ctx, registry=registry)
        assert calls == [1]
    finally:
        dmod.reset()
