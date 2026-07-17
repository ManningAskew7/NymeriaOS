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
  import { api, probeConnection } from '$lib/services/api.svelte';
  import { initLifecycle, destroyLifecycle, backupToPreferences, restoreFromPreferences } from '$lib/utils/lifecycle';
  import {
    extractTokenFromHash,
    consumeTokenHandoff,
    issuePersonalToken,
  } from '$lib/utils/tokenHandoff';
  import Spinner from '$lib/components/common/Spinner.svelte';

  // If config is valid but setupCompleted is false, auto-complete
  function completeSetupIfConfigured() {
    if (configStore.isConfigured && !configStore.setupCompleted) {
      configStore.setupCompleted = true;
    }
  }
  completeSetupIfConfigured();

  // First-run token handoff (`#token=nym_...` fragment planted by the setup
  // wizard's browser auto-open). Inert under Capacitor: the webview URL never
  // carries a fragment, so this stays false. Mirrored from desktop for parity
  // (a served mobile web build would consume it the same way).
  let consumingTokenHandoff = $state(
    typeof window !== 'undefined' && extractTokenFromHash(window.location.hash) !== null
  );

  async function runTokenHandoff() {
    try {
      await consumeTokenHandoff({
        hash: window.location.hash,
        origin: window.location.origin,
        // Scrub FIRST (before the probe) so the raw token leaves the address
        // bar and its history entry as early as possible.
        scrub: () =>
          history.replaceState(null, '', window.location.pathname + window.location.search),
        probe: probeConnection,
        // The fragment carries the 24h bootstrap token; exchange it for a
        // long-lived personal token so the session survives past day one.
        issueToken: issuePersonalToken,
        adopt: (url, key) => {
          configStore.apiUrl = url;
          configStore.apiKey = key;
          configStore.completeSetup();
        },
      });
      // On failure this falls through to the SetupWizard silently; the bad
      // fragment is never shown or persisted.
    } finally {
      // The splash gate below must ALWAYS clear, even if a future edit makes
      // something above throw: a stuck flag would brick boot on this path.
      consumingTokenHandoff = false;
    }
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
    // Priority: dismiss the message action sheet, then cancel an in-progress
    // prompt edit, then return to chat panel from side panels, else exit.
    if (chatStore.actionSheetMessageId) {
      chatStore.closeActionSheet();
      return;
    }
    if (chatStore.isEditing) {
      chatStore.cancelEdit();
      return;
    }
    if (uiStore.activePanel !== 'chat') {
      uiStore.goToChat();
    }
    // If already on chat, let default behavior (exit app) happen
  }

  function handleAppStateChange(isActive: boolean) {
    if (isActive) {
      // App foregrounded — resume polling
      if (configStore.isConfigured) {
        healthStore.startPolling();
        notificationStore.startPolling();
        autonomousStore.connect();
      }
    } else {
      // App backgrounded — pause polling, backup state
      healthStore.stopPolling();
      notificationStore.stopPolling();
      autonomousStore.disconnect();
      backupToPreferences();
    }
  }

  onMount(() => {
    let destroyed = false;

    async function startMobileRuntime() {
      // Consume a first-run #token fragment before the isConfigured checks
      // below run (the template gates on consumingTokenHandoff meanwhile).
      if (consumingTokenHandoff) {
        await runTokenHandoff();
        if (destroyed) return;
      }

      const restored = await restoreFromPreferences();
      if (destroyed) return;
      if (restored) {
        configStore.reloadFromStorage('restoreFromPreferences');
        completeSetupIfConfigured();
      }

      // Initialize Capacitor lifecycle
      await initLifecycle({
        onBackButton: handleBackButton,
        onAppStateChange: handleAppStateChange,
      });
      if (destroyed) {
        await destroyLifecycle();
        return;
      }

      if (configStore.isConfigured) {
        // Start health and notification polling + autonomous stream
        healthStore.startPolling();
        notificationStore.startPolling();
        autonomousStore.connect();

        // Resolve identity FIRST so subsequent reads use the correctly-scoped
        // localStorage keys. Awaited — otherwise the unscoped read of
        // threadsStore.currentThreadId below races the identity refresh and
        // momentarily renders the previous user's thread on a returning user.
        try {
          const id = await configStore.refreshIdentity();
          if (id === null && !configStore.isConfigured) {
            console.warn('[Page] /me returned unauthorized; clearing apiKey');
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
      }
    }

    void startMobileRuntime();

    return () => {
      destroyed = true;
      healthStore.stopPolling();
      notificationStore.stopPolling();
      autonomousStore.disconnect();
      destroyLifecycle();
    };
  });
</script>

{#if consumingTokenHandoff}
  <!-- First-run token handoff in flight: a real connecting state instead of
       flashing the SetupWizard and yanking it away when the probe lands. -->
  <div class="token-handoff" role="status">
    <Spinner size="lg" />
    <p>Connecting to your Nymeria server...</p>
  </div>
{:else if configStore.needsSetup}
  <SetupWizard />
{:else}
  <MobileShell />
{/if}

<!-- Global toast layer — sits above every other surface so 401/403/409
     responses from the account/admin endpoints stay visible regardless of
     which panel/modal is on top. -->
<ErrorToast />

<style>
  /* First-run token-handoff splash: centered, token-styled, motion-free apart
     from the Spinner (which already animates linearly and reads fine static). */
  .token-handoff {
    position: fixed;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-md);
    background: var(--bg-base);
  }

  .token-handoff p {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }
</style>
