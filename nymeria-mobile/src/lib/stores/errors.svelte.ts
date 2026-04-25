// Tiny toast queue for surfacing structured app-level errors. Lives at the
// root and is rendered by components/common/ErrorToast.svelte. Exists so that
// 401/403/409 responses from the account/admin endpoints can be relayed to the
// user with a recovery hint instead of falling through as silent fetch errors.
//
// auth_invalid additionally triggers configStore.signOut(): an invalid token
// means there's nothing to recover other than re-authenticating, so we route
// the user back to SetupWizard rather than leaving them on a broken screen.

import { configStore } from '$lib/stores/config.svelte';

export type ErrorKind =
  | 'auth_invalid'
  | 'account_disabled'
  | 'forbidden_admin'
  | 'forbidden_owner'
  | 'last_admin'
  | 'resource_owned'
  | 'generic';

export interface ErrorToastEntry {
  id: string;
  kind: ErrorKind;
  message: string;
  ttlMs?: number | null;
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

  function pushAuthInvalid(message = 'Your session has expired. Sign in again to continue.'): void {
    push({ kind: 'auth_invalid', message, ttlMs: 0 });
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
