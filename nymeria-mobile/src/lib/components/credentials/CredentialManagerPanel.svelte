<script lang="ts">
  import { credentialsStore } from '$lib/stores/credentials.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import type { Credential, CredentialOwnerType } from '$lib/types';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';

  let showForm = $state(false);
  let completingId = $state<string | null>(null);
  let formName = $state('');
  let formProvider = $state('api');
  let formKind = $state('api_key');
  let formOwnerType = $state<CredentialOwnerType>('user');
  let formSecretField = $state('value');
  let formSecretValue = $state('');
  let formAllowedTarget = $state('');
  let formStatus = $state<'idle' | 'saving' | 'error'>('idle');
  let formError = $state('');

  let isAdmin = $derived(configStore.identity?.role === 'admin');
  let visibleCredentials = $derived(credentialsStore.credentials.filter((c) => c.status !== 'disabled'));
  let pendingCredentials = $derived(visibleCredentials.filter((c) => c.status === 'pending_setup'));

  $effect(() => {
    if (!credentialsStore.loaded && !credentialsStore.loading) {
      credentialsStore.load();
    }
  });

  function resetForm() {
    showForm = false;
    completingId = null;
    formName = '';
    formProvider = 'api';
    formKind = 'api_key';
    formOwnerType = 'user';
    formSecretField = 'value';
    formSecretValue = '';
    formAllowedTarget = '';
    formStatus = 'idle';
    formError = '';
  }

  function startCreate() {
    resetForm();
    showForm = true;
  }

  function startComplete(credential: Credential) {
    resetForm();
    completingId = credential.id;
    formName = credential.name;
    formProvider = credential.provider;
    formKind = credential.kind;
    formOwnerType = credential.ownerType;
    const required = credential.metadata?.required_fields;
    formSecretField = Array.isArray(required) && required.length > 0 ? String(required[0]) : 'value';
    formAllowedTarget = credential.allowedTargets[0] || '';
    showForm = true;
  }

  function splitTarget(target: string): { target_type?: string; target_id?: string } {
    const idx = target.indexOf(':');
    if (idx <= 0) return {};
    return {
      target_type: target.slice(0, idx),
      target_id: target.slice(idx + 1),
    };
  }

  async function saveCredential() {
    formStatus = 'saving';
    formError = '';
    const allowed = formAllowedTarget.trim() ? [formAllowedTarget.trim()] : [];
    const secret_fields = formSecretValue ? { [formSecretField.trim() || 'value']: formSecretValue } : {};
    const result = completingId
      ? await credentialsStore.update(completingId, {
          name: formName.trim(),
          status: Object.keys(secret_fields).length > 0 ? 'active' : 'pending_setup',
          allowed_targets: allowed,
          secret_fields,
        })
      : await credentialsStore.create({
          owner_type: formOwnerType,
          name: formName.trim(),
          provider: formProvider.trim(),
          kind: formKind.trim(),
          status: Object.keys(secret_fields).length > 0 ? 'active' : 'pending_setup',
          allowed_targets: allowed,
          secret_fields,
          metadata: { source: 'settings_connections' },
        });
    if (!result) {
      formStatus = 'error';
      formError = credentialsStore.error || 'Could not save credential';
      return;
    }
    if (formAllowedTarget.trim()) {
      const target = splitTarget(formAllowedTarget.trim());
      if (target.target_type && target.target_id) {
        await credentialsStore.bind(result.id, {
          target_type: target.target_type,
          target_id: target.target_id,
          binding_name: 'default',
        });
      }
    }
    resetForm();
  }

  function statusLabel(credential: Credential): string {
    if (credential.status === 'pending_setup') return 'setup';
    return credential.status;
  }

  function providerLabel(value: string): string {
    return value.replace(/_/g, ' ');
  }
</script>

<div class="credentials-panel">
  <div class="panel-actions">
    <Button size="sm" variant="ghost" onclick={() => credentialsStore.refresh()} disabled={credentialsStore.loading}>
      <Icon name="refresh" size={14} /> Refresh
    </Button>
    <Button size="sm" variant="primary" onclick={startCreate}>
      <Icon name="plus" size={14} /> Add
    </Button>
  </div>

  {#if credentialsStore.error}
    <div class="error-row">
      <Icon name="error" size={15} />
      <span>{credentialsStore.error}</span>
    </div>
  {/if}

  {#if pendingCredentials.length > 0}
    <div class="pending-strip">
      <Icon name="warning" size={16} />
      <span>{pendingCredentials.length} pending setup</span>
    </div>
  {/if}

  {#if showForm}
    <div class="credential-form">
      <div class="form-grid">
        <label>
          <span>Name</span>
          <input bind:value={formName} placeholder="Linear API" />
        </label>
        <label>
          <span>Provider</span>
          <input bind:value={formProvider} placeholder="linear" disabled={completingId !== null} />
        </label>
        <label>
          <span>Kind</span>
          <input bind:value={formKind} placeholder="api_key" disabled={completingId !== null} />
        </label>
        {#if isAdmin && completingId === null}
          <label>
            <span>Owner</span>
            <select bind:value={formOwnerType}>
              <option value="user">User</option>
              <option value="system">System</option>
            </select>
          </label>
        {/if}
        <label>
          <span>Secret Field</span>
          <input bind:value={formSecretField} placeholder="value" />
        </label>
        <label>
          <span>Secret</span>
          <input type="password" bind:value={formSecretValue} autocomplete="off" spellcheck="false" />
        </label>
        <label class="wide">
          <span>Allowed Target</span>
          <input bind:value={formAllowedTarget} placeholder="native_tool:nasa_apod, mcp_server:gmail, or custom_tool:linear_search" />
        </label>
      </div>
      {#if formError}
        <div class="error-row">
          <Icon name="error" size={15} />
          <span>{formError}</span>
        </div>
      {/if}
      <div class="form-actions">
        <Button size="sm" variant="ghost" onclick={resetForm}>Cancel</Button>
        <Button size="sm" variant="primary" onclick={saveCredential} disabled={!formName.trim() || formStatus === 'saving'} loading={formStatus === 'saving'}>
          Save
        </Button>
      </div>
    </div>
  {/if}

  <div class="credential-list">
    {#if credentialsStore.loading && visibleCredentials.length === 0}
      <div class="empty-state">
        <Icon name="loading" size={20} />
        <span>Loading credentials…</span>
      </div>
    {:else if visibleCredentials.length === 0}
      <div class="empty-state">
        <Icon name="key" size={24} />
        <p>No credentials saved yet.</p>
        <span>Save API keys and tokens here. They're stored locally and shared only with the tool or MCP server you scope them to.</span>
        <div class="empty-cta">
          <Button onclick={startCreate}>
            <Icon name="plus" size={14} />
            Add your first credential
          </Button>
        </div>
      </div>
    {:else}
      {#each visibleCredentials as credential}
        <div class="credential-row">
          <div class="credential-main">
            <div class="credential-title">
              <span>{credential.name}</span>
              <span class="status-pill status-{credential.status}">{statusLabel(credential)}</span>
              <span class="owner-pill">{credential.ownerType}</span>
            </div>
            <div class="credential-meta">
              <span>{providerLabel(credential.provider)}</span>
              <span>{credential.kind}</span>
              {#if credential.accountLabel}<span>{credential.accountLabel}</span>{/if}
              {#if credential.secretFields.length}<span>{credential.secretFields.join(', ')}</span>{/if}
            </div>
            {#if credential.allowedTargets.length > 0}
              <div class="target-list">
                {#each credential.allowedTargets as target}
                  <code>{target}</code>
                {/each}
              </div>
            {/if}
          </div>
          <div class="credential-actions">
            {#if credential.status === 'pending_setup'}
              <button type="button" title="Complete setup" onclick={() => startComplete(credential)}>
                <Icon name="edit" size={15} />
              </button>
            {/if}
            <button type="button" title="Test credential" onclick={() => credentialsStore.test(credential.id)}>
              <Icon name="check" size={15} />
            </button>
            <button type="button" title="Disable credential" onclick={() => credentialsStore.disable(credential.id)}>
              <Icon name="trash" size={15} />
            </button>
          </div>
        </div>
      {/each}
    {/if}
  </div>
</div>

<style>
  .credentials-panel {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .panel-actions,
  .form-actions,
  .credential-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .panel-actions {
    justify-content: flex-end;
  }

  .error-row,
  .pending-strip {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .error-row {
    color: var(--error);
    background: rgba(var(--error-rgb), 0.1);
    border: 1px solid rgba(var(--error-rgb), 0.25);
  }

  .pending-strip {
    color: var(--warning);
    background: rgba(var(--warning-rgb), 0.1);
    border: 1px solid rgba(var(--warning-rgb), 0.25);
  }

  .credential-form {
    padding: var(--spacing-md);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
  }

  .form-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--spacing-sm);
  }

  .form-grid label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .form-grid label.wide {
    grid-column: 1 / -1;
  }

  input,
  select {
    width: 100%;
    padding: var(--spacing-sm);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    background: var(--bg-base);
    color: var(--text-primary);
  }

  .form-actions {
    justify-content: flex-end;
    margin-top: var(--spacing-md);
  }

  .credential-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .credential-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-md);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
  }

  .credential-main {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .credential-title,
  .credential-meta,
  .target-list {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
  }

  .credential-title {
    font-weight: 600;
    color: var(--text-primary);
  }

  .credential-meta {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .status-pill,
  .owner-pill {
    padding: 2px 7px;
    /* §3 — text chips use --radius-sm. Full pill is reserved for count
       badges and dot indicators, not status / owner labels. */
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    font-weight: 600;
  }

  .status-pill {
    color: var(--success);
    background: rgba(var(--success-rgb), 0.12);
  }

  .status-pending_setup {
    color: var(--warning);
    background: rgba(var(--warning-rgb), 0.14);
  }

  .owner-pill {
    color: var(--text-secondary);
    background: var(--bg-base);
    border: 1px solid var(--border-subtle);
  }

  .target-list code {
    padding: 2px 6px;
    border-radius: var(--radius-sm);
    background: var(--bg-base);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .credential-actions button {
    width: 30px;
    height: 30px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
  }

  .credential-actions button:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    padding: var(--spacing-xl) var(--spacing-md);
    text-align: center;
    color: var(--text-muted);
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

  @media (max-width: 720px) {
    .form-grid,
    .credential-row {
      grid-template-columns: 1fr;
    }

    .credential-row {
      flex-direction: column;
    }
  }
</style>
