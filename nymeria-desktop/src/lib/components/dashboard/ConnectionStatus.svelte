<script lang="ts">
  import { onMount } from 'svelte';
  import { healthStore } from '$lib/stores/health.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { api } from '$lib/services/api.svelte';
  import { Icon } from '$lib/components/common';

  onMount(() => {
    healthStore.startPolling();
    return () => healthStore.stopPolling();
  });

  // POST /restart is gated by require_admin_user — hide the button for
  // non-admins so the UI doesn't promise an action that will 403.
  let isAdmin = $derived(configStore.identity?.role === 'admin');

  let restarting = $state(false);

  const statusText = $derived(
    restarting
      ? 'Restarting...'
      : healthStore.checking && !healthStore.connected
        ? 'Checking...'
        : healthStore.connected
          ? 'API Connected'
          : 'API Disconnected'
  );

  const dotClass = $derived(
    restarting
      ? 'checking'
      : healthStore.checking && !healthStore.connected
        ? 'checking'
        : healthStore.connected
          ? 'connected'
          : 'disconnected'
  );

  async function handleRestart() {
    if (restarting) return;
    restarting = true;

    await api.restartServer();

    // Poll /health until the new instance is ready (up to 30s)
    let attempts = 0;
    const poll = setInterval(async () => {
      attempts++;
      const ok = await api.healthCheck();
      if (ok) {
        clearInterval(poll);
        restarting = false;
        healthStore.check();
      } else if (attempts > 60) {
        // Give up after ~30s
        clearInterval(poll);
        restarting = false;
        healthStore.check();
      }
    }, 500);
  }
</script>

<div class="connection-status">
  <div class="status-indicator">
    <span class="dot {dotClass}"></span>
    <span class="status-text">{statusText}</span>
  </div>
  <div class="status-actions">
    {#if healthStore.connected && healthStore.latencyMs !== null && !restarting}
      <span class="latency-badge">{healthStore.latencyMs}ms</span>
    {/if}
    {#if healthStore.connected && !restarting && isAdmin}
      <button
        class="restart-btn"
        onclick={handleRestart}
        title="Restart API server"
        type="button"
      >
        <Icon name="refresh" size={12} />
      </button>
    {/if}
  </div>
</div>

<style>
  .connection-status {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-md);
    border-top: 1px solid var(--glass-border);
    background: var(--glass-bg);
    flex-shrink: 0;
  }

  .status-indicator {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .dot.connected {
    background: var(--success);
    box-shadow: 0 0 6px var(--success);
    animation: pulse 2s ease-in-out infinite;
  }

  .dot.disconnected {
    background: var(--error);
    box-shadow: 0 0 6px var(--error);
    animation: disconnectedPulse 3s ease-in-out infinite;
  }

  .dot.checking {
    background: var(--warning);
    box-shadow: 0 0 6px var(--warning);
  }

  .status-text {
    font-size: var(--font-size-2xs);
    font-weight: 500;
    color: var(--text-secondary);
  }

  .status-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .latency-badge {
    font-size: var(--font-size-3xs);
    font-weight: 600;
    color: var(--text-muted);
    background: var(--bg-elevated);
    /* Asymmetric vertical padding: -1 top / +1 bottom shifts the text up
       by 1px within the chip without changing total chip height. */
    padding: 1px 6px 3px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    /* Optical centering: numeric/short text in a pill-shaped chip reads as
       left-shifted because pill curves absorb some of the right padding.
       A small positive text-indent nudges the content into the optical
       center without changing the chip's size or rounding. */
    text-indent: 1px;
  }

  .restart-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    padding: 0;
    background: transparent;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .restart-btn:hover {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.08);
  }

  @keyframes pulse {
    0%, 100% {
      opacity: 1;
    }
    50% {
      opacity: 0.5;
    }
  }

  @keyframes disconnectedPulse {
    0%, 100% {
      box-shadow: 0 0 6px var(--error);
    }
    50% {
      box-shadow: 0 0 2px var(--error);
      opacity: 0.7;
    }
  }
</style>
