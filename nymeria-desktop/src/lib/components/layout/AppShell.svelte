<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    sidebar: Snippet;
    main: Snippet;
    rightPanel: Snippet;
    showRightPanel?: boolean;
  }

  let { sidebar, main, rightPanel, showRightPanel = true }: Props = $props();
</script>

<div class="app-shell">
  <aside class="sidebar">
    {@render sidebar()}
  </aside>

  <main class="main-panel">
    {@render main()}
  </main>

  {#if showRightPanel}
    <aside class="right-panel">
      {@render rightPanel()}
    </aside>
  {/if}
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
    width: var(--sidebar-width);
    min-width: var(--sidebar-width);
    height: 100%;
    background: var(--glass-bg-strong);
    border-right: 1px solid var(--glass-border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
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
    width: var(--right-panel-width);
    min-width: var(--right-panel-width);
    height: 100%;
    background: var(--glass-bg-strong);
    border-left: 1px solid var(--glass-border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
</style>
