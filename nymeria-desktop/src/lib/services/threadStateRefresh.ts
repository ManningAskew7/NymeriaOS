import type { CommandExecuteResponse, CommandResultLevel, ContextStats } from '$lib/types';

/**
 * Re-reading a thread's state after something outside a turn changed it
 * (#440): a typed slash command (`/model X thread`, `/provider switch`,
 * `/fast`, `/smart`, `/fallback revert`, a bare global `/model`) or a
 * Thread Settings write (Save, Reset, the hold Revert). Before this only a
 * turn's `done` refreshed the header chips and the status bar, so they
 * showed the old model, and a `fallback:` chip for a hold already ended,
 * until the next turn finished.
 *
 * Shared byte-identical by both apps (drift gate EXACT_MATCH). Deps are
 * injected (the queuedPrompt.ts precedent) so the order and the guards are
 * unit-tested; `liveThreadState.ts` binds them to the real stores.
 */
export interface ThreadStateRefreshDeps {
  /** GET /threads/{id}/context: the RESOLVED model (a live hold included) and usage. */
  getThreadContextStats(threadId: string): Promise<ContextStats | null>;
  /** Reload the thread's config into the config store (the header chips read it). */
  loadThreadConfig(threadId: string): Promise<unknown>;
  /** Re-read the global settings (the inherited model chip reads them). */
  refreshServerSettings(): Promise<void>;
  /** The thread the chat view shows now. */
  currentThreadId(): string | null;
  /** Bumped on every connection switch (`identityReloadGeneration`). */
  identityGeneration(): number;
  /** Put fresh stats on the status bar (context stats plus active model). */
  applyContextStats(stats: ContextStats): void;
}

export interface TypedCommandDeps extends ThreadStateRefreshDeps {
  executeCommand(line: string, threadId?: string): Promise<CommandExecuteResponse>;
  addCommandResult(line: string, markdown: string, success: boolean, level?: CommandResultLevel): void;
  /** The error card's copy for a command whose request failed. */
  describeFailure(error: unknown, slashRoot: string): string;
}

/**
 * Refresh what a config write can change for `threadId`: its context stats
 * (status bar), then its config (header model and hold chips), and the
 * global server settings (a bare `/model` writes the global default, which
 * the chip shows for a thread with no override). Never rejects.
 *
 * The stats read comes FIRST on purpose: it runs the backend's model
 * resolver, which evicts an expired idle hold, so the config read after it
 * cannot show a hold that is already dead.
 *
 * Stats land only while `threadId` is still the open thread (the status
 * bar belongs to it), and nothing lands after a connection switch. The
 * config still reloads for a thread the user has left: the store keys it
 * by thread, so that thread's chips are fresh when they come back.
 */
export async function refreshThreadState(
  deps: ThreadStateRefreshDeps,
  threadId: string | null | undefined
): Promise<void> {
  const generation = deps.identityGeneration();
  const settings = deps.refreshServerSettings().catch(() => undefined);
  if (threadId) {
    let stats: ContextStats | null = null;
    try {
      stats = await deps.getThreadContextStats(threadId);
    } catch {
      stats = null;
    }
    if (deps.identityGeneration() === generation) {
      if (stats && deps.currentThreadId() === threadId) deps.applyContextStats(stats);
      try {
        await deps.loadThreadConfig(threadId);
      } catch {
        // Best effort: the next turn's end or a thread switch reloads it.
      }
    }
  }
  await settings;
}

/**
 * Run one typed slash command: execute it, add its result card, then
 * refresh the state it may have changed, keyed to the thread it was SENT
 * for. A command that answered (success or not: a multi-step command can
 * fail after writing part of its change) is followed by a refresh; one
 * whose request threw is not (nothing reliable to read). A connection
 * switch while it ran drops both the card and the refresh: the view now
 * belongs to another backend.
 */
export async function runTypedCommand(
  deps: TypedCommandDeps,
  line: string,
  threadId: string | undefined,
  slashRoot: string
): Promise<void> {
  const generation = deps.identityGeneration();
  let result: CommandExecuteResponse;
  try {
    result = await deps.executeCommand(line, threadId);
  } catch (error) {
    if (deps.identityGeneration() !== generation) return;
    // The card is the ONE error surface for a failed command (backlog
    // #135): humanized copy, error accent from the store's level fallback.
    deps.addCommandResult(line, deps.describeFailure(error, slashRoot), false);
    return;
  }
  if (deps.identityGeneration() !== generation) return;
  deps.addCommandResult(line, result.markdown, result.success, result.level);
  await refreshThreadState(deps, threadId);
}
