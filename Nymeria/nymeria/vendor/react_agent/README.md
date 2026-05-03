# Nymeria React Agent Fork

This package is an owned Nymeria runtime component, not a drop-in mirror of an
upstream repository.

It started as a LangGraph ReAct-style agent package and still depends on
LangGraph and LangChain primitives, but the source in this directory has
diverged enough that it should be maintained as Nymeria code. Treat LangGraph
and LangChain as dependencies to upgrade deliberately, not as a source tree to
copy over this directory.

## What This Fork Owns

- Graph construction for Nymeria's two-node ReAct loop.
- SQLite and PostgreSQL checkpointer wrappers used by sync and async chat
  paths.
- Provider construction for OpenRouter, OpenAI-compatible endpoints, Anthropic,
  CLIProxy, and local LLM endpoints.
- Reasoning-token preservation and replay for OpenAI-compatible and Anthropic
  providers.
- Tool execution timeout handling, tool-result truncation, turn-safety routing,
  and mid-turn tool-reload routing.
- Small compatibility patches for provider and proxy behavior that Nymeria
  relies on at runtime.

## Upstream Relationship

The upstream reference point is LangGraph/LangChain's ReAct graph pattern and
runtime APIs, especially `StateGraph`, `ToolNode`, checkpointers, and
LangChain chat model interfaces. There is no tracked upstream branch or
snapshot in this repo that can be merged automatically.

When LangGraph or LangChain changes, port only the specific behavior needed by
Nymeria. Do not replace this directory wholesale with an upstream example,
template, or package without first auditing the Nymeria-specific behavior above.

## Sync Policy

1. Read the relevant LangGraph/LangChain release notes or docs before changing
   graph, checkpoint, tool-node, or provider integration code.
2. Identify the exact upstream behavior being adopted and the Nymeria behavior
   that must remain intact.
3. Make a focused patch in this fork instead of a bulk vendor refresh.
4. Preserve the public import surface used by `nymeria.core.agent` unless the
   callers are updated in the same change.
5. Run the focused backend tests for the touched surface. For provider or graph
   changes, include at least the model-capability, reasoning-history,
   tool-reload, and compaction tests that apply.
6. Update the docs when a change alters runtime behavior, dependency upgrade
   constraints, or the fork policy itself.

## Notes For Future Refactors

The directory name remains `vendor/react_agent` for import stability. If the
package is renamed later, keep a compatibility shim until all saved imports and
call sites are migrated.
