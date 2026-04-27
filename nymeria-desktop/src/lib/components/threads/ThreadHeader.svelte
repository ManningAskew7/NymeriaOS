<script lang="ts">
  import type { Thread, ThreadConfig } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { outlookStore } from '$lib/stores/outlook.svelte';

  interface Props {
    thread: Thread;
    threadConfig?: ThreadConfig | null;
    onOpenSettings: () => void;
  }

  let { thread, threadConfig, onOpenSettings }: Props = $props();

  const isCallable = $derived(threadConfig?.callable ?? false);

  type OfficeBridge = {
    context?: {
      ui?: {
        openBrowserWindow?: (url: string) => void;
      };
    };
  };

  function openInBrowser() {
    const url = window.location.origin + window.location.pathname;
    const office = (globalThis as typeof globalThis & { Office?: OfficeBridge }).Office;
    // In Outlook, use Office.js to open in the system default browser
    if (office?.context?.ui?.openBrowserWindow) {
      office.context.ui.openBrowserWindow(url);
    } else {
      window.open(url, '_blank');
    }
  }

  function popOut() {
    const url = window.location.origin + window.location.pathname;
    window.open(url, 'nymeria-popout', 'width=900,height=700,resizable=yes,scrollbars=yes');
  }

  function shortModelName(modelId: string): string {
    const parts = modelId.split('/');
    return parts[parts.length - 1];
  }

  const effectiveModel = $derived.by(() => {
    if (threadConfig?.llmConfig?.model) {
      return { name: shortModelName(threadConfig.llmConfig.model), full: threadConfig.llmConfig.model, isOverride: true };
    }
    if (serverSettingsStore.model) {
      return { name: shortModelName(serverSettingsStore.model), full: serverSettingsStore.model, isOverride: false };
    }
    return null;
  });

  const disabledCount = $derived(threadConfig?.disabledTools?.length ?? 0);
  const enabledOptionalCount = $derived(threadConfig?.enabledTools?.length ?? 0);

  const activeToolCount = $derived.by(() => {
    if (!defaultToolsStore.loaded) return null;
    return defaultToolsStore.defaultToolNames.length - disabledCount + enabledOptionalCount;
  });

  const toolsTooltip = $derived.by(() => {
    if (activeToolCount === null) return '';
    const parts = [`${activeToolCount} active`];
    if (disabledCount > 0) parts.push(`${disabledCount} disabled`);
    if (enabledOptionalCount > 0) parts.push(`${enabledOptionalCount} optional enabled`);
    return parts.join(', ');
  });

  const callableCount = $derived(defaultToolsStore.callableThreadCount);

  const triggerCount = $derived(
    triggersStore.triggers.filter(t => t.enabled && t.thread_id === thread.id).length
  );

  const hasInstructions = $derived(!!threadConfig?.instructions);
  const instructionsTooltip = $derived.by(() => {
    if (!threadConfig?.instructions) return '';
    const preview = threadConfig.instructions.substring(0, 80);
    return preview + (threadConfig.instructions.length > 80 ? '...' : '');
  });
</script>

<div class="thread-header">
  <div class="header-info">
    <span class="thread-title">{thread.title}</span>

    <div class="header-badges">
      {#if effectiveModel}
        <span
          class="badge model-badge"
          class:default={!effectiveModel.isOverride}
          class:override={effectiveModel.isOverride}
          title="{effectiveModel.full} ({effectiveModel.isOverride ? 'thread override' : 'default'})"
        >
          {effectiveModel.name}
        </span>
      {/if}
      {#if activeToolCount !== null}
        <span
          class="badge tools-badge"
          class:reduced={disabledCount > 0}
          title={toolsTooltip}
        >
          {activeToolCount} tools
        </span>
      {/if}
      {#if callableCount > 0}
        <span class="badge callables-badge" title="{callableCount} callable thread{callableCount !== 1 ? 's' : ''} available as tools">
          {callableCount} callable{callableCount !== 1 ? 's' : ''}
        </span>
      {/if}
      {#if triggerCount > 0}
        <span class="badge triggers-badge" title="{triggerCount} active trigger{triggerCount !== 1 ? 's' : ''}">
          {triggerCount} trigger{triggerCount !== 1 ? 's' : ''}
        </span>
      {/if}
      {#if hasInstructions}
        <span class="badge instructions-badge" title={instructionsTooltip}>
          instructions
        </span>
      {/if}
      {#if isCallable}
        <span class="badge callable-badge" title="Callable thread">
          <span class="callable-chevron">&lt;</span>
          Callable
        </span>
      {/if}
    </div>
  </div>

  <div class="header-actions">
    {#if outlookStore.isOutlookMode}
      <button
        class="action-btn"
        onclick={popOut}
        title="Pop out to resizable window"
        type="button"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="15 3 21 3 21 9" />
          <line x1="10" y1="14" x2="21" y2="3" />
          <path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5" />
        </svg>
      </button>
      <button
        class="action-btn"
        onclick={openInBrowser}
        title="Open in full browser"
        type="button"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10" />
          <line x1="2" y1="12" x2="22" y2="12" />
          <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
        </svg>
      </button>
    {/if}
    <button
      class="settings-btn"
      class:active={threadConfig?.hasCustomizations ?? false}
      onclick={onOpenSettings}
      title="Thread settings"
      type="button"
    >
      <Icon name="cog" size={16} />
    </button>
  </div>
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

  .model-badge.override {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 30%, transparent);
  }

  .model-badge.default {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
    border: 1px solid color-mix(in srgb, var(--text-muted) 25%, transparent);
  }

  .tools-badge {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 30%, transparent);
  }

  .tools-badge.reduced {
    background: color-mix(in srgb, var(--warning, #f59e0b) 20%, transparent);
    color: var(--warning, #f59e0b);
    border: 1px solid color-mix(in srgb, var(--warning, #f59e0b) 30%, transparent);
  }

  .callables-badge {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 30%, transparent);
  }

  .triggers-badge {
    background: color-mix(in srgb, var(--success, #10b981) 20%, transparent);
    color: var(--success, #10b981);
    border: 1px solid color-mix(in srgb, var(--success, #10b981) 30%, transparent);
  }

  .instructions-badge {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
    border: 1px solid color-mix(in srgb, var(--text-muted) 25%, transparent);
  }

  .callable-badge {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 30%, transparent);
    display: inline-flex;
    align-items: center;
  }

  .callable-chevron {
    font-weight: 700;
    font-size: 11px;
    margin-right: 2px;
    line-height: 1;
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .action-btn {
    padding: 6px;
    color: var(--text-muted);
    border-radius: var(--radius-sm);
    transition: all var(--transition-fast);
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .action-btn:hover {
    background: var(--bg-hover);
    color: var(--accent-primary);
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
    transform: rotate(30deg);
  }

  .settings-btn.active {
    color: var(--accent-primary);
  }
</style>
