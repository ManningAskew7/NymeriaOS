"""Tests for the skills loader.

Run with:
    docker exec nymeria-api python -m pytest /app/tests/test_skills_loader.py -v

Or locally:
    cd Nymeria && pytest tests/test_skills_loader.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nymeria.skills import (
    SkillManager,
    SkillParseError,
    load_skill_directory,
    parse_skill_file,
)


VALID_SKILL = """---
name: test-skill
description: A skill for testing. Use this when running unit tests.
license: MIT
allowed-tools: "Read,Write,Bash(ls:*)"
---

# Test skill

Do the test thing.
"""


SKILL_KIT = """---
name: trigger-management
description: Configure and inspect triggers.
metadata:
  nymeria:
    required_tools:
      - trigger_config
      - trigger_info
    tool_ttl: 6h
---

# Trigger kit
"""


def _write_skill(root: Path, name: str, content: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(content)
    return d


def test_parse_skill_file_ok(tmp_path: Path):
    p = tmp_path / "SKILL.md"
    p.write_text(VALID_SKILL)
    fm, body = parse_skill_file(p)
    assert fm.name == "test-skill"
    assert fm.description.startswith("A skill for testing")
    assert fm.license == "MIT"
    assert "Read" in fm.allowed_tools and "Write" in fm.allowed_tools
    assert body.startswith("# Test skill")


def test_parse_skill_file_missing_frontmatter(tmp_path: Path):
    p = tmp_path / "SKILL.md"
    p.write_text("just a body, no frontmatter")
    with pytest.raises(SkillParseError):
        parse_skill_file(p)


def test_parse_skill_file_missing_description(tmp_path: Path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: foo\n---\nbody")
    with pytest.raises(SkillParseError):
        parse_skill_file(p)


def test_parse_skill_file_rejects_non_kebab_case(tmp_path: Path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: FooBar\ndescription: x\n---\nbody")
    with pytest.raises(SkillParseError):
        parse_skill_file(p)


def test_load_skill_directory(tmp_path: Path):
    d = _write_skill(tmp_path, "test-skill", VALID_SKILL)
    skill = load_skill_directory(d, scope="user", user_id="tester")
    assert skill is not None
    assert skill.name == "test-skill"
    assert skill.scope == "user"
    assert skill.user_id == "tester"
    assert skill.body.startswith("# Test skill")
    assert skill.required_tools == []
    assert skill.tool_ttl == "2h"
    assert skill.is_skill_kit is False


def test_load_skill_kit_metadata(tmp_path: Path):
    d = _write_skill(tmp_path, "trigger-management", SKILL_KIT)
    skill = load_skill_directory(d, scope="bundled")
    assert skill is not None
    assert skill.required_tools == ["trigger_config", "trigger_info"]
    assert skill.tool_ttl == "6h"
    assert skill.is_skill_kit is True


def test_manager_precedence_user_over_global(tmp_path: Path):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Same skill name in global and user scopes — user must win.
    _write_skill(data_dir / "global", "dupe", VALID_SKILL.replace(
        "A skill for testing.", "Global version."
    ))
    _write_skill(data_dir / "users" / "alice", "dupe", VALID_SKILL.replace(
        "A skill for testing.", "Alice version."
    ).replace("test-skill", "dupe"))
    # Also rename global's frontmatter name so the two don't collide on that.
    global_md = data_dir / "global" / "dupe" / "SKILL.md"
    global_md.write_text(global_md.read_text().replace("test-skill", "dupe"))

    manager = SkillManager(bundled_dir=bundled, data_skills_dir=data_dir)
    manager.reload()

    alice_view = manager.get("dupe", user_id="alice")
    assert alice_view is not None
    assert alice_view.scope == "user"
    assert "Alice" in alice_view.description

    bob_view = manager.get("dupe", user_id="bob")
    assert bob_view is not None
    assert bob_view.scope == "global"
    assert "Global" in bob_view.description


def test_manager_list_for_thread(tmp_path: Path):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_skill(data_dir / "global", "test-skill", VALID_SKILL)
    _write_skill(data_dir / "global", "other-skill",
                 VALID_SKILL.replace("test-skill", "other-skill"))

    manager = SkillManager(bundled_dir=bundled, data_skills_dir=data_dir)

    # Thread opts in to test-skill and globally-enabled other-skill.
    active = manager.list_for_thread(
        user_id="alice",
        enabled_global_skills=["other-skill"],
        thread_enabled_skills=["test-skill"],
        thread_disabled_skills=[],
    )
    names = {s.name for s in active}
    assert names == {"test-skill", "other-skill"}

    # Thread disables the globally-enabled one.
    active = manager.list_for_thread(
        user_id="alice",
        enabled_global_skills=["other-skill"],
        thread_enabled_skills=[],
        thread_disabled_skills=["other-skill"],
    )
    assert active == []


def test_manager_skips_skill_with_invalid_frontmatter(tmp_path: Path, caplog):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Valid
    _write_skill(data_dir / "global", "test-skill", VALID_SKILL)
    # Invalid — no description
    _write_skill(data_dir / "global", "broken", "---\nname: broken\n---\nbody")

    manager = SkillManager(bundled_dir=bundled, data_skills_dir=data_dir)
    names = {s.name for s in manager.list_installed(user_id="alice")}
    assert "test-skill" in names
    assert "broken" not in names
