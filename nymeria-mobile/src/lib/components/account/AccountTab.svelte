<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Avatar from './Avatar.svelte';
  import RoleChip from './RoleChip.svelte';
  import TokenManagementSection from './TokenManagementSection.svelte';
  import PlatformLinkingSection from './PlatformLinkingSection.svelte';
  import { identityDisplayName } from './avatar';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';

  let identity = $derived(configStore.identity);
  let isAdmin = $derived(identity?.role === 'admin');

  let editingName = $state(false);
  let nameDraft = $state('');
  let saving = $state(false);
  let nameError = $state<string | null>(null);
  let nameSaved = $state(false);

  function startEdit() {
    nameDraft = identity?.display_name ?? '';
    nameError = null;
    nameSaved = false;
    editingName = true;
  }

  function cancelEdit() {
    editingName = false;
    nameError = null;
  }

  async function saveName() {
    if (saving) return;
    nameError = null;
    saving = true;
    try {
      await configStore.updateIdentityDisplayName(nameDraft);
      editingName = false;
      nameSaved = true;
      setTimeout(() => (nameSaved = false), 2200);
    } catch (e) {
      nameError = humanizeErrorText(e, { action: 'save', resource: 'your name' });
    } finally {
      saving = false;
    }
  }

  function handleSignOut() {
    const ok = window.confirm(
      'This will sign you out and return to the setup wizard. Continue?'
    );
    if (!ok) return;
    configStore.signOut();
  }
</script>

<div class="account-tab">
  {#if !identity}
    <div class="empty-state">
      <Icon name="user" size={32} />
      <p>Not connected to a Nymeria server.</p>
      <span>Switch to the Connection tab to set an API URL and token.</span>
    </div>
  {:else}
    <section class="section">
      <div class="section-header">
        <h3>Identity</h3>
        <RoleChip role={identity.role} />
      </div>

      <div class="identity-card">
        <Avatar {identity} size={56} state="connected" />
        <div class="identity-fields">
          <div class="field">
            <label class="field-label">Email</label>
            <div class="field-value readonly">{identity.email}</div>
          </div>

          <div class="field">
            <label class="field-label" for="display-name">Display name</label>
            {#if editingName}
              <input
                id="display-name"
                class="field-input"
                type="text"
                bind:value={nameDraft}
                disabled={saving}
                maxlength="120"
                placeholder="How should Nymeria refer to you?"
              />
              <div class="row-actions">
                <Button size="sm" onclick={saveName} disabled={saving}>
                  {saving ? 'Saving…' : 'Save'}
                </Button>
                <Button variant="ghost" size="sm" onclick={cancelEdit} disabled={saving}>
                  Cancel
                </Button>
              </div>
            {:else}
              <div class="field-edit-row">
                <div class="field-value">{identity.display_name || '(none)'}</div>
                <Button variant="ghost" size="sm" onclick={startEdit}>
                  <Icon name="edit" size={14} />
                </Button>
                {#if nameSaved}
                  <span class="saved-pill"><Icon name="check" size={12} /> Saved</span>
                {/if}
              </div>
            {/if}
            {#if nameError}
              <div class="field-error"><Icon name="error" size={12} />{nameError}</div>
            {/if}
          </div>

          <div class="field">
            <label class="field-label">Account ID</label>
            <div class="field-value readonly mono">{identity.id}</div>
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="section-header">
        <h3>API tokens</h3>
      </div>
      <p class="section-hint">
        Each token is a separate sign-in. Issue one for every device, then
        revoke them individually if needed.
      </p>
      <TokenManagementSection mode="self" />
    </section>

    {#if isAdmin}
      <section class="section">
        <div class="section-header">
          <h3>Linked platforms</h3>
        </div>
        <p class="section-hint">
          Chat-platform IDs that route to this account.
        </p>
        <PlatformLinkingSection
          userId={identity.id}
          userLabel={identityDisplayName(identity)}
        />
      </section>
    {/if}

    <section class="section danger-section">
      <div class="section-header">
        <h3>Sign out</h3>
      </div>
      <p class="section-hint">
        Returns to the setup wizard. Your saved API config is cleared.
      </p>
      <Button variant="ghost" onclick={handleSignOut}>
        <Icon name="x" size={14} />
        Sign out
      </Button>
    </section>
  {/if}
</div>

<style>
  .account-tab {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
    padding-bottom: var(--spacing-lg);
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-xl);
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

  .section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm);
    border-bottom: 1px solid var(--border-subtle);
    padding-bottom: var(--spacing-xs);
  }

  .section-header h3 {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
  }

  .section-hint {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    line-height: 1.5;
  }

  .identity-card {
    display: flex;
    gap: var(--spacing-md);
    align-items: flex-start;
    padding: var(--spacing-md);
    background: var(--bg-elevated-2, var(--bg-elevated));
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }

  .identity-fields {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .field-label {
    font-size: 11px;
    color: var(--text-muted);
    font-weight: 600;
  }

  .field-value {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    word-break: break-all;
  }

  .field-value.readonly {
    color: var(--text-secondary);
  }

  .field-value.mono {
    font-family: var(--font-mono);
    font-size: 12px;
  }

  .field-edit-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .field-input {
    width: 100%;
    padding: 10px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-md);
  }

  .row-actions {
    display: flex;
    gap: var(--spacing-xs);
  }

  .field-error {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: 12px;
    color: var(--error);
  }

  .saved-pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    color: var(--success);
    font-weight: 500;
  }

  .phase-badge {
    font-size: 9px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    padding: 2px 6px;
    border-radius: 4px;
  }

  .danger-section .section-header h3 {
    color: var(--error);
  }
</style>
