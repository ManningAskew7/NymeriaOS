<script lang="ts">
  import Icon from '$lib/components/common/Icon.svelte';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { uiStore } from '$lib/stores/ui.svelte';

  interface Props {
    onOpenSettings: (tab?: string) => void;
  }

  let { onOpenSettings }: Props = $props();

  let showDropdown = $state(false);

  function safeHostname(url: string): string {
    try {
      return new URL(url).hostname;
    } catch {
      return url;
    }
  }

  function getDisplayName(): string {
    const conn = connectionsStore.activeConnection;
    if (conn) return conn.name;
    const url = configStore.apiUrl;
    return url ? safeHostname(url) : 'Not connected';
  }

  function isActive(): boolean {
    return connectionsStore.activeConnection !== null;
  }

  function toggleDropdown(e: MouseEvent) {
    e.stopPropagation();
    showDropdown = !showDropdown;
  }

  function closeDropdown() {
    showDropdown = false;
  }

  async function handleSwitch(id: string) {
    closeDropdown();
    await connectionsStore.switchTo(id);
  }

  function handleManage() {
    closeDropdown();
    onOpenSettings('connection');
  }
</script>

{#if !uiStore.sidebarCollapsed}
  <div class="connection-wrapper">
    <button
      class="footer-btn"
      class:is-active={isActive()}
      type="button"
      onclick={toggleDropdown}
      title="Switch connection"
    >
      <Icon name="server" size={18} />
      <span class="conn-name">{getDisplayName()}</span>
      {#if connectionsStore.switching}
        <Icon name="loading" size={14} />
      {/if}
    </button>

    {#if showDropdown}
      <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
      <div class="dropdown-backdrop" onclick={closeDropdown}></div>
      <div class="dropdown">
        {#if connectionsStore.connections.length === 0}
          <div class="dropdown-empty">
            No saved connections
          </div>
        {:else}
          {#each connectionsStore.connections as conn}
            {@const isConnActive = connectionsStore.activeConnectionId === conn.id}
            <button
              class="dropdown-item"
              class:active={isConnActive}
              onclick={() => handleSwitch(conn.id)}
              disabled={connectionsStore.switching}
            >
              <span class="status-dot" class:connected={isConnActive}></span>
              <span class="item-name">{conn.name}</span>
              <span class="item-url">{safeHostname(conn.apiUrl)}</span>
            </button>
          {/each}
        {/if}
        <div class="dropdown-divider"></div>
        <button class="dropdown-item manage" onclick={handleManage}>
          <Icon name="settings" size={14} />
          <span>Manage Connections...</span>
        </button>
      </div>
    {/if}
  </div>
{:else}
  <div class="connection-wrapper">
    <button
      class="icon-btn"
      class:is-active={isActive()}
      type="button"
      onclick={toggleDropdown}
      title={getDisplayName()}
      aria-label="Switch connection"
    >
      <Icon name="server" size={20} />
    </button>

    {#if showDropdown}
      <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
      <div class="dropdown-backdrop" onclick={closeDropdown}></div>
      <div class="dropdown collapsed-dropdown">
        {#if connectionsStore.connections.length === 0}
          <div class="dropdown-empty">
            No saved connections
          </div>
        {:else}
          {#each connectionsStore.connections as conn}
            {@const isConnActive = connectionsStore.activeConnectionId === conn.id}
            <button
              class="dropdown-item"
              class:active={isConnActive}
              onclick={() => handleSwitch(conn.id)}
              disabled={connectionsStore.switching}
            >
              <span class="status-dot" class:connected={isConnActive}></span>
              <span class="item-name">{conn.name}</span>
            </button>
          {/each}
        {/if}
        <div class="dropdown-divider"></div>
        <button class="dropdown-item manage" onclick={handleManage}>
          <Icon name="settings" size={14} />
          <span>Manage...</span>
        </button>
      </div>
    {/if}
  </div>
{/if}

<style>
  .connection-wrapper {
    position: relative;
  }

  .footer-btn {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--text-secondary);
    border-radius: var(--radius-md);
    transition: all var(--transition-fast);
  }

  .footer-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
    transform: translateX(2px);
  }

  .footer-btn.is-active {
    color: var(--accent-primary);
  }

  .conn-name {
    flex: 1;
    text-align: left;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-sm);
  }

  .icon-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    color: var(--text-secondary);
    border-radius: var(--radius-md);
    transition: all var(--transition-fast);
    position: relative;
  }

  .icon-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .icon-btn.is-active {
    color: var(--accent-primary);
  }

  .dropdown-backdrop {
    position: fixed;
    inset: 0;
    z-index: 998;
  }

  .dropdown {
    position: absolute;
    bottom: 100%;
    left: 0;
    right: 0;
    margin-bottom: 4px;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
    z-index: 999;
    min-width: 200px;
    max-height: 300px;
    overflow-y: auto;
  }

  .collapsed-dropdown {
    left: auto;
    left: 0;
    min-width: 180px;
  }

  .dropdown-empty {
    padding: var(--spacing-md);
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    text-align: center;
  }

  .dropdown-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    transition: background var(--transition-fast);
    text-align: left;
  }

  .dropdown-item:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .dropdown-item.active {
    color: var(--accent-primary);
  }

  .dropdown-item:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .dropdown-item.manage {
    color: var(--text-muted);
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-muted);
    flex-shrink: 0;
  }

  .status-dot.connected {
    background: var(--success, #22c55e);
    box-shadow: 0 0 6px rgba(34, 197, 94, 0.4);
  }

  .item-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .item-url {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
    flex-shrink: 0;
  }

  .dropdown-divider {
    height: 1px;
    background: var(--glass-border);
    margin: var(--spacing-xs) 0;
  }
</style>
