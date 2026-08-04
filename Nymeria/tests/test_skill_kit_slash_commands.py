from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli_fixtures import run
import nymeria.core.command_service as command_service_mod
from nymeria.core.command_service import (
    CommandContext,
    CommandService,
    deactivate_skill_kit,
    prepare_skill_slash_command,
)
from nymeria.core.thread_config import (
    TemporaryToolEntry,
    ThreadConfig,
    ThreadConfigManager,
)
from nymeria.core.time_utils import utc_now
from nymeria.skills import Skill


def _skill(
    tmp_path: Path,
    name: str,
    *,
    required_tools: list[str] | None = None,
    ttl: str = "30m",
    internal: bool = False,
    body: str | None = None,
) -> Skill:
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {}
    if required_tools is not None or internal:
        nymeria: dict[str, Any] = {}
        if required_tools is not None:
            nymeria["required_tools"] = required_tools
            nymeria["tool_ttl"] = ttl
        if internal:
            nymeria["internal"] = True
        metadata["nymeria"] = nymeria
    return Skill(
        name=name,
        description=f"{name} description",
        metadata=metadata,
        body=body or f"# {name}\n\nBody for {name}.",
        path=skill_dir,
        scope="bundled",
    )


class _SkillManager:
    def __init__(self, skills: list[Skill]) -> None:
        self.skills = {skill.name: skill for skill in skills}
        self.get_calls: list[tuple[str, str | None]] = []

    def list_installed(self, user_id: str | None = None) -> list[Skill]:
        return sorted(self.skills.values(), key=lambda skill: skill.name)

    def get(self, name: str, user_id: str | None = None) -> Skill | None:
        self.get_calls.append((name, user_id))
        return self.skills.get(name)


def _agent(tmp_path: Path, skills: list[Skill]) -> SimpleNamespace:
    cfg_dir = tmp_path / "thread-config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    agent = SimpleNamespace(
        skill_manager=_SkillManager(skills),
        thread_config_manager=ThreadConfigManager(cfg_dir),
        invalidated=[],
    )
    agent.invalidate_thread_config_cache = lambda thread_id: agent.invalidated.append(thread_id)
    return agent


def _ctx() -> CommandContext:
    return CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="desktop",
        is_admin=False,
    )


def test_is_internal_defaults_false_and_reads_frontmatter(tmp_path: Path) -> None:
    public = _skill(tmp_path, "public-kit", required_tools=["tool_a"])
    internal = _skill(
        tmp_path,
        "internal-kit",
        required_tools=["tool_a"],
        internal=True,
    )

    assert public.is_internal is False
    assert internal.is_internal is True


def test_static_skill_and_kit_commands_are_registered_without_dynamic_name_commands(
    tmp_path: Path,
) -> None:
    public = _skill(tmp_path, "public-kit", required_tools=["tool_a"])
    service = CommandService()

    commands = service.list_commands(
        actor="user",
        surface="desktop",
        is_admin=False,
        user_id="alice",
        agent=_agent(tmp_path, [public]),
    )
    by_name = {cmd.name: cmd for cmd in commands}

    assert by_name["skill"].execution_kind == "chat_stream"
    assert by_name["kit"].execution_kind == "chat_stream"
    assert "public-kit" not in by_name


def test_skill_slash_activates_plain_skill_and_builds_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = _skill(tmp_path, "plain-skill", body="# Plain\n\nFollow these steps.")
    agent = _agent(tmp_path, [plain])
    calls: list[dict[str, Any]] = []

    def fake_activate(**kwargs):
        calls.append(kwargs)
        return True, "activated"

    monkeypatch.setattr(command_service_mod, "activate_skill_kit", fake_activate)

    result = prepare_skill_slash_command(
        agent=agent,
        thread_id="thread-1",
        user_id="alice",
        mode="skill",
        rest="plain-skill polish the draft",
    )

    assert result.success is True
    assert result.should_stream is True
    assert calls[0]["skill_name"] == "plain-skill"
    assert calls[0]["ttl_override"] is None
    assert "Follow these steps." in result.message
    assert "polish the draft" in result.message


def test_skill_slash_rejects_skill_kit(tmp_path: Path) -> None:
    kit = _skill(tmp_path, "work-kit", required_tools=["tool_a"])
    result = prepare_skill_slash_command(
        agent=_agent(tmp_path, [kit]),
        thread_id="thread-1",
        user_id="alice",
        mode="skill",
        rest="work-kit do it",
    )

    assert result.success is False
    assert result.should_stream is False
    assert "Use `/kit work-kit`" in result.message
    # SkillSlashResult carries the outcome in ``success``: the message is a
    # body, never a legacy "[Error]:" prefix (#132).
    assert not result.message.startswith("[")


def test_kit_slash_passes_ttl_override_and_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = _skill(tmp_path, "work-kit", required_tools=["tool_a"])
    agent = _agent(tmp_path, [kit])
    calls: list[dict[str, Any]] = []

    def fake_activate(**kwargs):
        calls.append(kwargs)
        return True, "activated"

    monkeypatch.setattr(command_service_mod, "activate_skill_kit", fake_activate)

    result = prepare_skill_slash_command(
        agent=agent,
        thread_id="thread-1",
        user_id="alice",
        mode="kit",
        rest="work-kit 1h do the thing",
    )

    assert result.success is True
    assert result.should_stream is True
    assert calls[0]["ttl_override"] == "1h"
    assert "do the thing" in result.message


def test_kit_slash_treats_non_ttl_tail_as_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = _skill(tmp_path, "work-kit", required_tools=["tool_a"])
    agent = _agent(tmp_path, [kit])
    calls: list[dict[str, Any]] = []

    def fake_activate(**kwargs):
        calls.append(kwargs)
        return True, "activated"

    monkeypatch.setattr(command_service_mod, "activate_skill_kit", fake_activate)

    result = prepare_skill_slash_command(
        agent=agent,
        thread_id="thread-1",
        user_id="alice",
        mode="kit",
        rest="work-kit garbage is a prompt",
    )

    assert result.success is True
    assert calls[0]["ttl_override"] is None
    assert "garbage is a prompt" in result.message


def test_kit_slash_rejects_plain_skill(tmp_path: Path) -> None:
    plain = _skill(tmp_path, "plain-skill")
    result = prepare_skill_slash_command(
        agent=_agent(tmp_path, [plain]),
        thread_id="thread-1",
        user_id="alice",
        mode="kit",
        rest="plain-skill do it",
    )

    assert result.success is False
    assert "Use `/skill plain-skill`" in result.message


def test_skill_or_kit_off_deactivates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = _skill(tmp_path, "work-kit", required_tools=["tool_a"])
    agent = _agent(tmp_path, [kit])
    calls: list[dict[str, Any]] = []

    def fake_deactivate(**kwargs):
        calls.append(kwargs)
        return "success", "deactivated"

    monkeypatch.setattr(command_service_mod, "deactivate_skill_kit", fake_deactivate)

    result = prepare_skill_slash_command(
        agent=agent,
        thread_id="thread-1",
        user_id="alice",
        mode="kit",
        rest="work-kit off",
    )

    assert result.success is True
    assert result.should_stream is False
    assert calls == [
        {
            "agent": agent,
            "thread_id": "thread-1",
            "user_id": "alice",
            "skill_name": "work-kit",
        }
    ]


def test_skill_slash_off_deactivates_plain_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = _skill(tmp_path, "plain-skill")
    agent = _agent(tmp_path, [plain])
    calls: list[dict[str, Any]] = []

    def fake_deactivate(**kwargs):
        calls.append(kwargs)
        return "success", "deactivated"

    monkeypatch.setattr(command_service_mod, "deactivate_skill_kit", fake_deactivate)

    result = prepare_skill_slash_command(
        agent=agent,
        thread_id="thread-1",
        user_id="alice",
        mode="skill",
        rest="plain-skill off",
    )

    assert result.success is True
    assert result.should_stream is False
    assert calls[0]["skill_name"] == "plain-skill"


def test_slash_commands_hide_internal_skills(tmp_path: Path) -> None:
    internal = _skill(
        tmp_path,
        "internal-kit",
        required_tools=["tool_a"],
        internal=True,
    )

    result = prepare_skill_slash_command(
        agent=_agent(tmp_path, [internal]),
        thread_id="thread-1",
        user_id="alice",
        mode="kit",
        rest="internal-kit do it",
    )

    assert result.success is False
    assert "not found" in result.message


def test_skill_prompt_notes_attachments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = _skill(tmp_path, "plain-skill")
    agent = _agent(tmp_path, [plain])
    monkeypatch.setattr(
        command_service_mod,
        "activate_skill_kit",
        lambda **kwargs: (True, "activated"),
    )

    result = prepare_skill_slash_command(
        agent=agent,
        thread_id="thread-1",
        user_id="alice",
        mode="skill",
        rest="plain-skill",
        has_attachments=True,
    )

    assert result.success is True
    assert "attached files/images" in result.message


def test_skills_list_reports_plain_skills_kits_and_status(tmp_path: Path) -> None:
    active_plain = _skill(tmp_path, "active-skill")
    active_kit = _skill(tmp_path, "active-kit", required_tools=["tool_a"])
    inactive_kit = _skill(tmp_path, "inactive-kit", required_tools=["tool_b"])
    internal = _skill(
        tmp_path,
        "internal-kit",
        required_tools=["tool_d"],
        internal=True,
    )
    agent = _agent(tmp_path, [active_plain, active_kit, inactive_kit, internal])
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="thread-1",
            enabled_skills=["active-skill", "active-kit", "internal-kit"],
        )
    )

    result = run(
        CommandService().execute(
            _ctx(),
            "/skills list",
            api=SimpleNamespace(agent=agent),
        )
    )

    assert result.success is True
    assert "`active-skill` - skill, active" in result.markdown
    assert "`active-kit` - kit, active" in result.markdown
    assert "`inactive-kit` - kit, inactive" in result.markdown
    assert "internal-kit" not in result.markdown


def test_skills_show_returns_body_without_activating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = _skill(
        tmp_path,
        "debug-skill",
        internal=True,
        body="# Debug Skill\n\nFull body text.",
    )
    agent = _agent(tmp_path, [skill])

    def fail_activate(**kwargs):
        pytest.fail("show must not activate the skill")

    monkeypatch.setattr(command_service_mod, "activate_skill_kit", fail_activate)

    result = run(
        CommandService().execute(
            CommandContext(user_id="alice", actor="user", surface="desktop"),
            "/skills show debug-skill",
            api=SimpleNamespace(agent=agent),
        )
    )

    assert result.success is True
    # `skills inspect` folded in here (backlog #131), so one show renders the
    # metadata table AND the body: neither old caller lost a field.
    assert "# Debug Skill" in result.markdown
    assert "Full body text." in result.markdown
    assert "Required tools" in result.markdown
    assert "Scope" in result.markdown


def test_skills_show_renders_a_bodyless_skill_without_pretending_it_has_one(
    tmp_path: Path,
) -> None:
    skill = _skill(tmp_path, "bare-skill", body="   ")
    agent = _agent(tmp_path, [skill])

    result = run(
        CommandService().execute(
            CommandContext(user_id="alice", actor="user", surface="desktop"),
            "/skills show bare-skill",
            api=SimpleNamespace(agent=agent),
        )
    )

    assert result.success is True
    assert "Name" in result.markdown
    assert "(No body.)" in result.markdown


def test_skills_off_all_deactivates_visible_active_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _skill(tmp_path, "first-skill")
    second = _skill(tmp_path, "second-kit", required_tools=["tool_b"])
    internal = _skill(
        tmp_path,
        "internal-kit",
        required_tools=["tool_c"],
        internal=True,
    )
    agent = _agent(tmp_path, [first, second, internal])
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="thread-1",
            enabled_skills=["first-skill", "second-kit", "internal-kit"],
        )
    )
    calls: list[str] = []

    def fake_deactivate(**kwargs):
        calls.append(kwargs["skill_name"])
        return "success", "deactivated"

    monkeypatch.setattr(command_service_mod, "deactivate_skill_kit", fake_deactivate)

    result = run(
        CommandService().execute(
            _ctx(),
            "/skills off all",
            api=SimpleNamespace(agent=agent),
        )
    )

    assert result.success is True
    assert calls == ["first-skill", "second-kit"]


def test_skills_disable_all_is_the_canonical_spelling_of_the_old_off_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The depth-3 `/skills off all` folded into a value of `skills disable`.

    Both spellings must reach the same body, and the thread-scoped `all` must
    refuse the global SCOPE rather than silently ignoring it. The refusal
    survived the #131 wave B scope-token migration: only its spelling moved,
    and the retired `--global` flag is now an error before the handler runs.
    """
    first = _skill(tmp_path, "first-skill")
    agent = _agent(tmp_path, [first])
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-1", enabled_skills=["first-skill"])
    )
    calls: list[str] = []

    def fake_deactivate(**kwargs):
        calls.append(kwargs["skill_name"])
        return "success", "deactivated"

    monkeypatch.setattr(command_service_mod, "deactivate_skill_kit", fake_deactivate)

    canonical = run(
        CommandService().execute(
            _ctx(), "/skills disable all", api=SimpleNamespace(agent=agent)
        )
    )
    assert canonical.success is True, canonical.markdown
    assert calls == ["first-skill"]

    refused = run(
        CommandService().execute(
            _ctx(), "/skills disable all global", api=SimpleNamespace(agent=agent)
        )
    )
    assert refused.success is False
    assert "drop the `global` scope" in refused.markdown
    assert calls == ["first-skill"]

    retired_flag = run(
        CommandService().execute(
            _ctx(), "/skills disable --global all", api=SimpleNamespace(agent=agent)
        )
    )
    assert retired_flag.success is False
    assert "Unknown option `--global`" in retired_flag.markdown
    assert calls == ["first-skill"]


def test_skills_off_all_body_carries_no_sentinel_from_the_helper(
    tmp_path: Path,
) -> None:
    # The real deactivate helper (no monkeypatch): its message is quoted
    # verbatim into each per-skill line, so a prefixed helper string used to
    # print a literal "[Success]:" mid-body under the rendered artifact.
    first = _skill(tmp_path, "first-skill")
    second = _skill(tmp_path, "second-kit", required_tools=["tool_b"])
    agent = _agent(tmp_path, [first, second])
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="thread-1",
            enabled_skills=["first-skill", "second-kit"],
        )
    )

    result = run(
        CommandService().execute(
            _ctx(),
            "/skills off all",
            api=SimpleNamespace(agent=agent),
        )
    )

    assert result.success is True
    assert result.level == "success"
    assert result.markdown == (
        "**Done.** Deactivated skills:\n"
        "- `first-skill`: Skill 'first-skill' deactivated.\n"
        "- `second-kit`: Skill kit 'second-kit' deactivated."
    )


def test_deactivate_skill_kit_threads_user_id_through_lookup(tmp_path: Path) -> None:
    kit = _skill(tmp_path, "user-kit", required_tools=["tool_a"])
    agent = _agent(tmp_path, [kit])
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="thread-1",
            enabled_skills=["user-kit"],
            temporary_tools={
                "tool_a": TemporaryToolEntry(
                    enabled_at=utc_now(),
                    expires_at=utc_now() + timedelta(hours=1),
                )
            },
        )
    )

    level, msg = deactivate_skill_kit(
        agent=agent,
        thread_id="thread-1",
        user_id="alice",
        skill_name="user-kit",
    )

    assert level == "success"
    # The (level, message) contract (#144) authors the outcome at the
    # source; the message stays a plain body with no sentinel or artifact.
    assert msg == "Skill kit 'user-kit' deactivated. Evicted tools: tool_a."
    assert agent.skill_manager.get_calls == [("user-kit", "alice")]
    tc = agent.thread_config_manager.get_config("thread-1")
    assert tc is not None
    assert "tool_a" not in tc.temporary_tools


def test_deactivate_skill_kit_noop_is_an_info_readout(tmp_path: Path) -> None:
    """#144 review catch: 'was not active' is a readout, not a completed
    action, so the helper authors ``info``: no surface may render
    ``**Done.**`` for it (the CLI glyphs a checkmark off exactly that
    artifact, which would claim a deactivation that never happened)."""
    plain = _skill(tmp_path, "plain-skill")
    agent = _agent(tmp_path, [plain])
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-1", enabled_skills=[])
    )

    level, msg = deactivate_skill_kit(
        agent=agent,
        thread_id="thread-1",
        user_id="alice",
        skill_name="plain-skill",
    )

    assert level == "info"
    assert msg == "Skill 'plain-skill' was not active."

    # The /skill <name> off relay carries the authored level out verbatim,
    # which is what the chat router renders (test_api_chat_router pins the
    # wire side).
    result = prepare_skill_slash_command(
        agent=agent,
        thread_id="thread-1",
        user_id="alice",
        mode="skill",
        rest="plain-skill off",
    )
    assert result.level == "info"
    assert result.success is True
    assert result.should_stream is False
