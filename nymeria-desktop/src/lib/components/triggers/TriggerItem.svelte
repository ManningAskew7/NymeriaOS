<script lang="ts">
  import type { Trigger, TriggerSourceInfo } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { triggersStore } from '$lib/stores/triggers.svelte';

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

  const sourceInfo = $derived<TriggerSourceInfo | undefined>(
    triggersStore.sources[trigger.source_type]
  );
  const sourceIcon = $derived(sourceInfo?.icon || 'bolt');

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

  async function handleTest() {
    testing = true;
    testResult = null;
    try {
      const result = await triggersStore.testTrigger(trigger.id);
      testResult = result.conditions_pass
        ? `Preview: ${result.rendered_output.substring(0, 100)}${result.rendered_output.length > 100 ? '...' : ''}`
        : 'Conditions would NOT pass for sample event';
      setTimeout(() => { testResult = null; }, 5000);
    } catch (e) {
      testResult = `Error: ${e instanceof Error ? e.message : 'test failed'}`;
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
        <span class="action-badge">{trigger.action.type.replace('_', ' ')}</span>
        <span class="fire-count">{trigger.fire_count}x</span>
        <span class="last-fired">{formatTimeAgo(trigger.last_fired)}</span>
      </div>
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
          title="Go to thread"
        >
          {threadTitle}
        </button>
      {/if}

      <label class="toggle" title={trigger.enabled ? 'Disable' : 'Enable'}>
        <input
          type="checkbox"
          checked={trigger.enabled}
          disabled={toggling}
          onchange={handleToggle}
        />
        <span class="toggle-track"></span>
      </label>

      <button class="action-btn" onclick={handleTest} disabled={testing} type="button" title="Test">
        <Icon name="terminal" size={13} />
      </button>

      {#if onHistory}
        <button class="action-btn" onclick={() => onHistory(trigger)} type="button" title="History">
          <Icon name="clock" size={13} />
        </button>
      {/if}

      {#if onEdit}
        <button class="action-btn" onclick={() => onEdit(trigger)} type="button" title="Edit">
          <Icon name="edit" size={13} />
        </button>
      {/if}

      {#if !confirmDelete}
        <button class="action-btn delete-btn" onclick={() => (confirmDelete = true)} type="button" title="Delete">
          <Icon name="x" size={13} />
        </button>
      {:else}
        <button class="action-btn confirm-delete" onclick={handleDelete} type="button" title="Confirm delete">
          <Icon name="check" size={13} />
        </button>
        <button class="action-btn" onclick={() => (confirmDelete = false)} type="button" title="Cancel">
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
    animation: fadeIn 0.2s ease-out both;
    transition: opacity var(--transition-fast);
  }

  .trigger-item.disabled {
    opacity: 0.5;
  }

  .trigger-item:last-child {
    border-bottom: none;
  }

  @keyframes fadeIn {
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
    background: rgba(var(--accent-secondary-rgb, 99, 102, 241), 0.15);
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

  .test-result {
    font-size: 10px;
    color: var(--accent-secondary, var(--accent-primary));
    margin-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    animation: fadeIn 0.2s ease-out;
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
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .action-btn:hover {
    background: var(--bg-elevated-2);
    color: var(--text-primary);
  }

  .delete-btn:hover {
    color: var(--error);
  }

  .confirm-delete {
    color: var(--error);
    background: rgba(var(--error-rgb, 239, 68, 68), 0.1);
  }
</style>
