<script lang="ts">
  import { api } from '$lib/services/api';
  import type { AuthPromptEvent, AuthPromptField } from '$lib/types';
  import { renderMarkdown } from '$lib/utils/markdown';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';

  /**
   * This is a non-blocking floating panel that the user can drag, dismiss, or
   * ignore while still interacting with the rest of the app. There is no
   * full-screen backdrop and no focus trap. After a successful save the panel
   * closes; the agent does not get a synthetic turn, so the user prompts the
   * agent (e.g. "ok, try again") to continue. On a test failure the error is
   * shown inline with a copy button so the user can paste it back into chat.
   */

  interface Props {
    prompt: AuthPromptEvent | null;
    onResolved: () => void;
  }

  let { prompt, onResolved }: Props = $props();

  let values = $state<Record<string, string>>({});
  let labelDraft = $state('');
  let submitting = $state(false);
  let testing = $state(false);
  let checkingOAuth = $state(false);
  let lastError = $state<string | null>(null);
  let lastInfo = $state<string | null>(null);
  let attempts = $state(0);
  let isOpen = $derived(prompt !== null);
  let isOAuth = $derived(prompt?.mode === 'oauth' || prompt?.mode === 'oauth_device');
  let oauthResolved = $derived(prompt?.resolution_status === 'active');
  let oauthFailed = $derived(
    !!prompt?.resolution_status && prompt.resolution_status !== 'active'
  );
  let oauthLaunched = $state(false);
  let copiedCode = $state(false);
  let copiedError = $state(false);

  // Drag state.
  const PANEL_WIDTH = 440;
  const PANEL_MIN_HEIGHT = 240;
  let panelX = $state<number>(0);
  let panelY = $state<number>(0);
  let dragOffsetX = 0;
  let dragOffsetY = 0;
  let isDragging = $state(false);
  let positionInitialized = false;

  // Tick once per second so the device-code countdown re-renders.
  let nowMs = $state(Date.now());
  let deviceExpiresAt = $state<number | null>(null);

  function initialPosition(): { x: number; y: number } {
    if (typeof window === 'undefined') return { x: 24, y: 24 };
    const margin = 24;
    const x = Math.max(margin, window.innerWidth - PANEL_WIDTH - margin);
    const y = margin;
    return { x, y };
  }

  // Reset per-prompt state and (re)position the panel.
  $effect(() => {
    if (prompt) {
      const initial: Record<string, string> = {};
      for (const field of prompt.fields) {
        initial[field.name] = '';
      }
      values = initial;
      labelDraft = prompt.account_label || '';
      submitting = false;
      testing = false;
      checkingOAuth = false;
      lastError = null;
      lastInfo = prompt.resolution_message || null;
      if (prompt.resolution_ok === false) {
        lastError = prompt.resolution_message || `OAuth setup ended with status: ${prompt.resolution_status || 'failed'}`;
        lastInfo = null;
      }
      attempts = 0;
      oauthLaunched = false;
      copiedCode = false;
      copiedError = false;
      if (!positionInitialized) {
        const pos = initialPosition();
        panelX = pos.x;
        panelY = pos.y;
        positionInitialized = true;
      }
      if (prompt.mode === 'oauth_device' && typeof prompt.expires_in === 'number') {
        deviceExpiresAt = Date.now() + prompt.expires_in * 1000;
      } else {
        deviceExpiresAt = null;
      }
    } else {
      positionInitialized = false;
    }
  });

  // Drive the device-code countdown only while a device prompt is active.
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

  async function handleCopyError() {
    if (!lastError) return;
    try {
      await navigator.clipboard.writeText(lastError);
      copiedError = true;
      setTimeout(() => {
        copiedError = false;
      }, 1500);
    } catch {
      copiedError = false;
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
    if (submitting || testing || checkingOAuth) return;
    // Dismiss: report the last error so server-side audit + recent-prompts
    // history captures it. We no longer send a user_message — the user
    // drives follow-up in chat, not via this panel.
    await api.exitCredentialPrompt(prompt.prompt_id, lastError, attempts, null);
    close();
  }

  async function handleCancel() {
    if (!prompt || submitting || testing || checkingOAuth) return;
    await api.cancelCredentialPrompt(prompt.prompt_id, null);
    close();
  }

  function requestBody() {
    const trimmedLabel = labelDraft.trim();
    return {
      secret_fields: values,
      account_label: trimmedLabel || null,
      user_message: null,
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

  async function handleCheckOAuthStatus() {
    if (!prompt || !isOAuth || checkingOAuth) return;
    checkingOAuth = true;
    lastError = null;
    try {
      const result = await api.getCredentialPromptStatus(prompt.prompt_id);
      if (result.ok || result.status === 'active') {
        lastInfo = result.message || 'Connection completed.';
        return;
      }
      if (result.status === 'pending_setup') {
        lastInfo = result.message || 'Still waiting for sign-in to complete.';
        return;
      }
      lastError = result.message || `Credential setup is ${result.status}.`;
    } catch (err) {
      lastError = err instanceof Error ? err.message : String(err);
    } finally {
      checkingOAuth = false;
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

  let instructionsHtml = $derived(
    prompt?.instructions ? renderMarkdown(prompt.instructions) : ''
  );

  async function handleMarkdownClick(event: MouseEvent) {
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
        window.open(href, '_blank', 'noopener,noreferrer');
      }
    }
  }

  // ---- drag handling ----

  function clampPosition(x: number, y: number, panelHeight: number): { x: number; y: number } {
    if (typeof window === 'undefined') return { x, y };
    const maxX = Math.max(0, window.innerWidth - PANEL_WIDTH);
    const maxY = Math.max(0, window.innerHeight - panelHeight);
    return {
      x: Math.min(Math.max(0, x), maxX),
      y: Math.min(Math.max(0, y), maxY),
    };
  }

  function handleDragStart(event: PointerEvent) {
    if (event.button !== 0) return;
    const target = event.currentTarget as HTMLElement;
    dragOffsetX = event.clientX - panelX;
    dragOffsetY = event.clientY - panelY;
    isDragging = true;
    target.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  function handleDragMove(event: PointerEvent) {
    if (!isDragging) return;
    const panelEl = event.currentTarget as HTMLElement;
    const panelHeight = panelEl.getBoundingClientRect().height || PANEL_MIN_HEIGHT;
    const next = clampPosition(
      event.clientX - dragOffsetX,
      event.clientY - dragOffsetY,
      panelHeight,
    );
    panelX = next.x;
    panelY = next.y;
  }

  function handleDragEnd(event: PointerEvent) {
    if (!isDragging) return;
    const target = event.currentTarget as HTMLElement;
    try {
      target.releasePointerCapture(event.pointerId);
    } catch {
      // ignore
    }
    isDragging = false;
  }
</script>

{#if isOpen && prompt}
  {@const showLabelEditor = prompt.existing_accounts.length > 0 || !!prompt.account_label}

  <div
    class="auth-panel"
    class:dragging={isDragging}
    role="dialog"
    aria-labelledby={`auth-title-${prompt.prompt_id}`}
    tabindex="-1"
    style="left: {panelX}px; top: {panelY}px;"
    onpointermove={handleDragMove}
    onpointerup={handleDragEnd}
    onpointercancel={handleDragEnd}
  >
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <header class="panel-header" onpointerdown={handleDragStart}>
      <span class="header-grip" aria-hidden="true">
        <span></span><span></span>
        <span></span><span></span>
        <span></span><span></span>
      </span>
      <h2 class="header-title" id={`auth-title-${prompt.prompt_id}`} tabindex="-1">
        Connect {prompt.display_name}
      </h2>
      <button
        class="close-btn"
        type="button"
        aria-label="Dismiss connection prompt"
        onclick={handleClose}
        onpointerdown={(e) => e.stopPropagation()}
      >
        <span class="icon-wrap"><Icon name="x" size={18} /></span>
      </button>
    </header>

    <div class="panel-body">
      {#if descriptionHtml || instructionsHtml}
        <div class="intro">
          {#if descriptionHtml}
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
            <div class="description" role="region" aria-label="Connection summary" onclick={handleMarkdownClick}>
              {@html descriptionHtml}
            </div>
          {/if}

          {#if instructionsHtml}
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
            <div class="callout" role="region" aria-label="Step by step instructions" onclick={handleMarkdownClick}>
              <div class="callout-label">Steps from Nymeria</div>
              <div class="callout-body">{@html instructionsHtml}</div>
            </div>
          {/if}
        </div>
      {/if}

      {#if prompt.existing_accounts.length > 0}
        <div class="existing-banner">
          <span class="icon-wrap" aria-hidden="true"><Icon name="info" size={14} /></span>
          <span>
            Already connected:
            {prompt.existing_accounts.map((acc) => acc.account_label || acc.name).join(', ')}.
            Add another?
          </span>
        </div>
      {/if}

      {#if isOAuth && prompt.scopes && prompt.scopes.length > 0}
        <details class="scopes">
          <summary>
            <span class="scopes-chevron" aria-hidden="true">
              <Icon name="chevronRight" size={12} />
            </span>
            Permissions requested ({prompt.scopes.length})
          </summary>
          <ul>
            {#each prompt.scopes as scope (scope)}
              <li><code>{scope}</code></li>
            {/each}
          </ul>
        </details>
      {/if}

      {#if prompt.mode === 'oauth'}
        <div class="oauth-block">
          <p class="lead">
            Sign in with {prompt.display_name} to authorise Nymeria. The page
            opens in your browser. Return here when finished.
          </p>
          <Button type="button" onclick={handleOAuthSignIn} disabled={!prompt.auth_url || oauthResolved}>
            Sign in with {prompt.display_name}
          </Button>
          {#if oauthResolved}
            <div class="status-row success">
              <span class="icon-wrap" aria-hidden="true"><Icon name="success" size={14} /></span>
              <span>{prompt.resolution_message || lastInfo || 'Connection completed.'}</span>
            </div>
          {:else if oauthFailed}
            <div class="status-row error">
              <span class="icon-wrap" aria-hidden="true"><Icon name="warning" size={14} /></span>
              <span>{lastError || prompt.resolution_message || 'Sign-in did not complete.'}</span>
            </div>
          {:else if oauthLaunched}
            <div class="status-row">
              <span class="icon-wrap" aria-hidden="true"><Icon name="info" size={14} /></span>
              <span>Waiting for sign-in to complete…</span>
            </div>
          {/if}
        </div>
      {:else if prompt.mode === 'oauth_device'}
        <div class="oauth-block">
          <p class="lead">Open the verification page on any device and enter the code below.</p>
          <div class="device-code-row">
            {#if prompt.user_code}
              <code class="device-code">{prompt.user_code}</code>
            {:else}
              <div class="device-code device-code-skeleton" aria-label="Waiting for code">
                <span></span>
              </div>
            {/if}
            <Button variant="secondary" type="button" onclick={handleCopyCode} disabled={!prompt.user_code}>
              {copiedCode ? 'Copied' : 'Copy code'}
            </Button>
          </div>
          {#if prompt.verification_uri || prompt.verification_uri_complete}
            <div class="device-link">
              <code>{prompt.verification_uri_complete || prompt.verification_uri}</code>
              <Button type="button" onclick={handleOpenVerification} disabled={oauthResolved}>
                {prompt.verification_uri_complete ? 'Open with code pre-filled' : 'Open in browser'}
              </Button>
            </div>
          {/if}
          {#if deviceExpiresAt !== null}
            <div class="countdown" class:expired={deviceSecondsLeft <= 0}>
              Code expires in {formatCountdown(deviceSecondsLeft)}
            </div>
          {/if}
          {#if oauthResolved}
            <div class="status-row success">
              <span class="icon-wrap" aria-hidden="true"><Icon name="success" size={14} /></span>
              <span>{prompt.resolution_message || lastInfo || 'Connection completed.'}</span>
            </div>
          {:else if oauthFailed}
            <div class="status-row error">
              <span class="icon-wrap" aria-hidden="true"><Icon name="warning" size={14} /></span>
              <span>{lastError || prompt.resolution_message || 'Sign-in did not complete.'}</span>
            </div>
          {:else if oauthLaunched}
            <div class="status-row">
              <span class="icon-wrap" aria-hidden="true"><Icon name="info" size={14} /></span>
              <span>Waiting for sign-in to complete…</span>
            </div>
          {/if}
        </div>
      {:else}
        <form onsubmit={handleSubmit} class="auth-form">
          {#each prompt.fields as field (field.name)}
            <div class="field">
              <label for={`auth-field-${prompt.prompt_id}-${field.name}`}>{field.label}</label>
              {#if fieldType(field) === 'textarea'}
                <textarea
                  id={`auth-field-${prompt.prompt_id}-${field.name}`}
                  class="secret-input"
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
                  class="secret-input"
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

          {#if lastError}
            <div class="alert error" role="alert">
              <span class="alert-icon" aria-hidden="true"><Icon name="warning" size={16} /></span>
              <div class="alert-body">
                <div class="alert-title">Connection test failed</div>
                <div class="alert-text">{lastError}</div>
              </div>
              <button
                type="button"
                class="alert-copy"
                aria-label={copiedError ? 'Copied error to clipboard' : 'Copy error to clipboard'}
                title={copiedError ? 'Copied' : 'Copy to clipboard'}
                onclick={handleCopyError}
              >
                {#if copiedError}
                  <span class="icon-wrap"><Icon name="check" size={14} /></span>
                {:else}
                  <span class="icon-wrap"><Icon name="copy" size={14} /></span>
                {/if}
              </button>
            </div>
          {/if}

          {#if lastInfo}
            <div class="alert info" role="status">
              <span class="alert-icon" aria-hidden="true"><Icon name="success" size={16} /></span>
              <div class="alert-body"><div class="alert-text">{lastInfo}</div></div>
            </div>
          {/if}

          <div class="actions">
            <Button variant="ghost" type="button" onclick={handleCancel} disabled={submitting || testing}>
              Cancel
            </Button>
            <Button variant="secondary" type="button" onclick={handleTest} disabled={submitting || testing}>
              {testing ? 'Testing…' : 'Test credential'}
            </Button>
            <Button type="submit" disabled={submitting}>
              {submitting ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </form>
      {/if}

      {#if isOAuth}
        {#if lastInfo && !oauthResolved}
          <div class="alert info" role="status">
            <span class="alert-icon" aria-hidden="true"><Icon name="success" size={16} /></span>
            <div class="alert-body"><div class="alert-text">{lastInfo}</div></div>
          </div>
        {/if}

        {#if lastError && !oauthFailed}
          <div class="alert error" role="alert">
            <span class="alert-icon" aria-hidden="true"><Icon name="warning" size={16} /></span>
            <div class="alert-body">
              <div class="alert-title">Connection status</div>
              <div class="alert-text">{lastError}</div>
            </div>
            <button
              type="button"
              class="alert-copy"
              aria-label={copiedError ? 'Copied status to clipboard' : 'Copy status to clipboard'}
              title={copiedError ? 'Copied' : 'Copy to clipboard'}
              onclick={handleCopyError}
            >
              {#if copiedError}
                <span class="icon-wrap"><Icon name="check" size={14} /></span>
              {:else}
                <span class="icon-wrap"><Icon name="copy" size={14} /></span>
              {/if}
            </button>
          </div>
        {/if}

        <div class="actions">
          <Button variant="ghost" type="button" onclick={handleCancel} disabled={oauthResolved}>Cancel</Button>
          <Button variant="secondary" type="button" onclick={handleCheckOAuthStatus} disabled={checkingOAuth}>
            {checkingOAuth ? 'Checking…' : 'Check status'}
          </Button>
          {#if oauthResolved}
            <Button type="button" onclick={close}>Close</Button>
          {/if}
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .auth-panel {
    position: fixed;
    z-index: 900;
    width: 440px;
    /* §7 glass-surface exception — the hairline --glass-border is edge
       definition for the glassmorphic surface against the blurred
       backdrop, not redundant chrome. Together with the elevation
       shadow this is the documented "rarely both" case for glass modals.
       Outer shadow tokenized to --shadow-xl; inner highlight kept
       (it's the glass top-edge reflection, not an elevation shadow). */
    background: var(--glass-bg-strong);
    backdrop-filter: var(--glass-blur-strong);
    -webkit-backdrop-filter: var(--glass-blur-strong);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg);
    box-shadow:
      inset 0 1px 0 rgba(255, 255, 255, 0.04),
      var(--shadow-xl);
    display: flex;
    flex-direction: column;
    max-height: min(720px, calc(100vh - 48px));
    overflow: hidden;
    animation: slideUp var(--transition-normal);
  }

  @media (max-width: 600px) {
    .auth-panel {
      width: calc(100vw - 32px);
    }
  }

  .auth-panel.dragging {
    user-select: none;
    box-shadow:
      inset 0 1px 0 rgba(255, 255, 255, 0.06),
      0 32px 64px rgba(0, 0, 0, 0.55);
  }

  /* ---- header ---- */

  .panel-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
    cursor: grab;
    touch-action: none;
  }

  .auth-panel.dragging .panel-header {
    cursor: grabbing;
  }

  .header-grip {
    display: inline-grid;
    grid-template-columns: repeat(2, 3px);
    grid-template-rows: repeat(3, 3px);
    gap: 2px;
    width: 8px;
    color: var(--text-muted);
    opacity: 0.7;
    transition: opacity var(--transition-fast);
  }

  .panel-header:hover .header-grip {
    opacity: 1;
  }

  .header-grip > span {
    width: 3px;
    height: 3px;
    border-radius: 50%;
    background: currentColor;
  }

  .header-title {
    flex: 1;
    margin: 0;
    font-size: var(--font-size-lg);
    font-weight: 600;
    color: var(--text-primary);
    line-height: 1.2;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .header-title:focus {
    outline: none;
  }

  .close-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--text-secondary);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: background var(--transition-fast), color var(--transition-fast);
  }

  .close-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  /* ---- body ---- */

  .panel-body {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    padding: var(--spacing-md);
    overflow-y: auto;
  }

  /* Tighter rhythm between description + steps; the wrapping .intro keeps
     them visually grouped, while the body's larger gap separates that block
     from the form/oauth content. */
  .intro {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .description {
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.5;
    /* §5 — multi-paragraph credential explanations capped to 70ch so the
       prose stays readable on wide modal layouts. */
    max-width: 70ch;
  }

  .description :global(p) {
    margin: 0 0 var(--spacing-xs) 0;
  }

  .description :global(p:last-child) {
    margin-bottom: 0;
  }

  .description :global(a),
  .callout-body :global(a) {
    color: var(--accent-primary);
    text-decoration: underline;
  }

  .description :global(code),
  .callout-body :global(code) {
    background: var(--bg-elevated-2);
    padding: 2px 4px;
    border-radius: 3px;
    font-family: var(--font-mono);
    font-size: 0.9em;
  }

  .callout {
    border-left: 3px solid var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 8%, var(--bg-elevated));
    border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .callout-label {
    font-size: var(--font-size-xs);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--accent-primary);
    margin-bottom: var(--spacing-xs);
    font-weight: 600;
  }

  .callout-body :global(p) {
    margin: 0 0 var(--spacing-xs) 0;
  }

  .callout-body :global(p:last-child) {
    margin-bottom: 0;
  }

  .callout-body :global(ol),
  .callout-body :global(ul) {
    margin: var(--spacing-xs) 0;
    padding-left: var(--spacing-lg);
  }

  .callout-body :global(li) {
    margin-bottom: var(--spacing-xs);
  }

  /* Icon SVGs don't ship with display: block, so they carry an inline
     baseline gap and refuse to centre inside small flex boxes. Force the
     SVG to render as a block-level shape inside any icon wrapper. */
  .icon-wrap {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1em;
    height: 1em;
    line-height: 0;
    flex-shrink: 0;
    color: inherit;
  }

  .icon-wrap :global(svg) {
    display: block;
  }

  .existing-banner {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.4;
  }

  .existing-banner .icon-wrap {
    width: 14px;
    height: 14px;
    color: var(--accent-primary);
  }

  /* ---- form ---- */

  .auth-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .field label {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    font-weight: 500;
  }

  .field input,
  .field textarea {
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-family: var(--font-sans);
    font-size: var(--font-size-sm);
    transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
  }

  /* Only the secret-value fields use monospace; the connection label uses
     the body font because it's plain prose. */
  .field input.secret-input,
  .field textarea.secret-input {
    font-family: var(--font-mono);
  }

  .field textarea {
    resize: vertical;
    min-height: 80px;
  }

  .field input:focus,
  .field textarea:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(var(--accent-primary-rgb), 0.18);
  }

  .help {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
    line-height: 1.4;
  }

  /* ---- alerts ---- */

  .alert {
    position: relative;
    display: grid;
    grid-template-columns: 16px 1fr;
    align-items: start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    line-height: 1.4;
  }

  .alert.error {
    background: color-mix(in srgb, var(--error) 12%, transparent);
    color: var(--error);
    border: 1px solid color-mix(in srgb, var(--error) 32%, transparent);
    padding-right: 36px; /* room for the copy icon button */
  }

  .alert.info {
    background: color-mix(in srgb, var(--success) 12%, transparent);
    color: var(--success);
    border: 1px solid color-mix(in srgb, var(--success) 32%, transparent);
  }

  .alert-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    line-height: 0;
    margin-top: 2px;
    color: inherit;
  }

  .alert-icon :global(svg) {
    display: block;
  }

  .alert-body {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .alert-title {
    font-weight: 600;
  }

  .alert-text {
    color: var(--text-primary);
    word-break: break-word;
    white-space: pre-wrap;
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    line-height: 1.45;
  }

  .alert.info .alert-text {
    color: inherit;
    font-family: var(--font-sans);
    font-size: var(--font-size-sm);
  }

  .alert-copy {
    position: absolute;
    top: 6px;
    right: 6px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    border: 0;
    background: transparent;
    color: inherit;
    border-radius: var(--radius-sm);
    cursor: pointer;
    opacity: 0.7;
    transition: opacity var(--transition-fast), background var(--transition-fast);
  }

  .alert-copy:hover {
    opacity: 1;
    background: color-mix(in srgb, currentColor 14%, transparent);
  }

  .alert-copy .icon-wrap {
    width: 14px;
    height: 14px;
  }

  /* ---- misc rows ---- */

  .status-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .status-row .icon-wrap {
    width: 14px;
    height: 14px;
    color: var(--accent-primary);
  }

  .status-row.success {
    color: var(--success);
    border-color: color-mix(in srgb, var(--success) 32%, transparent);
    background: color-mix(in srgb, var(--success) 12%, var(--bg-elevated));
  }

  .status-row.success .icon-wrap {
    color: var(--success);
  }

  .status-row.error {
    color: var(--error);
    border-color: color-mix(in srgb, var(--error) 32%, transparent);
    background: color-mix(in srgb, var(--error) 12%, var(--bg-elevated));
  }

  .status-row.error .icon-wrap {
    color: var(--error);
  }

  .actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
  }

  .scopes {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .scopes summary {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    cursor: pointer;
    padding: 2px 4px;
    margin-left: -4px;
    border-radius: var(--radius-sm);
    list-style: none;
    color: var(--text-secondary);
    transition: color var(--transition-fast);
  }

  .scopes summary::-webkit-details-marker {
    display: none;
  }

  .scopes summary:hover {
    color: var(--text-primary);
  }

  .scopes-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 12px;
    height: 12px;
    line-height: 0;
    transition: transform var(--transition-fast);
  }

  .scopes-chevron :global(svg) {
    display: block;
  }

  .scopes[open] .scopes-chevron {
    transform: rotate(90deg);
  }

  .scopes ul {
    margin: var(--spacing-xs) 0 0 0;
    padding-left: var(--spacing-lg);
    max-height: 160px;
    overflow-y: auto;
  }

  .scopes code {
    font-family: var(--font-mono);
    font-size: 0.85em;
    word-break: break-all;
  }

  /* ---- oauth ---- */

  .oauth-block {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .lead {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .device-code-row {
    display: flex;
    align-items: stretch;
    gap: var(--spacing-sm);
  }

  .device-code {
    flex: 1;
    padding: var(--spacing-md);
    font-family: var(--font-mono);
    font-size: 1.3rem;
    letter-spacing: 0.12em;
    text-align: center;
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    user-select: all;
  }

  .device-code-skeleton {
    display: flex;
    align-items: center;
    justify-content: center;
    user-select: none;
  }

  .device-code-skeleton > span {
    display: block;
    width: 60%;
    height: 1.1em;
    border-radius: var(--radius-sm);
    background: linear-gradient(
      90deg,
      var(--bg-elevated-2) 0%,
      var(--bg-hover) 50%,
      var(--bg-elevated-2) 100%
    );
    background-size: 200% 100%;
    animation: skeletonShimmer 1.6s ease-in-out infinite;
  }

  .device-link {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .device-link code {
    flex: 1;
    min-width: 0;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    word-break: break-all;
  }

  .countdown {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .countdown.expired {
    color: var(--error);
  }

  /* ---- animations ---- */
  /* slideUp comes from the global app.css keyframes. */

  @keyframes skeletonShimmer {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }

  @media (prefers-reduced-motion: reduce) {
    .auth-panel,
    .device-code-skeleton > span {
      animation: none;
    }
  }
</style>
