"""Tests for the phase 2 workflow verbs: ``nym.llm``, ``nym.thread``, and the
side-effect family (``emit``, ``todo.add``, ``notify``, ``memory.*``), plus
the shared structured-output helper and the child-proxy wire conversion.

Unit-level around each handler using the modules' monkeypatch seams (the
phase 1 idiom), plus one end-to-end child run proving the ``schema=`` wire
path (pydantic-style class in the child, JSON Schema dict at the parent).
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nymeria.core.workflows import WorkflowBudget, execute_workflow
from nymeria.core.workflows import runner as wf_runner
from nymeria.core.workflows import verbs_effects, verbs_llm, verbs_state, verbs_thread
from nymeria.core.workflows.registry import (
    VerbContext,
    VerbError,
    load_builtin_verbs,
    verb_metadata,
)
from nymeria.core.workflows.structured import (
    ainvoke_structured,
    parse_json_reply,
    validate_json_value,
)
from nymeria.vendor.react_agent.config import LLMConfig

pytestmark = pytest.mark.asyncio

SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "integer"}, "note": {"type": "string"}},
}


def _ctx(**overrides) -> VerbContext:
    defaults = dict(user_id="tester", thread_id="t1", run_id="r1")
    defaults.update(overrides)
    return VerbContext(**defaults)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    """Chat-model stand-in: scripted native and prompt-path replies."""

    def __init__(self, *, native=None, native_error=None, replies=()):
        self.native = native
        self.native_error = native_error
        self.replies = list(replies)
        self.prompts: list = []
        self.native_calls = 0

    def with_structured_output(self, schema):
        if self.native_error == "construct":
            raise RuntimeError("no structured support")
        outer = self

        class _Runner:
            async def ainvoke(self, prompt, config=None):
                outer.native_calls += 1
                outer.prompts.append(prompt)
                if outer.native_error == "invoke":
                    raise RuntimeError("gateway rejected tools")
                return outer.native

        return _Runner()

    async def ainvoke(self, prompt, config=None):
        self.prompts.append(prompt)
        return FakeMessage(self.replies.pop(0))


# --- structured helper -------------------------------------------------------


def _must(error) -> str:
    assert error is not None
    return error


async def test_validate_json_value_shapes():
    assert validate_json_value({"answer": 3}, SCHEMA) is None
    assert "missing required" in _must(validate_json_value({}, SCHEMA))
    assert "expected top-level object" in _must(validate_json_value("x", SCHEMA))
    assert "expected integer" in _must(validate_json_value({"answer": "3"}, SCHEMA))
    assert "got boolean" in _must(validate_json_value({"answer": True}, SCHEMA))
    assert validate_json_value("anything", "not-a-schema") is None


async def test_parse_json_reply_tolerates_fences_and_prose():
    assert parse_json_reply('{"a": 1}') == ({"a": 1}, None)
    fenced = '```json\n{"a": 1}\n```'
    assert parse_json_reply(fenced) == ({"a": 1}, None)
    prose = 'Sure! Here you go: {"a": 1} Hope that helps.'
    assert parse_json_reply(prose) == ({"a": 1}, None)
    value, error = parse_json_reply("no json here")
    assert value is None and "not valid JSON" in _must(error)


async def test_structured_native_success_is_single_call():
    llm = FakeLLM(native={"answer": 7})
    result = await ainvoke_structured(llm, "q", SCHEMA)
    assert result == {"answer": 7}
    assert llm.native_calls == 1
    assert llm.replies == []


async def test_structured_native_invalid_gets_one_prompt_retry():
    llm = FakeLLM(native={"wrong": 1}, replies=['{"answer": 5}'])
    result = await ainvoke_structured(llm, "q", SCHEMA)
    assert result == {"answer": 5}
    # The retry prompt carries the validation error and the schema.
    retry_prompt = llm.prompts[-1]
    assert "missing required" in retry_prompt
    assert "JSON Schema" in retry_prompt


async def test_structured_no_native_support_gets_two_prompt_attempts():
    llm = FakeLLM(native_error="construct", replies=["not json", '{"answer": 2}'])
    result = await ainvoke_structured(llm, "q", SCHEMA)
    assert result == {"answer": 2}
    assert llm.native_calls == 0
    assert len(llm.prompts) == 2


async def test_structured_native_invoke_failure_gets_one_prompt_attempt():
    """A failed native INVOCATION was a real round-trip: it consumes attempt
    one, so the prompt path gets exactly one attempt (two total)."""
    llm = FakeLLM(native_error="invoke", replies=['{"answer": 9}'])
    result = await ainvoke_structured(llm, "q", SCHEMA)
    assert result == {"answer": 9}
    assert llm.native_calls == 1
    assert len(llm.prompts) == 2  # the native try + one prompt attempt
    assert llm.replies == []


async def test_structured_native_invoke_failure_never_makes_third_call():
    llm = FakeLLM(native_error="invoke", replies=["not json", '{"answer": 1}'])
    with pytest.raises(VerbError):
        await ainvoke_structured(llm, "q", SCHEMA)
    # The second scripted reply must remain unconsumed: two invocations max.
    assert llm.replies == ['{"answer": 1}']


async def test_structured_exhausted_raises_verb_error():
    llm = FakeLLM(native_error="construct", replies=["nope", "still nope"])
    with pytest.raises(VerbError) as excinfo:
        await ainvoke_structured(llm, "q", SCHEMA)
    assert "failed validation after retry" in str(excinfo.value)


async def test_structured_rejects_non_dict_schema():
    from typing import Any, cast

    with pytest.raises(VerbError):
        await ainvoke_structured(FakeLLM(), "q", cast(Any, "not a schema"))


async def test_structured_native_pydantic_object_is_dumped():
    class FakeModelResult:
        def model_dump(self):
            return {"answer": 4}

    llm = FakeLLM(native=FakeModelResult())
    assert await ainvoke_structured(llm, "q", SCHEMA) == {"answer": 4}


# --- nym.llm -----------------------------------------------------------------


async def test_llm_verb_requires_prompt_and_agent(monkeypatch):
    with pytest.raises(VerbError):
        await verbs_llm._llm_verb(_ctx(), "llm", {"prompt": "  "})
    monkeypatch.setattr(verbs_llm, "_current_agent", lambda: None)
    with pytest.raises(VerbError) as excinfo:
        await verbs_llm._llm_verb(_ctx(), "llm", {"prompt": "hi"})
    assert "no agent runtime" in str(excinfo.value)


async def test_llm_verb_plain_text(monkeypatch):
    seen = {}
    llm = FakeLLM(replies=["  the answer  "])

    def fake_build(agent, thread_id, model, acting_user_id=None):
        seen["thread_id"], seen["model"] = thread_id, model
        seen["acting_user_id"] = acting_user_id
        return llm

    monkeypatch.setattr(verbs_llm, "_current_agent", lambda: object())
    monkeypatch.setattr(verbs_llm, "build_llm_for_thread", fake_build)
    result = await verbs_llm._llm_verb(_ctx(), "llm", {"prompt": "hi"})
    assert result == "the answer"
    # The workflow run's user rides along as the credential-owner fallback
    # for unclaimed threads (dev-todo #76).
    assert seen == {"thread_id": "t1", "model": "fast", "acting_user_id": "tester"}


async def test_llm_verb_schema_returns_dict(monkeypatch):
    monkeypatch.setattr(verbs_llm, "_current_agent", lambda: object())
    monkeypatch.setattr(
        verbs_llm,
        "build_llm_for_thread",
        lambda agent, tid, model, acting_user_id=None: FakeLLM(native={"answer": 1}),
    )
    result = await verbs_llm._llm_verb(
        _ctx(), "llm", {"prompt": "hi", "schema": SCHEMA}
    )
    assert result == {"answer": 1}


async def test_llm_verb_empty_reply_is_verb_error(monkeypatch):
    monkeypatch.setattr(verbs_llm, "_current_agent", lambda: object())
    monkeypatch.setattr(
        verbs_llm,
        "build_llm_for_thread",
        lambda agent, tid, model, acting_user_id=None: FakeLLM(replies=[""]),
    )
    with pytest.raises(VerbError):
        await verbs_llm._llm_verb(_ctx(), "llm", {"prompt": "hi"})


def _fake_agent(cfg, **settings_overrides):
    values = dict(
        llm_provider=cfg.provider,
        llm_model=cfg.model,
        llm_fast_model="",
        llm_smart_model="",
        llm_background_model="",
    )
    values.update(settings_overrides)
    settings = SimpleNamespace(**values)
    return SimpleNamespace(
        settings=settings,
        # Accepts the dev-todo #76 acting-user owner-fallback argument the
        # nym.llm wire path now passes (the workflow run's user).
        get_llm_config_for_thread=lambda thread_id="", acting_user_id=None: cfg,
    )


async def test_build_llm_no_override_keeps_thread_config(monkeypatch):
    built = {}
    cfg = LLMConfig(provider="anthropic", model="m0", api_key="k")
    monkeypatch.setattr(
        verbs_llm, "_create_llm", lambda c: built.setdefault("cfg", c)
    )
    verbs_llm.build_llm_for_thread(_fake_agent(cfg), "t1", None)
    assert built["cfg"].model == "m0"
    assert built["cfg"].api_key == "k"


async def test_build_llm_tier_alias_same_provider_keeps_endpoint(monkeypatch):
    built = {}
    cfg = LLMConfig(
        provider="anthropic", model="m0", api_key="k", base_url="http://proxy"
    )
    agent = _fake_agent(cfg, llm_fast_model="claude-fast")
    monkeypatch.setattr(
        verbs_llm, "_create_llm", lambda c: built.setdefault("cfg", c)
    )
    verbs_llm.build_llm_for_thread(agent, "t1", "fast")
    assert built["cfg"].model == "claude-fast"
    assert built["cfg"].provider == "anthropic"
    assert built["cfg"].api_key == "k"
    assert built["cfg"].base_url == "http://proxy"


async def test_build_llm_cross_provider_resolves_new_credentials(monkeypatch):
    built = {}
    cfg = LLMConfig(provider="anthropic", model="m0", api_key="k")
    monkeypatch.setattr(
        verbs_llm, "_create_llm", lambda c: built.setdefault("cfg", c)
    )
    monkeypatch.setattr(
        verbs_llm,
        "_cross_provider_overrides",
        lambda settings, provider: {
            "base_url": "http://other",
            "api_key": "k2",
            "provider_route": None,
        },
    )
    verbs_llm.build_llm_for_thread(_fake_agent(cfg), "t1", "openai:gpt-x")
    assert built["cfg"].provider == "openai"
    assert built["cfg"].model == "gpt-x"
    assert built["cfg"].api_key == "k2"
    assert built["cfg"].base_url == "http://other"


async def test_build_llm_no_model_anywhere_is_verb_error(monkeypatch):
    cfg = LLMConfig(provider="anthropic", model="", api_key="k")
    monkeypatch.setattr(verbs_llm, "_create_llm", lambda c: c)
    with pytest.raises(VerbError):
        verbs_llm.build_llm_for_thread(_fake_agent(cfg), "t1", None)


# --- nym.thread --------------------------------------------------------------


def _thread_tc(thread_id="target-1", callable_=True, name="Helper"):
    return SimpleNamespace(
        thread_id=thread_id, callable=callable_, callable_name=name
    )


def _thread_agent(tc=None, *, configs=None, callables=(), ancestor=False):
    manager = SimpleNamespace(
        get_config=lambda tid: (configs or {}).get(tid, tc if tid == getattr(tc, "thread_id", None) else None),
        list_callable_threads=lambda owned_thread_ids=None: list(callables),
    )
    aborted = []
    edges = []
    agent = SimpleNamespace(
        thread_config_manager=manager,
        accounts_repo=SimpleNamespace(
            list_threads_for_user=lambda user_id: [c.thread_id for c in callables]
        ),
        settings=SimpleNamespace(llm_provider="anthropic"),
        is_ancestor_invocation=lambda child, target: ancestor,
        abort_with_cascade=lambda tid: aborted.append(tid),
        register_callable_invocation=lambda parent, child: edges.append(
            ("+", parent, child)
        ),
        unregister_callable_invocation=lambda parent, child: edges.append(
            ("-", parent, child)
        ),
    )
    agent._aborted = aborted
    agent._edges = edges
    return agent


async def test_thread_verb_arg_validation(monkeypatch):
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: object())
    with pytest.raises(VerbError):
        await verbs_thread._thread_verb(_ctx(), "thread", {"prompt": ""})
    # kit= is fresh-spawn-only on nym.thread; existing threads take kits via
    # nym.threads.configure (the phase 2 blanket rejection was lifted).
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._thread_verb(
            _ctx(),
            "thread",
            {"prompt": "p", "kit": "web-research", "id_or_title": "t-x"},
        )
    assert "threads.configure" in str(excinfo.value)
    with pytest.raises(VerbError):
        await verbs_thread._thread_verb(
            _ctx(), "thread", {"prompt": "p", "mode": "yolo"}
        )
    with pytest.raises(VerbError):
        await verbs_thread._thread_verb(
            _ctx(),
            "thread",
            {"prompt": "p", "mode": "handoff", "schema": SCHEMA},
        )
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._thread_verb(
            _ctx(), "thread", {"prompt": "p", "mode": "handoff"}
        )
    assert "id_or_title" in str(excinfo.value)


async def test_resolve_target_by_id_and_callable_name(monkeypatch):
    monkeypatch.setattr(
        verbs_thread, "_check_ownership", lambda agent, u, tid: None
    )
    tc = _thread_tc()
    agent = _thread_agent(tc)
    assert verbs_thread.resolve_target_thread(agent, "tester", "target-1") == (
        "target-1",
        "Helper",
    )
    # Fallback: callable-name match, case-insensitive.
    agent2 = _thread_agent(None, callables=[tc])
    assert verbs_thread.resolve_target_thread(agent2, "tester", "helper") == (
        "target-1",
        "Helper",
    )
    with pytest.raises(VerbError) as excinfo:
        verbs_thread.resolve_target_thread(agent2, "tester", "nobody")
    assert "no thread matches" in str(excinfo.value)


async def test_resolve_target_enforces_ownership_and_callable(monkeypatch):
    tc = _thread_tc(callable_=False)
    agent = _thread_agent(tc)
    monkeypatch.setattr(
        verbs_thread, "_check_ownership", lambda a, u, tid: None
    )
    with pytest.raises(VerbError) as excinfo:
        verbs_thread.resolve_target_thread(agent, "tester", "target-1")
    assert "not callable" in str(excinfo.value)

    monkeypatch.setattr(
        verbs_thread,
        "_check_ownership",
        lambda a, u, tid: "[Error]: not yours",
    )
    with pytest.raises(VerbError) as excinfo:
        verbs_thread.resolve_target_thread(
            _thread_agent(_thread_tc()), "tester", "target-1"
        )
    assert "not yours" in str(excinfo.value)


async def test_thread_verb_existing_ask(monkeypatch):
    seen = {}
    agent = _thread_agent(_thread_tc())

    def fake_invoke(tid, task, user, label, trigger):
        seen.update(tid=tid, task=task, user=user, label=label, trigger=trigger)
        return "the reply"

    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(verbs_thread, "_check_ownership", lambda a, u, t: None)
    monkeypatch.setattr(verbs_thread, "_invoke_thread", fake_invoke)
    result = await verbs_thread._thread_verb(
        _ctx(), "thread", {"prompt": "do it", "id_or_title": "target-1"}
    )
    assert result == "the reply"
    assert seen["tid"] == "target-1"
    assert seen["user"] == "tester"
    assert seen["trigger"] == "workflow:adhoc"
    # The blocking ask registers the caller->target edge for the duration of
    # the wait (deadlock-guard visibility) and removes it after.
    assert agent._edges == [("+", "t1", "target-1"), ("-", "t1", "target-1")]


async def test_thread_verb_ask_reply_may_start_with_error_text(monkeypatch):
    """Free-form ask replies are NOT failure-classified on an [Error] prefix."""
    monkeypatch.setattr(verbs_thread, "_check_ownership", lambda a, u, t: None)
    monkeypatch.setattr(
        verbs_thread, "_current_agent", lambda: _thread_agent(_thread_tc())
    )
    monkeypatch.setattr(
        verbs_thread,
        "_invoke_thread",
        lambda *a: "[Error]: is what the log line said; here is my analysis...",
    )
    result = await verbs_thread._thread_verb(
        _ctx(), "thread", {"prompt": "p", "id_or_title": "target-1"}
    )
    assert result.startswith("[Error]: is what the log line said")


async def test_thread_verb_handoff_error_receipt_is_verb_error(monkeypatch):
    monkeypatch.setattr(verbs_thread, "_check_ownership", lambda a, u, t: None)
    monkeypatch.setattr(
        verbs_thread, "_current_agent", lambda: _thread_agent(_thread_tc())
    )
    monkeypatch.setattr(
        verbs_thread,
        "_handoff_thread",
        lambda *a: "[Error]: handoff validation failed",
    )
    with pytest.raises(VerbError):
        await verbs_thread._thread_verb(
            _ctx(),
            "thread",
            {"prompt": "p", "id_or_title": "target-1", "mode": "handoff"},
        )


async def test_thread_verb_self_and_ancestor_deadlocks_rejected(monkeypatch):
    own = _thread_tc(thread_id="t1")
    monkeypatch.setattr(verbs_thread, "_check_ownership", lambda a, u, t: None)
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: _thread_agent(own))
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._thread_verb(
            _ctx(), "thread", {"prompt": "p", "id_or_title": "t1"}
        )
    assert "deadlock" in str(excinfo.value)

    other = _thread_tc(thread_id="target-1")
    monkeypatch.setattr(
        verbs_thread, "_current_agent", lambda: _thread_agent(other, ancestor=True)
    )
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._thread_verb(
            _ctx(), "thread", {"prompt": "p", "id_or_title": "target-1"}
        )
    assert "ancestor" in str(excinfo.value)


async def test_thread_verb_handoff_allows_own_thread(monkeypatch):
    seen = {}

    def fake_handoff(tid, task, user, label, caller_thread_id, trigger):
        seen.update(tid=tid, caller=caller_thread_id)
        return "[HandedOff]: handoff_id=h1 target_thread_id=t1"

    own = _thread_tc(thread_id="t1")
    monkeypatch.setattr(verbs_thread, "_check_ownership", lambda a, u, t: None)
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: _thread_agent(own))
    monkeypatch.setattr(verbs_thread, "_handoff_thread", fake_handoff)
    result = await verbs_thread._thread_verb(
        _ctx(),
        "thread",
        {"prompt": "p", "id_or_title": "t1", "mode": "handoff"},
    )
    assert result.startswith("[HandedOff]")
    assert seen == {"tid": "t1", "caller": "t1"}


async def test_thread_verb_error_marker_becomes_verb_error(monkeypatch):
    monkeypatch.setattr(verbs_thread, "_check_ownership", lambda a, u, t: None)
    monkeypatch.setattr(
        verbs_thread, "_current_agent", lambda: _thread_agent(_thread_tc())
    )
    monkeypatch.setattr(
        verbs_thread,
        "_invoke_thread",
        lambda *a: '[NymeriaSubAgentError]{"code": "boom"}',
    )
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._thread_verb(
            _ctx(), "thread", {"prompt": "p", "id_or_title": "target-1"}
        )
    assert "sub-agent turn failed" in str(excinfo.value)


async def test_thread_verb_fresh_spawn_maps_args(monkeypatch):
    seen = {}

    def fake_spawn(spawn_args, config):
        seen["args"], seen["config"] = spawn_args, config
        return "[Spawned]: thread_id=spawned-1\n\nchild says hi"

    monkeypatch.setattr(
        verbs_thread, "_current_agent", lambda: _thread_agent(None)
    )
    monkeypatch.setattr(verbs_thread, "_spawn_fresh", fake_spawn)
    result = await verbs_thread._thread_verb(
        _ctx(),
        "thread",
        {
            "prompt": "research it",
            "title": "Research",
            "tools": ["web_search"],
            "model": "openai:gpt-x",
            "kit": "web-research",
        },
    )
    assert "child says hi" in result
    assert seen["args"]["optional_tools"] == ["web_search"]
    assert seen["args"]["llm_provider"] == "openai"
    assert seen["args"]["llm_model"] == "gpt-x"
    assert seen["args"]["kit"] == "web-research"
    assert seen["config"]["configurable"] == {
        "user_id": "tester",
        "thread_id": "t1",
    }


async def test_thread_verb_fresh_spawn_tier_alias_passthrough(monkeypatch):
    seen = {}

    def fake_spawn(spawn_args, config):
        seen["args"] = spawn_args
        return "ok"

    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: _thread_agent(None))
    monkeypatch.setattr(verbs_thread, "_spawn_fresh", fake_spawn)
    await verbs_thread._thread_verb(
        _ctx(), "thread", {"prompt": "p", "model": "fast"}
    )
    assert seen["args"]["llm_model"] == "fast"
    assert "llm_provider" not in seen["args"]


async def test_thread_verb_schema_extracts_from_reply(monkeypatch):
    captured = {}

    async def fake_structured(llm, prompt, schema):
        captured["prompt"], captured["schema"] = prompt, schema
        return {"answer": 3}

    monkeypatch.setattr(verbs_thread, "_check_ownership", lambda a, u, t: None)
    monkeypatch.setattr(
        verbs_thread, "_current_agent", lambda: _thread_agent(_thread_tc())
    )
    monkeypatch.setattr(verbs_thread, "_invoke_thread", lambda *a: "raw reply here")
    monkeypatch.setattr(
        verbs_thread,
        "build_llm_for_thread",
        lambda agent, tid, model, acting_user_id=None: FakeLLM(),
    )
    monkeypatch.setattr(verbs_thread, "ainvoke_structured", fake_structured)
    result = await verbs_thread._thread_verb(
        _ctx(),
        "thread",
        {"prompt": "p", "id_or_title": "target-1", "schema": SCHEMA},
    )
    assert result == {"answer": 3}
    assert "raw reply here" in captured["prompt"]
    assert captured["schema"] == SCHEMA


async def test_thread_verb_cancellation_cascades_abort(monkeypatch):
    release = threading.Event()

    def slow_invoke(*args):
        release.wait(timeout=10)
        return "late"

    agent = _thread_agent(_thread_tc())
    monkeypatch.setattr(verbs_thread, "_check_ownership", lambda a, u, t: None)
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(verbs_thread, "_invoke_thread", slow_invoke)

    async def _run():
        return await verbs_thread._thread_verb(
            _ctx(), "thread", {"prompt": "p", "id_or_title": "target-1"}
        )

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent._aborted == ["target-1"]
    release.set()


# --- side-effect verbs -------------------------------------------------------


async def test_emit_prefixes_and_validates(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        verbs_effects,
        "_publish_autonomous_event",
        lambda et, tid, uid, task_id, data: seen.update(
            et=et, tid=tid, uid=uid, task_id=task_id, data=data
        ),
    )
    result = await verbs_effects._emit_verb(
        _ctx(), "emit", {"event_type": "progress", "payload": {"pct": 40}}
    )
    assert result == {"published": True, "event_type": "workflow_progress"}
    assert seen == {
        "et": "workflow_progress",
        "tid": "t1",
        "uid": "tester",
        "task_id": "r1",
        "data": {"pct": 40},
    }
    # Already-prefixed stays; core stream types cannot be spoofed.
    await verbs_effects._emit_verb(
        _ctx(), "emit", {"event_type": "workflow_done", "payload": {}}
    )
    assert seen["et"] == "workflow_done"
    await verbs_effects._emit_verb(_ctx(), "emit", {"event_type": "response"})
    assert seen["et"] == "workflow_response"
    with pytest.raises(VerbError):
        await verbs_effects._emit_verb(_ctx(), "emit", {"event_type": "NOPE!"})
    with pytest.raises(VerbError):
        await verbs_effects._emit_verb(
            _ctx(), "emit", {"event_type": "x", "payload": "not a dict"}
        )


async def test_emit_rejects_engine_reserved_types(monkeypatch):
    monkeypatch.setattr(
        verbs_effects, "_publish_autonomous_event", lambda *a: None
    )
    # Both spellings of every reserved type: bare ("step" gets prefixed into
    # "workflow_step") and already-prefixed.
    for reserved in sorted(verbs_effects.RESERVED_EVENT_TYPES):
        for spelling in (reserved, reserved[len("workflow_"):]):
            with pytest.raises(VerbError) as excinfo:
                await verbs_effects._emit_verb(
                    _ctx(), "emit", {"event_type": spelling, "payload": {}}
                )
            assert "reserved" in str(excinfo.value)


async def test_todo_add_follows_hook_path():
    todo_list = MagicMock()
    todo_list.add_item.return_value = SimpleNamespace(
        id="abc12345", task="follow up", scheduled_for=None
    )
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=todo_list)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("nymeria.tools.todo._get_todo_manager") as gm:
        gm.return_value.atomic_update.return_value = cm
        result = await verbs_effects._todo_add_verb(
            _ctx(), "todo.add", {"task": "follow up"}
        )
    assert result["id"] == "abc12345"
    kwargs = todo_list.add_item.call_args.kwargs
    assert kwargs["created_by"] == "workflow"
    assert kwargs["thread_id"] == "t1"


async def test_todo_add_full_list_and_empty_task():
    with pytest.raises(VerbError):
        await verbs_effects._todo_add_verb(_ctx(), "todo.add", {"task": " "})
    todo_list = MagicMock()
    todo_list.add_item.return_value = None
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=todo_list)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("nymeria.tools.todo._get_todo_manager") as gm:
        gm.return_value.atomic_update.return_value = cm
        with pytest.raises(VerbError) as excinfo:
            await verbs_effects._todo_add_verb(_ctx(), "todo.add", {"task": "x"})
    assert "full" in str(excinfo.value)


async def test_notify_routes_via_profile(monkeypatch):
    seen = {}

    def fake_send(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            profile_name="default",
            attempted=["telegram"],
            delivered_to=["telegram"],
            errors={},
        )

    monkeypatch.setattr(verbs_effects, "_send_via_profile", fake_send)
    result = await verbs_effects._notify_verb(
        _ctx(), "notify", {"message": "done", "profile": "ops"}
    )
    assert result["delivered_to"] == ["telegram"]
    assert seen["user_id"] == "tester"
    assert seen["thread_id"] == "t1"
    assert seen["profile_name"] == "ops"
    with pytest.raises(VerbError):
        await verbs_effects._notify_verb(_ctx(), "notify", {"message": ""})


async def test_memory_verbs_wrap_tools_with_explicit_config(monkeypatch):
    calls = []

    class FakeTool:
        def __init__(self, reply):
            self.reply = reply

        async def ainvoke(self, tool_args, config):
            calls.append((tool_args, config))
            return self.reply

    tools = {"memory_add": FakeTool("Saved."), "memory_read": FakeTool("- note")}
    monkeypatch.setattr(verbs_effects, "_memory_tool", lambda name: tools[name])

    result = await verbs_effects._memory_add_verb(
        _ctx(), "memory.add", {"content": "likes ramen", "key": "food"}
    )
    assert result == "Saved."
    tool_args, config = calls[0]
    assert tool_args == {"scope": "global", "content": "likes ramen", "key": "food"}
    assert config["configurable"] == {"user_id": "tester", "thread_id": "t1"}

    result = await verbs_effects._memory_read_verb(
        _ctx(), "memory.read", {"scope": "thread"}
    )
    assert result == "- note"
    assert calls[1][0] == {"scope": "thread"}

    with pytest.raises(VerbError):
        await verbs_effects._memory_add_verb(_ctx(), "memory.add", {"content": ""})


async def test_memory_tool_error_string_is_verb_error(monkeypatch):
    class FailTool:
        async def ainvoke(self, tool_args, config):
            return "[Error]: memory store unavailable"

    monkeypatch.setattr(verbs_effects, "_memory_tool", lambda name: FailTool())
    with pytest.raises(VerbError) as excinfo:
        await verbs_effects._memory_add_verb(
            _ctx(), "memory.add", {"content": "x"}
        )
    assert "memory store unavailable" in str(excinfo.value)


# --- nym.threads.create / nym.threads.configure ------------------------------


def _configure_tc(thread_id="t-9", **overrides):
    defaults = dict(
        thread_id=thread_id,
        callable=False,
        callable_name=None,
        instructions=None,
        llm_config=None,
        enabled_tools=[],
        disabled_tools=[],
        active_llm_fallback=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _configure_agent(tc=None, *, callables=(), owned=(), role="user", save_ok=True):
    saved = []
    manager = SimpleNamespace(
        get_config=lambda tid: (
            tc if tc is not None and tid == tc.thread_id else None
        ),
        list_callable_threads=lambda owned_thread_ids=None: list(callables),
        save_config=lambda cfg: (saved.append(cfg), save_ok)[1],
    )
    agent = SimpleNamespace(
        thread_config_manager=manager,
        accounts_repo=SimpleNamespace(
            list_threads_for_user=lambda user_id: list(owned),
            get_user_by_id=lambda uid: SimpleNamespace(role=role),
        ),
        settings=SimpleNamespace(llm_provider="anthropic"),
        invalidate_thread_config_cache=lambda tid: None,
    )
    agent._saved = saved
    return agent


SPAWN_RECEIPT = (
    "[Spawned]: thread_id=spawned-research-a1b2c3d4\n"
    'Callable as: spawned_research_a1b2c3d4(task="..."). Any thread can invoke this.\n'
    "To delete later: ..."
)


async def test_threads_create_maps_args_and_parses_receipt(monkeypatch):
    seen = {}

    def fake_spawn(spawn_args, config):
        seen["args"], seen["config"] = spawn_args, config
        return SPAWN_RECEIPT

    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: _thread_agent(None))
    monkeypatch.setattr(verbs_thread, "_spawn_fresh", fake_spawn)
    result = await verbs_thread._threads_create_verb(
        _ctx(),
        "threads.create",
        {
            "title": "Research",
            "instructions": "focus",
            "tools": ["web_search"],
            "kit": "web-research",
            "ttl_hours": 5,
        },
    )
    assert result == {
        "thread_id": "spawned-research-a1b2c3d4",
        "callable_name": "spawned_research_a1b2c3d4",
    }
    assert seen["args"]["prompt"] == ""  # create-only: no turn runs
    assert seen["args"]["title"] == "Research"
    assert seen["args"]["kit"] == "web-research"
    assert seen["args"]["make_callable"] is True
    assert seen["args"]["ttl_hours"] == 5
    assert seen["config"]["configurable"] == {"user_id": "tester", "thread_id": "t1"}


async def test_threads_create_non_callable_receipt(monkeypatch):
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: _thread_agent(None))
    monkeypatch.setattr(
        verbs_thread,
        "_spawn_fresh",
        lambda spawn_args, config: "[Spawned]: thread_id=spawned-x-1\nTo delete later: ...",
    )
    result = await verbs_thread._threads_create_verb(
        _ctx(), "threads.create", {"title": "X", "callable": False}
    )
    assert result == {"thread_id": "spawned-x-1", "callable_name": None}


async def test_threads_create_arg_validation(monkeypatch):
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: _thread_agent(None))
    with pytest.raises(VerbError):
        await verbs_thread._threads_create_verb(_ctx(), "threads.create", {})
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._threads_create_verb(
            _ctx(), "threads.create", {"title": "X", "prompt": "run this"}
        )
    assert "does not run a turn" in str(excinfo.value)
    with pytest.raises(VerbError):
        await verbs_thread._threads_create_verb(
            _ctx(), "threads.create", {"title": "X", "callable": "yes"}
        )
    with pytest.raises(VerbError):
        await verbs_thread._threads_create_verb(
            _ctx(), "threads.create", {"title": "X", "ttl_hours": 0}
        )
    with pytest.raises(VerbError):
        await verbs_thread._threads_create_verb(
            _ctx(), "threads.create", {"title": "X", "ttl_hours": True}
        )


async def test_threads_create_error_marker_raises(monkeypatch):
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: _thread_agent(None))
    monkeypatch.setattr(
        verbs_thread,
        "_spawn_fresh",
        lambda spawn_args, config: "[Error]: Spawn depth limit reached (3).",
    )
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._threads_create_verb(
            _ctx(), "threads.create", {"title": "X"}
        )
    assert "thread creation failed" in str(excinfo.value)


async def test_parse_spawn_receipt_unrecognized():
    with pytest.raises(VerbError):
        verbs_thread._parse_spawn_receipt("something unexpected")


async def test_threads_configure_requires_a_field(monkeypatch):
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: _configure_agent())
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._threads_configure_verb(
            _ctx(), "threads.configure", {"id_or_title": "t-9"}
        )
    assert "nothing to configure" in str(excinfo.value)


async def test_threads_configure_applies_fields(monkeypatch):
    tc = _configure_tc(disabled_tools=["web_search"], active_llm_fallback="stale")
    agent = _configure_agent(tc)
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(
        verbs_thread, "_check_ownership", lambda a, u, t, name="x": None
    )
    result = await verbs_thread._threads_configure_verb(
        _ctx(),
        "threads.configure",
        {
            "id_or_title": "t-9",
            "instructions": "Be brief.",
            "model": "openai:gpt-x",
            "tools_enable": ["web_search"],
            "tools_disable": ["bash_execute"],
        },
    )
    assert result == {
        "thread_id": "t-9",
        "updated": ["instructions", "model", "tools_enable", "tools_disable"],
    }
    assert tc.instructions == "Be brief."
    assert tc.llm_config.provider == "openai"
    assert tc.llm_config.model == "gpt-x"
    assert tc.active_llm_fallback is None
    assert "web_search" in tc.enabled_tools
    assert "web_search" not in tc.disabled_tools
    assert "bash_execute" in tc.disabled_tools
    assert agent._saved  # persisted


async def test_threads_configure_target_need_not_be_callable(monkeypatch):
    # The config target is any OWNED thread; callable is an invocation rule.
    tc = _configure_tc(callable=False)
    agent = _configure_agent(tc)
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(
        verbs_thread, "_check_ownership", lambda a, u, t, name="x": None
    )
    result = await verbs_thread._threads_configure_verb(
        _ctx(),
        "threads.configure",
        {"id_or_title": "t-9", "instructions": "hi"},
    )
    assert result["thread_id"] == "t-9"


async def test_threads_configure_resolves_by_callable_name(monkeypatch):
    tc = _configure_tc(thread_id="t-2", callable=True, callable_name="Helper")
    agent = _configure_agent(None, callables=[tc], owned=["t-2"])
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(
        verbs_thread, "_check_ownership", lambda a, u, t, name="x": None
    )
    result = await verbs_thread._threads_configure_verb(
        _ctx(), "threads.configure", {"id_or_title": "helper", "instructions": "x"}
    )
    assert result["thread_id"] == "t-2"


async def test_threads_configure_ownership_denial(monkeypatch):
    tc = _configure_tc()
    agent = _configure_agent(tc)
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(
        verbs_thread,
        "_check_ownership",
        lambda a, u, t, name="x": "denied: not your thread",
    )
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._threads_configure_verb(
            _ctx(), "threads.configure", {"id_or_title": "t-9", "instructions": "x"}
        )
    assert "denied" in str(excinfo.value)


async def test_threads_configure_unknown_target(monkeypatch):
    agent = _configure_agent(None)
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._threads_configure_verb(
            _ctx(), "threads.configure", {"id_or_title": "nope", "instructions": "x"}
        )
    assert "no thread matches" in str(excinfo.value)


async def test_threads_configure_blocks_admin_only_tools(monkeypatch):
    tc = _configure_tc()
    agent = _configure_agent(tc, role="user")
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(
        verbs_thread, "_check_ownership", lambda a, u, t, name="x": None
    )
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._threads_configure_verb(
            _ctx(),
            "threads.configure",
            {"id_or_title": "t-9", "tools_enable": ["claude_code"]},
        )
    assert "admin-only" in str(excinfo.value)
    assert not agent._saved  # gate fires before any save


async def test_threads_configure_admin_can_enable_admin_tools(monkeypatch):
    tc = _configure_tc()
    agent = _configure_agent(tc, role="admin")
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(
        verbs_thread, "_check_ownership", lambda a, u, t, name="x": None
    )
    result = await verbs_thread._threads_configure_verb(
        _ctx(),
        "threads.configure",
        {"id_or_title": "t-9", "tools_enable": ["claude_code"]},
    )
    assert "tools_enable" in result["updated"]
    assert "claude_code" in tc.enabled_tools


async def test_threads_configure_enable_disable_conflict(monkeypatch):
    tc = _configure_tc()
    agent = _configure_agent(tc)
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(
        verbs_thread, "_check_ownership", lambda a, u, t, name="x": None
    )
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._threads_configure_verb(
            _ctx(),
            "threads.configure",
            {
                "id_or_title": "t-9",
                "tools_enable": ["web_search"],
                "tools_disable": ["web_search"],
            },
        )
    assert "both enabled and disabled" in str(excinfo.value)


async def test_threads_configure_instructions_cap(monkeypatch):
    tc = _configure_tc()
    agent = _configure_agent(tc)
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(
        verbs_thread, "_check_ownership", lambda a, u, t, name="x": None
    )
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._threads_configure_verb(
            _ctx(),
            "threads.configure",
            {"id_or_title": "t-9", "instructions": "x" * 5001},
        )
    assert "5000" in str(excinfo.value)


async def test_threads_configure_kit_activation(monkeypatch):
    # Kit-only configure on an owned-but-unconfigured thread: resolution
    # falls back to a fresh default config, activation runs post-save.
    agent = _configure_agent(None, owned=["t-9"])
    calls = {}

    def fake_activate(a, thread_id, user_id, kit):
        calls["thread_id"], calls["user_id"], calls["kit"] = thread_id, user_id, kit
        return True, "[Success]: kit activated"

    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(
        verbs_thread, "_check_ownership", lambda a, u, t, name="x": None
    )
    monkeypatch.setattr(verbs_thread, "_activate_kit", fake_activate)
    result = await verbs_thread._threads_configure_verb(
        _ctx(), "threads.configure", {"id_or_title": "t-9", "kit": "web-research"}
    )
    assert result == {"thread_id": "t-9", "updated": ["kit"]}
    assert calls == {"thread_id": "t-9", "user_id": "tester", "kit": "web-research"}


async def test_threads_configure_kit_failure_raises(monkeypatch):
    tc = _configure_tc()
    agent = _configure_agent(tc)
    monkeypatch.setattr(verbs_thread, "_current_agent", lambda: agent)
    monkeypatch.setattr(
        verbs_thread, "_check_ownership", lambda a, u, t, name="x": None
    )
    monkeypatch.setattr(
        verbs_thread,
        "_activate_kit",
        lambda a, t, u, k: (False, "[Error]: tool binding failed"),
    )
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._threads_configure_verb(
            _ctx(), "threads.configure", {"id_or_title": "t-9", "kit": "bad-kit"}
        )
    assert "kit activation failed" in str(excinfo.value)


# --- nym.state ----------------------------------------------------------------


def _patch_state_store(monkeypatch, tmp_path):
    monkeypatch.setattr(
        verbs_state, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path)
    )


async def test_state_requires_saved_workflow(monkeypatch, tmp_path):
    _patch_state_store(monkeypatch, tmp_path)
    for handler, args in (
        (verbs_state._state_get_verb, {"key": "k"}),
        (verbs_state._state_set_verb, {"key": "k", "value": 1}),
        (verbs_state._state_delete_verb, {"key": "k"}),
    ):
        with pytest.raises(VerbError) as excinfo:
            await handler(_ctx(), "state", args)  # default workflow_id="adhoc"
        assert "saved workflow" in str(excinfo.value)


async def test_state_set_get_roundtrip(monkeypatch, tmp_path):
    _patch_state_store(monkeypatch, tmp_path)
    ctx = _ctx(workflow_id="wf-1")
    stored = await verbs_state._state_set_verb(
        ctx, "state.set", {"key": "last_hash", "value": {"sha": "abc", "n": 3}}
    )
    assert stored == {"key": "last_hash", "stored": True}
    value = await verbs_state._state_get_verb(
        ctx, "state.get", {"key": "last_hash"}
    )
    assert value == {"sha": "abc", "n": 3}
    # Absent key: default= when given, None otherwise.
    assert (
        await verbs_state._state_get_verb(
            ctx, "state.get", {"key": "missing", "default": "fallback"}
        )
        == "fallback"
    )
    assert (
        await verbs_state._state_get_verb(ctx, "state.get", {"key": "missing"})
        is None
    )


async def test_state_isolated_per_workflow_and_user(monkeypatch, tmp_path):
    _patch_state_store(monkeypatch, tmp_path)
    await verbs_state._state_set_verb(
        _ctx(workflow_id="wf-1"), "state.set", {"key": "k", "value": "mine"}
    )
    other_workflow = await verbs_state._state_get_verb(
        _ctx(workflow_id="wf-2"), "state.get", {"key": "k"}
    )
    other_user = await verbs_state._state_get_verb(
        _ctx(workflow_id="wf-1", user_id="someone-else"), "state.get", {"key": "k"}
    )
    assert other_workflow is None
    assert other_user is None


async def test_state_delete(monkeypatch, tmp_path):
    _patch_state_store(monkeypatch, tmp_path)
    ctx = _ctx(workflow_id="wf-1")
    await verbs_state._state_set_verb(ctx, "state.set", {"key": "k", "value": 1})
    first = await verbs_state._state_delete_verb(ctx, "state.delete", {"key": "k"})
    second = await verbs_state._state_delete_verb(ctx, "state.delete", {"key": "k"})
    assert first == {"key": "k", "deleted": True}
    assert second == {"key": "k", "deleted": False}
    assert (
        await verbs_state._state_get_verb(ctx, "state.get", {"key": "k"}) is None
    )


async def test_state_cap_enforced(monkeypatch, tmp_path):
    _patch_state_store(monkeypatch, tmp_path)
    ctx = _ctx(workflow_id="wf-1", budget=WorkflowBudget(state_cap_bytes=16))
    with pytest.raises(VerbError) as excinfo:
        await verbs_state._state_set_verb(
            ctx, "state.set", {"key": "k", "value": "x" * 64}
        )
    assert "byte cap" in str(excinfo.value)


async def test_state_shrinking_writes_recover_over_cap_documents(
    monkeypatch, tmp_path
):
    # Write under a generous cap, then lower it: delete/set must still offer
    # a way OUT of the over-cap document (shrinking writes always allowed).
    _patch_state_store(monkeypatch, tmp_path)
    big = _ctx(workflow_id="wf-1", budget=WorkflowBudget(state_cap_bytes=4096))
    await verbs_state._state_set_verb(big, "state.set", {"key": "a", "value": "x" * 200})
    await verbs_state._state_set_verb(big, "state.set", {"key": "b", "value": "y" * 200})

    small = _ctx(workflow_id="wf-1", budget=WorkflowBudget(state_cap_bytes=64))
    # A growing write is still refused under the lowered cap...
    with pytest.raises(VerbError):
        await verbs_state._state_set_verb(
            small, "state.set", {"key": "c", "value": "z" * 200}
        )
    # ...but deleting a key (a shrinking write) succeeds and recovers space.
    result = await verbs_state._state_delete_verb(small, "state.delete", {"key": "a"})
    assert result == {"key": "a", "deleted": True}
    # And a shrinking set is allowed too.
    stored = await verbs_state._state_set_verb(
        small, "state.set", {"key": "b", "value": "tiny"}
    )
    assert stored == {"key": "b", "stored": True}


async def test_state_corrupt_document_reads_empty(monkeypatch, tmp_path):
    _patch_state_store(monkeypatch, tmp_path)
    ctx = _ctx(workflow_id="wf-1")
    path = verbs_state._state_path("wf-1", "tester")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert (
        await verbs_state._state_get_verb(
            ctx, "state.get", {"key": "k", "default": "d"}
        )
        == "d"
    )


async def test_state_key_and_value_validation(monkeypatch, tmp_path):
    _patch_state_store(monkeypatch, tmp_path)
    ctx = _ctx(workflow_id="wf-1")
    with pytest.raises(VerbError):
        await verbs_state._state_get_verb(ctx, "state.get", {"key": "  "})
    with pytest.raises(VerbError) as excinfo:
        await verbs_state._state_set_verb(ctx, "state.set", {"key": "k"})
    assert "requires a value" in str(excinfo.value)


# --- registry, budget, wire --------------------------------------------------


async def test_all_builtin_verbs_registered_with_positionals():
    load_builtin_verbs()
    meta = verb_metadata()
    assert meta["llm"]["positional"] == ["prompt"]
    assert meta["thread"]["positional"] == ["prompt"]
    assert meta["threads.create"]["positional"] == ["title"]
    assert meta["threads.configure"]["positional"] == ["id_or_title"]
    assert meta["state.get"]["positional"] == ["key"]
    assert meta["state.set"]["positional"] == ["key", "value"]
    assert meta["state.delete"]["positional"] == ["key"]
    assert meta["emit"]["positional"] == ["event_type", "payload"]
    assert meta["todo.add"]["positional"] == ["task"]
    assert meta["notify"]["positional"] == ["message"]
    assert meta["memory.add"]["positional"] == ["content"]
    assert meta["memory.read"]["positional"] == ["scope"]


async def test_ai_verbs_get_wall_clock_timeout():
    budget = WorkflowBudget(wall_clock_seconds=300.0, verb_timeout_seconds=15.0)
    assert budget.resolve_verb_timeout() == 15.0
    assert budget.resolve_verb_timeout(ai=True) == 300.0


async def test_runner_wire_safe_duck_types_pydantic_shapes():
    class FakeModel:
        @classmethod
        def model_json_schema(cls):
            return {"type": "object", "required": ["a"]}

    class FakeInstance:
        def model_dump(self):
            return {"a": 1}

    class Broken:
        @classmethod
        def model_json_schema(cls):
            raise RuntimeError("boom")

    assert wf_runner._wire_safe(FakeModel) == {"type": "object", "required": ["a"]}
    assert wf_runner._wire_safe(FakeInstance()) == {"a": 1}
    assert wf_runner._wire_safe(Broken) is Broken
    assert wf_runner._wire_safe("plain") == "plain"
    assert wf_runner._wire_safe({"k": 1}) == {"k": 1}


# --- end to end --------------------------------------------------------------


async def test_end_to_end_llm_schema_wire_path(monkeypatch):
    """The child passes a pydantic-style CLASS; the parent receives a JSON
    Schema dict and the structured helper returns a dict to the author."""
    received = {}

    def fake_build(agent, thread_id, model, acting_user_id=None):
        return FakeLLM(native={"answer": 42})

    async def fake_structured(llm, prompt, schema):
        received["schema"] = schema
        return {"answer": 42}

    monkeypatch.setattr(verbs_llm, "_current_agent", lambda: object())
    monkeypatch.setattr(verbs_llm, "build_llm_for_thread", fake_build)
    monkeypatch.setattr(verbs_llm, "ainvoke_structured", fake_structured)
    source = (
        "class Answer:\n"
        "    @classmethod\n"
        "    def model_json_schema(cls):\n"
        "        return {'type': 'object', 'required': ['answer']}\n"
        "\n"
        "def run():\n"
        "    out = nym.llm('the question', schema=Answer)\n"
        "    return {'got': out['answer']}\n"
    )
    result = await execute_workflow(
        source=source,
        user_id="tester",
        thread_id="t1",
        budget=WorkflowBudget(wall_clock_seconds=30.0),
        persist_record=False,
    )
    assert result.envelope.status == "ok"
    assert result.envelope.output == {"got": 42}
    assert received["schema"] == {"type": "object", "required": ["answer"]}
