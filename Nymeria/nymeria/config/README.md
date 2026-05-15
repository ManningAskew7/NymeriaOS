# config/

Settings, logging, model capabilities, and system prompts.

## Start here

`settings.py` — Pydantic Settings model. Single source of truth for all environment variables.

## Contents

- `settings.py` — all env var definitions, hot-reload support
- `logging_config.py` — log profiles, per-module overrides
- `model_capabilities.py` — per-model feature flags (vision, tools, reasoning)
- `llm_providers.py` — provider enum and helpers
- `soul.md` — main system prompt
- `self_agent_prompt.md` — self-agent mode prompt
