"""Container-side adoption of the `.env.docker` init picks on first boot.

The host wizard carried the bootstrap admin's picks in two env vars (see
`tests/test_init_seed_env.py` for the wire format). These tests cover the readers,
both on `UserProfileManager.get_profile`'s lazy creation path:
`_migrate_default_global_skills` (skills) and `_migrate_default_thread_tools` (tools).
Both must apply the picks once, only for the bootstrap admin, only on a fresh profile,
and degrade to the normal defaults when the vars are absent. The tools path runs here
(not in the agent's init-time `_migrate_tool_preferences`) precisely because on a real
first boot that pass runs before the profile exists.
"""

from __future__ import annotations

from types import SimpleNamespace

from nymeria.config.init_seed_env import (
    INIT_DEFAULT_THREAD_TOOLS_ENV,
    INIT_ENABLED_GLOBAL_SKILLS_ENV,
)
from nymeria.core.accounts import BOOTSTRAP_USER_ID
from nymeria.core.user_profile import DEFAULT_GLOBAL_SKILLS, UserProfileManager


# --- skills path: _migrate_default_global_skills ----------------------------


def test_bootstrap_admin_adopts_env_skills_on_first_load(monkeypatch, tmp_path):
    monkeypatch.setenv(
        INIT_ENABLED_GLOBAL_SKILLS_ENV, "self-improve:tool-management:mcp-management"
    )
    manager = UserProfileManager(tmp_path / "data")

    profile = manager.get_profile(BOOTSTRAP_USER_ID)

    assert profile.enabled_global_skills == [
        "self-improve",
        "tool-management",
        "mcp-management",
    ]
    assert profile.global_skill_defaults_migrated is True


def test_no_env_skills_falls_back_to_default_global_skills(monkeypatch, tmp_path):
    monkeypatch.delenv(INIT_ENABLED_GLOBAL_SKILLS_ENV, raising=False)
    manager = UserProfileManager(tmp_path / "data")

    profile = manager.get_profile(BOOTSTRAP_USER_ID)

    assert profile.enabled_global_skills == DEFAULT_GLOBAL_SKILLS


def test_env_skills_only_apply_to_bootstrap_admin(monkeypatch, tmp_path):
    monkeypatch.setenv(INIT_ENABLED_GLOBAL_SKILLS_ENV, "self-improve:tool-management")
    manager = UserProfileManager(tmp_path / "data")

    # A non-bootstrap user ignores the install-wide picks and gets the defaults.
    other = manager.get_profile("alice")

    assert other.enabled_global_skills == DEFAULT_GLOBAL_SKILLS


def test_env_skills_are_one_shot_after_watermark(monkeypatch, tmp_path):
    monkeypatch.setenv(INIT_ENABLED_GLOBAL_SKILLS_ENV, "self-improve:tool-management")
    manager = UserProfileManager(tmp_path / "data")
    first = manager.get_profile(BOOTSTRAP_USER_ID)
    assert first.enabled_global_skills == ["self-improve", "tool-management"]

    # Simulate the user clearing skills in Settings after first boot.
    first.enabled_global_skills = []
    manager.save_profile(first)

    # A changed env var on a later boot must not re-apply (watermark is set).
    monkeypatch.setenv(INIT_ENABLED_GLOBAL_SKILLS_ENV, "self-improve:skill-management")
    again = manager.get_profile(BOOTSTRAP_USER_ID)
    assert again.enabled_global_skills == []


# --- tools path: _migrate_default_thread_tools (the lazy get_profile path) ----
#
# Tools adopt on the SAME lazy path that first materializes the profile, NOT in
# the agent's init-time `_migrate_tool_preferences` (which, on a genuine first
# boot, runs before the bootstrap profile exists and so iterates an empty user
# list). These tests therefore drive the real first-boot ordering: get_profile
# with no pre-created profile.


def test_bootstrap_admin_adopts_env_tools_on_first_get_profile(monkeypatch, tmp_path):
    monkeypatch.setenv(
        INIT_DEFAULT_THREAD_TOOLS_ENV, "read_file:web_search_tavily:image_gen_gemini"
    )
    manager = UserProfileManager(tmp_path / "data")
    # First boot: the profile does not exist yet; get_profile creates AND seeds it.
    assert manager.list_users() == []

    tools = manager.get_profile(BOOTSTRAP_USER_ID).tool_preferences.default_thread_tools

    assert tools == ["read_file", "web_search_tavily", "image_gen_gemini"]


def test_no_env_tools_leaves_default_thread_tools_unset(monkeypatch, tmp_path):
    # No picks: get_profile must NOT seed default_thread_tools, leaving it for the
    # agent's core-seed migration / the SEED_TOOLS fallback (unchanged behavior).
    monkeypatch.delenv(INIT_DEFAULT_THREAD_TOOLS_ENV, raising=False)
    manager = UserProfileManager(tmp_path / "data")

    profile = manager.get_profile(BOOTSTRAP_USER_ID)

    assert profile.tool_preferences.default_thread_tools is None


def test_env_tools_only_apply_to_bootstrap_admin(monkeypatch, tmp_path):
    monkeypatch.setenv(INIT_DEFAULT_THREAD_TOOLS_ENV, "read_file:web_search_tavily")
    manager = UserProfileManager(tmp_path / "data")

    other = manager.get_profile("alice")

    # A non-bootstrap user ignores the install-wide picks.
    assert other.tool_preferences.default_thread_tools is None


def test_env_tools_are_one_shot_once_set(monkeypatch, tmp_path):
    monkeypatch.setenv(INIT_DEFAULT_THREAD_TOOLS_ENV, "read_file:web_search_tavily")
    manager = UserProfileManager(tmp_path / "data")
    first = manager.get_profile(BOOTSTRAP_USER_ID).tool_preferences.default_thread_tools
    assert first == ["read_file", "web_search_tavily"]

    # Simulate the user customizing their defaults in Settings after first boot.
    profile = manager.get_profile(BOOTSTRAP_USER_ID)
    profile.tool_preferences.default_thread_tools = ["bash_execute"]
    manager.save_profile(profile)

    # A changed env var on a later boot must not re-seed (the field is set now).
    monkeypatch.setenv(INIT_DEFAULT_THREAD_TOOLS_ENV, "read_file:web_search_brave")
    again = manager.get_profile(BOOTSTRAP_USER_ID).tool_preferences.default_thread_tools
    assert again == ["bash_execute"]


def test_first_boot_ordering_agent_pass_then_get_profile_seeds(monkeypatch, tmp_path):
    """Regression guard for the first-boot ordering bug.

    The agent's init-time `_migrate_tool_preferences` runs over an EMPTY user list
    (no profile.json yet), then the first `get_profile` materializes and seeds the
    bootstrap profile. The picks must land despite the agent pass having already run.
    """
    from nymeria.core.agent import NymeriaAgent

    monkeypatch.setenv(INIT_DEFAULT_THREAD_TOOLS_ENV, "read_file:web_search_tavily")
    manager = UserProfileManager(tmp_path / "data")
    assert manager.list_users() == []  # nothing for the agent pass to see

    NymeriaAgent._migrate_tool_preferences(SimpleNamespace(profile_manager=manager))

    tools = manager.get_profile(BOOTSTRAP_USER_ID).tool_preferences.default_thread_tools
    assert tools == ["read_file", "web_search_tavily"]


def test_agent_migration_does_not_clobber_env_seeded_tools(monkeypatch, tmp_path):
    """A later restart's init pass keeps the already-seeded picks (parity holds)."""
    from nymeria.core.agent import NymeriaAgent

    monkeypatch.setenv(INIT_DEFAULT_THREAD_TOOLS_ENV, "read_file:web_search_tavily")
    manager = UserProfileManager(tmp_path / "data")
    assert manager.get_profile(BOOTSTRAP_USER_ID).tool_preferences.default_thread_tools == [
        "read_file",
        "web_search_tavily",
    ]

    NymeriaAgent._migrate_tool_preferences(SimpleNamespace(profile_manager=manager))

    after = manager.get_profile(BOOTSTRAP_USER_ID).tool_preferences.default_thread_tools
    assert after == ["read_file", "web_search_tavily"]


# --- full-stack compose passthrough ------------------------------------------


def test_full_stack_compose_passes_init_pick_carriers_to_api_and_worker():
    """Lock the delivery path for the full Postgres + Redis stack.

    The writer puts the carriers in `.env.docker`, which the full stack reads via
    `--env-file` interpolation, NOT a service `env_file:`. A var the api/worker
    `environment:` anchor does not list never reaches those containers, so this
    test pins both carriers to a `${VAR:-}` passthrough in both services (the
    worker inherits the api anchor; either process may be the one that first
    materializes the bootstrap admin profile on the shared volume).
    """
    import yaml
    from pathlib import Path

    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
    for service in ("api", "worker"):
        env = services[service]["environment"]
        assert env[INIT_DEFAULT_THREAD_TOOLS_ENV] == (
            "${" + INIT_DEFAULT_THREAD_TOOLS_ENV + ":-}"
        ), f"{service} must pass the tools carrier through with an inert default"
        assert env[INIT_ENABLED_GLOBAL_SKILLS_ENV] == (
            "${" + INIT_ENABLED_GLOBAL_SKILLS_ENV + ":-}"
        ), f"{service} must pass the skills carrier through with an inert default"
