# NymeriaOS Roadmap

This file tracks current product and engineering follow-ups. Completed feature
descriptions belong in [feature-list.md](./feature-list.md), implementation
details belong in the relevant system doc, and one-off historical plans should
not be added here.

## Active Follow-Ups

1. Add optional Google Embeddings 2 support for RAG without changing the default
   embedding provider.
2. Polish generated-image artifact UX: gallery/history affordances, retry and
   error recovery, and provider-specific configuration defaults.
3. Review the `notify` tool and notification routing so in-app, push, and
   external destinations have a clear operator model.
4. Improve tool search and enablement UX for both users and the agent.
5. Harden MCP server setup and per-thread MCP tool enablement edge cases.
6. Improve desktop and mobile streaming state polish, especially processing,
   thinking, waiting, queued, and final-response transitions.
7. Add native Gmail tools to the optional tool catalog; current Gmail support
   is through managed MCP server setup.
8. Close remaining NymeriaOS MCP server gaps for provider/model mutation UX,
   callable-thread workflows, and admin lifecycle ergonomics. Triggers, TODOs,
   thread config, settings, notifications, profile, and RAG are already
   API-backed MCP tools.
9. Add a persistent workspace/file browser panel for produced artifacts; current
   chat surfaces expose workspace artifact modals and downloads.
10. Continue `/orchestrate` repair work. (The `/goal` sibling was removed in
    2026-08; `/orchestrate` covers supervised multi-step objectives on
    generic primitives.)

## Future Product Ideas

- Sandboxed Python snippet tools for small transformations.
- Composite tools or reusable workflow chains.
- Automatic callable-thread routing based on intent.
- Tool and Skill Kit marketplace distribution.
- Richer Docker MCP Gateway control, including catalog enablement, secrets,
  OAuth handoff, and update prompts.
- Optional OS keychain storage for desktop-hosted secrets.

## Reference Docs

- [architecture.md](./architecture.md)
- [tools.md](../agent-systems/tools.md)
- [api.md](../api.md)
- [skills.md](../agent-systems/skills.md)
- [notifications.md](../agent-systems/notifications.md)
- [deployment-README.md](../deployment/deployment-README.md)
