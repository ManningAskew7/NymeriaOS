<script lang="ts">
  import { fade, fly } from 'svelte/transition';
  import { api } from '$lib/services/api';
  import type { UiPromptEvent } from '$lib/types';
  import { buildArtifactSrcdoc } from '$lib/artifacts/buildArtifactSrcdoc';
  import { trapFocus } from '$lib/actions/focus';
  import {
    OVERLAY_FADE_IN,
    OVERLAY_FADE_OUT,
    DIALOG_RISE_IN,
    DIALOG_RISE_OUT,
  } from '$lib/utils/transitions';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';

  /**
   * Renders an agent-authored HTML form (ui_prompt tool) in a sandboxed
   * iframe and blocks the agent's tool call until the user submits or
   * dismisses. Unlike the common Modal, the backdrop deliberately does NOT
   * close on click: dismissal cancels the agent's question irrevocably, so
   * it must be an explicit act (Escape, the X, or Dismiss).
   *
   * Security invariants (see docs/private/plans/ui-prompt-tool.md):
   * - sandbox="allow-scripts" ONLY. NEVER add allow-same-origin: the agent's
   *   script would inherit the app origin (Tauri IPC, tokens, backend).
   * - The srcdoc carries a default-src 'none' CSP (only inline script/style
   *   and data: assets), so no network request of any kind leaves the frame;
   *   all assets are inlined. See buildArtifactSrcdoc.ts for the policy.
   * - The one channel that CSP cannot close is the frame navigating ITSELF
   *   to an external URL (the sandbox only blocks TOP navigation). The load
   *   guard below cancels the prompt on any iframe load event after the
   *   initial srcdoc render, so a navigating document loses the form and
   *   the agent gets a definitive "cancelled".
   * - Messages are accepted only from this iframe's contentWindow with a
   *   matching prompt id.
   */

  interface Props {
    prompt: UiPromptEvent | null;
    onResolved: () => void;
  }

  let { prompt, onResolved }: Props = $props();

  const MIN_FRAME_HEIGHT = 160;
  const DEFAULT_FRAME_HEIGHT = 320;

  let iframeEl = $state<HTMLIFrameElement | null>(null);
  let frameLoads = 0;
  let frameHeight = $state(DEFAULT_FRAME_HEIGHT);
  let submitting = $state(false);
  let submitError = $state<string | null>(null);
  let isOpen = $derived(prompt !== null);
  let heading = $derived(prompt?.title?.trim() || 'Nymeria needs your input');

  // Theme snapshot for the srcdoc (daisyUI light/dark); midnight and
  // platinum are dark themes.
  function currentTheme(): 'light' | 'dark' {
    if (typeof document === 'undefined') return 'dark';
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }

  let srcdoc = $derived(
    prompt
      ? buildArtifactSrcdoc({ html: prompt.html, promptId: prompt.prompt_id, theme: currentTheme() })
      : ''
  );

  // Reset per-prompt state whenever a new prompt arrives. The countdown
  // deadline is anchored to LOCAL receipt time + timeout_seconds rather
  // than the server's expires_at, so client clock skew can never close a
  // live prompt early (the wire expires_at stays authoritative server-side).
  let nowMs = $state(Date.now());
  let deadlineMs = $state<number | null>(null);

  $effect(() => {
    if (prompt) {
      frameHeight = DEFAULT_FRAME_HEIGHT;
      submitting = false;
      submitError = null;
      frameLoads = 0;
      deadlineMs = prompt.timeout_seconds > 0 ? Date.now() + prompt.timeout_seconds * 1000 : null;
    } else {
      deadlineMs = null;
    }
  });

  // At zero the backend has already timed the tool out, so the modal just
  // closes locally.
  let secondsLeft = $derived(
    deadlineMs !== null ? Math.max(0, Math.ceil((deadlineMs - nowMs) / 1000)) : null
  );

  $effect(() => {
    if (!prompt) return;
    nowMs = Date.now();
    const id = setInterval(() => {
      nowMs = Date.now();
    }, 1000);
    return () => clearInterval(id);
  });

  $effect(() => {
    if (isOpen && secondsLeft === 0) {
      onResolved();
    }
  });

  function formatCountdown(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  async function resolvePrompt(status: 'submitted' | 'cancelled', values?: Record<string, unknown>) {
    if (!prompt || submitting) return;
    submitting = true;
    submitError = null;
    try {
      await api.submitUiPromptResult(prompt.prompt_id, {
        status,
        values: status === 'submitted' ? (values ?? {}) : null,
      });
      onResolved();
    } catch (err) {
      if (status === 'cancelled') {
        // Dismissal must always work; the backend timeout covers delivery.
        onResolved();
        return;
      }
      submitError = err instanceof Error ? err.message : String(err);
    } finally {
      submitting = false;
    }
  }

  function handleMessage(event: MessageEvent) {
    if (!prompt || !iframeEl) return;
    if (event.source !== iframeEl.contentWindow) return;
    const data = event.data as Record<string, unknown> | null;
    if (!data || data.source !== 'nymeria-ui-prompt' || data.prompt_id !== prompt.prompt_id) return;
    if (data.type === 'submit') {
      const values = (data.values && typeof data.values === 'object')
        ? (data.values as Record<string, unknown>)
        : {};
      void resolvePrompt('submitted', values);
    } else if (data.type === 'cancel') {
      void resolvePrompt('cancelled');
    } else if (data.type === 'resize' && typeof data.height === 'number' && Number.isFinite(data.height)) {
      frameHeight = Math.max(MIN_FRAME_HEIGHT, Math.ceil(data.height));
    }
  }

  // Residual-channel guard: a sandboxed document can always navigate ITSELF
  // (CSP's abandoned navigate-to never shipped; the sandbox only withholds
  // TOP navigation), and a self-navigation's URL can carry data. The srcdoc
  // fires exactly one load event, so any later load means the agent HTML
  // navigated the frame: cancel the prompt so the form (and any further
  // interaction with it) is gone. The counter resets per prompt alongside
  // the {#key}-driven iframe recreation.
  function handleFrameLoad() {
    frameLoads += 1;
    if (frameLoads > 1) {
      void resolvePrompt('cancelled');
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (isOpen && event.key === 'Escape') {
      void resolvePrompt('cancelled');
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} onmessage={handleMessage} />

{#if isOpen && prompt}
  <div class="ui-prompt-backdrop" in:fade={OVERLAY_FADE_IN} out:fade={OVERLAY_FADE_OUT}>
    <div
      class="ui-prompt-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby={`ui-prompt-title-${prompt.prompt_id}`}
      tabindex="-1"
      use:trapFocus
      in:fly={DIALOG_RISE_IN}
      out:fly={DIALOG_RISE_OUT}
    >
      <header class="prompt-header">
        <h2 id={`ui-prompt-title-${prompt.prompt_id}`}>{heading}</h2>
        <button
          class="close-btn"
          type="button"
          aria-label="Dismiss without answering"
          data-tooltip="Dismiss without answering"
          onclick={() => void resolvePrompt('cancelled')}
        >
          <Icon name="x" size={18} />
        </button>
      </header>

      <div class="prompt-body">
        {#key prompt.prompt_id}
          <!-- allow-scripts ONLY; never add allow-same-origin (see header comment). -->
          <iframe
            bind:this={iframeEl}
            class="prompt-frame"
            title={heading}
            sandbox="allow-scripts"
            {srcdoc}
            onload={handleFrameLoad}
            style={`height: ${frameHeight}px;`}
          ></iframe>
        {/key}

        {#if submitError}
          <div class="alert error" role="alert">
            <span class="alert-icon" aria-hidden="true"><Icon name="warning" size={16} /></span>
            <div class="alert-body">
              <div class="alert-title">Could not send your answer</div>
              <div class="alert-text">{submitError} Submit the form again to retry.</div>
            </div>
          </div>
        {/if}
      </div>

      <footer class="prompt-footer">
        <span class="waiting-hint">
          {#if submitting}
            Sending your answer…
          {:else}
            Nymeria is waiting for this form.
          {/if}
          {#if secondsLeft !== null && !submitting}
            <span class="countdown" class:urgent={secondsLeft <= 30}>
              Expires in {formatCountdown(secondsLeft)}
            </span>
          {/if}
        </span>
        <Button variant="ghost" type="button" disabled={submitting} onclick={() => void resolvePrompt('cancelled')}>
          Dismiss
        </Button>
      </footer>
    </div>
  </div>
{/if}

<style>
  .ui-prompt-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .ui-prompt-modal {
    position: relative;
    /* §7 glass-surface exception, matching Modal.svelte: the hairline
       --glass-border is edge definition for the glass surface, and the
       elevation shadow is the floating-layer treatment. */
    background: var(--glass-bg-strong);
    backdrop-filter: var(--glass-blur-strong);
    -webkit-backdrop-filter: var(--glass-blur-strong);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-xl);
    box-shadow: var(--shadow-xl);
    display: flex;
    flex-direction: column;
    width: min(720px, 92vw);
    max-height: 90vh;
    overflow: hidden;
  }

  .prompt-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-lg);
    border-bottom: 1px solid var(--border-subtle);
  }

  .prompt-header h2 {
    margin: 0;
    font-size: var(--font-size-lg);
    font-weight: 600;
    color: var(--text-primary);
    line-height: 1.2;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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

  .prompt-body {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    padding: var(--spacing-md) var(--spacing-lg);
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }

  .prompt-frame {
    width: 100%;
    min-height: 160px;
    max-height: 65vh;
    border: 0;
    border-radius: var(--radius-md);
    background: transparent;
  }

  .alert.error {
    display: grid;
    grid-template-columns: 16px 1fr;
    align-items: start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    line-height: 1.4;
    background: color-mix(in srgb, var(--error) 12%, transparent);
    color: var(--error);
    border: 1px solid color-mix(in srgb, var(--error) 32%, transparent);
  }

  .alert-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    line-height: 0;
    margin-top: 2px;
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
  }

  .prompt-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-lg);
    border-top: 1px solid var(--border-subtle);
  }

  .waiting-hint {
    display: inline-flex;
    align-items: baseline;
    gap: var(--spacing-sm);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    min-width: 0;
  }

  .countdown {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
    font-variant-numeric: tabular-nums;
  }

  .countdown.urgent {
    color: var(--error);
  }
</style>
