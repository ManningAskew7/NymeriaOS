<script lang="ts">
  import type { Hook } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { hooksStore } from '$lib/stores/hooks.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import {
    hookCategory,
    describeHookLogic,
    HOOK_ACTION_META,
    HOOK_EVENT_META,
  } from '$lib/utils/hooks';

  interface Props {
    hook: Hook;
    threadTitle?: string;
    onNavigateToThread?: () => void;
    onEdit?: (hook: Hook) => void;
    animationDelay?: number;
  }

  let { hook, threadTitle, onNavigateToThread, onEdit, animationDelay = 0 }: Props = $props();

  let testResult = $state<string | null>(null);
  let testing = $state(false);
  let confirmDelete = $state(false);
  let toggling = $state(false);
  let actionError = $state<string | null>(null);

  const category = $derived(hookCategory(hook.action));
  const actionMeta = $derived(HOOK_ACTION_META[hook.action]);
  const eventMeta = $derived(HOOK_EVENT_META[hook.event]);
  const summary = $derived(describeHookLogic(hook));

  async function handleToggle() {
    toggling = true;
    actionError = null;
    try {
      await hooksStore.updateHook(hook.id, { enabled: !hook.enabled });
    } catch (e) {
      actionError = humanizeErrorText(e, {
        action: hook.enabled ? 'disable' : 'enable',
        resource: 'the hook',
      });
      setTimeout(() => { actionError = null; }, 6000);
    } finally {
      toggling = false;
    }
  }

  async function handleDelete() {
    actionError = null;
    try {
      await hooksStore.deleteHook(hook.id);
      confirmDelete = false;
    } catch (e) {
      actionError = humanizeErrorText(e, { action: 'delete', resource: 'the hook' });
      setTimeout(() => { actionError = null; }, 6000);
    }
  }

  async function handleTest() {
    testing = true;
    testResult = null;
    try {
      const result = await hooksStore.testHook(hook.id);
      const r = result.rendered ?? '(no preview)';
      testResult = `Preview: ${r.substring(0, 100)}${r.length > 100 ? '…' : ''}`;
      setTimeout(() => { testResult = null; }, 6000);
    } catch (e) {
      testResult = humanizeErrorText(e, { action: 'test', resource: 'the hook' });
      setTimeout(() => { testResult = null; }, 6000);
    } finally {
      testing = false;
    }
  }
</script>

<div
  class="hook-item cat-{category}"
  class:disabled={!hook.enabled}
  style="animation-delay: {animationDelay}ms"
>
  <div class="hook-main">
    <div class="cat-icon">
      <Icon name={actionMeta.icon} size={14} />
    </div>

    <div class="hook-info">
      <div class="hook-name-row">
        <span class="hook-name" class:strikethrough={!hook.enabled}>{hook.name}</span>
      </div>
      <div class="hook-meta">
        <span class="badge event">{eventMeta.label}</span>
        <span class="badge action">{actionMeta.label}</span>
        {#if hook.scope === 'global'}<span class="badge scope">global</span>{/if}
      </div>
      <div class="hook-summary">{summary}</div>
      {#if testResult}
        <div class="test-result">{testResult}</div>
      {/if}
      {#if actionError}
        <div class="item-error">{actionError}</div>
      {/if}
    </div>

    <div class="hook-actions">
      {#if threadTitle && onNavigateToThread}
        <button class="thread-badge" onclick={onNavigateToThread} type="button">{threadTitle}</button>
      {/if}

      <label class="toggle">
        <input
          type="checkbox"
          checked={hook.enabled}
          disabled={toggling}
          onchange={handleToggle}
          aria-label={hook.enabled ? 'Disable hook' : 'Enable hook'}
        />
        <span class="toggle-track"></span>
      </label>

      <button class="action-btn" onclick={handleTest} disabled={testing} type="button" aria-label="Test hook">
        <Icon name="terminal" size={13} />
      </button>

      {#if onEdit}
        <button class="action-btn" onclick={() => onEdit(hook)} type="button" aria-label="Edit hook">
          <Icon name="edit" size={13} />
        </button>
      {/if}

      {#if !confirmDelete}
        <button class="action-btn delete-btn" onclick={() => (confirmDelete = true)} type="button" aria-label="Delete hook">
          <Icon name="x" size={13} />
        </button>
      {:else}
        <button class="action-btn confirm-delete" onclick={handleDelete} type="button" aria-label="Confirm delete hook">
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
  .hook-item {
    --cat-color: var(--text-muted);
    display: flex;
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--glass-border);
    animation: itemIn var(--transition-normal) both;
    transition: opacity var(--transition-fast);
  }

  .hook-item.cat-guardrails { --cat-color: var(--warning); }
  .hook-item.cat-context { --cat-color: var(--accent-primary); }
  .hook-item.cat-reactions { --cat-color: var(--info, var(--accent-secondary, var(--text-secondary))); }

  .hook-item.disabled { opacity: 0.5; }
  .hook-item:last-child { border-bottom: none; }

  @keyframes itemIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .hook-main {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    width: 100%;
    min-width: 0;
  }

  .cat-icon {
    flex-shrink: 0;
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--cat-color) 16%, transparent);
    color: var(--cat-color);
  }

  .hook-info {
    flex: 1;
    min-width: 0;
  }

  .hook-name-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .hook-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .hook-name.strikethrough {
    text-decoration: line-through;
    color: var(--text-muted);
  }

  .hook-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 2px;
    font-size: 10px;
    color: var(--text-muted);
  }

  .badge {
    padding: 1px 5px;
    border-radius: var(--radius-sm);
    background: var(--bg-elevated-2);
    font-weight: 500;
  }

  .badge.action {
    color: var(--cat-color);
    background: color-mix(in srgb, var(--cat-color) 12%, transparent);
  }

  .hook-summary {
    font-size: 10px;
    color: var(--text-muted);
    margin-top: 3px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-family: var(--font-mono);
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

  .item-error {
    font-size: 10px;
    color: var(--error);
    margin-top: 2px;
    white-space: normal;
    animation: itemIn var(--transition-normal);
  }

  .hook-actions {
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

  .thread-badge:hover { background: rgba(var(--accent-primary-rgb), 0.2); }

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

  .toggle input:checked + .toggle-track { background: var(--accent-primary); }

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

  .delete-btn:active { color: var(--error); }

  .confirm-delete {
    color: var(--error);
    background: color-mix(in srgb, var(--error) 10%, transparent);
  }
</style>
