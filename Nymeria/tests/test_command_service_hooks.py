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


def _only(manager: HookManager):
    hooks = manager.get_hooks("alice")
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
    assert manager.get_hooks("alice") == []


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
    assert "requires a name" in result.markdown


def test_create_rejects_unknown_flag(manager: HookManager) -> None:
    # A misspelled/unknown option must error, not fold silently into the name.
    result = _run(
        '/hook create MyHook --event done --action notify --text hi --bogus junk'
    )
    assert result.success is False
    assert "unknown option" in result.markdown
    assert manager.get_hooks("alice") == []


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
    result = _run("/hook list")
    assert result.success is True
    assert "No hooks found" in result.markdown


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
    assert manager.get_hooks("alice") == []


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
    assert manager.get_hooks("alice")[0].logic.command == "echo bye"


def test_run_command_requires_command_flag(
    manager: HookManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_run_command_flag(monkeypatch, True)
    result = _run('/hook create RC --event done --action run_command')
    assert result.success is False
    assert "requires --command" in result.markdown
    assert manager.get_hooks("alice") == []


def test_run_command_denied_when_flag_off(
    manager: HookManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_run_command_flag(monkeypatch, False)
    result = _run(
        '/hook create RC --event done --action run_command --command "echo hi"'
    )
    assert result.success is False
    assert "HOOKS_RUN_COMMAND_ENABLED" in result.markdown
    assert manager.get_hooks("alice") == []


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
    assert manager.get_hooks("alice") == []


class _UserRoleAccountsRepo:
    def get_user_by_id(self, user_id: str):
        return SimpleNamespace(
            id=user_id, email=f"{user_id}@example.test", display_name=user_id, role="user"
        )
