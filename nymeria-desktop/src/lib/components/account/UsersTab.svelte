<script lang="ts">
  import { untrack } from 'svelte';
  import type { AccountIdentity, AdminUser, UserRole } from '$lib/types';
  import { api } from '$lib/services/api.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Modal from '$lib/components/common/Modal.svelte';
  import Avatar from './Avatar.svelte';
  import RoleChip from './RoleChip.svelte';
  import CopyOnceTokenDialog from './CopyOnceTokenDialog.svelte';
  import TokenManagementSection from './TokenManagementSection.svelte';
  import PlatformLinkingSection from './PlatformLinkingSection.svelte';
  import { identityDisplayName } from './avatar';

  // Master/detail state
  let users = $state<AdminUser[]>([]);
  let loading = $state(false);
  let loadError = $state<string | null>(null);
  let selectedId = $state<string | null>(null);
  let selectedUser = $derived(
    selectedId ? users.find((u) => u.id === selectedId) ?? null : null
  );

  // Filter
  let filterText = $state('');
  let filteredUsers = $derived(
    filterText.trim()
      ? users.filter((u) => {
          const q = filterText.trim().toLowerCase();
          return (
            u.email.toLowerCase().includes(q) ||
            (u.display_name || '').toLowerCase().includes(q) ||
            u.role.toLowerCase().includes(q) ||
            u.id.toLowerCase().includes(q)
          );
        })
      : users
  );

  // Create user dialog
  let showCreate = $state(false);
  let createEmail = $state('');
  let createDisplayName = $state('');
  let createRole = $state<UserRole>('user');
  let createId = $state('');
  let createTokenLabel = $state('');
  let creating = $state(false);
  let createError = $state<string | null>(null);

  // Copy-once after create
  let showCopyDialog = $state(false);
  let issuedRawToken = $state<string | null>(null);
  let issuedFor = $state<string | null>(null);
  let issuedLabel = $state<string | null>(null);
  let issuedIdentity = $state<AccountIdentity | null>(null);
  let savingIssuedAccount = $state(false);
  let issuedAccountMessage = $state<string | null>(null);
  let issuedAccountError = $state<string | null>(null);

  // Detail-pane edit state
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
      loadError = e instanceof Error ? e.message : 'Failed to load users';
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

  // Reload selected user when the list refreshes — keeps thread/todo counts
  // current after edits without an extra GET /admin/users/{id}.
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
    createId = '';
    createTokenLabel = '';
    createError = null;
    showCreate = true;
  }

  function closeCreate() {
    if (creating) return;
    showCreate = false;
  }

  async function handleCreate() {
    if (creating) return;
    const requestedEmail = createEmail.trim().toLowerCase();
    const requestedId = createId.trim();
    if (!requestedEmail) {
      createError = 'Email is required';
      return;
    }
    creating = true;
    createError = null;
    try {
      const body: Parameters<typeof api.createAdminUser>[0] = {
        email: requestedEmail,
        role: createRole,
      };
      if (createDisplayName.trim()) body.display_name = createDisplayName.trim();
      if (requestedId) body.id = requestedId;
      if (createTokenLabel.trim()) body.token_label = createTokenLabel.trim();
      const res = await api.createAdminUser(body);
      await load();
      const created =
        users.find(
          (u) =>
            (requestedId && u.id === requestedId) ||
            u.email.toLowerCase() === requestedEmail
        ) ?? null;
      issuedRawToken = res.raw_token;
      issuedFor = createDisplayName.trim() || requestedEmail;
      issuedLabel = res.metadata.label;
      issuedIdentity = created
        ? {
            id: created.id,
            email: created.email,
            display_name: created.display_name,
            role: created.role,
          }
        : null;
      issuedAccountMessage = null;
      issuedAccountError = null;
      showCreate = false;
      showCopyDialog = true;
    } catch (e) {
      createError = e instanceof Error ? e.message : 'Failed to create user';
    } finally {
      creating = false;
    }
  }

  function closeCopyDialog() {
    showCopyDialog = false;
    issuedRawToken = null;
    issuedFor = null;
    issuedLabel = null;
    issuedIdentity = null;
    issuedAccountMessage = null;
    issuedAccountError = null;
  }

  async function saveIssuedCreatedAccount(switchAfterSave = false) {
    if (!issuedRawToken || savingIssuedAccount) return;
    if (!issuedIdentity) {
      issuedAccountError = 'Cannot save this token because the new account identity is not loaded.';
      return;
    }
    savingIssuedAccount = true;
    issuedAccountMessage = null;
    issuedAccountError = null;
    try {
      const entry = connectionsStore.upsertAccountCredential({
        apiUrl: configStore.apiUrl,
        apiKey: issuedRawToken,
        identity: issuedIdentity,
        name: identityDisplayName(issuedIdentity),
      });
      if (switchAfterSave) {
        await connectionsStore.switchTo(entry.id);
        closeCopyDialog();
      } else {
        issuedAccountMessage = 'Saved to the account switcher.';
      }
    } catch (e) {
      issuedAccountError = e instanceof Error ? e.message : 'Failed to save account';
    } finally {
      savingIssuedAccount = false;
    }
  }

  function handleSelect(id: string) {
    selectedId = id;
  }

  function handleBack() {
    selectedId = null;
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
      saveError = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  async function handleToggleDisabled() {
    if (!selectedUser || togglingDisabled) return;
    const subject = selectedUser.display_name || selectedUser.email;
    const action = selectedUser.disabled ? 're-enable' : 'disable';
    const ok = window.confirm(
      `${action.charAt(0).toUpperCase() + action.slice(1)} ${subject}? ${selectedUser.disabled ? "They'll be able to sign in again." : "Their next request returns 401 and any active session ends."}`
    );
    if (!ok) return;
    togglingDisabled = true;
    saveError = null;
    try {
      await api.updateAdminUser(selectedUser.id, { disabled: !selectedUser.disabled });
      await load();
    } catch (e) {
      saveError = e instanceof Error ? e.message : `Failed to ${action} user`;
    } finally {
      togglingDisabled = false;
    }
  }

  async function handleDelete() {
    if (!selectedUser || deleting) return;
    const subject = selectedUser.display_name || selectedUser.email;
    const ok = window.confirm(
      `Permanently delete ${subject}? Tokens and platform identities are cleaned up automatically. The backend refuses with 409 if this user still owns threads or todos; you'll need to re-assign or remove those first.`
    );
    if (!ok) return;
    deleting = true;
    deleteError = null;
    try {
      await api.deleteAdminUser(selectedUser.id);
      selectedId = null;
      await load();
    } catch (e) {
      deleteError = e instanceof Error ? e.message : 'Failed to delete user';
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
      <div class="header-left">
        <input
          class="filter-input"
          type="search"
          placeholder="Search by email, name, role…"
          bind:value={filterText}
        />
      </div>
      <Button size="sm" onclick={openCreate}>
        <Icon name="plus" size={14} />
        New user
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
        <Icon name="users" size={24} />
        <p>You're the only account so far.</p>
        <span>Add another user to delegate access for bots, family, or collaborators…</span>
        <div class="empty-cta">
          <Button onclick={openCreate}>
            <Icon name="plus" size={14} />
            Create your first user
          </Button>
        </div>
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
            onclick={() => handleSelect(user.id)}
          >
            <Avatar identity={user} size={32} state={user.disabled ? 'disabled' : 'connected'} />
            <div class="user-meta">
              <div class="user-line">
                <span class="user-name">{identityDisplayName(user)}</span>
                <RoleChip role={user.role} size="xs" />
                {#if user.disabled}
                  <span class="status-chip disabled-chip">Disabled</span>
                {/if}
                {#if user.id === configStore.identity?.id}
                  <span class="status-chip you-chip">You</span>
                {/if}
              </div>
              <div class="user-secondary">
                <span class="user-email">{user.email}</span>
                <span class="dot">·</span>
                <span>
                  {user.token_count} token{user.token_count === 1 ? '' : 's'}
                </span>
                <span class="dot">·</span>
                <span title={user.last_token_use ?? 'never'}>
                  {user.last_token_use ? `Last seen ${formatRelative(user.last_token_use)}` : 'Never seen'}
                </span>
              </div>
            </div>
            <Icon name="chevronRight" size={16} />
          </button>
        {/each}
      </div>
    {/if}
  {:else}
    <!-- Detail view -->
    <div class="detail-header">
      <button class="back-btn" type="button" onclick={handleBack} aria-label="Back to users">
        <Icon name="chevronLeft" size={16} />
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
        <span class="detail-id">id: <code>{selectedUser.id}</code></span>
      </div>
    </div>

    <section class="detail-section">
      <div class="section-header"><h4>Profile</h4></div>
      <div class="form-grid">
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
          {saving ? 'Saving…' : 'Save changes'}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onclick={handleToggleDisabled}
          disabled={togglingDisabled}
        >
          <Icon name={selectedUser.disabled ? 'check' : 'x'} size={14} />
          {selectedUser.disabled ? 'Enable account' : 'Disable account'}
        </Button>
      </div>
      {#if saveError}
        <div class="form-error"><Icon name="error" size={14} /><span>{saveError}</span></div>
      {/if}
    </section>

    <section class="detail-section">
      <div class="section-header"><h4>API tokens</h4></div>
      <TokenManagementSection
        mode="admin"
        userId={selectedUser.id}
        userLabel={identityDisplayName(selectedUser)}
        targetIdentity={selectedUser}
      />
    </section>

    <section class="detail-section">
      <div class="section-header"><h4>Linked platforms</h4></div>
      <PlatformLinkingSection
        userId={selectedUser.id}
        userLabel={identityDisplayName(selectedUser)}
      />
    </section>

    <section class="detail-section danger-section">
      <div class="section-header"><h4>Delete user</h4></div>
      <p class="section-hint">
        Cleans up tokens and platform identities automatically. The backend
        refuses with 409 if this user still owns threads or todos; re-assign
        or remove those first.
      </p>
      {#if (selectedUser.thread_count ?? 0) + (selectedUser.todo_count ?? 0) > 0}
        <div class="ownership-note">
          <Icon name="warning" size={14} />
          <span>
            Currently owns
            {#if (selectedUser.thread_count ?? 0) > 0}
              {selectedUser.thread_count} thread{selectedUser.thread_count === 1 ? '' : 's'}
            {/if}
            {#if (selectedUser.thread_count ?? 0) > 0 && (selectedUser.todo_count ?? 0) > 0}
              and
            {/if}
            {#if (selectedUser.todo_count ?? 0) > 0}
              {selectedUser.todo_count} todo{selectedUser.todo_count === 1 ? '' : 's'}
            {/if}
            ; clean these up before deleting.
          </span>
        </div>
      {/if}
      <Button
        variant="ghost"
        onclick={handleDelete}
        disabled={deleting || isMe}
        title={isMe ? "Can't delete yourself" : 'Permanently delete this user'}
      >
        <Icon name="trash" size={14} />
        {deleting ? 'Deleting…' : isMe ? 'Cannot delete yourself' : `Delete ${identityDisplayName(selectedUser)}`}
      </Button>
      {#if deleteError}
        <div class="form-error"><Icon name="error" size={14} /><span>{deleteError}</span></div>
      {/if}
    </section>
  {/if}
</div>

<!-- Create user modal -->
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
      <p class="hint">Identifies the account; must be unique.</p>
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
      <p class="hint">Admins can manage other users and call the protected /admin endpoints.</p>
    </div>

    <details class="advanced">
      <summary>Advanced</summary>
      <div class="field nested">
        <label for="create-id">Custom user ID</label>
        <input
          id="create-id"
          type="text"
          bind:value={createId}
          placeholder="(auto-generated from email)"
          disabled={creating}
          maxlength="80"
          autocomplete="off"
        />
        <p class="hint">Used in file paths under data/. Only set if you have a strong reason.</p>
      </div>
      <div class="field nested">
        <label for="create-token-label">Initial token label</label>
        <input
          id="create-token-label"
          type="text"
          bind:value={createTokenLabel}
          placeholder="bootstrap"
          disabled={creating}
          maxlength="80"
          autocomplete="off"
        />
      </div>
    </details>

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
  onSaveAccount={() => saveIssuedCreatedAccount(false)}
  onSaveAndSwitch={() => saveIssuedCreatedAccount(true)}
  savingAccount={savingIssuedAccount}
  accountActionMessage={issuedAccountMessage}
  accountActionError={issuedAccountError}
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

  .header-left {
    flex: 1;
    min-width: 0;
  }

  .filter-input {
    width: 100%;
    padding: 6px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .filter-input:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(34, 211, 238, 0.15));
  }

  .error-banner {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px var(--spacing-sm);
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.3);
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
    padding: 2px 8px;
    font-size: var(--font-size-2xs);
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

  .empty-cta {
    margin-top: var(--spacing-sm);
  }

  .user-table {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .user-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 10px var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    text-align: left;
    transition: all var(--transition-fast);
    color: var(--text-primary);
  }

  .user-row:hover {
    background: var(--bg-hover);
    border-color: var(--border-default);
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
    font-size: var(--font-size-sm);
    font-weight: 500;
  }

  .user-secondary {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    flex-wrap: wrap;
  }

  .user-email {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dot {
    color: var(--text-muted);
  }

  .status-chip {
    display: inline-block;
    font-size: var(--font-size-3xs);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    text-indent: 0.06em;
    padding: 1px 6px;
    /* §3 — text chip uses --radius-sm token, not full pill. */
    border-radius: var(--radius-sm);
  }

  .status-chip.disabled-chip {
    color: var(--error);
    background: rgba(239, 68, 68, 0.12);
    border: 1px solid rgba(239, 68, 68, 0.4);
  }

  .status-chip.you-chip {
    color: var(--accent-primary);
    background: var(--accent-primary-alpha, rgba(34, 211, 238, 0.12));
    border: 1px solid var(--accent-primary);
  }

  /* Detail view */
  .detail-header {
    display: flex;
    align-items: center;
    margin-bottom: var(--spacing-xs);
  }

  .back-btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px var(--spacing-xs);
    color: var(--text-muted);
    background: transparent;
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
    transition: color var(--transition-fast);
  }

  .back-btn:hover {
    color: var(--text-primary);
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
    min-width: 0;
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

  .detail-id {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
  }

  .detail-id code {
    font-family: var(--font-mono);
    background: var(--bg-base);
    padding: 1px 5px;
    border-radius: 3px;
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

  .section-header h4 {
    margin: 0;
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .section-hint {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--spacing-sm);
  }

  .form-row {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .row-label {
    font-size: var(--font-size-2xs);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    font-weight: 600;
  }

  .form-row input,
  .form-row select {
    padding: 6px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .form-row input:focus,
  .form-row select:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(34, 211, 238, 0.15));
  }

  .row-actions {
    display: flex;
    gap: var(--spacing-sm);
    align-items: center;
  }

  .form-error {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--error);
    font-size: var(--font-size-xs);
  }

  .ownership-note {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 10px var(--spacing-sm);
    background: rgba(251, 191, 36, 0.1);
    border: 1px solid rgba(251, 191, 36, 0.4);
    border-radius: var(--radius-sm);
    color: var(--warning, #fbbf24);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .danger-section .section-header h4 {
    color: var(--error);
  }

  /* Create form */
  .create-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: min(420px, 90vw);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .field.nested {
    margin-top: var(--spacing-sm);
  }

  .field label {
    font-size: var(--font-size-2xs);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    font-weight: 600;
  }

  .field input,
  .field select {
    padding: 8px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  .advanced {
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: var(--spacing-sm);
  }

  .advanced summary {
    cursor: pointer;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    user-select: none;
  }

  .actions {
    display: flex;
    gap: var(--spacing-sm);
    align-items: center;
  }
</style>
