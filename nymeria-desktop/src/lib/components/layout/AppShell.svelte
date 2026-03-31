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
    return () => window.removeEventListener('keydown', handleKeydown);
  });
</script>

<div class="app-shell">
  <aside class="sidebar" class:collapsed={uiStore.sidebarCollapsed} class:outlook-hide={outlookStore.isOutlookMode && uiStore.sidebarCollapsed}>
    {@render sidebar()}
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

  <aside class="right-panel" class:collapsed={uiStore.rightPanelCollapsed}>
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

</style>
