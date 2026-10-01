/**
 * Mobile connection switch (#242).
 *
 * Mobile has no saved-connections store: Settings > Connection is its one
 * switch surface. Saving a CHANGED url or token is a backend switch and runs
 * the same sequence as desktop's `connectionsStore.applyConnection`, with the
 * mobile services (health polling instead of the desktop sync poll): tear
 * the old session down, repoint the config, resolve identity with a FORCED
 * reload (every backend-scoped store resets even when both backends name
 * the account `default`), resync threads, reconnect. Before this existed the
 * Save wrote the config and nothing else, so the thread list, the open
 * transcript, every cache and the event stream stayed on the old backend
 * while new messages posted the old thread ids to the new one.
 *
 * Mobile-only file (the drift gate ignores it); desktop's counterpart is
 * `connections.svelte.ts::applyConnection`.
 */

import { configStore } from './config.svelte';
import { chatStore } from './chat.svelte';
import { threadsStore } from './threads.svelte';
import { autonomousStore } from './autonomous.svelte';
import { healthStore } from './health.svelte';
import { notificationStore } from './notifications.svelte';
import { switchToThread } from './navigation.svelte';
import { probeConnection } from '$lib/services/api.svelte';
import { backupToPreferences } from '$lib/utils/lifecycle';
import { normalizeBackendUrl } from '$lib/utils/identityScope';

/** True when (url, token) names a different connection than the live one. */
export function connectionChanged(apiUrl: string, apiKey: string): boolean {
  return (
    normalizeBackendUrl(apiUrl) !== normalizeBackendUrl(configStore.apiUrl) ||
    apiKey.trim() !== configStore.apiKey.trim()
  );
}

/**
 * How a switch landed, for the Save message: the new backend's /me answered
 * (`connected`), refused the token (`refused`: the session is cleared and the
 * setup wizard takes over), or did not answer (`unreachable`: the app stays
 * pointed at the new backend with empty caches, no identity, no threads).
 */
export type SwitchOutcome = 'connected' | 'refused' | 'unreachable';

/** Make (url, token) the live connection and reset everything scoped to the old one. */
export async function switchBackend(apiUrl: string, apiKey: string): Promise<SwitchOutcome> {
  // 1. Disconnect the event stream and polling while config still names the old backend.
  autonomousStore.disconnect();
  healthStore.stopPolling();
  notificationStore.stopPolling();

  // 2. Clear the open transcript (bumps the stream generation, withdraws pending prompts).
  chatStore.clearMessages();

  // 3. Drop every backend-scoped store's state BEFORE the api client targets
  //    the new backend: nothing the old one served is readable, or postable
  //    to the new one (a send in the open thread), during the /me round trip.
  configStore.beginConnectionSwitch(apiUrl);

  // 4. Repoint the config; every api call reads it per request from here on.
  configStore.apiUrl = apiUrl;
  configStore.apiKey = apiKey;
  configStore.setupCompleted = true;

  // 5. Forced identity refresh: the reload hooks fire again under the
  //    resolved scope, and an unresolved /me keeps the switch parked on the
  //    new backend rather than the old scope.
  const identity = await configStore.refreshIdentity({ forceReload: true }).catch(() => null);
  const outcome: SwitchOutcome = identity
    ? 'connected'
    : configStore.isConfigured
      ? 'unreachable'
      : 'refused';

  // 6. Threads: in-memory reset, the new list, then reopen the thread this
  //    backend + account last had open if it still exists (staying on the
  //    current panel: the user is in Settings).
  const reopen = threadsStore.savedCurrentThreadId();
  threadsStore.reset();
  await threadsStore.syncFromBackend();
  if (
    reopen &&
    threadsStore.currentThreadId === null &&
    threadsStore.threads.some((t) => t.id === reopen)
  ) {
    void switchToThread(reopen, { navigate: false });
  }

  // 7. Reconnect, unless the token was refused (a 401 cleared the session and
  //    the setup wizard takes over).
  if (configStore.isConfigured) {
    healthStore.startPolling();
    notificationStore.startPolling();
    autonomousStore.connect();
  }

  // 8. Back the new config up now rather than at the next backgrounding, so a
  //    WebView storage clear cannot restore the previous connection.
  void backupToPreferences();
  return outcome;
}

/**
 * The Settings > Connection Test: validates the FORM values with the stateless
 * probe (health, then /me with that token). It never repoints or persists the
 * live connection; committing is Save's job. (It used to write the form into
 * the live config first, so a failed Test left the app pointed at, and
 * persisted, a bad URL.)
 */
export async function testConnection(
  apiUrl: string,
  apiKey: string
): Promise<{ ok: boolean; message: string }> {
  const result = await probeConnection(apiUrl.trim(), apiKey.trim());
  return result.ok
    ? { ok: true, message: 'Connection successful!' }
    : { ok: false, message: result.message };
}

/**
 * What Settings > Connection Save did: `unchanged` (the live values: nothing
 * torn down), `incomplete` (a blank URL or token: refused before any
 * teardown, nothing written), or how the switch landed.
 */
export type SaveOutcome = 'unchanged' | 'incomplete' | SwitchOutcome;

/**
 * The Settings > Connection Save. A changed connection switches; an unchanged
 * one keeps the live session untouched (no teardown) and only marks setup
 * complete. A blank field is refused: a switch to it would tear the live
 * session down and land on the setup wizard.
 */
export async function saveConnection(apiUrl: string, apiKey: string): Promise<SaveOutcome> {
  // Stored the way desktop's saved connections store it: trimmed, no trailing slash.
  const url = apiUrl.trim().replace(/\/+$/, '');
  const key = apiKey.trim();
  if (!url || !key) return 'incomplete';
  if (!connectionChanged(url, key)) {
    configStore.setupCompleted = true;
    return 'unchanged';
  }
  return switchBackend(url, key);
}

/**
 * The Settings > Connection message for a Save. Success only when the live
 * connection is usable: a switch whose /me never answered must not read as
 * connected (the app is pointed at a backend that did not answer, with no
 * identity and no threads).
 */
export function saveOutcomeMessage(outcome: SaveOutcome): { ok: boolean; message: string } {
  switch (outcome) {
    case 'connected':
      return { ok: true, message: 'Connected to the new backend.' };
    case 'unchanged':
      return { ok: true, message: 'Connection saved!' };
    case 'unreachable':
      return {
        ok: false,
        message: 'Saved, but the backend did not answer. Check the URL and that the server is running.',
      };
    case 'refused':
      return { ok: false, message: 'The backend refused this token. Sign in again with a valid one.' };
    case 'incomplete':
      return { ok: false, message: 'Enter both the backend URL and the token.' };
  }
}
