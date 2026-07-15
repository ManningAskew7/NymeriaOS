"""Tests for bundled workflow templates: the catalog loader, the admin-gated
install engine, and the tool/read surfaces.

Mirrors the hook-template tests: the shipped catalog is validated at load, a
broken bundled file is skipped (not raised), and install publishes an approved
GLOBAL workflow tool (admin only, idempotent by tool id).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

import nymeria
from nymeria.core.custom_tools import CustomToolLoader
from nymeria.core.workflow_templates import (
    WorkflowTemplate,
    _validate_template,
    get_template,
    load_templates,
    template_parameter_names,
)
from nymeria.tools.tool_create import install_workflow_template

tool_create_module = importlib.import_module("nymeria.tools.tool_create")
tool_create_tool = tool_create_module.tool_create

BUNDLED_DIR = Path(nymeria.__file__).parent / "workflows_bundled"

# A minimal but complete valid template for directory-scoped loader tests.
VALID_TEMPLATE = {
    "id": "echo_tmpl",
    "name": "Echo",
    "description": "Echo a value.",
    "notes": "test only",
    "workflow": {
        "source_code": "def run(value: str):\n    return {'echo': value}\n",
        "entrypoint": "run",
        "continuations": [],
        "budget": {},
    },
}


def _fake_admin_agent(role: str = "admin"):
    user = SimpleNamespace(id="u1", role=role, disabled=False)
    repo = SimpleNamespace(
        get_user_by_id=lambda uid: user if uid == "u1" else None,
        list_users=lambda: [user],
    )
    return SimpleNamespace(accounts_repo=repo, reload_custom_tools=lambda: [])


@pytest.fixture()
def wf_env(tmp_path, monkeypatch):
    """Sandbox the loader + settings dirs; keep the REAL bundled catalog."""
    loader = CustomToolLoader(tmp_path / "custom_tools")
    fake_settings = SimpleNamespace(
        data_dir=tmp_path,
        custom_tools_dir=tmp_path / "custom_tools",
        bundled_workflows_dir=BUNDLED_DIR,
    )
    monkeypatch.setattr(tool_create_module, "get_custom_tool_loader", lambda: loader)
    monkeypatch.setattr(tool_create_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr("nymeria.config.settings.get_settings", lambda: fake_settings)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: fake_settings)
    return SimpleNamespace(loader=loader, tmp=tmp_path)


def _tool_config(user_id: str = "u1", thread_id: str = "t1") -> dict:
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


def _run_tool(**kwargs) -> dict:
    coroutine = cast(Any, tool_create_tool).coroutine
    import asyncio

    return json.loads(
        asyncio.run(coroutine(tool_call_id="call-1", config=_tool_config(), **kwargs))
    )


# --- catalog loader ------------------------------------------------------------


def test_bundled_catalog_loads_and_derives_params():
    """The shipped recipes load, static-validate, and derive expected params."""
    templates = {t.id: t for t in load_templates(BUNDLED_DIR)}
    assert set(templates) == {"two_thread_conversation", "url_watcher"}

    two = templates["two_thread_conversation"]
    assert template_parameter_names(two) == [
        "end_marker",
        "max_exchanges",
        "opening",
        "thread_a",
        "thread_b",
        "time_budget_seconds",
    ]
    watcher = templates["url_watcher"]
    assert template_parameter_names(watcher) == ["alert_after_failures", "url"]


def test_get_template_by_id():
    assert get_template("url_watcher", BUNDLED_DIR) is not None
    assert get_template("does_not_exist", BUNDLED_DIR) is None


def test_load_templates_skips_invalid_files(tmp_path):
    """A malformed or invalid bundled file is skipped, not raised."""
    (tmp_path / "echo_tmpl.json").write_text(json.dumps(VALID_TEMPLATE), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    # Structurally parseable but fails static validation (param has no type hint).
    bad_static = dict(VALID_TEMPLATE, id="bad_static")
    bad_static["workflow"] = dict(
        VALID_TEMPLATE["workflow"], source_code="def run(x):\n    return x\n"
    )
    (tmp_path / "bad_static.json").write_text(json.dumps(bad_static), encoding="utf-8")
    # Out-of-range budget (max_ai_calls le=100) fails WorkflowToolConfig -> skipped.
    bad_budget = dict(VALID_TEMPLATE, id="bad_budget")
    bad_budget["workflow"] = dict(VALID_TEMPLATE["workflow"], budget={"max_ai_calls": 9999})
    (tmp_path / "bad_budget.json").write_text(json.dumps(bad_budget), encoding="utf-8")

    templates = load_templates(tmp_path)
    assert [t.id for t in templates] == ["echo_tmpl"]


def test_template_id_pattern_rejected():
    with pytest.raises(Exception):
        WorkflowTemplate.model_validate(dict(VALID_TEMPLATE, id="Bad-Id"))


# --- install engine ------------------------------------------------------------


def test_install_admin_creates_approved_global_tool(wf_env):
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        definition, created = install_workflow_template(
            user_id="u1", template_id="url_watcher", agent=None, is_admin=True
        )
    assert created is True
    assert definition is not None
    assert definition.implementation_type == "workflow"
    assert "bundled" in definition.tags
    assert "template:url_watcher" in definition.tags
    # Self-approved on install (admin), so the execution gate admits it.
    cfg = definition.workflow_config
    assert cfg is not None and cfg.approved_revision == cfg.revision_hash
    # Persisted to the global registry + a retained source revision.
    saved = wf_env.loader.get_definition("url_watcher")
    assert saved is not None
    revisions = list((wf_env.tmp / "custom_tools" / "revisions" / "url_watcher").glob("*.py"))
    assert len(revisions) == 1


def test_install_is_idempotent_by_tool_id(wf_env):
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        _, created_first = install_workflow_template(
            user_id="u1", template_id="url_watcher", agent=None, is_admin=True
        )
        existing, created_second = install_workflow_template(
            user_id="u1", template_id="url_watcher", agent=None, is_admin=True
        )
    assert created_first is True
    assert created_second is False
    assert existing is not None and existing.id == "url_watcher"


def test_install_non_admin_refused(wf_env):
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        with pytest.raises(ValueError, match="requires an admin"):
            install_workflow_template(
                user_id="u2", template_id="url_watcher", agent=None, is_admin=False
            )
    # Nothing was published.
    assert wf_env.loader.get_definition("url_watcher") is None


def test_install_unknown_template_refused(wf_env):
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        with pytest.raises(ValueError, match="Unknown workflow template"):
            install_workflow_template(
                user_id="u1", template_id="nope", agent=None, is_admin=True
            )


def test_install_budget_keys_match_the_publish_path(wf_env):
    """The loader's budget-key tuple must not drift from the install path's.

    ``_build_config`` hand-builds the same config ``_coerce_workflow_config``
    builds at install (the loader stays store-shaped and cannot import the tools
    layer). A key added to one tuple only would make templates load-valid but
    install-invalid, so pin the two together.
    """
    from nymeria.core.workflow_templates import _BUDGET_KEYS

    assert set(_BUDGET_KEYS) == set(tool_create_module._WORKFLOW_BUDGET_KEYS)


def test_reinstall_of_disabled_template_is_not_a_false_conflict(wf_env):
    """A disabled install is still OUR install, not a foreign tool.

    The loader cache drops disabled definitions, so a cache-based identity check
    reported an admin's own disabled template as "already in use by another
    tool" and told them to delete it.
    """
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        install_workflow_template(
            user_id="u1", template_id="url_watcher", agent=None, is_admin=True
        )
        # The admin disables it (tools UI toggle / PUT /tools/custom / raw edit).
        path = wf_env.tmp / "custom_tools" / "url_watcher.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["enabled"] = False
        path.write_text(json.dumps(data), encoding="utf-8")
        wf_env.loader.load_all()
        assert wf_env.loader.get_definition("url_watcher") is None  # cache hides it

        existing, created = install_workflow_template(
            user_id="u1", template_id="url_watcher", agent=None, is_admin=True
        )
    assert created is False
    assert existing is not None and existing.id == "url_watcher"
    # Reported honestly rather than silently re-enabled behind the admin's back.
    assert existing.enabled is False


def test_install_refuses_a_divergent_copy_of_its_own_template(wf_env):
    """Identity is the shipped revision hash, not a self-asserted tag.

    The custom-tools dir is an agent-writable resource surface, so a definition
    can carry our template tag with foreign source. Returning "already
    installed" would leave an admin believing the vetted recipe is live while
    the divergent revision sits in the pending queue under the trusted name.
    """
    from nymeria.tools.definitions.custom_tool_schema import (
        CustomToolDefinition,
        WorkflowToolConfig,
    )

    shipped = get_template("url_watcher", BUNDLED_DIR)
    genuine_hash = None
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        installed, _ = install_workflow_template(
            user_id="u1", template_id="url_watcher", agent=None, is_admin=True
        )
        genuine_hash = installed.workflow_config.revision_hash

    # Foreign source, our tag, AND the shipped revision_hash copied in: the
    # stored hash is as self-asserted as the tag, so identity must be
    # RECOMPUTED from content the way workflow_execution_gate does.
    planted = CustomToolDefinition(
        id="url_watcher",
        name="URL change watcher",
        description="looks like the bundled recipe, is not",
        implementation_type="workflow",
        workflow_config=WorkflowToolConfig(
            source_code="def run(url: str):\n    return {'not': 'the shipped recipe'}\n",
            entrypoint="run",
            revision_hash=genuine_hash,
        ),
        tags=["bundled", "workflow", "template:url_watcher"],
    )
    wf_env.loader.save_definition(planted)
    assert shipped.workflow["source_code"] != planted.workflow_config.source_code
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        with pytest.raises(ValueError, match="DIVERGENT"):
            install_workflow_template(
                user_id="u1", template_id="url_watcher", agent=None, is_admin=True
            )


def test_reinstall_still_enables_the_tool_on_this_thread(wf_env, monkeypatch):
    """The docs promise install enables the recipe on this thread.

    The common case is an agent in a NEW thread installing a recipe that is
    already published globally; returning early on `not created` left it with no
    binding and no way to call the tool.
    """
    enabled: list[list[str]] = []
    monkeypatch.setattr(
        tool_create_module, "_enable", lambda names, *a, **k: enabled.append(names) or ""
    )
    with patch(
        "nymeria.core.agent.get_current_agent", return_value=_fake_admin_agent("admin")
    ):
        first = _run_tool(action="install_template", template_id="url_watcher")
        second = _run_tool(action="install_template", template_id="url_watcher")
    assert first["created"] is True
    assert second["created"] is False and second["already_installed"] is True
    assert enabled == [["url_watcher"], ["url_watcher"]]


def test_install_conflict_with_foreign_tool(wf_env):
    # A DIFFERENT tool occupying the template's id is a conflict, not a
    # misleading "already installed" (idempotency keys off our bundled tag).
    from nymeria.tools.definitions.custom_tool_schema import (
        CustomToolDefinition,
        HTTPToolConfig,
    )

    foreign = CustomToolDefinition(
        id="url_watcher",
        name="Foreign",
        description="a different tool that happens to share the id",
        implementation_type="http",
        http_config=HTTPToolConfig(url="https://example.com"),
        tags=["agent-created"],
    )
    wf_env.loader.save_definition(foreign)
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        with pytest.raises(ValueError, match="already in use"):
            install_workflow_template(
                user_id="u1", template_id="url_watcher", agent=None, is_admin=True
            )


# --- tool surface --------------------------------------------------------------


def test_tool_install_template_missing_id(wf_env):
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        payload = _run_tool(action="install_template")
    assert payload["ok"] is False
    assert payload["error"]["type"] == "validation_error"


def test_tool_install_template_admin_success(wf_env, monkeypatch):
    # Keep the enable step out of the way: an empty enable result leaves the
    # publish JSON as the pure return (see _prefix_command_result).
    monkeypatch.setattr(tool_create_module, "_enable", lambda *a, **k: "")
    with patch(
        "nymeria.core.agent.get_current_agent", return_value=_fake_admin_agent("admin")
    ):
        payload = _run_tool(action="install_template", template_id="two_thread_conversation")
    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["approval"] == "approved"
    assert "bundled" in payload["tool"]["tags"]
    assert wf_env.loader.get_definition("two_thread_conversation") is not None


def test_tool_install_template_non_admin_error(wf_env):
    with patch(
        "nymeria.core.agent.get_current_agent", return_value=_fake_admin_agent("user")
    ):
        payload = _run_tool(action="install_template", template_id="url_watcher")
    assert payload["ok"] is False
    assert "admin" in payload["error"]["message"].lower()


def test_workflow_info_templates_lists_catalog(wf_env):
    workflow_info_module = importlib.import_module("nymeria.tools.workflow_info")
    text = workflow_info_module._templates()
    assert "two_thread_conversation" in text
    assert "url_watcher" in text


# --- recipe runtime behavior (exec the bundled source against a fake nym) ------


class _FakeState:
    def __init__(self):
        self._d: dict[str, Any] = {}

    def get(self, key):
        return self._d.get(key)

    def set(self, key, value):
        self._d[key] = value

    def delete(self, key):
        self._d.pop(key, None)


def _fake_nym(*, fetch=None, thread=None):
    notified: list[str] = []
    nym = SimpleNamespace(
        tools=SimpleNamespace(fetch_url_nymeria=fetch or (lambda url: "content")),
        state=_FakeState(),
        notify=lambda message, **kw: notified.append(message),
        thread=thread or (lambda **kw: "reply"),
    )
    return nym, notified


def _recipe_namespace(template_id: str, nym) -> dict[str, Any]:
    """Exec the REAL shipped source, so these tests bind to what we publish.

    Returns the namespace rather than calling straight through, so a test can
    swap a module the recipe imported (e.g. a fake clock over ``time``) before
    invoking the entrypoint.
    """
    source = get_template(template_id, BUNDLED_DIR).workflow["source_code"]
    namespace: dict[str, Any] = {"nym": nym}
    exec(compile(source, f"<{template_id}>", "exec"), namespace)  # noqa: S102
    return namespace


def _run_recipe(template_id: str, nym, **params):
    return _recipe_namespace(template_id, nym)["run"](**params)


def test_url_watcher_error_return_is_treated_as_failure(wf_env):
    # fetch_url_nymeria returns "[Error]:" strings, it does not raise; the recipe
    # must detect that on the return value (the ship-blocker the review caught).
    nym, notified = _fake_nym(fetch=lambda url: "[Error]: HTTP 503")
    r1 = _run_recipe("url_watcher", nym, url="http://x", alert_after_failures=2)
    assert r1 == {"checked": False, "failures": 1}
    assert notified == []  # a single blip never pages
    r2 = _run_recipe("url_watcher", nym, url="http://x", alert_after_failures=2)
    assert r2["failures"] == 2
    assert len(notified) == 1  # alerts exactly at the streak threshold


def test_url_watcher_change_detection(wf_env):
    pages = iter(["hello", "hello", "world"])
    nym, notified = _fake_nym(fetch=lambda url: next(pages))
    assert _run_recipe("url_watcher", nym, url="http://x")["changed"] is False  # seed
    assert _run_recipe("url_watcher", nym, url="http://x")["changed"] is False  # same
    assert notified == []
    assert _run_recipe("url_watcher", nym, url="http://x")["changed"] is True  # changed
    assert len(notified) == 1


def test_two_thread_alternates_and_stops_on_marker(wf_env):
    seen: list[str] = []

    def fake_thread(prompt, id_or_title, mode="ask"):
        seen.append(id_or_title)
        return "wrap it up [END CONVERSATION]" if len(seen) == 3 else f"reply {len(seen)}"

    nym, notified = _fake_nym(thread=fake_thread)
    result = _run_recipe(
        "two_thread_conversation", nym,
        thread_a="A", thread_b="B", opening="hi", max_exchanges=10,
    )
    assert result["ended_by_marker"] is True
    assert result["replies"] == 3
    assert seen == ["A", "B", "A"]  # strict alternation
    assert len(notified) == 1  # transcript delivered


def test_url_watcher_isolates_state_per_url(wf_env):
    """One installed tool watches many URLs against ONE shared state document.

    Workflow state is namespaced by (workflow_id, user_id) only, and the recipe
    installs as a single global tool, so every TODO bound to it shares a state
    doc. Un-namespaced keys made two watched URLs overwrite each other's digest
    and report a change on every fire, forever.
    """
    pages = {"http://a.example": "AAA static", "http://b.example": "BBB static"}
    nym, notified = _fake_nym(fetch=lambda url: pages[url])
    for _ in range(3):  # interleaved fires, neither page ever changing
        for url in pages:
            assert _run_recipe("url_watcher", nym, url=url)["changed"] is False
    assert notified == []

    pages["http://b.example"] = "BBB CHANGED"
    assert _run_recipe("url_watcher", nym, url="http://b.example")["changed"] is True
    assert _run_recipe("url_watcher", nym, url="http://a.example")["changed"] is False
    assert len(notified) == 1


def test_url_watcher_isolates_failure_streaks_per_url(wf_env):
    """A shared streak let one URL's outage suppress another's alert."""

    def fetch(url):
        return "[Error]: down" if url == "http://down.example" else "fine"

    nym, notified = _fake_nym(fetch=fetch)
    for _ in range(2):
        _run_recipe("url_watcher", nym, url="http://down.example", alert_after_failures=2)
        _run_recipe("url_watcher", nym, url="http://up.example", alert_after_failures=2)
    # The healthy URL's success must not reset the failing URL's streak.
    assert len(notified) == 1
    assert "down.example" in notified[0]


def test_two_thread_call_caps_never_bite_before_the_exchange_clamp(wf_env):
    """Each exchange is one nym.thread (an AI call); the final notify is not."""
    budget = get_template("two_thread_conversation", BUNDLED_DIR).workflow["budget"]
    clamp = 20  # MAX_EXCHANGES in the recipe source
    assert budget["max_ai_calls"] > clamp
    assert budget["max_calls"] > clamp + 1


def test_two_thread_default_time_budget_fits_the_REAL_tool_path_cap(wf_env, monkeypatch):
    """The recipe's own clock must fit the surface install_template binds it to.

    Asks the engine what it actually enforces rather than trusting the declared
    budget: run_workflow_tool lowers EVERY tool-path run to tool_timeout - 2
    whatever the definition declares, and install_template enables the tool on
    the calling thread, so the tool path is the recipe's primary surface. The
    declared 3600s is honored only headless. Delivery happens after the loop and
    an overrun is SIGKILLed, so a default that outran this cap would lose the
    whole transcript on the recipe's own front door.
    """
    import asyncio

    from nymeria.config.settings import Settings
    from nymeria.core.workflows import tool_runtime

    captured: dict[str, Any] = {}

    async def fake_run_workflow_by_id(loader, tool_id, params, **kwargs):
        captured.update(kwargs)
        return "stop here", None  # a refusal short-circuits before executing

    monkeypatch.setattr(tool_runtime, "run_workflow_by_id", fake_run_workflow_by_id)

    def _cap_for(tool_timeout: float) -> float:
        # run_workflow_tool imports get_settings function-locally, so patch the
        # SOURCE module. Setting a `get_settings` attribute on tool_runtime is
        # silently inert (it has none) and the cap would fall back to the
        # hardcoded 300.0 in its except branch, leaving this test blind to the
        # very default it is named for.
        monkeypatch.setattr(
            "nymeria.config.get_settings",
            lambda: SimpleNamespace(tool_timeout=tool_timeout),
        )
        captured.clear()
        asyncio.run(
            tool_runtime.run_workflow_tool(
                None, "two_thread_conversation", {}, _tool_config()
            )
        )
        return captured["wall_clock_cap"]

    # A sentinel first: proves the cap really tracks tool_timeout and that the
    # patch above is live, so the assertions below cannot quietly degrade into
    # asserting the hardcoded fallback.
    assert _cap_for(123) == pytest.approx(121.0)

    tool_path_cap = _cap_for(Settings.model_fields["tool_timeout"].default)
    template = get_template("two_thread_conversation", BUNDLED_DIR)
    declared = template.workflow["budget"]["wall_clock_seconds"]
    assert tool_path_cap < declared  # the premise: the tool path is far smaller

    parameters = _validate_template(template)
    default_budget = parameters["time_budget_seconds"].default
    assert default_budget < tool_path_cap, (
        f"default time_budget_seconds={default_budget}s does not fit the tool "
        f"path's real {tool_path_cap}s wall clock (tool_timeout default "
        f"{Settings.model_fields['tool_timeout'].default}); the run would be "
        "SIGKILLed before nym.notify and the whole transcript would be lost"
    )


def test_two_thread_stops_on_its_own_clock_and_still_delivers(wf_env):
    """A run that cannot afford another reply must stop and deliver, not die.

    The script cannot poll the engine's remaining budget, so it keeps its own
    clock and stops while there is still room to deliver. Driven by a fake
    monotonic clock the sub-agent advances, so this pins the arithmetic with no
    sleeps and no timing flake.
    """

    class _FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = _FakeClock()

    def fake_thread(prompt, id_or_title, mode="ask"):
        clock.now += 60.0  # every sub-agent reply "takes" 60s
        return "keep going"  # never emits the end marker

    nym, notified = _fake_nym(thread=fake_thread)
    namespace = _recipe_namespace("two_thread_conversation", nym)
    namespace["time"] = clock
    result = namespace["run"](
        thread_a="A", thread_b="B", opening="hi",
        max_exchanges=20, time_budget_seconds=240,
    )

    # 4 replies spend the whole 240s; a 5th would land at 300s, so it stops.
    assert result["replies"] == 4
    assert result["ended_by_time"] is True
    assert result["ended_by_marker"] is False
    # The point of stopping early: the partial transcript IS delivered.
    assert len(notified) == 1
    assert "stopped by time budget" in notified[0]
    assert result["transcript"] == ["[A]: keep going", "[B]: keep going"] * 2


def test_two_thread_clamps_max_exchanges(wf_env):
    count = {"n": 0}

    def fake_thread(prompt, id_or_title, mode="ask"):
        count["n"] += 1
        return "keep going"  # never emits the end marker

    nym, _ = _fake_nym(thread=fake_thread)
    result = _run_recipe(
        "two_thread_conversation", nym,
        thread_a="A", thread_b="B", opening="hi", max_exchanges=999,
    )
    # Clamped by MAX_EXCHANGES, the coarse backstop. Instant fake replies never
    # touch the time budget, so this isolates the exchange clamp itself.
    assert result["replies"] == 20
    assert result["ended_by_time"] is False
    assert count["n"] == 20
