<script lang="ts">
  import '../app.css';
  import { onMount } from 'svelte';
  import { AppShell, Sidebar, MainPanel, RightPanel } from '$lib/components/layout';
  import { SetupWizard } from '$lib/components/common';
  import ErrorToast from '$lib/components/common/ErrorToast.svelte';
  import StartupOverlay from '$lib/components/common/StartupOverlay.svelte';
  import AuthPromptModal from '$lib/components/credentials/AuthPromptModal.svelte';
  import UiPromptModal from '$lib/components/artifacts/UiPromptModal.svelte';
  import RulerOverlay from '$lib/components/dev/RulerOverlay.svelte';
  import TooltipPortal from '$lib/components/common/TooltipPortal.svelte';
  import { authPromptStore } from '$lib/stores/authPrompt.svelte';
  import { uiPromptStore } from '$lib/stores/uiPrompt.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { backendProcessStore } from '$lib/stores/backendProcess.svelte';
  import { outlookStore } from '$lib/stores/outlook.svelte';
  import { autonomousStore } from '$lib/stores/autonomous.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import { startSyncPoll, stopSyncPoll } from '$lib/stores/syncPoll.svelte';
  import { api, probeConnection } from '$lib/services/api.svelte';
  import { debugLog } from '$lib/utils/debug';
  import { createInitGate } from '$lib/utils/appInit';
  import {
    extractTokenFromHash,
    consumeTokenHandoff,
    issuePersonalToken,
  } from '$lib/utils/tokenHandoff';
  import Spinner from '$lib/components/common/Spinner.svelte';

  debugLog('[Page] Script executing - setupCompleted:', configStore.setupCompleted, 'isConfigured:', configStore.isConfigured);

  // If config is valid but setupCompleted is false, mark setup as completed
  // This handles migration from before setupCompleted flag existed
  if (configStore.isConfigured && !configStore.setupCompleted) {
    debugLog('[Page] Config valid but setupCompleted=false, auto-completing setup');
    configStore.setupCompleted = true;
  }

  // First-run token handoff: `nymeria init` opens the served web UI with a
  // one-shot `#token=nym_...` fragment once the backend is healthy. Detected
  // synchronously at script init so the render gate below shows a connecting
  // splash instead of flashing the SetupWizard and yanking it away (the probe
  // is sub-second on localhost). Inert under Tauri: its window never carries
  // a fragment, so this stays false and nothing changes.
  let consumingTokenHandoff = $state(
    typeof window !== 'undefined' && extractTokenFromHash(window.location.hash) !== null
  );

  async function runTokenHandoff() {
    try {
      const adopted = await consumeTokenHandoff({
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
          // Flips isConfigured, which the initGate effect below reacts to with
          // the normal boot (refreshIdentity, connection upsert, thread sync).
          configStore.completeSetup();
        },
      });
      debugLog('[Page] Token handoff', adopted ? 'adopted' : 'not adopted');
      // On failure this falls through to the SetupWizard silently; the origin
      // autodetect there prefills the backend URL, so the user just pastes a
      // token as before. The bad fragment is never shown or persisted.
    } finally {
      // The splash gate below must ALWAYS clear, even if a future edit makes
      // something above throw: a stuck flag would brick boot on this path.
      consumingTokenHandoff = false;
    }
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
      const message = e instanceof Error ? e.message : String(e);
      if (message.includes('Client-only mode')) {
        debugLog('[Page] Tauri client-only mode, checking build-time defaults');
      } else {
        console.warn('[Page] Tauri auto-config failed, checking build-time defaults:', e);
      }
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
        .filter((t) => t.platform === 'desktop' || t.platform === 'cli' || t.platform === 'callable')
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
   * If the restored thread is native-platform-only (trigger, webhook), switch
   * to the most recent personal thread instead.
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

      if (match.platform !== 'desktop' && match.platform !== 'cli' && match.platform !== 'callable') {
        debugLog(`[Page] Restored thread ${restoredId} is ${match.platform}, finding personal thread`);
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

  // Run the post-config init sequence exactly once, the first time the client
  // is configured. Reacting to configStore.isConfigured (rather than a one-shot
  // onMount) covers all three arrival points: credentials cached at mount,
  // supplied by Tauri/build-time auto-config, or entered in the Setup Wizard.
  // Without this, completing the wizard never triggered syncFromBackend(), so
  // the sidebar stayed stuck on its first-sync gate ("Loading threads…").
  const initGate = createInitGate();
  $effect(() => {
    if (initGate.shouldInitialize(configStore.isConfigured)) {
      void initializeApp();
    }
  });

  // Connect to autonomous event stream on mount
  onMount(() => {
    debugLog('[Page] onMount - setupCompleted:', configStore.setupCompleted, 'isConfigured:', configStore.isConfigured);

    // Initialize Outlook bridge (no-ops if not in Outlook)
    outlookStore.initialize();

    // Consume a first-run #token fragment before anything else can render the
    // SetupWizard (the template gates on consumingTokenHandoff meanwhile).
    if (consumingTokenHandoff) {
      void runTokenHandoff();
    }

    // Pull any auto-config (Tauri source-checkout dev or build-time defaults)
    // into the config store. This may flip isConfigured to true, which the
    // init effect below picks up. initializeApp() is driven by that effect, not
    // chained here, so it also fires when credentials arrive later via the
    // Setup Wizard rather than only when present at mount.
    void autoConfigFromTauri();

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

<!-- Render boundary around the whole app view. Inert unless a descendant
     throws during render or an effect (the failure mode that otherwise blanks
     the screen, e.g. a transient reactive read against half-torn-down state
     during a backend/account switch). On a caught error it shows a recoverable
     fallback instead of a white screen and logs the stack for diagnosis. The
     global toast/auth layers below stay outside so they survive a main-view
     crash. -->
<svelte:boundary onerror={(error) => console.error('[App] Render boundary caught a fatal error:', error)}>
  {#if !backendProcessStore.isReady && backendProcessStore.isTauri}
    <StartupOverlay />
  {:else if consumingTokenHandoff}
    <!-- First-run token handoff in flight: a real connecting state instead of
         flashing the SetupWizard and yanking it away when the probe lands. -->
    <div class="token-handoff" role="status">
      <Spinner size="lg" />
      <p>Connecting to your Nymeria server...</p>
    </div>
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

  {#snippet failed(error, reset)}
    <div class="app-crash" role="alert">
      <div class="app-crash-card">
        <h1>Something went wrong</h1>
        <p>
          The interface hit an unexpected error. Your data and connections are
          safe; this is a display problem, not lost work.
        </p>
        <div class="app-crash-actions">
          <button class="app-crash-btn primary" type="button" onclick={reset}>Try again</button>
          <button class="app-crash-btn" type="button" onclick={() => location.reload()}>Reload app</button>
        </div>
        {#if error instanceof Error && error.message}
          <code class="app-crash-detail">{error.message}</code>
        {/if}
      </div>
    </div>
  {/snippet}
</svelte:boundary>

<!-- Global toast layer — sits above every other surface so 401/403/409
     responses from the account/admin endpoints stay visible regardless of
     which panel/modal is on top. -->
<ErrorToast />

<!-- Global auth-prompt modal — opens when the agent calls request_credential. -->
<AuthPromptModal prompt={authPromptStore.active} onResolved={() => authPromptStore.clear()} />

<!-- Global ui-prompt modal: opens when the agent calls ui_prompt. Renders the
     agent-authored HTML form in a sandboxed iframe and posts the answer back. -->
<UiPromptModal prompt={uiPromptStore.active} onResolved={(id) => uiPromptStore.clearById(id)} />

<!-- Dev ruler overlay — draggable guides, crosshair, and measurement box for
     pixel-perfect alignment work. Toggle with the pin in the bottom-right
     corner or Ctrl+Shift+R. -->
<RulerOverlay />

<!-- Global tooltip portal — renders any [data-tooltip] hover/focus bubble in
     a fixed-position layer with the max possible z-index so it can never be
     clipped by an ancestor's overflow or covered by another stacking context. -->
<TooltipPortal />

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

  /* Fallback shown by the render boundary above when the main view throws.
     Styled with theme tokens so it stays legible across Midnight/Light/Platinum. */
  .app-crash {
    position: fixed;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--spacing-lg);
    background: var(--bg-base);
    z-index: 9999;
  }

  .app-crash-card {
    max-width: 420px;
    text-align: center;
    background: var(--bg-elevated);
    /* §7 — resting card on a blank crash page: border alone defines the
       card, shadow would over-decorate a static surface. */
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    padding: var(--spacing-xl);
  }

  .app-crash-card h1 {
    margin: 0 0 var(--spacing-sm);
    font-size: var(--font-size-lg);
    color: var(--text-primary);
  }

  .app-crash-card p {
    margin: 0 0 var(--spacing-md);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .app-crash-actions {
    display: flex;
    gap: var(--spacing-sm);
    justify-content: center;
  }

  .app-crash-btn {
    padding: 8px 16px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-default);
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    cursor: pointer;
    transition: background var(--transition-fast), border-color var(--transition-fast);
  }

  .app-crash-btn:hover {
    background: var(--bg-hover);
  }

  .app-crash-btn.primary {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
    color: var(--text-on-accent);
  }

  .app-crash-btn.primary:hover {
    background: var(--accent-hover);
  }

  .app-crash-detail {
    display: block;
    margin-top: var(--spacing-md);
    font-family: var(--font-mono);
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    word-break: break-word;
  }
</style>
