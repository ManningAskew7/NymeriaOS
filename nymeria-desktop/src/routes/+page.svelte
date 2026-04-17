<script lang="ts">
  import '../app.css';
  import { onMount } from 'svelte';
  import { AppShell, Sidebar, MainPanel, RightPanel } from '$lib/components/layout';
  import { SetupWizard } from '$lib/components/common';
  import StartupOverlay from '$lib/components/common/StartupOverlay.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { backendProcessStore } from '$lib/stores/backendProcess.svelte';
  import { outlookStore } from '$lib/stores/outlook.svelte';
  import { autonomousStore } from '$lib/stores/autonomous.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { startSyncPoll, stopSyncPoll } from '$lib/stores/syncPoll.svelte';
  import { api } from '$lib/services/api.svelte';

  // Debug: log immediately on script execution
  console.log('[Page] Script executing - setupCompleted:', configStore.setupCompleted, 'isConfigured:', configStore.isConfigured);

  // If config is valid but setupCompleted is false, mark setup as completed
  // This handles migration from before setupCompleted flag existed
  if (configStore.isConfigured && !configStore.setupCompleted) {
    console.log('[Page] Config valid but setupCompleted=false, auto-completing setup');
    configStore.setupCompleted = true;
  }

  // Auto-configure from Tauri on first run
  async function autoConfigFromTauri() {
    if (typeof window === 'undefined' || !('__TAURI__' in window)) return;
    if (configStore.isConfigured) return;

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      const config = await invoke<{ api_url: string; api_key: string }>('get_auto_config');
      console.log('[Page] Auto-configuring from Tauri');
      configStore.apiUrl = config.api_url;
      configStore.apiKey = config.api_key;
      configStore.completeSetup();
    } catch (e) {
      console.warn('[Page] Tauri auto-config failed, checking build-time defaults:', e);
      // In client-only mode, use baked-in defaults if available
      const envUrl = import.meta.env.VITE_DEFAULT_API_URL;
      const envKey = import.meta.env.VITE_DEFAULT_API_KEY;
      if (envUrl && envKey && !configStore.isConfigured) {
        console.log('[Page] Auto-configuring from build-time defaults');
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
    ]).then(([history, stats]) => {
      // Only apply if the user hasn't switched threads or started streaming
      if (threadsStore.currentThreadId === threadId && !chatStore.isStreaming) {
        chatStore.setMessages(history.messages);
        chatStore.setContextStats(stats);
        chatStore.setActiveModel(stats?.model ?? null);
        chatStore.setLoadingHistory(false);

        // Start the cross-client sync poller
        startSyncPoll(threadId, history.messages.length);
      } else {
        // Stale: another loader now owns the flag, don't touch it.
      }
    }).catch((err) => {
      console.error('[Page] Failed to load thread history:', err);
      if (threadsStore.currentThreadId === threadId) {
        chatStore.setLoadingHistory(false);
      }
    });
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
      if (match && match.platform !== 'desktop') {
        console.log(`[Page] Restored thread ${restoredId} is ${match.platform}, finding desktop thread`);
        const desktopThreads = threadsStore.threads.filter(
          (t) => !t.id.startsWith('trigger-') &&
                 !t.id.startsWith('discord_') &&
                 !t.id.startsWith('telegram_') &&
                 !t.id.startsWith('slack_')
        );
        const fallback = desktopThreads.sort(
          (a, b) => b.updatedAt.getTime() - a.updatedAt.getTime()
        )[0];
        if (fallback) {
          threadsStore.selectThread(fallback.id);
          console.log('[Page] Switched to desktop thread:', fallback.id);
          loadThreadHistory(fallback.id);
        } else {
          threadsStore.clearCurrent();
          console.log('[Page] No desktop threads available, cleared selection');
        }
        return;
      }

      // Thread is desktop (or unknown to backend) — load it
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
    console.log('[Page] onMount - setupCompleted:', configStore.setupCompleted, 'isConfigured:', configStore.isConfigured);

    // Initialize Outlook bridge (no-ops if not in Outlook)
    outlookStore.initialize();

    // Try auto-config from Tauri, then initialize
    autoConfigFromTauri().then(() => {
      initializeApp();
    });

    // Return cleanup — SSE disconnect happens via autonomousStore
    return () => {
      console.log('[Page] Cleanup - disconnecting SSE');
      stopSyncPoll();
      autonomousStore.disconnect();
    };
  });

  function initializeApp() {
    // Connect if configured (setupCompleted is redundant now but kept for safety)
    if (configStore.isConfigured) {
      // Sync thread metadata from backend (server is authoritative for titles/pins).
      // Runs in background — localStorage provides instant render, backend updates after.
      threadsStore.syncFromBackend();

      // Restore last thread's chat history if one was saved
      const initialThreadId = threadsStore.currentThreadId;
      if (initialThreadId) {
        console.log('[Page] Restoring thread:', initialThreadId);
        restoreThread(initialThreadId);
      }

      console.log('[Page] Config ready, connecting to SSE in 500ms');
      setTimeout(() => {
        console.log('[Page] Calling autonomousStore.connect()');
        autonomousStore.connect();
      }, 500);
    } else {
      console.log('[Page] Config NOT ready (no apiUrl or apiKey), SSE will connect when configured');
    }
  }
</script>

{#if !backendProcessStore.isReady && backendProcessStore.isTauri}
  <StartupOverlay />
{:else if configStore.isFirstRun}
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
