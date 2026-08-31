<script lang="ts">
  import type { Trigger, TriggerSourceInfo } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';

  interface Props {
    trigger: Trigger;
    threadTitle?: string;
    onNavigateToThread?: () => void;
    onEdit?: (trigger: Trigger) => void;
    onHistory?: (trigger: Trigger) => void;
    animationDelay?: number;
  }

  let {
    trigger,
    threadTitle,
    onNavigateToThread,
    onEdit,
    onHistory,
    animationDelay = 0,
  }: Props = $props();

  let testResult = $state<string | null>(null);
  let testing = $state(false);

  let confirmDelete = $state(false);
  let toggling = $state(false);
  let resuming = $state(false);
  let resumeError = $state<string | null>(null);

  // The backend's auto-pause policy stops a trigger whose action keeps failing
  // and leaves `enabled` alone, so the toggle below still reads "on" while
  // nothing polls or fires. This flag is what the row must show instead.
  const autoPaused = $derived(!!trigger.auto_paused_at);

  const sourceInfo = $derived<TriggerSourceInfo | undefined>(
    triggersStore.sources[trigger.source_type]
  );
  const sourceIcon = $derived(sourceInfo?.icon || 'bolt');

  // §9 Concept 2: surface "create task" to users; keep `create_todo` as the
  // internal action-type value the backend understands.
  function actionLabel(type: string): string {
    if (type === 'create_todo') return 'create task';
    return type.replace('_', ' ');
  }

  function formatTimeAgo(dateStr: string | null): string {
    if (!dateStr) return 'Never';
    const diff = Date.now() - new Date(dateStr).getTime();
    const seconds = Math.floor(diff / 1000);
    if (seconds < 60) return 'Just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  }

  async function handleToggle() {
    toggling = true;
    try {
      await triggersStore.updateTrigger(trigger.id, { enabled: !trigger.enabled });
    } finally {
      toggling = false;
    }
  }

  async function handleDelete() {
    await triggersStore.deleteTrigger(trigger.id);
    confirmDelete = false;
  }

  // Clears the pause and the failure history in one call. The store swaps in
  // the returned trigger, so this whole note leaves with it.
  async function handleResume() {
    resuming = true;
    resumeError = null;
    try {
      await triggersStore.resumeTrigger(trigger.id);
    } catch (e) {
      resumeError = humanizeErrorText(e, { action: 'resume', resource: 'the trigger' });
    } finally {
      resuming = false;
    }
  }

  async function handleTest() {
    testing = true;
    testResult = null;
    try {
      const result = await triggersStore.testTrigger(trigger.id);
      testResult = result.conditions_pass
        ? `Preview: ${result.rendered_output.substring(0, 100)}${result.rendered_output.length > 100 ? '…' : ''}`
        : 'Conditions would NOT pass for sample event';
      setTimeout(() => { testResult = null; }, 5000);
    } catch (e) {
      testResult = humanizeErrorText(e, { action: 'test', resource: 'the trigger' });
      setTimeout(() => { testResult = null; }, 5000);
    } finally {
      testing = false;
    }
  }

  const healthColor = $derived(
    trigger.health_status === 'healthy' ? 'var(--success)'
    : trigger.health_status === 'degraded' ? 'var(--warning)'
    : 'var(--error)'
  );

  // "Nymeria stopped this trigger after 3 failed actions, 2h ago." Naming
  // Nymeria is the point: the row must not read as the user's own off switch.
  // The stamp is lowercased so the "Just now" branch reads as part of the
  // sentence; formatTimeAgo's "Never" branch is unreachable here, this only
  // renders when the stamp exists.
  const pauseSummary = $derived(
    `Nymeria stopped this trigger after ${trigger.action_failures} ` +
      `failed ${trigger.action_failures === 1 ? 'action' : 'actions'}, ` +
      `${formatTimeAgo(trigger.auto_paused_at).toLowerCase()}.`
  );
</script>

<div
  class="trigger-item"
  class:disabled={!trigger.enabled}
  style="animation-delay: {animationDelay}ms"
>
  <div class="trigger-main">
    <div class="trigger-icon">
      <Icon name={sourceIcon} size={14} />
    </div>

    <div class="trigger-info">
      <div class="trigger-name-row">
        <span class="trigger-name" class:strikethrough={!trigger.enabled}>
          {trigger.name}
        </span>
        <span class="health-dot" style="background: {healthColor}" title={trigger.health_status}></span>
      </div>
      <div class="trigger-meta">
        <span class="source-badge">{trigger.source_type}</span>
        <span class="action-badge">{actionLabel(trigger.action.type)}</span>
        <span class="fire-count">{trigger.fire_count}x</span>
        <span class="last-fired">{formatTimeAgo(trigger.last_fired)}</span>
      </div>
      {#if autoPaused}
        <div class="pause-note">
          <span class="paused-chip">
            <Icon name="pause" size={9} />
            <span>Auto-paused</span>
          </span>
          <span class="pause-text">{pauseSummary}</span>
          <button class="resume-btn" onclick={handleResume} disabled={resuming} type="button">
            <Icon name={resuming ? 'loading' : 'play'} size={11} />
            <span>{resuming ? 'Resuming…' : 'Resume'}</span>
          </button>
          {#if resumeError}
            <span class="pause-error" role="alert">{resumeError}</span>
          {/if}
        </div>
      {/if}
      {#if trigger.last_error && trigger.health_status !== 'healthy'}
        <div class="error-hint">{trigger.last_error}</div>
      {/if}
      {#if testResult}
        <div class="test-result">{testResult}</div>
      {/if}
    </div>

    <div class="trigger-actions">
      {#if threadTitle && onNavigateToThread}
        <button
          class="thread-badge"
          onclick={onNavigateToThread}
          type="button"
        >
          {threadTitle}
        </button>
      {/if}

      <label class="toggle">
        <input
          type="checkbox"
          checked={trigger.enabled}
          disabled={toggling}
          onchange={handleToggle}
          aria-label={trigger.enabled ? 'Disable trigger' : 'Enable trigger'}
        />
        <span class="toggle-track"></span>
      </label>

      <button class="action-btn" onclick={handleTest} disabled={testing} type="button" aria-label="Test trigger">
        <Icon name="terminal" size={13} />
      </button>

      {#if onHistory}
        <button class="action-btn" onclick={() => onHistory(trigger)} type="button" aria-label="History">
          <Icon name="clock" size={13} />
        </button>
      {/if}

      {#if onEdit}
        <button class="action-btn" onclick={() => onEdit(trigger)} type="button" aria-label="Edit trigger">
          <Icon name="edit" size={13} />
        </button>
      {/if}

      {#if !confirmDelete}
        <button class="action-btn delete-btn" onclick={() => (confirmDelete = true)} type="button" aria-label="Delete trigger">
          <Icon name="x" size={13} />
        </button>
      {:else}
        <button class="action-btn confirm-delete" onclick={handleDelete} type="button" aria-label="Confirm delete trigger">
          <Icon name="check" size={13} />
        </button>
        <button class="action-btn" onclick={() => (confirmDelete = false)} type="button" aria-label="Cancel">
          <Icon name="x" size={13} />
        </button>
      {/if}
    </div>
  </div>
</div>

<style>
  .trigger-item {
    display: flex;
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--glass-border);
    animation: itemIn var(--transition-normal) both;
    transition: opacity var(--transition-fast);
  }

  .trigger-item.disabled {
    opacity: 0.5;
  }

  .trigger-item:last-child {
    border-bottom: none;
  }

  /* Intentional variant of the global fadeIn: adds a 4px rise. */
  @keyframes itemIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .trigger-main {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    width: 100%;
    min-width: 0;
  }

  .trigger-icon {
    flex-shrink: 0;
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--accent-secondary) 15%, transparent);
    color: var(--accent-secondary, var(--accent-primary));
  }

  .trigger-info {
    flex: 1;
    min-width: 0;
  }

  .trigger-name-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .trigger-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .trigger-name.strikethrough {
    text-decoration: line-through;
    color: var(--text-muted);
  }

  .health-dot {
    flex-shrink: 0;
    width: 6px;
    height: 6px;
    border-radius: 50%;
  }

  .trigger-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 2px;
    font-size: 10px;
    color: var(--text-muted);
  }

  .source-badge,
  .action-badge {
    padding: 1px 5px;
    border-radius: var(--radius-sm);
    background: var(--bg-elevated-2);
    font-weight: 500;
    text-transform: lowercase;
  }

  .error-hint {
    font-size: 10px;
    color: var(--error);
    margin-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* --- Auto-pause (the failure policy stopped this trigger) ---
     Amber and pause-marked so it never reads as the user's own off switch
     (dimmed row, struck-through name) or as the plain health dot: those say
     what a trigger IS, this says the system took it out of service. Wraps
     rather than truncates, since the count and the fix are the whole point. */
  .pause-note {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    margin-top: 3px;
    font-size: 10px;
    line-height: 1.35;
  }

  .paused-chip {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    flex-shrink: 0;
    padding: 1px 5px;
    border-radius: var(--radius-sm);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--warning);
    background: color-mix(in srgb, var(--warning) 14%, transparent);
    border: 1px solid color-mix(in srgb, var(--warning) 38%, transparent);
  }

  .pause-text {
    flex: 1;
    min-width: 0;
    color: var(--text-secondary);
  }

  .pause-error {
    flex-basis: 100%;
    color: var(--error);
  }

  .resume-btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
    min-height: 26px;
    padding: 3px 9px;
    font-size: 10px;
    font-weight: 600;
    border-radius: var(--radius-md);
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    border: 1px solid var(--glass-border);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .resume-btn:active:not(:disabled) {
    background: color-mix(in srgb, var(--bg-elevated-2) 75%, var(--accent-primary));
    border-color: var(--accent-primary);
  }

  .resume-btn:disabled {
    opacity: 0.55;
  }

  .test-result {
    font-size: 10px;
    color: var(--accent-secondary, var(--accent-primary));
    margin-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    animation: itemIn var(--transition-normal);
  }

  .trigger-actions {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
  }

  .thread-badge {
    padding: 1px 6px;
    font-size: 10px;
    font-weight: 500;
    border-radius: var(--radius-sm);
    background: rgba(var(--accent-primary-rgb), 0.1);
    color: var(--accent-primary);
    border: none;
    cursor: pointer;
    white-space: nowrap;
    max-width: 80px;
    overflow: hidden;
    text-overflow: ellipsis;
    transition: all var(--transition-fast);
  }

  .thread-badge:hover {
    background: rgba(var(--accent-primary-rgb), 0.2);
  }

  .toggle {
    position: relative;
    display: inline-flex;
    cursor: pointer;
  }

  .toggle input {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-track {
    width: 28px;
    height: 16px;
    border-radius: 8px;
    background: var(--bg-elevated-2);
    transition: background var(--transition-fast);
    position: relative;
  }

  .toggle-track::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--text-muted);
    transition: all var(--transition-fast);
  }

  .toggle input:checked + .toggle-track {
    background: var(--accent-primary);
  }

  .toggle input:checked + .toggle-track::after {
    transform: translateX(12px);
    background: white;
  }

  .action-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border: none;
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .action-btn:active {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .delete-btn:active {
    color: var(--error);
  }

  .confirm-delete {
    color: var(--error);
    background: color-mix(in srgb, var(--error) 10%, transparent);
  }
</style>
