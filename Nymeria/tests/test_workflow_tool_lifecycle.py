"""Tests for the workflow custom-tool lifecycle: tool_create draft/test/
publish with the per-revision approval gate, the loader execution path, the
shared approval service, the REST approval router, and workflow_info."""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from nymeria.core.custom_tools import CustomToolLoader
from nymeria.core.workflows.envelope import ok_envelope

from nymeria.tools.tool_create import (
    ToolDraftStore,
    _publish_draft,
    create_draft_definition,
    list_pending_workflows,
    resolve_workflow_approval,
    tool_create as tool_create_tool,
)

# The package exports a `tool_create` tool object that shadows the submodule
# on attribute access; importlib resolves the real module for monkeypatching.
tool_create_module = importlib.import_module("nymeria.tools.tool_create")
workflow_info_module = importlib.import_module("nymeria.tools.workflow_info")

WF_SOURCE = (
    "def run(name: str, count: int = 2):\n"
    '    """Echo.\n\n    Args:\n        name: What to echo.\n    """\n'
    "    return nym.tools.hello_test(name=name)\n"
)


def _fake_agent_with_role(role: str, extra_admins: tuple[str, ...] = ()):
    users = [SimpleNamespace(id="u1", role=role, disabled=False)]
    users += [SimpleNamespace(id=a, role="admin", disabled=False) for a in extra_admins]
    by_id = {u.id: u for u in users}
    repo = SimpleNamespace(
        get_user_by_id=lambda uid: by_id.get(uid),
        list_users=lambda: list(users),
    )
    return SimpleNamespace(accounts_repo=repo)


@pytest.fixture()
def wf_env(tmp_path, monkeypatch):
    """Sandbox the draft store, loader, and settings dirs under tmp_path."""
    store = ToolDraftStore(tmp_path / "tool_drafts")
    loader = CustomToolLoader(tmp_path / "custom_tools")
    fake_settings = SimpleNamespace(
        data_dir=tmp_path,
        custom_tools_dir=tmp_path / "custom_tools",
    )
    monkeypatch.setattr(tool_create_module, "_draft_store", lambda: store)
    monkeypatch.setattr(tool_create_module, "get_custom_tool_loader", lambda: loader)
    monkeypatch.setattr(tool_create_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr("nymeria.config.settings.get_settings", lambda: fake_settings)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: fake_settings)
    return SimpleNamespace(store=store, loader=loader, tmp=tmp_path)


async def _fake_execute_workflow(**kwargs):
    return SimpleNamespace(
        envelope=ok_envelope(
            f"ran:{kwargs.get('entrypoint')}",
            {"calls_used": 1, "ai_calls_used": 0, "wall_seconds": 0.1},
        )
    )


def _tool_config(user_id: str = "u1", thread_id: str = "t1") -> dict:
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


def _run_tool(**kwargs):
    coroutine = cast(Any, tool_create_tool).coroutine
    return json.loads(
        asyncio.run(coroutine(tool_call_id="call-1", config=_tool_config(), **kwargs))
    )


# --- draft ---------------------------------------------------------------------


def test_workflow_draft_admin_auto_approves(wf_env):
    with patch(
        "nymeria.core.agent.get_current_agent",
        return_value=_fake_agent_with_role("admin"),
    ):
        payload = _run_tool(
            action="draft",
            implementation_type="workflow",
            tool_id="wf_lc_echo",
            name="WF Echo",
            description="Echo via workflow",
            python_code=WF_SOURCE,
        )
    assert payload["ok"] is True
    draft = payload["draft"]
    assert draft["approval"] == "approved"
    assert draft["parameter_names"] == ["count", "name"]
    assert "auto-approved" in payload["approval_note"]
    saved = wf_env.store.get("u1", "wf_lc_echo")
    assert saved.parameters["name"].required is True
    assert saved.parameters["name"].description == "What to echo."
    assert saved.parameters["count"].required is False


def test_workflow_draft_non_admin_pends_and_notifies(wf_env):
    notified: list[tuple[str, str]] = []

    def fake_notify(*, user_id, summary, thread_id="", task_id=None):
        notified.append((user_id, summary))

    with patch(
        "nymeria.core.agent.get_current_agent",
        return_value=_fake_agent_with_role("user", extra_admins=("boss", "boss2")),
    ), patch("nymeria.core.notifications.create_notification", fake_notify):
        payload = _run_tool(
            action="draft",
            implementation_type="workflow",
            tool_id="wf_lc_pending",
            name="WF Pending",
            description="Pending workflow",
            python_code=WF_SOURCE,
        )
    assert payload["ok"] is True
    assert payload["draft"]["approval"] == "pending"
    assert "awaits admin approval" in payload["approval_note"]
    assert sorted(uid for uid, _ in notified) == ["boss", "boss2"]
    assert all("wf_lc_pending" in text for _, text in notified)


def test_workflow_draft_rejects_declared_parameters_and_bad_budget(wf_env):
    with pytest.raises(ValueError, match="derived from the entrypoint"):
        create_draft_definition(
            user_id="u1",
            tool_id="wf_lc_params",
            name="WF",
            description="d",
            parameters={"x": {"type": "string"}},
            http_config=None,
            implementation_type="workflow",
            python_code=WF_SOURCE,
        )
    with pytest.raises(ValueError, match="unknown budget key"):
        create_draft_definition(
            user_id="u1",
            tool_id="wf_lc_budget",
            name="WF",
            description="d",
            parameters=None,
            http_config=None,
            implementation_type="workflow",
            python_code=WF_SOURCE,
            budget={"nope": 1},
        )


def test_workflow_draft_static_validation_errors_surface(wf_env):
    with patch(
        "nymeria.core.agent.get_current_agent",
        return_value=_fake_agent_with_role("admin"),
    ):
        payload = _run_tool(
            action="draft",
            implementation_type="workflow",
            tool_id="wf_lc_invalid",
            name="WF",
            description="d",
            python_code="def run(a):\n    return a\n",
        )
    assert payload["ok"] is False
    assert payload["error"]["type"] == "validation_error"
    assert "type hint" in payload["error"]["message"]


# --- test ----------------------------------------------------------------------


def _draft_workflow(role: str = "admin", tool_id: str = "wf_lc_echo") -> dict:
    with patch(
        "nymeria.core.agent.get_current_agent",
        return_value=_fake_agent_with_role(role),
    ):
        return _run_tool(
            action="draft",
            implementation_type="workflow",
            tool_id=tool_id,
            name="WF Echo",
            description="Echo via workflow",
            python_code=WF_SOURCE,
        )


def test_workflow_test_requires_approval(wf_env):
    _draft_workflow(role="user", tool_id="wf_lc_gated")
    with patch(
        "nymeria.core.agent.get_current_agent",
        return_value=_fake_agent_with_role("user"),
    ):
        payload = _run_tool(
            action="test", tool_id="wf_lc_gated", sample_params={"name": "hi"}
        )
    assert payload["ok"] is False
    assert payload["error"]["type"] == "approval_required"
    assert "not approved yet" in payload["error"]["message"]


def test_workflow_test_runs_engine_when_approved(wf_env, monkeypatch):
    _draft_workflow(role="admin")
    monkeypatch.setattr(
        "nymeria.core.workflows.executor.execute_workflow", _fake_execute_workflow
    )
    with patch(
        "nymeria.core.agent.get_current_agent",
        return_value=_fake_agent_with_role("admin"),
    ):
        payload = _run_tool(
            action="test", tool_id="wf_lc_echo", sample_params={"name": "hi"}
        )
    assert payload["ok"] is True
    assert "ran:run" in payload["response_preview"]
    assert wf_env.store.get("u1", "wf_lc_echo").last_test_ok is True


def test_workflow_test_missing_required_param(wf_env):
    _draft_workflow(role="admin")
    with patch(
        "nymeria.core.agent.get_current_agent",
        return_value=_fake_agent_with_role("admin"),
    ):
        payload = _run_tool(action="test", tool_id="wf_lc_echo", sample_params={})
    assert payload["ok"] is False
    assert "missing required parameter" in payload["error"]["message"]


# --- publish -------------------------------------------------------------------


def test_workflow_publish_requires_test_then_persists(wf_env, monkeypatch):
    _draft_workflow(role="admin")
    monkeypatch.setattr(
        "nymeria.core.workflows.executor.execute_workflow", _fake_execute_workflow
    )
    with patch(
        "nymeria.core.agent.get_current_agent",
        return_value=_fake_agent_with_role("admin"),
    ):
        # Untested (and no sample_params shortcut for workflows).
        payload = _run_tool(
            action="publish", tool_id="wf_lc_echo", sample_params={"name": "hi"}
        )
        assert payload["ok"] is False
        assert payload["error"]["type"] == "untested_draft"

        _run_tool(action="test", tool_id="wf_lc_echo", sample_params={"name": "hi"})

    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        result = _publish_draft(
            store=wf_env.store,
            user_id="u1",
            draft_id="wf_lc_echo",
            thread_id="t1",
            ttl="2h",
            tool_call_id="call-2",
        )
    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["tool"]["implementation_type"] == "workflow"
    assert payload["tool"]["approval"] == "approved"
    assert "workflow" in payload["tool"]["tags"]

    definition = wf_env.loader.get_definition("wf_lc_echo")
    assert definition is not None and definition.workflow_config is not None
    assert definition.workflow_config.approved_revision == definition.workflow_config.revision_hash
    # Source retention wrote a revision file for diffing.
    revisions = list((wf_env.tmp / "custom_tools" / "revisions" / "wf_lc_echo").glob("*.py"))
    assert len(revisions) == 1


def test_workflow_publish_gate_blocks_declined_revision(wf_env, monkeypatch):
    _draft_workflow(role="admin")
    monkeypatch.setattr(
        "nymeria.core.workflows.executor.execute_workflow", _fake_execute_workflow
    )
    with patch(
        "nymeria.core.agent.get_current_agent",
        return_value=_fake_agent_with_role("admin"),
    ):
        _run_tool(action="test", tool_id="wf_lc_echo", sample_params={"name": "hi"})
    with patch("nymeria.core.notifications.create_notification", lambda **kw: None):
        resolve_workflow_approval(
            kind="draft",
            owner_user_id="u1",
            target_id="wf_lc_echo",
            approve=False,
            actor_user_id="boss",
            note="not yet",
        )
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        payload = json.loads(
            _publish_draft(
                store=wf_env.store,
                user_id="u1",
                draft_id="wf_lc_echo",
                thread_id="t1",
                ttl="2h",
                tool_call_id="call-2",
            )
        )
    assert payload["ok"] is False
    assert payload["error"]["type"] == "approval_required"
    assert "declined" in payload["error"]["message"]


# --- approval service ------------------------------------------------------------


def test_pending_scan_and_resolution(wf_env):
    _draft_workflow(role="user", tool_id="wf_lc_a")
    _draft_workflow(role="admin", tool_id="wf_lc_b")  # auto-approved, not pending

    pending = list_pending_workflows()
    assert [p["tool_id"] for p in pending] == ["wf_lc_a"]
    assert pending[0]["kind"] == "draft"
    assert pending[0]["owner_user_id"] == "u1"

    notified: list[str] = []
    with patch(
        "nymeria.core.notifications.create_notification",
        lambda **kw: notified.append(kw["user_id"]),
    ):
        result = resolve_workflow_approval(
            kind="draft",
            owner_user_id="u1",
            target_id="wf_lc_a",
            approve=True,
            actor_user_id="boss",
        )
    assert result["decision"] == "approved"
    assert result["draft"]["approval"] == "approved"
    assert notified == ["u1"]
    assert list_pending_workflows() == []

    with pytest.raises(ValueError, match="not found"):
        resolve_workflow_approval(
            kind="draft",
            owner_user_id="u1",
            target_id="missing",
            approve=True,
            actor_user_id="boss",
        )


def test_resolution_on_published_tool(wf_env):
    _draft_workflow(role="admin", tool_id="wf_lc_pub")
    draft = wf_env.store.get("u1", "wf_lc_pub")
    # Simulate an edited published definition awaiting re-approval.
    from nymeria.tools.definitions.custom_tool_schema import CustomToolDefinition

    stale_config = draft.workflow_config.model_copy(
        update={"approved_revision": None, "approved_by": None, "approved_at": None}
    )
    definition = CustomToolDefinition(
        id="wf_lc_pub",
        name=draft.name,
        description=draft.description,
        parameters=draft.parameters,
        implementation_type="workflow",
        workflow_config=stale_config,
    )
    wf_env.loader.save_definition(definition)

    pending = list_pending_workflows()
    assert [p["kind"] for p in pending if p["tool_id"] == "wf_lc_pub"] == ["tool"]

    with patch("nymeria.core.notifications.create_notification", lambda **kw: None):
        result = resolve_workflow_approval(
            kind="tool",
            owner_user_id="",
            target_id="wf_lc_pub",
            approve=True,
            actor_user_id="boss",
        )
    assert result["decision"] == "approved"
    refreshed = wf_env.loader.get_definition("wf_lc_pub")
    assert refreshed.workflow_config.approved_by == "boss"


# --- loader execution path --------------------------------------------------------


def test_loader_binds_workflow_tool_and_injects_config(wf_env, monkeypatch):
    _draft_workflow(role="admin")
    monkeypatch.setattr(
        "nymeria.core.workflows.executor.execute_workflow", _fake_execute_workflow
    )
    with patch(
        "nymeria.core.agent.get_current_agent",
        return_value=_fake_agent_with_role("admin"),
    ):
        _run_tool(action="test", tool_id="wf_lc_echo", sample_params={"name": "hi"})
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        _publish_draft(
            store=wf_env.store,
            user_id="u1",
            draft_id="wf_lc_echo",
            thread_id="t1",
            ttl="2h",
            tool_call_id="call-2",
        )

    tools = wf_env.loader.load_all()
    tool = next(t for t in tools if t.name == "wf_lc_echo")

    captured: dict = {}

    async def capturing_execute(**kwargs):
        captured.update(kwargs)
        return await _fake_execute_workflow(**kwargs)

    monkeypatch.setattr(
        "nymeria.core.workflows.executor.execute_workflow", capturing_execute
    )
    # End to end through langchain: args_schema validation + RunnableConfig
    # injection (the bash.py pattern) + the execution-time re-gate.
    result = asyncio.run(tool.ainvoke({"name": "hi"}, _tool_config()))
    assert str(result).startswith("ran:run")
    assert captured["user_id"] == "u1"
    assert captured["thread_id"] == "t1"
    assert captured["params"]["name"] == "hi"
    assert captured["params"]["count"] == 2  # schema default applied

    # Revoking approval takes effect on the NEXT call without any reload.
    definition = wf_env.loader.get_definition("wf_lc_echo")
    definition.workflow_config = definition.workflow_config.model_copy(
        update={"approved_revision": None}
    )
    result = asyncio.run(tool.ainvoke({"name": "hi"}, _tool_config()))
    assert str(result).startswith("[Error]: approval_required")


def test_loader_skips_unknown_type_still(wf_env):
    # The dispatch switch: a workflow definition loads, an unknown type does not.
    _draft_workflow(role="admin")
    (wf_env.tmp / "custom_tools" / "broken.json").write_text(
        json.dumps(
            {
                "id": "broken",
                "name": "b",
                "description": "d",
                "implementation_type": "no_such_type",
            }
        )
    )
    names = [t.name for t in wf_env.loader.load_all()]
    assert "broken" not in names


# --- workflow_info -----------------------------------------------------------------


def _info(action: str, admin: bool, **kwargs) -> str:
    with patch.object(workflow_info_module, "is_admin", lambda uid: admin):
        return workflow_info_module.workflow_info.func(
            action=action, config=_tool_config(), **kwargs
        )


def test_workflow_info_list_show_pending(wf_env):
    _draft_workflow(role="user", tool_id="wf_lc_info")

    listing = _info("list", admin=False)
    assert "wf_lc_info" in listing and "approval=pending" in listing

    shown = _info("show", admin=False, workflow_id="wf_lc_info")
    assert "def run(" in shown and "approval=pending" in shown

    assert "admin-only" in _info("pending", admin=False)
    pending = _info("pending", admin=True)
    assert "wf_lc_info" in pending and "human-only" in pending


def test_workflow_info_log_reads_run_records(wf_env):
    from nymeria.core.workflows.trace import StepTrace, persist_run_record

    trace = StepTrace(run_id="r1", workflow_id="wf_lc_logged")
    persist_run_record(
        trace,
        {"ok": True, "status": "ok", "output": "x", "budget": {"calls_used": 2}},
        user_id="u1",
        thread_id="t1",
    )
    text = _info("log", admin=False, workflow_id="wf_lc_logged")
    assert "run=r1" in text and "status=ok" in text
    # Another user's runs are invisible to a non-admin.
    with patch.object(workflow_info_module, "is_admin", lambda uid: False):
        other = workflow_info_module.workflow_info.func(
            action="log",
            workflow_id="wf_lc_logged",
            config={"configurable": {"user_id": "someone_else", "thread_id": "t"}},
        )
    assert "no run records" in other
