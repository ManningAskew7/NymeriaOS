# OpenTUI Standalone TUI Plan

**Status:** Deferred documentation-only research
**Updated:** 2026-05-13
**Current shipped terminal UI:** `nymeria cli`

This document records the research and architectural direction for a possible
future OpenTUI terminal application. It is not an implementation plan currently
in progress, and it does not describe a shipped command.

The existing Python CLI remains the supported terminal interface. A future
OpenTUI client must be completely standalone from `nymeria cli`: no new
`--renderer opentui` mode, no reuse of the prompt_toolkit/Rich runtime, and no
shared renderer lifecycle. The only intended shared contract is Nymeria's public
REST/SSE API and, if useful, the on-disk API connection profile format.

## Decision Summary

- Use OpenTUI only for a new standalone terminal app, tentatively exposed as a
  separate `nymeria-tui` binary.
- Use Bun + TypeScript + Solid + OpenTUI as the preferred stack. This matches
  OpenCode's local implementation and OpenTUI's first-class Solid binding docs.
- Make the TUI an API-only thin client. It should call the same backend surface
  used by desktop/mobile/bots: `/chat`, `/threads`, thread history, stop, and
  `/autonomous/stream`.
- Keep the current Python CLI as the stable scrollback-native fallback. The
  OpenTUI app would be a separate richer TUI track, not a replacement.
- Do not start implementation until there is time to build and test a real TUI
  component tree.

## Research Findings

OpenTUI is MIT licensed. The license permits use, modification, distribution,
sublicensing, and sale, provided the copyright and permission notice are
included in copies or substantial portions of the software:
https://raw.githubusercontent.com/anomalyco/opentui/main/LICENSE

The public OpenTUI docs describe it as a TypeScript terminal UI library on a
native Zig core, with first-class React and Solid support. The project page also
states that OpenTUI powers OpenCode in production:
https://opentui.com/

The renderer docs are directly relevant to Nymeria's previous footer/scrollback
problems:
https://opentui.com/docs/core-concepts/renderer/

- `screenMode: "alternate-screen"` is the default full-screen TUI mode.
- `screenMode: "main-screen"` renders on the main screen but still reserves a
  render region, so it is not true direct scrollback rendering.
- `screenMode: "split-footer"` pins OpenTUI to a reserved footer region while
  normal output continues above it. This is the most relevant OpenTUI mode for
  preserving native scrollback with a stable prompt/footer.
- In split-footer mode, `externalOutputMode: "capture-stdout"` can capture
  writes and replay them above the footer instead of letting output overlap the
  footer.
- OpenTUI exposes scrollback writer APIs for rich styled output above the
  footer, including `writeToScrollback(...)` and `createScrollbackSurface(...)`.
  `createScrollbackSurface(...)` is the important primitive for streaming
  content that needs multiple renders before committed rows are appended.

The Solid binding docs add JSX helpers such as `createScrollbackWriter(...)`,
plus hooks for keyboard, resize, paste, selection, renderer access, and terminal
dimensions:
https://opentui.com/docs/bindings/solid/

NPM research on 2026-05-13:

- `@opentui/core` reported version `0.2.8`, license `MIT`, repository
  `git+https://github.com/anomalyco/opentui.git`.
- `@opentui/solid` reported version `0.2.8`, license `MIT`, and a Solid peer
  dependency.
- `@opentui/core` depends on platform-specific native packages such as
  `@opentui/core-linux-x64`, `@opentui/core-linux-arm64`,
  `@opentui/core-darwin-*`, and `@opentui/core-win32-*`. Packaging must verify
  these native bindings on the target platforms before any release.

Local OpenCode reference at `/opt/opencode`:

- OpenCode's package uses Bun, TypeScript, Solid, `@opentui/core`, and
  `@opentui/solid`.
- The TUI entrypoint is under
  `/opt/opencode/packages/opencode/src/cli/cmd/tui/`.
- `app.tsx` owns `createCliRenderer(...)`, Solid `render(...)`, global
  providers, renderer config, mouse/keyboard setup, and shutdown handling.
- `context/sdk.tsx` wraps the generated SDK client and batches SSE/global event
  store updates for a single render pass.
- `context/sync.tsx` keeps the terminal UI state in a Solid store keyed by
  sessions, messages, parts, todos, MCP state, provider state, and status.
- `routes/session/index.tsx` uses OpenTUI renderables such as
  `ScrollBoxRenderable`, sticky scroll behavior, message components, footer,
  sidebar, prompts, dialogs, and keyboard commands.
- `component/prompt/index.tsx` is a full prompt component built around
  `TextareaRenderable`, prompt history, autocomplete, paste handling, keyboard
  bindings, and model/status metadata.

The main lesson from OpenCode is architectural, not visual: one renderer owns
terminal output, an SSE-fed store owns state, and UI components render from that
store. Nymeria should not mix arbitrary stdout writes with component-managed
footer/input rendering inside a live TUI.

## Future Architecture

Recommended package shape if implementation resumes:

```text
nymeria-tui/
  package.json
  bun.lock
  tsconfig.json
  src/
    index.tsx
    app.tsx
    api/
      client.ts
      sse.ts
      events.ts
      profile.ts
    state/
      store.ts
      transcript.ts
      selectors.ts
    components/
      Session.tsx
      Transcript.tsx
      Prompt.tsx
      Footer.tsx
      StatusBar.tsx
      ThreadPicker.tsx
      Dialog.tsx
    test/
```

The package should be independent from `Nymeria/nymeria/triggers/cli/`. It may
reuse the same backend API and the same credential profile file for user
convenience, but it must parse and validate that file in TypeScript rather than
importing or shelling into the Python CLI.

Renderer defaults:

- Start with `screenMode: "alternate-screen"` for a conventional, reliable
  component TUI.
- Prototype `screenMode: "split-footer"` separately if the goal is native
  scrollback plus a stable composer. Use OpenTUI scrollback writers or
  `createScrollbackSurface(...)`; do not write directly to `process.stdout`
  while the footer is active.
- Set `exitOnCtrlC: false` so Nymeria can map Ctrl+C to stop/clear behavior
  before deciding to exit.
- Keep `openConsoleOnError` disabled outside development and route user-visible
  errors through a TUI dialog/toast/status component.
- Use OpenTUI resize hooks instead of terminal escape-sequence repair logic.

Data flow:

```text
Prompt submit
  -> POST /chat as SSE
  -> TypeScript SSE parser
  -> Nymeria stream event normalizer
  -> Solid store update
  -> Transcript/status/prompt components render from store

Background autonomous stream
  -> GET /autonomous/stream
  -> Same event normalizer
  -> Append events for selected thread or surface a background notification

Stop
  -> POST /threads/{thread_id}/stop
  -> Store moves active turn into stopping/cancelled state
```

Initial V1, if revived:

- Load API URL/token/user from flags, environment, or existing
  `~/.nymeria/cli.json` profile.
- Show thread picker or start/resume a thread by flag.
- Load history with `GET /threads/{thread_id}/history`.
- Stream chat events from `POST /chat`.
- Render user messages, assistant response text, thinking previews, tool calls,
  tool results, errors, compaction notices, artifacts, and done state.
- Keep a responsive prompt/footer/status area while streaming.
- Support stop, resize, paste, multiline input, and clean shutdown.
- Support only a small command set at first: help, login/connect, logout, thread
  switch/new/list, stop, clear, and exit.

Later phases:

- Sidebar with thread list, context usage, TODOs, triggers, tools, and skills.
- Command palette and richer slash command autocomplete.
- Permission/question dialogs if backend tool gating exposes interactive prompts.
- Export/copy transcript.
- Theme selection.
- Optional split-footer scrollback mode once the component tree is stable.

## Non-Goals

- Do not port the current prompt_toolkit/Rich renderer into OpenTUI.
- Do not make OpenTUI another value of `--renderer`.
- Do not replace or destabilize `nymeria cli`.
- Do not attempt CLI slash-command parity in the first pass.
- Do not implement a local in-process agent transport. The standalone TUI should
  be a frontend over the API.

## Test And Verification Plan For A Future Implementation

- Unit-test the TypeScript SSE parser and event normalizer with fixtures from
  `Nymeria/tests/cli_fixtures.py` where practical.
- Unit-test store reducers for response chunks, thinking chunks, tool call/result
  ordering, errors, done, cancellation, compaction, autonomous task output, and
  unknown event tolerance.
- Component-test transcript rendering with fixed terminal widths for Markdown,
  code blocks, tables, long lines, wide Unicode, tool rows, and streaming
  updates.
- Add PTY smoke tests for startup, resize, paste, Ctrl+C stop, Ctrl+D/Escape
  exit, and terminal restoration after exceptions.
- Manually test WSL/Linux first, then macOS and Windows after native OpenTUI
  package behavior is verified.
- Before any release, verify bundled license notices for OpenTUI and native
  platform packages.

## Documentation Rules

Until implementation exists:

- User-facing docs must not list `nymeria-tui` as an available command.
- `ui-knowledgebase.md` should describe only the shipped Python CLI.
- Maintainer docs may reference this file as deferred research.

If implementation resumes:

- Keep this document updated as the source of truth for the standalone TUI
  boundary.
- Update `ui-knowledgebase.md` only after the new command is runnable.
- State clearly that the new TUI and `nymeria cli` are standalone clients over
  the same backend, not renderer variants of one another.
