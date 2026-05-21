<script lang="ts">
  import type { Snippet } from 'svelte';
  import { onMount } from 'svelte';
  import { uiStore } from '$lib/stores/ui.svelte';
  import { outlookStore } from '$lib/stores/outlook.svelte';

  interface Props {
    sidebar: Snippet;
    main: Snippet;
    rightPanel: Snippet;
  }

  let { sidebar, main, rightPanel }: Props = $props();

  // Drag-to-resize state
  let dragging = $state<null | 'sidebar' | 'right'>(null);
  let dragStartX = 0;
  let dragStartWidth = 0;
  // Hard floor for the center chat column so the conversation stays readable
  // when a sidebar is dragged wider. Small enough that sidebars can still grow
  // meaningfully on a typical 1280–1920px wide window.
  const MAIN_MIN_WIDTH = 360;

  function startSidebarDrag(e: PointerEvent) {
    if (uiStore.sidebarCollapsed) return;
    dragging = 'sidebar';
    dragStartX = e.clientX;
    dragStartWidth = uiStore.sidebarWidth;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    e.preventDefault();
  }

  function startRightDrag(e: PointerEvent) {
    if (uiStore.rightPanelCollapsed) return;
    dragging = 'right';
    dragStartX = e.clientX;
    dragStartWidth = uiStore.rightPanelWidth;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    e.preventDefault();
  }

  function onPointerMove(e: PointerEvent) {
    if (!dragging) return;
    const dx = e.clientX - dragStartX;
    const viewport = typeof window !== 'undefined' ? window.innerWidth : 0;
    if (dragging === 'sidebar') {
      const otherSide = uiStore.rightPanelCollapsed ? 0 : uiStore.rightPanelWidth;
      const maxByMain = Math.max(0, viewport - otherSide - MAIN_MIN_WIDTH);
      const next = Math.min(dragStartWidth + dx, maxByMain);
      uiStore.setSidebarWidth(next);
    } else {
      const otherSide = uiStore.sidebarCollapsed ? 0 : uiStore.sidebarWidth;
      const maxByMain = Math.max(0, viewport - otherSide - MAIN_MIN_WIDTH);
      const next = Math.min(dragStartWidth - dx, maxByMain);
      uiStore.setRightPanelWidth(next);
    }
  }

  function onPointerUp(e: PointerEvent) {
    if (!dragging) return;
    dragging = null;
    uiStore.persistWidths();
    try {
      (e.target as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {}
  }

  // In Outlook mode, only allow one panel open at a time
  function outlookToggleSidebar() {
    if (outlookStore.isOutlookMode && uiStore.sidebarCollapsed && !uiStore.rightPanelCollapsed) {
      uiStore.toggleRightPanel(); // close right panel first
    }
    uiStore.toggleSidebar();
  }

  function outlookToggleRightPanel() {
    if (outlookStore.isOutlookMode && uiStore.rightPanelCollapsed && !uiStore.sidebarCollapsed) {
      uiStore.toggleSidebar(); // close sidebar first
    }
    uiStore.toggleRightPanel();
  }

  function handleKeydown(e: KeyboardEvent) {
    // Don't intercept shortcuts while typing in inputs
    const tag = (e.target as HTMLElement)?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as HTMLElement)?.isContentEditable) {
      return;
    }

    // Ctrl+B → toggle sidebar
    if (e.ctrlKey && !e.shiftKey && e.key === 'b') {
      e.preventDefault();
      uiStore.toggleSidebar();
    }
    // Ctrl+Shift+B → toggle right panel
    if (e.ctrlKey && e.shiftKey && e.key === 'B') {
      e.preventDefault();
      uiStore.toggleRightPanel();
    }
  }

  onMount(() => {
    window.addEventListener('keydown', handleKeydown);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    return () => {
      window.removeEventListener('keydown', handleKeydown);
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
    };
  });
</script>

<div
  class="app-shell"
  class:dragging
>
  <aside
    class="sidebar"
    class:collapsed={uiStore.sidebarCollapsed}
    class:outlook-hide={outlookStore.isOutlookMode && uiStore.sidebarCollapsed}
    style:--sidebar-width="{uiStore.sidebarWidth}px"
  >
    {@render sidebar()}
    {#if !uiStore.sidebarCollapsed}
      <div
        class="resize-handle right-edge"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize sidebar"
        onpointerdown={startSidebarDrag}
      ></div>
    {/if}
    <button
      type="button"
      class="panel-toggle sidebar-toggle"
      onclick={() => outlookToggleSidebar()}
      aria-label={uiStore.sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      aria-expanded={!uiStore.sidebarCollapsed}
      title={uiStore.sidebarCollapsed ? 'Expand sidebar (Ctrl+B)' : 'Collapse sidebar (Ctrl+B)'}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        {#if uiStore.sidebarCollapsed}
          <polyline points="9,6 15,12 9,18" />
        {:else}
          <polyline points="15,6 9,12 15,18" />
        {/if}
      </svg>
    </button>
  </aside>

  <main class="main-panel">
    {@render main()}
  </main>

  <aside
    class="right-panel"
    class:collapsed={uiStore.rightPanelCollapsed}
    style:--right-panel-width="{uiStore.rightPanelWidth}px"
  >
    {#if !uiStore.rightPanelCollapsed}
      <div
        class="resize-handle left-edge"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize dashboard"
        onpointerdown={startRightDrag}
      ></div>
    {/if}
    <button
      type="button"
      class="panel-toggle right-panel-toggle"
      onclick={() => outlookToggleRightPanel()}
      aria-label={uiStore.rightPanelCollapsed ? 'Expand dashboard' : 'Collapse dashboard'}
      aria-expanded={!uiStore.rightPanelCollapsed}
      title={uiStore.rightPanelCollapsed ? 'Expand dashboard (Ctrl+Shift+B)' : 'Collapse dashboard (Ctrl+Shift+B)'}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        {#if uiStore.rightPanelCollapsed}
          <polyline points="15,6 9,12 15,18" />
        {:else}
          <polyline points="9,6 15,12 9,18" />
        {/if}
      </svg>
    </button>
    {@render rightPanel()}
  </aside>
</div>

<style>
  .app-shell {
    display: flex;
    height: 100vh;
    width: 100vw;
    overflow: hidden;
    background: var(--bg-base);
  }

  .sidebar {
    position: relative;
    width: var(--sidebar-width);
    min-width: var(--sidebar-width);
    height: 100%;
    background: var(--glass-bg-strong);
    border-right: 1px solid var(--glass-border);
    display: flex;
    flex-direction: column;
    overflow: visible;
    transition: width 250ms cubic-bezier(0.4, 0, 0.2, 1),
                min-width 250ms cubic-bezier(0.4, 0, 0.2, 1);
  }

  .sidebar.collapsed {
    width: var(--sidebar-width-collapsed);
    min-width: var(--sidebar-width-collapsed);
  }

  .sidebar.outlook-hide {
    width: 0;
    min-width: 0;
    border-right-color: transparent;
  }

  .sidebar.outlook-hide > :global(:not(.panel-toggle)) {
    display: none;
  }

  .main-panel {
    flex: 1;
    min-width: 0;
    height: 100%;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .right-panel {
    position: relative;
    width: var(--right-panel-width);
    min-width: var(--right-panel-width);
    height: 100%;
    background: var(--glass-bg-strong);
    border-left: 1px solid var(--glass-border);
    display: flex;
    flex-direction: column;
    overflow: visible;
    transition: width 250ms cubic-bezier(0.4, 0, 0.2, 1),
                min-width 250ms cubic-bezier(0.4, 0, 0.2, 1),
                border-color 250ms cubic-bezier(0.4, 0, 0.2, 1);
  }

  .right-panel.collapsed {
    width: 0;
    min-width: 0;
    border-left-color: transparent;
  }

  /* Hover-reveal toggle arrows */
  .panel-toggle {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--glass-bg);
    backdrop-filter: var(--glass-blur);
    border: 1px solid var(--glass-border);
    border-radius: 50%;
    color: var(--text-muted);
    cursor: pointer;
    z-index: 10;
    opacity: 0;
    transition: opacity 200ms ease,
                background var(--transition-fast),
                color var(--transition-fast),
                box-shadow var(--transition-fast);
  }

  /* Extended hover zone for easier targeting */
  .panel-toggle::before {
    content: '';
    position: absolute;
    top: -40px;
    bottom: -40px;
    left: -40px;
    right: -40px;
  }

  .panel-toggle:hover {
    opacity: 1;
    background: var(--bg-hover);
    color: var(--accent-primary);
    box-shadow: var(--accent-glow-sm);
  }

  .sidebar-toggle {
    right: -44px;
  }

  .right-panel-toggle {
    left: -44px;
  }

  /* Drag-to-resize handles on the inner edge of each side panel */
  .resize-handle {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 6px;
    cursor: col-resize;
    z-index: 9;
    background: transparent;
    transition: background 120ms ease;
  }

  .resize-handle.right-edge {
    right: -3px;
  }

  .resize-handle.left-edge {
    left: -3px;
  }

  .resize-handle:hover,
  .app-shell.dragging .resize-handle {
    background: color-mix(in srgb, var(--accent-primary) 35%, transparent);
  }

  /* Suppress width transitions and text selection while a drag is in flight */
  .app-shell.dragging {
    cursor: col-resize;
    user-select: none;
  }

  .app-shell.dragging .sidebar,
  .app-shell.dragging .right-panel {
    transition: none;
  }

</style>
