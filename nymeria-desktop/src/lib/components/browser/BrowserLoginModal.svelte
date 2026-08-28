<script lang="ts">
  import { fade, fly } from 'svelte/transition';
  import { api } from '$lib/services/api';
  import type { BrowserLoginInputEvent } from '$lib/services/api/browser-login';
  import type { ActiveBrowserLogin } from '$lib/stores/browserLogin.svelte';
  import { browserLoginStore } from '$lib/stores/browserLogin.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import { trapFocus } from '$lib/actions/focus';
  import { pushOverlay, removeOverlay, isTopOverlay } from '$lib/utils/overlayStack';
  import {
    OVERLAY_FADE_IN,
    OVERLAY_FADE_OUT,
    DIALOG_RISE_IN,
    DIALOG_RISE_OUT,
  } from '$lib/utils/transitions';
  import {
    cdpModifiers,
    keyEventFor,
    normalizedPoint,
    takeWireBatch,
  } from '$lib/utils/browserLoginInput';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import InlineLoader from '../common/InlineLoader.svelte';

  /**
   * The live login handoff viewer: streams one Chrome tab as JPEG frames
   * and forwards the user's keys and clicks, so they can log the agent's
   * browser into a site by hand while Nymeria is paused and structurally
   * unable to see the screen (frames flow backend -> desktop only).
   *
   * Like UiPromptModal, the backdrop deliberately does NOT close on click:
   * a stray click mid-login would end the session under the user's hands.
   * Ending is explicit: "Hand the tab back" (completed), "Cancel login" /
   * Escape / the X (cancelled), or a server-side ending arriving on the
   * stream or the autonomous bus (expired, aborted, failed), which shows
   * its reason instead of silently vanishing the window.
   *
   * Input rules with teeth live in utils/browserLoginInput.ts (normalized
   * clamped coordinates, atomic keys, the paste-chord suppression). The
   * one channel here carries plaintext passwords: nothing from the input
   * path is ever logged or persisted.
   */

  interface Props {
    login: ActiveBrowserLogin | null;
    // Carries the id of the session that was actually closed, so the parent
    // clears THAT session and never a newer one that displaced it.
    onResolved: (sessionId: string) => void;
  }

  let { login, onResolved }: Props = $props();

  const DEFAULT_FRAME_WIDTH = 1280;
  const DEFAULT_FRAME_HEIGHT = 800;

  let isOpen = $derived(login !== null);
  let session = $derived(login?.session ?? null);
  let endedReason = $derived(login?.ended ?? null);

  let latestFrame = $state<string | null>(null);
  let frameWidth = $state(DEFAULT_FRAME_WIDTH);
  let frameHeight = $state(DEFAULT_FRAME_HEIGHT);
  let attached = $state(false);
  let streamLost = $state(false);
  let inputError = $state<string | null>(null);
  let ending = $state(false);
  let announcement = $state('');
  let imgEl = $state<HTMLImageElement | null>(null);
  let panelEl = $state<HTMLDivElement | null>(null);
  // Last rendered frame seq; plain (non-reactive) on purpose, it only feeds
  // the reconnect cursor.
  let lastSeq = 0;

  let host = $derived.by(() => {
    const raw = session?.url?.trim() || '';
    if (!raw) return 'the site';
    try {
      return new URL(raw).host || raw.slice(0, 80);
    } catch {
      return raw.slice(0, 80);
    }
  });

  const END_COPY: Record<string, string> = {
    completed: 'Logged in. Nymeria has the tab back and will continue.',
    expired:
      'The login window hit its 10 minute limit. Ask Nymeria to open a new one if you were not finished.',
    cancelled: 'The login was cancelled.',
    aborted: 'The task was stopped, which ended this login.',
    failed: 'The live view could not run.',
  };
  let endedCopy = $derived(
    endedReason ? END_COPY[endedReason] || 'The session has ended.' : null
  );

  // Countdown anchored to LOCAL receipt time + seconds_remaining, never the
  // server's expires_at, so client clock skew can never close a live session
  // early. The TTL is hard and never extended by activity; at zero the modal
  // stays open and waits for the server's own ending (its sweep runs every
  // 15s), so the last keystrokes still land instead of being cut off.
  let nowMs = $state(Date.now());
  let deadlineMs = $derived(
    login ? login.receivedAtMs + login.session.seconds_remaining * 1000 : null
  );
  let secondsLeft = $derived(
    deadlineMs !== null ? Math.max(0, Math.ceil((deadlineMs - nowMs) / 1000)) : null
  );

  $effect(() => {
    if (!isOpen) return;
    nowMs = Date.now();
    const id = setInterval(() => {
      nowMs = Date.now();
    }, 1000);
    return () => clearInterval(id);
  });

  function formatCountdown(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  // Escape closes only the topmost overlay (opening this over Global
  // Settings must not close both on one keypress).
  let layer: symbol | null = null;
  $effect(() => {
    if (isOpen) {
      const id = pushOverlay('Browser login');
      layer = id;
      return () => {
        removeOverlay(id);
        layer = null;
      };
    }
  });

  // Reset per-session state when a new session arrives.
  $effect(() => {
    const sid = login?.session.session_id;
    if (sid) {
      latestFrame = null;
      lastSeq = 0;
      frameWidth = DEFAULT_FRAME_WIDTH;
      frameHeight = DEFAULT_FRAME_HEIGHT;
      attached = false;
      streamLost = false;
      inputError = null;
      announcement = '';
      queue = [];
    }
  });

  $effect(() => {
    if (endedCopy) announcement = endedCopy;
  });

  // ---- frame stream ----------------------------------------------------

  $effect(() => {
    const current = login;
    if (!current || current.ended) return;
    const sid = current.session.session_id;
    const abortController = new AbortController();
    let disposed = false;

    (async () => {
      let attempts = 0;
      while (!disposed) {
        const stream = api.streamBrowserLoginFrames(sid, {
          fromSeq: lastSeq,
          signal: abortController.signal,
        });
        let lost: { http_status: number | null } | null = null;
        for await (const event of stream) {
          if (disposed) break;
          if (event.type === 'login_attach') {
            attached = true;
            streamLost = false;
            attempts = 0;
            announcement = 'Live view connected.';
          } else if (event.type === 'login_frame') {
            latestFrame = event.data;
            lastSeq = event.seq;
            const meta = event.metadata;
            const width = Number(meta?.deviceWidth);
            const height = Number(meta?.deviceHeight);
            if (Number.isFinite(width) && width > 0) frameWidth = width;
            if (Number.isFinite(height) && height > 0) frameHeight = height;
          } else if (event.type === 'login_end') {
            browserLoginStore.endById(sid, event.end_reason || 'cancelled');
            return;
          } else if (event.type === 'login_stream_lost') {
            lost = event;
          }
        }
        if (disposed) return;
        if (lost && lost.http_status === 404) {
          // The session is gone server-side (ended and reaped). The bus
          // event usually carried the real reason already; this is the
          // fallback for a viewer that missed it.
          browserLoginStore.endById(sid, 'ended');
          return;
        }
        attempts += 1;
        if (attempts > 5) {
          streamLost = true;
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 800 * attempts));
      }
    })();

    return () => {
      disposed = true;
      abortController.abort();
    };
  });

  // ---- input path ------------------------------------------------------
  // Adaptive batching, one POST in flight: the first event flushes
  // immediately, anything arriving mid-flight piles up and goes as the next
  // batch (the same shape the extension uses for frames). Never logged.

  let queue: BrowserLoginInputEvent[] = [];
  let sendInFlight = false;

  function enqueue(event: BrowserLoginInputEvent): void {
    if (!session || endedReason) return;
    // Collapse pending pointer moves AT ENQUEUE, not only in takeWireBatch:
    // while a POST is in flight a 60Hz move stream would otherwise grow the
    // queue unboundedly between flushes. The batch-side collapse stays as
    // the wire-shape guarantee; this one bounds memory.
    if (event.type === 'mouse' && event.action === 'move') {
      const pendingMove = queue.findIndex(
        (entry) => entry.type === 'mouse' && entry.action === 'move'
      );
      if (pendingMove !== -1) {
        queue[pendingMove] = event;
        void flushInput();
        return;
      }
    }
    queue.push(event);
    void flushInput();
  }

  async function flushInput(): Promise<void> {
    if (sendInFlight) return;
    const current = session;
    if (!current) return;
    sendInFlight = true;
    try {
      while (queue.length > 0 && !endedReason) {
        if (session?.session_id !== current.session_id) {
          // The session was displaced mid-flight; `queue` now belongs to
          // the NEW session, and draining it here would post the new
          // session's keystrokes under the old session id. Bail and let
          // the re-kick below flush under the live id.
          break;
        }
        const { batch, rest } = takeWireBatch(queue);
        queue = rest;
        if (batch.length === 0) break;
        try {
          const ack = await api.sendBrowserLoginInput(current.session_id, batch);
          inputError = null;
          if (!ack.session_active) {
            browserLoginStore.endById(current.session_id, 'ended');
            break;
          }
        } catch (err) {
          inputError = `${humanizeErrorText(err, { action: 'send', resource: 'your input' })} Your last input did not reach the tab; try it again.`;
          break;
        }
      }
    } finally {
      sendInFlight = false;
    }
    if (queue.length > 0 && session && session.session_id !== current.session_id) {
      // One-shot re-kick with a fresh `current`; its ids match, so this
      // cannot recurse further.
      void flushInput();
    }
  }

  function pointFrom(clientX: number, clientY: number): { x: number; y: number } | null {
    if (!imgEl) return null;
    return normalizedPoint(imgEl.getBoundingClientRect(), clientX, clientY);
  }

  function onRegionKeydown(event: KeyboardEvent): void {
    if (!session || endedReason) return;
    if (event.key === 'Escape') {
      // The documented way OUT of the driving surface: Escape stops driving
      // and moves focus to the viewer chrome; it must not cancel the login
      // (the window-level Escape does that once focus is outside).
      event.preventDefault();
      event.stopPropagation();
      panelEl?.focus();
      return;
    }
    const wire = keyEventFor(event);
    if (!wire) return;
    event.preventDefault();
    enqueue(wire);
  }

  function onRegionPaste(event: ClipboardEvent): void {
    if (!session || endedReason) return;
    event.preventDefault();
    const text = event.clipboardData?.getData('text') ?? '';
    if (text) enqueue({ type: 'text', text: text.slice(0, 4096) });
  }

  function onRegionClick(event: MouseEvent): void {
    if (!session || endedReason) return;
    const point = pointFrom(event.clientX, event.clientY);
    if (!point) return;
    enqueue({
      type: 'mouse',
      action: 'click',
      ...point,
      button: 'left',
      click_count: 1,
      modifiers: cdpModifiers(event),
    });
  }

  function onRegionContextMenu(event: MouseEvent): void {
    if (!session || endedReason) return;
    event.preventDefault();
    const point = pointFrom(event.clientX, event.clientY);
    if (!point) return;
    enqueue({
      type: 'mouse',
      action: 'click',
      ...point,
      button: 'right',
      click_count: 1,
      modifiers: cdpModifiers(event),
    });
  }

  function onRegionPointerMove(event: PointerEvent): void {
    if (!session || endedReason || !latestFrame) return;
    const point = pointFrom(event.clientX, event.clientY);
    if (!point) return;
    enqueue({ type: 'mouse', action: 'move', ...point, modifiers: cdpModifiers(event) });
  }

  function onRegionWheel(event: WheelEvent): void {
    if (!session || endedReason) return;
    event.preventDefault();
    const point = pointFrom(event.clientX, event.clientY);
    if (!point) return;
    enqueue({ type: 'wheel', ...point, delta_x: event.deltaX, delta_y: event.deltaY });
  }

  // ---- ending ----------------------------------------------------------

  async function endSession(reason: 'completed' | 'cancelled'): Promise<void> {
    const current = session;
    if (!current || ending) return;
    // Capture before the await: a newer session could displace `login`
    // during the POST, and both the POST target and the parent's clear must
    // be the session we actually closed.
    const resolvedId = current.session_id;
    ending = true;
    try {
      await api.endBrowserLoginSession(resolvedId, reason);
    } catch {
      // Closing must always work: the backend TTL owns cleanup either way.
    } finally {
      ending = false;
    }
    onResolved(resolvedId);
  }

  function closeViewer(): void {
    const current = session;
    if (current) onResolved(current.session_id);
  }

  function handleWindowKeydown(event: KeyboardEvent): void {
    if (!isOpen || event.key !== 'Escape') return;
    if (!layer || !isTopOverlay(layer)) return;
    if (endedReason) {
      closeViewer();
    } else {
      void endSession('cancelled');
    }
  }
</script>

<svelte:window onkeydown={handleWindowKeydown} />

{#if isOpen && session}
  <div class="browser-login-backdrop" in:fade={OVERLAY_FADE_IN} out:fade={OVERLAY_FADE_OUT}>
    <div
      bind:this={panelEl}
      class="browser-login-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby={`browser-login-title-${session.session_id}`}
      tabindex="-1"
      use:trapFocus
      in:fly={DIALOG_RISE_IN}
      out:fly={DIALOG_RISE_OUT}
    >
      <header class="login-header">
        <span class="site-icon" aria-hidden="true"><Icon name="globe" size={18} /></span>
        <div class="heading-block">
          <h2 id={`browser-login-title-${session.session_id}`}>Log in by hand</h2>
          <span class="site-host">{host}</span>
        </div>
        <button
          class="close-btn"
          type="button"
          aria-label="Cancel without finishing"
          data-tooltip="Cancel without finishing"
          onclick={() => (endedReason ? closeViewer() : void endSession('cancelled'))}
        >
          <Icon name="x" size={18} />
        </button>
      </header>

      {#if !endedReason}
        <p class="driving-banner">
          You are driving. Nymeria is paused and cannot see this screen, so what
          you type here stays between you and {host}.
        </p>
      {/if}

      <div class="login-body">
        {#if endedReason}
          <div class="ended-state" class:success={endedReason === 'completed'}>
            <span class="ended-icon" aria-hidden="true">
              <Icon name={endedReason === 'completed' ? 'success' : 'info'} size={20} />
            </span>
            <p>{endedCopy}</p>
          </div>
        {:else}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
          <!-- The linter does not know role="application": this surface IS
               interactive (a remote view of a real browser tab), and the
               application role is what tells a screen reader to pass keys
               through to it instead of intercepting them in browse mode. -->
          <div
            class="driving-region"
            role="application"
            aria-label={`Live browser view of ${host}. Your keys and clicks are sent to the tab. Press Escape to stop driving and reach the viewer controls.`}
            tabindex="0"
            onkeydown={onRegionKeydown}
            onpaste={onRegionPaste}
            onclick={onRegionClick}
            oncontextmenu={onRegionContextMenu}
            onpointermove={onRegionPointerMove}
            onwheel={onRegionWheel}
            style={`aspect-ratio: ${frameWidth} / ${frameHeight};`}
          >
            {#if latestFrame}
              <img
                bind:this={imgEl}
                class="frame"
                src={`data:image/jpeg;base64,${latestFrame}`}
                alt={`Live view of the browser tab you are logging in at ${host}`}
                draggable="false"
              />
            {:else}
              <div class="frame-placeholder">
                {#if streamLost}
                  <div class="alert error" role="alert">
                    <span class="alert-icon" aria-hidden="true"><Icon name="warning" size={16} /></span>
                    <div class="alert-body">
                      <div class="alert-title">The connection to the tab was lost</div>
                      <div class="alert-text">
                        The session may still be running. Close the viewer and ask
                        Nymeria to open the login window again.
                      </div>
                    </div>
                  </div>
                {:else if attached}
                  <InlineLoader text="Waiting for the first frame…" />
                {:else}
                  <InlineLoader text="Connecting to the tab…" />
                {/if}
              </div>
            {/if}
          </div>
          {#if inputError}
            <div class="alert error" role="alert">
              <span class="alert-icon" aria-hidden="true"><Icon name="warning" size={16} /></span>
              <div class="alert-body">
                <div class="alert-text">{inputError}</div>
              </div>
            </div>
          {/if}
        {/if}
      </div>

      <footer class="login-footer">
        {#if endedReason}
          <span class="footer-hint"></span>
          <Button variant="primary" type="button" onclick={closeViewer}>Close viewer</Button>
        {:else}
          <span class="footer-hint">
            <span>Hand the tab back when you have logged in.</span>
            {#if secondsLeft !== null}
              <span class="countdown" class:urgent={secondsLeft <= 60}>
                {formatCountdown(secondsLeft)} left, the timer cannot be extended
              </span>
            {/if}
          </span>
          <div class="footer-actions">
            <Button
              variant="ghost"
              type="button"
              disabled={ending}
              onclick={() => void endSession('cancelled')}
            >
              Cancel login
            </Button>
            <Button
              variant="primary"
              type="button"
              loading={ending}
              onclick={() => void endSession('completed')}
            >
              Hand the tab back
            </Button>
          </div>
        {/if}
      </footer>

      <!-- Boundary announcements only (connected, ended): a 20fps frame
           stream and a per-second countdown must never ride a live region. -->
      <span class="visually-hidden" aria-live="polite">{announcement}</span>
    </div>
  </div>
{/if}

<style>
  .browser-login-backdrop {
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

  .browser-login-modal {
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
    width: min(960px, 94vw);
    max-height: 92vh;
    overflow: hidden;
  }

  .login-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-lg);
    border-bottom: 1px solid var(--border-subtle);
  }

  .site-icon {
    display: inline-flex;
    color: var(--text-secondary);
  }

  .heading-block {
    display: flex;
    align-items: baseline;
    gap: var(--spacing-sm);
    min-width: 0;
    flex: 1;
  }

  .login-header h2 {
    margin: 0;
    font-size: var(--font-size-lg);
    font-weight: 600;
    color: var(--text-primary);
    line-height: 1.2;
    white-space: nowrap;
  }

  .site-host {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
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

  .close-btn:active {
    transform: scale(var(--press-scale-icon));
  }

  .driving-banner {
    margin: 0;
    padding: var(--spacing-xs) var(--spacing-lg);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    background: var(--accent-tint-bg);
    border-bottom: 1px solid var(--accent-tint-border);
  }

  .login-body {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    padding: var(--spacing-md) var(--spacing-lg);
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }

  .driving-region {
    position: relative;
    width: 100%;
    max-height: 68vh;
    background: var(--bg-base);
    border-radius: var(--radius-md);
    overflow: hidden;
    cursor: crosshair;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .driving-region:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
  }

  .frame {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    user-select: none;
    -webkit-user-drag: none;
  }

  .frame-placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--spacing-2xl);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .ended-state {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-lg);
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--text-muted) 8%, transparent);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .ended-state.success {
    background: color-mix(in srgb, var(--success) 10%, transparent);
  }

  .ended-state p {
    margin: 0;
  }

  .ended-icon {
    display: inline-flex;
    color: var(--text-secondary);
  }

  .ended-state.success .ended-icon {
    color: var(--success);
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

  .login-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-lg);
    border-top: 1px solid var(--border-subtle);
  }

  .footer-hint {
    display: inline-flex;
    flex-direction: column;
    gap: 2px;
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    min-width: 0;
  }

  .footer-actions {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .countdown {
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
  }

  .countdown.urgent {
    color: var(--error);
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }
</style>
