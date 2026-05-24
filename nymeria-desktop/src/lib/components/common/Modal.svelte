<script lang="ts">
  import type { Snippet } from 'svelte';
  import { trapFocus } from '$lib/actions/focus';
  import Icon from './Icon.svelte';

  interface Props {
    title: string;
    isOpen: boolean;
    onClose: () => void;
    children: Snippet;
  }

  let { title, isOpen, onClose, children }: Props = $props();
  const titleId = `modal-title-${Math.random().toString(36).slice(2)}`;

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
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
  <div class="modal-backdrop">
    <button
      class="modal-backdrop-button"
      type="button"
      tabindex="-1"
      aria-label="Close {title}"
      onclick={handleBackdropClick}
    ></button>
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby={titleId} tabindex="-1" use:trapFocus>
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
    animation: fadeIn 150ms ease;
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
    animation: slideUp 200ms ease;
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

  .modal-header h2 {
    margin: 0;
    font-size: var(--font-size-lg);
    font-weight: 600;
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

  .modal-content {
    padding: var(--spacing-lg);
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }

  @keyframes fadeIn {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  @keyframes slideUp {
    from {
      opacity: 0;
      transform: translateY(20px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>
