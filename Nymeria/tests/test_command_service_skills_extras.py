"""Tests for the skills search/install/enable/disable/inspect backend commands.

These were added in Phase 2a of the CLI thin-client refactor so that the
backend owns the full /skills surface instead of duplicating it in the CLI
command layer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli_fixtures import run
import nymeria.core.agent as agent_module
from nymeria.core.command_service import (
    CommandContext,
    CommandService,
)


class _FakeMarketplaceEntry:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description


class _FakeMarketplaceFetcher:
    def __init__(self, entries: list[_FakeMarketplaceEntry], installed_skill: Any) -> None:
        self.entries = entries
        self.installed_skill = installed_skill
        self.fetch_calls: list[tuple[str, Path]] = []

    def list(self, query: str | None = None):
        if query is None:
            return list(self.entries)
        return [entry for entry in self.entries if query.lower() in entry.name.lower()]

    def fetch(self, name: str, target_dir: Path):
        self.fetch_calls.append((name, target_dir))
        return self.installed_skill


class _FakeSkillKitSkill:
    """Stand-in for nymeria.skills.Skill with the methods _cmd_skills_inspect needs."""

    def __init__(
        self,
        name: str,
        *,
        scope: str = "user",
        description: str = "",
        required_tools: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        path: Path | None = None,
    ) -> None:
        self.name = name
        self.scope = scope
        self.description = description
        self.required_tools = required_tools or []
        self.allowed_tools = allowed_tools or []
        self.path = path or Path("/tmp") / name
        self.is_skill_kit = bool(required_tools)

    def list_scripts(self) -> list[str]:
        return []

    def list_references(self) -> list[str]:
        return []


class _FakeSkillManager:
    def __init__(self) -> None:
        self.reloaded = 0
        self.skill: _FakeSkillKitSkill | None = None

    def target_dir(self, scope: str, user_id: str | None = None) -> Path:
        base = Path("/tmp/nymeria-skills") / scope
        if user_id:
            base = base / user_id
        return base

    def reload(self) -> None:
        self.reloaded += 1

    def get(self, name: str, user_id: str | None = None):
        if self.skill is not None and self.skill.name == name:
            return self.skill
        return None

    def list_installed(self, user_id: str | None = None) -> list[Any]:
        return [self.skill] if self.skill else []


class _FakeProfile:
    def __init__(self, enabled_global_skills: list[str] | None = None) -> None:
        self.enabled_global_skills = list(enabled_global_skills or [])


class _FakeProfileManager:
    def __init__(self) -> None:
        self.profile = _FakeProfile()
        self.saved: list[_FakeProfile] = []

    def get_profile(self, user_id: str) -> _FakeProfile:
        return self.profile

    def save_profile(self, profile: _FakeProfile) -> None:
        self.saved.append(profile)


class _SkillCommandFakeApi:
    def __init__(self, thread_config: dict[str, Any] | None = None) -> None:
        self.thread_config = thread_config or {"enabled_skills": [], "disabled_skills": []}
        self.updates: list[dict[str, Any]] = []

    async def close(self) -> None:
        return None

    async def get_thread_config(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        return dict(self.thread_config)

    async def update_thread_config(self, thread_id: str, **kwargs) -> None:
        self.updates.append({"thread_id": thread_id, **kwargs})


def _make_agent(
    skill_manager: _FakeSkillManager | None = None,
    profile_manager: _FakeProfileManager | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        skill_manager=skill_manager,
        profile_manager=profile_manager,
        thread_config_manager=None,
    )


@pytest.fixture
def patched_marketplace(monkeypatch: pytest.MonkeyPatch):
    """Replace ``skills.marketplace.get_fetcher`` with a controllable fake."""

    fetchers: dict[str, _FakeMarketplaceFetcher] = {}

    def install_for(source: str, fetcher: _FakeMarketplaceFetcher) -> None:
        fetchers[source] = fetcher

    def fake_get_fetcher(source: str) -> _FakeMarketplaceFetcher:
        if source not in fetchers:
            raise NotImplementedError(f"unknown source: {source}")
        return fetchers[source]

    monkeypatch.setattr(
        "nymeria.skills.marketplace.get_fetcher",
        fake_get_fetcher,
    )

    return install_for


def test_skills_search_lists_marketplace_entries(
    monkeypatch: pytest.MonkeyPatch,
    patched_marketplace,
) -> None:
    """`/skills search` proxies through marketplace fetcher and renders names."""
    fetcher = _FakeMarketplaceFetcher(
        entries=[
            _FakeMarketplaceEntry("skill-creator", "Create and package skills."),
            _FakeMarketplaceEntry("artifact-builder", "Build artifacts."),
        ],
        installed_skill=None,
    )
    patched_marketplace("anthropic", fetcher)

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/skills search creator",
            api=_SkillCommandFakeApi(),
        )
    )

    assert result.success is True
    assert "skill-creator" in result.markdown
    assert "artifact-builder" not in result.markdown


def test_skills_search_reports_no_matches(
    monkeypatch: pytest.MonkeyPatch,
    patched_marketplace,
) -> None:
    fetcher = _FakeMarketplaceFetcher(entries=[], installed_skill=None)
    patched_marketplace("anthropic", fetcher)

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                actor="user",
                surface="cli",
            ),
            "/skills search",
            api=_SkillCommandFakeApi(),
        )
    )

    assert result.success is True
    assert "No marketplace skills matched" in result.markdown


def test_skills_install_calls_marketplace_and_reloads_manager(
    monkeypatch: pytest.MonkeyPatch,
    patched_marketplace,
) -> None:
    installed = _FakeSkillKitSkill("skill-creator")
    fetcher = _FakeMarketplaceFetcher(entries=[], installed_skill=installed)
    patched_marketplace("anthropic", fetcher)

    skill_manager = _FakeSkillManager()
    agent = _make_agent(skill_manager=skill_manager)
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: agent)

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/skills install skill-creator --scope user",
            api=_SkillCommandFakeApi(),
        )
    )

    assert result.success is True, result.markdown
    assert "skill-creator" in result.markdown
    assert fetcher.fetch_calls == [("skill-creator", skill_manager.target_dir("user", "alice"))]
    assert skill_manager.reloaded == 1


def test_skills_enable_thread_scope_updates_thread_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(skill_manager=_FakeSkillManager())
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: agent)

    api = _SkillCommandFakeApi(
        thread_config={"enabled_skills": [], "disabled_skills": ["skill-creator"]}
    )

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/skills enable skill-creator",
            api=api,
        )
    )

    assert result.success is True, result.markdown
    assert api.updates == [
        {
            "thread_id": "thread-1",
            "user_id": "alice",
            "enabled_skills": ["skill-creator"],
            "disabled_skills": [],
        }
    ]


def test_skills_disable_global_scope_writes_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_manager = _FakeProfileManager()
    profile_manager.profile.enabled_global_skills = ["skill-creator", "other"]
    agent = _make_agent(
        skill_manager=_FakeSkillManager(),
        profile_manager=profile_manager,
    )
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: agent)

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/skills disable --global skill-creator",
            api=_SkillCommandFakeApi(),
        )
    )

    assert result.success is True, result.markdown
    assert profile_manager.profile.enabled_global_skills == ["other"]
    assert profile_manager.saved


def test_skills_inspect_formats_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_manager = _FakeSkillManager()
    skill_manager.skill = _FakeSkillKitSkill(
        "skill-creator",
        scope="user",
        description="Create and package skills.",
        required_tools=["bash_execute", "filesystem"],
        allowed_tools=["bash_execute"],
    )
    agent = _make_agent(skill_manager=skill_manager)
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: agent)

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/skills inspect skill-creator",
            api=_SkillCommandFakeApi(),
        )
    )

    assert result.success is True, result.markdown
    assert "skill-creator" in result.markdown
    assert "Required tools" in result.markdown
    assert "bash_execute" in result.markdown
    assert "Create and package skills." in result.markdown


def test_skills_inspect_reports_missing_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(skill_manager=_FakeSkillManager())
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: agent)

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                actor="user",
                surface="cli",
            ),
            "/skills inspect nonexistent",
            api=_SkillCommandFakeApi(),
        )
    )

    assert result.success is False
    assert "not found" in result.markdown


# ── #129 wave 2a: declared params ────────────────────────────────────────────


def _cli_ctx(thread_id: str = "thread-1") -> CommandContext:
    return CommandContext(
        user_id="alice",
        thread_id=thread_id,
        actor="user",
        surface="cli",
        is_admin=True,
    )


def test_skills_search_reads_a_source_given_after_the_query(
    patched_marketplace,
) -> None:
    # The query is a repeatable positional, not a raw tail, so the option keeps
    # parsing after the free words exactly as the hand parser did.
    anthropic = _FakeMarketplaceFetcher(
        entries=[_FakeMarketplaceEntry("wrong-shelf", "From the default source.")],
        installed_skill=None,
    )
    clawhub = _FakeMarketplaceFetcher(
        entries=[_FakeMarketplaceEntry("skill-creator", "From clawhub.")],
        installed_skill=None,
    )
    patched_marketplace("anthropic", anthropic)
    patched_marketplace("clawhub", clawhub)

    result = run(
        CommandService().execute(
            _cli_ctx(), "/skills search creator --source clawhub", api=_SkillCommandFakeApi()
        )
    )

    assert result.success is True, result.markdown
    assert "skill-creator" in result.markdown
    assert "(clawhub)" in result.markdown
    assert "wrong-shelf" not in result.markdown


def test_skills_search_accepts_the_equals_form_source(patched_marketplace) -> None:
    clawhub = _FakeMarketplaceFetcher(
        entries=[_FakeMarketplaceEntry("skill-creator", "From clawhub.")],
        installed_skill=None,
    )
    patched_marketplace("clawhub", clawhub)

    result = run(
        CommandService().execute(
            _cli_ctx(), "/skills search --source=clawhub", api=_SkillCommandFakeApi()
        )
    )

    assert result.success is True, result.markdown
    assert "skill-creator" in result.markdown
    assert "(clawhub)" in result.markdown


def test_skills_install_rejects_an_unknown_scope(
    monkeypatch: pytest.MonkeyPatch, patched_marketplace
) -> None:
    installed = _FakeSkillKitSkill("skill-creator")
    fetcher = _FakeMarketplaceFetcher(entries=[], installed_skill=installed)
    patched_marketplace("anthropic", fetcher)
    skill_manager = _FakeSkillManager()
    monkeypatch.setattr(
        agent_module, "get_current_agent", lambda: _make_agent(skill_manager=skill_manager)
    )

    result = run(
        CommandService().execute(
            _cli_ctx(), "/skills install skill-creator --scope everyone",
            api=_SkillCommandFakeApi(),
        )
    )

    assert result.success is False
    assert "`everyone` is not a valid --scope" in result.markdown
    assert "Valid: user, global" in result.markdown
    assert fetcher.fetch_calls == []
    assert skill_manager.reloaded == 0


def test_skills_install_rejects_a_second_name(
    monkeypatch: pytest.MonkeyPatch, patched_marketplace
) -> None:
    fetcher = _FakeMarketplaceFetcher(
        entries=[], installed_skill=_FakeSkillKitSkill("skill-creator")
    )
    patched_marketplace("anthropic", fetcher)
    monkeypatch.setattr(
        agent_module,
        "get_current_agent",
        lambda: _make_agent(skill_manager=_FakeSkillManager()),
    )

    result = run(
        CommandService().execute(
            _cli_ctx(), "/skills install skill-creator artifact-builder",
            api=_SkillCommandFakeApi(),
        )
    )

    assert result.success is False
    assert "Unexpected argument `artifact-builder`" in result.markdown
    assert fetcher.fetch_calls == []


def test_skills_enable_accepts_the_global_flag_after_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_manager = _FakeProfileManager()
    monkeypatch.setattr(
        agent_module,
        "get_current_agent",
        lambda: _make_agent(
            skill_manager=_FakeSkillManager(), profile_manager=profile_manager
        ),
    )
    api = _SkillCommandFakeApi()

    result = run(
        CommandService().execute(
            _cli_ctx(), "/skills enable skill-creator --global", api=api
        )
    )

    assert result.success is True, result.markdown
    assert profile_manager.profile.enabled_global_skills == ["skill-creator"]
    assert api.updates == []


def test_skills_inspect_rejects_extra_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_manager = _FakeSkillManager()
    skill_manager.skill = _FakeSkillKitSkill("skill-creator")
    monkeypatch.setattr(
        agent_module, "get_current_agent", lambda: _make_agent(skill_manager=skill_manager)
    )

    result = run(
        CommandService().execute(
            _cli_ctx(), "/skills inspect skill-creator verbose", api=_SkillCommandFakeApi()
        )
    )

    assert result.success is False
    assert "Unexpected argument `verbose`" in result.markdown
    assert "Required tools" not in result.markdown


def test_skills_show_requires_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    skill_manager = _FakeSkillManager()
    skill_manager.skill = _FakeSkillKitSkill("skill-creator")
    monkeypatch.setattr(
        agent_module, "get_current_agent", lambda: _make_agent(skill_manager=skill_manager)
    )

    result = run(
        CommandService().execute(_cli_ctx(), "/skills show", api=_SkillCommandFakeApi())
    )

    assert result.success is False
    assert "Missing required argument: name" in result.markdown
    assert "Usage: `/skills show <name>`" in result.markdown


def test_skills_root_guides_a_typo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_module,
        "get_current_agent",
        lambda: _make_agent(skill_manager=_FakeSkillManager()),
    )

    result = run(
        CommandService().execute(_cli_ctx(), "/skills serch", api=_SkillCommandFakeApi())
    )

    assert result.success is False
    assert "Unexpected argument `serch`" in result.markdown
    assert "Did you mean `/skills search`?" in result.markdown
    assert (
        "Valid subcommands: disable, enable, install, list, search, show."
        in result.markdown
    )
    assert "Usage: `/skills`." in result.markdown
    assert "See `/help skills`." in result.markdown
