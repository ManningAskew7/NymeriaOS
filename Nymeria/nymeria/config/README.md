# config/

Settings, logging, model capabilities, and system prompts.

## Start here

`settings.py`  -  Pydantic Settings model. Single source of truth for all environment variables.

## Contents

- `settings.py`  -  all env var definitions, hot-reload support
- `logging_config.py`  -  log profiles, per-module overrides
- `model_capabilities.py`  -  per-model feature flags (vision, tools, reasoning)
- `llm_providers.py`  -  registry of 130+ LLM providers (Anthropic native, plus the OpenAI-chat-compatible long tail: OpenAI, OpenRouter, xAI, Gemini, Groq, DeepSeek, Mistral, Azure, Together, Fireworks, etc.). Each spec carries base URL, env var mappings, docs URL, aliases, and API-format flag.
- `soul.md`  -  main system prompt
- `self_agent_prompt.md`  -  self-agent mode prompt
