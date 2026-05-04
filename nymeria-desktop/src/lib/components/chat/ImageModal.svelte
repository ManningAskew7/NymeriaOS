<script lang="ts">
  import type { FileAttachment } from '$lib/types';
  import { trapFocus } from '$lib/actions/focus';
  import { Icon } from '$lib/components/common';
  import { formatFileSize } from '$lib/utils/fileProcessing';

  interface Props {
    image: FileAttachment | null;
    onClose: () => void;
  }

  let { image, onClose }: Props = $props();

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      onClose();
    }
  }

</script>

<svelte:window onkeydown={handleKeydown} />

{#if image}
  <div
    class="modal-backdrop"
  >
    <button
      class="modal-backdrop-button"
      type="button"
      tabindex="-1"
      aria-label="Close image preview"
      onclick={onClose}
    ></button>
    <div class="modal-content" role="dialog" aria-modal="true" aria-label="Image preview" tabindex="-1" use:trapFocus>
      <div class="modal-header">
        <span class="image-info">
          {image.name} ({formatFileSize(image.size)})
        </span>
        <button
          type="button"
          class="close-button"
          onclick={onClose}
          title="Close"
          aria-label="Close"
        >
          <Icon name="x" size={20} />
        </button>
      </div>
      <div class="modal-body">
        <img src={image.dataUrl} alt={image.name} />
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.8);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    animation: fadeIn var(--transition-fast);
  }

  .modal-backdrop-button {
    position: absolute;
    inset: 0;
    padding: 0;
    border: 0;
    background: transparent;
  }

  @keyframes fadeIn {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }

  .modal-content {
    position: relative;
    max-width: 90vw;
    max-height: 90vh;
    background: var(--bg-elevated-1);
    border-radius: var(--radius-lg);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    animation: scaleIn var(--transition-fast);
  }

  @keyframes scaleIn {
    from {
      transform: scale(0.95);
      opacity: 0;
    }
    to {
      transform: scale(1);
      opacity: 1;
    }
  }

  .modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
    background: var(--bg-elevated-2);
  }

  .image-info {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .close-button {
    padding: var(--spacing-xs);
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all var(--transition-fast);
  }

  .close-button:hover {
    background: var(--bg-elevated-1);
    color: var(--text-primary);
  }

  .modal-body {
    overflow: auto;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .modal-body img {
    max-width: 100%;
    max-height: calc(90vh - 50px);
    object-fit: contain;
  }
</style>
