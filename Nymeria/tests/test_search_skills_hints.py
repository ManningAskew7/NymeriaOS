"""The search_skills result carries a next_step hint telling the model how to
act on a hit (load an installed skill vs install a marketplace one)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from nymeria.core.agent import set_current_agent
from nymeria.tools.search_skills import search_skills


class _FakeSkill:
    def __init__(self, name, description):
        self.name = name
        self.description = description
        self.scope = "global"


class _FakeSkillManager:
    # embedding_index None forces the substring branch (no network / no index).
    embedding_index = None

    def list_installed(self, user_id=None):
        return [_FakeSkill("ocr-tool", "extract text from images")]


def test_installed_results_hint_points_to_skill_tool():
    agent = SimpleNamespace(skill_manager=_FakeSkillManager())
    set_current_agent(agent)
    try:
        raw = search_skills.func(
            "extract text",
            source="installed",
            config={"configurable": {"thread_id": "t", "user_id": "u"}},
        )
    finally:
        set_current_agent(None)

    payload = json.loads(raw)
    assert "next_step" in payload
    assert "Skill(name=" in payload["next_step"]
    assert payload["results"], "expected the substring match to return a result"
