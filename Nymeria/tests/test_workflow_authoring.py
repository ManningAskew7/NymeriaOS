"""Tests for workflow custom-tool authoring: schema derivation, static
validation, revision hashing, the approval gate, budget overrides, source
retention, the tool-runtime wrapper, and run-record read/write."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nymeria.core.workflows import authoring
from nymeria.core.workflows.authoring import (
    approval_state,
    approve_revision,
    budget_from_config,
    compute_revision_hash,
    config_revision_hash,
    decline_revision,
    retain_source_revision,
    stamp_revision,
    validate_workflow_static,
    workflow_execution_gate,
)
from nymeria.core.workflows.envelope import (
    WorkflowEnvelope,
    WorkflowError,
    error_envelope,
    ok_envelope,
)
from nymeria.core.workflows.tool_runtime import (
    format_envelope_for_agent,
    run_workflow_tool,
)
from nymeria.core.workflows.trace import StepTrace, persist_run_record, read_run_records
from nymeria.tools.definitions.custom_tool_schema import (
    CustomToolDefinition,
    ToolParameter,
    WorkflowToolConfig,
)


def _config(source: str, entrypoint: str = "run", **kwargs) -> WorkflowToolConfig:
    return WorkflowToolConfig(source_code=source, entrypoint=entrypoint, **kwargs)


def _validated(source: str, **kwargs):
    params, errors = validate_workflow_static(config=_config(source, **kwargs))
    assert errors == [], errors
    return params


# --- schema -------------------------------------------------------------------


def test_workflow_definition_requires_workflow_config():
    with pytest.raises(ValueError, match="workflow_config is required"):
        CustomToolDefinition(
            id="wf_tool",
            name="WF",
            description="d",
            implementation_type="workflow",
        )


def test_workflow_config_clamps_budget_overrides():
    with pytest.raises(ValueError):
        _config("def run():\n    return 1\n", wall_clock_seconds=10_000)
    with pytest.raises(ValueError):
        _config("def run():\n    return 1\n", max_calls=0)
    with pytest.raises(ValueError):
        _config("def run():\n    return 1\n", continuations=["not valid!"])


# --- parameter derivation -----------------------------------------------------


def test_derive_parameters_supported_hints_and_defaults():
    source = '''
def run(name: str, count: int, ratio: float, active: bool, items: list, meta: dict, limit: int = 5):
    """Do a thing.

    Args:
        name: The item name.
        count: How many.
    """
    return name
'''
    params = _validated(source)
    assert params["name"].type == "string" and params["name"].required
    assert params["name"].description == "The item name."
    assert params["count"].type == "integer" and params["count"].description == "How many."
    assert params["ratio"].type == "number"
    assert params["active"].type == "boolean"
    assert params["items"].type == "array"
    assert params["meta"].type == "object"
    assert params["limit"].required is False and params["limit"].default == 5


def test_derive_parameters_optional_shapes():
    source = (
        "from typing import Optional, Union\n"
        "def run(a: Optional[str], b: str | None, c: Union[int, None], d: list[str], e: dict[str, int]):\n"
        "    return a\n"
    )
    params = _validated(source)
    for key, json_type in (("a", "string"), ("b", "string"), ("c", "integer")):
        assert params[key].type == json_type
        assert params[key].required is False
    assert params["d"].type == "array" and params["d"].required
    assert params["e"].type == "object"


def test_derive_parameters_sphinx_docstring_and_kwonly():
    source = (
        "def run(a: str, *, b: int = 2):\n"
        '    """Runs.\n\n    :param a: alpha value\n    """\n'
        "    return a\n"
    )
    params = _validated(source)
    assert params["a"].description == "alpha value"
    assert params["b"].required is False and params["b"].default == 2


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("def run(a):\n    return a\n", "needs a type hint"),
        ("def run(a: bytes):\n    return a\n", "unsupported type hint"),
        ("def run(*args):\n    return 1\n", "*args is not supported"),
        ("def run(**kw):\n    return 1\n", "**kwargs is not supported"),
        ("def run(a: str = object()):\n    return a\n", "must be a literal"),
        (
            "from typing import Union\ndef run(a: Union[str, int]):\n    return a\n",
            "only Union[T, None]",
        ),
        # 'config' collides with the langchain RunnableConfig injection on the
        # bound tool; underscore/model_ names break the pydantic args schema.
        ("def run(config: str):\n    return config\n", "reserved name"),
        ("def run(_hidden: str):\n    return _hidden\n", "reserved name"),
        ("def run(model_config: str):\n    return model_config\n", "reserved name"),
    ],
)
def test_derive_parameters_rejections(source: str, fragment: str):
    _, errors = validate_workflow_static(config=_config(source))
    assert any(fragment in error for error in errors), errors


def test_docstring_returns_section_does_not_leak_descriptions():
    # No blank line before 'Returns:': its entries must not become parameter
    # descriptions (they feed the revision hash).
    source = (
        "def run(result: str):\n"
        '    """Runs.\n\n'
        "    Args:\n"
        "        other: unused entry.\n"
        "    Returns:\n"
        "        result: leaked text.\n"
        '    """\n'
        "    return result\n"
    )
    params = _validated(source)
    assert params["result"].description == ""


# --- static validation --------------------------------------------------------


def test_static_validation_rejects_secrets_and_top_level_statements():
    _, errors = validate_workflow_static(
        config=_config('KEY = "x"\nprint(KEY)\ndef run():\n    return KEY\n')
    )
    assert any("top-level Expr is not allowed" in e for e in errors)
    _, errors = validate_workflow_static(
        config=_config('TOKEN = "ghp_' + "a" * 40 + '"\ndef run():\n    return 1\n')
    )
    assert any("raw secret" in e for e in errors)


def test_static_validation_syntax_error_short_circuits():
    _, errors = validate_workflow_static(config=_config("def run(:\n"))
    assert len(errors) == 1 and errors[0].startswith("syntax error")


def test_static_validation_missing_entrypoint():
    _, errors = validate_workflow_static(config=_config("def other():\n    return 1\n"))
    assert any("entrypoint function 'run' was not found" in e for e in errors)


def test_continuation_signature_checks():
    good = (
        "def run():\n    return 1\n"
        "def after(state, decision):\n    return state\n"
    )
    params, errors = validate_workflow_static(
        config=_config(good, continuations=["after"])
    )
    assert errors == []

    bad = "def run():\n    return 1\ndef after(state):\n    return state\n"
    _, errors = validate_workflow_static(config=_config(bad, continuations=["after"]))
    assert any("exactly (state, decision)" in e for e in errors)

    _, errors = validate_workflow_static(config=_config(good, continuations=["missing"]))
    assert any("continuation function 'missing' was not found" in e for e in errors)

    _, errors = validate_workflow_static(config=_config(good, continuations=["run"]))
    assert any("cannot be the main entrypoint" in e for e in errors)

    _, errors = validate_workflow_static(
        config=_config(good, continuations=["after", "after"])
    )
    assert any("duplicate continuation" in e for e in errors)

    star = "def run():\n    return 1\ndef after(*args, **kw):\n    return 1\n"
    _, errors = validate_workflow_static(config=_config(star, continuations=["after"]))
    assert any("must not use *args" in e for e in errors)


# --- revision hash and the gate -------------------------------------------------


def _simple_params() -> dict:
    return {
        "a": ToolParameter(type="string", required=True),
        "b": ToolParameter(type="integer", required=False, default=2),
    }


def test_revision_hash_is_canonical():
    params = _simple_params()
    reordered = {"b": params["b"], "a": params["a"]}
    base = compute_revision_hash(
        source="def run():\n    return 1\n",
        entrypoint="run",
        continuations=["x", "y"],
        parameters=params,
    )
    assert base == compute_revision_hash(
        source="def run():\n    return 1\n",
        entrypoint="run",
        continuations=["y", "x"],
        parameters=reordered,
    )
    assert base != compute_revision_hash(
        source="def run():\n    return 2\n",
        entrypoint="run",
        continuations=["x", "y"],
        parameters=params,
    )
    assert base != compute_revision_hash(
        source="def run():\n    return 1\n",
        entrypoint="other",
        continuations=["x", "y"],
        parameters=params,
    )


def test_gate_lifecycle_approve_edit_decline():
    source = "def run(a: str):\n    return a\n"
    config = _config(source)
    params = _validated(source)
    config = stamp_revision(config, params)

    # Fresh: not approved yet.
    refusal = workflow_execution_gate(config, params)
    assert refusal is not None and "not approved yet" in refusal
    assert approval_state(config, params) == "pending"

    # Approved: gate opens.
    config = approve_revision(config, params, approved_by="admin1")
    assert workflow_execution_gate(config, params) is None
    assert approval_state(config, params) == "approved"
    assert config.approved_revision == config_revision_hash(config, params)

    # Behavior edit: approval resets by construction.
    edited = config.model_copy(update={"source_code": "def run(a: str):\n    return a + '!'\n"})
    refusal = workflow_execution_gate(edited, params)
    assert refusal is not None and "changed since its approval" in refusal
    assert approval_state(edited, params) == "pending"

    # Declined revision: distinct copy, pinned to the declined hash.
    declined = decline_revision(edited, params, declined_by="admin1", note="too risky")
    refusal = workflow_execution_gate(declined, params)
    assert refusal is not None and "declined" in refusal and "too risky" in refusal
    assert approval_state(declined, params) == "declined"

    # Editing again after a decline returns to pending (new hash).
    re_edited = declined.model_copy(
        update={"source_code": "def run(a: str):\n    return a + '?'\n"}
    )
    assert approval_state(re_edited, params) == "pending"


def test_approve_recomputes_hash_ignoring_stale_stored_field():
    source = "def run(a: str):\n    return a\n"
    params = _validated(source)
    config = _config(source).model_copy(update={"revision_hash": "stale"})
    approved = approve_revision(config, params, approved_by="admin1")
    assert approved.revision_hash != "stale"
    assert workflow_execution_gate(approved, params) is None


# --- budget overrides -----------------------------------------------------------


def test_budget_from_config_overrides_and_defaults():
    source = "def run():\n    return 1\n"
    default_budget = budget_from_config(_config(source))
    assert default_budget.wall_clock_seconds == 600
    assert default_budget.max_calls == 50

    overridden = budget_from_config(
        _config(source, wall_clock_seconds=900, max_calls=100, max_ai_calls=20)
    )
    assert overridden.wall_clock_seconds == 900
    assert overridden.max_calls == 100
    assert overridden.max_ai_calls == 20


# --- source retention -----------------------------------------------------------


def test_retain_source_revision_prunes_to_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "nymeria.config.settings.get_settings",
        lambda: SimpleNamespace(custom_tools_dir=tmp_path),
    )
    for index in range(authoring.MAX_SOURCE_REVISIONS + 3):
        revision = f"{index:02d}" + "a" * 62  # distinct 16-char prefixes
        retain_source_revision("wf_tool", revision, f"def run():\n    return {index}\n")
    revisions = list((tmp_path / "revisions" / "wf_tool").glob("*.py"))
    assert len(revisions) == authoring.MAX_SOURCE_REVISIONS


def test_retain_source_revision_swallows_unexpected_errors(monkeypatch):
    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr("nymeria.config.settings.get_settings", _boom)
    # Best effort by contract: retention failure never blocks a save.
    retain_source_revision("wf_tool", "b" * 64, "def run():\n    return 1\n")


# --- tool runtime (envelope formatting + wrapper) --------------------------------


def test_format_envelope_ok_and_error_shapes():
    ok_text = format_envelope_for_agent(
        ok_envelope({"answer": 42}, {"calls_used": 3, "ai_calls_used": 1, "wall_seconds": 2.5})
    )
    assert '"answer": 42' in ok_text
    assert "[workflow: 3 calls, 1 ai, 2.5s]" in ok_text

    err_text = format_envelope_for_agent(
        error_envelope(
            WorkflowError(
                kind="author_error",
                message="boom",
                step=4,
                verb="llm",
                traceback="Traceback: boom",
            ),
            {"calls_used": 4, "ai_calls_used": 2, "wall_seconds": 1.0},
        )
    )
    assert err_text.startswith("[Error]: workflow error (author_error) - boom")
    assert "(step 4, verb llm)" in err_text
    assert "Traceback: boom" in err_text


class _FakeLoader:
    def __init__(self, definition):
        self._definition = definition

    def get_definition(self, tool_id):
        return self._definition


def _approved_definition(source: str = "def run(a: str):\n    return a\n"):
    params, errors = validate_workflow_static(config=_config(source))
    assert errors == []
    config = approve_revision(_config(source), params, approved_by="admin1")
    return CustomToolDefinition(
        id="wf_echo",
        name="WF Echo",
        description="echo",
        parameters=params,
        implementation_type="workflow",
        workflow_config=config,
    )


def test_run_workflow_tool_gate_and_missing_definition():
    missing = asyncio.run(run_workflow_tool(_FakeLoader(None), "wf_echo", {}, None))
    assert "[Error]" in missing and "not found" in missing

    definition = _approved_definition()
    assert definition.workflow_config is not None
    definition.workflow_config = stamp_revision(
        definition.workflow_config.model_copy(update={"approved_revision": None}),
        definition.parameters,
    )
    gated = asyncio.run(run_workflow_tool(_FakeLoader(definition), "wf_echo", {}, None))
    assert gated.startswith("[Error]: approval_required")


def test_run_workflow_tool_success_and_depth(monkeypatch):
    definition = _approved_definition()
    captured: dict = {}

    async def fake_execute_workflow(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            envelope=ok_envelope("done", {"calls_used": 1, "ai_calls_used": 0, "wall_seconds": 0.1})
        )

    monkeypatch.setattr(
        "nymeria.core.workflows.executor.execute_workflow", fake_execute_workflow
    )
    config = {"configurable": {"user_id": "u1", "thread_id": "t1", "workflow_depth": 1}}
    result = asyncio.run(
        run_workflow_tool(_FakeLoader(definition), "wf_echo", {"a": "hi"}, config)
    )
    assert result.startswith("done")
    assert captured["user_id"] == "u1"
    assert captured["thread_id"] == "t1"
    assert captured["depth"] == 1
    assert captured["params"] == {"a": "hi"}
    assert captured["workflow_id"] == "wf_echo"

    # Past max_depth the call is refused BEFORE spawning.
    deep = {"configurable": {"user_id": "u1", "thread_id": "t1", "workflow_depth": 3}}
    refused = asyncio.run(
        run_workflow_tool(_FakeLoader(definition), "wf_echo", {"a": "hi"}, deep)
    )
    assert "nesting depth" in refused and refused.startswith("[Error]")


# --- run records ------------------------------------------------------------------


def test_persist_and_read_run_records(tmp_path, monkeypatch):
    # trace.py imports get_settings from the config package, not .settings.
    monkeypatch.setattr(
        "nymeria.config.get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )
    for index, user in enumerate(["u1", "u1", "u2"]):
        trace = StepTrace(run_id=f"run{index}", workflow_id="wf_echo")
        envelope = WorkflowEnvelope(ok=True, status="ok", output=index).to_dict()
        assert (
            persist_run_record(trace, envelope, user_id=user, thread_id=f"t{index}")
            is not None
        )

    records = read_run_records("wf_echo", limit=10)
    assert len(records) == 3
    assert {r["user_id"] for r in records} == {"u1", "u2"}
    assert all(r["status"] == "ok" and r["timestamp"] for r in records)

    mine = read_run_records("wf_echo", limit=10, user_id="u1")
    assert len(mine) == 2 and all(r["user_id"] == "u1" for r in mine)

    # Corrupt files are skipped, never raised.
    (tmp_path / "workflows" / "runs" / "wf_echo" / "bad.json").write_text("{nope")
    assert len(read_run_records("wf_echo", limit=10)) == 3

    assert read_run_records("missing_wf") == []


# --- headless binding validation (phase 4) --------------------------------------


BINDING_SOURCE = (
    "def run(event: dict, name: str, count: int = 2):\n"
    "    return {'name': name}\n"
)


def _published_binding_definition(monkeypatch, *, approved: bool = True):
    config = _config(BINDING_SOURCE)
    params, errors = validate_workflow_static(config=config)
    assert errors == [], errors
    if approved:
        config = approve_revision(config, params, approved_by="admin")
    definition = SimpleNamespace(
        implementation_type="workflow", workflow_config=config, parameters=params
    )
    monkeypatch.setattr(
        "nymeria.core.custom_tools.get_custom_tool_loader",
        lambda: SimpleNamespace(
            get_definition=lambda wf: definition if wf == "wf_bind" else None
        ),
    )
    return definition


def test_workflow_binding_error_paths(monkeypatch):
    from nymeria.core.workflows.tool_runtime import (
        workflow_binding_error,
        workflow_declares_event,
    )

    _published_binding_definition(monkeypatch)

    assert "no published workflow tool" in workflow_binding_error("ghost", {})
    # Sound binding: event supplied by the firing surface, name bound.
    assert (
        workflow_binding_error("wf_bind", {"name": "x"}, allow_event=True) is None
    )
    # Required name not covered.
    assert "does not cover" in workflow_binding_error(
        "wf_bind", {}, allow_event=True
    )
    # event declared and required, but this surface never supplies one.
    assert "does not cover" in workflow_binding_error(
        "wf_bind", {"name": "x"}, allow_event=False
    )
    assert "unknown parameter" in workflow_binding_error(
        "wf_bind", {"name": "x", "bogus": 1}, allow_event=True
    )
    assert workflow_declares_event("wf_bind") is True
    assert workflow_declares_event("ghost") is False


def test_workflow_binding_error_regates_revision(monkeypatch):
    from nymeria.core.workflows.tool_runtime import workflow_binding_error

    _published_binding_definition(monkeypatch, approved=False)
    error = workflow_binding_error("wf_bind", {"name": "x"}, allow_event=True)
    assert error is not None and "approv" in error
