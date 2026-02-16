<script lang="ts">
  import type { Thread, ThreadConfig } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { triggersStore } from '$lib/stores/triggers.svelte';

  interface Props {
    thread: Thread;
    threadConfig?: ThreadConfig | null;
    onOpenSettings: () => void;
  }

  let { thread, threadConfig, onOpenSettings }: Props = $props();

  const hasConfig = $derived(threadConfig?.hasCustomizations ?? false);

  const modelLabel = $derived(() => {
    if (threadConfig?.llmConfig?.model) {
      // Show short model name
      const m = threadConfig.llmConfig.model;
      const parts = m.split('/');
      return parts[parts.length - 1];
    }
    return null;
  });

  const disabledCount = $derived(threadConfig?.disabledTools?.length ?? 0);
  const triggerCount = $derived(
    triggersStore.triggers.filter(t => t.enabled && t.thread_id === thread.id).length
  );
</script>

<div class="thread-header">
  <div class="header-info">
    <span class="thread-title">{thread.title}</span>

    <div class="header-badges">
      {#if modelLabel()}
        <span class="badge model-badge" title="Custom model for this thread">
          {modelLabel()}
        </span>
      {/if}
      {#if disabledCount > 0}
        <span class="badge tools-badge" title="{disabledCount} tool{disabledCount !== 1 ? 's' : ''} disabled">
          -{disabledCount} tools
        </span>
      {/if}
      {#if triggerCount > 0}
        <span class="badge triggers-badge" title="{triggerCount} active trigger{triggerCount !== 1 ? 's' : ''}">
          {triggerCount} trigger{triggerCount !== 1 ? 's' : ''}
        </span>
      {/if}
      {#if hasConfig && !modelLabel() && disabledCount === 0}
        <span class="badge config-badge" title="Thread has custom instructions">
          configured
        </span>
      {/if}
    </div>
  </div>

  <button
    class="settings-btn"
    class:active={hasConfig}
    onclick={onOpenSettings}
    title="Thread settings"
    type="button"
  >
    <Icon name="cog" size={16} />
  </button>
</div>

<style>
  .thread-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-default);
    background: var(--glass-bg);
    backdrop-filter: var(--glass-blur);
    min-height: 40px;
    flex-shrink: 0;
  }

  .header-info {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    min-width: 0;
    flex: 1;
  }

  .thread-title {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .header-badges {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
  }

  .badge {
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    font-size: 10px;
    font-weight: 500;
    border-radius: var(--radius-full);
    white-space: nowrap;
  }

  .model-badge {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 30%, transparent);
  }

  .tools-badge {
    background: color-mix(in srgb, var(--warning, #f59e0b) 20%, transparent);
    color: var(--warning, #f59e0b);
    border: 1px solid color-mix(in srgb, var(--warning, #f59e0b) 30%, transparent);
  }

  .triggers-badge {
    background: color-mix(in srgb, var(--success, #10b981) 20%, transparent);
    color: var(--success, #10b981);
    border: 1px solid color-mix(in srgb, var(--success, #10b981) 30%, transparent);
  }

  .config-badge {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
    border: 1px solid color-mix(in srgb, var(--text-muted) 25%, transparent);
  }

  .settings-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    transition: all var(--transition-fast);
    flex-shrink: 0;
  }

  .settings-btn:hover {
    color: var(--accent-primary);
    background: var(--bg-hover);
  }

  .settings-btn.active {
    color: var(--accent-primary);
  }
</style>
