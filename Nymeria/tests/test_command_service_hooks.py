"""Tests for the /hook backend commands (lifecycle-hooks Pass 3, slice C1).

Drives the real ``CommandService`` against a real ``HookManager`` backed by a
tmp dir (patched onto ``tools.hooks._get_hook_manager``), so the flag grammar,
the shared flat-field -> params mapping, and the store validation are exercised
end to end. Mirrors ``test_command_service_triggers.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli_fixtures import run
import nymeria.core.agent as agent_module
from nymeria.core.command_service import CommandContext, CommandService
from nymeria.core.hook_manager import HookManager


class _FakeAccountsRepo:
    def get_user_by_id(self, user_id: str):
        return SimpleNamespace(
            id=user_id, email=f"{user_id}@example.test", display_name=user_id, role="admin"
        )


@pytest.fixture(autouse=True)
def patched_agent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        agent_module,
        "get_current_agent",
        lambda: SimpleNamespace(accounts_repo=_FakeAccountsRepo()),
    )


@pytest.fixture
def manager(tmp_path, monkeypatch: pytest.MonkeyPatch) -> HookManager:
    mgr = HookManager(tmp_path)
    monkeypatch.setattr("nymeria.tools.hooks._get_hook_manager", lambda: mgr)
    return mgr


def _ctx(thread_id: str | None = "thread-1") -> CommandContext:
    return CommandContext(
        user_id="alice",
        thread_id=thread_id or "",
        actor="user",
        surface="cli",
        is_admin=True,
    )


def _run(cmd: str, thread_id: str | None = "thread-1"):
    return run(CommandService().execute(_ctx(thread_id), cmd))


def _user_hooks(manager: HookManager):
    """Alice's hooks minus the ever-present virtual system turn-metadata hook."""
    from nymeria.core.hook_manager import SYSTEM_HOOK_IDS
    return [h for h in manager.get_hooks("alice") if h.id not in SYSTEM_HOOK_IDS]


def _only(manager: HookManager):
    hooks = _user_hooks(manager)
    assert len(hooks) == 1, hooks
    return hooks[0]


# --- create: one per action, verifying the stored logic --------------------

def test_create_block_if_matches(manager: HookManager) -> None:
    result = _run(
        '/hook create Block rm -rf --event pre_tool_use --action block_if_matches '
        '--matcher Bash --cond "command contains rm -rf" --reason "no destructive"'
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.event == "pre_tool_use"
    assert hook.logic.action == "block_if_matches"
    assert hook.matcher == "Bash"
    assert hook.scope == "thread"
    assert hook.thread_id == "thread-1"
    assert hook.created_by == "user"
    assert len(hook.logic.conditions) == 1
    cond = hook.logic.conditions[0]
    assert (cond.field, cond.operator, cond.value) == ("command", "contains", "rm -rf")
    assert hook.logic.reason == "no destructive"


def test_create_rewrite_arg(manager: HookManager) -> None:
    result = _run(
        '/hook create Redirect --event pre_tool_use --action rewrite_arg '
        '--matcher Write --cond "file_path starts_with /etc" --set "file_path=/tmp/blocked"'
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.logic.action == "rewrite_arg"
    assert hook.logic.updates == {"file_path": "/tmp/blocked"}
    assert hook.logic.conditions[0].operator == "starts_with"


def test_create_inject_context(manager: HookManager) -> None:
    result = _run(
        '/hook create Remind --event prompt_submit --action inject_context '
        '--text "follow the house style"'
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.event == "prompt_submit"
    assert hook.logic.action == "inject_context"
    assert hook.logic.text == "follow the house style"


def test_create_webhook(manager: HookManager) -> None:
    result = _run(
        '/hook create Ping --event done --action webhook '
        '--url https://example.com/hook --text "done {thread_id}"'
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.logic.action == "webhook"
    assert hook.logic.url == "https://example.com/hook"
    assert hook.logic.text == "done {thread_id}"


def test_create_global_scope_has_no_thread(manager: HookManager) -> None:
    result = _run(
        '/hook create Global reminder --event prompt_submit --action inject_context '
        '--text hi --scope global'
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.scope == "global"
    assert hook.thread_id == ""


def test_create_thread_scope_without_thread_errors(manager: HookManager) -> None:
    result = _run(
        '/hook create Nope --event prompt_submit --action inject_context --text hi',
        thread_id=None,
    )
    assert result.success is False
    assert "active thread" in result.markdown
    assert _user_hooks(manager) == []


def test_create_illegal_event_action_pair(manager: HookManager) -> None:
    result = _run(
        '/hook create Bad --event prompt_submit --action block_if_matches --cond "x contains y"'
    )
    assert result.success is False
    assert "not valid for event" in result.markdown


def test_create_bad_condition_operator(manager: HookManager) -> None:
    result = _run(
        '/hook create Bad --event pre_tool_use --action block_if_matches '
        '--matcher Bash --cond "command wat rm"'
    )
    assert result.success is False
    assert "invalid condition operator" in result.markdown


def test_create_text_action_requires_text(manager: HookManager) -> None:
    result = _run('/hook create NoText --event prompt_submit --action inject_context')
    assert result.success is False
    assert "requires --text" in result.markdown


def test_create_rewrite_requires_set(manager: HookManager) -> None:
    result = _run(
        '/hook create NoSet --event pre_tool_use --action rewrite_arg --matcher Write'
    )
    assert result.success is False
    assert "requires at least one --set" in result.markdown


def test_create_reports_hook_limit(manager: HookManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager, "add_hook", lambda *a, **k: None)
    result = _run(
        '/hook create Over --event prompt_submit --action inject_context --text hi'
    )
    assert result.success is False
    assert "Hook limit reached" in result.markdown


def test_create_requires_name(manager: HookManager) -> None:
    result = _run('/hook create --event prompt_submit --action inject_context --text hi')
    assert result.success is False
    assert "Missing required argument: name" in result.markdown
    assert _user_hooks(manager) == []

    # A quoted empty string binds as a name token but is still not a name.
    blank = _run(
        '/hook create "" --event prompt_submit --action inject_context --text hi'
    )
    assert blank.success is False
    assert "requires a name" in blank.markdown
    assert _user_hooks(manager) == []


def test_create_rejects_unknown_flag(manager: HookManager) -> None:
    # A misspelled/unknown option must error, not fold silently into the name.
    result = _run(
        '/hook create MyHook --event done --action notify --text hi --bogus junk'
    )
    assert result.success is False
    assert "Unknown option `--bogus`" in result.markdown
    assert _user_hooks(manager) == []


def test_plural_alias_routes_subcommand(manager: HookManager) -> None:
    # `/hooks create ...` (plural alias) resolves to the bare `hook` path with the
    # subcommand in args; the bare handler must re-dispatch it, not usage-error.
    result = _run(
        '/hooks create Via alias --event done --action notify --text "hi {thread_id}"'
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.name == "Via alias"
    assert hook.logic.action == "notify"


# --- list + filters ---------------------------------------------------------

def test_list_table_and_filters(manager: HookManager) -> None:
    manager.add_hook("alice", name="A on", event="prompt_submit", text="a",
                     scope="thread", thread_id="thread-1", enabled=True)
    manager.add_hook("alice", name="B off", event="prompt_submit", text="b",
                     scope="thread", thread_id="thread-1", enabled=False)
    manager.add_hook("alice", name="C global", event="prompt_submit", text="c",
                     scope="global")
    manager.add_hook("alice", name="D other", event="prompt_submit", text="d",
                     scope="thread", thread_id="thread-9", enabled=True)

    full = _run("/hook list")
    assert "| ID | State | Event | Action | Scope | Name |" in full.markdown
    for name in ("A on", "B off", "C global", "D other"):
        assert name in full.markdown

    enabled = _run("/hook list --enabled-only")
    assert "B off" not in enabled.markdown
    assert "A on" in enabled.markdown

    glob = _run("/hook list --global")
    assert "C global" in glob.markdown
    assert "A on" not in glob.markdown

    this_thread = _run("/hook list --thread current")
    # global + this-thread hooks show; the other thread's does not.
    assert "A on" in this_thread.markdown
    assert "C global" in this_thread.markdown
    assert "D other" not in this_thread.markdown


def test_list_empty(manager: HookManager) -> None:
    # A pristine store still lists the virtual system turn-metadata hook.
    result = _run("/hook list")
    assert result.success is True
    assert "turn-metadata" in result.markdown
    assert "turn_metadata" in result.markdown


def test_bare_hook_lists(manager: HookManager) -> None:
    manager.add_hook("alice", name="Solo", event="prompt_submit", text="x",
                     scope="global")
    result = _run("/hook")
    assert "Solo" in result.markdown


# --- show / test / enable / disable / delete --------------------------------

def test_show_renders_detail(manager: HookManager) -> None:
    hook = manager.add_hook("alice", name="Guard", event="pre_tool_use",
                            action="block_if_matches",
                            params={"conditions": [{"field": "command", "operator": "contains",
                                                    "value": "rm"}], "reason": "no"},
                            matcher="Bash", scope="global")
    assert hook is not None
    result = _run(f"/hook show {hook.id}")
    assert hook.id in result.markdown
    assert "block_if_matches" in result.markdown


def test_test_renders_dry_run(manager: HookManager) -> None:
    hook = manager.add_hook("alice", name="Inj", event="prompt_submit",
                            text="hello {user_id}", scope="global")
    assert hook is not None
    result = _run(f"/hook test {hook.id}")
    assert result.success is True
    assert "Test render" in result.markdown


def test_enable_disable_flips(manager: HookManager) -> None:
    hook = manager.add_hook("alice", name="Toggle", event="prompt_submit",
                            text="x", scope="global", enabled=True)
    assert hook is not None
    _run(f"/hook disable {hook.id}")
    assert manager.get_hook("alice", hook.id).enabled is False
    _run(f"/hook enable {hook.id}")
    assert manager.get_hook("alice", hook.id).enabled is True


def test_delete_removes(manager: HookManager) -> None:
    hook = manager.add_hook("alice", name="Gone", event="prompt_submit",
                            text="x", scope="global")
    assert hook is not None
    result = _run(f"/hook delete {hook.id} --yes")
    assert result.success is True
    assert _user_hooks(manager) == []


def test_delete_pristine_system_hook_says_already_at_defaults(manager: HookManager) -> None:
    """/hook delete on the never-edited system hook is an idempotent reset."""
    result = _run("/hook delete turn-metadata --yes")
    assert result.success is True
    assert "already at its built-in defaults" in result.markdown


def test_delete_customized_system_hook_resets(manager: HookManager) -> None:
    """/hook delete on a materialized system hook RESETS it (reset copy, not delete)."""
    # A name edit materializes the copy-on-write record via the command surface.
    edited = _run("/hook edit turn-metadata name=mine")
    assert edited.success is True, edited.markdown
    assert any(h.id == "turn-metadata" for h in manager.get_hooks_cached("alice"))
    result = _run("/hook delete turn-metadata --yes")
    assert result.success is True
    assert "Reset system hook `turn-metadata`" in result.markdown
    # Back to virtual: nothing stored to remove.
    assert manager.get_hooks_cached("alice") == []


def test_show_unknown_id(manager: HookManager) -> None:
    result = _run("/hook show deadbeef")
    assert result.success is False
    assert "No hook matching" in result.markdown


def test_resolve_ambiguous_prefix(manager: HookManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        manager, "get_hooks",
        lambda uid: [SimpleNamespace(id="ab01"), SimpleNamespace(id="ab02")],
    )
    result = _run("/hook show ab")
    assert result.success is False
    assert "matches multiple hooks" in result.markdown


# --- edit -------------------------------------------------------------------

def test_edit_scalar_fields(manager: HookManager) -> None:
    hook = manager.add_hook("alice", name="Old", event="prompt_submit",
                            text="x", scope="global", enabled=True)
    assert hook is not None
    result = _run(f'/hook edit {hook.id} name="New name" enabled=false')
    assert result.success is True, result.markdown
    updated = manager.get_hook("alice", hook.id)
    assert updated.name == "New name"
    assert updated.enabled is False


def test_edit_conditions_merge_preserves_reason(manager: HookManager) -> None:
    hook = manager.add_hook("alice", name="Guard", event="pre_tool_use",
                            action="block_if_matches",
                            params={"conditions": [], "reason": "keep me"},
                            matcher="Bash", scope="global")
    assert hook is not None
    result = _run(f'/hook edit {hook.id} --cond "command contains rm"')
    assert result.success is True, result.markdown
    updated = manager.get_hook("alice", hook.id)
    assert len(updated.logic.conditions) == 1
    assert updated.logic.reason == "keep me"  # sibling preserved on partial edit


def test_edit_action_switch(manager: HookManager) -> None:
    hook = manager.add_hook("alice", name="Switch", event="done",
                            action="inject_context", text="hi", scope="global")
    assert hook is not None
    result = _run(f'/hook edit {hook.id} action=notify text="notify now"')
    assert result.success is True, result.markdown
    updated = manager.get_hook("alice", hook.id)
    assert updated.logic.action == "notify"
    assert updated.logic.text == "notify now"


def test_edit_rejects_rescope(manager: HookManager) -> None:
    hook = manager.add_hook("alice", name="Scoped", event="prompt_submit",
                            text="x", scope="global")
    assert hook is not None
    result = _run(f"/hook edit {hook.id} scope=thread")
    assert result.success is False
    assert "re-scope" in result.markdown


def test_edit_no_updates(manager: HookManager) -> None:
    hook = manager.add_hook("alice", name="Idle", event="prompt_submit",
                            text="x", scope="global")
    assert hook is not None
    result = _run(f"/hook edit {hook.id}")
    assert result.success is False
    assert "No updates" in result.markdown


# --- /hook log ---------------------------------------------------------------

def test_hook_log_empty(manager: HookManager) -> None:
    result = _run("/hook log")
    assert result.success is True
    assert "No hook executions" in result.markdown


def test_hook_log_lists_and_filters(manager: HookManager) -> None:
    from nymeria.core.hook_manager import HookExecution

    hook = manager.add_hook("alice", name="Guard", event="pre_tool_use",
                            action="block_if_matches", params={"conditions": []})
    assert hook is not None
    manager.log_execution("alice", HookExecution(
        hook_id=hook.id, hook_name="Guard", event="pre_tool_use", plane="mutate",
        status="ok", detail="deny: nope", tool_name="bash",
    ))
    manager.log_execution("alice", HookExecution(
        hook_id="deadbeef", hook_name="Other", event="done", plane="observe", status="no_op",
    ))
    result = _run("/hook log")
    assert result.success is True
    assert "Hook executions: 2" in result.markdown
    assert "deny: nope" in result.markdown
    # Filter by (prefix-resolved) hook id.
    result = _run(f"/hook log {hook.id[:4]}")
    assert result.success is True
    assert hook.id in result.markdown
    assert "deadbeef" not in result.markdown


def test_hook_log_unknown_prefix(manager: HookManager) -> None:
    result = _run("/hook log zzzz")
    assert result.success is False
    assert "No hook matching" in result.markdown


# --- run_command authoring gate ---------------------------------------------

def _set_run_command_flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    from nymeria import config as config_module

    monkeypatch.setattr(
        config_module, "get_settings",
        lambda: SimpleNamespace(hooks_run_command_enabled=enabled),
    )


def test_run_command_create_edit_roundtrip(
    manager: HookManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_run_command_flag(monkeypatch, True)  # caller is admin via patched_agent
    result = _run(
        '/hook create RC --event done --action run_command '
        '--command "echo hi" --timeout 20'
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.logic.action == "run_command"
    assert hook.logic.command == "echo hi"
    assert hook.logic.timeout_seconds == 20
    edited = _run(f'/hook edit {hook.id[:6]} command="echo bye"')
    assert edited.success is True, edited.markdown
    assert _user_hooks(manager)[0].logic.command == "echo bye"


def test_run_command_requires_command_flag(
    manager: HookManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_run_command_flag(monkeypatch, True)
    result = _run('/hook create RC --event done --action run_command')
    assert result.success is False
    assert "requires --command" in result.markdown
    assert _user_hooks(manager) == []


def test_run_command_denied_when_flag_off(
    manager: HookManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_run_command_flag(monkeypatch, False)
    result = _run(
        '/hook create RC --event done --action run_command --command "echo hi"'
    )
    assert result.success is False
    assert "HOOKS_RUN_COMMAND_ENABLED" in result.markdown
    assert _user_hooks(manager) == []


def test_run_command_denied_for_non_admin(
    manager: HookManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_run_command_flag(monkeypatch, True)
    # Re-point the current agent at a non-admin account (overrides patched_agent).
    monkeypatch.setattr(
        agent_module, "get_current_agent",
        lambda: SimpleNamespace(accounts_repo=_UserRoleAccountsRepo()),
    )
    result = _run(
        '/hook create RC --event done --action run_command --command "echo hi"'
    )
    assert result.success is False
    assert "admin-only" in result.markdown
    assert _user_hooks(manager) == []


class _UserRoleAccountsRepo:
    def get_user_by_id(self, user_id: str):
        return SimpleNamespace(
            id=user_id, email=f"{user_id}@example.test", display_name=user_id, role="user"
        )


def test_run_command_edit_denied_for_non_admin(
    manager: HookManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-place behavior edits re-gate: authoring admin is not a permanent pass."""
    _set_run_command_flag(monkeypatch, True)
    created = _run(
        '/hook create RC --event done --action run_command --command "echo hi"'
    )
    assert created.success is True, created.markdown
    hook = _only(manager)
    monkeypatch.setattr(
        agent_module, "get_current_agent",
        lambda: SimpleNamespace(accounts_repo=_UserRoleAccountsRepo()),
    )
    result = _run(f'/hook edit {hook.id[:6]} command="echo bye"')
    assert result.success is False
    assert "admin-only" in result.markdown
    assert _user_hooks(manager)[0].logic.command == "echo hi"


def test_run_command_edit_enabled_allowed_for_non_admin(
    manager: HookManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled/name-only edits stay ungated so the owner can switch a hook off."""
    _set_run_command_flag(monkeypatch, True)
    created = _run(
        '/hook create RC --event done --action run_command --command "echo hi"'
    )
    assert created.success is True, created.markdown
    hook = _only(manager)
    monkeypatch.setattr(
        agent_module, "get_current_agent",
        lambda: SimpleNamespace(accounts_repo=_UserRoleAccountsRepo()),
    )
    result = _run(f'/hook edit {hook.id[:6]} enabled=false')
    assert result.success is True, result.markdown
    updated = _user_hooks(manager)[0]
    assert updated.enabled is False
    assert updated.logic.command == "echo hi"


# --- require_approval authoring + the approvals resolve surface ---------------


@pytest.fixture
def approvals_store(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Isolated approval store + fresh coordinator + a live waiter loop."""
    import asyncio
    from types import SimpleNamespace as NS

    import nymeria.core.hook_approvals as ha

    settings = NS(data_dir=tmp_path, fcm_enabled=False)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    monkeypatch.setattr(ha, "_coordinator", None)
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _mint(loop, user_id="alice", **kw):
    from nymeria.core.hook_approvals import create_pending_approval

    async def _do():
        return create_pending_approval(
            user_id=user_id,
            thread_id=kw.get("thread_id", "thread-1"),
            hook_id="h1",
            hook_name="guard",
            tool_name=kw.get("tool_name", "bash_execute"),
            tool_call_id=kw.get("tool_call_id", "call-1"),
            tool_args={"command": "ls"},
            prompt="Approve?",
            window_seconds=60.0,
        )

    return loop.run_until_complete(_do())


def test_create_require_approval(manager: HookManager) -> None:
    result = _run(
        '/hook create Approve bash --event pre_tool_use --action require_approval '
        '--matcher bash_execute --cond "command contains sudo" '
        '--text "Run {tool_name}?" --timeout 240'
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.logic.action == "require_approval"
    assert hook.matcher == "bash_execute"
    assert hook.logic.prompt == "Run {tool_name}?"
    assert hook.logic.timeout_seconds == 240.0
    assert len(hook.logic.conditions) == 1


def test_create_require_approval_bare_is_legal(manager: HookManager) -> None:
    result = _run(
        "/hook create Ask first --event pre_tool_use --action require_approval"
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.logic.action == "require_approval"
    assert hook.logic.conditions == []
    assert hook.logic.timeout_seconds == 180.0


def test_hook_approvals_lists_pending(manager: HookManager, approvals_store) -> None:
    record, _ = _mint(approvals_store)
    result = _run("/hook approvals")
    assert result.success is True, result.markdown
    assert record["record_id"] in result.markdown
    assert "bash_execute" in result.markdown


def test_hook_approve_wakes_waiter(manager: HookManager, approvals_store) -> None:
    import asyncio

    record, future = _mint(approvals_store)
    result = _run(f"/hook approve {record['record_id'][:6]} looks fine")
    assert result.success is True, result.markdown
    resolved = approvals_store.run_until_complete(asyncio.wait_for(future, 2))
    assert resolved["approved"] is True
    assert resolved["resolved_by"] == "alice"
    assert resolved["note"] == "looks fine"


def test_hook_deny_wakes_waiter_with_note(manager: HookManager, approvals_store) -> None:
    import asyncio

    record, future = _mint(approvals_store)
    result = _run(f"/hook deny {record['record_id']} not now")
    assert result.success is True, result.markdown
    resolved = approvals_store.run_until_complete(asyncio.wait_for(future, 2))
    assert resolved["approved"] is False
    assert resolved["note"] == "not now"


def test_hook_approve_stale_record_reports_and_cleans(
    manager: HookManager, approvals_store
) -> None:
    from nymeria.core.hook_approvals import (
        get_hook_approval_coordinator,
        load_record,
    )

    record, _ = _mint(approvals_store)
    get_hook_approval_coordinator().discard(record["record_id"])
    result = _run(f"/hook approve {record['record_id']}")
    assert result.success is False
    assert "no longer pending" in result.markdown
    assert load_record(record["record_id"]) is None


def test_hook_approvals_blocked_for_agent_actor_on_both_parse_paths(
    manager: HookManager, approvals_store
) -> None:
    """The agent must never resolve its own held tool calls, on ANY route.

    agent_allowed=False on the subcommand definitions only gates the direct
    "/hook <sub>" parse path; the plural "/hooks <sub>" alias resolves to the
    parent handler (agent_allowed=True) and re-dispatches internally, so the
    executor re-checks the actor. Both gates are load-bearing.
    """
    from nymeria.core.hook_approvals import get_hook_approval_coordinator

    record, _future = _mint(approvals_store)
    agent_ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="agent",
        surface="api",
        is_admin=True,
    )
    for cmd in (
        f"/hook approve {record['record_id']}",
        f"/hook deny {record['record_id']}",
        "/hook approvals",
        f"/hooks approve {record['record_id']}",
        f"/hooks deny {record['record_id']}",
        "/hooks approvals",
    ):
        result = run(CommandService().execute(agent_ctx, cmd))
        assert result.success is False, (cmd, result.markdown)
        assert "not available to the agent" in result.markdown, (cmd, result.markdown)
    # The hold is still pending: nothing was popped from the rendezvous.
    assert get_hook_approval_coordinator().pending_count() == 1


def test_hook_approve_scopes_to_owner_for_non_admin(
    manager: HookManager, approvals_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Demote alice: a non-admin must not see or resolve another user's hold.
    class _UserRepo:
        def get_user_by_id(self, user_id: str):
            return SimpleNamespace(
                id=user_id, email="x@example.test", display_name=user_id, role="user"
            )

    monkeypatch.setattr(
        agent_module,
        "get_current_agent",
        lambda: SimpleNamespace(accounts_repo=_UserRepo()),
    )
    foreign, _ = _mint(approvals_store, user_id="bob")
    listing = _run("/hook approvals")
    assert "No pending hook approvals" in listing.markdown
    result = _run(f"/hook approve {foreign['record_id']}")
    assert result.success is False
    assert "No pending approval" in result.markdown


# --- Definition-level fire gate (--fire-cond / --once) -------------------------

def test_create_with_fire_cond_and_once(manager):
    result = _run(
        '/hook create advisory --event post_tool_use --action inject_context '
        '--text "wrap up now" --scope global '
        '--fire-cond "context_pct_of_trigger gte 85" --once'
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.once is True
    assert len(hook.fire_conditions) == 1
    cond = hook.fire_conditions[0]
    assert (cond.field, cond.operator, cond.value) == (
        "context_pct_of_trigger", "gte", "85"
    )


def test_create_rejects_bad_fire_cond_operator(manager):
    result = _run(
        '/hook create n --event done --action inject_context --text x '
        '--fire-cond "pct sideways 85"'
    )
    assert result.success is False
    assert "invalid condition operator" in result.markdown
    assert _user_hooks(manager) == []


def test_edit_fire_gate(manager):
    _run('/hook create n --event done --action inject_context --text x --scope global')
    hook = _only(manager)
    result = _run(
        f'/hook edit {hook.id} once=true --fire-cond "final_text contains FAIL"'
    )
    assert result.success is True, result.markdown
    updated = manager.get_hook("alice", hook.id)
    assert updated.once is True
    assert updated.fire_conditions[0].value == "FAIL"
    # And back off.
    result = _run(f"/hook edit {hook.id} once=false")
    assert result.success is True, result.markdown
    assert manager.get_hook("alice", hook.id).once is False


# --- templates + install (bundled hook-template catalog) --------------------

def test_hook_templates_lists_bundled_catalog(manager):
    result = _run("/hook templates")
    assert result.success is True, result.markdown
    assert "context-checkpoint-advisory" in result.markdown
    assert "/hook install" in result.markdown


def test_hook_install_bundled_template(manager):
    result = _run("/hook install context-checkpoint-advisory")
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.template == "context-checkpoint-advisory"
    assert hook.event == "post_tool_use"
    assert hook.scope == "global"
    assert hook.once is True
    assert hook.created_by == "user"


def test_hook_install_is_idempotent(manager):
    _run("/hook install context-checkpoint-advisory")
    result = _run("/hook install context-checkpoint-advisory")
    assert result.success is True, result.markdown
    assert "already installed" in result.markdown
    assert len(_user_hooks(manager)) == 1


def test_hook_install_thread_scope_binds_current_thread(manager):
    result = _run("/hook install context-checkpoint-advisory --scope thread")
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.scope == "thread"
    assert hook.thread_id == "thread-1"


def test_hook_install_thread_scope_requires_thread(manager):
    result = _run(
        "/hook install context-checkpoint-advisory --scope thread", thread_id=None
    )
    assert result.success is False
    assert "active thread" in result.markdown


def test_hook_install_unknown_template(manager):
    result = _run("/hook install no-such-template")
    assert result.success is False
    assert "Unknown template" in result.markdown
    assert _user_hooks(manager) == []


def test_hook_install_requires_template_id(manager):
    result = _run("/hook install")
    assert result.success is False
    assert "Usage" in result.markdown


def test_hook_install_disabled_flag(manager):
    result = _run("/hook install context-checkpoint-advisory --disabled")
    assert result.success is True, result.markdown
    assert _only(manager).enabled is False


def test_create_single_use_flag_and_edit(manager):
    result = _run(
        "/hook create oneshot --event done --action inject_context "
        "--text follow --single-use --scope global"
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.single_use is True
    result = _run(f"/hook edit {hook.id} single_use=false")
    assert result.success is True, result.markdown
    assert manager.get_hook("alice", hook.id).single_use is False


# --- run_workflow authoring ---------------------------------------------------

def _pass_workflow_binding(monkeypatch: pytest.MonkeyPatch, error: str | None = None):
    """Stub the bind-time workflow validation (no published workflows in tests)."""
    monkeypatch.setattr(
        "nymeria.core.workflows.tool_runtime.workflow_binding_error",
        lambda workflow_id, params, allow_event=False: error,
    )


def test_run_workflow_create_edit_roundtrip(
    manager: HookManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pass_workflow_binding(monkeypatch)
    result = _run(
        '/hook create Guard --event pre_tool_use --action run_workflow '
        '--matcher Bash --workflow wf_guard --workflow-params \'{"mode": "strict"}\' '
        "--on-fault deny --timeout 45"
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.event == "pre_tool_use"
    assert hook.matcher == "Bash"
    assert hook.logic.action == "run_workflow"
    assert hook.logic.workflow_id == "wf_guard"
    assert hook.logic.params == {"mode": "strict"}
    assert hook.logic.on_fault == "deny"
    assert hook.logic.timeout_seconds == 45
    edited = _run(
        f'/hook edit {hook.id[:6]} workflow_params=\'{{"mode": "lax"}}\' on_fault=allow'
    )
    assert edited.success is True, edited.markdown
    updated = _user_hooks(manager)[0]
    assert updated.logic.params == {"mode": "lax"}
    assert updated.logic.on_fault == "allow"
    assert updated.logic.workflow_id == "wf_guard"  # untouched fields survive


def test_run_workflow_requires_workflow_flag(manager: HookManager) -> None:
    result = _run("/hook create G --event done --action run_workflow")
    assert result.success is False
    assert "--workflow" in result.markdown
    assert _user_hooks(manager) == []


def test_run_workflow_rejects_bad_params_json(manager: HookManager) -> None:
    result = _run(
        "/hook create G --event done --action run_workflow "
        "--workflow wf_x --workflow-params not-json"
    )
    assert result.success is False
    assert "JSON object" in result.markdown
    assert _user_hooks(manager) == []


def test_run_workflow_create_surfaces_binding_error(
    manager: HookManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pass_workflow_binding(monkeypatch, "no published workflow tool named 'wf_x'")
    result = _run(
        "/hook create G --event done --action run_workflow --workflow wf_x"
    )
    assert result.success is False
    assert "no published workflow tool named 'wf_x'" in result.markdown
    assert _user_hooks(manager) == []


# --- declared-argument adoption (backlog #129 wave 1c) ------------------------


def test_create_name_collects_the_bare_words_around_the_options(
    manager: HookManager,
) -> None:
    """The name is whatever the options leave behind, in any position.

    The hand parser pulled ``--option value`` pairs out of the token list from
    anywhere and joined the remainder, so a multi-word name works unquoted and
    a bare word AFTER an option still belongs to the name. Both spellings are
    in live use (the Discord cog sends name-then-options).
    """
    result = _run(
        "/hook create Rate limit --event done --action notify guard "
        '--text "hi {thread_id}" --scope global'
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.name == "Rate limit guard"
    assert hook.logic.action == "notify"


def test_hook_commands_reject_arguments_they_used_to_ignore(
    manager: HookManager,
) -> None:
    """Extras were silently dropped by every hand parser in this family."""
    for command in ("/hook templates", "/hook list"):
        ok = _run(command)
        assert ok.success is True, command

        rejected = _run(f"{command} bogus")
        assert rejected.success is False, command
        assert "Unexpected argument `bogus`" in rejected.markdown, command

    # /hook log takes one optional hook id and used to ignore anything after it.
    extra = _run("/hook log deadbeef bogus")
    assert extra.success is False
    assert "Unexpected argument `bogus`" in extra.markdown

    # A typo'd subcommand on the family root keeps the guidance layer.
    unknown = _run("/hook frobnicate")
    assert unknown.success is False
    assert "Usage:" in unknown.markdown
    assert "Subcommands:" in unknown.markdown


def test_hook_options_accept_the_equals_spelling(manager: HookManager) -> None:
    result = _run(
        '/hook create Equals --event done --action=notify --text="hi there" '
        "--scope=global"
    )
    assert result.success is True, result.markdown
    hook = _only(manager)
    assert hook.logic.action == "notify"
    assert hook.logic.text == "hi there"
    assert hook.scope == "global"


def test_hook_log_limit_is_an_integer(manager: HookManager) -> None:
    from nymeria.core.hook_manager import HookExecution

    for index in range(3):
        manager.log_execution("alice", HookExecution(
            hook_id=f"h{index}", hook_name="n", event="done", plane="observe",
            status="ok", detail=f"entry {index}",
        ))

    capped = _run("/hook log --limit=2")
    assert capped.success is True, capped.markdown
    assert "Hook executions: 2" in capped.markdown

    # The old parser silently fell back to 20 rows for a non-numeric limit.
    bad = _run("/hook log --limit abc")
    assert bad.success is False
    assert "--limit must be an integer, got `abc`" in bad.markdown


def test_delete_accepts_the_short_confirm_flag(manager: HookManager) -> None:
    hook = manager.add_hook("alice", name="Gone", event="prompt_submit",
                            text="x", scope="global")
    assert hook is not None
    result = _run(f"/hook delete {hook.id} -y")
    assert result.success is True, result.markdown
    assert _user_hooks(manager) == []


def test_cond_must_be_quoted_and_an_unbalanced_quote_is_honest(
    manager: HookManager,
) -> None:
    """``--cond`` values are one quoted token, so quoting is now enforced.

    The family has no free-text tail, so the splitter's silent whitespace
    fallback would validate a tokenization the user never meant.
    """
    ok = _run(
        "/hook create Guard --event pre_tool_use --action block_if_matches "
        '--matcher Bash --cond "command contains rm -rf" --reason no --scope global'
    )
    assert ok.success is True, ok.markdown
    condition = _only(manager).logic.conditions[0]
    assert (condition.field, condition.operator, condition.value) == (
        "command", "contains", "rm -rf"
    )

    broken = _run(
        "/hook create Guard2 --event pre_tool_use --action block_if_matches "
        '--matcher Bash --cond "command contains rm --reason no --scope global'
    )
    assert broken.success is False
    assert "Unbalanced quote" in broken.markdown
    assert len(_user_hooks(manager)) == 1  # nothing new stored


def test_edit_takes_key_values_and_options_in_any_order(manager: HookManager) -> None:
    """The Discord cog sends key=value tokens BEFORE ``--cond``/``--set``."""
    hook = manager.add_hook("alice", name="Guard", event="pre_tool_use",
                            action="block_if_matches",
                            params={"conditions": [], "reason": "keep"},
                            matcher="Bash", scope="global")
    assert hook is not None
    result = _run(
        f'/hook edit {hook.id} name="New name" --cond "command contains rm" '
        "enabled=false"
    )
    assert result.success is True, result.markdown
    updated = manager.get_hook("alice", hook.id)
    assert updated.name == "New name"
    assert updated.enabled is False
    assert updated.logic.conditions[0].value == "rm"


def test_hook_root_still_serves_the_unregistered_detail_synonym(
    manager: HookManager,
) -> None:
    """``detail`` has no registered path, so the parent handler renders it."""
    hook = manager.add_hook("alice", name="Solo", event="prompt_submit",
                            text="x", scope="global")
    assert hook is not None

    for command in (f"/hook detail {hook.id}", f"/hooks detail {hook.id}"):
        result = _run(command)
        assert result.success is True, (command, result.markdown)
        assert f"Hook {hook.id}" in result.markdown, command

    bare = _run("/hook detail")
    assert bare.success is False
    assert "Usage:" in bare.markdown


def test_parent_handler_still_backstops_a_restricted_sub_for_the_agent(
    manager: HookManager,
) -> None:
    """The second gate of the ``/hooks <sub>`` bypass fix.

    Alias expansion means a restricted subcommand resolves to its own
    definition today, so nothing should reach the parent. This asserts the
    fail-closed backstop that catches it if something ever does again.
    """
    from nymeria.core.command_params import BoundArgs
    from nymeria.core.command_service import _CommandExecutor

    executor = _CommandExecutor(
        api=None, thread_id="thread-1", user_id="alice", actor="agent"
    )
    denied = run(
        executor._cmd_hook(BoundArgs(values={"subcommand": "disable", "id": "abc"}))
    )
    assert "not available to the agent" in denied
