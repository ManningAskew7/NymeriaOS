<script lang="ts">
  import '../app.css';
  import { onMount } from 'svelte';
  import { AppShell, Sidebar, MainPanel, RightPanel } from '$lib/components/layout';
  import { SetupWizard } from '$lib/components/common';
  import ErrorToast from '$lib/components/common/ErrorToast.svelte';
  import StartupOverlay from '$lib/components/common/StartupOverlay.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { backendProcessStore } from '$lib/stores/backendProcess.svelte';
  import { outlookStore } from '$lib/stores/outlook.svelte';
  import { autonomousStore } from '$lib/stores/autonomous.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import { startSyncPoll, stopSyncPoll } from '$lib/stores/syncPoll.svelte';
  import { api } from '$lib/services/api.svelte';
  import { debugLog } from '$lib/utils/debug';

  debugLog('[Page] Script executing - setupCompleted:', configStore.setupCompleted, 'isConfigured:', configStore.isConfigured);

  // If config is valid but setupCompleted is false, mark setup as completed
  // This handles migration from before setupCompleted flag existed
  if (configStore.isConfigured && !configStore.setupCompleted) {
    debugLog('[Page] Config valid but setupCompleted=false, auto-completing setup');
    configStore.setupCompleted = true;
  }

  // Auto-configure from Tauri on first run
  async function autoConfigFromTauri() {
    if (typeof window === 'undefined' || !('__TAURI__' in window)) return;
    if (configStore.isConfigured) return;

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      const config = await invoke<{ api_url: string; api_key: string }>('get_auto_config');
      debugLog('[Page] Auto-configuring from Tauri');
      configStore.apiUrl = config.api_url;
      configStore.apiKey = config.api_key;
      configStore.completeSetup();
    } catch (e) {
      console.warn('[Page] Tauri auto-config failed, checking build-time defaults:', e);
      // In client-only mode, use baked-in defaults if available
      const envUrl = import.meta.env.VITE_DEFAULT_API_URL;
      const envKey = import.meta.env.VITE_DEFAULT_API_KEY;
      if (envUrl && envKey && !configStore.isConfigured) {
        debugLog('[Page] Auto-configuring from build-time defaults');
        configStore.apiUrl = envUrl;
        configStore.apiKey = envKey;
        configStore.completeSetup();
      }
    }
  }

  /**
   * Load history for a thread ID, guarding against stale applies.
   * Only updates chat state if the user hasn't navigated away.
   */
  function loadThreadHistory(threadId: string) {
    chatStore.clearMessages();
    chatStore.setLoadingHistory(true);
    stopSyncPoll();
    Promise.all([
      api.getThreadHistory(threadId),
      api.getThreadContextStats(threadId),
      api.getThreadStatus(threadId),
    ]).then(([history, stats, status]) => {
      // Only apply if the user hasn't switched threads or started streaming
      if (threadsStore.currentThreadId === threadId && !chatStore.isStreaming) {
        chatStore.setMessages(history.messages);
        chatStore.setContextStats(stats);
        chatStore.setActiveModel(stats?.model ?? null);
        chatStore.setLoadingHistory(false);

        // Start the cross-client sync poller
        startSyncPoll(threadId, status);
      } else {
        // Stale: another loader now owns the flag, don't touch it.
      }
    }).catch((err) => {
      console.error('[Page] Failed to load thread history:', err);
      if (threadsStore.currentThreadId === threadId) {
        chatStore.setLoadingHistory(false);
        if (isNotFoundError(err)) {
          threadsStore.clearCurrent();
          chatStore.clearMessages();
          void threadsStore.syncFromBackend();
        }
      }
    });
  }

  function isNotFoundError(error: unknown): boolean {
    return error instanceof Error && /\b404\b/.test(error.message);
  }

  function fallbackRestorableThread(
    backendThreads: { thread_id: string; platform: string }[],
    excludeId?: string
  ) {
    const restorableIds = new Set(
      backendThreads
        .filter((t) => t.platform === 'desktop' || t.platform === 'callable')
        .map((t) => t.thread_id)
    );
    return threadsStore.threads
      .filter((t) => t.id !== excludeId && restorableIds.has(t.id))
      .sort((a, b) => b.updatedAt.getTime() - a.updatedAt.getTime())[0];
  }

  function selectFallbackOrClear(
    backendThreads: { thread_id: string; platform: string }[],
    excludeId?: string
  ) {
    const fallback = fallbackRestorableThread(backendThreads, excludeId);
    if (fallback) {
      threadsStore.selectThread(fallback.id);
      debugLog('[Page] Switched to fallback thread:', fallback.id);
      loadThreadHistory(fallback.id);
    } else {
      threadsStore.clearCurrent();
      chatStore.clearMessages();
      debugLog('[Page] No restorable threads available, cleared selection');
    }
  }

  /**
   * Validate the restored thread against the backend and load its history.
   * If the restored thread is non-desktop (trigger, webhook), switch to
   * the most recent desktop thread instead.
   */
  function restoreThread(restoredId: string) {
    api.listThreads().then((backendThreads) => {
      // Bail if user already navigated away during the request
      if (threadsStore.currentThreadId !== restoredId) return;

      const match = backendThreads.find((t) => t.thread_id === restoredId);
      if (!match) {
        debugLog(`[Page] Restored thread ${restoredId} is no longer listed, clearing stale selection`);
        selectFallbackOrClear(backendThreads, restoredId);
        return;
      }

      if (match.platform !== 'desktop' && match.platform !== 'callable') {
        debugLog(`[Page] Restored thread ${restoredId} is ${match.platform}, finding desktop thread`);
        selectFallbackOrClear(backendThreads, restoredId);
        return;
      }

      // Thread is restorable — load it
      loadThreadHistory(restoredId);
    }).catch((err) => {
      console.warn('[Page] Backend thread validation failed, loading directly:', err);
      // Backend unreachable — load the restored thread as-is
      if (threadsStore.currentThreadId === restoredId) {
        loadThreadHistory(restoredId);
      }
    });
  }

  // Connect to autonomous event stream on mount
  onMount(() => {
    debugLog('[Page] onMount - setupCompleted:', configStore.setupCompleted, 'isConfigured:', configStore.isConfigured);

    // Initialize Outlook bridge (no-ops if not in Outlook)
    outlookStore.initialize();

    // Try auto-config from Tauri, then initialize
    autoConfigFromTauri().then(() => {
      initializeApp();
    });

    // When the window regains focus, re-verify the token — if it was rotated
    // or revoked server-side, we want to route back to SetupWizard before any
    // user-visible call 401s.
    window.addEventListener('focus', () => {
      if (configStore.isConfigured) {
        configStore.refreshIdentity().then((id) => {
          if (id === null && !configStore.isConfigured) {
            // Token invalid — clear setup so the SetupWizard shows.
            console.warn('[Page] Token no longer valid, routing to SetupWizard');
          }
        });
      }
    });

    // Return cleanup — SSE disconnect happens via autonomousStore
    return () => {
      debugLog('[Page] Cleanup - disconnecting SSE');
      stopSyncPoll();
      autonomousStore.disconnect();
    };
  });

  async function initializeApp() {
    // Connect if configured (setupCompleted is redundant now but kept for safety)
    if (configStore.isConfigured) {
      // Resolve identity FIRST so subsequent localStorage reads use the
      // correctly-scoped keys. Awaited — without this, the unscoped read of
      // threadsStore.currentThreadId below races the identity refresh and
      // momentarily flashes the previous user's thread on a returning user.
      try {
        const id = await configStore.refreshIdentity();
        if (id === null && !configStore.isConfigured) {
          console.warn('[Page] /me returned unauthorized; clearing apiKey to route to SetupWizard');
          return;
        }
        if (id) {
          connectionsStore.upsertAccountCredential({
            apiUrl: configStore.apiUrl,
            apiKey: configStore.apiKey,
            identity: id,
            makeActive: true,
          });
        }
      } catch (e) {
        // Network failure — fall through; stores stay in legacy/unscoped
        // mode until the next successful /me call (window-focus listener).
        console.warn('[Page] refreshIdentity failed, continuing with cached scope:', e);
      }

      // Identity settled. Now safe to read scoped state and kick off backend sync.
      await threadsStore.syncFromBackend();

      // Restore last thread's chat history if one was saved
      const initialThreadId = threadsStore.currentThreadId;
      if (initialThreadId) {
        debugLog('[Page] Restoring thread:', initialThreadId);
        restoreThread(initialThreadId);
      }

      debugLog('[Page] Config ready, connecting to SSE in 500ms');
      setTimeout(() => {
        debugLog('[Page] Calling autonomousStore.connect()');
        autonomousStore.connect();
      }, 500);
    } else {
      debugLog('[Page] Config NOT ready (no apiUrl or apiKey), SSE will connect when configured');
    }
  }
</script>

{#if !backendProcessStore.isReady && backendProcessStore.isTauri}
  <StartupOverlay />
{:else if configStore.needsSetup}
  <SetupWizard />
{:else}
  <AppShell>
    {#snippet sidebar()}
      <Sidebar />
    {/snippet}

    {#snippet main()}
      <MainPanel />
    {/snippet}

    {#snippet rightPanel()}
      <RightPanel />
    {/snippet}
  </AppShell>
{/if}

<!-- Global toast layer — sits above every other surface so 401/403/409
     responses from the account/admin endpoints stay visible regardless of
     which panel/modal is on top. -->
<ErrorToast />
