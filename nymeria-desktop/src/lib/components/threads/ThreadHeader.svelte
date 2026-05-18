<script lang="ts">
  import type { Thread, ThreadConfig } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { api } from '$lib/services/api.svelte';
  import { skillsStore } from '$lib/stores/skills.svelte';
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

  function isMcpToolName(name: string): boolean {
    return name.startsWith('mcp__');
  }

  let activeSkillCount = $state<number | null>(null);
  let activeSkillTooltip = $state('');
  let activeKitCount = $state<number | null>(null);
  let activeKitTooltip = $state('');
  let activeSkillRequestId = 0;
  let callableCount = $state<number | null>(null);
  let callableTooltip = $state('');
  let callableRequestId = 0;

  $effect(() => {
    if (!skillsStore.enabledGlobalLoaded && !skillsStore.enabledGlobalLoading) {
      skillsStore.loadGlobal();
    }
  });

  $effect(() => {
    const threadId = thread.id;
    const enabledSkillsKey = (threadConfig?.enabledSkills ?? []).join('\x1f');
    const disabledSkillsKey = (threadConfig?.disabledSkills ?? []).join('\x1f');
    const globalSkillsKey = skillsStore.enabledGlobal.join('\x1f');
    const requestId = ++activeSkillRequestId;
    activeSkillCount = null;
    activeSkillTooltip = '';
    activeKitCount = null;
    activeKitTooltip = '';

    api.getThreadActiveSkills(threadId)
      .then((response) => {
        if (requestId !== activeSkillRequestId) return;
        const skillNames = response.skills.filter((s) => !s.is_skill_kit).map((s) => s.name);
        const kitNames = response.skills.filter((s) => s.is_skill_kit).map((s) => s.name);
        activeSkillCount = skillNames.length;
        activeSkillTooltip = skillNames.length
          ? `${skillNames.length} active skill${skillNames.length !== 1 ? 's' : ''}: ${skillNames.join(', ')}`
          : 'No active skills';
        activeKitCount = kitNames.length;
        activeKitTooltip = kitNames.length
          ? `${kitNames.length} active kit${kitNames.length !== 1 ? 's' : ''}: ${kitNames.join(', ')}`
          : 'No active kits';
      })
      .catch((err) => {
        if (requestId !== activeSkillRequestId) return;
        console.warn('[ThreadHeader] Failed to load active skills:', err);
        activeSkillCount = null;
        activeSkillTooltip = '';
        activeKitCount = null;
        activeKitTooltip = '';
      });

    void enabledSkillsKey;
    void disabledSkillsKey;
    void globalSkillsKey;
  });

  $effect(() => {
    const threadId = thread.id;
    const disabledToolsKey = (threadConfig?.disabledTools ?? []).join('\x1f');
    const callableTeamKey = `${threadConfig?.callableTeamId ?? ''}\x1f${threadConfig?.callableTeamName ?? ''}`;
    const callableStateKey = `${threadConfig?.callable ?? false}\x1f${threadConfig?.callableName ?? ''}`;
    const callableThreadsKey = threadsStore.threads
      .map((item) => `${item.id}:${item.callable ?? false}:${item.platform ?? ''}`)
      .join('\x1f');
    const threadTeamsKey = threadsStore.threadTeams
      .map((team) => `${team.id}:${team.name}:${team.threadIds.join(',')}`)
      .join('\x1f');
    const requestId = ++callableRequestId;
    callableCount = null;
    callableTooltip = '';

    api.getThreadCallableTools(threadId)
      .then((response) => {
        if (requestId !== callableRequestId) return;
        const names = response.callable_threads.map((item) => item.name);
        callableCount = response.callable_thread_count;
        callableTooltip = names.length
          ? `${names.length} callable thread${names.length !== 1 ? 's' : ''}: ${names.join(', ')}`
          : 'No callable threads available from this thread';
      })
      .catch((err) => {
        if (requestId !== callableRequestId) return;
        console.warn('[ThreadHeader] Failed to load callable tools:', err);
        callableCount = null;
        callableTooltip = '';
      });

    void disabledToolsKey;
    void callableTeamKey;
    void callableStateKey;
    void callableThreadsKey;
    void threadTeamsKey;
  });

  const disabledNonMcpCount = $derived(
    (threadConfig?.disabledTools ?? []).filter((name) => !isMcpToolName(name)).length
  );
  const enabledOptionalNonMcpCount = $derived(
    (threadConfig?.enabledTools ?? []).filter((name) => !isMcpToolName(name)).length
  );
  const disabledMcpCount = $derived(
    (threadConfig?.disabledTools ?? []).filter(isMcpToolName).length
  );
  const enabledOptionalMcpCount = $derived(
    (threadConfig?.enabledTools ?? []).filter(isMcpToolName).length
  );

  const activeToolCount = $derived.by(() => {
    if (!defaultToolsStore.loaded) return null;
    const defaultNonMcpCount = defaultToolsStore.defaultToolNames.filter((name) => !isMcpToolName(name)).length;
    return defaultNonMcpCount - disabledNonMcpCount + enabledOptionalNonMcpCount;
  });

  const activeMcpToolCount = $derived.by(() => {
    if (!defaultToolsStore.loaded) return null;
    const defaultMcpCount = defaultToolsStore.defaultToolNames.filter(isMcpToolName).length;
    return defaultMcpCount - disabledMcpCount + enabledOptionalMcpCount;
  });

  const toolsTooltip = $derived.by(() => {
    if (activeToolCount === null) return '';
    const parts = [`${activeToolCount} active`];
    if (disabledNonMcpCount > 0) parts.push(`${disabledNonMcpCount} disabled`);
    if (enabledOptionalNonMcpCount > 0) parts.push(`${enabledOptionalNonMcpCount} optional enabled`);
    return parts.join(', ');
  });

  const mcpTooltip = $derived.by(() => {
    if (activeMcpToolCount === null) return '';
    const parts = [`${activeMcpToolCount} MCP active`];
    if (disabledMcpCount > 0) parts.push(`${disabledMcpCount} disabled`);
    if (enabledOptionalMcpCount > 0) parts.push(`${enabledOptionalMcpCount} optional enabled`);
    return parts.join(', ');
  });

  const triggerCount = $derived(
    triggersStore.triggers.filter(t => t.enabled && t.thread_id === thread.id).length
  );

  const hasInstructions = $derived(!!threadConfig?.instructions);
  const instructionsTooltip = $derived.by(() => {
    if (!threadConfig?.instructions) return '';
    const preview = threadConfig.instructions.substring(0, 80);
    return preview + (threadConfig.instructions.length > 80 ? '...' : '');
  });

  type MetaPart = {
    id: string;
    text: string;
    tooltip?: string;
    variant?: 'default' | 'reduced' | 'accent';
  };

  const metaParts = $derived.by<MetaPart[]>(() => {
    const parts: MetaPart[] = [];

    if (effectiveModel) {
      parts.push({
        id: 'model',
        text: effectiveModel.name,
        tooltip: `${effectiveModel.full}${effectiveModel.isOverride ? ' (thread override)' : ''}`,
        variant: effectiveModel.isOverride ? 'accent' : 'default',
      });
    }
    if (activeToolCount !== null) {
      parts.push({
        id: 'tools',
        text: `${activeToolCount} tools`,
        tooltip: toolsTooltip,
        variant: disabledNonMcpCount > 0 ? 'reduced' : 'default',
      });
    }
    if (activeMcpToolCount !== null) {
      parts.push({
        id: 'mcp',
        text: `${activeMcpToolCount} MCP`,
        tooltip: mcpTooltip,
        variant: disabledMcpCount > 0 ? 'reduced' : 'default',
      });
    }
    if (callableCount !== null && callableCount > 0) {
      parts.push({
        id: 'callables',
        text: `${callableCount} callable`,
        tooltip: callableTooltip,
        variant: 'default',
      });
    }
    if (activeSkillCount !== null && activeSkillCount > 0) {
      parts.push({
        id: 'skills',
        text: `${activeSkillCount} skill${activeSkillCount !== 1 ? 's' : ''}`,
        tooltip: activeSkillTooltip,
        variant: 'default',
      });
    }
    if (activeKitCount !== null && activeKitCount > 0) {
      parts.push({
        id: 'kits',
        text: `${activeKitCount} kit${activeKitCount !== 1 ? 's' : ''}`,
        tooltip: activeKitTooltip,
        variant: 'default',
      });
    }
    if (triggerCount > 0) {
      parts.push({
        id: 'triggers',
        text: `${triggerCount} trigger${triggerCount !== 1 ? 's' : ''}`,
        tooltip: `${triggerCount} active trigger${triggerCount !== 1 ? 's' : ''}`,
        variant: 'default',
      });
    }
    if (hasInstructions) {
      parts.push({
        id: 'instructions',
        text: 'instructions',
        tooltip: instructionsTooltip,
        variant: 'default',
      });
    }
    if (isCallable) {
      parts.push({
        id: 'callable',
        text: 'callable',
        tooltip: 'This thread can be called by other threads',
        variant: 'accent',
      });
    }
    return parts;
  });
</script>

<header class="thread-header">
  <h2 class="title" title={thread.title}>{thread.title}</h2>

  {#if metaParts.length > 0}
    <div class="meta">
      {#each metaParts as part (part.id)}
        <span class="meta-part meta-part--{part.id}" class:reduced={part.variant === 'reduced'} class:accent={part.variant === 'accent'} title={part.tooltip}>{part.text}</span>
      {/each}
    </div>
  {/if}

  <div class="actions">
    {#if outlookStore.isOutlookMode}
      <button
        class="icon-btn"
        onclick={popOut}
        title="Pop out to resizable window"
        type="button"
        aria-label="Pop out"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="15 3 21 3 21 9" />
          <line x1="10" y1="14" x2="21" y2="3" />
          <path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5" />
        </svg>
      </button>
      <button
        class="icon-btn"
        onclick={openInBrowser}
        title="Open in full browser"
        type="button"
        aria-label="Open in browser"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10" />
          <line x1="2" y1="12" x2="22" y2="12" />
          <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
        </svg>
      </button>
    {/if}
    <button
      class="icon-btn cog"
      class:active={threadConfig?.hasCustomizations ?? false}
      onclick={onOpenSettings}
      title="Thread settings"
      type="button"
      aria-label="Thread settings"
    >
      <Icon name="cog" size={16} />
    </button>
  </div>
</header>

<style>
  .thread-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    padding: 4px 6px 4px var(--spacing-md);
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-default);
    flex-shrink: 0;
    min-height: 34px;
  }

  .title {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: -0.005em;
    line-height: 1.25;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 0 1 auto;
    min-width: 0;
    max-width: 45%;
  }

  .meta {
    display: flex;
    align-items: center;
    gap: 14px;
    font-size: 12px;
    color: var(--text-secondary);
    line-height: 1.25;
    letter-spacing: 0.005em;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    flex: 1 1 auto;
  }

  .meta-part {
    position: relative;
    flex-shrink: 0;
    cursor: default;
    font-variant-numeric: tabular-nums;
    padding-left: 11px;
    --dot-color: var(--text-muted);
  }

  .meta-part::before {
    content: '';
    position: absolute;
    left: 0;
    top: 50%;
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--dot-color);
    transform: translateY(-50%);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--dot-color) 35%, transparent);
    transition: box-shadow var(--transition-fast);
  }

  .meta-part:hover::before {
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--dot-color) 18%, transparent);
  }

  .meta-part--model { --dot-color: var(--accent-primary); }
  .meta-part--tools { --dot-color: var(--success); }
  .meta-part--mcp { --dot-color: var(--info); }
  .meta-part--callables { --dot-color: var(--accent-secondary); }
  .meta-part--skills { --dot-color: var(--accent-primary); }
  .meta-part--kits { --dot-color: color-mix(in srgb, var(--accent-primary) 55%, var(--text-muted)); }
  .meta-part--triggers { --dot-color: var(--warning); }
  .meta-part--instructions { --dot-color: var(--text-muted); }
  .meta-part--callable { --dot-color: var(--accent-primary); }

  .meta-part.reduced {
    color: var(--warning);
    --dot-color: var(--warning);
  }

  .meta-part.accent {
    color: var(--accent-primary);
    font-weight: 500;
  }

  .actions {
    display: flex;
    align-items: center;
    gap: 2px;
    flex: 0 0 auto;
    margin-left: auto;
  }

  .icon-btn {
    display: grid;
    place-items: center;
    width: 26px;
    height: 26px;
    padding: 0;
    background: transparent;
    border: 0;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    cursor: pointer;
    transition:
      color var(--transition-fast),
      background var(--transition-fast);
  }

  .icon-btn :global(svg) {
    display: block;
  }

  .icon-btn:hover {
    color: var(--accent-primary);
    background: var(--bg-hover);
  }

  .icon-btn:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 1px;
  }

  .icon-btn.cog :global(svg) {
    transition: transform var(--transition-fast);
  }

  .icon-btn.cog:hover :global(svg) {
    transform: rotate(45deg);
  }

  .icon-btn.cog.active {
    color: var(--accent-primary);
  }
</style>
