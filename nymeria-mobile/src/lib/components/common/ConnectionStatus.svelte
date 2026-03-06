<script lang="ts">
  import { healthStore } from '$lib/stores/health.svelte';

  let statusLabel = $derived(
    healthStore.checking
      ? 'Checking...'
      : healthStore.connected
        ? 'Connected'
        : 'Disconnected'
  );

  let statusClass = $derived(
    healthStore.checking
      ? 'checking'
      : healthStore.connected
        ? 'connected'
        : 'disconnected'
  );
</script>

<button class="connection-status" onclick={() => healthStore.check()} title="Tap to check connection">
  <span class="status-dot {statusClass}"></span>
  <span class="status-label">{statusLabel}</span>
  {#if healthStore.connected && healthStore.latencyMs !== null}
    <span class="latency">{healthStore.latencyMs}ms</span>
  {/if}
</button>

<style>
  .connection-status {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    min-height: 32px;
  }

  .connection-status:active {
    background: var(--bg-hover);
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .status-dot.connected {
    background: var(--success);
    box-shadow: 0 0 4px var(--success);
  }

  .status-dot.disconnected {
    background: var(--error);
    animation: pulse 1.5s ease-in-out infinite;
  }

  .status-dot.checking {
    background: var(--warning, #f59e0b);
    animation: pulse 1s ease-in-out infinite;
  }

  .status-label {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .latency {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
</style>
