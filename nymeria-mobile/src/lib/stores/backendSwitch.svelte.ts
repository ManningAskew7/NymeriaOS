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

/** Make (url, token) the live connection and reset everything scoped to the old one. */
export async function switchBackend(apiUrl: string, apiKey: string): Promise<void> {
  // 1. Disconnect the event stream and polling while config still names the old backend.
  autonomousStore.disconnect();
  healthStore.stopPolling();
  notificationStore.stopPolling();

  // 2. Clear the open transcript (bumps the stream generation, withdraws pending prompts).
  chatStore.clearMessages();

  // 3. Repoint the config; every api call reads it per request from here on.
  configStore.apiUrl = apiUrl;
  configStore.apiKey = apiKey;
  configStore.setupCompleted = true;

  // 4. Forced identity refresh: the reload hooks fire on an unchanged scope too,
  //    and an unresolved /me drops the old scope rather than keeping it live.
  await configStore.refreshIdentity({ forceReload: true }).catch(() => {});

  // 5. Threads: in-memory reset (lands on no open thread), then the new list.
  threadsStore.reset();
  await threadsStore.syncFromBackend();

  // 6. Reconnect, unless the token was refused (a 401 cleared the session and
  //    the setup wizard takes over).
  if (configStore.isConfigured) {
    healthStore.startPolling();
    notificationStore.startPolling();
    autonomousStore.connect();
  }

  // 7. Back the new config up now rather than at the next backgrounding, so a
  //    WebView storage clear cannot restore the previous connection.
  void backupToPreferences();
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
 * The Settings > Connection Save. A changed connection switches; an unchanged
 * one keeps the live session untouched (no teardown) and only marks setup
 * complete. Returns whether a switch ran.
 */
export async function saveConnection(apiUrl: string, apiKey: string): Promise<boolean> {
  // Stored the way desktop's saved connections store it: trimmed, no trailing slash.
  const url = apiUrl.trim().replace(/\/+$/, '');
  const key = apiKey.trim();
  if (!connectionChanged(url, key)) {
    configStore.setupCompleted = true;
    return false;
  }
  await switchBackend(url, key);
  return true;
}
