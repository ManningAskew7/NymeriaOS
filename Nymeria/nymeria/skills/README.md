# skills/

Agent Skills system. Skills use SKILL.md progressive-disclosure: only names are shown to the agent until one is invoked, then the full body loads.

## Start here

`__init__.py` — `SkillManager` class, skill loader.

## Contents

- `__init__.py` — skill loading, management, scope resolution
- `meta_tool.py` — the LangChain tool the agent calls to invoke a skill
- `marketplace.py` — skill registry fetcher for discovering/installing skills
- `embedding_index.py` — semantic search over skill descriptions
