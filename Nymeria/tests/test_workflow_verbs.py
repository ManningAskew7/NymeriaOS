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
from nymeria.core.workflows import verbs_effects, verbs_llm, verbs_thread
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

    def fake_build(agent, thread_id, model):
        seen["thread_id"], seen["model"] = thread_id, model
        return llm

    monkeypatch.setattr(verbs_llm, "_current_agent", lambda: object())
    monkeypatch.setattr(verbs_llm, "build_llm_for_thread", fake_build)
    result = await verbs_llm._llm_verb(_ctx(), "llm", {"prompt": "hi"})
    assert result == "the answer"
    assert seen == {"thread_id": "t1", "model": "fast"}


async def test_llm_verb_schema_returns_dict(monkeypatch):
    monkeypatch.setattr(verbs_llm, "_current_agent", lambda: object())
    monkeypatch.setattr(
        verbs_llm,
        "build_llm_for_thread",
        lambda agent, tid, model: FakeLLM(native={"answer": 1}),
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
        lambda agent, tid, model: FakeLLM(replies=[""]),
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
        get_llm_config_for_thread=lambda thread_id="": cfg,
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
    with pytest.raises(VerbError) as excinfo:
        await verbs_thread._thread_verb(
            _ctx(), "thread", {"prompt": "p", "kit": "web-research"}
        )
    assert "kit=" in str(excinfo.value)
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
        },
    )
    assert "child says hi" in result
    assert seen["args"]["optional_tools"] == ["web_search"]
    assert seen["args"]["llm_provider"] == "openai"
    assert seen["args"]["llm_model"] == "gpt-x"
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
        verbs_thread, "build_llm_for_thread", lambda agent, tid, model: FakeLLM()
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


# --- registry, budget, wire --------------------------------------------------


async def test_all_phase2_verbs_registered_with_positionals():
    load_builtin_verbs()
    meta = verb_metadata()
    assert meta["llm"]["positional"] == ["prompt"]
    assert meta["thread"]["positional"] == ["prompt"]
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

    def fake_build(agent, thread_id, model):
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
