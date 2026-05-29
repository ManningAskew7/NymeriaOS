<script lang="ts">
  import type { Thread, ThreadConfig } from '$lib/types';
  import { tick } from 'svelte';
  import { slide } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import { Icon } from '$lib/components/common';
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
  import { computeEffectiveToolCounts, liveTemporaryToolNames } from '$lib/utils/toolCounts';

  interface Props {
    thread: Thread;
    threadConfig?: ThreadConfig | null;
    onOpenSettings: () => void;
  }

  let { thread, threadConfig, onOpenSettings }: Props = $props();

  let showMeta = $state(true);
  // Developer-mode raw checkpoint viewer (null = closed). The button that sets
  // this is gated behind configStore.developerMode.
  let checkpointThreadId = $state<string | null>(null);

  const healthDotClass = $derived(
    healthStore.checking && !healthStore.connected
      ? 'checking'
      : healthStore.connected
        ? 'connected'
        : 'disconnected'
  );

  const healthTooltip = $derived(
    healthStore.checking && !healthStore.connected
      ? 'Checking API...'
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
    return preview + (threadConfig.instructions.length > 80 ? '...' : '');
  });

  type MetaPart = {
    id: string;
    text: string;
    /* Shown instead of `text` when the row is too crowded for full labels. The
       dot color already encodes the category, so we keep only the count and
       drop the unit word. Word-only metrics use '' so just their dot remains.
       Omit to keep the full text in compact mode (used for the model name). */
    compactText?: string;
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
        compactText: `${activeToolCount}`,
        tooltip: toolsTooltip,
        variant: disabledNonMcpCount > 0 ? 'reduced' : 'default',
      });
    }
    if (activeMcpToolCount !== null) {
      parts.push({
        id: 'mcp',
        text: `${activeMcpToolCount} MCP`,
        compactText: `${activeMcpToolCount}`,
        tooltip: mcpTooltip,
        variant: disabledMcpCount > 0 ? 'reduced' : 'default',
      });
    }
    if (callableCount !== null && callableCount > 0) {
      parts.push({
        id: 'callables',
        text: `${callableCount} callable`,
        compactText: `${callableCount}`,
        tooltip: callableTooltip,
        variant: 'default',
      });
    }
    if (activeSkillCount !== null && activeSkillCount > 0) {
      parts.push({
        id: 'skills',
        text: `${activeSkillCount} skill${activeSkillCount !== 1 ? 's' : ''}`,
        compactText: `${activeSkillCount}`,
        tooltip: activeSkillTooltip,
        variant: 'default',
      });
    }
    if (activeKitCount !== null && activeKitCount > 0) {
      parts.push({
        id: 'kits',
        text: `${activeKitCount} kit${activeKitCount !== 1 ? 's' : ''}`,
        compactText: `${activeKitCount}`,
        tooltip: activeKitTooltip,
        variant: 'default',
      });
    }
    if (triggerCount > 0) {
      parts.push({
        id: 'triggers',
        text: `${triggerCount} trigger${triggerCount !== 1 ? 's' : ''}`,
        compactText: `${triggerCount}`,
        tooltip: `${triggerCount} active trigger${triggerCount !== 1 ? 's' : ''}`,
        variant: 'default',
      });
    }
    if (hasInstructions) {
      parts.push({
        id: 'instructions',
        text: 'instructions',
        compactText: '',
        tooltip: instructionsTooltip,
        variant: 'default',
      });
    }
    if (isCallable) {
      parts.push({
        id: 'callable',
        text: 'callable',
        compactText: '',
        tooltip: 'This thread can be called by other threads',
        variant: 'accent',
      });
    }
    return parts;
  });

  // The metrics row keeps as many full labels as the width allows and collapses
  // the rest, right to left, to counts/dots (see MetaPart.compactText) so it
  // always shows the most information that fits. The title yields space first
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
  let metaGap = 14;

  function compactOf(part: MetaPart): string {
    return part.compactText ?? part.text;
  }

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
    const contentKey = metaParts.map((p) => `${p.id}:${p.text}`).join('|');
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
</script>

<header class="thread-header" bind:this={headerEl}>
  <h2 class="title" title={thread.title} bind:this={titleEl}>{thread.title}</h2>

  {#if metaParts.length > 0}
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
          <span class="meta-part meta-part--{part.id}" class:reduced={part.variant === 'reduced'} class:accent={part.variant === 'accent'} title={part.tooltip}>{showFull ? part.text : compactOf(part)}</span>
        {/each}
      </div>
    {/if}
  {/if}

  <div class="actions" bind:this={actionsEl}>
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
    {#if configStore.developerMode}
      <button
        class="icon-btn"
        onclick={() => (checkpointThreadId = thread.id)}
        title="View raw checkpoint (developer)"
        type="button"
        aria-label="View raw checkpoint"
      >
        <Icon name="terminal" size={16} />
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
       text reads as too high. 1px down matches the offset already applied to
       the .meta row beside it. */
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
     point left when the row is open ("collapse it back"). */
  .meta-toggle :global(svg) {
    display: block;
    transition: transform 120ms cubic-bezier(0.33, 1, 0.68, 1);
  }

  .meta-toggle.open :global(svg) {
    transform: rotate(180deg);
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
    gap: 14px;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.25;
    letter-spacing: 0.005em;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    /* Content-sized: the row is exactly as wide as its labels and yields to the
       title only after the title has shrunk to its minimum (see flex-shrink
       values). measure() decides when the labels no longer fit and collapses
       them to counts; the cog stays pinned right via .actions margin-left. */
    flex: 0 1 auto;
    /* 2px left padding gives the first meta-part's dot room to render its 1px
       outer ring without being clipped by overflow:hidden. */
    padding-left: 2px;
    transform: translate(1px, 1px);
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
