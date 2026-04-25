<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import { uiStore } from '$lib/stores/ui.svelte';
  import Avatar from './Avatar.svelte';
  import RoleChip from './RoleChip.svelte';
  import AccountMenu from './AccountMenu.svelte';
  import AccountSwitcher from './AccountSwitcher.svelte';
  import AddAccountSheet from './AddAccountSheet.svelte';
  import { identityDisplayName } from './avatar';

  interface Props {
    onOpenSettings: (tab?: string) => void;
  }

  let { onOpenSettings }: Props = $props();

  let showMenu = $state(false);
  let showSwitcher = $state(false);
  let showAddAccount = $state(false);

  function openSwitcher() {
    showMenu = false;
    showSwitcher = true;
  }

  function closeSwitcher() {
    showSwitcher = false;
  }

  function openAddAccount() {
    showMenu = false;
    showSwitcher = false;
    showAddAccount = true;
  }

  function closeAddAccount() {
    showAddAccount = false;
  }

  let identity = $derived(configStore.identity);
  let activeEntry = $derived(connectionsStore.activeConnection);
  let entryError = $derived(activeEntry?.identityError ?? null);
  let stillVerifying = $derived(
    !!configStore.isConfigured && identity === null && !entryError
  );

  let avatarState = $derived<'connected' | 'disabled' | 'unverified' | 'loading' | 'plain'>(
    stillVerifying
      ? 'loading'
      : identity == null
      ? 'unverified'
      : 'connected'
  );

  let primaryLabel = $derived(
    stillVerifying ? 'Connecting...' : identityDisplayName(identity)
  );
  let secondaryLabel = $derived(
    entryError
      ? entryError
      : identity?.display_name && identity.display_name !== identity.email
      ? identity.email
      : ''
  );

  function toggleMenu(e: MouseEvent) {
    e.stopPropagation();
    if (showSwitcher) {
      // Clicking the trigger while the switcher is open should also close it.
      showSwitcher = false;
      return;
    }
    showMenu = !showMenu;
  }

  function closeMenu() {
    showMenu = false;
  }
</script>

<div class="account-wrapper">
  {#if !uiStore.sidebarCollapsed}
    <button
      class="account-trigger"
      class:menu-open={showMenu || showSwitcher}
      type="button"
      onclick={toggleMenu}
      aria-haspopup="menu"
      aria-expanded={showMenu}
      title="Account"
    >
      <Avatar {identity} size={28} state={avatarState} />
      <span class="identity-stack">
        <span class="identity-line">
          <span class="identity-name">{primaryLabel}</span>
          <RoleChip role={identity?.role} size="xs" />
        </span>
        {#if secondaryLabel}
          <span class="identity-secondary">{secondaryLabel}</span>
        {/if}
      </span>
      <span class="caret" aria-hidden="true">
        <svg viewBox="0 0 12 12" width="10" height="10">
          <path d="M3 4.5l3 3 3-3" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </span>
    </button>
  {:else}
    <button
      class="account-trigger collapsed"
      class:menu-open={showMenu || showSwitcher}
      type="button"
      onclick={toggleMenu}
      aria-haspopup="menu"
      aria-expanded={showMenu}
      title={primaryLabel}
      aria-label="Account: {primaryLabel}"
    >
      <Avatar {identity} size={28} state={avatarState} />
    </button>
  {/if}

  <AccountMenu
    isOpen={showMenu}
    onClose={closeMenu}
    {onOpenSettings}
    onOpenSwitcher={openSwitcher}
    onOpenAddAccount={openAddAccount}
  />
  <AccountSwitcher
    isOpen={showSwitcher}
    onClose={closeSwitcher}
    onAddAccount={openAddAccount}
  />
</div>

<AddAccountSheet isOpen={showAddAccount} onClose={closeAddAccount} />

<style>
  .account-wrapper {
    position: relative;
  }

  .account-trigger {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: 6px var(--spacing-sm);
    color: var(--text-secondary);
    border-radius: var(--radius-md);
    transition: all var(--transition-fast);
    text-align: left;
  }

  .account-trigger:hover,
  .account-trigger.menu-open {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .account-trigger.collapsed {
    width: 40px;
    height: 40px;
    padding: 0;
    justify-content: center;
  }

  .identity-stack {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    gap: 2px;
  }

  .identity-line {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }

  .identity-name {
    flex: 0 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    line-height: 1.2;
  }

  .identity-secondary {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 10px;
    color: var(--text-muted);
    letter-spacing: 0.01em;
    line-height: 1.2;
  }

  .caret {
    color: var(--text-muted);
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: transform var(--transition-fast), color var(--transition-fast);
  }

  .account-trigger.menu-open .caret {
    transform: rotate(180deg);
    color: var(--accent-primary);
  }
</style>
