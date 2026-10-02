import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { chatStore } from '$lib/stores/chat.svelte';
import { identityReloadGeneration } from '$lib/stores/config.svelte';
import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
import { threadsStore } from '$lib/stores/threads.svelte';
import { refreshThreadState, runTypedCommand, type TypedCommandDeps } from './threadStateRefresh';

/**
 * `threadStateRefresh.ts`'s deps bound to the app's stores (#440). Shared
 * byte-identical by both apps; the call sites are the chat panel's command
 * path and Thread Settings' Save, Reset and hold Revert.
 */
export const liveThreadStateDeps: TypedCommandDeps = {
  getThreadContextStats: (threadId) => api.getThreadContextStats(threadId),
  loadThreadConfig: (threadId) => threadConfigStore.loadConfig(threadId),
  refreshServerSettings: () => serverSettingsStore.refresh(),
  currentThreadId: () => threadsStore.currentThreadId,
  identityGeneration: () => identityReloadGeneration(),
  applyContextStats: (stats) => {
    chatStore.setContextStats(stats);
    chatStore.setActiveModel(stats.model);
  },
  executeCommand: (line, threadId) => api.executeCommand(line, threadId),
  addCommandResult: (line, markdown, success, level) => {
    chatStore.addCommandResult(line, markdown, success, level);
  },
  describeFailure: (error, slashRoot) =>
    humanizeErrorText(error, { action: 'run', resource: `the ${slashRoot} command` }),
};

/** Re-read a thread's stats, config and the server settings after a config write. */
export function refreshLiveThreadState(threadId: string | null | undefined): Promise<void> {
  return refreshThreadState(liveThreadStateDeps, threadId);
}

/** Run a typed slash command: result card, then the refresh, keyed to `threadId`. */
export function runLiveTypedCommand(
  line: string,
  threadId: string | undefined,
  slashRoot: string
): Promise<void> {
  return runTypedCommand(liveThreadStateDeps, line, threadId, slashRoot);
}
