<script lang="ts">
  import type { Thread, ThreadConfig } from '$lib/types';
  import { tick } from 'svelte';
  import { slide } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import { Icon } from '$lib/components/common';
  import { portal } from '$lib/actions/portal';
  import { tooltipWhenClipped } from '$lib/actions/tooltip';
  import CheckpointViewer from '$lib/components/common/CheckpointViewer.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { api } from '$lib/services/api.svelte';
  import { skillsStore } from '$lib/stores/skills.svelte';
  import { outlookStore } from '$lib/stores/outlook.svelte';
  import { healthStore } from '$lib/stores/health.svelte';
  import { uiStore } from '$lib/stores/ui.svelte';
  import { computeEffectiveToolCounts, liveTemporaryToolNames } from '$lib/utils/toolCounts';

  interface Props {
    thread: Thread;
    threadConfig?: ThreadConfig | null;
    /* Opens the per-thread settings panel; `tab` deep-links a specific tab
       (any id ThreadSettingsPanel.normalizeTab accepts). Omitted = default. */
    onOpenSettings: (tab?: string) => void;
  }

  let { thread, threadConfig, onOpenSettings }: Props = $props();

  let showMeta = $state(true);
  // Developer-mode raw checkpoint viewer (null = closed). The button that sets
  // this is gated behind configStore.developerMode.
  let checkpointThreadId = $state<string | null>(null);

  // Appearance setting (uiStore): collapse the inline metadata row into a single
  // summary chip on the right, with the full breakdown in a popover.
  const summaryMode = $derived(uiStore.threadHeaderSummary);

  // Summary-chip popover state. Fixed-positioned and portaled to <body> (same
  // approach as the task-card overflow menu) so the panel chrome can't clip it.
  let summaryOpen = $state(false);
  let chipEl = $state<HTMLButtonElement>();
  let summaryEl = $state<HTMLDivElement>();
  let summaryStyle = $state('');

  const healthDotClass = $derived(
    healthStore.checking && !healthStore.connected
      ? 'checking'
      : healthStore.connected
        ? 'connected'
        : 'disconnected'
  );

  const healthTooltip = $derived(
    healthStore.checking && !healthStore.connected
      ? 'Checking API…'
      : healthStore.connected
        ? 'API connected'
        : 'API disconnected'
  );

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

  let activeSkillCount = $state<number | null>(null);
  let activeSkillTooltip = $state('');
  let activeKitCount = $state<number | null>(null);
  let activeKitTooltip = $state('');
  let activeSkillRequestId = 0;
  let callableCount = $state<number | null>(null);
  let callableTooltip = $state('');
  let callableRequestId = 0;
  // Non-reactive markers for which thread the current counts belong to, so
  // the offline guards below can keep last-known counts through a connection
  // blip without ever showing another thread's numbers.
  let skillCountsThreadId: string | null = null;
  let callableCountsThreadId: string | null = null;

  $effect(() => {
    if (!skillsStore.enabledGlobalLoaded && !skillsStore.enabledGlobalLoading) {
      skillsStore.loadGlobal();
    }
  });

  $effect(() => {
    const threadId = thread.id;
    // Tracked so a backend reconnect refires a fetch that failed while the
    // link was down (the count would otherwise stay null until the thread
    // config next changed). Health polling runs from the always-mounted
    // right panel, so `connected` reliably flips true once the backend is
    // reachable.
    const connected = healthStore.connected;
    const enabledSkillsKey = (threadConfig?.enabledSkills ?? []).join('\x1f');
    const disabledSkillsKey = (threadConfig?.disabledSkills ?? []).join('\x1f');
    const globalSkillsKey = skillsStore.enabledGlobal.join('\x1f');
    const requestId = ++activeSkillRequestId;

    if (!connected) {
      // Offline (or health not yet confirmed at startup): skip the doomed
      // request and keep the last-known counts on screen — a health-poll
      // blip must not blank the header. Clear only if the open thread
      // changed, where stale foreign counts would be worse than none. The
      // bumped requestId already drops any in-flight response.
      if (skillCountsThreadId !== threadId) {
        skillCountsThreadId = threadId;
        activeSkillCount = null;
        activeSkillTooltip = '';
        activeKitCount = null;
        activeKitTooltip = '';
      }
      return;
    }

    skillCountsThreadId = threadId;
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
    // Tracked for the same reconnect-refire reason as the skills effect above.
    const connected = healthStore.connected;
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

    if (!connected) {
      // Same offline posture as the skills effect: keep last-known counts
      // through a blip, clear only on a thread change, never fetch.
      if (callableCountsThreadId !== threadId) {
        callableCountsThreadId = threadId;
        callableCount = null;
        callableTooltip = '';
      }
      return;
    }

    callableCountsThreadId = threadId;
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

  const effectiveToolCounts = $derived.by(() => {
    if (!defaultToolsStore.loaded) return null;
    return computeEffectiveToolCounts({
      defaultToolNames: defaultToolsStore.defaultToolNames,
      enabledTools: threadConfig?.enabledTools ?? [],
      disabledTools: threadConfig?.disabledTools ?? [],
      temporaryTools: liveTemporaryToolNames(threadConfig?.temporaryTools),
    });
  });

  const disabledNonMcpCount = $derived(effectiveToolCounts?.disabledNonMcpCount ?? 0);
  const enabledOptionalNonMcpCount = $derived(effectiveToolCounts?.enabledExtraNonMcpCount ?? 0);
  const disabledMcpCount = $derived(effectiveToolCounts?.disabledMcpCount ?? 0);
  const enabledOptionalMcpCount = $derived(effectiveToolCounts?.enabledExtraMcpCount ?? 0);
  const activeToolCount = $derived(effectiveToolCounts?.activeNonMcpCount ?? null);
  const activeMcpToolCount = $derived(effectiveToolCounts?.activeMcpCount ?? null);

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
    return preview + (threadConfig.instructions.length > 80 ? '…' : '');
  });

  type MetaPart = {
    id: string;
    /* Numeric chips render `count` (semibold, primary) beside `label` (muted).
       Word-only chips (instructions, callable) omit `count`. When the row is
       too crowded for full labels the chip collapses to the count alone, or —
       for word-only chips — to `compactIcon`. The model chip omits both, so
       it keeps its full label in compact mode. */
    count?: number;
    label: string;
    compactIcon?: 'fileText' | 'users';
    tooltip?: string;
    variant?: 'default' | 'reduced' | 'accent';
    /* Per-thread settings tab this stat deep-links to (ThreadSettingsPanel
       normalizeTab id). Set = the chip renders as a button; unset (e.g.
       triggers, which have no per-thread tab) = a plain informational chip. */
    settingsTab?: string;
  };

  const metaParts = $derived.by<MetaPart[]>(() => {
    const parts: MetaPart[] = [];

    if (effectiveModel) {
      parts.push({
        id: 'model',
        label: effectiveModel.name,
        tooltip: `${effectiveModel.full}${effectiveModel.isOverride ? ' (thread override)' : ''}`,
        variant: effectiveModel.isOverride ? 'accent' : 'default',
        settingsTab: 'model',
      });
    }
    if (activeToolCount !== null) {
      parts.push({
        id: 'tools',
        count: activeToolCount,
        label: 'tools',
        tooltip: toolsTooltip,
        variant: disabledNonMcpCount > 0 ? 'reduced' : 'default',
        settingsTab: 'tools',
      });
    }
    if (activeMcpToolCount !== null) {
      parts.push({
        id: 'mcp',
        count: activeMcpToolCount,
        label: 'MCP',
        tooltip: mcpTooltip,
        variant: disabledMcpCount > 0 ? 'reduced' : 'default',
        settingsTab: 'mcp',
      });
    }
    if (callableCount !== null && callableCount > 0) {
      parts.push({
        id: 'callables',
        count: callableCount,
        label: 'callable',
        tooltip: callableTooltip,
        variant: 'default',
        settingsTab: 'agent',
      });
    }
    if (activeSkillCount !== null && activeSkillCount > 0) {
      parts.push({
        id: 'skills',
        count: activeSkillCount,
        label: `skill${activeSkillCount !== 1 ? 's' : ''}`,
        tooltip: activeSkillTooltip,
        variant: 'default',
        settingsTab: 'skills',
      });
    }
    if (activeKitCount !== null && activeKitCount > 0) {
      parts.push({
        id: 'kits',
        count: activeKitCount,
        label: `kit${activeKitCount !== 1 ? 's' : ''}`,
        tooltip: activeKitTooltip,
        variant: 'default',
        settingsTab: 'skills',
      });
    }
    if (triggerCount > 0) {
      // No settingsTab: per-thread triggers are managed from the Dashboard,
      // not a thread-settings tab, so this chip stays informational.
      parts.push({
        id: 'triggers',
        count: triggerCount,
        label: `trigger${triggerCount !== 1 ? 's' : ''}`,
        tooltip: `${triggerCount} active trigger${triggerCount !== 1 ? 's' : ''}`,
        variant: 'default',
      });
    }
    if (hasInstructions) {
      parts.push({
        id: 'instructions',
        label: 'instructions',
        compactIcon: 'fileText',
        tooltip: instructionsTooltip,
        variant: 'default',
        settingsTab: 'behavior',
      });
    }
    if (isCallable) {
      parts.push({
        id: 'callable',
        label: 'callable',
        compactIcon: 'users',
        tooltip: 'This thread can be called by other threads',
        variant: 'accent',
        settingsTab: 'agent',
      });
    }
    return parts;
  });

  // Rows shown in the summary-mode popover: every metric except the model (kept
  // inline beside the title) and tools (the chip's headline number). Same
  // presence rules as metaParts, so the popover stays in sync with what the
  // inline row would have shown.
  type BreakdownRow = {
    id: string;
    label: string;
    count: number | string;
    tooltip?: string;
    variant?: 'reduced' | 'accent' | 'default';
    /* Same deep-link semantics as MetaPart.settingsTab. */
    settingsTab?: string;
  };

  const breakdownRows = $derived.by<BreakdownRow[]>(() => {
    const rows: BreakdownRow[] = [];
    if (activeMcpToolCount !== null) {
      rows.push({ id: 'mcp', label: 'MCP', count: activeMcpToolCount, tooltip: mcpTooltip, variant: disabledMcpCount > 0 ? 'reduced' : 'default', settingsTab: 'mcp' });
    }
    if (callableCount !== null && callableCount > 0) {
      rows.push({ id: 'callables', label: 'Callable', count: callableCount, tooltip: callableTooltip, settingsTab: 'agent' });
    }
    if (activeSkillCount !== null && activeSkillCount > 0) {
      rows.push({ id: 'skills', label: 'Skills', count: activeSkillCount, tooltip: activeSkillTooltip, settingsTab: 'skills' });
    }
    if (activeKitCount !== null && activeKitCount > 0) {
      rows.push({ id: 'kits', label: 'Kits', count: activeKitCount, tooltip: activeKitTooltip, settingsTab: 'skills' });
    }
    if (triggerCount > 0) {
      rows.push({ id: 'triggers', label: 'Triggers', count: triggerCount, tooltip: `${triggerCount} active trigger${triggerCount !== 1 ? 's' : ''}` });
    }
    if (hasInstructions) {
      rows.push({ id: 'instructions', label: 'Instructions', count: '✓', tooltip: instructionsTooltip, settingsTab: 'behavior' });
    }
    if (isCallable) {
      rows.push({ id: 'callable', label: 'Callable thread', count: '✓', tooltip: 'This thread can be called by other threads', variant: 'accent', settingsTab: 'agent' });
    }
    return rows;
  });

  // Chip headline mirrors the image: the active tool count. Falls back to a
  // generic label if tools haven't resolved yet but other metrics have.
  const summaryLabel = $derived(activeToolCount !== null ? `${activeToolCount} tools` : 'Details');
  const showSummaryChip = $derived(activeToolCount !== null || breakdownRows.length > 0);

  // The metrics row keeps as many full-label chips as the width allows and
  // collapses the rest, right to left, to their compact forms (count-only or
  // icon-only chips, see MetaPart) so it always shows the most information
  // that fits. The title yields space first
  // (it shrinks before the meta), so the meta keeps expanding until even a
  // fully-truncated title can't free more room. This needs real widths, so we
  // measure each item in both forms.
  let headerEl = $state<HTMLElement>();
  let titleEl = $state<HTMLHeadingElement>();
  let metaEl = $state<HTMLDivElement>();
  let actionsEl = $state<HTMLDivElement>();
  // How many leading items render with full labels; the rest render compact.
  // Defaults to "all full" until the first measurement runs.
  let fullCount = $state(Number.MAX_SAFE_INTEGER);
  // While set, forces every item to one form so we can read its width.
  let measuring = $state<'full' | 'compact' | null>(null);
  // Cached per-item widths and the inter-item gap, so resize re-checks reuse
  // them instead of re-rendering (which would flicker during a drag).
  let fullWidths: number[] = [];
  let compactWidths: number[] = [];
  let metaGap = 8;

  // The horizontal space the meta can occupy if the title shrinks all the way to
  // its min-width. `metaLeft - titleWidth` is constant no matter how truncated
  // the title currently is, so this depends only on the header width, never on
  // the current mode -- which is what stops it from latching.
  function availableMetaWidth(): number {
    if (!metaEl || !titleEl || !actionsEl) return 0;
    const header = headerEl ?? metaEl.parentElement;
    const headerGap = header ? parseFloat(getComputedStyle(header).columnGap) || 0 : 0;
    const titleMin = parseFloat(getComputedStyle(titleEl).minWidth) || 0;
    const metaLeft = metaEl.getBoundingClientRect().left;
    const titleWidth = titleEl.getBoundingClientRect().width;
    const actionsLeft = actionsEl.getBoundingClientRect().left;
    const titleHeadroom = Math.max(0, titleWidth - titleMin);
    return actionsLeft - headerGap - metaLeft + titleHeadroom;
  }

  function recomputeFullCount() {
    const n = fullWidths.length;
    if (!n || compactWidths.length !== n) return;
    const available = availableMetaWidth() - 4; // hair of breathing room
    const gaps = metaGap * Math.max(0, n - 1);
    // Largest number of leading full-label items whose total still fits; the
    // remaining items stay compact. Monotonic in k, so a linear scan suffices.
    let k = n;
    for (; k >= 0; k -= 1) {
      let total = gaps;
      for (let i = 0; i < n; i += 1) total += i < k ? fullWidths[i] : compactWidths[i];
      if (total <= available) break;
    }
    fullCount = Math.max(0, k);
  }

  // Re-measure both forms whenever the metric set changes, then choose how many
  // to expand. The two passes force every item to one form so we can read both
  // its widths; this only runs when the metrics themselves change.
  $effect(() => {
    const contentKey = metaParts.map((p) => `${p.id}:${p.count ?? ''}:${p.label}`).join('|');
    void contentKey;
    if (!metaEl) return;
    let cancelled = false;
    void (async () => {
      measuring = 'full';
      await tick();
      if (cancelled || !metaEl) return;
      metaGap = parseFloat(getComputedStyle(metaEl).columnGap) || metaGap;
      fullWidths = (Array.from(metaEl.children) as HTMLElement[]).map((el) => el.offsetWidth);
      measuring = 'compact';
      await tick();
      if (cancelled || !metaEl) return;
      compactWidths = (Array.from(metaEl.children) as HTMLElement[]).map((el) => el.offsetWidth);
      measuring = null;
      recomputeFullCount();
    })();
    return () => {
      cancelled = true;
    };
  });

  // Re-decide on width changes (window resize, panel toggles) from cached widths
  // alone, so resizing never forces a re-measure or flicker.
  $effect(() => {
    if (!headerEl) return;
    const observer = new ResizeObserver(() => recomputeFullCount());
    observer.observe(headerEl);
    return () => observer.disconnect();
  });

  // --- Summary-chip popover (summary mode only) ---

  function openSummary() {
    if (!chipEl) return;
    const r = chipEl.getBoundingClientRect();
    // Anchor by the right edge so the popover lines up under the chip and grows
    // leftward with its content. The header is pinned to the top of the window,
    // so it always has room to drop downward (no up-flip needed).
    const right = Math.max(8, window.innerWidth - r.right);
    summaryStyle = `top:${r.bottom + 6}px; right:${right}px;`;
    summaryOpen = true;
  }

  function closeSummary() {
    if (summaryEl?.contains(document.activeElement)) chipEl?.focus();
    summaryOpen = false;
  }

  function toggleSummary(e: MouseEvent) {
    e.stopPropagation();
    if (summaryOpen) closeSummary();
    else openSummary();
  }

  function handleSummaryOutsideClick(e: MouseEvent) {
    const t = e.target as HTMLElement;
    if (!t.closest('.summary-popover') && !t.closest('.summary-chip')) closeSummary();
  }

  function handleSummaryKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      closeSummary();
    }
  }

  $effect(() => {
    if (!summaryOpen) return;
    document.addEventListener('click', handleSummaryOutsideClick, true);
    document.addEventListener('keydown', handleSummaryKeydown, true);
    window.addEventListener('scroll', closeSummary, true);
    window.addEventListener('resize', closeSummary, true);
    return () => {
      document.removeEventListener('click', handleSummaryOutsideClick, true);
      document.removeEventListener('keydown', handleSummaryKeydown, true);
      window.removeEventListener('scroll', closeSummary, true);
      window.removeEventListener('resize', closeSummary, true);
    };
  });

  // Close the popover if summary mode is switched off while it's open.
  $effect(() => {
    if (!summaryMode && summaryOpen) summaryOpen = false;
  });
</script>

{#snippet chipContent(part: MetaPart, showFull: boolean)}
  {#if showFull}
    {#if part.count !== undefined}<span class="count">{part.count}</span>{/if}
    <span class="label">{part.label}</span>
  {:else if part.count !== undefined}
    <span class="count">{part.count}</span>
  {:else if part.compactIcon}
    <Icon name={part.compactIcon} size={11} />
  {:else}
    <span class="label">{part.label}</span>
  {/if}
{/snippet}

<header class="thread-header" bind:this={headerEl}>
  <h2 class="title" use:tooltipWhenClipped={thread.title} bind:this={titleEl}>{thread.title}</h2>

  {#if summaryMode}
    {#if effectiveModel}
      <button
        class="header-model"
        class:accent={effectiveModel.isOverride}
        type="button"
        data-tooltip={`${effectiveModel.full}${effectiveModel.isOverride ? ' (thread override)' : ''}`}
        aria-label="Configure thread model"
        onclick={() => onOpenSettings('model')}
      >{effectiveModel.name}</button>
    {/if}
  {:else if metaParts.length > 0}
    <button
      class="meta-toggle {healthDotClass}"
      class:open={showMeta}
      type="button"
      onclick={() => (showMeta = !showMeta)}
      data-tooltip={`${healthTooltip} — click to ${showMeta ? 'hide' : 'show'} details`}
      aria-label="Toggle thread details"
      aria-expanded={showMeta}
    >
      <Icon name="chevronRight" size={14} />
    </button>

    {#if showMeta}
      <div class="meta" bind:this={metaEl} transition:slide={{ axis: 'x', duration: 240, easing: cubicOut }}>
        {#each metaParts as part, i (part.id)}
          {@const showFull = measuring === 'full' || (measuring !== 'compact' && i < fullCount)}
          {#if part.settingsTab}
            <button
              class="meta-part meta-part--{part.id}"
              class:reduced={part.variant === 'reduced'}
              class:accent={part.variant === 'accent'}
              type="button"
              data-tooltip={part.tooltip}
              aria-label={`Configure ${part.label}`}
              onclick={() => onOpenSettings(part.settingsTab)}
            >
              {@render chipContent(part, showFull)}
            </button>
          {:else}
            <span class="meta-part meta-part--{part.id}" class:reduced={part.variant === 'reduced'} class:accent={part.variant === 'accent'} data-tooltip={part.tooltip}>
              {@render chipContent(part, showFull)}
            </span>
          {/if}
        {/each}
      </div>
    {/if}
  {/if}

  <div class="actions" bind:this={actionsEl}>
    {#if outlookStore.isOutlookMode}
      <button
        class="icon-btn"
        onclick={popOut}
        data-tooltip="Pop out to resizable window"
        type="button"
        aria-label="Pop out"
      >
        <Icon name="externalLink" size={14} />
      </button>
      <button
        class="icon-btn"
        onclick={openInBrowser}
        data-tooltip="Open in full browser"
        type="button"
        aria-label="Open in browser"
      >
        <Icon name="globe" size={14} />
      </button>
    {/if}
    {#if configStore.developerMode}
      <button
        class="icon-btn"
        onclick={() => (checkpointThreadId = thread.id)}
        data-tooltip="View raw checkpoint (developer)"
        type="button"
        aria-label="View raw checkpoint"
      >
        <Icon name="terminal" size={16} />
      </button>
    {/if}
    {#if summaryMode && showSummaryChip}
      <button
        class="summary-chip"
        class:open={summaryOpen}
        bind:this={chipEl}
        onclick={toggleSummary}
        type="button"
        data-tooltip="Thread details"
        aria-haspopup="true"
        aria-expanded={summaryOpen}
      >
        <span>{summaryLabel}</span>
        <Icon name="chevronDown" size={14} />
      </button>
    {/if}
    <button
      class="icon-btn cog"
      class:active={threadConfig?.hasCustomizations ?? false}
      onclick={() => onOpenSettings()}
      data-tooltip={threadConfig?.hasCustomizations ? 'Thread settings (customized)' : 'Thread settings'}
      type="button"
      aria-label={threadConfig?.hasCustomizations ? 'Thread settings, customized' : 'Thread settings'}
    >
      <Icon name="cog" size={16} />
    </button>
  </div>
</header>

{#if summaryMode && summaryOpen}
  <div
    class="summary-popover"
    bind:this={summaryEl}
    use:portal
    style={summaryStyle}
    aria-label="Thread details"
    transition:slide={DROPDOWN_TRANSITION}
  >
    {#each breakdownRows as row (row.id)}
      {#if row.settingsTab}
        <button
          class="summary-row {row.variant ?? ''}"
          type="button"
          data-tooltip={row.tooltip}
          aria-label={`Configure ${row.label}`}
          onclick={() => {
            closeSummary();
            onOpenSettings(row.settingsTab);
          }}
        >
          <span class="summary-row-label">{row.label}</span>
          <span class="count" class:zero={row.count === 0}>{row.count}</span>
        </button>
      {:else}
        <div class="summary-row {row.variant ?? ''}" data-tooltip={row.tooltip}>
          <span class="summary-row-label">{row.label}</span>
          <span class="count" class:zero={row.count === 0}>{row.count}</span>
        </div>
      {/if}
    {/each}
  </div>
{/if}

<CheckpointViewer threadId={checkpointThreadId} onClose={() => (checkpointThreadId = null)} />

<style>
  .thread-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    /* Slim chrome strip: less vertical breathing than a panel header so it
       reads as a title bar, not a section banner. Left padding matches panel
       gutters so the title aligns with content below. Right padding is
       tighter because the cog button has its own internal padding. */
    padding: var(--spacing-xs) var(--spacing-sm) var(--spacing-xs) var(--spacing-md);
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-default);
    flex-shrink: 0;
    min-height: 34px;
  }

  .title {
    margin: 0;
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: -0.005em;
    line-height: 1.25;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    /* The title yields width to the meta row, not the other way around. A very
       high shrink factor means that when title + meta can't both fit, almost
       all the shrinkage lands on the title (its ellipsis appears) while the
       meta keeps its full labels. The title still shows as much as fits, and
       only shrinks under genuine pressure (no fixed cap). min-width keeps a
       readable sliver and is the floor the compact-mode measurement uses. */
    flex: 0 1 auto;
    flex-shrink: 1000;
    min-width: 4rem;
    /* Optical centering nudge — flex align-items:center centers the line box,
       but the bold font's ink sits slightly above the line-box center, so the
       text reads as too high without a 1px drop. (The chips beside it are
       self-centered boxes and need no counterpart nudge.) */
    transform: translateY(1px);
  }

  .meta-toggle {
    display: grid;
    place-items: center;
    width: 22px;
    height: 22px;
    padding: 0;
    background: transparent;
    border: 0;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    cursor: pointer;
    flex-shrink: 0;
    /* Cancel most of the header's flex gap so the toggle sits close to the
       title. The chevron is centered in this 22px button, so a small positive
       residual keeps a tight, constant title-to-toggle gap regardless of title
       length without the hover background overlapping the title. */
    margin-left: calc(var(--spacing-md) * -1 + 4px);
    transition: color var(--transition-fast), background var(--transition-fast);
  }

  .meta-toggle:hover {
    color: var(--accent-primary);
    background: var(--bg-hover);
  }

  /* The meta row expands horizontally to the right, so the chevron tracks that
     axis: it points right when collapsed ("expand outward") and rotates to
     point left when the row is open ("collapse it back").

     translateY(2px) is an optical-centering nudge for the chevron only — the
     button itself stays put. It must be respecified on the .open rule
     because that rule sets a single `transform` and would otherwise drop the
     translate when it adds the rotation. Translate listed first so the matrix
     `translate * rotate` shifts the already-rotated glyph down 2px on screen
     (the visual end state is identical for both directions of the chevron). */
  .meta-toggle :global(svg) {
    display: block;
    transform: translateY(2px);
    transition: transform 120ms var(--ease-out);
  }

  .meta-toggle.open :global(svg) {
    transform: translateY(2px) rotate(180deg);
  }

  /* The connected state stays neutral so this reads as a plain toggle; only a
     degraded API surfaces a colored cue (the bottom-bar status carries the
     full health detail, and the tooltip still reports it here). */
  .meta-toggle.disconnected {
    color: var(--error);
  }

  .meta-toggle.checking {
    color: var(--warning);
  }

  .meta {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: var(--font-size-xs);
    letter-spacing: 0.005em;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    /* Content-sized: the row is exactly as wide as its chips and yields to the
       title only after the title has shrunk to its minimum (see flex-shrink
       values). measure() decides when the labels no longer fit and collapses
       them to counts; the cog stays pinned right via .actions margin-left.
       The chips are self-contained boxes centered by the flex row, so the old
       dot-row's optical-centering translate and dot-ring padding are gone. */
    flex: 0 1 auto;
  }

  /* Accent-tinted stat chips (DESIGN.md §3): subtle tinted fill + border from
     the theme's accent-tint tokens so every theme (light/dark/glass) inherits,
     count semibold in primary text, unit label muted. Colour still encodes
     state on top of the shared tint: .reduced flips a chip to the warning
     tint (tools turned off), .accent marks an override/callable. */
  .meta-part {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    height: 22px;
    padding: 0 8px;
    flex-shrink: 0;
    cursor: default;
    /* `font: inherit` first (the shorthand resets line-height), so buttons
       pick up the meta row's xs sizing instead of the UA button font; the
       explicit line-height below then applies to spans and buttons alike. */
    font: inherit;
    letter-spacing: inherit;
    line-height: 1;
    font-variant-numeric: tabular-nums;
    background: var(--accent-tint-bg);
    border: 1px solid var(--accent-tint-border);
    border-radius: var(--radius-md);
    transition: border-color var(--transition-fast), background var(--transition-fast);
  }

  .meta-part:hover {
    border-color: color-mix(in srgb, var(--accent-primary) 45%, transparent);
  }

  /* Clickable stat chips (deep-link into the thread settings tab): the
     stronger hover treatment matches the summary chip, marking these as real
     buttons rather than recolored labels. */
  button.meta-part {
    cursor: pointer;
  }

  button.meta-part:hover {
    border-color: color-mix(in srgb, var(--accent-primary) 55%, transparent);
    background: var(--accent-tint-border);
  }

  button.meta-part.reduced:hover {
    border-color: color-mix(in srgb, var(--warning) 50%, transparent);
    background: color-mix(in srgb, var(--warning) 16%, transparent);
  }

  button.meta-part:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 1px;
  }

  button.meta-part:active {
    transform: scale(var(--press-scale-icon));
  }

  .meta-part .count {
    font-weight: 600;
    color: var(--text-primary);
  }

  .meta-part .label {
    font-weight: 500;
    color: var(--text-muted);
  }

  /* The model chip's label is the thread's identity line, not a unit word, so
     it reads a step stronger than the other labels. */
  .meta-part--model .label {
    color: var(--text-secondary);
  }

  /* Compact word-only chips collapse to a small icon (see MetaPart.compactIcon). */
  .meta-part :global(svg) {
    display: block;
    color: var(--text-muted);
  }

  .meta-part.reduced {
    background: color-mix(in srgb, var(--warning) 9%, transparent);
    border-color: color-mix(in srgb, var(--warning) 32%, transparent);
  }

  .meta-part.reduced:hover {
    border-color: color-mix(in srgb, var(--warning) 50%, transparent);
  }

  .meta-part.reduced .count,
  .meta-part.reduced .label {
    color: var(--warning);
  }

  /* Visibly stronger ring than the default tint border (which sits at 30%
     alpha), so override/callable chips read as marked, not just recolored. */
  .meta-part.accent {
    border-color: color-mix(in srgb, var(--accent-primary) 50%, transparent);
  }

  .meta-part.accent .label {
    color: var(--accent-primary);
  }

  .meta-part.accent :global(svg) {
    color: var(--accent-primary);
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
      background var(--transition-fast),
      transform var(--transition-fast);
  }

  .icon-btn :global(svg) {
    display: block;
  }

  .icon-btn:hover {
    color: var(--accent-primary);
    background: var(--bg-hover);
  }

  /* Pressed cue: tactile scale-down on the header action buttons. The .cog
     variant rotates its inner SVG on hover; this scales the button itself,
     so the two transforms don't conflict. */
  .icon-btn:active {
    transform: scale(var(--press-scale-icon));
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

  /* Summary mode: the model name sits inline beside the title (replacing its
     spot in the meta row), muted like the secondary metadata it summarises.
     A quiet button: clicking deep-links to the thread's Model settings tab. */
  .header-model {
    padding: 0;
    background: transparent;
    border: 0;
    font-family: inherit;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 0 1 auto;
    min-width: 0;
    cursor: pointer;
    transition: color var(--transition-fast);
    /* Same 1px optical-centering nudge as the title, so the two sit level. */
    transform: translateY(1px);
  }

  .header-model:hover {
    color: var(--text-secondary);
  }

  .header-model:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 1px;
    border-radius: var(--radius-sm);
  }

  .header-model.accent {
    color: var(--accent-primary);
    font-weight: 500;
  }

  /* Same-specificity .accent would otherwise win over :hover by source order,
     leaving override-model buttons with no hover cue: brighten toward text
     instead of dropping the accent identity. */
  .header-model.accent:hover {
    color: color-mix(in srgb, var(--accent-primary) 70%, var(--text-primary));
  }

  /* The single chip that stands in for the whole meta row in summary mode. A
     quiet bordered pill (left of the cog) that opens the breakdown popover. */
  .summary-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    height: 26px;
    padding: 0 8px;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    /* Same accent-tinted treatment as the inline stat chips, so summary mode
       shares the meta row's visual vocabulary; hover/open strengthen the
       border and text as the interactivity cue. */
    background: var(--accent-tint-bg);
    border: 1px solid var(--accent-tint-border);
    border-radius: var(--radius-md);
    cursor: pointer;
    white-space: nowrap;
    flex-shrink: 0;
    transition: color var(--transition-fast), background var(--transition-fast),
      border-color var(--transition-fast);
  }

  .summary-chip:hover {
    color: var(--text-primary);
    /* Must be clearly stronger than the resting 30%-alpha tint border: this
       is a real button, so the hover cue has to be perceptible. */
    border-color: color-mix(in srgb, var(--accent-primary) 55%, transparent);
    background: var(--accent-tint-border);
  }

  .summary-chip:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 1px;
  }

  .summary-chip :global(svg) {
    display: block;
    color: var(--text-muted);
    /* Nudged down 1px so the chevron sits optically centred against the
       cap-height of the "22 tools" label rather than its full line box. */
    transform: translateY(1px);
    transition: transform 120ms var(--ease-out);
  }

  /* Open state highlights the chip itself rather than flipping the chevron —
     the app's disclosure arrows don't do a 180° flip, and the popover's
     presence is the real open cue. */
  .summary-chip.open {
    color: var(--text-primary);
    border-color: color-mix(in srgb, var(--accent-primary) 55%, transparent);
    background: var(--accent-tint-border);
  }

  /* Breakdown popover (fixed-positioned + portaled). Chrome mirrors the other
     floating menus: elevated surface, shadow-defined elevation, no border. */
  .summary-popover {
    position: fixed;
    min-width: 184px;
    padding: var(--spacing-xs);
    background: var(--bg-elevated-2, var(--bg-elevated));
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-md);
    z-index: 1000;
  }

  .summary-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    border-radius: var(--radius-sm);
    cursor: default;
  }

  /* Clickable breakdown rows (deep-link like the inline chips): menu-item
     hover, full-width button reset. Rows without a settings target (triggers)
     stay plain divs. */
  button.summary-row {
    width: 100%;
    background: transparent;
    border: 0;
    font-family: inherit;
    text-align: left;
    cursor: pointer;
    transition: background var(--transition-fast), color var(--transition-fast);
  }

  button.summary-row:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  button.summary-row:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .summary-row .count {
    color: var(--text-primary);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  /* A zero count is dimmed so a present-but-empty category (e.g. MCP 0) reads as
     quieter than the active ones. */
  .summary-row .count.zero {
    color: var(--text-muted);
    font-weight: 500;
  }

  .summary-row.reduced .count {
    color: var(--warning);
  }

  .summary-row.accent .count {
    color: var(--accent-primary);
  }
</style>
