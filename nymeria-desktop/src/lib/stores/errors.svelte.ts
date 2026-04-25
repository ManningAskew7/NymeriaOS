// Tiny toast queue for surfacing structured app-level errors. Lives at the
// root and is rendered by components/common/ErrorToast.svelte. Exists so that
// 401/403/409 responses from the account/admin endpoints can be relayed to the
// user with a recovery hint instead of falling through as silent fetch errors.
//
// auth_invalid additionally triggers configStore.signOut(): an invalid token
// means there's nothing to recover other than re-authenticating, so we route
// the user back to SetupWizard rather than leaving them on a broken screen.

import { configStore } from '$lib/stores/config.svelte';
import { connectionsStore } from '$lib/stores/connections.svelte';

export type ErrorKind =
  | 'auth_invalid'      // 401 — token revoked, expired, or account disabled
  | 'account_disabled'  // 401 with disabled-account body marker
  | 'forbidden_admin'   // 403 from an admin-gated endpoint, caller is not admin
  | 'forbidden_owner'   // 403 from an ownership-gated endpoint
  | 'last_admin'        // 409 from the last-admin guard
  | 'resource_owned'    // 409 from delete-with-resources guard
  | 'generic';          // anything else worth showing

export interface ErrorToastEntry {
  id: string;
  kind: ErrorKind;
  message: string;
  /** Auto-dismiss after this many ms. 0 / null = persistent. */
  ttlMs?: number | null;
  /** Optional CTA shown next to the message. */
  action?: { label: string; onClick: () => void };
}

function createErrorsStore() {
  let queue = $state<ErrorToastEntry[]>([]);

  function push(entry: Omit<ErrorToastEntry, 'id'>): string {
    const id = crypto.randomUUID();
    const ttl = entry.ttlMs === undefined ? 5000 : entry.ttlMs;
    queue = [...queue, { id, ...entry, ttlMs: ttl }];
    if (ttl && ttl > 0) {
      setTimeout(() => dismiss(id), ttl);
    }
    return id;
  }

  function dismiss(id: string): void {
    queue = queue.filter((t) => t.id !== id);
  }

  function clear(): void {
    queue = [];
  }

  /**
   * Push an auth-invalid toast and route the user back to SetupWizard. Use
   * when a 401 indicates the current token is dead — anything else (network
   * blip, wrong URL) should use push({kind:'generic', ...}) instead so we
   * don't sign people out unnecessarily.
   */
  function pushAuthInvalid(message = 'Your session has expired. Sign in again to continue.'): void {
    push({ kind: 'auth_invalid', message, ttlMs: 0 });
    connectionsStore.clearActive();
    configStore.signOut();
  }

  return {
    get queue() {
      return queue;
    },
    push,
    dismiss,
    clear,
    pushAuthInvalid,
  };
}

export const errorsStore = createErrorsStore();
