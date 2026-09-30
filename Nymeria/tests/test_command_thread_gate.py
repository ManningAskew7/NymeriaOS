"""#425/#426: a client-chosen thread id is checked where it enters the command
layer, and the other doors that took a thread id on trust now check it too.

``POST /commands/execute`` and ``GET /commands/options/{ref}`` let the client
choose ``thread_id``. The command layer used to rely on its client doors, and
store-direct handlers (``/skills disable all``, ``/browser switch``, ...) and
COMMAND_SUBMIT hooks never passed one, so another user's thread could be
reconfigured or read. Each refusal below is paired with an admitted caller
succeeding through the same harness.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_fixtures import run
from nymeria.core import command_hooks
from nymeria.core import command_service as command_service_module
from nymeria.core.command_service import (
    CommandBackendClient,
    CommandContext,
    CommandService,
    _CommandBackendUser,
)

ALICE_THREAD = "alice-thread"


@pytest.fixture
def app(tmp_path, api_client_builder, monkeypatch):
    """The real API app over a real accounts store: alice (owner of
    ``alice-thread``), bob (another non-admin) and an admin, plus spies on
    the two things a gated command must never reach: the COMMAND_SUBMIT hook
    seam and the handler executor."""
    from tests.test_api_thread_config_router import FakeAgent

    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, alice = api_client_builder.authenticated_client(agent, settings, user_id="alice")
    repo = agent.accounts_repo
    repo.create_user("bob", "bob@example.com", "Bob", role="user")
    repo.create_user("root", "root@example.com", "Root", role="admin")
    repo.claim_thread(ALICE_THREAD, "alice")

    reached: list[tuple[str, str | None]] = []
    real_fire = command_hooks.fire_command_submit
    real_executor = command_service_module._CommandExecutor

    async def spy_fire(ctx, definition, parsed):
        reached.append(("hook", ctx.thread_id))
        return await real_fire(ctx, definition, parsed)

    def spy_executor(*args, **kwargs):
        reached.append(("handler", kwargs.get("thread_id")))
        return real_executor(*args, **kwargs)

    monkeypatch.setattr(command_hooks, "fire_command_submit", spy_fire)
    monkeypatch.setattr(command_service_module, "_CommandExecutor", spy_executor)
    return SimpleNamespace(
        client=client,
        agent=agent,
        tokens={
            "alice": alice,
            "bob": repo.issue_token("bob"),
            "root": repo.issue_token("root"),
        },
        reached=reached,
        auth=api_client_builder.auth,
    )


def _execute(app, who: str, command: str, thread_id: str, **headers: str) -> dict:
    response = app.client.post(
        "/commands/execute",
        headers=app.auth(app.tokens[who], **headers),
        json={"command": command, "thread_id": thread_id, "surface": "cli"},
    )
    assert response.status_code == 200, response.text
    return response.json()


GATED_COMMANDS = [
    # The store-direct handlers the #425 sweep found, then a door-backed one
    # and a no-thread one: the gate is per entry, not per handler.
    "/skills list",
    "/skills disable all",
    "/browser",
    "/browser list",
    "/browser switch clear",
    "/provider reasoning_passback",
    "/notepad read",
    "/status",
    "/help",
]


@pytest.mark.parametrize("command", GATED_COMMANDS)
def test_another_users_thread_runs_no_handler_and_no_hook(app, command):
    refused = _execute(app, "bob", command, ALICE_THREAD)
    assert refused["success"] is False
    assert refused["markdown"] == "**Error:** Not found"
    assert app.reached == []

    admitted = _execute(app, "alice", command, ALICE_THREAD)
    assert admitted["markdown"] != "**Error:** Not found"
    if command != "/help":  # /help answers before the hook seam
        assert ("hook", ALICE_THREAD) in app.reached


def test_an_admin_and_an_ownerless_thread_pass_and_nothing_is_claimed(app):
    assert _execute(app, "root", "/skills list", ALICE_THREAD)["markdown"] != "**Error:** Not found"
    assert _execute(app, "bob", "/skills list", "fresh-thread")["markdown"] != "**Error:** Not found"
    assert app.agent.accounts_repo.get_thread_owner("fresh-thread") is None


def test_a_shared_channel_admits_the_bot_relay_and_refuses_a_direct_caller(app):
    shared = "discord_111_222"
    direct = _execute(app, "bob", "/skills list", shared)
    assert direct["markdown"] == "**Error:** Not found"
    relayed = _execute(app, "root", "/skills list", shared, **{"X-Nymeria-Act-As": "bob"})
    assert relayed["markdown"] != "**Error:** Not found"
    assert ("handler", shared) in app.reached


def test_an_ownerless_id_that_folds_onto_an_owned_one_is_refused(app):
    # `alice.-thread` has no owner and is stored as `alice-thread`.
    folded = _execute(app, "bob", "/skills list", "alice.-thread")
    assert folded["markdown"] == "**Error:** Not found"
    response = app.client.get(
        "/threads/alice.-thread/notepad", headers=app.auth(app.tokens["bob"])
    )
    assert response.status_code == 404


def test_the_options_route_checks_the_thread(app):
    def options(who: str, thread_id: str) -> int:
        return app.client.get(
            f"/commands/options/skills?thread_id={thread_id}",
            headers=app.auth(app.tokens[who]),
        ).status_code

    assert options("bob", ALICE_THREAD) == 404
    assert options("alice", ALICE_THREAD) == 200
    assert options("bob", "fresh-thread") == 200


# -- TODO targets ------------------------------------------------------------------


def test_a_todo_cannot_be_aimed_at_another_users_thread(app, tmp_path):
    from nymeria.core.todo_manager import TodoManager

    def stored(user_id: str) -> list[str]:
        return [item.thread_id for item in TodoManager(tmp_path).get_todos(user_id).items]

    # Over the command layer (its own thread is bob's, the --thread target is alice's).
    refused = _execute(app, "bob", f"/todos add water plants --thread {ALICE_THREAD}", "bob-thread")
    assert refused["success"] is False and ALICE_THREAD not in stored("bob")
    own = _execute(app, "bob", "/todos add water plants --thread bob-thread", "bob-thread")
    assert own["success"] is True, own["markdown"]
    assert "bob-thread" in stored("bob")

    # Over REST, create and retarget.
    headers = app.auth(app.tokens["bob"])
    create = app.client.post("/todos", headers=headers, json={"task": "x", "thread_id": ALICE_THREAD})
    assert create.status_code == 404
    todo_id = app.client.post("/todos", headers=headers, json={"task": "y"}).json()["id"]
    retarget = app.client.patch(f"/todos/{todo_id}", headers=headers, json={"thread_id": ALICE_THREAD})
    assert retarget.status_code == 404
    assert ALICE_THREAD not in stored("bob")
    assert app.client.patch(
        f"/todos/{todo_id}", headers=headers, json={"thread_id": "bob-thread-2"}
    ).status_code == 200

    # And retargeting over the command layer.
    edit = _execute(app, "bob", f"/todos edit {todo_id} --thread {ALICE_THREAD}", "bob-thread")
    assert edit["success"] is False
    assert ALICE_THREAD not in stored("bob") and "bob-thread-2" in stored("bob")
    moved = _execute(app, "bob", f"/todos edit {todo_id} --thread bob-thread-3", "bob-thread")
    assert moved["success"] is True, moved["markdown"]
    assert "bob-thread-3" in stored("bob")


# -- the agent's own slash_command (thread_admitted) -------------------------------


class _Accounts:
    def __init__(self) -> None:
        self.owners = {ALICE_THREAD: "alice"}

    def claim_thread(self, thread_id: str, user_id: str) -> str:
        return self.owners.setdefault(thread_id, user_id)

    def get_thread_owner(self, thread_id: str) -> str | None:
        return self.owners.get(thread_id)

    def get_user_by_id(self, user_id: str):
        return SimpleNamespace(
            id=user_id, email="", display_name=user_id, role="admin" if user_id == "root" else "user"
        )


def _agent_turn(user_id: str, thread_id: str, command: str, *, admitted: bool):
    agent = SimpleNamespace(
        accounts_repo=_Accounts(),
        thread_config_manager=SimpleNamespace(get_config=lambda thread_id: None),
    )
    ctx = CommandContext(
        user_id=user_id, thread_id=thread_id, source="agent", thread_admitted=admitted
    )
    client = CommandBackendClient.from_context(ctx, agent=agent)
    return run(CommandService().execute(ctx, command, api=client))


@pytest.fixture
def notes(monkeypatch, tmp_path):
    from nymeria.tools import thread_notes

    monkeypatch.setattr(
        "nymeria.config.get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path, memory_char_limit=100),
    )
    monkeypatch.setattr(thread_notes, "_notes_dir", None)
    thread_notes.write_notepad("discord_111_222", "channel notes", mode="replace")
    thread_notes.write_notepad(ALICE_THREAD, "alice notes", mode="replace")


def test_a_non_admin_agent_reaches_its_own_shared_channel_turn(notes):
    own_turn = _agent_turn("bob", "discord_111_222", "/notepad read", admitted=True)
    assert own_turn.success is True and "channel notes" in own_turn.markdown
    unadmitted = _agent_turn("bob", "discord_111_222", "/notepad read", admitted=False)
    assert unadmitted.success is False


def test_admission_is_not_ownership_of_a_personal_thread(notes):
    result = _agent_turn("bob", ALICE_THREAD, "/notepad read", admitted=True)
    assert result.success is False and "alice notes" not in result.markdown


def test_admission_covers_only_the_admitted_thread():
    """An agent turn is admitted to ITS thread, not to every shared channel:
    otherwise a non-admin's agent could aim a TODO (or any door) at a guild
    channel its user never reached."""
    import httpx

    def client_for(thread_id: str):
        agent = SimpleNamespace(accounts_repo=_Accounts())
        ctx = CommandContext(
            user_id="bob", thread_id=thread_id, source="agent", thread_admitted=True
        )
        return CommandBackendClient.from_context(ctx, agent=agent)

    in_channel = client_for("discord_111_222")
    in_channel.require_thread_access("discord_111_222")
    with pytest.raises(httpx.HTTPStatusError):
        in_channel.require_thread_access("discord_999_888")

    from_personal = client_for("bob-thread")
    from_personal.require_thread_access("bob-thread")
    with pytest.raises(httpx.HTTPStatusError):
        from_personal.require_thread_access("discord_999_888")
    refused = run(
        CommandService().execute(
            CommandContext(user_id="bob", thread_id="bob-thread", source="agent",
                           thread_admitted=True),
            "/todos add water plants --thread discord_999_888",
            api=from_personal,
        )
    )
    assert refused.success is False


def test_slash_command_marks_its_own_turn_admitted(monkeypatch):
    import importlib

    slash_command = importlib.import_module("nymeria.tools.slash_command")

    seen: list[CommandContext] = []

    class _Service:
        async def execute(self, ctx, command, **kwargs):
            seen.append(ctx)
            return SimpleNamespace(markdown="ok")

    monkeypatch.setattr(slash_command, "get_command_service", lambda: _Service())
    config = {"configurable": {"user_id": "bob", "thread_id": "discord_111_222"}}
    assert run(slash_command._dispatch_command("/notepad read", config)) == "ok"
    assert seen[0].thread_admitted is True and seen[0].thread_id == "discord_111_222"
    assert seen[0].via_act_as is False  # hooks keep reading the truthful bit


# -- watchdog tools ------------------------------------------------------------------


class _ReachedTodoStore(Exception):
    pass


@pytest.fixture
def watchdog(monkeypatch, notes):
    import importlib

    watchdog_module = importlib.import_module("nymeria.tools.watchdog_dispatch")
    agent = SimpleNamespace(accounts_repo=_Accounts())
    monkeypatch.setattr("nymeria.tools.utils.current_agent", lambda: agent)

    class _Todos:
        def atomic_update(self, user_id):
            raise _ReachedTodoStore(user_id)

    monkeypatch.setattr(watchdog_module, "_get_todo_manager", lambda: _Todos())
    return SimpleNamespace(module=watchdog_module)


def _cfg(user_id: str) -> dict:
    return {"configurable": {"user_id": user_id, "thread_id": f"{user_id}-watchdog"}}


def test_watchdog_reads_only_threads_its_user_may_read(watchdog):
    read = watchdog.module.watchdog_read_notepad.func
    refused = read(ALICE_THREAD, config=_cfg("bob"))
    assert refused.startswith("[Error]") and "alice notes" not in refused
    assert read(ALICE_THREAD, config=_cfg("alice")) == "alice notes"
    assert read(ALICE_THREAD, config=_cfg("root")) == "alice notes"
    assert read("discord_111_222", config=_cfg("bob")).startswith("[Error]")


def test_watchdog_cannot_dispatch_to_another_users_thread(watchdog):
    dispatch = watchdog.module.watchdog_dispatch.func
    result = dispatch(ALICE_THREAD, "do something", config=_cfg("bob"))
    assert result.startswith("[Error]") and ALICE_THREAD in result
    with pytest.raises(_ReachedTodoStore):  # the owner's dispatch gets as far as storing it
        dispatch(ALICE_THREAD, "do something", config=_cfg("alice"))


# -- #426 global skill install ---------------------------------------------------------


@pytest.mark.parametrize(
    "user_id, scope, fetched",
    [("bob", "global", False), ("bob", "user", True), ("root", "global", True)],
)
def test_global_skill_install_is_admin_only(monkeypatch, tmp_path, user_id, scope, fetched):
    from nymeria.skills import marketplace

    calls: list[str] = []

    class _Fetcher:
        def fetch(self, name, target_dir):
            calls.append(str(target_dir))
            return SimpleNamespace(name=name)

    monkeypatch.setattr(marketplace, "get_fetcher", lambda source: _Fetcher())
    skill_manager = SimpleNamespace(
        target_dir=lambda scope, user_id=None: tmp_path / scope, reload=lambda: None
    )
    agent = SimpleNamespace(accounts_repo=_Accounts(), skill_manager=skill_manager)
    ctx = CommandContext(user_id=user_id, thread_id=None, actor="user", surface="cli")
    client = CommandBackendClient(agent, user=_CommandBackendUser(id=user_id, role="user"))
    result = run(
        CommandService().execute(ctx, f"/skills install demo --scope {scope}", api=client)
    )
    assert bool(calls) is fetched
    assert result.success is fetched
    if not fetched:
        assert "requires admin" in result.markdown


# -- ratchet ---------------------------------------------------------------------------


def test_every_api_entry_says_whether_it_gates_the_thread():
    """Under ``nymeria/api/`` a thread id usually comes from the client, so
    every ``get_command_service().execute(...)`` there must pass
    ``gate_thread`` EXPLICITLY: a new entry cannot inherit the ungated
    default by omission."""
    root = Path(command_service_module.__file__).resolve().parents[1] / "api"
    missing: list[str] = []
    seen = 0
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            target = node.func.value
            if node.func.attr != "execute" or not (
                isinstance(target, ast.Call)
                and isinstance(target.func, ast.Name)
                and target.func.id == "get_command_service"
            ):
                continue
            seen += 1
            if not any(keyword.arg == "gate_thread" for keyword in node.keywords):
                missing.append(f"{path.name}:{node.lineno}")
    assert seen >= 2, "the sweep found no entries: the pattern moved"
    assert not missing, f"execute() calls without an explicit gate_thread: {missing}"
