<script lang="ts">
  import { fade, fly } from 'svelte/transition';
  import { Button } from '$lib/components/common';
  import { trapFocus } from '$lib/actions/focus';
  import { portal } from '$lib/actions/portal';
  import { isTopOverlay, pushOverlay, removeOverlay } from '$lib/utils/overlayStack';
  import {
    OVERLAY_FADE_IN,
    OVERLAY_FADE_OUT,
    DIALOG_RISE_IN,
    DIALOG_RISE_OUT,
  } from '$lib/utils/transitions';

  interface Props {
    toolCount: number;
    callableCount: number;
    onContinue: () => void;
    onGoBack: () => void;
  }

  let { toolCount, callableCount, onContinue, onGoBack }: Props = $props();

  const total = $derived(toolCount + callableCount);

  // Mounted-when-open; Escape only acts when this warning is the topmost
  // overlay (see $lib/utils/overlayStack).
  let layer: symbol | null = null;

  $effect(() => {
    const id = pushOverlay('tool-count-warning');
    layer = id;
    return () => {
      removeOverlay(id);
      layer = null;
    };
  });

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape' && layer && isTopOverlay(layer)) onGoBack();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="warning-backdrop" use:portal in:fade={OVERLAY_FADE_IN} out:fade={OVERLAY_FADE_OUT}>
  <button
    class="warning-backdrop-button"
    type="button"
    tabindex="-1"
    aria-label="Go back from high tool count warning"
    onclick={onGoBack}
  ></button>
  <div class="warning-panel" role="dialog" aria-modal="true" aria-labelledby="tool-count-warning-title" tabindex="-1" use:trapFocus in:fly={DIALOG_RISE_IN} out:fly={DIALOG_RISE_OUT}>
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
      <Button variant="secondary" onclick={onGoBack}>
        Go Back
      </Button>
      <Button variant="primary" onclick={onContinue}>
        Save Anyway
      </Button>
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
    /* §7 "rarely both" exception — the colored warning border is
       SEMANTIC (it signals severity, not decorative chrome) and the
       shadow is FUNCTIONAL (this is a standalone floating modal with
       its own backdrop, so it needs elevation). Each treatment serves
       a different purpose; together they communicate "important and
       floating". */
    border: 1px solid var(--warning);
    border-radius: var(--radius-lg);
    padding: var(--spacing-lg) var(--spacing-xl);
    max-width: 420px;
    width: 90vw;
    text-align: center;
    box-shadow: var(--shadow-xl);
  }

  .warning-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border-radius: var(--radius-full);
    background: color-mix(in srgb, var(--warning) 15%, transparent);
    color: var(--warning);
    font-size: var(--font-size-xl);
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

</style>
