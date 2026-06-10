<script lang="ts">
  import { trapFocus } from '$lib/actions/focus';
  import { configStore } from '$lib/stores/config.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Avatar from './Avatar.svelte';
  import RoleChip from './RoleChip.svelte';
  import { identityDisplayName } from './avatar';

  interface Props {
    isOpen: boolean;
    onClose: () => void;
    onOpenSettings: (tab?: string) => void;
  }

  let { isOpen, onClose, onOpenSettings }: Props = $props();

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

  function handleManageConnection() {
    onClose();
    onOpenSettings('connection');
  }

  function handleSignOut() {
    const ok = window.confirm(
      'This will sign you out and return to the setup wizard. Continue?'
    );
    if (!ok) return;
    configStore.signOut();
    onClose();
  }

  function handleBackdropClick(e: MouseEvent) {
    if (e.target === e.currentTarget) onClose();
  }

  // §6 a11y — Escape closes the bottom-sheet modal while open. Window
  // listener (vs onkeydown on the backdrop) so the handler fires no
  // matter where focus currently sits inside the sheet.
  $effect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  });
</script>

{#if isOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="account-backdrop" onclick={handleBackdropClick}>
    <div class="account-sheet" role="dialog" aria-modal="true" aria-label="Account" tabindex="-1" use:trapFocus>
      <div class="sheet-handle" aria-hidden="true"></div>
      <div class="sheet-header">
        <Avatar {identity} size={56} state={identity ? 'connected' : 'unverified'} />
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

      <div class="sheet-actions">
        <button class="sheet-item" type="button" onclick={handleManageAccount}>
          <Icon name="user" size={18} />
          <span>Manage account</span>
        </button>

        {#if isAdmin}
          <button class="sheet-item" type="button" onclick={handleManageUsers}>
            <Icon name="users" size={18} />
            <span>Manage users</span>
            <span class="item-badge">admin</span>
          </button>
        {/if}

        <button class="sheet-item" type="button" onclick={handleManageConnection}>
          <Icon name="server" size={18} />
          <span>Connection settings</span>
        </button>

        <button class="sheet-item destructive" type="button" onclick={handleSignOut}>
          <Icon name="x" size={18} />
          <span>Sign out</span>
        </button>
      </div>

      <button class="sheet-cancel" type="button" onclick={onClose}>Cancel</button>
    </div>
  </div>
{/if}

<style>
  .account-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 1000;
    display: flex;
    align-items: flex-end;
    justify-content: center;
    animation: fade-in var(--transition-fast);
  }

  @keyframes fade-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .account-sheet {
    width: 100%;
    background: var(--bg-elevated);
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    padding: 8px var(--spacing-md) calc(var(--safe-area-bottom) + var(--spacing-md));
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    animation: sheet-up 220ms cubic-bezier(0.16, 1, 0.3, 1);
    border-top: 1px solid var(--border-subtle);
    max-height: 80dvh;
    overflow-y: auto;
  }

  @keyframes sheet-up {
    from { transform: translateY(100%); }
    to { transform: translateY(0); }
  }

  .sheet-handle {
    align-self: center;
    width: 36px;
    height: 4px;
    border-radius: 2px;
    background: var(--border-default);
    margin-bottom: var(--spacing-sm);
  }

  .sheet-header {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-xs) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
  }

  .header-meta {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .header-name-row {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .header-name {
    flex: 0 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
  }

  .header-email {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .header-host {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 11px;
    color: var(--text-muted);
    letter-spacing: 0.02em;
  }

  .sheet-actions {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .sheet-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: 14px var(--spacing-sm);
    color: var(--text-secondary);
    font-size: var(--font-size-md);
    text-align: left;
    border-radius: var(--radius-sm);
    background: transparent;
    transition: background var(--transition-fast);
  }

  .sheet-item span {
    flex: 1;
  }

  .sheet-item:active {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .sheet-item.destructive {
    color: var(--error);
  }

  .item-badge {
    flex: 0 !important;
    font-size: 9px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--warning);
    background: rgba(var(--warning-rgb), 0.12);
    border: 1px solid rgba(var(--warning-rgb), 0.4);
    padding: 1px 5px;
    border-radius: 4px;
  }

  .sheet-cancel {
    margin-top: var(--spacing-sm);
    padding: 14px;
    border-radius: var(--radius-md);
    background: var(--bg-base);
    color: var(--text-primary);
    font-size: var(--font-size-md);
    font-weight: 500;
    text-align: center;
  }

  .sheet-cancel:active {
    background: var(--bg-hover);
  }
</style>
