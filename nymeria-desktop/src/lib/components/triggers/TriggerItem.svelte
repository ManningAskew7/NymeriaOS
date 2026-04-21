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
    create_todo: { label: 'Create TODO', icon: 'check', templateKey: 'task_template' },
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

  function toggleExpand() {
    if (hasDetails) expanded = !expanded;
  }

  function onKeydownCard(e: KeyboardEvent) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggleExpand();
    }
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
      testResult = `Error: ${err instanceof Error ? err.message : 'test failed'}`;
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

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div
  class="trigger-card"
  class:disabled={!trigger.enabled}
  class:expandable={hasDetails}
  class:expanded
  class:unhealthy={trigger.health_status !== 'healthy' && trigger.enabled}
  style="animation-delay: {animationDelay}ms"
  role={hasDetails ? 'button' : undefined}
  tabindex={hasDetails ? 0 : undefined}
  aria-expanded={hasDetails ? expanded : undefined}
  onclick={toggleExpand}
  onkeydown={onKeydownCard}
>
  <span class="health-rail" style="background: {healthColor}" aria-hidden="true"></span>

  <!-- Header row: icon + name + toggle + expand affordance -->
  <div class="card-header">
    <div class="source-badge" title={sourceLabel}>
      <Icon name={sourceIcon} size={14} />
    </div>

    <div class="title-col">
      <div class="title-row">
        <span class="trigger-name" class:muted={!trigger.enabled}>{trigger.name}</span>
        <span class="source-chip">{sourceLabel}</span>
      </div>
      <div class="subtitle-row">
        <span class="action-kind">
          <Icon name={actionMeta.icon} size={10} />
          <span>{actionMeta.label}</span>
        </span>
        {#if actionTemplate}
          <span class="action-preview">{truncate(actionTemplate, 62)}</span>
        {/if}
      </div>
    </div>

    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <div class="header-actions" onclick={stop} onkeydown={stop} role="group">
      <label
        class="toggle"
        class:on={trigger.enabled}
        title={trigger.enabled ? 'Disable' : 'Enable'}
      >
        <input
          type="checkbox"
          checked={trigger.enabled}
          disabled={toggling}
          onchange={handleToggle}
        />
        <span class="toggle-track">
          <span class="toggle-thumb"></span>
        </span>
      </label>

      {#if hasDetails}
        <span class="chevron" class:rotated={expanded} aria-hidden="true">
          <Icon name="chevronDown" size={12} />
        </span>
      {/if}
    </div>
  </div>

  <!-- Thread pill: most visible in global view so you know what thread this belongs to -->
  {#if threadTitle}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <button
      class="thread-pill"
      class:clickable={!!onNavigateToThread}
      onclick={handleThreadClick}
      type="button"
      title={onNavigateToThread ? `Go to thread: ${threadTitle}` : threadTitle}
    >
      <Icon name="chat" size={10} />
      <span class="thread-name">{threadTitle}</span>
      {#if onNavigateToThread}
        <Icon name="chevronRight" size={10} />
      {/if}
    </button>
  {/if}

  <!-- Source summary always visible -->
  <div class="source-summary">
    <span class="summary-label">Source</span>
    <span class="summary-value">{sourceSummary}</span>
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
      <span class="num">{trigger.fire_count}</span>
      {trigger.fire_count === 1 ? 'fire' : 'fires'}
    </span>
    <span class="meta-sep" aria-hidden="true">·</span>
    <span class="meta-item" title="Last fired">
      <Icon name="clock" size={10} />
      {formatTimeAgo(trigger.last_fired)}
    </span>
    {#if trigger.cooldown_seconds > 0}
      <span class="meta-sep" aria-hidden="true">·</span>
      <span class="meta-item" title="Cooldown between fires">
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
  {#if expanded && hasDetails}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <div class="details" onclick={stop} onkeydown={stop} role="group">
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

<style>
  .trigger-card {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px 10px 14px;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle, var(--border-default));
    border-radius: var(--radius-md);
    overflow: hidden;
    animation: cardIn 0.28s ease-out both;
    transition:
      background var(--transition-fast),
      border-color var(--transition-fast),
      transform var(--transition-fast);
  }

  @keyframes cardIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .trigger-card.expandable {
    cursor: pointer;
  }

  .trigger-card:hover {
    background: var(--bg-hover);
    border-color: var(--border-default);
  }

  .trigger-card:focus-visible {
    outline: 1px solid var(--accent-primary);
    outline-offset: 1px;
  }

  .trigger-card.disabled {
    opacity: 0.62;
  }

  .trigger-card.unhealthy {
    border-color: color-mix(in srgb, var(--error) 45%, var(--border-subtle));
  }

  .trigger-card.expanded {
    background: var(--bg-hover);
  }

  /* Health rail — a thin accent rail on the left that reflects status */
  .health-rail {
    position: absolute;
    left: 0;
    top: 10px;
    bottom: 10px;
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
    align-items: flex-start;
    gap: 10px;
    min-width: 0;
  }

  .source-badge {
    flex-shrink: 0;
    width: 30px;
    height: 30px;
    border-radius: 8px;
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
    gap: 3px;
  }

  .title-row {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }

  .trigger-name {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: -0.005em;
    line-height: 1.25;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 0 1 auto;
    min-width: 0;
  }

  .trigger-name.muted {
    color: var(--text-muted);
  }

  .source-chip {
    flex-shrink: 0;
    padding: 1px 6px;
    font-size: 9.5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.1);
    border-radius: var(--radius-full);
    line-height: 1.45;
  }

  .subtitle-row {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    font-size: 11px;
    color: var(--text-muted);
    line-height: 1.3;
  }

  .action-kind {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    flex-shrink: 0;
    font-weight: 600;
    color: var(--text-secondary);
  }

  .action-preview {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
    font-style: italic;
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
    gap: 6px;
    margin-top: 1px;
  }

  /* Toggle */
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
    display: block;
    width: 28px;
    height: 16px;
    border-radius: 8px;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
    position: relative;
    transition: all var(--transition-fast);
  }

  .toggle-thumb {
    position: absolute;
    top: 1px;
    left: 1px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--text-muted);
    transition: all var(--transition-fast);
  }

  .toggle.on .toggle-track {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .toggle.on .toggle-thumb {
    left: 13px;
    background: white;
  }

  .chevron {
    display: inline-flex;
    color: var(--text-muted);
    transition: transform var(--transition-fast);
  }

  .chevron.rotated {
    transform: rotate(180deg);
  }

  /* --- Thread pill (global view) --- */
  .thread-pill {
    align-self: flex-start;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px 3px 7px;
    max-width: 100%;
    font-size: 11px;
    font-weight: 500;
    color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.08);
    border: 1px solid rgba(var(--accent-primary-rgb), 0.2);
    border-radius: var(--radius-full);
    cursor: default;
    transition: all var(--transition-fast);
  }

  .thread-pill.clickable {
    cursor: pointer;
  }

  .thread-pill.clickable:hover {
    background: rgba(var(--accent-primary-rgb), 0.16);
    border-color: rgba(var(--accent-primary-rgb), 0.4);
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
    gap: 6px;
    font-size: 11px;
    min-width: 0;
  }

  .summary-label {
    flex-shrink: 0;
    font-size: 9.5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
  }

  .summary-value {
    color: var(--text-secondary);
    font-family:
      ui-monospace,
      'JetBrains Mono',
      'SF Mono',
      Menlo,
      monospace;
    font-size: 10.5px;
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
    gap: 5px;
    font-size: 11px;
    color: var(--text-muted);
    line-height: 1.3;
  }

  .meta-item {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
  }

  .meta-item .num {
    color: var(--text-secondary);
    font-weight: 600;
  }

  .health-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
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
    gap: 5px;
    padding: 6px 8px;
    font-size: 11px;
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
    gap: 10px;
    margin-top: 2px;
    padding-top: 10px;
    border-top: 1px dashed var(--border-subtle, var(--border-default));
    cursor: default;
    animation: detailsIn 0.22s ease-out both;
  }

  @keyframes detailsIn {
    from { opacity: 0; transform: translateY(-2px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .detail-block {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .detail-label {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 9.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
  }

  .template-body {
    margin: 0;
    padding: 8px 10px;
    background: var(--bg-base);
    border: 1px solid var(--border-subtle, var(--border-default));
    border-radius: var(--radius-sm);
    font-family:
      ui-monospace,
      'JetBrains Mono',
      'SF Mono',
      Menlo,
      monospace;
    font-size: 11px;
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
    gap: 4px 10px;
    margin: 0;
    font-size: 11px;
  }

  .config-grid dt {
    color: var(--text-muted);
    font-weight: 500;
    white-space: nowrap;
  }

  .config-grid dd {
    margin: 0;
    color: var(--text-secondary);
    font-family:
      ui-monospace,
      'JetBrains Mono',
      'SF Mono',
      Menlo,
      monospace;
    font-size: 10.5px;
    overflow-wrap: anywhere;
    min-width: 0;
  }

  .conditions-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .conditions-list li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    line-height: 1.3;
  }

  .cond-field,
  .cond-value {
    padding: 1px 5px;
    font-family:
      ui-monospace,
      'JetBrains Mono',
      'SF Mono',
      Menlo,
      monospace;
    font-size: 10.5px;
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
    gap: 4px;
    padding-top: 6px;
    border-top: 1px dashed var(--border-subtle, var(--border-default));
  }

  .spacer {
    flex: 1;
  }

  .ghost-btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px 8px;
    font-size: 11px;
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
    font-size: 11px;
    color: var(--error);
    margin-right: 2px;
  }
</style>
