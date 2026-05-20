<script lang="ts">
  import { api } from '$lib/services/api';
  import type { AuthPromptEvent, AuthPromptField } from '$lib/types';
  import { renderMarkdown } from '$lib/utils/markdown';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import Modal from '../common/Modal.svelte';

  interface Props {
    prompt: AuthPromptEvent | null;
    onResolved: () => void;
  }

  let { prompt, onResolved }: Props = $props();

  let values = $state<Record<string, string>>({});
  let labelDraft = $state('');
  let userNote = $state('');
  let submitting = $state(false);
  let testing = $state(false);
  let lastError = $state<string | null>(null);
  let lastInfo = $state<string | null>(null);
  let attempts = $state(0);
  let isOpen = $derived(prompt !== null);
  let isOAuth = $derived(prompt?.mode === 'oauth' || prompt?.mode === 'oauth_device');
  let oauthLaunched = $state(false);
  let copiedCode = $state(false);
  // Tick once per second so the device-code countdown re-renders.
  let nowMs = $state(Date.now());
  let deviceExpiresAt = $state<number | null>(null);

  // Reset state every time a new prompt comes in.
  $effect(() => {
    if (prompt) {
      const initial: Record<string, string> = {};
      for (const field of prompt.fields) {
        initial[field.name] = '';
      }
      values = initial;
      labelDraft = prompt.account_label || '';
      userNote = '';
      submitting = false;
      testing = false;
      lastError = null;
      lastInfo = null;
      attempts = 0;
      oauthLaunched = false;
      copiedCode = false;
      if (prompt.mode === 'oauth_device' && typeof prompt.expires_in === 'number') {
        deviceExpiresAt = Date.now() + prompt.expires_in * 1000;
      } else {
        deviceExpiresAt = null;
      }
    }
  });

  // Drive the countdown for device-code mode. Only ticks while a prompt is
  // active to avoid a leaked interval after the modal closes.
  $effect(() => {
    if (!prompt || prompt.mode !== 'oauth_device') return;
    nowMs = Date.now();
    const id = setInterval(() => {
      nowMs = Date.now();
    }, 1000);
    return () => clearInterval(id);
  });

  let deviceSecondsLeft = $derived(
    deviceExpiresAt ? Math.max(0, Math.ceil((deviceExpiresAt - nowMs) / 1000)) : 0,
  );

  function formatCountdown(seconds: number): string {
    if (seconds <= 0) return 'expired';
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  async function openExternal(href: string) {
    try {
      const { openUrl } = await import('@tauri-apps/plugin-opener');
      await openUrl(href);
    } catch {
      window.open(href, '_blank', 'noopener,noreferrer');
    }
  }

  async function handleOAuthSignIn() {
    if (!prompt || !prompt.auth_url) return;
    oauthLaunched = true;
    await openExternal(prompt.auth_url);
  }

  async function handleOpenVerification() {
    if (!prompt) return;
    const href = prompt.verification_uri_complete || prompt.verification_uri;
    if (!href) return;
    oauthLaunched = true;
    await openExternal(href);
  }

  async function handleCopyCode() {
    if (!prompt?.user_code) return;
    try {
      await navigator.clipboard.writeText(prompt.user_code);
      copiedCode = true;
      setTimeout(() => {
        copiedCode = false;
      }, 1500);
    } catch {
      copiedCode = false;
    }
  }

  function close() {
    onResolved();
  }

  async function handleClose() {
    if (!prompt) {
      close();
      return;
    }
    if (submitting || testing) return;
    // X / Esc / backdrop — return last error to the agent so it can help.
    await api.exitCredentialPrompt(prompt.prompt_id, lastError, attempts, userNote.trim() || null);
    close();
  }

  async function handleCancel() {
    if (!prompt || submitting || testing) return;
    await api.cancelCredentialPrompt(prompt.prompt_id, userNote.trim() || null);
    close();
  }

  function requestBody() {
    const trimmedLabel = labelDraft.trim();
    return {
      secret_fields: values,
      account_label: trimmedLabel || null,
      user_message: userNote.trim() || null,
    };
  }

  async function handleTest() {
    if (!prompt || submitting || testing) return;
    testing = true;
    lastError = null;
    lastInfo = null;
    try {
      const result = await api.testCredentialPrompt(prompt.prompt_id, requestBody());
      attempts = result.attempts;
      if (result.ok) {
        lastInfo = result.message || (result.tested ? 'Connection verified.' : 'This provider has no verification probe yet.');
        return;
      }
      lastError = result.error || result.message || 'Connection test failed';
    } catch (err) {
      lastError = err instanceof Error ? err.message : String(err);
    } finally {
      testing = false;
    }
  }

  async function handleSubmit(event: SubmitEvent) {
    event.preventDefault();
    if (!prompt || submitting) return;
    submitting = true;
    lastError = null;
    try {
      const result = await api.submitCredentialPrompt(prompt.prompt_id, requestBody());
      attempts = result.attempts;
      if (result.ok) {
        close();
        return;
      }
      lastError = result.error || result.message || 'Connection test failed';
    } catch (err) {
      lastError = err instanceof Error ? err.message : String(err);
    } finally {
      submitting = false;
    }
  }

  function fieldType(field: AuthPromptField): 'password' | 'text' | 'textarea' {
    if (field.kind === 'textarea') return 'textarea';
    return field.secret ? 'password' : 'text';
  }

  let descriptionHtml = $derived(
    prompt?.description ? renderMarkdown(prompt.description) : ''
  );

  async function handleDescriptionClick(event: MouseEvent) {
    // Intercept anchor clicks so http(s) links open in the OS browser
    // rather than navigating the Tauri webview.
    const anchor = (event.target as HTMLElement | null)?.closest('a');
    if (!anchor) return;
    const href = anchor.getAttribute('href');
    if (!href) return;
    if (/^https?:\/\//i.test(href)) {
      event.preventDefault();
      try {
        const { openUrl } = await import('@tauri-apps/plugin-opener');
        await openUrl(href);
      } catch {
        // Fallback for browser/dev contexts where the opener plugin is unavailable.
        window.open(href, '_blank', 'noopener,noreferrer');
      }
    }
  }
</script>

<Modal title={prompt ? `Connect ${prompt.display_name}` : ''} {isOpen} onClose={handleClose}>
  {#if prompt}
    {@const showLabelEditor = prompt.existing_accounts.length > 0 || prompt.account_label}

    {#if descriptionHtml}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <div
        class="description"
        role="region"
        aria-label="Connection instructions"
        onclick={handleDescriptionClick}
      >{@html descriptionHtml}</div>
    {/if}

    {#if prompt.existing_accounts.length > 0}
      <div class="existing-banner">
        <Icon name="info" size={14} />
        <span>
          Already connected:
          {prompt.existing_accounts
            .map((acc) => acc.account_label || acc.name)
            .join(', ')}.
          Add another?
        </span>
      </div>
    {/if}

    {#if isOAuth && prompt.scopes && prompt.scopes.length > 0}
      <details class="scopes">
        <summary>Permissions requested ({prompt.scopes.length})</summary>
        <ul>
          {#each prompt.scopes as scope (scope)}
            <li><code>{scope}</code></li>
          {/each}
        </ul>
      </details>
    {/if}

    {#if prompt.mode === 'oauth'}
      <div class="oauth-block">
        <p class="oauth-lead">
          Sign in with {prompt.display_name} to authorise Nymeria. The sign-in
          page opens in your browser. Return here once it is finished.
        </p>
        <Button type="button" onclick={handleOAuthSignIn} disabled={!prompt.auth_url}>
          Sign in with {prompt.display_name}
        </Button>
        {#if oauthLaunched}
          <div class="waiting">
            <Icon name="info" size={14} />
            <span>Waiting for sign-in to complete…</span>
          </div>
        {/if}
      </div>
    {:else if prompt.mode === 'oauth_device'}
      <div class="oauth-block">
        <p class="oauth-lead">
          Open the verification page on any device and enter the code below.
        </p>
        <div class="device-code-row">
          <code class="device-code">{prompt.user_code || ''}</code>
          <Button variant="secondary" type="button" onclick={handleCopyCode} disabled={!prompt.user_code}>
            {copiedCode ? 'Copied' : 'Copy code'}
          </Button>
        </div>
        {#if prompt.verification_uri || prompt.verification_uri_complete}
          <div class="device-link">
            <code>{prompt.verification_uri_complete || prompt.verification_uri}</code>
            <Button type="button" onclick={handleOpenVerification}>
              {prompt.verification_uri_complete ? 'Open with code pre-filled' : 'Open in browser'}
            </Button>
          </div>
        {/if}
        {#if deviceExpiresAt !== null}
          <div class="countdown" class:expired={deviceSecondsLeft <= 0}>
            Code expires in {formatCountdown(deviceSecondsLeft)}
          </div>
        {/if}
        {#if oauthLaunched}
          <div class="waiting">
            <Icon name="info" size={14} />
            <span>Waiting for sign-in to complete…</span>
          </div>
        {/if}
      </div>
    {:else}
      <form onsubmit={handleSubmit} class="auth-form">
        {#each prompt.fields as field (field.name)}
          <div class="field">
            <label for={`auth-field-${prompt.prompt_id}-${field.name}`}>
              {field.label}
            </label>
            {#if fieldType(field) === 'textarea'}
              <textarea
                id={`auth-field-${prompt.prompt_id}-${field.name}`}
                bind:value={values[field.name]}
                placeholder={field.placeholder || ''}
                rows="4"
                autocomplete="off"
                spellcheck="false"
                required
              ></textarea>
            {:else}
              <input
                id={`auth-field-${prompt.prompt_id}-${field.name}`}
                type={fieldType(field)}
                bind:value={values[field.name]}
                placeholder={field.placeholder || ''}
                autocomplete="off"
                spellcheck="false"
                required
              />
            {/if}
            {#if field.help}
              <small class="help">{field.help}</small>
            {/if}
          </div>
        {/each}

        {#if showLabelEditor}
          <div class="field">
            <label for={`auth-label-${prompt.prompt_id}`}>Name this connection</label>
            <input
              id={`auth-label-${prompt.prompt_id}`}
              type="text"
              bind:value={labelDraft}
              placeholder="e.g. work, personal"
              autocomplete="off"
            />
          </div>
        {/if}

        <div class="field">
          <label for={`auth-note-${prompt.prompt_id}`}>Note to Nymeria (optional)</label>
          <textarea
            id={`auth-note-${prompt.prompt_id}`}
            class="note"
            bind:value={userNote}
            rows="3"
            placeholder="Anything you want Nymeria to know after this setup closes"
          ></textarea>
        </div>

        {#if lastError}
          <div class="error">
            <Icon name="warning" size={14} />
            <span>{lastError}</span>
          </div>
        {/if}

        {#if lastInfo}
          <div class="info">
            <Icon name="success" size={14} />
            <span>{lastInfo}</span>
          </div>
        {/if}

        <div class="actions">
          <Button variant="ghost" type="button" onclick={handleCancel} disabled={submitting || testing}>
            Cancel
          </Button>
          <Button variant="secondary" type="button" onclick={handleTest} disabled={submitting || testing}>
            {testing ? 'Testing…' : 'Test'}
          </Button>
          <Button type="submit" disabled={submitting}>
            {submitting ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </form>
    {/if}

    {#if isOAuth}
      <div class="oauth-actions">
        <Button variant="ghost" type="button" onclick={handleCancel}>
          Cancel
        </Button>
      </div>
    {/if}
  {/if}
</Modal>

<style>
  .auth-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: min(440px, 80vw);
  }

  .description {
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .description :global(p) {
    margin: 0 0 var(--spacing-xs) 0;
  }

  .description :global(p:last-child) {
    margin-bottom: 0;
  }

  .description :global(a) {
    color: var(--accent);
    text-decoration: underline;
  }

  .description :global(ul),
  .description :global(ol) {
    margin: var(--spacing-xs) 0;
    padding-left: var(--spacing-lg);
  }

  .description :global(code) {
    background: var(--bg-subtle);
    padding: 2px 4px;
    border-radius: 3px;
    font-family: var(--font-mono, monospace);
    font-size: 0.9em;
  }

  .existing-banner {
    display: flex;
    gap: var(--spacing-xs);
    align-items: flex-start;
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    background: var(--bg-subtle);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .field label {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .field input,
  .field textarea {
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--bg-input, var(--glass-bg));
    color: var(--text-primary);
    font-family: var(--font-mono, monospace);
    font-size: var(--font-size-sm);
  }

  .field textarea {
    resize: vertical;
    min-height: 80px;
  }

  .field textarea.note {
    font-family: inherit;
  }

  .field input:focus,
  .field textarea:focus {
    outline: none;
    border-color: var(--accent);
  }

  .help {
    color: var(--text-tertiary, var(--text-secondary));
    font-size: var(--font-size-xs);
  }

  .error {
    display: flex;
    gap: var(--spacing-xs);
    align-items: flex-start;
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--danger, #c0392b) 12%, transparent);
    color: var(--danger, #c0392b);
    font-size: var(--font-size-sm);
  }

  .info {
    display: flex;
    gap: var(--spacing-xs);
    align-items: flex-start;
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--success, #2e7d32) 12%, transparent);
    color: var(--success, #2e7d32);
    font-size: var(--font-size-sm);
  }

  .actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
  }

  .scopes {
    margin: var(--spacing-sm) 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .scopes summary {
    cursor: pointer;
  }

  .scopes ul {
    margin: var(--spacing-xs) 0 0 0;
    padding-left: var(--spacing-lg);
    max-height: 160px;
    overflow-y: auto;
  }

  .scopes code {
    font-family: var(--font-mono, monospace);
    font-size: 0.85em;
    word-break: break-all;
  }

  .oauth-block {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: min(440px, 80vw);
    padding: var(--spacing-md) 0;
  }

  .oauth-lead {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .device-code-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .device-code {
    flex: 1;
    padding: var(--spacing-md);
    font-family: var(--font-mono, monospace);
    font-size: 1.4rem;
    letter-spacing: 0.1em;
    text-align: center;
    background: var(--bg-subtle);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    user-select: all;
  }

  .device-link {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .device-link code {
    flex: 1;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-subtle);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono, monospace);
    font-size: var(--font-size-sm);
    word-break: break-all;
  }

  .countdown {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    text-align: right;
  }

  .countdown.expired {
    color: var(--danger, #c0392b);
  }

  .waiting {
    display: flex;
    gap: var(--spacing-xs);
    align-items: center;
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    background: var(--bg-subtle);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .oauth-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
  }
</style>
