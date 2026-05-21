# Nymeria Documentation

This directory is the canonical documentation set for the Nymeria backend,
frontends, integrations, deployment, and operations. Historical research and
old plans are kept out of the active index unless they are still useful.

Read the smallest doc that matches your task. Code is authoritative when a doc
and implementation disagree.

## Start Here

| Need | Read |
| --- | --- |
| First local setup | [QUICKSTART.md](./QUICKSTART.md) |
| Current architecture | [architecture.md](./architecture.md) |
| API routes and SSE protocol | [api.md](./api.md) |
| Environment variables | [configuration.md](./configuration.md) |
| Feature inventory | [feature-list.md](./feature-list.md) |
| Current roadmap and follow-ups | [ROADMAP.md](./ROADMAP.md) |
| Production deployment | [PRODUCTION_DEPLOYMENT.md](./PRODUCTION_DEPLOYMENT.md) |
| Release workflow | [release-workflow.md](./release-workflow.md) |

## Runtime Systems

| Topic | Doc |
| --- | --- |
| Tool system | [tools.md](./tools.md) |
| Generated flat tool index | [tools-index.md](./tools-index.md) |
| Tool hot-loading | [tool-hot-loading.md](./tool-hot-loading.md) |
| Skills and Skill Kits | [skills.md](./skills.md) |
| Accounts and authentication | [accounts.md](./accounts.md) |
| Credential vault | [credentials.md](./credentials.md) |
| Context compaction and checkpoints | [compaction-and-checkpoints.md](./compaction-and-checkpoints.md) |
| Reasoning token streaming | [reasoning-streaming.md](./reasoning-streaming.md) |
| TODO scheduling and watchdog | [user-todo-management.md](./user-todo-management.md) |
| Event-driven triggers | [triggers.md](./triggers.md) |
| Notifications | [notifications.md](./notifications.md) |
| Logging | [logging.md](./logging.md) |
| Open `/goal` and `/orchestrate` issues | [BUG-goal-orchestrate-review-2026-05-18.md](./BUG-goal-orchestrate-review-2026-05-18.md) |

## Deployment

| Topic | Doc |
| --- | --- |
| Slim vs Docker shapes | [deployment/README.md](./deployment/README.md) |
| Slim single-process launcher | [deployment/slim.md](./deployment/slim.md) |
| Remote access | [deployment/remote-access.md](./deployment/remote-access.md) |
| Docker production deployment | [PRODUCTION_DEPLOYMENT.md](./PRODUCTION_DEPLOYMENT.md) |
| Database migrations | [MIGRATION.md](./MIGRATION.md) |
| Git-crypt and secrets | [git-crypt.md](./git-crypt.md) |
| Secrets audit | [SECRETS_AUDIT.md](./SECRETS_AUDIT.md) |

## Providers And Infrastructure

| Topic | Doc |
| --- | --- |
| CLIProxy | [cliproxy.md](./cliproxy.md) |
| OpenRouter | [openrouter.md](./openrouter.md) |
| OpenAI-compatible chat providers | [chat_completions_providers.md](./chat_completions_providers.md) |
| Local LLM inference | [local-llm.md](./local-llm.md) |
| HexStrike MCP | [hexstrike-mcp.md](./hexstrike-mcp.md) |
| Private B tools | [_prv_b.md](./_prv_b.md) |

## Frontends

| Topic | Doc |
| --- | --- |
| Desktop/mobile shared file rules | [desktop-vs-mobile.md](./desktop-vs-mobile.md) |
| Frontend account UX | [frontend-accounts.md](./frontend-accounts.md) |
| UI behavior reference | [ui-knowledgebase.md](./ui-knowledgebase.md) |
| Outlook add-in | [outlook-addin.md](./outlook-addin.md) |
| Manual Chrome MCP frontend testing | [chrome-mcp-testing.md](./chrome-mcp-testing.md) |

## Bot And Chat Integrations

| Integration | Doc |
| --- | --- |
| Discord | [discord-bot.md](./discord-bot.md) |
| Telegram | [telegram-bot.md](./telegram-bot.md) |
| Slack | [slack-bot.md](./slack-bot.md) |
| Matrix | [matrix-bot.md](./matrix-bot.md) |
| Mattermost | [mattermost-bot.md](./mattermost-bot.md) |
| Zulip | [zulip-bot.md](./zulip-bot.md) |
| Rocket.Chat | [rocketchat-bot.md](./rocketchat-bot.md) |
| Signal | [signal-bot.md](./signal-bot.md) |
| Twitch | [twitch-bot.md](./twitch-bot.md) |
| WhatsApp | [whatsapp-bot.md](./whatsapp-bot.md) |
| Messenger | [messenger-bot.md](./messenger-bot.md) |
| Instagram | [instagram-bot.md](./instagram-bot.md) |
| Webex | [webex-bot.md](./webex-bot.md) |
| Microsoft Teams | [teams-bot.md](./teams-bot.md) |
| Google Chat | [google-chat-bot.md](./google-chat-bot.md) |
| LINE | [line-bot.md](./line-bot.md) |

## Beta, Release, And Product Material

| Topic | Doc |
| --- | --- |
| Beta tester quickstart | [BETA_QUICKSTART.md](./BETA_QUICKSTART.md) |
| Beta access control | [BETA_ACCESS_CONTROL.md](./BETA_ACCESS_CONTROL.md) |
| Private beta package index | [BETA_PRIVATE_INDEX.md](./BETA_PRIVATE_INDEX.md) |
| Beta troubleshooting | [BETA_TROUBLESHOOTING.md](./BETA_TROUBLESHOOTING.md) |
| Project brief | [PROJECT_BRIEF.md](./PROJECT_BRIEF.md) |
| _PRV_A setup | [_prv_a/setup-guide.md](./_prv_a/setup-guide.md) |
| _PRV_A notepad content | [_prv_a/notepad-content.md](./_prv_a/notepad-content.md) |
| _PRV_A system prompt | [_prv_a/system-prompt.md](./_prv_a/system-prompt.md) |

## Historical Material

- [archive/](./archive/) contains retired docs kept only for historical context.
- [project/business-strategy/](./project/business-strategy/) contains product
  strategy material, not implementation guidance.
- [project/security-audit-2026-04-19/](./project/security-audit-2026-04-19/)
  contains historical audit artifacts. Prefer current security docs and tests
  for implementation work.
- `Nymeria/security-audit/` contains the current remediation-oriented audit
  notes that are still useful for security work.

## Regenerating Inventories

```bash
cd /opt/NymeriaOS/Nymeria
python3 scripts/generate_tools_index.py > docs/tools-index.md
```

Run the generator after tool additions or removals. `docs/tools.md` contains the
human-authored guide and must be updated manually when tool behavior changes.
