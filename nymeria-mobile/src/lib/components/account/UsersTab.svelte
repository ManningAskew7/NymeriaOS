<script lang="ts">
  import { untrack } from 'svelte';
  import type { AdminUser, UserRole } from '$lib/types';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import { configStore } from '$lib/stores/config.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Modal from '$lib/components/common/Modal.svelte';
  import Avatar from './Avatar.svelte';
  import RoleChip from './RoleChip.svelte';
  import CopyOnceTokenDialog from './CopyOnceTokenDialog.svelte';
  import TokenManagementSection from './TokenManagementSection.svelte';
  import PlatformLinkingSection from './PlatformLinkingSection.svelte';
  import { identityDisplayName } from './avatar';

  let users = $state<AdminUser[]>([]);
  let loading = $state(false);
  let loadError = $state<string | null>(null);
  let selectedId = $state<string | null>(null);
  let selectedUser = $derived(
    selectedId ? users.find((u) => u.id === selectedId) ?? null : null
  );

  let filterText = $state('');
  let filteredUsers = $derived(
    filterText.trim()
      ? users.filter((u) => {
          const q = filterText.trim().toLowerCase();
          return (
            u.email.toLowerCase().includes(q) ||
            (u.display_name || '').toLowerCase().includes(q) ||
            u.role.toLowerCase().includes(q)
          );
        })
      : users
  );

  let showCreate = $state(false);
  let createEmail = $state('');
  let createDisplayName = $state('');
  let createRole = $state<UserRole>('user');
  let creating = $state(false);
  let createError = $state<string | null>(null);

  let showCopyDialog = $state(false);
  let issuedRawToken = $state<string | null>(null);
  let issuedFor = $state<string | null>(null);
  let issuedLabel = $state<string | null>(null);

  let editName = $state('');
  let editRole = $state<UserRole>('user');
  let saving = $state(false);
  let saveError = $state<string | null>(null);
  let detailDirty = $state(false);

  let togglingDisabled = $state(false);
  let deleting = $state(false);
  let deleteError = $state<string | null>(null);

  async function load() {
    if (loading) return;
    loading = true;
    loadError = null;
    try {
      users = await api.listAdminUsers();
    } catch (e) {
      loadError = humanizeErrorText(e, { action: 'load', resource: 'users' });
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void configStore.identity?.id;
    untrack(() => {
      void load();
    });
  });

  $effect(() => {
    if (!selectedUser) return;
    untrack(() => {
      editName = selectedUser?.display_name ?? '';
      editRole = (selectedUser?.role as UserRole) ?? 'user';
      detailDirty = false;
      saveError = null;
      deleteError = null;
    });
  });

  function openCreate() {
    createEmail = '';
    createDisplayName = '';
    createRole = 'user';
    createError = null;
    showCreate = true;
  }

  function closeCreate() {
    if (creating) return;
    showCreate = false;
  }

  async function handleCreate() {
    if (creating) return;
    if (!createEmail.trim()) {
      createError = 'Enter an email address for the new user.';
      return;
    }
    creating = true;
    createError = null;
    try {
      const body: Parameters<typeof api.createAdminUser>[0] = {
        email: createEmail.trim(),
        role: createRole,
      };
      if (createDisplayName.trim()) body.display_name = createDisplayName.trim();
      const res = await api.createAdminUser(body);
      issuedRawToken = res.raw_token;
      issuedFor = createDisplayName.trim() || createEmail.trim();
      issuedLabel = res.metadata.label;
      showCreate = false;
      showCopyDialog = true;
      await load();
    } catch (e) {
      createError = humanizeErrorText(e, { action: 'create', resource: 'the user' });
    } finally {
      creating = false;
    }
  }

  function closeCopyDialog() {
    showCopyDialog = false;
    issuedRawToken = null;
    issuedFor = null;
    issuedLabel = null;
  }

  function checkDirty() {
    if (!selectedUser) return;
    detailDirty =
      editName.trim() !== (selectedUser.display_name ?? '') ||
      editRole !== selectedUser.role;
  }

  async function handleSaveEdits() {
    if (!selectedUser || saving || !detailDirty) return;
    saving = true;
    saveError = null;
    try {
      const patch: { display_name?: string; role?: UserRole } = {};
      if (editName.trim() !== (selectedUser.display_name ?? '')) {
        patch.display_name = editName.trim();
      }
      if (editRole !== selectedUser.role) {
        patch.role = editRole;
      }
      await api.updateAdminUser(selectedUser.id, patch);
      detailDirty = false;
      await load();
    } catch (e) {
      saveError = humanizeErrorText(e, { action: 'save', resource: 'the user' });
    } finally {
      saving = false;
    }
  }

  async function handleToggleDisabled() {
    if (!selectedUser || togglingDisabled) return;
    const subject = selectedUser.display_name || selectedUser.email;
    const action = selectedUser.disabled ? 're-enable' : 'disable';
    const ok = window.confirm(`${action.charAt(0).toUpperCase() + action.slice(1)} ${subject}?`);
    if (!ok) return;
    togglingDisabled = true;
    saveError = null;
    try {
      await api.updateAdminUser(selectedUser.id, { disabled: !selectedUser.disabled });
      await load();
    } catch (e) {
      saveError = humanizeErrorText(e, { action: 'update', resource: 'the user' });
    } finally {
      togglingDisabled = false;
    }
  }

  async function handleDelete() {
    if (!selectedUser || deleting) return;
    const subject = selectedUser.display_name || selectedUser.email;
    const ok = window.confirm(`Permanently delete ${subject}?`);
    if (!ok) return;
    deleting = true;
    deleteError = null;
    try {
      await api.deleteAdminUser(selectedUser.id);
      selectedId = null;
      await load();
    } catch (e) {
      deleteError = humanizeErrorText(e, { action: 'delete', resource: 'the user' });
    } finally {
      deleting = false;
    }
  }

  function formatRelative(iso: string | null): string {
    if (!iso) return '—';
    try {
      const ms = Date.now() - new Date(iso).getTime();
      if (ms < 60_000) return 'just now';
      if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
      if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
      const days = Math.floor(ms / 86_400_000);
      if (days < 30) return `${days}d ago`;
      return new Date(iso).toLocaleDateString();
    } catch {
      return iso;
    }
  }

  let isMe = $derived(selectedUser?.id === configStore.identity?.id);
</script>

<div class="users-tab">
  {#if !selectedUser}
    <div class="list-header">
      <input
        class="filter-input"
        type="search"
        placeholder="Search users…"
        bind:value={filterText}
      />
      <Button size="sm" onclick={openCreate}>
        <Icon name="plus" size={16} />
        New
      </Button>
    </div>

    {#if loadError}
      <div class="error-banner">
        <Icon name="error" size={14} />
        <span>{loadError}</span>
        <button class="banner-action" type="button" onclick={load}>Retry</button>
      </div>
    {/if}

    {#if loading && users.length === 0}
      <div class="empty-state">
        <Icon name="loading" size={20} />
        <span>Loading users…</span>
      </div>
    {:else if users.length === 0}
      <div class="empty-state">
        <Icon name="users" size={28} />
        <p>You're the only account so far.</p>
        <span>Create another user to delegate access.</span>
      </div>
    {:else if filteredUsers.length === 0}
      <div class="empty-state">
        <Icon name="info" size={20} />
        <span>No users match "{filterText.trim()}".</span>
      </div>
    {:else}
      <div class="user-table">
        {#each filteredUsers as user (user.id)}
          <button
            class="user-row"
            class:disabled={user.disabled}
            type="button"
            onclick={() => (selectedId = user.id)}
          >
            <Avatar identity={user} size={36} state={user.disabled ? 'disabled' : 'connected'} />
            <div class="user-meta">
              <div class="user-line">
                <span class="user-name">{identityDisplayName(user)}</span>
                <RoleChip role={user.role} size="xs" />
                {#if user.disabled}
                  <span class="status-chip disabled-chip">Off</span>
                {/if}
                {#if user.id === configStore.identity?.id}
                  <span class="status-chip you-chip">You</span>
                {/if}
              </div>
              <div class="user-secondary">
                <span class="user-email">{user.email}</span>
              </div>
            </div>
            <Icon name="chevronRight" size={16} />
          </button>
        {/each}
      </div>
    {/if}
  {:else}
    <div class="detail-header">
      <button class="back-btn" type="button" onclick={() => (selectedId = null)} aria-label="Back to users">
        <Icon name="chevronLeft" size={18} />
        <span>Users</span>
      </button>
    </div>

    <div class="detail-card">
      <Avatar identity={selectedUser} size={56} state={selectedUser.disabled ? 'disabled' : 'connected'} />
      <div class="detail-meta">
        <div class="detail-name-row">
          <span class="detail-name">{identityDisplayName(selectedUser)}</span>
          <RoleChip role={selectedUser.role} />
          {#if selectedUser.disabled}
            <span class="status-chip disabled-chip">Disabled</span>
          {/if}
          {#if isMe}
            <span class="status-chip you-chip">You</span>
          {/if}
        </div>
        <span class="detail-email">{selectedUser.email}</span>
      </div>
    </div>

    <section class="detail-section">
      <div class="section-header"><h3 class="section-label">Profile</h3></div>
      <div class="form-stack">
        <label class="form-row">
          <span class="row-label">Display name</span>
          <input
            type="text"
            bind:value={editName}
            oninput={checkDirty}
            disabled={saving}
            maxlength="120"
            placeholder="(none)"
          />
        </label>
        <label class="form-row">
          <span class="row-label">Role</span>
          <select bind:value={editRole} onchange={checkDirty} disabled={saving}>
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
        </label>
      </div>
      <div class="row-actions">
        <Button size="sm" onclick={handleSaveEdits} disabled={!detailDirty || saving}>
          {saving ? 'Saving…' : 'Save'}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onclick={handleToggleDisabled}
          disabled={togglingDisabled}
        >
          {selectedUser.disabled ? 'Enable' : 'Disable'}
        </Button>
      </div>
      {#if saveError}
        <div class="form-error"><Icon name="error" size={14} /><span>{saveError}</span></div>
      {/if}
    </section>

    <section class="detail-section">
      <div class="section-header"><h3 class="section-label">API tokens</h3></div>
      <TokenManagementSection
        mode="admin"
        userId={selectedUser.id}
        userLabel={identityDisplayName(selectedUser)}
      />
    </section>

    <section class="detail-section">
      <div class="section-header"><h3 class="section-label">Linked platforms</h3></div>
      <PlatformLinkingSection
        userId={selectedUser.id}
        userLabel={identityDisplayName(selectedUser)}
      />
    </section>

    <section class="detail-section danger-section">
      <div class="section-header"><h3 class="section-label">Delete user</h3></div>
      <p class="section-hint">
        Cleans up tokens and platform identities. Refuses with 409 if this user
        still owns threads or tasks.
      </p>
      <Button
        variant="ghost"
        onclick={handleDelete}
        disabled={deleting || isMe}
      >
        <Icon name="trash" size={14} />
        {deleting ? 'Deleting…' : isMe ? 'Cannot delete yourself' : 'Delete user'}
      </Button>
      {#if deleteError}
        <div class="form-error"><Icon name="error" size={14} /><span>{deleteError}</span></div>
      {/if}
    </section>
  {/if}
</div>

<Modal title="Create user" isOpen={showCreate} onClose={closeCreate}>
  <div class="create-form">
    <div class="field">
      <label for="create-email">Email</label>
      <input
        id="create-email"
        type="email"
        bind:value={createEmail}
        placeholder="alice@example.com"
        disabled={creating}
        autocomplete="off"
      />
    </div>

    <div class="field">
      <label for="create-display-name">Display name</label>
      <input
        id="create-display-name"
        type="text"
        bind:value={createDisplayName}
        placeholder="(optional)"
        disabled={creating}
        maxlength="120"
        autocomplete="off"
      />
    </div>

    <div class="field">
      <label for="create-role">Role</label>
      <select id="create-role" bind:value={createRole} disabled={creating}>
        <option value="user">user</option>
        <option value="admin">admin</option>
      </select>
    </div>

    {#if createError}
      <div class="form-error">
        <Icon name="error" size={14} />
        <span>{createError}</span>
      </div>
    {/if}

    <div class="actions">
      <Button onclick={handleCreate} disabled={creating}>
        {creating ? 'Creating…' : 'Create user'}
      </Button>
      <Button variant="ghost" onclick={closeCreate} disabled={creating}>Cancel</Button>
    </div>
  </div>
</Modal>

<CopyOnceTokenDialog
  isOpen={showCopyDialog}
  onClose={closeCopyDialog}
  rawToken={issuedRawToken}
  label={issuedLabel}
  forUser={issuedFor}
/>

<style>
  .users-tab {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .list-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .filter-input {
    flex: 1;
    min-width: 0;
    padding: 10px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-md);
  }

  .error-banner {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px var(--spacing-sm);
    background: rgba(var(--error-rgb), 0.08);
    border: 1px solid rgba(var(--error-rgb), 0.3);
    border-radius: var(--radius-sm);
    color: var(--error);
    font-size: var(--font-size-sm);
  }

  .banner-action {
    margin-left: auto;
    color: var(--error);
    background: transparent;
    border: 1px solid currentColor;
    border-radius: var(--radius-sm);
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    padding: var(--spacing-xl) var(--spacing-md);
    color: var(--text-muted);
    text-align: center;
  }

  .empty-state p {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-md);
  }

  .empty-state span {
    font-size: var(--font-size-sm);
  }

  .user-table {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .user-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 12px var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    text-align: left;
    color: var(--text-primary);
  }

  .user-row.disabled {
    opacity: 0.65;
  }

  .user-meta {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .user-line {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    min-width: 0;
  }

  .user-name {
    flex: 0 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-md);
    font-weight: 500;
  }

  .user-secondary {
    display: flex;
    font-size: 12px;
    color: var(--text-muted);
  }

  .user-email {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .status-chip {
    font-size: 9px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 1px 6px;
    border-radius: 4px;
  }

  .status-chip.disabled-chip {
    color: var(--error);
    background: rgba(var(--error-rgb), 0.12);
    border: 1px solid rgba(var(--error-rgb), 0.4);
  }

  .status-chip.you-chip {
    color: var(--accent-primary);
    background: var(--accent-tint-bg);
    border: 1px solid var(--accent-primary);
  }

  .detail-header {
    display: flex;
    align-items: center;
  }

  .back-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 4px;
    color: var(--text-muted);
    background: transparent;
    border-radius: var(--radius-sm);
    font-size: var(--font-size-md);
  }

  .detail-card {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-md);
    padding: var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }

  .detail-meta {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .detail-name-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .detail-name {
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
  }

  .detail-email {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .detail-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .section-header {
    border-bottom: 1px solid var(--border-subtle);
    padding-bottom: var(--spacing-xs);
  }

  .section-header h3 {
    /* type role from global .section-label; keep sm size for section header */
    margin: 0;
    font-size: var(--font-size-sm);
  }

  .section-hint {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    line-height: 1.5;
    /* §5 — multi-line section explanations capped to 70ch so the prose
       stays readable on wide panel layouts. */
    max-width: 70ch;
  }

  .form-stack {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .form-row {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .row-label {
    font-size: 11px;
    color: var(--text-muted);
    font-weight: 600;
  }

  .form-row input,
  .form-row select {
    padding: 10px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-md);
  }

  .row-actions {
    display: flex;
    gap: var(--spacing-sm);
  }

  .form-error {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--error);
    font-size: 12px;
  }

  .danger-section .section-header h3 {
    color: var(--error);
  }

  .create-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: min(360px, 88vw);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .field label {
    font-size: 11px;
    color: var(--text-muted);
    font-weight: 600;
  }

  .field input,
  .field select {
    padding: 10px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-md);
  }

  .actions {
    display: flex;
    gap: var(--spacing-sm);
    align-items: center;
  }
</style>
