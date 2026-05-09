/**
 * Tracks the backend process lifecycle (starting → ready → failed).
 * Listens for Tauri events emitted by the Rust process manager.
 */

type BackendStatus = 'starting' | 'ready' | 'failed' | 'unknown';
type BackendMode = 'unknown' | 'managed' | 'external';

type BackendStatusResponse = {
  api: string;
  worker: string;
};

function createBackendProcessStore() {
  let status = $state<BackendStatus>('unknown');
  let errorMessage = $state<string | null>(null);
  let mode = $state<BackendMode>('unknown');

  function applyBackendStatusResponse(response: BackendStatusResponse) {
    if (response.api === 'external' && response.worker === 'external') {
      mode = 'external';
      status = 'ready';
      errorMessage = null;
      return;
    }

    mode = 'managed';
    if (response.api === 'running') {
      status = 'ready';
      errorMessage = null;
    } else if (status === 'unknown') {
      status = 'starting';
    }
  }

  // Listen for Tauri backend-status events
  if (typeof window !== 'undefined' && '__TAURI__' in window) {
    import('@tauri-apps/api/event').then(({ listen }) => {
      listen<string>('backend-status', (event) => {
        const payload = event.payload;
        if (payload === 'starting') {
          mode = 'managed';
          status = 'starting';
          errorMessage = null;
        } else if (payload === 'ready') {
          mode = 'managed';
          status = 'ready';
          errorMessage = null;
        } else if (payload === 'client-only') {
          mode = 'external';
          status = 'ready';
          errorMessage = null;
        } else if (payload.startsWith('failed:')) {
          mode = 'managed';
          status = 'failed';
          errorMessage = payload.substring(7);
        }
      });
    });

    import('@tauri-apps/api/core').then(({ invoke }) => {
      invoke<BackendStatusResponse>('get_backend_status')
        .then(applyBackendStatusResponse)
        .catch(() => {
          if (status === 'unknown') status = 'starting';
        });
    });

    // Assume startup until the Rust shell reports whether this is source
    // checkout-managed or installed client-only mode.
    status = 'starting';
  } else {
    // Not running in Tauri — assume backend is externally managed
    status = 'ready';
    mode = 'external';
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
    get isManagedBackend() {
      return mode === 'managed';
    },
    get isExternalBackend() {
      return mode === 'external' || !(typeof window !== 'undefined' && '__TAURI__' in window);
    },
  };
}

export const backendProcessStore = createBackendProcessStore();
