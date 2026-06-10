<script lang="ts">
  interface Props {
    toolCount: number;
    callableCount: number;
    onContinue: () => void;
    onGoBack: () => void;
  }

  let { toolCount, callableCount, onContinue, onGoBack }: Props = $props();

  const total = $derived(toolCount + callableCount);

  function handleBackdropClick(e: MouseEvent) {
    if ((e.target as HTMLElement).classList.contains('warning-overlay')) {
      onGoBack();
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onGoBack();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="warning-overlay" onclick={handleBackdropClick} onkeydown={handleKeydown} role="dialog" aria-modal="true" tabindex="-1">
  <div class="warning-panel">
    <div class="warning-icon">!</div>
    <h3>High Tool Count</h3>
    <p class="warning-count">
      You have <strong>{total} tools</strong> enabled
      {#if callableCount > 0}
        ({toolCount} tools + {callableCount} callable threads)
      {/if}
    </p>
    <p class="warning-message">
      Having too many tools can degrade model performance and increase response latency.
      Consider creating specialized callable threads to keep each thread's tool list focused.
    </p>
    <div class="warning-actions">
      <button class="btn btn-secondary" onclick={onGoBack} type="button">
        Go Back
      </button>
      <button class="btn btn-primary" onclick={onContinue} type="button">
        Save Anyway
      </button>
    </div>
  </div>
</div>

<style>
  .warning-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 400;
    padding: var(--spacing-lg);
  }

  .warning-panel {
    background: var(--bg-elevated);
    border: 1px solid var(--warning);
    border-radius: var(--radius-lg);
    padding: var(--spacing-lg);
    width: 100%;
    max-width: 400px;
    text-align: center;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.4);
  }

  .warning-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 48px;
    height: 48px;
    border-radius: 50%;
    background: color-mix(in srgb, var(--warning) 15%, transparent);
    color: var(--warning);
    font-size: 24px;
    font-weight: 700;
    margin-bottom: var(--spacing-sm);
  }

  h3 {
    margin: 0 0 var(--spacing-sm);
    font-size: var(--font-size-lg);
    font-weight: 600;
    color: var(--text-primary);
  }

  .warning-count {
    margin: 0 0 var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--warning);
  }

  .warning-message {
    margin: 0 0 var(--spacing-lg);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .warning-actions {
    display: flex;
    gap: var(--spacing-sm);
    justify-content: center;
  }

  .btn {
    padding: var(--spacing-sm) var(--spacing-lg);
    font-size: var(--font-size-base);
    font-weight: 500;
    border-radius: var(--radius-md);
    min-height: var(--touch-target-min);
    min-width: 120px;
  }

  .btn-secondary {
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-default);
  }

  .btn-secondary:active {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .btn-primary {
    color: var(--text-on-accent);
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }

  .btn-primary:active {
    filter: brightness(0.9);
  }
</style>
