"""Tests for the system turn-metadata hook (backlog #66).

The built-in ``[Time:]/[Trigger:]`` block is the reserved system hook
``turn-metadata``. These tests pin the four hard invariants of the
conversion:

1. Byte-identical default output (golden strings constructed as the
   pre-change code built them).
2. Strip-regex sync: whatever the seam emits is stripped from history.
3. Fallback on a broken customization (render-time frame violation, action
   fault, unbindable planted definition) with a visible hook-log entry.
4. Toggle semantics (definition enabled flag, per-thread hook_overrides,
   fire gate), with the hooks MASTER switch deliberately not applying.
"""

from __future__ import annotations

import asyncio

import pytest

from nymeria.core import prompts
from nymeria.core.agent import NymeriaAgent
from nymeria.core.agent_history import strip_prompt_context
from nymeria.core.hook_manager import (
    SYSTEM_TURN_METADATA_ID,
    HookManager,
    TurnMetadataLogic,
    system_turn_metadata_definition,
)
from nymeria.core.thread_config import ThreadConfig

FROZEN_TIME = "Sunday, July 12, 2026 at 09:30 AM (Australia/Sydney)"


@pytest.fixture
def frozen_time(monkeypatch):
    monkeypatch.setattr(
        "nymeria.core.time_utils.format_user_time", lambda value=None: FROZEN_TIME
    )


class FakeThreadConfigManager:
    def __init__(self, configs: dict[str, ThreadConfig] | None = None):
        self.configs = configs or {}

    def get_config(self, thread_id: str) -> ThreadConfig | None:
        return self.configs.get(thread_id)


def make_agent(tmp_path, configs: dict[str, ThreadConfig] | None = None) -> NymeriaAgent:
    agent = NymeriaAgent.__new__(NymeriaAgent)
    agent.thread_config_manager = FakeThreadConfigManager(configs)  # type: ignore[assignment]
    agent.settings = None  # type: ignore[assignment]
    agent.hook_manager = HookManager(tmp_path)
    return agent


def prefix(agent, message, **kw):
    kw.setdefault("is_self_invoke", False)
    kw.setdefault("trigger_override", None)
    kw.setdefault("is_autonomous", False)
    kw.setdefault("thread_id", "t1")
    kw.setdefault("user_id", "default")
    return agent._prefix_turn_metadata(message, **kw)


# --- 1. Byte-identical default output (golden) -------------------------------


def test_default_interactive_output_is_byte_identical(frozen_time, tmp_path):
    """Golden: the exact string the pre-#66 code produced for a user turn."""
    agent = make_agent(tmp_path)
    golden = f"[Time: {FROZEN_TIME}]\n[Trigger: User Message]\n\nhello there"
    assert prefix(agent, "hello there") == golden


def test_default_autonomous_output_is_byte_identical(frozen_time, tmp_path):
    """Golden: autonomous wake-up = metadata + rules + message, same bytes."""
    agent = make_agent(tmp_path)
    golden = (
        f"[Time: {FROZEN_TIME}]\n[Trigger: Scheduled TODO]\n\n"
        f"{prompts.AUTONOMOUS_MODE_RULES.strip()}\n\nwork the todo"
    )
    out = prefix(
        agent, "work the todo",
        is_self_invoke=True, is_autonomous=True,
    )
    assert out == golden


def test_default_trigger_override_output_is_byte_identical(frozen_time, tmp_path):
    agent = make_agent(tmp_path)
    golden = (
        f"[Time: {FROZEN_TIME}]\n[Trigger: Email Trigger]\n\n"
        f"{prompts.AUTONOMOUS_MODE_RULES.strip()}\n\ncheck the inbox"
    )
    out = prefix(
        agent, "check the inbox",
        is_self_invoke=True, trigger_override="Email Trigger", is_autonomous=True,
    )
    assert out == golden


def test_engine_path_with_default_template_matches_builtin_bytes(frozen_time, tmp_path):
    """A materialized-but-unchanged template renders the exact stock bytes.

    This is the revert guarantee: customize then restore the default template
    and the wire bytes are indistinguishable from pristine.
    """
    agent = make_agent(tmp_path)
    # A name-only edit materializes the stored record without touching logic.
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID, name="Turn metadata (mine)"
    )
    stored = [
        h for h in agent.hook_manager.get_hooks_cached("default")
        if h.id == SYSTEM_TURN_METADATA_ID
    ]
    assert stored, "update_hook must materialize the system record"
    golden = f"[Time: {FROZEN_TIME}]\n[Trigger: User Message]\n\nhello there"
    assert prefix(agent, "hello there") == golden


def test_async_seam_matches_sync_output(frozen_time, tmp_path):
    agent = make_agent(tmp_path)
    sync_out = prefix(agent, "hi")

    async def _run():
        return await agent._aprefix_turn_metadata(
            "hi", is_self_invoke=False, trigger_override=None,
            is_autonomous=False, thread_id="t1", user_id="default",
        )

    assert asyncio.run(_run()) == sync_out

    # And on the customized path.
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID,
        text="[Time: {time}]\n[Trigger: via {trigger}]",
    )
    sync_custom = prefix(agent, "hi")
    assert asyncio.run(_run()) == sync_custom
    assert "[Trigger: via User Message]" in sync_custom


# --- 2. Strip-regex round trip ------------------------------------------------


def test_default_metadata_strips_from_history(frozen_time, tmp_path):
    agent = make_agent(tmp_path)
    assert strip_prompt_context(prefix(agent, "hello there")) == "hello there"


def test_custom_template_emits_at_prefix_and_strips(frozen_time, tmp_path):
    agent = make_agent(tmp_path)
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID,
        text="[Time: {time} (local)]\n[Trigger: src={trigger}]",
    )
    out = prefix(agent, "hi")
    assert out == (
        f"[Time: {FROZEN_TIME} (local)]\n[Trigger: src=User Message]\n\nhi"
    )
    assert strip_prompt_context(out) == "hi"


def test_authoring_frame_guarantees_strippable_render(frozen_time, tmp_path):
    """Any frame-valid template rendered with benign values is strippable."""
    from nymeria.core.agent_history import CONTEXT_PREFIX_PATTERN
    from nymeria.core.text_format import safe_format

    templates = [
        "[Time: {time}]\n[Trigger: {trigger}]",
        "[Time: now={time}!]\n[Trigger: {trigger} via {holder_kind}]",
        "[Time: redacted]\n[Trigger: turn]",
        "[Time: {unknown_placeholder}]\n[Trigger: x]",
    ]
    values = {"time": FROZEN_TIME, "trigger": "User Message", "holder_kind": "user"}
    for template in templates:
        assert prompts.TURN_METADATA_TEMPLATE_PATTERN.fullmatch(template), template
        rendered = safe_format(template, values)
        assert CONTEXT_PREFIX_PATTERN.fullmatch(rendered + "\n\n"), template


# --- 3. Fallback on broken customization --------------------------------------


def _executions(agent):
    agent.hook_manager.flush_execution_log("default")
    return agent.hook_manager.get_executions("default", limit=50)


def test_render_time_frame_violation_falls_back_with_log(frozen_time, tmp_path):
    """A template whose placeholder expands to ']' cannot leak: built-in wins."""
    agent = make_agent(tmp_path)
    # Frame-valid at authoring, but {prompt} is attacker/content-controlled.
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID,
        text="[Time: {time}]\n[Trigger: {prompt}]",
    )
    out = prefix(agent, "evil ] breakout")
    golden = f"[Time: {FROZEN_TIME}]\n[Trigger: User Message]\n\nevil ] breakout"
    assert out == golden
    details = [e.get("detail", "") for e in _executions(agent)]
    assert any("history-strip frame" in d for d in details)


def test_action_fault_falls_back_with_log(frozen_time, tmp_path, monkeypatch):
    agent = make_agent(tmp_path)
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID,
        text="[Time: {time}]\n[Trigger: custom]",
    )

    from nymeria.core.hooks.actions import ACTIONS

    def _boom(ctx, params):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(ACTIONS, "turn_metadata", _boom)
    out = prefix(agent, "hello there")
    golden = f"[Time: {FROZEN_TIME}]\n[Trigger: User Message]\n\nhello there"
    assert out == golden
    entries = _executions(agent)
    assert any(
        e.get("status") == "error"
        and "fell back to the built-in turn metadata" in e.get("detail", "")
        for e in entries
    )


def test_unbindable_planted_definition_falls_back_with_log(frozen_time, tmp_path):
    """A store-planted record the bridge cannot bind still yields metadata."""
    agent = make_agent(tmp_path)
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID, name="planted"
    )
    # Simulate a raw on-disk edit to an action the engine does not know.
    path = agent.hook_manager._path_for("default")
    raw = path.read_text(encoding="utf-8").replace('"turn_metadata"', '"no_such_action"')
    path.write_text(raw, encoding="utf-8")
    out = prefix(agent, "hello there")
    # The record now fails HookLogic validation entirely, so the whole store
    # quarantines and the load reads pristine: built-in bytes either way.
    golden = f"[Time: {FROZEN_TIME}]\n[Trigger: User Message]\n\nhello there"
    assert out == golden


def test_dispatch_status_none_is_conservative_builtin(frozen_time, tmp_path, monkeypatch):
    """If nothing gets recorded at all, the seam keeps the built-in block."""
    agent = make_agent(tmp_path)
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID, text="[Time: {time}]\n[Trigger: x]"
    )
    import nymeria.core.agent_turn_metadata as seam

    # Bypass the real prepare (empty status slot) and stub the dispatch to
    # return nothing: with no status recorded at all, the seam must keep the
    # built-in block rather than dropping metadata.
    monkeypatch.setattr(seam, "_prepare_dispatch", lambda *a, **k: (object(), {}))
    from nymeria.core import hooks as hooks_pkg

    monkeypatch.setattr(hooks_pkg, "dispatch", lambda *a, **k: None)
    out = prefix(agent, "msg")
    assert out == f"[Time: {FROZEN_TIME}]\n[Trigger: User Message]\n\nmsg"


# --- 4. Toggle + gate semantics -------------------------------------------------


def test_disabled_definition_omits_metadata(frozen_time, tmp_path):
    agent = make_agent(tmp_path)
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID, enabled=False
    )
    assert prefix(agent, "hi") == "hi"
    # Autonomous turns keep the hardcoded rules block.
    out = prefix(agent, "wake", is_self_invoke=True, is_autonomous=True)
    assert out == f"{prompts.AUTONOMOUS_MODE_RULES.strip()}\n\nwake"


def test_thread_override_disables_pristine_metadata(frozen_time, tmp_path):
    """The per-thread toggle works WITHOUT materializing a stored record."""
    config = ThreadConfig(
        thread_id="t1", hook_overrides={SYSTEM_TURN_METADATA_ID: False}
    )
    agent = make_agent(tmp_path, {"t1": config})
    assert prefix(agent, "hi") == "hi"
    # Other threads keep the block.
    assert prefix(agent, "hi", thread_id="t2").startswith("[Time: ")


def test_thread_override_on_beats_disabled_definition(frozen_time, tmp_path):
    config = ThreadConfig(
        thread_id="t1", hook_overrides={SYSTEM_TURN_METADATA_ID: True}
    )
    agent = make_agent(tmp_path, {"t1": config})
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID, enabled=False
    )
    assert prefix(agent, "hi").startswith("[Time: ")
    assert prefix(agent, "hi", thread_id="t2") == "hi"


def test_hooks_master_switch_does_not_strip_metadata(frozen_time, tmp_path):
    """The master kill switch governs user hooks, never the system block."""
    config = ThreadConfig(thread_id="t1", hooks_enabled=False)
    agent = make_agent(tmp_path, {"t1": config})
    assert prefix(agent, "hi").startswith("[Time: ")
    # Same with a customized template.
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID, text="[Time: {time}]\n[Trigger: mine]"
    )
    assert prefix(agent, "hi").startswith("[Time: ")
    assert "[Trigger: mine]" in prefix(agent, "hi")


def test_fire_conditions_gate_omits_deliberately(frozen_time, tmp_path):
    """An unfired gate is a deliberate omission, not a fallback."""
    from nymeria.core.conditions import HookCondition

    agent = make_agent(tmp_path)
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID,
        fire_conditions=[
            HookCondition(field="is_autonomous", operator="equals", value="True")
        ],
    )
    # Interactive turn: gate does not match -> no metadata.
    assert prefix(agent, "hi") == "hi"
    # Autonomous turn: gate matches -> metadata present.
    out = prefix(agent, "wake", is_self_invoke=True, is_autonomous=True)
    assert out.startswith(f"[Time: {FROZEN_TIME}]\n[Trigger: Scheduled TODO]")


# --- Manager semantics -----------------------------------------------------------


def test_get_hooks_includes_virtual_system_definition(tmp_path):
    manager = HookManager(tmp_path)
    hooks = manager.get_hooks("default")
    assert any(h.id == SYSTEM_TURN_METADATA_ID for h in hooks)
    hook = manager.get_hook("default", SYSTEM_TURN_METADATA_ID)
    assert hook is not None
    assert hook.logic.action == "turn_metadata"
    assert hook.created_by == "system"
    assert hook.scope == "global"
    # Virtual: nothing on disk, and the raw per-turn view stays empty.
    assert manager.get_hooks_cached("default") == []


def test_update_materializes_copy_on_write(tmp_path):
    manager = HookManager(tmp_path)
    assert manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID, text="[Time: {time}]\n[Trigger: z]"
    )
    stored = manager.get_hooks_cached("default")
    assert [h.id for h in stored] == [SYSTEM_TURN_METADATA_ID]
    assert stored[0].logic.text == "[Time: {time}]\n[Trigger: z]"  # type: ignore[missing-attribute]
    # No duplicate in the authoring view.
    ids = [h.id for h in manager.get_hooks("default")]
    assert ids.count(SYSTEM_TURN_METADATA_ID) == 1


def test_rejected_update_leaves_hook_virtual(tmp_path):
    manager = HookManager(tmp_path)
    with pytest.raises(ValueError):
        manager.update_hook(
            "default", SYSTEM_TURN_METADATA_ID, text="not a valid frame"
        )
    assert manager.get_hooks_cached("default") == []


def test_delete_resets_system_hook(tmp_path):
    manager = HookManager(tmp_path)
    assert manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID, text="[Time: {time}]\n[Trigger: z]"
    )
    assert manager.delete_hook("default", SYSTEM_TURN_METADATA_ID) is True
    assert manager.get_hooks_cached("default") == []
    # The virtual default is back.
    hook = manager.get_hook("default", SYSTEM_TURN_METADATA_ID)
    assert hook is not None
    assert hook.logic.text == prompts.DEFAULT_TURN_METADATA_TEMPLATE  # type: ignore[missing-attribute]
    # Pristine delete: nothing stored to remove.
    assert manager.delete_hook("default", SYSTEM_TURN_METADATA_ID) is False


def test_add_hook_rejects_system_action(tmp_path):
    manager = HookManager(tmp_path)
    with pytest.raises(ValueError, match="reserved"):
        manager.add_hook(
            "default", name="fake", event="prompt_submit", action="turn_metadata"
        )


def test_update_rejects_switch_to_system_action(tmp_path):
    manager = HookManager(tmp_path)
    hook = manager.add_hook(
        "default", name="mine", event="prompt_submit",
        action="inject_context", text="hello",
    )
    assert hook is not None
    with pytest.raises(ValueError, match="reserved"):
        manager.update_hook("default", hook.id, action="turn_metadata")


def test_update_rejects_locked_fields_on_system_hook(tmp_path):
    manager = HookManager(tmp_path)
    for field, value in (
        ("event", "done"),
        ("scope", "thread"),
        ("thread_id", "t1"),
        ("single_use", True),
    ):
        with pytest.raises(ValueError, match="cannot be changed"):
            manager.update_hook("default", SYSTEM_TURN_METADATA_ID, **{field: value})
    with pytest.raises(ValueError, match="cannot switch action"):
        manager.update_hook(
            "default", SYSTEM_TURN_METADATA_ID, action="inject_context"
        )


def test_template_frame_validation():
    valid = [
        "[Time: {time}]\n[Trigger: {trigger}]",
        "[Time: static]\n[Trigger: also static]",
    ]
    for template in valid:
        assert TurnMetadataLogic(text=template).text == template
    invalid = [
        "no frame at all",
        "[Time:]\n[Trigger: x]",  # empty interior (strip regex needs 1+ chars)
        "[Time: a]\n[Trigger: b]\n[Extra: c]",  # third line
        "[Time: a]b\n[Trigger: c]",  # trailing junk on line one
        "[Time: a]]\n[Trigger: b]",  # ] inside
        "[Trigger: b]\n[Time: a]",  # wrong order
    ]
    for template in invalid:
        with pytest.raises(Exception):
            TurnMetadataLogic(text=template)


def test_system_definition_validates_and_defaults():
    d = system_turn_metadata_definition()
    assert d.id == SYSTEM_TURN_METADATA_ID
    assert d.event == "prompt_submit"
    assert d.enabled is True
    assert d.logic.text == prompts.DEFAULT_TURN_METADATA_TEMPLATE  # type: ignore[missing-attribute]


def test_registry_for_turn_excludes_system_definition(frozen_time, tmp_path):
    """A stored override never double-dispatches through the general seam."""
    agent = make_agent(tmp_path)
    assert agent.hook_manager.update_hook(
        "default", SYSTEM_TURN_METADATA_ID, text="[Time: {time}]\n[Trigger: z]"
    )
    assert agent._hook_registry_for_turn("t1", "default") is None
    # A normal user hook still resolves alongside the system record.
    agent.hook_manager.add_hook(
        "default", name="mine", event="prompt_submit",
        action="inject_context", text="ctx", scope="global",
    )
    registry = agent._hook_registry_for_turn("t1", "default")
    assert registry is not None
    from nymeria.core.hooks import HookEvent

    names = [r.name for r in registry.matching(HookEvent.PROMPT_SUBMIT, _any_ctx())]
    assert names == ["mine"]


def _any_ctx():
    from nymeria.core.hooks import HookContext, HookEvent

    return HookContext(
        event=HookEvent.PROMPT_SUBMIT, thread_id="t1", user_id="default",
        is_autonomous=False,
    )


# --- Spec lockstep additions -----------------------------------------------------


def test_system_action_taxonomy_lockstep():
    from nymeria.core.hook_manager import HOOK_LOGIC_BY_ACTION, SYSTEM_ACTIONS
    from nymeria.core.hook_spec import ACTION_SPECS, event_actions, system_actions
    from nymeria.core.hooks import ACTIONS

    assert set(system_actions()) == {"turn_metadata"}
    assert SYSTEM_ACTIONS == {"turn_metadata"}
    # Present in the engine + store tables, absent from authoring legality.
    assert "turn_metadata" in ACTIONS
    assert "turn_metadata" in HOOK_LOGIC_BY_ACTION
    for actions in event_actions().values():
        assert "turn_metadata" not in actions
    assert ACTION_SPECS["turn_metadata"].system is True
    assert ACTION_SPECS["turn_metadata"].events == ("prompt_submit",)
    assert ACTION_SPECS["turn_metadata"].plane == "mutate"
