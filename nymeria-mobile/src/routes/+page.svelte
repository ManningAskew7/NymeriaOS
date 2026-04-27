<script lang="ts">
  import '../app.css';
  import { onMount } from 'svelte';
  import MobileShell from '$lib/components/layout/MobileShell.svelte';
  import { SetupWizard } from '$lib/components/common';
  import ErrorToast from '$lib/components/common/ErrorToast.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { healthStore } from '$lib/stores/health.svelte';
  import { notificationStore } from '$lib/stores/notifications.svelte';
  import { autonomousStore } from '$lib/stores/autonomous.svelte';
  import { uiStore } from '$lib/stores/ui.svelte';
  import { api } from '$lib/services/api.svelte';
  import { initLifecycle, destroyLifecycle, backupToPreferences, restoreFromPreferences } from '$lib/utils/lifecycle';

  // If config is valid but setupCompleted is false, auto-complete
  if (configStore.isConfigured && !configStore.setupCompleted) {
    configStore.setupCompleted = true;
  }

  function loadThreadHistory(threadId: string) {
    chatStore.clearMessages();
    chatStore.setLoadingHistory(true);
    Promise.all([
      api.getThreadHistory(threadId),
      api.getThreadContextStats(threadId),
    ])
      .then(([history, stats]) => {
        if (threadsStore.currentThreadId === threadId && !chatStore.isStreaming) {
          chatStore.setMessages(history.messages);
          chatStore.setContextStats(stats);
          chatStore.setActiveModel(stats?.model ?? null);
          chatStore.setLoadingHistory(false);
        }
      })
      .catch((err) => {
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

  function handleBackButton() {
    // Priority: return to chat panel from side panels → exit
    if (uiStore.activePanel !== 'chat') {
      uiStore.goToChat();
    }
    // If already on chat, let default behavior (exit app) happen
  }

  function handleAppStateChange(isActive: boolean) {
    if (isActive) {
      // App foregrounded — resume polling
      if (configStore.isConfigured) {
        healthStore.check();
        autonomousStore.connect();
      }
    } else {
      // App backgrounded — pause polling, backup state
      autonomousStore.disconnect();
      backupToPreferences();
    }
  }

  onMount(() => {
    // Restore from Capacitor Preferences (if localStorage was cleared)
    restoreFromPreferences();

    // Initialize Capacitor lifecycle
    initLifecycle({
      onBackButton: handleBackButton,
      onAppStateChange: handleAppStateChange,
    });

    if (configStore.isConfigured) {
      // Start health and notification polling + autonomous stream
      healthStore.startPolling();
      notificationStore.startPolling();
      autonomousStore.connect();

      // Resolve identity FIRST so subsequent reads use the correctly-scoped
      // localStorage keys. Awaited — otherwise the unscoped read of
      // threadsStore.currentThreadId below races the identity refresh and
      // momentarily renders the previous user's thread on a returning user.
      (async () => {
        try {
          const id = await configStore.refreshIdentity();
          if (id === null && configStore.apiKey) {
            console.warn('[Page] /me returned unauthorized; clearing apiKey');
            configStore.apiKey = '';
            return;
          }
        } catch (e) {
          console.warn('[Page] refreshIdentity failed, continuing with cached scope:', e);
        }
        await threadsStore.syncFromBackend();
        const initialThreadId = threadsStore.currentThreadId;
        if (initialThreadId) {
          loadThreadHistory(initialThreadId);
        }
      })();
    }

    return () => {
      healthStore.stopPolling();
      notificationStore.stopPolling();
      autonomousStore.disconnect();
      destroyLifecycle();
    };
  });
</script>

{#if configStore.isFirstRun}
  <SetupWizard />
{:else}
  <MobileShell />
{/if}

<!-- Global toast layer — sits above every other surface so 401/403/409
     responses from the account/admin endpoints stay visible regardless of
     which panel/modal is on top. -->
<ErrorToast />
