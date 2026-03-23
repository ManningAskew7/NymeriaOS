/**
 * Tracks the backend process lifecycle (starting → ready → failed).
 * Listens for Tauri events emitted by the Rust process manager.
 */

type BackendStatus = 'starting' | 'ready' | 'failed' | 'unknown';

function createBackendProcessStore() {
  let status = $state<BackendStatus>('unknown');
  let errorMessage = $state<string | null>(null);

  // Listen for Tauri backend-status events
  if (typeof window !== 'undefined' && '__TAURI__' in window) {
    import('@tauri-apps/api/event').then(({ listen }) => {
      listen<string>('backend-status', (event) => {
        const payload = event.payload;
        if (payload === 'starting') {
          status = 'starting';
          errorMessage = null;
        } else if (payload === 'ready') {
          status = 'ready';
          errorMessage = null;
        } else if (payload.startsWith('failed:')) {
          status = 'failed';
          errorMessage = payload.substring(7);
        }
      });
    });

    // Set initial status to starting (Tauri is managing the backend)
    status = 'starting';
  } else {
    // Not running in Tauri — assume backend is externally managed
    status = 'ready';
  }

  return {
    get status() {
      return status;
    },
    get errorMessage() {
      return errorMessage;
    },
    get isReady() {
      return status === 'ready';
    },
    get isTauri() {
      return typeof window !== 'undefined' && '__TAURI__' in window;
    },
  };
}

export const backendProcessStore = createBackendProcessStore();
