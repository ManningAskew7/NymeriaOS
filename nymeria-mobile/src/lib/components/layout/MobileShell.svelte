<script lang="ts">
  import { onMount } from 'svelte';
  import { uiStore } from '$lib/stores/ui.svelte';
  import { hapticImpact } from '$lib/utils/haptics';
  import LeftPanel from './LeftPanel.svelte';
  import ChatPanel from './ChatPanel.svelte';
  import RightPanel from './RightPanel.svelte';

  let container: HTMLElement;
  let scrollTimeout: ReturnType<typeof setTimeout> | null = null;
  let lastPanel = $state(uiStore.activePanel);

  function handleScroll() {
    // Debounce: sync panel state after scroll settles
    if (scrollTimeout) clearTimeout(scrollTimeout);
    scrollTimeout = setTimeout(() => {
      if (container) {
        uiStore.syncFromScroll(container.scrollLeft, container.clientWidth);
        // Haptic feedback when panel changes
        if (uiStore.activePanel !== lastPanel) {
          hapticImpact('light');
          lastPanel = uiStore.activePanel;
        }
      }
    }, 100);
  }

  onMount(() => {
    uiStore.setScrollContainer(container);

    // Start on chat panel (index 1)
    if (container) {
      container.scrollTo({ left: container.clientWidth, behavior: 'instant' });
    }

    return () => {
      uiStore.setScrollContainer(null);
      if (scrollTimeout) clearTimeout(scrollTimeout);
    };
  });
</script>

<main class="mobile-shell" id="main-content">
  <!-- Panel indicator dots -->
  <div class="panel-indicators">
    <button
      class="dot"
      class:active={uiStore.activePanel === 'left'}
      onclick={() => uiStore.goToPanel('left')}
      aria-label="Threads panel"
    ></button>
    <button
      class="dot"
      class:active={uiStore.activePanel === 'chat'}
      onclick={() => uiStore.goToPanel('chat')}
      aria-label="Chat panel"
    ></button>
    <button
      class="dot"
      class:active={uiStore.activePanel === 'right'}
      onclick={() => uiStore.goToPanel('right')}
      aria-label="Dashboard panel"
    ></button>
  </div>

  <!-- Scroll-snap horizontal container -->
  <div
    class="panel-container"
    bind:this={container}
    onscroll={handleScroll}
  >
    <div class="panel">
      <LeftPanel />
    </div>
    <div class="panel">
      <ChatPanel />
    </div>
    <div class="panel">
      <RightPanel />
    </div>
  </div>
</main>

<style>
  .mobile-shell {
    display: flex;
    flex-direction: column;
    height: 100dvh;
    width: 100vw;
    overflow: hidden;
    background: var(--bg-base);
  }

  .panel-indicators {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 8px;
    padding: 6px 0;
    background: var(--bg-base);
    z-index: 10;
    flex-shrink: 0;
  }

  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--border-default);
    transition: all var(--transition-fast);
    cursor: pointer;
  }

  .dot.active {
    width: 20px;
    border-radius: 3px;
    background: var(--accent-primary);
  }

  .panel-container {
    flex: 1;
    display: flex;
    overflow-x: auto;
    overflow-y: hidden;
    scroll-snap-type: x mandatory;
    scroll-behavior: smooth;
    /* Hide scrollbar */
    scrollbar-width: none;
    -ms-overflow-style: none;
  }

  .panel-container::-webkit-scrollbar {
    display: none;
  }

  .panel {
    flex: 0 0 100vw;
    width: 100vw;
    height: 100%;
    scroll-snap-align: start;
    overflow: hidden;
  }
</style>
