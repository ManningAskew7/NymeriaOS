<script lang="ts">
  import type { Trigger, TriggerSourceInfo } from '$lib/types';
  import { slide, crossfade } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import { Icon, ToggleSwitch } from '$lib/components/common';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';

  // Crossfade animates an element from its old DOM position to its new one
  // by computing a transform between the two bounding rects. We use this so
  // the OUTLOOK chip and the toggle physically slide from the header row to
  // the action row when the card expands — matched to the box's open speed.
  const [send, receive] = crossfade({
    duration: 120,
    easing: cubicOut,
    fallback: (node) => ({
      duration: 100,
      easing: cubicOut,
      css: (t) => `opacity: ${t}`,
    }),
  });

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

  let expanded = $state(false);
  let testResult = $state<string | null>(null);
  let testOk = $state(false);
  let testing = $state(false);
  let confirmDelete = $state(false);
  let toggling = $state(false);

  const sourceInfo = $derived<TriggerSourceInfo | undefined>(
    triggersStore.sources[trigger.source_type]
  );
  const sourceIcon = $derived(sourceInfo?.icon || 'bolt');

  const SOURCE_LABELS: Record<string, string> = {
    webhook: 'Webhook',
    outlook_email: 'Outlook',
    rss: 'RSS Feed',
    slack: 'Slack',
    teams: 'Teams',
    http_poll: 'HTTP Poll',
  };
  const sourceLabel = $derived(
    SOURCE_LABELS[trigger.source_type] ||
      sourceInfo?.name ||
      trigger.source_type.replace(/_/g, ' ')
  );

  const ACTION_META: Record<string, { label: string; icon: string; templateKey: string }> = {
    agent_prompt: { label: 'Prompt', icon: 'chat', templateKey: 'prompt_template' },
    notify: { label: 'Notify', icon: 'bell', templateKey: 'message_template' },
    create_todo: { label: 'Create task', icon: 'check', templateKey: 'task_template' },
  };
  const actionMeta = $derived(
    ACTION_META[trigger.action.type] || { label: trigger.action.type, icon: 'bolt', templateKey: '' }
  );

  const actionTemplate = $derived.by<string>(() => {
    const cfg = trigger.action.config || {};
    const key = actionMeta.templateKey;
    const val = (key && (cfg[key] ?? cfg['prompt'])) as string | undefined;
    return typeof val === 'string' ? val : '';
  });

  function fieldLabel(key: string): string {
    return key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase());
  }

  function formatCooldown(seconds: number): string {
    if (!seconds) return 'No cooldown';
    if (seconds < 60) return `${seconds}s cooldown`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m cooldown`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h cooldown`;
    return `${Math.round(seconds / 86400)}d cooldown`;
  }

  function formatTimeAgo(dateStr: string | null): string {
    if (!dateStr) return 'Never fired';
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

  function truncate(s: string, n: number): string {
    if (!s) return '';
    const clean = s.replace(/\s+/g, ' ').trim();
    return clean.length > n ? clean.slice(0, n).trimEnd() + '…' : clean;
  }

  function isSensitiveKey(key: string): boolean {
    const k = key.toLowerCase();
    return (
      k.includes('secret') ||
      k.includes('token') ||
      k.includes('password') ||
      k === 'api_key' ||
      k.endsWith('_key')
    );
  }

  function stringifyValue(v: unknown): string {
    if (v === null || v === undefined || v === '') return '—';
    if (typeof v === 'boolean') return v ? 'Yes' : 'No';
    if (Array.isArray(v)) return v.length ? v.join(', ') : '—';
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v);
  }

  // Summary pulled from source_config: shows the most identifying 1-2 fields inline
  const sourceSummary = $derived.by<string>(() => {
    const cfg = trigger.source_config || {};
    if (trigger.source_type === 'webhook') {
      return `POST /triggers/webhook/${trigger.id.slice(0, 8)}…`;
    }
    if (trigger.source_type === 'outlook_email') {
      const folder = (cfg.folder as string) || 'inbox';
      const parts = [folder];
      if (cfg.unread_only) parts.push('unread');
      if (cfg.from_filter) parts.push(`from: ${cfg.from_filter}`);
      else if (cfg.subject_filter) parts.push(`re: ${cfg.subject_filter}`);
      return parts.join(' · ');
    }
    // Generic: first non-empty, non-secret string/boolean field
    const entries = Object.entries(cfg).filter(
      ([k, v]) => !isSensitiveKey(k) && v !== '' && v !== null && v !== undefined
    );
    if (entries.length === 0) return 'Default configuration';
    const [k, v] = entries[0];
    return `${fieldLabel(k)}: ${stringifyValue(v)}`;
  });

  // All source_config fields for expanded view
  const configEntries = $derived(
    Object.entries(trigger.source_config || {})
      .filter(([, v]) => v !== '' && v !== null && v !== undefined)
      .map(([k, v]) => ({
        key: k,
        label: fieldLabel(k),
        value: isSensitiveKey(k) ? '••••••••' : stringifyValue(v),
      }))
  );

  const hasDetails = $derived(
    configEntries.length > 0 ||
      (trigger.conditions?.length ?? 0) > 0 ||
      !!actionTemplate ||
      !!trigger.last_error
  );

  const healthColor = $derived(
    trigger.health_status === 'healthy'
      ? 'var(--success)'
      : trigger.health_status === 'degraded'
        ? 'var(--warning)'
        : 'var(--error)'
  );

  const healthLabel = $derived(
    trigger.health_status === 'healthy'
      ? 'Healthy'
      : trigger.health_status === 'degraded'
        ? 'Degraded'
        : 'Failing'
  );

  // Index of the first character that wasn't visible in the collapsed state.
  // Characters before this index render instantly; characters at/after it
  // cascade in via the letter-by-letter animation.
  let cutoffIndex = $state(0);
  let titleEl = $state<HTMLSpanElement | null>(null);

  function measureCutoff() {
    if (!titleEl) {
      cutoffIndex = 0;
      return;
    }
    const containerWidth = titleEl.clientWidth;
    if (containerWidth === 0) {
      cutoffIndex = 0;
      return;
    }
    const computed = window.getComputedStyle(titleEl);
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    if (!ctx) {
      cutoffIndex = 0;
      return;
    }
    // Reconstruct the font shorthand the browser would use for this element.
    ctx.font = `${computed.fontStyle} ${computed.fontWeight} ${computed.fontSize} ${computed.fontFamily}`;
    const name = trigger.name;
    const fullWidth = ctx.measureText(name).width;
    if (fullWidth <= containerWidth) {
      // Nothing was truncated — all letters are "already visible".
      cutoffIndex = name.length;
      return;
    }
    const ellipsisWidth = ctx.measureText('…').width;
    let idx = 0;
    for (let i = 1; i <= name.length; i++) {
      const w = ctx.measureText(name.slice(0, i)).width;
      if (w + ellipsisWidth > containerWidth) {
        idx = i - 1;
        break;
      }
      idx = i;
    }
    cutoffIndex = Math.max(0, idx);
  }

  function toggleExpand() {
    if (!expanded) {
      measureCutoff();
    }
    expanded = !expanded;
  }

  function stop(e: Event) {
    e.stopPropagation();
  }

  async function handleToggle(e: Event) {
    e.stopPropagation();
    toggling = true;
    try {
      await triggersStore.updateTrigger(trigger.id, { enabled: !trigger.enabled });
    } finally {
      toggling = false;
    }
  }

  async function handleDelete(e: Event) {
    e.stopPropagation();
    await triggersStore.deleteTrigger(trigger.id);
    confirmDelete = false;
  }

  async function handleTest(e: Event) {
    e.stopPropagation();
    testing = true;
    testResult = null;
    try {
      const result = await triggersStore.testTrigger(trigger.id);
      testOk = result.conditions_pass;
      testResult = result.conditions_pass
        ? truncate(result.rendered_output, 140)
        : 'Conditions would NOT pass for the sample event.';
      if (!expanded) expanded = true;
      setTimeout(() => {
        testResult = null;
      }, 6000);
    } catch (err) {
      testOk = false;
      testResult = humanizeErrorText(err, { action: 'test', resource: 'the trigger' });
      setTimeout(() => {
        testResult = null;
      }, 6000);
    } finally {
      testing = false;
    }
  }

  function handleThreadClick(e: Event) {
    e.stopPropagation();
    onNavigateToThread?.();
  }
</script>

<div
  class="trigger-card"
  class:disabled={!trigger.enabled}
  class:expandable={hasDetails}
  class:expanded
  class:unhealthy={trigger.health_status !== 'healthy' && trigger.enabled}
  style="animation-delay: {animationDelay}ms"
  role="button"
  tabindex="0"
  aria-expanded={expanded}
  onclick={toggleExpand}
  onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleExpand(); } }}
>
  <span class="health-rail" style="background: {healthColor}" aria-hidden="true"></span>

  <!-- Header row: icon + name + (when collapsed) chip+toggle + expand affordance -->
  <div class="card-header">
    <div class="source-badge" title={sourceLabel}>
      <Icon name={sourceIcon} size={14} />
    </div>

    <div class="title-col">
      <div class="title-row">
        <span
          class="trigger-name"
          class:muted={!trigger.enabled}
          class:expanded-name={expanded}
          bind:this={titleEl}
        >
          {#if expanded}
            {#each [...trigger.name] as ch, i (i)}
              <span
                class="reveal-char"
                class:instant={i < cutoffIndex}
                style="--idx: {Math.max(0, i - cutoffIndex)}"
              >{ch}</span>
            {/each}
          {:else}
            {trigger.name}
          {/if}
        </span>
        {#if !expanded}
          <span
            class="source-chip"
            data-source={trigger.source_type}
            in:receive={{ key: `chip-${trigger.id}` }}
            out:send={{ key: `chip-${trigger.id}` }}
          >{sourceLabel}</span>
        {/if}
      </div>
    </div>

    <div class="header-actions">
      {#if !expanded}
        <div
          class="header-toggle"
          in:receive={{ key: `toggle-${trigger.id}` }}
          out:send={{ key: `toggle-${trigger.id}` }}
        >
          <ToggleSwitch
            checked={trigger.enabled}
            disabled={toggling}
            onclick={handleToggle}
            title={trigger.enabled ? 'Disable' : 'Enable'}
            ariaLabel={trigger.enabled ? 'Disable trigger' : 'Enable trigger'}
            size="sm"
            variant="outlined"
          />
        </div>
      {/if}

      <button
        class="expand-btn"
        class:rotated={expanded}
        type="button"
        aria-label={expanded ? 'Collapse trigger details' : 'Expand trigger details'}
        onclick={(e) => { e.stopPropagation(); toggleExpand(); }}
        tabindex="-1"
      >
        <Icon name="chevronRight" size={12} />
      </button>
    </div>
  </div>

  {#if expanded}
  <div class="expanded-wrap" transition:slide={DROPDOWN_TRANSITION}>
    <!-- Thread pill + chip + toggle + action preview -->
    <div class="thread-action-row">
      {#if threadTitle}
        {#if onNavigateToThread}
          <button
            class="thread-pill clickable"
            onclick={handleThreadClick}
            type="button"
            title={`Go to thread: ${threadTitle}`}
          >
            <Icon name="chat" size={10} />
            <span class="thread-name">{threadTitle}</span>
            <Icon name="chevronRight" size={10} />
          </button>
        {:else}
          <span class="thread-pill" title={threadTitle}>
            <Icon name="chat" size={10} />
            <span class="thread-name">{threadTitle}</span>
          </span>
        {/if}
      {/if}
      <span
        class="source-chip in-action-row"
        data-source={trigger.source_type}
        in:receive={{ key: `chip-${trigger.id}` }}
        out:send={{ key: `chip-${trigger.id}` }}
      >{sourceLabel}</span>
      <div
        class="action-row-toggle"
        in:receive={{ key: `toggle-${trigger.id}` }}
        out:send={{ key: `toggle-${trigger.id}` }}
      >
        <ToggleSwitch
          checked={trigger.enabled}
          disabled={toggling}
          onclick={handleToggle}
          title={trigger.enabled ? 'Disable' : 'Enable'}
          ariaLabel={trigger.enabled ? 'Disable trigger' : 'Enable trigger'}
          size="sm"
          variant="outlined"
        />
      </div>
      {#if actionTemplate}
        <span class="action-preview">{truncate(actionTemplate, 62)}</span>
      {:else if actionMeta}
        <span class="action-kind">
          <Icon name={actionMeta.icon} size={10} />
          <span>{actionMeta.label}</span>
        </span>
      {/if}
    </div>

    <!-- Meta stats -->
    <div class="meta-row">
      <span class="meta-item" title={healthLabel}>
        <span class="health-dot" style="background: {healthColor}"></span>
        {healthLabel}
      </span>
      <span class="meta-sep" aria-hidden="true">·</span>
      <span class="meta-item" title="Times fired">
        <Icon name="bolt" size={10} />
        <span><span class="num">{trigger.fire_count}</span> {trigger.fire_count === 1 ? 'fire' : 'fires'}</span>
      </span>
      <span class="meta-sep" aria-hidden="true">·</span>
      <span class="meta-item clock-item" title="Last fired">
        <Icon name="clock" size={10} />
        {formatTimeAgo(trigger.last_fired)}
      </span>
      {#if trigger.cooldown_seconds > 0}
        <span class="meta-sep" aria-hidden="true">·</span>
        <span class="meta-item cooldown-item" title="Cooldown between fires">
          {formatCooldown(trigger.cooldown_seconds)}
        </span>
      {/if}
    </div>

    {#if trigger.last_error && trigger.health_status !== 'healthy'}
      <div class="error-banner" title={trigger.last_error}>
        <Icon name="warning" size={11} />
        <span>{truncate(trigger.last_error, 90)}</span>
      </div>
    {/if}

    {#if testResult !== null}
      <div class="test-banner" class:ok={testOk} class:bad={!testOk}>
        <Icon name={testOk ? 'success' : 'warning'} size={11} />
        <span>{testResult}</span>
      </div>
    {/if}

    <!-- Expanded details -->
    {#if hasDetails}
    <div class="details">
      <div class="source-summary">
        <span class="summary-label">Source</span>
        <span class="summary-value">{sourceSummary}</span>
      </div>
      {#if actionTemplate}
        <div class="detail-block">
          <div class="detail-label">
            <Icon name={actionMeta.icon} size={11} />
            <span>{actionMeta.label} Template</span>
          </div>
          <pre class="template-body">{actionTemplate}</pre>
        </div>
      {/if}

      {#if configEntries.length > 0}
        <div class="detail-block">
          <div class="detail-label">
            <Icon name="cog" size={11} />
            <span>Source Configuration</span>
          </div>
          <dl class="config-grid">
            {#each configEntries as entry (entry.key)}
              <dt>{entry.label}</dt>
              <dd>{entry.value}</dd>
            {/each}
          </dl>
        </div>
      {/if}

      {#if trigger.conditions && trigger.conditions.length > 0}
        <div class="detail-block">
          <div class="detail-label">
            <Icon name="sort" size={11} />
            <span>Conditions ({trigger.conditions.length})</span>
          </div>
          <ul class="conditions-list">
            {#each trigger.conditions as cond, i (i)}
              <li>
                <code class="cond-field">{cond.field}</code>
                <span class="cond-op">{cond.operator.replace(/_/g, ' ')}</span>
                <code class="cond-value">{cond.value}</code>
              </li>
            {/each}
          </ul>
        </div>
      {/if}

      <div class="action-bar">
        <button class="ghost-btn" onclick={handleTest} disabled={testing} type="button">
          <Icon name="terminal" size={12} />
          <span>{testing ? 'Testing…' : 'Test'}</span>
        </button>
        {#if onHistory}
          <button class="ghost-btn" onclick={(e) => { stop(e); onHistory(trigger); }} type="button">
            <Icon name="clock" size={12} />
            <span>History</span>
          </button>
        {/if}
        {#if onEdit}
          <button class="ghost-btn" onclick={(e) => { stop(e); onEdit(trigger); }} type="button">
            <Icon name="edit" size={12} />
            <span>Edit</span>
          </button>
        {/if}
        <div class="spacer"></div>
        {#if !confirmDelete}
          <button
            class="ghost-btn danger"
            onclick={(e) => { stop(e); confirmDelete = true; }}
            type="button"
          >
            <Icon name="trash" size={12} />
          </button>
        {:else}
          <span class="confirm-label">Delete?</span>
          <button class="ghost-btn danger solid" onclick={handleDelete} type="button">
            <Icon name="check" size={12} />
          </button>
          <button
            class="ghost-btn"
            onclick={(e) => { stop(e); confirmDelete = false; }}
            type="button"
          >
            <Icon name="x" size={12} />
          </button>
        {/if}
      </div>
    </div>
    {/if}
  </div>
  {/if}
</div>

<style>
  .trigger-card {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    /* Matches .todo-item — both feeds use the same list-item tier so a
       trigger card and a todo card read as the same family. */
    padding: var(--spacing-sm-plus) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle, var(--border-default));
    border-radius: var(--radius-md);
    overflow: hidden;
    cursor: pointer;
    animation: cardIn var(--transition-normal) both;
    transition:
      background var(--transition-fast),
      border-color var(--transition-fast),
      transform var(--transition-fast);
  }

  @keyframes cardIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .trigger-card:hover {
    background: var(--bg-hover);
    border-color: var(--border-default);
  }

  .trigger-card.disabled {
    opacity: 0.62;
  }

  .trigger-card.unhealthy {
    border-color: color-mix(in srgb, var(--error) 45%, var(--border-subtle));
  }

  .trigger-card.expanded {
    background: var(--bg-hover);
    /* Extra bottom padding to breathe around the revealed details */
    padding-bottom: var(--spacing-sm-plus);
  }

  /* Health rail — a thin accent rail on the left that reflects status.
     Insets match the todo-item scheduled rail so the two feeds align. */
  .health-rail {
    position: absolute;
    left: 0;
    top: var(--spacing-sm-plus);
    bottom: var(--spacing-sm-plus);
    width: 2px;
    border-radius: 0 1px 1px 0;
    opacity: 0.8;
    transition: opacity var(--transition-fast);
  }

  .trigger-card.disabled .health-rail {
    opacity: 0.35;
  }

  /* --- Header --- */
  .card-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    min-width: 0;
  }

  .source-badge {
    flex-shrink: 0;
    width: 24px;
    height: 24px;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(
      135deg,
      rgba(var(--accent-primary-rgb), 0.18),
      rgba(var(--accent-primary-rgb), 0.08)
    );
    border: 1px solid rgba(var(--accent-primary-rgb), 0.22);
    color: var(--accent-primary);
  }

  .source-badge :global(svg) {
    width: 12px;
    height: 12px;
  }

  .disabled .source-badge {
    background: var(--bg-elevated-2);
    border-color: var(--border-subtle, var(--border-default));
    color: var(--text-muted);
  }

  .title-col {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .title-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    min-width: 0;
  }

  .trigger-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    letter-spacing: -0.005em;
    line-height: 1.35;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 0 1 auto;
    min-width: 0;
    max-width: 100%;
  }

  /* When the card is expanded, drop the truncation so the full title can be
     shown. The reveal effect is driven by the per-character animation
     below instead of a width transition. */
  .trigger-name.expanded-name {
    overflow: visible;
    text-overflow: clip;
    max-width: none;
  }

  /* Each character of the title fades in with a staggered delay so the
     title appears to type itself in from left to right. The first character
     is delayed by ~140ms so the OUTLOOK chip + toggle have time to start
     sliding down before letters start filling that area. */
  .reveal-char {
    display: inline;
    opacity: 0;
    white-space: pre;
    animation: triggerNameChar 100ms cubic-bezier(0.4, 0, 0.2, 1) forwards;
    /* Wait long enough for the OUTLOOK chip + toggle to start clearing out
       of the title row (~60ms of the 120ms slide), then cascade fast (10ms
       per char) so the reveal finishes about the same time the box does. */
    animation-delay: calc(60ms + var(--idx, 0) * 10ms);
  }

  /* Characters that were already visible in the collapsed (truncated) state
     don't animate — they stay put so only the "newly revealed" letters
     past the ellipsis cascade in. */
  .reveal-char.instant {
    opacity: 1;
    animation: none;
  }

  @keyframes triggerNameChar {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .trigger-name.muted {
    color: var(--text-primary);
  }

  .source-chip {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    /* Gap matches horizontal padding (--spacing-sm = 8px) so the dot has
       equal breathing on both sides: 8px from chip-left to dot, 8px from
       dot to text, 8px from text to chip-right. Symmetric rhythm. */
    gap: var(--spacing-sm);
    flex-shrink: 0;
    padding: var(--spacing-2xs) var(--spacing-sm);
    font-size: var(--font-size-3xs);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    line-height: 1.45;
  }

  /* Per-source dot — leading colored circle differentiates trigger sources
     at a glance so a feed of mixed sources scans more easily than a row of
     identical pills. Falls back to muted text for any custom source not in
     the known list. */
  .source-chip::before {
    content: '';
    width: 6px;
    height: 6px;
    border-radius: var(--radius-full);
    background: var(--chip-dot-color, var(--text-muted));
    flex-shrink: 0;
  }

  .source-chip[data-source="webhook"] { --chip-dot-color: var(--warning); }
  .source-chip[data-source="outlook_email"] { --chip-dot-color: var(--info); }
  .source-chip[data-source="rss"] { --chip-dot-color: #fb923c; }
  .source-chip[data-source="slack"] { --chip-dot-color: var(--success); }
  .source-chip[data-source="teams"] { --chip-dot-color: #a78bfa; }
  .source-chip[data-source="http_poll"] { --chip-dot-color: var(--accent-primary); }

  .thread-action-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    min-width: 0;
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    flex-wrap: wrap;
  }

  .action-kind {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    flex-shrink: 0;
    font-weight: 600;
    color: var(--text-secondary);
  }

  .action-preview {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
    flex: 1 1 100%;
    font-style: italic;
  }

  /* Wrapper that holds all expanded content. flex column matches the gap the
     trigger-card has between its direct children so the layout looks the
     same as before, but lets us drive a single slide transition. */
  .expanded-wrap {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    min-width: 0;
  }

  /* Source chip and toggle when they live in the action row instead of the
     header. margin-left:auto pushes them to the right side, and the
     margin-right on the toggle leaves space for the chevron column above —
     that way the chip+toggle land at the exact same X as their header
     counterparts and only need to slide straight down. */
  .source-chip.in-action-row {
    align-self: center;
    margin-left: auto;
  }
  .action-row-toggle {
    display: inline-flex;
    align-items: center;
    flex-shrink: 0;
    /* Chevron (24px) + gap (6px) reserved above */
    margin-right: 30px;
  }

  .action-preview::before {
    content: '“';
    margin-right: 1px;
    opacity: 0.6;
  }
  .action-preview::after {
    content: '”';
    margin-left: 1px;
    opacity: 0.6;
  }

  .header-actions {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-top: 1px;
  }

  .header-actions :global(.toggle-switch) {
    transform: translateY(1px);
  }

  .action-row-toggle :global(.toggle-switch) {
    transform: translateY(-1px);
  }

  .expand-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    border: 0;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-muted);
    transition: transform 120ms cubic-bezier(0.33, 1, 0.68, 1);
    cursor: pointer;
  }

  .expand-btn:hover {
    background: var(--bg-elevated-2);
    color: var(--text-primary);
  }

  .expand-btn.rotated {
    transform: rotate(90deg);
  }

  /* --- Thread pill (global view) --- */
  .thread-pill {
    align-self: flex-start;
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-2xs) var(--spacing-sm);
    max-width: 100%;
    font-size: var(--font-size-2xs);
    font-weight: 500;
    color: var(--text-secondary);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    cursor: default;
    transition: all var(--transition-fast);
  }

  .thread-pill.clickable {
    cursor: pointer;
  }

  .thread-pill.clickable:hover {
    background: var(--bg-hover);
    border-color: var(--border-default);
    color: var(--text-primary);
  }

  .thread-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
    max-width: 220px;
  }

  /* --- Source summary --- */
  .source-summary {
    display: flex;
    align-items: baseline;
    gap: var(--spacing-sm);
    font-size: var(--font-size-2xs);
    min-width: 0;
  }

  .summary-label {
    flex-shrink: 0;
    font-size: var(--font-size-3xs);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
  }

  .summary-value {
    color: var(--text-secondary);
    font-family: var(--font-mono);
    font-size: var(--font-size-2xs);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
    flex: 1;
  }

  /* --- Meta row --- */
  .meta-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    row-gap: var(--spacing-xs);
    column-gap: var(--spacing-sm);
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  .meta-item {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-2xs);
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
  }

  .meta-item :global(svg) {
    width: 12px;
    height: 12px;
    flex-shrink: 0;
  }

  .clock-item :global(svg) {
    margin-right: 3px;
  }

  .cooldown-item {
    font-variant-numeric: normal;
  }

  .meta-item .num {
    color: var(--text-secondary);
    font-weight: 600;
  }

  .health-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-right: 3px;
  }

  .meta-sep {
    opacity: 0.4;
    user-select: none;
  }

  /* --- Banners --- */
  .error-banner,
  .test-banner {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm) var(--spacing-sm);
    font-size: var(--font-size-2xs);
    line-height: 1.35;
    border-radius: var(--radius-sm);
    border: 1px solid;
  }

  .error-banner {
    color: var(--error);
    background: color-mix(in srgb, var(--error) 10%, transparent);
    border-color: color-mix(in srgb, var(--error) 30%, transparent);
  }

  .error-banner span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }

  .test-banner.ok {
    color: var(--success);
    background: color-mix(in srgb, var(--success) 10%, transparent);
    border-color: color-mix(in srgb, var(--success) 30%, transparent);
  }

  .test-banner.bad {
    color: var(--warning);
    background: color-mix(in srgb, var(--warning) 10%, transparent);
    border-color: color-mix(in srgb, var(--warning) 30%, transparent);
  }

  .test-banner span {
    word-break: break-word;
    flex: 1;
    min-width: 0;
  }

  /* --- Expanded details --- */
  .details {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm-plus);
    margin-top: var(--spacing-2xs);
    padding-top: var(--spacing-sm-plus);
    border-top: 1px dashed var(--border-subtle, var(--border-default));
    cursor: default;
    animation: detailsIn var(--transition-normal) both;
  }

  @keyframes detailsIn {
    from { opacity: 0; transform: translateY(-2px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .detail-block {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    min-width: 0;
  }

  .detail-label {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-3xs);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
  }

  .template-body {
    margin: 0;
    padding: var(--spacing-sm) var(--spacing-sm-plus);
    background: var(--bg-base);
    border: 1px solid var(--border-subtle, var(--border-default));
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
    font-size: var(--font-size-2xs);
    line-height: 1.5;
    color: var(--text-secondary);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 120px;
    overflow: auto;
  }

  .config-grid {
    display: grid;
    grid-template-columns: minmax(70px, max-content) 1fr;
    gap: var(--spacing-xs) var(--spacing-sm-plus);
    margin: 0;
    font-size: var(--font-size-2xs);
  }

  .config-grid dt {
    color: var(--text-muted);
    font-weight: 500;
    white-space: nowrap;
  }

  .config-grid dd {
    margin: 0;
    color: var(--text-secondary);
    font-family: var(--font-mono);
    font-size: var(--font-size-2xs);
    overflow-wrap: anywhere;
    min-width: 0;
  }

  .conditions-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .conditions-list li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-2xs);
    line-height: 1.3;
  }

  .cond-field,
  .cond-value {
    padding: var(--spacing-2xs) var(--spacing-xs);
    font-family: var(--font-mono);
    font-size: var(--font-size-2xs);
    background: var(--bg-base);
    border: 1px solid var(--border-subtle, var(--border-default));
    border-radius: 3px;
    color: var(--text-secondary);
  }

  .cond-op {
    color: var(--text-muted);
    font-style: italic;
  }

  /* --- Action bar --- */
  .action-bar {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding-top: var(--spacing-sm);
    border-top: 1px dashed var(--border-subtle, var(--border-default));
  }

  .spacer {
    flex: 1;
  }

  .ghost-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-2xs);
    font-weight: 500;
    color: var(--text-secondary);
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .ghost-btn:hover:not(:disabled) {
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    border-color: var(--border-default);
  }

  .ghost-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .ghost-btn.danger {
    color: color-mix(in srgb, var(--error) 85%, var(--text-secondary));
  }

  .ghost-btn.danger:hover:not(:disabled) {
    color: var(--error);
    background: color-mix(in srgb, var(--error) 10%, transparent);
    border-color: color-mix(in srgb, var(--error) 35%, transparent);
  }

  .ghost-btn.danger.solid {
    color: white;
    background: var(--error);
    border-color: var(--error);
  }

  .ghost-btn.danger.solid:hover:not(:disabled) {
    filter: brightness(1.08);
  }

  .confirm-label {
    font-size: var(--font-size-2xs);
    color: var(--error);
    margin-right: 2px;
  }
</style>
