<script lang="ts">
  import type { FileAttachment } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { formatFileSize } from '$lib/utils/fileProcessing';

  interface Props {
    image: FileAttachment | null;
    onClose: () => void;
  }

  let { image, onClose }: Props = $props();

  // Pinch-to-zoom state
  let scale = $state(1);
  let translateX = $state(0);
  let translateY = $state(0);
  let initialDistance = $state(0);
  let initialScale = $state(1);
  let isPanning = $state(false);
  let lastPanX = $state(0);
  let lastPanY = $state(0);
  let imageContainerRef = $state<HTMLDivElement | null>(null);

  // Reset transform when image changes
  $effect(() => {
    if (image) {
      scale = 1;
      translateX = 0;
      translateY = 0;
    }
  });

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      onClose();
    }
  }

  function handleBackdropClick(event: MouseEvent) {
    if (event.target === event.currentTarget) {
      onClose();
    }
  }

  function getDistance(touches: TouchList): number {
    const dx = touches[0].clientX - touches[1].clientX;
    const dy = touches[0].clientY - touches[1].clientY;
    return Math.sqrt(dx * dx + dy * dy);
  }

  function handleTouchStart(event: TouchEvent) {
    if (event.touches.length === 2) {
      event.preventDefault();
      initialDistance = getDistance(event.touches);
      initialScale = scale;
    } else if (event.touches.length === 1 && scale > 1) {
      isPanning = true;
      lastPanX = event.touches[0].clientX;
      lastPanY = event.touches[0].clientY;
    }
  }

  function handleTouchMove(event: TouchEvent) {
    if (event.touches.length === 2) {
      event.preventDefault();
      const distance = getDistance(event.touches);
      const newScale = Math.min(Math.max(initialScale * (distance / initialDistance), 1), 5);
      scale = newScale;

      // Reset position when zooming back to 1
      if (newScale <= 1) {
        translateX = 0;
        translateY = 0;
      }
    } else if (event.touches.length === 1 && isPanning && scale > 1) {
      event.preventDefault();
      const dx = event.touches[0].clientX - lastPanX;
      const dy = event.touches[0].clientY - lastPanY;
      translateX += dx;
      translateY += dy;
      lastPanX = event.touches[0].clientX;
      lastPanY = event.touches[0].clientY;
    }
  }

  function handleTouchEnd(event: TouchEvent) {
    if (event.touches.length < 2) {
      initialDistance = 0;
    }
    if (event.touches.length === 0) {
      isPanning = false;
    }
  }

  function handleDoubleTap(event: MouseEvent) {
    if (scale > 1) {
      scale = 1;
      translateX = 0;
      translateY = 0;
    } else {
      scale = 2.5;
    }
  }

  let transformStyle = $derived(
    `transform: scale(${scale}) translate(${translateX / scale}px, ${translateY / scale}px)`
  );
</script>

<svelte:window onkeydown={handleKeydown} />

{#if image}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div
    class="modal-backdrop"
    role="dialog"
    aria-modal="true"
    aria-label="Image preview"
    tabindex="-1"
    onclick={handleBackdropClick}
    onkeydown={handleKeydown}
  >
    <div class="modal-content">
      <div class="modal-header">
        <span class="image-info">
          {image.name} ({formatFileSize(image.size)})
        </span>
        <div class="header-actions">
          {#if scale > 1}
            <button
              type="button"
              class="reset-zoom-btn"
              onclick={() => { scale = 1; translateX = 0; translateY = 0; }}
              title="Reset zoom"
            >
              {Math.round(scale * 100)}%
            </button>
          {/if}
          <button
            type="button"
            class="close-button"
            onclick={onClose}
            title="Close"
          >
            <Icon name="x" size={22} />
          </button>
        </div>
      </div>
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="modal-body"
        bind:this={imageContainerRef}
        ontouchstart={handleTouchStart}
        ontouchmove={handleTouchMove}
        ontouchend={handleTouchEnd}
        ondblclick={handleDoubleTap}
      >
        <img
          src={image.dataUrl}
          alt={image.name}
          style={transformStyle}
          draggable="false"
        />
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.9);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    animation: fadeIn var(--transition-fast);
  }

  @keyframes fadeIn {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .modal-content {
    width: 100vw;
    height: 100dvh;
    display: flex;
    flex-direction: column;
    animation: fadeIn var(--transition-fast);
  }

  .modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-md);
    padding-top: calc(var(--spacing-sm) + var(--safe-area-top));
    background: rgba(0, 0, 0, 0.6);
    flex-shrink: 0;
    z-index: 1;
  }

  .image-info {
    font-size: var(--font-size-sm);
    color: rgba(255, 255, 255, 0.8);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    flex-shrink: 0;
  }

  .reset-zoom-btn {
    padding: 4px 8px;
    font-size: var(--font-size-xs);
    color: rgba(255, 255, 255, 0.8);
    background: rgba(255, 255, 255, 0.15);
    border-radius: var(--radius-sm);
  }

  .close-button {
    display: flex;
    align-items: center;
    justify-content: center;
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    border-radius: var(--radius-md);
    color: rgba(255, 255, 255, 0.8);
  }

  .close-button:active {
    background: rgba(255, 255, 255, 0.15);
    color: white;
  }

  .modal-body {
    flex: 1;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
  }

  .modal-body img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    transform-origin: center;
    transition: transform 0.1s ease-out;
  }
</style>
