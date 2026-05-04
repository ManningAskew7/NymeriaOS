<script lang="ts">
  import { trapFocus } from '$lib/actions/focus';

  interface Props {
    toolCount: number;
    callableCount: number;
    onContinue: () => void;
    onGoBack: () => void;
  }

  let { toolCount, callableCount, onContinue, onGoBack }: Props = $props();

  const total = $derived(toolCount + callableCount);

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onGoBack();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="warning-backdrop">
  <button
    class="warning-backdrop-button"
    type="button"
    tabindex="-1"
    aria-label="Go back from high tool count warning"
    onclick={onGoBack}
  ></button>
  <div class="warning-panel" role="dialog" aria-modal="true" aria-labelledby="tool-count-warning-title" tabindex="-1" use:trapFocus>
    <div class="warning-icon">!</div>
    <h3 id="tool-count-warning-title">High Tool Count</h3>
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
      <button class="btn btn-ghost" onclick={onGoBack} type="button">
        Go Back
      </button>
      <button class="btn btn-primary" onclick={onContinue} type="button">
        Continue Anyway
      </button>
    </div>
  </div>
</div>

<style>
  .warning-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1100;
  }

  .warning-backdrop-button {
    position: absolute;
    inset: 0;
    padding: 0;
    border: 0;
    background: transparent;
  }

  .warning-panel {
    position: relative;
    background: var(--bg-elevated);
    border: 1px solid var(--warning, #f59e0b);
    border-radius: var(--radius-lg);
    padding: var(--spacing-lg) var(--spacing-xl, 24px);
    max-width: 420px;
    width: 90vw;
    text-align: center;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.4);
  }

  .warning-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border-radius: var(--radius-full);
    background: color-mix(in srgb, var(--warning, #f59e0b) 15%, transparent);
    color: var(--warning, #f59e0b);
    font-size: 20px;
    font-weight: 700;
    margin-bottom: var(--spacing-sm);
  }

  h3 {
    margin: 0 0 var(--spacing-sm);
    font-size: var(--font-size-base);
    font-weight: 600;
    color: var(--text-primary);
  }

  .warning-count {
    margin: 0 0 var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--warning, #f59e0b);
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
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .btn-ghost {
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-default);
  }

  .btn-ghost:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .btn-primary {
    color: white;
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }

  .btn-primary:hover {
    filter: brightness(1.1);
  }
</style>
