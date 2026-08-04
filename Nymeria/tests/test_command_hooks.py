"""COMMAND_SUBMIT fire-point tests (backlog #134).

Covers the seam in ``CommandService.execute()`` (``core/command_hooks.py``)
plus the engine's command-aware matcher and veto fault policy. Follows the
``test_command_service.py`` patterns: fresh service per test, synthetic ``zz``
commands, ``run()`` from cli_fixtures. Hook stores are per-test tmp dirs via
the ``hook_env`` fixture (the manager resolver is monkeypatched, so the
production stores are never touched).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cli_fixtures import run
from nymeria.core.command_params import CommandParam
from nymeria.core.command_service import (
    CommandContext,
    CommandService,
    _CommandExecutor,
)
from nymeria.core.hooks.base import HookContext, HookEvent, PreToolOutcome
from nymeria.core.hooks.registry import HookRegistry
from nymeria.core.hooks.scratch import ScratchStore


class _FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def close(self) -> None:  # pragma: no cover - lifecycle only
        return None


def _ctx(**overrides: Any) -> CommandContext:
    base: dict[str, Any] = dict(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )
    base.update(overrides)
    return CommandContext(**base)


@pytest.fixture()
def hook_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Per-test hook + thread-config stores; resolver patched onto them."""
    from nymeria.core import command_hooks
    from nymeria.core.hook_manager import HookManager
    from nymeria.core.thread_config import ThreadConfigManager

    hm = HookManager(tmp_path / "hookdata")
    tcm = ThreadConfigManager(tmp_path / "hookdata")
    settings = SimpleNamespace(hooks_enabled=True)
    monkeypatch.setattr(command_hooks, "_managers", lambda: (hm, tcm, settings))
    return SimpleNamespace(hm=hm, tcm=tcm, settings=settings)


def _register_zz(
    service: CommandService,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "zzhooked",
    params: tuple[CommandParam, ...] | None = (),
    calls: list | None = None,
    **register_kwargs: Any,
):
    """A synthetic command whose handler records its invocations."""
    recorded = calls if calls is not None else []
    service.register(
        name, description="synthetic hook target", category="Test",
        params=params, **register_kwargs,
    )

    if params is None:
        async def _handler(self, args, rest):  # noqa: ANN001, ANN202
            recorded.append((list(args), rest))
            return "zz ran"
    else:
        async def _handler(self, bound):  # noqa: ANN001, ANN202
            recorded.append(dict(bound.values))
            return "zz ran"

    monkeypatch.setattr(
        _CommandExecutor, "_cmd_" + name.replace(" ", "_"), _handler, raising=False
    )
    return recorded


def _add_deny(hook_env, *, matcher: str | None, reason: str = "no zz for you", **kw):
    return hook_env.hm.add_hook(
        "alice",
        name="deny hook",
        event="command_submit",
        action="block_if_matches",
        params={"conditions": [], "reason": reason},
        matcher=matcher,
        scope="global",
        **kw,
    )


# --------------------------------------------------------------------------- #
# Deny plane
# --------------------------------------------------------------------------- #

def test_deny_blocks_before_handler(hook_env, monkeypatch) -> None:
    service = CommandService()
    calls = _register_zz(service, monkeypatch)
    _add_deny(hook_env, matcher="zzhooked")

    result = run(service.execute(_ctx(), "/zzhooked", api=_FakeApi()))

    assert result.success is False
    assert result.level == "error"
    assert "was blocked by a lifecycle hook" in result.markdown
    assert "no zz for you" in result.markdown
    assert calls == []  # the handler never ran


def test_access_gates_run_before_hooks(hook_env, monkeypatch) -> None:
    """A permissive-or-deny hook can never preempt (or reopen) a closed gate."""
    service = CommandService()
    calls = _register_zz(service, monkeypatch, agent_allowed=False)
    _add_deny(hook_env, matcher="zzhooked", reason="hook copy must not appear")

    agent = _ctx(actor="agent", surface="agent")
    result = run(service.execute(agent, "/zzhooked", api=_FakeApi()))

    assert result.success is False
    assert "not available to the agent" in result.markdown
    assert "hook copy must not appear" not in result.markdown
    assert calls == []


def test_alias_spelling_cannot_dodge_a_canonical_matcher(hook_env, monkeypatch) -> None:
    service = CommandService()
    calls = _register_zz(service, monkeypatch, aliases=("zzalias",))
    _add_deny(hook_env, matcher="zzhooked")

    result = run(service.execute(_ctx(), "/zzalias", api=_FakeApi()))

    assert result.success is False
    assert "was blocked by a lifecycle hook" in result.markdown
    assert calls == []


def test_family_prefix_matcher_covers_root_and_subcommands(hook_env, monkeypatch) -> None:
    service = CommandService()
    fam_calls = _register_zz(service, monkeypatch, name="zzfam")
    sub_calls = _register_zz(service, monkeypatch, name="zzfam sub")
    other_calls = _register_zz(service, monkeypatch, name="zzother")
    _add_deny(hook_env, matcher="zzfam *")

    api = _FakeApi()
    assert run(service.execute(_ctx(), "/zzfam", api=api)).success is False
    assert run(service.execute(_ctx(), "/zzfam sub", api=api)).success is False
    assert run(service.execute(_ctx(), "/zzother", api=api)).success is True
    assert fam_calls == [] and sub_calls == []
    assert len(other_calls) == 1


def test_matcher_accepts_the_hyphenated_display_spelling(hook_env, monkeypatch) -> None:
    """"zzfam-sub" (the display form of path ("zzfam", "sub")) must match."""
    service = CommandService()
    calls = _register_zz(service, monkeypatch, name="zzfam sub")
    _add_deny(hook_env, matcher="zzfam-sub")

    result = run(service.execute(_ctx(), "/zzfam sub", api=_FakeApi()))

    assert result.success is False
    assert calls == []


def test_deny_beats_the_missing_required_form_rescue(hook_env, monkeypatch) -> None:
    service = CommandService()
    calls = _register_zz(
        service, monkeypatch,
        params=(CommandParam("mode", required=True, choices=("fast", "slow")),),
    )
    _add_deny(hook_env, matcher="zzhooked")

    result = run(
        service.execute(_ctx(supports_forms=True), "/zzhooked", api=_FakeApi())
    )

    assert result.success is False
    assert "was blocked by a lifecycle hook" in result.markdown
    assert not (result.data or {}).get("form")
    assert calls == []


# --------------------------------------------------------------------------- #
# Rewrite plane
# --------------------------------------------------------------------------- #

def _add_rewrite(hook_env, new_rest: str):
    return hook_env.hm.add_hook(
        "alice",
        name="rewrite hook",
        event="command_submit",
        action="rewrite_arg",
        params={"conditions": [], "updates": {"rest": new_rest}},
        matcher="zzhooked",
        scope="global",
    )


def test_rewrite_feeds_the_binder_and_notes_the_user(hook_env, monkeypatch) -> None:
    service = CommandService()
    calls = _register_zz(
        service, monkeypatch,
        params=(CommandParam("mode", required=True, choices=("fast", "slow")),),
    )
    _add_rewrite(hook_env, "slow")

    result = run(service.execute(_ctx(), "/zzhooked fast", api=_FakeApi()))

    assert result.success is True
    assert calls == [{"mode": "slow"}]  # the handler saw the rewritten value
    assert "Hook adjusted arguments: slow" in result.markdown


def test_invalid_rewrite_fails_as_a_usage_error(hook_env, monkeypatch) -> None:
    service = CommandService()
    calls = _register_zz(
        service, monkeypatch,
        params=(CommandParam("mode", required=True, choices=("fast", "slow")),),
    )
    _add_rewrite(hook_env, "bogus")

    result = run(service.execute(_ctx(), "/zzhooked fast", api=_FakeApi()))

    assert result.success is False
    assert "Usage:" in result.markdown
    assert calls == []  # a bad rewrite must not reach the handler
    # The bind-error exit still explains WHY the args changed (note attached).
    assert "Hook adjusted arguments: bogus" in result.markdown


def test_rewrite_note_is_suppressed_for_the_agent_actor(hook_env, monkeypatch) -> None:
    service = CommandService()
    calls = _register_zz(
        service, monkeypatch,
        params=(CommandParam("mode", required=True, choices=("fast", "slow")),),
        agent_allowed=True,
    )
    _add_rewrite(hook_env, "slow")

    agent = _ctx(actor="agent", surface="agent")
    result = run(service.execute(agent, "/zzhooked fast", api=_FakeApi()))

    assert result.success is True
    assert calls == [{"mode": "slow"}]  # the rewrite still applies
    assert "Hook adjusted" not in result.markdown  # notes never steer the model


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #

def test_no_echo_command_fires_hooks_with_redacted_args(hook_env, monkeypatch) -> None:
    """A condition on the raw args can never see a secret value."""
    service = CommandService()
    calls = _register_zz(
        service, monkeypatch,
        params=(CommandParam("secret", required=True, no_echo=True),),
    )
    hook_env.hm.add_hook(
        "alice",
        name="secret sniffer",
        event="command_submit",
        action="block_if_matches",
        params={
            "conditions": [
                {"field": "rest", "operator": "contains", "value": "hunter2"}
            ],
            "reason": "saw the secret",
        },
        matcher="zzhooked",
        scope="global",
    )

    result = run(service.execute(_ctx(), "/zzhooked hunter2", api=_FakeApi()))

    # The condition matched nothing (redacted payload), so the command ran
    # with its REAL argument intact. The two asserts are the teeth: the deny
    # not firing proves the hook never saw the value.
    assert result.success is True, result.markdown
    assert calls == [{"secret": "hunter2"}]


def test_rewrite_is_ignored_for_secret_bearing_commands(hook_env, monkeypatch) -> None:
    service = CommandService()
    calls = _register_zz(
        service, monkeypatch,
        params=(CommandParam("secret", required=True, no_echo=True),),
    )
    _add_rewrite(hook_env, "replaced")

    result = run(service.execute(_ctx(), "/zzhooked hunter2", api=_FakeApi()))

    assert result.success is True
    assert calls == [{"secret": "hunter2"}]  # the original argument survived
    assert "Hook rewrite ignored" in result.markdown


def test_rewrite_is_ignored_for_schema_less_commands(hook_env, monkeypatch) -> None:
    """A ``params is None`` command has no binder; an unvalidated substitution
    would be a silent wrong execution, so the rewrite is refused with a note."""
    service = CommandService()
    calls = _register_zz(service, monkeypatch, params=None)
    _add_rewrite(hook_env, "replaced words")

    result = run(service.execute(_ctx(), "/zzhooked original text", api=_FakeApi()))

    assert result.success is True
    assert calls == [(["original", "text"], "original text")]
    assert "Hook rewrite ignored" in result.markdown


def test_rescue_form_exit_carries_hook_notes(hook_env, monkeypatch) -> None:
    """A rewrite that empties the args still explains itself when the #110
    rescue picker fires, or the user faces an unexplained picker loop."""
    service = CommandService()
    _register_zz(
        service, monkeypatch,
        params=(CommandParam("mode", required=True, choices=("fast", "slow")),),
    )
    _add_rewrite(hook_env, "")

    result = run(
        service.execute(_ctx(supports_forms=True), "/zzhooked fast", api=_FakeApi())
    )

    assert result.success is True
    assert (result.data or {}).get("form")  # the picker rendered
    assert "Hook adjusted arguments" in result.markdown


def test_secret_bearing_catalog_commands_are_redacted() -> None:
    """The redaction predicate over the REAL catalog: the credential-writing
    commands are covered, and every schema-less command is classified."""
    from nymeria.core.command_hooks import _SECRET_RAIL_IDS, _is_redacted

    service = CommandService()
    for command_id in (
        "provider.setup", "provider.cliproxy", "provider.set",
        "env.set", "settings.set",
    ):
        definition = service._commands[command_id]
        assert _is_redacted(definition), f"{command_id} must fire hooks redacted"

    # Ratchet: a NEW params-less executable command must be classified here
    # (secret rail or reviewed non-secret), or hooks would see its raw args.
    reviewed_non_secret = {
        "stop", "clear", "restart.api", "help",
        "notepad.write", "todos.add",
        "fallback.approve", "fallback.deny",
    }
    unclassified = sorted(
        cmd.id
        for cmd in service._commands.values()
        if cmd.executable and cmd.params is None
        and cmd.id not in _SECRET_RAIL_IDS
        and cmd.id not in reviewed_non_secret
    )
    assert unclassified == [], (
        "params-less commands not classified for command-hook redaction: "
        f"{unclassified}"
    )


# --------------------------------------------------------------------------- #
# Scope and gating
# --------------------------------------------------------------------------- #

def test_thread_scoped_hook_is_inert_without_a_thread(hook_env, monkeypatch) -> None:
    service = CommandService()
    calls = _register_zz(service, monkeypatch)
    hook_env.hm.add_hook(
        "alice",
        name="thread deny",
        event="command_submit",
        action="block_if_matches",
        params={"conditions": [], "reason": "thread hook"},
        matcher="zzhooked",
        scope="thread",
        thread_id="thread-1",
    )

    no_thread = run(
        service.execute(_ctx(thread_id=None), "/zzhooked", api=_FakeApi())
    )
    assert no_thread.success is True  # only global-scope hooks fire threadless
    assert len(calls) == 1

    bound_thread = run(service.execute(_ctx(), "/zzhooked", api=_FakeApi()))
    assert bound_thread.success is False
    assert "thread hook" in bound_thread.markdown


def test_master_switch_disables_command_hooks(hook_env, monkeypatch) -> None:
    service = CommandService()
    calls = _register_zz(service, monkeypatch)
    _add_deny(hook_env, matcher="zzhooked")
    hook_env.settings.hooks_enabled = False

    result = run(service.execute(_ctx(), "/zzhooked", api=_FakeApi()))

    assert result.success is True
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Observe plane
# --------------------------------------------------------------------------- #

def test_notify_hook_schedules_an_observe_dispatch(hook_env, monkeypatch) -> None:
    service = CommandService()
    calls = _register_zz(service, monkeypatch)
    hook_env.hm.add_hook(
        "alice",
        name="submission notify",
        event="command_submit",
        action="notify",
        text="ran {command}",
        matcher="zzhooked",
        scope="global",
    )
    scheduled: list[tuple[HookEvent, str]] = []
    # The hooks package re-exports the dispatch FUNCTION under this name,
    # shadowing the module on attribute-style imports; importlib returns the
    # real module (the one the seam's call-time from-import reads).
    import importlib

    dispatch_mod = importlib.import_module("nymeria.core.hooks.dispatch")

    monkeypatch.setattr(
        dispatch_mod,
        "schedule_observe",
        lambda event, ctx, **kw: scheduled.append((event, ctx.command or "")),
    )

    result = run(service.execute(_ctx(), "/zzhooked", api=_FakeApi()))

    assert result.success is True
    assert len(calls) == 1  # observe never blocks the dispatch
    assert scheduled == [(HookEvent.COMMAND_SUBMIT, "zzhooked")]


def test_observe_still_fires_when_a_mutate_hook_denies(hook_env, monkeypatch) -> None:
    """Documented semantics: observe means "command was submitted", so a deny
    does not suppress it."""
    service = CommandService()
    calls = _register_zz(service, monkeypatch)
    _add_deny(hook_env, matcher="zzhooked")
    hook_env.hm.add_hook(
        "alice",
        name="submission notify",
        event="command_submit",
        action="notify",
        text="ran {command}",
        matcher="zzhooked",
        scope="global",
    )
    scheduled: list[HookEvent] = []
    import importlib

    dispatch_mod = importlib.import_module("nymeria.core.hooks.dispatch")
    monkeypatch.setattr(
        dispatch_mod,
        "schedule_observe",
        lambda event, ctx, **kw: scheduled.append(event),
    )

    result = run(service.execute(_ctx(), "/zzhooked", api=_FakeApi()))

    assert result.success is False  # the deny stood
    assert calls == []
    assert scheduled == [HookEvent.COMMAND_SUBMIT]


# --------------------------------------------------------------------------- #
# require_approval on command_submit (action-level; the hold machinery is the
# tool plane's, already covered there)
# --------------------------------------------------------------------------- #

def _approval_ctx(actor: str = "user") -> HookContext:
    return HookContext(
        event=HookEvent.COMMAND_SUBMIT,
        thread_id="t1",
        user_id="u1",
        is_autonomous=False,
        command="zzhooked",
        command_display="zzhooked",
        command_actor=actor,
        tool_args={"rest": ""},
    )


def _patch_approvals(monkeypatch, resolution: dict | None):
    """Route require_approval's collaborators to a controlled future."""
    import asyncio
    import importlib

    approvals = importlib.import_module("nymeria.core.hook_approvals")
    minted: dict = {}

    def _fake_create(**kwargs):
        minted.update(kwargs)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        if resolution is not None:
            future.set_result(resolution)
        return {"record_id": "r1", **kwargs}, future

    monkeypatch.setattr(approvals, "create_pending_approval", _fake_create)
    monkeypatch.setattr(approvals, "announce_request", lambda record: None)
    monkeypatch.setattr(approvals, "delete_record", lambda record_id: None)
    monkeypatch.setattr(approvals, "publish_resolved_event", lambda *a, **kw: None)
    monkeypatch.setattr(approvals, "clamp_window", lambda value: 0.05)
    return minted


def test_require_approval_command_grant_allows_with_note(monkeypatch) -> None:
    from nymeria.core.hooks.actions import require_approval

    minted = _patch_approvals(
        monkeypatch, {"approved": True, "resolved_by": "manning"}
    )
    outcome = run(require_approval(_approval_ctx(), {"conditions": []}))

    assert isinstance(outcome, PreToolOutcome)
    assert outcome.decision == "allow"
    assert outcome.note == "approved by manning"
    # The record's subject and prompt speak command, not tool.
    assert minted["tool_name"] == "/zzhooked"
    assert minted["prompt"] == "Approve command /zzhooked?"


def test_require_approval_command_timeout_copy_is_human(monkeypatch) -> None:
    """A human-typed command's timeout denial drops the model-directed
    anti-jailbreak tail; an agent-submitted command keeps it."""
    from nymeria.core.hooks.actions import require_approval

    _patch_approvals(monkeypatch, resolution=None)  # never resolves -> timeout
    human = run(require_approval(_approval_ctx("user"), {"conditions": []}))
    assert human.decision == "deny"
    assert "command" in (human.reason or "")
    assert "Do not retry" not in (human.reason or "")
    assert "Silence is not consent" not in (human.reason or "")

    _patch_approvals(monkeypatch, resolution=None)
    agent = run(require_approval(_approval_ctx("agent"), {"conditions": []}))
    assert agent.decision == "deny"
    assert "Do not retry" in (agent.reason or "")


# --------------------------------------------------------------------------- #
# Engine: veto fault policy and matcher unit coverage
# --------------------------------------------------------------------------- #

def _cmd_ctx(command: str) -> HookContext:
    return HookContext(
        event=HookEvent.COMMAND_SUBMIT,
        thread_id="t1",
        user_id="u1",
        is_autonomous=False,
        command=command,
        tool_args={"rest": ""},
    )


def test_command_submit_dispatch_fails_closed_on_a_raising_hook() -> None:
    from nymeria.core.hooks.dispatch import dispatch

    registry = HookRegistry()

    def _boom(ctx):  # noqa: ANN001, ANN202
        raise RuntimeError("kaput")

    registry.register(HookEvent.COMMAND_SUBMIT, _boom, name="broken")
    outcome = dispatch(
        HookEvent.COMMAND_SUBMIT, _cmd_ctx("tools list"),
        registry=registry, scratch=ScratchStore(),
    )

    assert isinstance(outcome, PreToolOutcome)
    assert outcome.decision == "deny"
    assert "broken" in (outcome.reason or "")


def test_command_matcher_grammar() -> None:
    registry = HookRegistry()
    registry.register(
        HookEvent.COMMAND_SUBMIT, lambda ctx: None, matcher="tools list|provider *"
    )

    def matches(command: str) -> bool:
        return bool(registry.matching(HookEvent.COMMAND_SUBMIT, _cmd_ctx(command)))

    assert matches("tools list")
    assert matches("tools-list")        # display spelling folds
    assert matches("provider")          # family prefix includes the root
    assert matches("provider set")
    assert not matches("tools")         # exact entry does not prefix-match
    assert not matches("memory")

    # A tool-plane registration never leaks onto the command event.
    tool_reg = HookRegistry()
    tool_reg.register(HookEvent.PRE_TOOL_USE, lambda ctx: None, matcher="bash")
    assert tool_reg.matching(HookEvent.COMMAND_SUBMIT, _cmd_ctx("bash")) == []


def test_underscore_token_display_spelling_folds() -> None:
    registry = HookRegistry()
    registry.register(
        HookEvent.COMMAND_SUBMIT, lambda ctx: None, matcher="sequential-tools"
    )
    assert registry.matching(
        HookEvent.COMMAND_SUBMIT, _cmd_ctx("sequential_tools")
    )
