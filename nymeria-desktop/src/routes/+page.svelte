<script lang="ts">
  import '../app.css';
  import { onMount } from 'svelte';
  import { AppShell, Sidebar, MainPanel, RightPanel } from '$lib/components/layout';
  import { SetupWizard } from '$lib/components/common';
  import { configStore } from '$lib/stores/config.svelte';
  import { autonomousStore } from '$lib/stores/autonomous.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { api } from '$lib/services/api.svelte';

  // Debug: log immediately on script execution
  console.log('[Page] Script executing - setupCompleted:', configStore.setupCompleted, 'isConfigured:', configStore.isConfigured);

  // If config is valid but setupCompleted is false, mark setup as completed
  // This handles migration from before setupCompleted flag existed
  if (configStore.isConfigured && !configStore.setupCompleted) {
    console.log('[Page] Config valid but setupCompleted=false, auto-completing setup');
    configStore.setupCompleted = true;
  }

  // Connect to autonomous event stream on mount
  onMount(() => {
    console.log('[Page] onMount - setupCompleted:', configStore.setupCompleted, 'isConfigured:', configStore.isConfigured);

    // Connect if configured (setupCompleted is redundant now but kept for safety)
    if (configStore.isConfigured) {
      // Restore last thread's chat history if one was saved
      const restoredThreadId = threadsStore.currentThreadId;
      if (restoredThreadId) {
        console.log('[Page] Restoring thread:', restoredThreadId);
        Promise.all([
          api.getThreadHistory(restoredThreadId),
          api.getThreadContextStats(restoredThreadId),
        ]).then(([history, stats]) => {
          // Only apply if the user hasn't switched threads or started streaming
          if (threadsStore.currentThreadId === restoredThreadId && !chatStore.isStreaming) {
            chatStore.setMessages(history.messages);
            chatStore.setContextStats(stats);
            chatStore.setActiveModel(stats?.model ?? null);
          }
        }).catch((err) => {
          console.error('[Page] Failed to restore thread history:', err);
        });
      }

      console.log('[Page] Config ready, connecting to SSE in 500ms');
      const timer = setTimeout(() => {
        console.log('[Page] Calling autonomousStore.connect()');
        autonomousStore.connect();
      }, 500);

      return () => {
        console.log('[Page] Cleanup - disconnecting SSE');
        clearTimeout(timer);
        autonomousStore.disconnect();
      };
    } else {
      console.log('[Page] Config NOT ready (no apiUrl or apiKey), SSE will connect when configured');
    }
  });
</script>

{#if configStore.isFirstRun}
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
