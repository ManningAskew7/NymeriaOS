<script lang="ts">
  import type { Snippet } from 'svelte';
  import { trapFocus } from '$lib/actions/focus';
  import Icon from './Icon.svelte';

  interface Props {
    title: string;
    onClose: () => void;
    children: Snippet;
    footer?: Snippet;
    width?: string;
  }

  let { title, onClose, children, footer, width = 'min(560px, 92vw)' }: Props = $props();

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      onClose();
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="wizard" style:width={width} role="dialog" aria-modal="true" aria-label={title} tabindex="-1" use:trapFocus>
  <header class="wizard-header">
    <h3>{title}</h3>
    <button class="close-btn" type="button" onclick={onClose} aria-label="Close">
      <Icon name="x" size={18} />
    </button>
  </header>

  <div class="wizard-body">
    {@render children()}
  </div>

  {#if footer}
    <div class="wizard-footer">
      {@render footer()}
    </div>
  {/if}
</div>

<style>
  .wizard {
    display: flex;
    flex-direction: column;
    max-height: 80vh;
  }

  .wizard-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md) var(--spacing-lg);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .close-btn {
    padding: var(--spacing-xs);
    color: var(--text-secondary);
    border-radius: var(--radius-sm);
    background: transparent;
    border: none;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .close-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .close-btn:active {
    transform: scale(var(--press-scale-icon));
  }

  .wizard-body {
    padding: var(--spacing-lg);
    overflow-y: auto;
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .wizard-body :global(p) {
    margin: 0;
    line-height: 1.5;
  }

  .wizard-footer {
    padding: var(--spacing-md) var(--spacing-lg);
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }
</style>
