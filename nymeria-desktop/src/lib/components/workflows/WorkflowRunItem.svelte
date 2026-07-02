<script lang="ts">
  import type { WorkflowRun } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { formatRelativeTime } from '$lib/utils/time';

  interface Props {
    run: WorkflowRun;
  }

  let { run }: Props = $props();

  let expanded = $state(false);

  // Status -> icon/colour follows the ActivityItem convention: colour is
  // reserved for genuine status (green ok, red error, amber attention),
  // everything else is neutral muted gray.
  let icon = $derived.by(() => {
    switch (run.status) {
      case 'ok':
        return 'success';
      case 'error':
        return 'close';
      case 'timeout':
      case 'needs_approval':
        return 'clock';
      default:
        return 'info';
    }
  });

  let color = $derived.by(() => {
    switch (run.status) {
      case 'ok':
        return 'var(--success)';
      case 'error':
        return 'var(--error)';
      case 'timeout':
      case 'needs_approval':
        return 'var(--warning)';
      default:
        return 'var(--text-muted)';
    }
  });

  // Short outcome label; the icon carries ok/error, so this only names the
  // non-obvious statuses.
  let outcome = $derived.by(() => {
    switch (run.status) {
      case 'needs_approval':
        return 'Waiting for approval';
      case 'timeout':
        return 'Timed out';
      case 'cancelled':
        return 'Cancelled';
      default:
        return '';
    }
  });

  // Error detail from the envelope, when the run failed.
  let errorDetail = $derived.by(() => {
    if (run.status !== 'error') return '';
    const err = run.envelope?.error as Record<string, unknown> | undefined;
    if (!err) return '';
    const kind = (err.kind as string) || '';
    const message = (err.message as string) || '';
    return message || kind;
  });

  let timeAgo = $derived(formatRelativeTime(new Date(run.timestamp)));

  function toggle() {
    if (run.steps.length === 0) return;
    expanded = !expanded;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggle();
    }
  }
</script>

{#snippet runRow()}
  <div class="run-icon" style="color: {color}">
    <Icon name={icon} size={12} />
  </div>

  <div class="run-content">
    <div class="run-body">
      <span class="run-lead">{run.workflow_id}</span>
      {#if outcome}<span class="run-detail">{' · '}{outcome}</span>{/if}
      {#if errorDetail}<span class="run-detail">{' · '}{errorDetail}</span>{/if}
    </div>

    <div class="run-meta">
      <span class="step-count">{run.steps.length} {run.steps.length === 1 ? 'step' : 'steps'}</span>
      <span class="run-time">{timeAgo}</span>
      {#if run.steps.length > 0}
        <span class="run-chevron" class:rotated={expanded} aria-hidden="true">
          <Icon name="chevronDown" size={12} />
        </span>
      {/if}
    </div>

    {#if expanded}
      <ol class="step-trace">
        {#each run.steps as step (step.step)}
          <li class="trace-step" class:errored={step.status === 'error'}>
            <span class="trace-verb">{step.verb}</span>
            <span class="trace-duration">{step.duration_ms}ms</span>
            {#if step.error_kind}
              <span class="trace-error">{step.error_kind}</span>
            {/if}
          </li>
        {/each}
      </ol>
    {/if}
  </div>
{/snippet}

<!-- Runs with a step trace expand in place (role="button", Enter/Space),
     matching the ActivityItem expand affordance; stepless records (e.g. a
     refused resume) render as a plain row. -->
{#if run.steps.length > 0}
  <div
    class="run-item expandable"
    class:expanded
    role="button"
    tabindex="0"
    aria-expanded={expanded}
    onclick={toggle}
    onkeydown={handleKeydown}
  >
    {@render runRow()}
  </div>
{:else}
  <div class="run-item">
    {@render runRow()}
  </div>
{/if}

<style>
  .run-item {
    display: flex;
    gap: var(--spacing-sm);
    width: 100%;
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
    animation: staggerFadeIn var(--transition-slow) backwards;
  }

  .run-item:nth-child(1) { animation-delay: 0.03s; }
  .run-item:nth-child(2) { animation-delay: 0.06s; }
  .run-item:nth-child(3) { animation-delay: 0.09s; }
  .run-item:nth-child(4) { animation-delay: 0.12s; }
  .run-item:nth-child(5) { animation-delay: 0.15s; }
  .run-item:nth-child(6) { animation-delay: 0.18s; }
  .run-item:nth-child(7) { animation-delay: 0.21s; }
  .run-item:nth-child(8) { animation-delay: 0.24s; }
  .run-item:nth-child(9) { animation-delay: 0.27s; }
  .run-item:nth-child(10) { animation-delay: 0.3s; }

  .run-item:hover {
    background: var(--bg-hover);
  }

  .run-item.expandable {
    cursor: pointer;
  }

  .run-item.expandable:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .run-icon {
    flex-shrink: 0;
    margin-top: -1px;
    margin-left: 2px;
    opacity: 0.8;
  }

  .run-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  .run-body {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .run-lead {
    font-weight: 600;
    color: var(--text-primary);
  }

  .run-meta {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    min-height: 18px;
  }

  .step-count {
    font-size: var(--font-size-2xs);
    font-weight: 500;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .run-time {
    margin-left: auto;
    flex-shrink: 0;
    white-space: nowrap;
    font-size: var(--font-size-3xs);
    color: var(--text-muted);
  }

  .run-chevron {
    display: flex;
    align-items: center;
    flex-shrink: 0;
    color: var(--text-muted);
    transition: transform 120ms var(--ease-out);
  }

  .run-chevron.rotated {
    transform: rotate(180deg);
  }

  /* Expanded step trace: a quiet numbered list of nym.* calls. */
  .step-trace {
    margin: var(--spacing-xs) 0 0;
    padding: 0 0 0 var(--spacing-md);
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .trace-step {
    display: flex;
    align-items: baseline;
    gap: var(--spacing-xs);
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .trace-verb {
    font-family: var(--font-mono);
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .trace-duration {
    flex-shrink: 0;
    font-size: var(--font-size-3xs);
    color: var(--text-muted);
  }

  .trace-error {
    flex-shrink: 0;
    font-size: var(--font-size-3xs);
    color: var(--error);
  }

  .trace-step.errored .trace-verb {
    color: var(--error);
  }
</style>
