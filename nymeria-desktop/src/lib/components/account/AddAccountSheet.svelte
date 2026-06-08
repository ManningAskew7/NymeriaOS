<script lang="ts">
  import type { AccountIdentity } from '$lib/types';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import { probeConnection } from '$lib/services/api.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Modal from '$lib/components/common/Modal.svelte';
  import Avatar from './Avatar.svelte';
  import RoleChip from './RoleChip.svelte';
  import { identityDisplayName } from './avatar';

  interface Props {
    isOpen: boolean;
    onClose: () => void;
    /**
     * Whether to switch to the new account immediately after saving. Defaults
     * to true — the typical "add and use" flow.
     */
    switchAfterSave?: boolean;
  }

  let { isOpen, onClose, switchAfterSave = true }: Props = $props();

  // Form state
  let apiUrl = $state('');
  let apiKey = $state('');
  let nameOverride = $state('');

  // Test state
  let testStatus = $state<'idle' | 'testing' | 'success' | 'error'>('idle');
  let testMessage = $state('');
  let resolvedIdentity = $state<AccountIdentity | null>(null);

  let saving = $state(false);

  // Reset form whenever the sheet re-opens.
  $effect(() => {
    if (isOpen) {
      apiUrl = '';
      apiKey = '';
      nameOverride = '';
      testStatus = 'idle';
      testMessage = '';
      resolvedIdentity = null;
      saving = false;
    }
  });

  function trimmedUrl(): string {
    return apiUrl.trim().replace(/\/$/, '');
  }

  function safeHostname(url: string): string {
    try {
      return new URL(url).hostname;
    } catch {
      return url;
    }
  }

  async function handleTest() {
    testStatus = 'testing';
    testMessage = '';
    resolvedIdentity = null;

    const result = await probeConnection(trimmedUrl(), apiKey.trim());
    if (!result.ok) {
      testStatus = 'error';
      testMessage = result.message;
      return;
    }
    resolvedIdentity = result.identity;
    testStatus = 'success';
    testMessage = '';
  }

  function defaultName(): string {
    if (resolvedIdentity) {
      return resolvedIdentity.display_name || resolvedIdentity.email || safeHostname(trimmedUrl());
    }
    return safeHostname(trimmedUrl());
  }

  async function handleSave() {
    if (saving || testStatus !== 'success') return;
    saving = true;
    try {
      const finalName = nameOverride.trim() || defaultName();
      const entry = connectionsStore.upsertAccountCredential({
        name: finalName,
        apiUrl: trimmedUrl(),
        apiKey: apiKey.trim(),
        identity: resolvedIdentity,
      });
      if (switchAfterSave) {
        await connectionsStore.switchTo(entry.id);
      }
      onClose();
    } finally {
      saving = false;
    }
  }
</script>

<Modal title="Add account" {isOpen} {onClose}>
  <div class="add-account">
    <p class="intro">
      Connect to another NymeriaOS server (or another account on the same one).
      Saved accounts show up in the bottom-bar switcher.
    </p>

    <div class="field">
      <label for="add-url">Server URL</label>
      <input
        id="add-url"
        type="url"
        bind:value={apiUrl}
        placeholder="http://localhost:8000"
        disabled={saving}
        autocomplete="off"
      />
    </div>

    <div class="field">
      <label for="add-token">Account token</label>
      <input
        id="add-token"
        type="password"
        bind:value={apiKey}
        placeholder="nym_..."
        disabled={saving}
        autocomplete="off"
      />
      <p class="hint">
        Issue a token via the admin Users panel, the <code>users</code> CLI, or
        use a bootstrap token from <code>BOOTSTRAP_TOKEN.txt</code>.
      </p>
    </div>

    <div class="actions">
      <Button
        variant="secondary"
        onclick={handleTest}
        disabled={saving || testStatus === 'testing' || !apiUrl || !apiKey}
      >
        {testStatus === 'testing' ? 'Testing…' : 'Test connection'}
      </Button>
    </div>

    {#if testStatus === 'error'}
      <div class="result error">
        <Icon name="error" size={16} />
        <span>{testMessage}</span>
      </div>
    {/if}

    {#if testStatus === 'success' && resolvedIdentity}
      <div class="preview-card">
        <div class="preview-row">
          <Avatar identity={resolvedIdentity} size={40} state="connected" />
          <div class="preview-meta">
            <div class="preview-name-row">
              <span class="preview-name">{identityDisplayName(resolvedIdentity)}</span>
              <RoleChip role={resolvedIdentity.role} />
            </div>
            {#if resolvedIdentity.email && resolvedIdentity.email !== resolvedIdentity.display_name}
              <span class="preview-email">{resolvedIdentity.email}</span>
            {/if}
            <span class="preview-host">{safeHostname(trimmedUrl())}</span>
          </div>
        </div>

        <div class="field nested">
          <label for="add-name">Label (optional)</label>
          <input
            id="add-name"
            type="text"
            bind:value={nameOverride}
            placeholder={defaultName()}
            disabled={saving}
            maxlength="80"
          />
          <p class="hint">How this account shows up in the switcher. Defaults to the resolved display name.</p>
        </div>

        <div class="actions">
          <Button onclick={handleSave} disabled={saving}>
            {#if saving}
              Saving…
            {:else if switchAfterSave}
              Save and switch
            {:else}
              Save account
            {/if}
          </Button>
          <Button variant="ghost" onclick={onClose} disabled={saving}>Cancel</Button>
        </div>
      </div>
    {/if}
  </div>
</Modal>

<style>
  .add-account {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: min(440px, 90vw);
  }

  .intro {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.5;
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

  .field input {
    padding: 8px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .field input:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(var(--accent-primary-rgb), 0.15));
  }

  .field input:disabled {
    opacity: 0.6;
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  .hint code {
    font-family: var(--font-mono);
    background: var(--bg-base);
    padding: 1px 5px;
    border-radius: 3px;
    font-size: var(--font-size-2xs);
  }

  .actions {
    display: flex;
    gap: var(--spacing-sm);
    align-items: center;
  }

  .result {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 10px var(--spacing-sm);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
  }

  .result.error {
    background: rgba(var(--error-rgb), 0.08);
    border: 1px solid rgba(var(--error-rgb), 0.3);
    color: var(--error);
  }

  .preview-card {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    padding: var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--accent-primary);
    border-radius: var(--radius-md);
    box-shadow: 0 0 0 3px var(--accent-primary-alpha, rgba(var(--accent-primary-rgb), 0.08));
  }

  .preview-row {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
  }

  .preview-meta {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .preview-name-row {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .preview-name {
    flex: 0 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
  }

  .preview-email {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .preview-host {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    letter-spacing: 0.02em;
  }
</style>
