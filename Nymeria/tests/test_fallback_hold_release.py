"""#236: a fallback hold no longer shadows a user's model choice.

A thread held on its fallback model (``ThreadConfig.active_llm_fallback``)
resolves to the fallback over its own override. Before this pass the
in-process command client every slash command writes through
(``CommandBackendClient.update_thread_config``) never touched the hold, so
``/model X thread`` answered "set" while every later turn ran the fallback;
it also silently ignored ``clear_active_fallback``/``clear_llm_config`` and
the skills fields, and ``/fallback revert`` failed outright on the HTTP
command shape. The PATCH route, meanwhile, ended a hold on ANY llm_config
write, so a GUI Save of unrelated settings silently ended it.

One rule now governs every door (``release_fallback_for_config_write``):
an explicit revert or a user's route command ends the hold; an actual route
change ends it unless a non-user made it (the agent, any other command
actor, the workflow verb: F3, the hold is the outage safety net and only a
user ends it); effort and other non-route edits keep it. The end note names
the model the thread runs afterwards, with a "changed" reason for a model
change (F5), or "expired" when the hold it ends had already expired
unevicted.

Every command test runs on BOTH command client shapes: the real in-process
client, and ``CommandHttpClient`` over the real routes (ASGI). Settings and
the model list come from the suite's ``FakeCommandApi``; the thread-config
reads and writes go through the real door, so the asserts read persisted
config, not a stub. Edges skipped, by design: concurrency between a hold
activating and a command's read (the route-change diff still applies to the
write), and non-thread surfaces of the hold (``/status`` is #159's).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cli_fixtures import run
from nymeria.core.agent_llm_config import (
    clear_expired_llm_fallback_if_idle,
    get_llm_config_for_thread,
)
from nymeria.core.command_service import (
    CommandBackendClient,
    CommandContext,
    CommandHttpClient,
    CommandService,
    _CommandBackendUser,
)
from nymeria.api.schemas.thread_config import ThreadConfigUpdateRequest
from nymeria.core.command_service import _IN_PROCESS_THREAD_CONFIG_FIELDS
from nymeria.core.thread_config import ActiveLLMFallback, ThreadConfig, ThreadLLMConfig
from nymeria.core.time_utils import utc_now
from nymeria.vendor.react_agent import nodes as nodes_module
from test_api_thread_config_router import FakeAgent
from test_command_service import FakeCommandApi
from test_llm_config_resolution import _make_agent

THREAD = "thread-1"  # FakeCommandApi lists it, so /fallback's owner guard passes
HELD_PROVIDER = "anthropic"
HELD_MODEL = "claude-haiku-4-5-20251001"
GLOBAL_PROVIDER = "openai"
GLOBAL_MODEL = "gpt-test"  # FakeCommandApi.get_settings' global model
NEW_MODEL = "grok-4.3"
SHAPES = ["in_process", "http"]


def _hold(*, permanent: bool = False, source_model: str = GLOBAL_MODEL, expired: bool = False):
    now = utc_now()
    if permanent:
        expires_at = None
    else:
        expires_at = now + (timedelta(seconds=-5) if expired else timedelta(hours=1))
    return ActiveLLMFallback(
        provider=HELD_PROVIDER,
        model=HELD_MODEL,
        source_provider=GLOBAL_PROVIDER,
        source_model=source_model,
        hold_seconds=0 if permanent else 3600,
        activated_at=now,
        expires_at=expires_at,
        reason="provider_server_error",
        http_status=502,
    )


class _DoorApi(FakeCommandApi):
    """FakeCommandApi whose thread-config read and write go through a REAL
    command client door (in-process adapter or HTTP over the real routes).

    ``agent`` mirrors the real clients: the in-process one carries the agent
    (``_agent()`` resolves it off the client), the HTTP one has none, which
    is exactly what broke ``/fallback revert`` on that shape."""

    def __init__(self, door: Any, agent: Any) -> None:
        super().__init__()
        self.door = door
        self.agent = agent

    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        data = await super().get_settings(user_id=user_id)
        data["llm_fast_model"] = "openai:gpt-test-mini"
        data["llm_smart_model"] = "openai:gpt-test-smart"
        return data

    async def get_thread_config(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_thread_config", (thread_id,), {"user_id": user_id}))
        return await self.door.get_thread_config(thread_id, user_id=user_id)

    async def update_thread_config(
        self, thread_id: str, *, user_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(("update_thread_config", (thread_id,), {"user_id": user_id, **kwargs}))
        return await self.door.update_thread_config(thread_id, user_id=user_id, **kwargs)


class _Harness:
    """One data dir, one owner, both doors onto it."""

    def __init__(self, tmp_path: Path, api_client_builder: Any) -> None:
        self.agent = FakeAgent(tmp_path)
        # The global route the hold-release rule and the end note read; the
        # same values FakeCommandApi.get_settings reports to the commands.
        self.agent.settings = SimpleNamespace(
            llm_provider=GLOBAL_PROVIDER, llm_model=GLOBAL_MODEL
        )
        settings = api_client_builder.settings(tmp_path)
        self.client, self.token = api_client_builder.authenticated_client(
            self.agent, settings, user_id="owner"
        )
        self.agent.accounts_repo.claim_thread(THREAD, "owner")

    # -- state ------------------------------------------------------------

    def seed(self, **fields: Any) -> None:
        assert self.agent.thread_config_manager.save_config(
            ThreadConfig(thread_id=THREAD, **fields)
        )

    def saved(self) -> ThreadConfig:
        tc = self.agent.thread_config_manager.get_config(THREAD)
        assert tc is not None
        return tc

    def resolved_model(self) -> tuple[str, str]:
        """What the next turn actually runs: the real resolver over the
        persisted config (the observable effect #236 was filed about)."""
        host = _make_agent(llm_provider=GLOBAL_PROVIDER, llm_model=GLOBAL_MODEL)
        host.thread_config_manager = self.agent.thread_config_manager
        host.invalidate_thread_config_cache = lambda thread_id: None
        config = get_llm_config_for_thread(host, THREAD)
        return config.provider, config.model

    # -- doors ------------------------------------------------------------

    async def _with_door(self, shape: str, body):
        if shape == "in_process":
            door = CommandBackendClient(
                self.agent, user=_CommandBackendUser(id="owner", role="user")
            )
            return await body(door)
        http = CommandHttpClient("http://api.test", self.token, use_act_as=False)
        http._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.client.app), base_url="http://api.test"
        )
        try:
            return await body(http)
        finally:
            await http.aclose()

    def command(
        self,
        shape: str,
        command: str,
        *,
        actor: str = "user",
        surface: str = "cli",
        is_admin: bool = False,
    ):
        async def body(door):
            api = _DoorApi(door, self.agent if shape == "in_process" else None)
            ctx = CommandContext(
                user_id="owner",
                thread_id=THREAD,
                actor=actor,  # type: ignore[arg-type]
                surface=surface,  # type: ignore[arg-type]
                is_admin=is_admin,
            )
            return await CommandService().execute(ctx, command, api=api), api

        return run(self._with_door(shape, body))

    def write(self, shape: str, **kwargs: Any) -> dict[str, Any]:
        async def body(door):
            return await door.update_thread_config(THREAD, user_id="owner", **kwargs)

        return run(self._with_door(shape, body))


@pytest.fixture
def harness(tmp_path: Path, api_client_builder, monkeypatch):
    # No process-global agent either: the HTTP command shape has none.
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: None)
    return _Harness(tmp_path, api_client_builder)


def _end_note(tc: ThreadConfig) -> dict[str, Any]:
    note = tc.pending_fallback_note
    assert note is not None, "no end note latched"
    assert note["phase"] == "end"
    return note


# -- a user's model commands end the hold, on both shapes -------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_model_thread_ends_the_hold_and_the_next_turn_runs_the_new_model(harness, shape):
    harness.seed(active_llm_fallback=_hold())
    assert harness.resolved_model() == (HELD_PROVIDER, HELD_MODEL)  # precondition

    result, _api = harness.command(shape, f"/model {NEW_MODEL} thread")

    assert result.success is True
    assert result.markdown.strip().endswith(
        f"Model for this thread set to {NEW_MODEL}. Ended the fallback hold "
        f"(was on {HELD_PROVIDER}/{HELD_MODEL})."
    )
    saved = harness.saved()
    assert saved.active_llm_fallback is None
    assert saved.llm_config is not None and saved.llm_config.model == NEW_MODEL
    note = _end_note(saved)
    assert note["reason"] == "changed"
    assert (note["from_model"], note["to_model"]) == (HELD_MODEL, NEW_MODEL)
    assert NEW_MODEL in note["text"] and "was changed" in note["text"]
    assert harness.resolved_model() == (GLOBAL_PROVIDER, NEW_MODEL)


def test_both_shapes_leave_the_same_state_and_reply(tmp_path, api_client_builder):
    """Parity, compared shape against shape rather than against literals, so
    a drift in either door fails here even if the two drift together."""
    outcomes = []
    for shape in SHAPES:
        h = _Harness(tmp_path / shape, api_client_builder)
        h.seed(active_llm_fallback=_hold())
        result, _api = h.command(shape, f"/model {NEW_MODEL} thread")
        saved = h.saved()
        outcomes.append(
            (
                result.success,
                result.markdown,
                saved.active_llm_fallback,
                saved.llm_config.model if saved.llm_config else None,
                saved.pending_fallback_note,
            )
        )
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][2] is None and outcomes[0][4] is not None


@pytest.mark.parametrize("shape", SHAPES)
def test_reasserting_the_configured_model_still_ends_the_hold(harness, shape):
    """The natural "get me back" move: the route does not change, so only
    the command's explicit revert can end the hold."""
    harness.seed(
        llm_config=ThreadLLMConfig(model=NEW_MODEL), active_llm_fallback=_hold()
    )

    result, api = harness.command(shape, f"/model {NEW_MODEL} thread")

    assert result.success is True
    saved = harness.saved()
    assert saved.active_llm_fallback is None
    note = _end_note(saved)
    assert note["reason"] == "reverted"
    assert note["to_model"] == NEW_MODEL
    assert harness.resolved_model() == (GLOBAL_PROVIDER, NEW_MODEL)


@pytest.mark.parametrize("shape", SHAPES)
def test_a_permanent_hold_ends_on_a_model_command(harness, shape):
    harness.seed(active_llm_fallback=_hold(permanent=True))

    result, _api = harness.command(shape, f"/model {NEW_MODEL} thread")

    assert result.success is True and "Ended the fallback hold" in result.markdown
    assert harness.saved().active_llm_fallback is None
    assert harness.resolved_model() == (GLOBAL_PROVIDER, NEW_MODEL)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/provider switch anthropic thread", ("anthropic", GLOBAL_MODEL)),
        ("/fast on", (GLOBAL_PROVIDER, "gpt-test-mini")),
        ("/smart on", (GLOBAL_PROVIDER, "gpt-test-smart")),
    ],
)
def test_provider_switch_and_fast_end_the_hold(harness, shape, command, expected):
    harness.seed(active_llm_fallback=_hold())

    result, _api = harness.command(shape, command)

    assert result.success is True, result.markdown
    assert f"Ended the fallback hold (was on {HELD_PROVIDER}/{HELD_MODEL})." in result.markdown
    saved = harness.saved()
    assert saved.active_llm_fallback is None
    assert _end_note(saved)["to_model"] == expected[1]
    assert harness.resolved_model() == expected


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize(
    ("command", "configured_model"),
    [
        ("/fast on", "gpt-test-mini"),
        (f"/provider switch {GLOBAL_PROVIDER} thread", GLOBAL_MODEL),
    ],
)
def test_a_reasserted_route_command_still_ends_the_hold(
    harness, shape, command, configured_model
):
    """/fast on while already on the fast model, or switching to the
    provider the thread already runs, writes the same route: only the
    command's explicit revert ends the hold."""
    harness.seed(
        llm_config=ThreadLLMConfig(provider=GLOBAL_PROVIDER, model=configured_model),
        active_llm_fallback=_hold(),
    )

    result, _api = harness.command(shape, command)

    assert result.success is True, result.markdown
    assert harness.saved().active_llm_fallback is None
    note = _end_note(harness.saved())
    assert (note["reason"], note["to_model"]) == ("reverted", configured_model)


@pytest.mark.parametrize("shape", SHAPES)
def test_without_a_hold_the_model_command_is_unchanged(harness, shape):
    harness.seed(instructions="keep me")

    result, api = harness.command(shape, f"/model {NEW_MODEL} thread")

    assert result.success is True
    assert result.markdown.strip().endswith(f"Model for this thread set to {NEW_MODEL}.")
    assert "fallback" not in result.markdown.lower()
    writes = [kw for name, _a, kw in api.calls if name == "update_thread_config"]
    assert writes == [{"user_id": "owner", "llm_config": {"model": NEW_MODEL}}]
    assert harness.saved().pending_fallback_note is None


# -- what keeps the hold -----------------------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_think_keeps_the_hold_and_says_so(harness, shape):
    harness.seed(active_llm_fallback=_hold())

    result, _api = harness.command(shape, "/think high thread")

    assert result.success is True
    assert (
        f"The fallback hold stays active: {HELD_PROVIDER}/{HELD_MODEL} until"
        in result.markdown
    )
    assert "(revert: /fallback revert)" in result.markdown
    saved = harness.saved()
    assert saved.active_llm_fallback is not None
    assert saved.llm_config is not None and saved.llm_config.reasoning_effort == "high"
    assert saved.pending_fallback_note is None
    assert harness.resolved_model() == (HELD_PROVIDER, HELD_MODEL)


@pytest.mark.parametrize("shape", SHAPES)
def test_the_agents_model_change_saves_but_keeps_the_hold(harness, shape):
    """F3: the agent is barred from /fallback revert and the hold is the
    outage safety net, so its model write lands in config and the hold stays
    until the user reverts it; the reply says so and whose revert it is."""
    harness.seed(active_llm_fallback=_hold())

    result, _api = harness.command(
        shape, f"/model {NEW_MODEL} thread", actor="agent", surface="agent"
    )

    assert result.success is True, result.markdown
    assert "The fallback hold stays active:" in result.markdown
    assert "(user can revert with /fallback revert)" in result.markdown
    saved = harness.saved()
    assert saved.llm_config is not None and saved.llm_config.model == NEW_MODEL
    assert saved.active_llm_fallback is not None
    assert saved.pending_fallback_note is None
    assert harness.resolved_model() == (HELD_PROVIDER, HELD_MODEL)

    # When the hold later ends, the note names the agent's model, the one
    # the thread is configured to run, not the hold's original source.
    harness.command(shape, "/fallback revert")
    assert _end_note(harness.saved())["to_model"] == NEW_MODEL


@pytest.mark.parametrize("shape", SHAPES)
def test_the_agents_model_change_without_a_hold_writes_as_before(harness, shape):
    result, api = harness.command(
        shape, f"/model {NEW_MODEL} thread", actor="agent", surface="agent"
    )

    assert result.success is True
    assert "fallback" not in result.markdown.lower()
    writes = [kw for name, _a, kw in api.calls if name == "update_thread_config"]
    assert writes == [{"user_id": "owner", "llm_config": {"model": NEW_MODEL}}]
    assert harness.saved().llm_config.model == NEW_MODEL  # type: ignore[union-attr]


@pytest.mark.parametrize("shape", SHAPES)
def test_an_effort_edit_keeps_the_hold_but_a_route_edit_ends_it(harness, shape):
    harness.seed(
        llm_config=ThreadLLMConfig(model=GLOBAL_MODEL), active_llm_fallback=_hold()
    )

    # Resubmitting the unchanged route plus an effort change (the GUI Save
    # shape): no route change, the hold stays.
    harness.write(shape, llm_config={"model": GLOBAL_MODEL, "reasoning_effort": "low"})
    assert harness.saved().active_llm_fallback is not None
    assert harness.saved().pending_fallback_note is None

    harness.write(shape, llm_config={"model": NEW_MODEL})
    saved = harness.saved()
    assert saved.active_llm_fallback is None
    note = _end_note(saved)
    assert (note["reason"], note["to_model"]) == ("changed", NEW_MODEL)


@pytest.mark.parametrize("shape", SHAPES)
def test_keep_holds_through_a_route_change_and_revert_beats_keep(harness, shape):
    harness.seed(active_llm_fallback=_hold())

    harness.write(shape, llm_config={"model": NEW_MODEL}, keep_active_fallback=True)
    assert harness.saved().active_llm_fallback is not None
    assert harness.saved().llm_config.model == NEW_MODEL  # type: ignore[union-attr]

    harness.write(shape, clear_active_fallback=True, keep_active_fallback=True)
    assert harness.saved().active_llm_fallback is None
    assert _end_note(harness.saved())["reason"] == "reverted"


@pytest.mark.parametrize("shape", SHAPES)
def test_clear_llm_config_ends_the_hold_only_when_it_moves_the_route(harness, shape):
    # An override equal to the global: clearing it changes nothing that runs.
    harness.seed(
        llm_config=ThreadLLMConfig(model=GLOBAL_MODEL), active_llm_fallback=_hold()
    )
    harness.write(shape, clear_llm_config=True)
    saved = harness.saved()
    assert saved.llm_config is None
    assert saved.active_llm_fallback is not None

    # An override that differs: clearing it moves the thread to the global,
    # and the note names the global model.
    harness.seed(
        llm_config=ThreadLLMConfig(model=NEW_MODEL), active_llm_fallback=_hold()
    )
    harness.write(shape, clear_llm_config=True)
    saved = harness.saved()
    assert saved.llm_config is None and saved.active_llm_fallback is None
    note = _end_note(saved)
    assert (note["reason"], note["to_model"]) == ("changed", GLOBAL_MODEL)


@pytest.mark.parametrize("shape", SHAPES)
def test_an_explicit_clear_without_a_hold_leaves_an_unconsumed_note(harness, shape):
    earlier = nodes_module.fallback_note_stamp(
        {"from_model": "x", "to_model": "y", "reason": "expired"},
        kind="transport",
        phase="end",
    )
    harness.seed(pending_fallback_note=earlier)

    harness.write(shape, clear_active_fallback=True)
    harness.write(shape, llm_config={"model": NEW_MODEL})

    assert harness.saved().pending_fallback_note == earlier


def test_patch_gui_save_of_unrelated_fields_keeps_the_hold(harness):
    """The desktop Save always resends llm_config (or clear_llm_config):
    saving instructions alone used to end the hold and send the next turn
    back at a possibly still-down primary."""
    harness.seed(
        llm_config=ThreadLLMConfig(provider=GLOBAL_PROVIDER, model=NEW_MODEL),
        active_llm_fallback=_hold(),
    )
    headers = {"Authorization": f"Bearer {harness.token}"}

    resp = harness.client.patch(
        f"/threads/{THREAD}/config",
        headers=headers,
        json={
            "instructions": "Be brief.",
            "llm_config": {
                "provider": GLOBAL_PROVIDER,
                "model": NEW_MODEL,
                "temperature": None,
                "reasoning_effort": None,
            },
        },
    )
    assert resp.status_code == 200
    saved = harness.saved()
    assert saved.instructions == "Be brief."
    assert saved.active_llm_fallback is not None
    assert resp.json()["active_llm_fallback"] is not None

    # A Save on a thread with no override sends clear_llm_config: also kept.
    harness.seed(active_llm_fallback=_hold())
    resp = harness.client.patch(
        f"/threads/{THREAD}/config",
        headers=headers,
        json={"instructions": "Be brief.", "clear_llm_config": True},
    )
    assert resp.status_code == 200
    assert harness.saved().active_llm_fallback is not None


# -- /fallback revert works on both shapes -----------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_fallback_revert_works_on_both_shapes(harness, shape):
    harness.seed(
        llm_config=ThreadLLMConfig(model=NEW_MODEL), active_llm_fallback=_hold()
    )

    result, _api = harness.command(shape, "/fallback revert")

    assert result.success is True, result.markdown
    assert result.markdown.strip().endswith(
        "Fallback hold cleared; this thread returns to its configured model "
        f"(was on {HELD_PROVIDER}/{HELD_MODEL})."
    )
    saved = harness.saved()
    assert saved.active_llm_fallback is None
    note = _end_note(saved)
    assert (note["reason"], note["to_model"]) == ("reverted", NEW_MODEL)

    again, _api = harness.command(shape, "/fallback revert")
    assert again.markdown.strip().endswith("This thread has no active fallback hold.")
    assert harness.saved().pending_fallback_note == note


# -- the in-process client applies every field it is sent -------------------------


def test_thread_skills_enable_and_disable_persist(harness):
    """In-process only: the skills commands need the agent, which the HTTP
    command shape does not have; the PATCH route always applied these."""
    harness.seed(instructions="x")

    enabled, _api = harness.command("in_process", "/skills enable research")
    assert enabled.success is True, enabled.markdown
    assert harness.saved().enabled_skills == ["research"]

    disabled, _api = harness.command("in_process", "/skills disable research")
    assert disabled.success is True, disabled.markdown
    saved = harness.saved()
    assert saved.enabled_skills == []
    assert saved.disabled_skills == ["research"]


def test_the_in_process_client_refuses_a_field_it_cannot_apply(harness):
    """A field the door cannot apply raises instead of answering success:
    the silent drop is the bug class behind #236."""
    harness.seed(instructions="before")
    door = CommandBackendClient(
        harness.agent, user=_CommandBackendUser(id="owner", role="user")
    )
    with pytest.raises(TypeError, match="instructions"):
        run(door.update_thread_config(THREAD, user_id="owner", instructions="after"))
    assert harness.saved().instructions == "before"


# -- the read: bare /model names the hold ------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_bare_model_shows_an_active_hold(harness, shape):
    harness.seed(
        llm_config=ThreadLLMConfig(model=NEW_MODEL), active_llm_fallback=_hold()
    )

    result, _api = harness.command(shape, "/model")

    lines = result.markdown.splitlines()
    assert f"this thread: {NEW_MODEL} (override)" in lines
    hold_line = next(line for line in lines if line.startswith("fallback hold (active):"))
    assert hold_line.startswith(
        f"fallback hold (active): {HELD_PROVIDER}/{HELD_MODEL} "
        f"(from {GLOBAL_MODEL}; reason: provider_server_error) until"
    )
    assert hold_line.endswith("(revert: /fallback revert)")
    assert "set with: /model <name> [global|thread] (a thread model ends the hold)" in lines


@pytest.mark.parametrize(
    ("hold", "label"), [(None, "no hold"), (_hold(expired=True), "expired, not evicted")]
)
def test_bare_model_without_a_live_hold_is_unchanged(harness, hold, label):
    harness.seed(active_llm_fallback=hold, instructions="x")

    result, _api = harness.command("in_process", "/model")

    # The pre-#236 rendering, byte for byte (the outcome renderer makes the
    # first body line the heading).
    assert result.markdown == (
        f"### global: {GLOBAL_MODEL} ({GLOBAL_PROVIDER})\n\n"
        "this thread: using global default\n"
        "set with: /model <name> [global|thread]"
    ), label


def test_bare_model_on_a_formless_surface_carries_the_hold_line(harness):
    harness.seed(active_llm_fallback=_hold(permanent=True))

    result, _api = harness.command("http", "/model", surface="telegram")

    assert (
        f"fallback hold (active): {HELD_PROVIDER}/{HELD_MODEL} "
        f"(from {GLOBAL_MODEL}; reason: provider_server_error) permanent "
        "(revert: /fallback revert)"
    ) in result.markdown


def test_bare_model_for_the_agent_names_the_users_revert(harness):
    harness.seed(active_llm_fallback=_hold())

    result, _api = harness.command("in_process", "/model", actor="agent", surface="agent")

    assert "(user can revert with /fallback revert)" in result.markdown
    assert "a thread model ends the hold" not in result.markdown


@pytest.mark.parametrize("shape", SHAPES)
def test_global_model_in_a_held_thread_keeps_the_hold_and_says_so(harness, shape):
    harness.seed(active_llm_fallback=_hold())

    result, api = harness.command(shape, f"/model {NEW_MODEL}", is_admin=True)

    assert result.success is True
    assert ("update_settings", (), {"user_id": "owner", "llm_model": NEW_MODEL}) in api.calls
    assert (
        f"Global model set to {NEW_MODEL}. This thread stays on its fallback hold: "
        f"{HELD_PROVIDER}/{HELD_MODEL} until"
    ) in result.markdown
    assert harness.saved().active_llm_fallback is not None


def test_global_model_refusal_for_a_non_admin_is_unchanged(harness):
    harness.seed(active_llm_fallback=_hold())

    result, api = harness.command("in_process", f"/model {NEW_MODEL}", is_admin=False)

    assert result.success is False
    assert result.markdown.strip().endswith(
        "Setting the global default model is admin only. To change the model "
        f"for this conversation instead, use `/model {NEW_MODEL} thread`."
    )
    assert not [c for c in api.calls if c[0] in ("update_settings", "update_thread_config")]


# -- the other writers, and the note itself -----------------------------------------


def test_expiry_after_a_hold_on_hold_names_the_configured_model():
    """A hold activated while already held records the INTERMEDIATE fallback
    as its source; the end note must still name what the thread runs next."""
    from test_fallback_consent import _StatefulManager

    host = _make_agent(llm_provider=GLOBAL_PROVIDER, llm_model=GLOBAL_MODEL)
    host.thread_config_manager = _StatefulManager(
        ThreadConfig(
            thread_id="t1",
            active_llm_fallback=_hold(source_model="intermediate-fallback", expired=True),
        )
    )
    host.invalidate_thread_config_cache = lambda thread_id: None

    assert clear_expired_llm_fallback_if_idle(host, "t1") is True

    note = host.thread_config_manager.config.pending_fallback_note
    assert (note["reason"], note["to_model"]) == ("expired", GLOBAL_MODEL)
    assert f"back on its primary model ({GLOBAL_MODEL})" in note["text"]
    assert "intermediate-fallback" not in note["text"]


def test_a_changed_end_note_names_the_new_model_to_the_model_and_in_history():
    from nymeria.core.agent_history import format_conversation_history

    stamp = nodes_module.fallback_note_stamp(
        {"from_model": HELD_MODEL, "to_model": NEW_MODEL, "reason": "changed"},
        kind="transport",
        phase="end",
    )
    assert stamp["text"] == (
        "[System info]: The fallback hold on this thread ended because the "
        f"thread's model was changed; the thread is now on {NEW_MODEL}. "
        f"{HELD_MODEL} handled the conversation since the switch."
    )
    human = HumanMessage(
        content=f"next\n\n{stamp['text']}",
        additional_kwargs={"fallback_note": stamp},
        id="h1",
    )
    history = format_conversation_history(
        [human, AIMessage(content="ok", id="a1")], thread_id="t1"
    )
    notice = next(entry for entry in history if entry.get("kind") == "fallback_notice")
    assert notice["content"] == (
        f"Fallback hold ended (model changed); this thread is now on {NEW_MODEL}."
    )
    assert history[0]["content"] == "next"


# -- every route field counts on its own (review S2) -------------------------------

# One write per field, nothing else in the llm_config, no flag: the PLAIN rule,
# so only _route_identity decides. A user's command always sends the explicit
# revert, which ends the hold whatever the diff says, so the command tests
# above cannot tell a dropped field from a kept one.
ROUTE_FIELDS = [
    ("provider", "anthropic"),
    ("base_url", "https://gateway.example/v1"),
    ("api_key", "sk-test-other-route"),
    ("provider_route", "openai_compat"),
    ("openai_api_mode", "responses"),
]


def _route_seed(**extra: Any) -> ThreadLLMConfig:
    return ThreadLLMConfig(provider=GLOBAL_PROVIDER, model=GLOBAL_MODEL).model_copy(
        update=extra
    )


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize(("field", "value"), ROUTE_FIELDS)
def test_each_route_field_alone_ends_the_hold(harness, shape, field, value):
    harness.seed(llm_config=_route_seed(), active_llm_fallback=_hold())

    harness.write(shape, llm_config={field: value})

    saved = harness.saved()
    assert getattr(saved.llm_config, field) == value
    assert saved.llm_config.model == GLOBAL_MODEL  # type: ignore[union-attr]
    assert saved.active_llm_fallback is None
    note = _end_note(saved)
    assert note["reason"] == "changed"


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize(("field", "value"), ROUTE_FIELDS)
def test_resending_a_route_field_unchanged_keeps_the_hold(harness, shape, field, value):
    """The GUI Save shape for every field: the stored value, sent again, is
    no route change, so the hold and the note slot stay as they were."""
    harness.seed(llm_config=_route_seed(**{field: value}), active_llm_fallback=_hold())

    harness.write(shape, llm_config={field: value})

    saved = harness.saved()
    assert saved.active_llm_fallback is not None
    assert saved.pending_fallback_note is None


@pytest.mark.parametrize("shape", SHAPES)
def test_base_url_empty_and_none_are_different_routes(harness, shape):
    """``""`` is the explicit direct API, ``None`` inherits the global base
    URL: moving between them changes where the thread sends requests."""
    harness.seed(llm_config=_route_seed(base_url=None), active_llm_fallback=_hold())
    harness.write(shape, llm_config={"base_url": ""})
    saved = harness.saved()
    assert saved.llm_config.base_url == ""  # type: ignore[union-attr]
    assert saved.active_llm_fallback is None
    assert _end_note(saved)["reason"] == "changed"

    harness.seed(llm_config=_route_seed(base_url=""), active_llm_fallback=_hold())
    harness.write(shape, llm_config={"base_url": None})
    saved = harness.saved()
    assert saved.llm_config.base_url is None  # type: ignore[union-attr]
    assert saved.active_llm_fallback is None
    assert _end_note(saved)["reason"] == "changed"

    harness.seed(llm_config=_route_seed(base_url=""), active_llm_fallback=_hold())
    harness.write(shape, llm_config={"base_url": ""})
    assert harness.saved().active_llm_fallback is not None


# -- only an explicit user ends a hold (review S3) ---------------------------------


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("actor", ["agent", "system"])
@pytest.mark.parametrize(
    ("command", "configured"),
    [
        (f"/model {NEW_MODEL} thread", (GLOBAL_PROVIDER, NEW_MODEL)),
        ("/provider switch anthropic thread", ("anthropic", GLOBAL_MODEL)),
        ("/fast on", (GLOBAL_PROVIDER, "gpt-test-mini")),
        ("/smart on", (GLOBAL_PROVIDER, "gpt-test-smart")),
    ],
)
def test_a_non_user_actors_route_command_saves_but_keeps_the_hold(
    harness, shape, actor, command, configured
):
    """F3 generalised: the agent and the declared (unused) "system" actor
    are not an explicit user, so their route change saves while the hold
    stays and still wins resolution; the reply says so."""
    harness.seed(active_llm_fallback=_hold())

    result, api = harness.command(
        shape, command, actor=actor, surface="agent" if actor == "agent" else "cli"
    )

    assert result.success is True, result.markdown
    assert "The fallback hold stays active:" in result.markdown
    writes = [kw for name, _a, kw in api.calls if name == "update_thread_config"]
    assert writes and all(w.get("keep_active_fallback") is True for w in writes)
    saved = harness.saved()
    assert saved.llm_config is not None
    assert (saved.llm_config.provider or GLOBAL_PROVIDER, saved.llm_config.model or GLOBAL_MODEL) == configured
    assert saved.active_llm_fallback is not None
    assert saved.pending_fallback_note is None
    assert harness.resolved_model() == (HELD_PROVIDER, HELD_MODEL)


# -- an expired, unevicted hold ends as "expired" (review S4) ----------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_a_route_change_on_an_expired_hold_records_expired(harness, shape):
    """Eviction is lazy (idle threads, at LLM build), so a long idle stretch
    can leave an expired record for a route change to meet; the note must
    say it expired, not that the model change ended it."""
    harness.seed(active_llm_fallback=_hold(expired=True))

    harness.write(shape, llm_config={"model": NEW_MODEL})

    saved = harness.saved()
    assert saved.active_llm_fallback is None
    note = _end_note(saved)
    assert (note["reason"], note["to_model"]) == ("expired", NEW_MODEL)
    assert "was changed" not in note["text"]


@pytest.mark.parametrize("shape", SHAPES)
def test_model_command_on_an_expired_hold_records_expired_and_claims_nothing(
    harness, shape
):
    harness.seed(active_llm_fallback=_hold(expired=True))

    result, _api = harness.command(shape, f"/model {NEW_MODEL} thread")

    assert result.success is True
    assert result.markdown.strip().endswith(f"Model for this thread set to {NEW_MODEL}.")
    note = _end_note(harness.saved())
    assert (note["reason"], note["to_model"]) == ("expired", NEW_MODEL)


@pytest.mark.parametrize("shape", SHAPES)
def test_an_explicit_revert_of_an_expired_hold_records_expired(harness, shape):
    harness.seed(llm_config=ThreadLLMConfig(model=NEW_MODEL), active_llm_fallback=_hold(expired=True))

    harness.write(shape, clear_active_fallback=True)

    note = _end_note(harness.saved())
    assert (note["reason"], note["to_model"]) == ("expired", NEW_MODEL)


# -- the in-process whitelist cannot drift from the PATCH schema -------------------


def test_the_in_process_field_whitelist_is_a_subset_of_the_patch_schema():
    """Every field the in-process door applies must be a real PATCH field:
    a typo here, or a PATCH field renamed later, would make the in-process
    shape raise while the HTTP shape silently ignores the kwarg (pydantic's
    default), which is the asymmetry #236 removed."""
    unknown = _IN_PROCESS_THREAD_CONFIG_FIELDS - set(ThreadConfigUpdateRequest.model_fields)
    assert unknown == set()



# -- /fallback status carries the primary-reclaim line (#439, D7) -------------------


def _utc_minute(value: Any) -> str:
    from datetime import timezone

    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC"


@pytest.mark.parametrize("shape", SHAPES)
def test_fallback_status_names_the_next_reclaim_check(harness, shape):
    hold = _hold()
    harness.seed(active_llm_fallback=hold)

    result, _api = harness.command(shape, "/fallback status")

    lines = result.markdown.splitlines()
    first_check = hold.activated_at + timedelta(seconds=600)
    assert (
        f"Reclaim: next check at the first turn after {_utc_minute(first_check)}."
        in lines
    )


@pytest.mark.parametrize("shape", SHAPES)
def test_fallback_status_shows_an_offered_reclaim(harness, shape):
    hold = _hold(permanent=True)
    offered_at = utc_now() - timedelta(minutes=3)
    hold.reclaim_offered_at = offered_at
    harness.seed(active_llm_fallback=hold)

    result, _api = harness.command(shape, "/fallback status")

    assert (
        f"Reclaim: the primary answered again at {_utc_minute(offered_at)}; the "
        "hold stays (revert: /fallback revert)."
    ) in result.markdown.splitlines()


def test_fallback_status_reclaim_line_for_the_agent_names_the_users_revert(harness):
    hold = _hold()
    hold.reclaim_offered_at = utc_now()
    harness.seed(active_llm_fallback=hold)

    result, _api = harness.command(
        "in_process", "/fallback status", actor="agent", surface="agent"
    )

    assert "the hold stays (the user can revert with /fallback revert)." in result.markdown


def test_fallback_status_says_a_refusal_hold_is_not_checked(harness):
    hold = _hold()
    hold.reason = "refusal"
    harness.seed(active_llm_fallback=hold)

    result, _api = harness.command("http", "/fallback status")

    assert "Reclaim: not checked for a refusal hold." in result.markdown.splitlines()


def test_fallback_status_without_a_hold_has_no_reclaim_line(harness):
    harness.seed(instructions="x")

    result, _api = harness.command("in_process", "/fallback status")

    assert "This thread: no fallback hold active." in result.markdown
    assert "Reclaim" not in result.markdown


def test_the_reclaim_line_renders_the_last_verdict_from_the_payload():
    from test_command_service import FakeCommandApi, _status_ctx

    api = FakeCommandApi()
    expires = (utc_now() + timedelta(hours=1)).isoformat()
    api.thread_config = {
        "active_llm_fallback": {
            "provider": HELD_PROVIDER,
            "model": HELD_MODEL,
            "source_model": GLOBAL_MODEL,
            "reason": "rate_limited",
            "expires_at": expires,
        },
        "fallback_reclaim": {
            "state": "scheduled",
            "next_check_at": "2026-10-01T15:00:00+00:00",
            "last_verdict": "rate_limited",
            "last_checked_at": "2026-10-01T14:00:00+00:00",
        },
    }

    result = run(CommandService().execute(_status_ctx(), "/fallback status", api=api))

    assert (
        "Reclaim: next check at the first turn after 2026-10-01 15:00 UTC "
        "(last: rate_limited at 2026-10-01 14:00 UTC)."
    ) in result.markdown.splitlines()
