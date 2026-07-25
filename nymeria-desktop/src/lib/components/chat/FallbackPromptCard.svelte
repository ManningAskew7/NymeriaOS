<script lang="ts">
  import type { FallbackPromptInfo } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import { chatStore } from '$lib/stores/chat.svelte';

  interface Props {
    info: FallbackPromptInfo;
  }

  let { info }: Props = $props();

  // LLM fallback consent card (llm-fallback-consent Phase 2): the turn is
  // parked before a model switch, waiting for the user (auto-swap on
  // timeout). Resolution follows the ToolCallCard approval idiom: a 200 is
  // authoritative and clears optimistically; the fallback_prompt_resolved
  // event is the cross-client (and timeout/abort) flip.

  const isRefusal = $derived(info.kind === 'refusal');

  const labelText = $derived(
    isRefusal
      ? `${info.fromModel || 'The model'} refused this turn`
      : `${info.fromModel || 'The model'} is failing`
  );

  const metaText = $derived(
    isRefusal
      ? 'safety classifier refusal, often a false positive'
      : `${info.reason || 'provider error'}${info.httpStatus ? ` (HTTP ${info.httpStatus})` : ''}`
  );

  const declineGuidance = $derived(
    isRefusal
      ? "If you don't swap: rewind the thread (edit an earlier message) and " +
        'rephrase the request; if it still refuses, rewind further, compact ' +
        'the thread, or switch this thread to a different model.'
      : "If you don't swap, the turn fails with the original provider error."
  );

  function holdLabel(seconds: number): string {
    if (seconds % 3600 === 0 && seconds >= 3600) {
      const hours = seconds / 3600;
      return hours === 1 ? '1 hour' : `${hours} hours`;
    }
    if (seconds % 60 === 0 && seconds >= 60) return `${seconds / 60} min`;
    return `${seconds}s`;
  }

  const holdOptions = $derived(info.holdOptions?.length ? info.holdOptions : [7200]);

  // Preselect the backend's default hold (the global fallback hold, preset
  // 2h) when it is one of the offered presets.
  const initialHold = (() => {
    const options = info.holdOptions?.length ? info.holdOptions : [7200];
    const preferred =
      info.defaultHoldSeconds && options.includes(info.defaultHoldSeconds)
        ? info.defaultHoldSeconds
        : options.includes(7200)
          ? 7200
          : options[0];
    return String(preferred);
  })();
  let holdChoice = $state(initialHold);

  // Countdown to the auto-swap deadline; stops ticking once resolved.
  let now = $state(Date.now());
  $effect(() => {
    if (info.resolved) return;
    const timer = setInterval(() => {
      now = Date.now();
    }, 1000);
    return () => clearInterval(timer);
  });

  const remainingSeconds = $derived.by(() => {
    const expires = Date.parse(info.expiresAt || '');
    if (Number.isNaN(expires)) return null;
    return Math.max(0, Math.round((expires - now) / 1000));
  });

  const countdownText = $derived.by(() => {
    if (remainingSeconds == null) return '';
    if (remainingSeconds <= 0) return 'Auto-swapping...';
    const minutes = Math.floor(remainingSeconds / 60);
    const seconds = remainingSeconds % 60;
    return `Auto-swaps in ${minutes}:${String(seconds).padStart(2, '0')}`;
  });

  const resolvedText = $derived.by(() => {
    const resolved = info.resolved;
    if (!resolved) return '';
    switch (resolved.outcome) {
      case 'approved': {
        const hold = resolved.holdPermanent
          ? ' until reverted'
          : resolved.holdSeconds
            ? ` for ${holdLabel(resolved.holdSeconds)}`
            : '';
        return `Swapped to ${info.toModel}${hold}`;
      }
      case 'declined':
        return 'Not swapped';
      case 'timeout':
        return `Auto-swapped to ${info.toModel}`;
      case 'aborted':
        return 'Turn stopped';
      default:
        return 'No longer pending';
    }
  });

  let busy = $state(false);
  let error = $state('');

  // Once the deadline passes the backend is already auto-swapping; a resolve
  // sent now would race it into a 409. Freeze the controls and let the
  // fallback_prompt_resolved event flip the card.
  const deadlinePassed = $derived(remainingSeconds != null && remainingSeconds <= 0);

  async function resolve(approved: boolean) {
    if (busy || info.resolved || deadlinePassed) return;
    busy = true;
    error = '';
    try {
      const options = approved
        ? holdChoice === 'permanent'
          ? { holdPermanent: true }
          : { holdSeconds: Number(holdChoice) }
        : {};
      await api.resolveLlmFallbackPrompt(info.recordId, approved, options);
      chatStore.resolveFallbackPromptCard(info.recordId, {
        outcome: approved ? 'approved' : 'declined',
        holdSeconds:
          approved && holdChoice !== 'permanent' ? Number(holdChoice) : null,
        holdPermanent: approved && holdChoice === 'permanent'
      });
    } catch (e) {
      error = humanizeErrorText(e, {
        action: approved ? 'approve' : 'decline',
        resource: 'the model swap'
      });
      busy = false;
    }
  }
</script>

<div class="fallback-card" class:resolved={!!info.resolved}>
  <div class="fallback-header">
    <span class="icon">
      <Icon name="refresh" size={14} />
    </span>
    <span class="label">{labelText}</span>
    <span class="meta">{metaText}</span>
    {#if info.resolved}
      <span class="resolved-chip">{resolvedText}</span>
    {:else if countdownText}
      <span class="countdown">{countdownText}</span>
    {/if}
  </div>

  {#if !info.resolved}
    <div class="fallback-text">
      Switch to <strong>{info.toModel}</strong>?
    </div>
    <div class="fallback-actions">
      <label class="hold-label">
        Hold for
        <select bind:value={holdChoice} disabled={busy || deadlinePassed} aria-label="Fallback hold duration">
          {#each holdOptions as seconds (seconds)}
            <option value={String(seconds)}>{holdLabel(seconds)}</option>
          {/each}
          {#if info.allowPermanent}
            <option value="permanent">Until reverted</option>
          {/if}
        </select>
      </label>
      <button
        type="button"
        class="swap-btn"
        onclick={() => resolve(true)}
        disabled={busy || deadlinePassed}
        aria-label="Swap to the fallback model"
      >
        <Icon name="refresh" size={12} />
        Swap
      </button>
      <button
        type="button"
        class="decline-btn"
        onclick={() => resolve(false)}
        disabled={busy || deadlinePassed}
        aria-label="Keep the current model"
      >
        Don't swap
      </button>
    </div>
    <div class="fallback-guidance">{declineGuidance}</div>
    {#if error}
      <div class="fallback-error">{error}</div>
    {/if}
  {/if}
</div>

<style>
  .fallback-card {
    align-self: flex-start;
    display: flex;
    flex-direction: column;
    gap: 8px;
    width: min(760px, 88%);
    margin: var(--spacing-xs) 0 var(--spacing-md);
    padding: 10px 12px;
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--bg-elevated) 88%, var(--warning, var(--accent-primary)));
    border: 1px solid var(--border-subtle);
    color: var(--text-secondary);
    animation: fadeSlide var(--transition-fast);
  }

  .fallback-card.resolved {
    background: color-mix(in srgb, var(--bg-elevated) 92%, var(--accent-primary));
  }

  .fallback-header {
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
  }

  .icon {
    display: flex;
    align-items: center;
    color: var(--warning, var(--accent-primary));
  }

  .resolved .icon {
    color: var(--accent-primary);
  }

  .label {
    font-weight: 600;
    font-size: var(--font-size-xs);
    color: var(--text-primary);
  }

  .meta {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .countdown {
    margin-left: auto;
    white-space: nowrap;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--warning, var(--accent-primary));
    font-variant-numeric: tabular-nums;
  }

  .resolved-chip {
    margin-left: auto;
    white-space: nowrap;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--accent-primary);
  }

  .fallback-text {
    font-size: var(--font-size-xs);
    line-height: 1.5;
    color: var(--text-secondary);
  }

  .fallback-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .hold-label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .hold-label select {
    padding: 4px 8px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-xs);
  }

  .swap-btn,
  .decline-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 12px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-xs);
    font-weight: 600;
    cursor: pointer;
    transition: background var(--transition-fast), border-color var(--transition-fast);
  }

  .swap-btn:hover:not(:disabled) {
    background: color-mix(in srgb, var(--bg-elevated) 80%, var(--accent-primary));
    border-color: var(--accent-primary);
  }

  .decline-btn:hover:not(:disabled) {
    background: color-mix(in srgb, var(--bg-elevated) 84%, var(--text-muted));
    border-color: var(--text-muted);
  }

  .swap-btn:disabled,
  .decline-btn:disabled {
    opacity: 0.55;
    cursor: default;
  }

  .fallback-guidance {
    font-size: var(--font-size-xs);
    line-height: 1.5;
    color: var(--text-muted);
  }

  .fallback-error {
    font-size: var(--font-size-xs);
    color: var(--error, #e5484d);
  }

  @keyframes fadeSlide {
    from {
      opacity: 0;
      transform: translateY(-4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>
