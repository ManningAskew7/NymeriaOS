<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Avatar from './Avatar.svelte';
  import RoleChip from './RoleChip.svelte';
  import { identityDisplayName } from './avatar';

  interface Props {
    isOpen: boolean;
    onClose: () => void;
    onOpenSettings: (tab?: string) => void;
    onOpenSwitcher: () => void;
    onOpenAddAccount: () => void;
  }

  let { isOpen, onClose, onOpenSettings, onOpenSwitcher, onOpenAddAccount }: Props = $props();

  let identity = $derived(configStore.identity);
  let isAdmin = $derived(identity?.role === 'admin');

  function safeHostname(url: string): string {
    try {
      return new URL(url).hostname;
    } catch {
      return url;
    }
  }

  function handleManageAccount() {
    onClose();
    onOpenSettings('account');
  }

  function handleManageUsers() {
    onClose();
    onOpenSettings('users');
  }

  function handleSwitchAccount() {
    onClose();
    onOpenSwitcher();
  }

  function handleAddAccount() {
    onClose();
    onOpenAddAccount();
  }

  function handleSignOut() {
    const onlySaved = connectionsStore.connections.length <= 1;
    if (onlySaved) {
      const ok = window.confirm(
        'This will sign you out and return to the setup wizard. Continue?'
      );
      if (!ok) return;
    }
    connectionsStore.clearActive();
    configStore.signOut();
    onClose();
  }

  function handleClickOutside(e: MouseEvent) {
    const target = e.target as HTMLElement;
    if (!target.closest('.account-menu') && !target.closest('.account-trigger')) {
      onClose();
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }

  $effect(() => {
    if (isOpen) {
      document.addEventListener('click', handleClickOutside, true);
      document.addEventListener('keydown', handleKeydown, true);
      return () => {
        document.removeEventListener('click', handleClickOutside, true);
        document.removeEventListener('keydown', handleKeydown, true);
      };
    }
  });
</script>

{#if isOpen}
  <div class="account-menu" role="menu">
    <div class="menu-header">
      <Avatar {identity} size={44} state={identity ? 'connected' : 'unverified'} />
      <div class="header-meta">
        <div class="header-name-row">
          <span class="header-name">{identityDisplayName(identity)}</span>
          <RoleChip role={identity?.role} />
        </div>
        {#if identity?.email && identity.email !== identity.display_name}
          <span class="header-email">{identity.email}</span>
        {/if}
        <span class="header-host">{safeHostname(configStore.apiUrl)}</span>
      </div>
    </div>

    <div class="menu-divider"></div>

    <button class="menu-item" type="button" role="menuitem" onclick={handleManageAccount}>
      <Icon name="user" size={16} />
      <span>Manage account</span>
    </button>

    {#if isAdmin}
      <button class="menu-item" type="button" role="menuitem" onclick={handleManageUsers}>
        <Icon name="users" size={16} />
        <span>Manage users</span>
        <span class="menu-item-badge">admin</span>
      </button>
    {/if}

    <button class="menu-item" type="button" role="menuitem" onclick={handleSwitchAccount}>
      <Icon name="server" size={16} />
      <span>Switch account</span>
    </button>

    <button class="menu-item" type="button" role="menuitem" onclick={handleAddAccount}>
      <Icon name="plus" size={16} />
      <span>Add account</span>
    </button>

    <div class="menu-divider"></div>

    <button class="menu-item destructive" type="button" role="menuitem" onclick={handleSignOut}>
      <Icon name="x" size={16} />
      <span>Sign out</span>
    </button>
  </div>
{/if}

<style>
  .account-menu {
    position: absolute;
    bottom: 100%;
    left: 0;
    right: 0;
    margin-bottom: var(--spacing-xs);
    background: var(--bg-elevated-2, var(--bg-elevated));
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
    z-index: 999;
    min-width: 240px;
    padding: var(--spacing-xs);
    animation: menu-in 120ms ease-out both;
  }

  @keyframes menu-in {
    from {
      opacity: 0;
      transform: translateY(4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }

  .menu-header {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-sm) var(--spacing-md);
  }

  .header-meta {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .header-name-row {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }

  .header-name {
    flex: 0 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    line-height: 1.25;
  }

  .header-email {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 11px;
    color: var(--text-secondary);
  }

  .header-host {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 10px;
    color: var(--text-muted);
    letter-spacing: 0.02em;
  }

  .menu-divider {
    height: 1px;
    background: var(--glass-border);
    margin: 2px var(--spacing-xs);
  }

  .menu-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: 8px var(--spacing-sm);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    text-align: left;
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast), color var(--transition-fast);
  }

  .menu-item span {
    flex: 1;
  }

  .menu-item:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .menu-item.destructive {
    color: var(--error);
  }

  .menu-item.destructive:hover {
    background: rgba(239, 68, 68, 0.12);
    color: var(--error);
  }

  .menu-item-badge {
    flex: 0 !important;
    font-size: 9px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--warning, #fbbf24);
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(251, 191, 36, 0.4);
    padding: 1px 5px;
    border-radius: 4px;
  }
</style>
