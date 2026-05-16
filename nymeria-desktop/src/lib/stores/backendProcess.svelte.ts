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
  let tauriAvailable = $state(false);

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

  function applyBackendStatusEvent(payload: string) {
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
  }

  async function initializeTauriState() {
    if (typeof window === 'undefined') {
      status = 'ready';
      mode = 'external';
      return;
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      const response = await invoke<BackendStatusResponse>('get_backend_status');
      tauriAvailable = true;
      applyBackendStatusResponse(response);

      import('@tauri-apps/api/event')
        .then(({ listen }) => listen<string>('backend-status', (event) => {
          applyBackendStatusEvent(event.payload);
        }))
        .catch(() => {});
    } catch {
      tauriAvailable = false;
      status = 'ready';
      mode = 'external';
      errorMessage = null;
    }
  }

  void initializeTauriState();

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
      return tauriAvailable;
    },
    get isManagedBackend() {
      return mode === 'managed';
    },
    get isExternalBackend() {
      return mode === 'external' || !tauriAvailable;
    },
  };
}

export const backendProcessStore = createBackendProcessStore();
