<script lang="ts">
  import type { Snippet } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import { trapFocus } from '$lib/actions/focus';
  import { portal } from '$lib/actions/portal';
  import { isTopOverlay, pushOverlay, removeOverlay } from '$lib/utils/overlayStack';
  import {
    OVERLAY_FADE_IN,
    OVERLAY_FADE_OUT,
    DIALOG_RISE_IN,
    DIALOG_RISE_OUT,
  } from '$lib/utils/transitions';
  import Icon from './Icon.svelte';

  interface Props {
    title: string;
    isOpen: boolean;
    onClose: () => void;
    children: Snippet;
  }

  let { title, isOpen, onClose, children }: Props = $props();
  const titleId = `modal-title-${Math.random().toString(36).slice(2)}`;

  // Escape closes only the topmost open overlay, so a modal stacked on
  // another (Provider Setup inside Global Settings) closes one layer at
  // a time instead of dismissing the whole stack.
  let layer: symbol | null = null;

  $effect(() => {
    if (isOpen) {
      const id = pushOverlay(title);
      layer = id;
      return () => {
        removeOverlay(id);
        layer = null;
      };
    }
  });

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape' && layer && isTopOverlay(layer)) {
      onClose();
    }
  }

  function handleBackdropClick(e: MouseEvent) {
    if (e.target === e.currentTarget) {
      onClose();
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

{#if isOpen}
  <!-- Portaled to <body>: the glass .modal surface's backdrop-filter makes it
       a containing block for fixed descendants, so a Modal rendered inside
       another Modal's children would otherwise be clipped to the parent
       dialog instead of covering the viewport. -->
  <div class="modal-backdrop" use:portal in:fade={OVERLAY_FADE_IN} out:fade={OVERLAY_FADE_OUT}>
    <button
      class="modal-backdrop-button"
      type="button"
      tabindex="-1"
      aria-label="Close {title}"
      onclick={handleBackdropClick}
    ></button>
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby={titleId} tabindex="-1" use:trapFocus in:fly={DIALOG_RISE_IN} out:fly={DIALOG_RISE_OUT}>
      <div class="modal-header">
        <h2 id={titleId}>{title}</h2>
        <button class="close-btn" onclick={onClose} type="button" aria-label="Close">
          <Icon name="x" size={20} />
        </button>
      </div>
      <div class="modal-content">
        {@render children()}
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal-backdrop-button {
    position: absolute;
    inset: 0;
    padding: 0;
    border: 0;
    background: transparent;
  }

  .modal {
    position: relative;
    /* §7 glass-surface exception — the hairline --glass-border is edge
       definition for the glassmorphic surface against the blurred
       backdrop, not redundant chrome. Together with --shadow-xl this is
       the documented "rarely both" case for glass modals. */
    background: var(--glass-bg-strong);
    backdrop-filter: var(--glass-blur-strong);
    -webkit-backdrop-filter: var(--glass-blur-strong);
    border-radius: var(--radius-xl);
    border: 1px solid var(--glass-border);
    box-shadow: var(--shadow-xl);
    min-width: min(400px, 90vw);
    max-width: 90vw;
    max-height: 90vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    /* Slimmer vertical padding so the header bar reads as a title strip,
       not a heavy banner. */
    padding: var(--spacing-xs) var(--spacing-lg);
    border-bottom: 1px solid var(--border-subtle);
  }

  .close-btn {
    padding: var(--spacing-xs);
    color: var(--text-secondary);
    border-radius: var(--radius-sm);
    transition: all var(--transition-fast);
  }

  .close-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .close-btn:active {
    transform: scale(var(--press-scale-icon));
  }

  .modal-content {
    padding: var(--spacing-lg);
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }

</style>
