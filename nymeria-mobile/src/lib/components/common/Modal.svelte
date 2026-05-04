<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';

  interface Props {
    title: string;
    isOpen: boolean;
    onClose: () => void;
    children: Snippet;
  }

  let { title, isOpen, onClose, children }: Props = $props();

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
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="modal-backdrop" onclick={handleBackdropClick} onkeydown={handleKeydown} role="dialog" aria-modal="true" tabindex="-1">
    <div class="modal">
      <div class="modal-header">
        <button class="close-btn" onclick={onClose} type="button" aria-label="Close">
          <Icon name="chevronLeft" size={22} />
        </button>
        <h2>{title}</h2>
        <div class="spacer"></div>
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
    background: var(--bg-base);
    display: flex;
    flex-direction: column;
    z-index: 1000;
    animation: slideIn 200ms ease;
  }

  .modal {
    display: flex;
    flex-direction: column;
    width: 100%;
    height: 100%;
    background: var(--bg-base);
  }

  .modal-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 0 var(--spacing-sm);
    height: var(--header-height);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .modal-header h2 {
    flex: 1;
    margin: 0;
    font-size: var(--font-size-lg);
    font-weight: 600;
    text-align: center;
  }

  .close-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
  }

  .close-btn:active {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .spacer {
    width: var(--touch-target-min);
    flex-shrink: 0;
  }

  .modal-content {
    flex: 1;
    overflow-y: auto;
    padding: var(--spacing-md);
  }

  @keyframes slideIn {
    from {
      transform: translateX(100%);
    }
    to {
      transform: translateX(0);
    }
  }
</style>
