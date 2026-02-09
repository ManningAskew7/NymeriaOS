<script lang="ts">
  import { onMount } from 'svelte';
  import { healthStore } from '$lib/stores/health.svelte';

  onMount(() => {
    healthStore.startPolling();
    return () => healthStore.stopPolling();
  });

  const statusText = $derived(
    healthStore.checking && !healthStore.connected
      ? 'Checking...'
      : healthStore.connected
        ? 'API Connected'
        : 'API Disconnected'
  );

  const dotClass = $derived(
    healthStore.checking && !healthStore.connected
      ? 'checking'
      : healthStore.connected
        ? 'connected'
        : 'disconnected'
  );
</script>

<div class="connection-status">
  <div class="status-indicator">
    <span class="dot {dotClass}"></span>
    <span class="status-text">{statusText}</span>
  </div>
  {#if healthStore.connected && healthStore.latencyMs !== null}
    <span class="latency-badge">{healthStore.latencyMs}ms</span>
  {/if}
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
  }

  .dot.checking {
    background: var(--warning);
    box-shadow: 0 0 6px var(--warning);
  }

  .status-text {
    font-size: 11px;
    font-weight: 500;
    color: var(--text-secondary);
  }

  .latency-badge {
    font-size: 10px;
    font-weight: 600;
    color: var(--text-muted);
    background: var(--bg-elevated);
    padding: 2px 6px;
    border-radius: var(--radius-full);
    border: 1px solid var(--border-subtle);
  }

  @keyframes pulse {
    0%, 100% {
      opacity: 1;
    }
    50% {
      opacity: 0.5;
    }
  }
</style>
