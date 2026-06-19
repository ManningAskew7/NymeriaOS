<script lang="ts">
  import { slide } from 'svelte/transition';
  import type { SavedConnection } from '$lib/types';
  import { focusOnMount } from '$lib/actions/focus';
  import { tooltipWhenClipped } from '$lib/actions/tooltip';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import Icon from '$lib/components/common/Icon.svelte';
  import Avatar from './Avatar.svelte';
  import RoleChip from './RoleChip.svelte';
  import { identityDisplayName } from './avatar';

  interface Props {
    isOpen: boolean;
    onClose: () => void;
    onAddAccount: () => void;
  }

  let { isOpen, onClose, onAddAccount }: Props = $props();

  let busyId = $state<string | null>(null);
  // ID of the row currently showing its inline action menu (Re-verify / Edit /
  // Remove). Only one row at a time; clicking the same row toggles closed.
  let openRowMenuId = $state<string | null>(null);
  // ID of the row in inline-rename mode.
  let editingNameId = $state<string | null>(null);
  let nameDraft = $state('');

  function safeHostname(url: string): string {
    try {
      return new URL(url).hostname;
    } catch {
      return url;
    }
  }

  function deriveLabel(entry: SavedConnection): string {
    if (entry.identity) return identityDisplayName(entry.identity);
    if (entry.name) return entry.name;
    return 'Unknown account';
  }

  async function handleSwitch(id: string) {
    if (busyId) return;
    if (id === connectionsStore.activeConnectionId) {
      onClose();
      return;
    }
    busyId = id;
    try {
      await connectionsStore.switchTo(id);
      // Re-resolve identity for the freshly active entry.
      await Promise.all([
        configStore.refreshIdentity(),
        connectionsStore.verifyEntry(id),
      ]);
      onClose();
    } finally {
      busyId = null;
    }
  }

  async function handleVerify(id: string) {
    if (busyId) return;
    busyId = id;
    try {
      await connectionsStore.verifyEntry(id);
    } finally {
      busyId = null;
      openRowMenuId = null;
    }
  }

  function startEditName(entry: SavedConnection) {
    editingNameId = entry.id;
    nameDraft = entry.name;
    openRowMenuId = null;
  }

  function commitEditName(entry: SavedConnection) {
    const next = nameDraft.trim();
    if (next && next !== entry.name) {
      connectionsStore.update(entry.id, { name: next });
    }
    editingNameId = null;
  }

  function cancelEditName() {
    editingNameId = null;
  }

  function handleRemove(entry: SavedConnection) {
    const ok = window.confirm(`Remove "${entry.name}" from saved accounts?`);
    if (!ok) return;
    connectionsStore.delete(entry.id);
    openRowMenuId = null;
  }

  function toggleRowMenu(id: string, e: MouseEvent) {
    e.stopPropagation();
    openRowMenuId = openRowMenuId === id ? null : id;
  }

  function handleClickOutside(e: MouseEvent) {
    const target = e.target as HTMLElement;
    if (!target.closest('.account-switcher') && !target.closest('.account-trigger')) {
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
  <div class="account-switcher" role="menu" transition:slide={DROPDOWN_TRANSITION}>
    <div class="switcher-header">
      <span class="switcher-title">Switch account</span>
      <button
        class="header-action"
        type="button"
        data-tooltip="Close"
        aria-label="Close switcher"
        onclick={onClose}
      >
        <Icon name="x" size={14} />
      </button>
    </div>

    <div class="switcher-body">
      {#if connectionsStore.connections.length === 0}
        <div class="empty-state">
          <span>No saved accounts yet.</span>
          <span class="empty-hint">Add one below to switch between NymeriaOS accounts without re-entering tokens.</span>
        </div>
      {:else}
        {#each connectionsStore.connections as entry (entry.id)}
          {@const isActive = connectionsStore.activeConnectionId === entry.id}
          {@const isBusy = busyId === entry.id}
          {@const isMenuOpen = openRowMenuId === entry.id}
          {@const isEditing = editingNameId === entry.id}
          <div class="row" class:row-active={isActive}>
            <button
              class="row-main"
              type="button"
              onclick={() => handleSwitch(entry.id)}
              disabled={isBusy || connectionsStore.switching}
            >
              <Avatar
                identity={entry.identity}
                size={28}
                state={entry.identity ? 'connected' : entry.identityError ? 'disabled' : 'unverified'}
              />
              <div class="row-text">
                {#if isEditing}
                  <input
                    class="rename-input"
                    type="text"
                    bind:value={nameDraft}
                    onkeydown={(e) => {
                      e.stopPropagation();
                      if (e.key === 'Enter') commitEditName(entry);
                      else if (e.key === 'Escape') cancelEditName();
                    }}
                    onblur={() => commitEditName(entry)}
                    onclick={(e) => e.stopPropagation()}
                    use:focusOnMount
                  />
                {:else}
                  <span class="row-line">
                    <span class="row-name">{deriveLabel(entry)}</span>
                    <RoleChip role={entry.identity?.role} size="xs" />
                  </span>
                  <span class="row-secondary">
                    <span class="row-host">{safeHostname(entry.apiUrl)}</span>
                    {#if entry.identityError}
                      <span class="row-error" use:tooltipWhenClipped={entry.identityError}>{entry.identityError}</span>
                    {:else if !entry.identity}
                      <span class="row-hint">Tap to verify</span>
                    {/if}
                  </span>
                {/if}
              </div>
              {#if isActive}
                <Icon name="check" size={14} />
              {:else if isBusy}
                <Icon name="loading" size={14} />
              {/if}
            </button>

            <button
              class="row-menu-toggle"
              type="button"
              aria-label="More actions"
              onclick={(e) => toggleRowMenu(entry.id, e)}
            >
              <span aria-hidden="true">⋯</span>
            </button>

            {#if isMenuOpen}
              <div class="row-menu" transition:slide={DROPDOWN_TRANSITION}>
                <button class="row-menu-item" type="button" onclick={() => handleVerify(entry.id)}>
                  <Icon name="refresh" size={12} />
                  <span>Re-verify</span>
                </button>
                <button class="row-menu-item" type="button" onclick={() => startEditName(entry)}>
                  <Icon name="edit" size={12} />
                  <span>Rename</span>
                </button>
                <button class="row-menu-item destructive" type="button" onclick={() => handleRemove(entry)}>
                  <Icon name="trash" size={12} />
                  <span>Remove account</span>
                </button>
              </div>
            {/if}
          </div>
        {/each}
      {/if}
    </div>

    <div class="switcher-footer">
      <button class="footer-add" type="button" onclick={onAddAccount}>
        <Icon name="plus" size={14} />
        <span>Add account</span>
      </button>
    </div>
  </div>
{/if}

<style>
  .account-switcher {
    position: absolute;
    bottom: 100%;
    left: 0;
    right: 0;
    margin-bottom: var(--spacing-xs);
    background: var(--bg-elevated-2, var(--bg-elevated));
    border-radius: var(--radius-md);
    /* §7 — floating dropdown: shadow alone defines elevation; border
       would be redundant chrome. Tokenized to --shadow-md (was a bespoke
       0 12px 32px / 0.45) so dropdown elevation stays in sync. */
    box-shadow: var(--shadow-md);
    z-index: 999;
    min-width: 280px;
    max-height: 380px;
    display: flex;
    flex-direction: column;
    /* Open/close motion handled by transition:slide={DROPDOWN_TRANSITION} on
       the element above — one source of truth across the app. */
  }

  .switcher-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 10px 8px var(--spacing-sm);
    border-bottom: 1px solid var(--border-subtle);
  }

  .switcher-title {
    font-size: var(--font-size-2xs);
    font-weight: 600;
    color: var(--text-muted);
  }

  .header-action {
    color: var(--text-muted);
    padding: 4px;
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast), color var(--transition-fast);
  }

  .header-action:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .switcher-body {
    flex: 1;
    overflow-y: auto;
    padding: 4px;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: var(--spacing-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    text-align: center;
  }

  .empty-hint {
    color: var(--text-muted);
    font-size: var(--font-size-2xs);
  }

  .row {
    position: relative;
    display: flex;
    align-items: stretch;
    gap: 2px;
    padding: 0;
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
  }

  .row:hover {
    background: var(--bg-hover);
  }

  .row-active {
    background: var(--accent-tint-bg);
  }

  .row-main {
    flex: 1;
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 8px var(--spacing-sm);
    text-align: left;
    color: var(--text-primary);
    background: transparent;
    border-radius: var(--radius-sm);
    min-width: 0;
  }

  .row-main:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .row-text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .row-line {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }

  .row-name {
    flex: 0 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-sm);
    font-weight: 500;
  }

  .row-secondary {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    color: var(--text-muted);
    font-size: var(--font-size-2xs);
  }

  .row-host {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-hint {
    color: var(--text-muted);
    font-style: italic;
  }

  .row-error {
    color: var(--error);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .rename-input {
    width: 100%;
    padding: 4px 6px;
    background: var(--bg-base);
    border: 1px solid var(--accent-primary);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .rename-input:focus {
    outline: none;
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
  }

  .row-menu-toggle {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    color: var(--text-muted);
    font-size: var(--font-size-base);
    line-height: 1;
    border-radius: var(--radius-sm);
    background: transparent;
    transition: background var(--transition-fast), color var(--transition-fast);
  }

  .row-menu-toggle:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .row-menu {
    position: absolute;
    right: 0;
    top: 100%;
    z-index: 1;
    background: var(--bg-elevated-2, var(--bg-elevated));
    border-radius: var(--radius-sm);
    /* §7 — floating context menu: shadow alone defines elevation; border
       would be redundant chrome. Tokenized to --shadow-md (raw 0 8px 20px
       was nearly identical to the token already). */
    box-shadow: var(--shadow-md);
    padding: 4px;
    min-width: 140px;
  }

  .row-menu-item {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 6px 8px;
    text-align: left;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
  }

  .row-menu-item:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .row-menu-item.destructive {
    color: var(--error);
  }

  .row-menu-item.destructive:hover {
    background: rgba(var(--error-rgb), 0.12);
  }

  .switcher-footer {
    border-top: 1px solid var(--border-subtle);
    padding: 4px;
  }

  .footer-add {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    width: 100%;
    padding: 8px;
    color: var(--accent-primary);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border-radius: var(--radius-sm);
    background: transparent;
    transition: background var(--transition-fast);
  }

  .footer-add:hover {
    background: var(--bg-hover);
  }
</style>
