<script lang="ts">
  import type { Hook, HookCondition, HookExecution, HookExecutionStatus } from '$lib/types';
  import { slide } from 'svelte/transition';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import { Icon, ToggleSwitch } from '$lib/components/common';
  import { tooltipWhenClipped } from '$lib/actions/tooltip';
  import { hooksStore } from '$lib/stores/hooks.svelte';
  import {
    hookCategory,
    describeHookLogic,
    hookActionMeta,
    hookEventMeta,
  } from '$lib/utils/hooks';
  import { formatRelativeTime } from '$lib/utils/time';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';

  interface Props {
    hook: Hook;
    threadTitle?: string;
    onNavigateToThread?: () => void;
    onEdit?: (hook: Hook) => void;
    animationDelay?: number;
  }

  let { hook, threadTitle, onNavigateToThread, onEdit, animationDelay = 0 }: Props = $props();

  let expanded = $state(false);
  let toggling = $state(false);
  let confirmDelete = $state(false);
  let testResult = $state<string | null>(null);
  let testFailed = $state(false);
  let testing = $state(false);
  let actionError = $state<string | null>(null);
  let showLog = $state(false);
  let logLoading = $state(false);
  let logEntries = $state<HookExecution[] | null>(null);

  const category = $derived(hookCategory(hook.action));
  // Accessor form, NOT the raw tables: the backend synthesizes the reserved
  // turn-metadata system hook (action outside the authorable union) into every
  // GET /hooks, and a raw table miss here crashed the whole app shell.
  const actionMeta = $derived(hookActionMeta(hook.action));
  const eventMeta = $derived(hookEventMeta(hook.event));
  const summary = $derived(describeHookLogic(hook));
  const conditions = $derived<HookCondition[]>(
    Array.isArray(hook.logic?.conditions) ? (hook.logic.conditions as HookCondition[]) : []
  );
  // Built-in system definition: delete resets it to defaults instead of
  // removing it, so the destructive affordance is a Reset. The action check
  // covers hook objects cached before `system` rode the wire.
  const isSystem = $derived(hook.system || (hook.action as string) === 'turn_metadata');

  /** Status presentation for one execution-log entry. */
  const LOG_STATUS_META: Record<HookExecutionStatus, { label: string; tone: string }> = {
    ok: { label: 'ok', tone: 'ok' },
    no_op: { label: 'no effect', tone: 'muted' },
    error: { label: 'error', tone: 'error' },
    timeout: { label: 'timeout', tone: 'error' },
    saturated: { label: 'saturated', tone: 'warn' },
    illegal: { label: 'illegal', tone: 'warn' },
  };

  function logStatusMeta(status: string): { label: string; tone: string } {
    return LOG_STATUS_META[status as HookExecutionStatus] ?? { label: status, tone: 'muted' };
  }

  function formatDuration(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds <= 0) return '';
    return seconds < 1 ? `${Math.round(seconds * 1000)}ms` : `${seconds.toFixed(1)}s`;
  }

  function relativeTime(iso: string): string {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? '' : formatRelativeTime(d);
  }

  function truncate(s: string, n: number): string {
    if (!s) return '';
    const clean = s.replace(/\s+/g, ' ').trim();
    return clean.length > n ? clean.slice(0, n).trimEnd() + '…' : clean;
  }

  function toggleExpand() {
    expanded = !expanded;
    if (!expanded) showLog = false;
  }

  async function handleToggle() {
    toggling = true;
    actionError = null;
    try {
      await hooksStore.updateHook(hook.id, { enabled: !hook.enabled });
    } catch (err) {
      actionError = humanizeErrorText(err, {
        action: hook.enabled ? 'disable' : 'enable',
        resource: 'the hook',
      });
      setTimeout(() => (actionError = null), 6000);
    } finally {
      toggling = false;
    }
  }

  async function handleDelete() {
    actionError = null;
    try {
      await hooksStore.deleteHook(hook.id);
      confirmDelete = false;
    } catch (err) {
      actionError = humanizeErrorText(err, {
        action: isSystem ? 'reset' : 'delete',
        resource: 'the hook',
      });
      setTimeout(() => (actionError = null), 6000);
    }
  }

  async function handleTest() {
    testing = true;
    testResult = null;
    testFailed = false;
    try {
      const result = await hooksStore.testHook(hook.id);
      testResult = truncate(result.rendered ?? '(no preview)', 220);
      if (!expanded) expanded = true;
      setTimeout(() => (testResult = null), 8000);
    } catch (err) {
      testFailed = true;
      testResult = humanizeErrorText(err, { action: 'test', resource: 'the hook' });
      setTimeout(() => (testResult = null), 8000);
    } finally {
      testing = false;
    }
  }

  async function handleLogToggle() {
    if (showLog) {
      showLog = false;
      return;
    }
    showLog = true;
    logLoading = true;
    try {
      logEntries = await hooksStore.getExecutions(hook.id, 10);
    } catch (err) {
      actionError = humanizeErrorText(err, { action: 'load', resource: 'the hook log' });
      setTimeout(() => (actionError = null), 6000);
      showLog = false;
    } finally {
      logLoading = false;
    }
  }

  function handleThreadClick() {
    onNavigateToThread?.();
  }
</script>

<!-- Plain container (was role="button"): a card with focusable children
     (toggle, action buttons) cannot itself be a button without invalid ARIA
     nesting. The header title area is the real disclosure control. -->
<div
  class="hook-card cat-{category}"
  class:disabled={!hook.enabled}
  style="animation-delay: {animationDelay}ms"
>
  <div class="card-header">
    <button
      class="header-main"
      type="button"
      aria-expanded={expanded}
      aria-label={expanded ? `Collapse ${hook.name}` : `Expand ${hook.name}`}
      onclick={toggleExpand}
    >
      <div class="cat-badge" data-category={category}>
        <Icon name={actionMeta.icon} size={14} />
      </div>

      <div class="title-col">
        <span class="hook-name" use:tooltipWhenClipped={hook.name}>{hook.name}</span>
        <div class="chip-row">
          <span class="chip event-chip">{eventMeta.label}</span>
          <span class="chip action-chip" data-category={category}>{actionMeta.label}</span>
          {#if isSystem}
            <span class="chip system-chip">Built-in</span>
          {:else if hook.scope === 'global'}
            <span class="chip scope-chip">Global</span>
          {/if}
        </div>
      </div>
    </button>

    <div class="header-actions">
      <ToggleSwitch
        checked={hook.enabled}
        disabled={toggling}
        onclick={handleToggle}
        ariaLabel={hook.enabled ? 'Disable hook' : 'Enable hook'}
        size="sm"
        variant="outlined"
      />
      <button
        class="expand-btn"
        class:rotated={expanded}
        type="button"
        tabindex="-1"
        aria-label={expanded ? 'Collapse hook details' : 'Expand hook details'}
        aria-expanded={expanded}
        onclick={toggleExpand}
      >
        <Icon name="chevronRight" size={12} />
      </button>
    </div>
  </div>

  {#if actionError}
    <div class="action-error" role="alert">
      <Icon name="warning" size={11} />
      <span>{actionError}</span>
    </div>
  {/if}

  {#if expanded}
    <div class="expanded-wrap" transition:slide={DROPDOWN_TRANSITION}>
      {#if threadTitle}
        {#if onNavigateToThread}
          <button class="thread-pill clickable" onclick={handleThreadClick} type="button">
            <Icon name="chat" size={10} />
            <span class="thread-name" use:tooltipWhenClipped={threadTitle}>{threadTitle}</span>
            <Icon name="chevronRight" size={10} />
          </button>
        {:else}
          <span class="thread-pill">
            <Icon name="chat" size={10} />
            <span class="thread-name" use:tooltipWhenClipped={threadTitle}>{threadTitle}</span>
          </span>
        {/if}
      {/if}

      <div class="detail-block">
        <div class="detail-label">
          <Icon name={actionMeta.icon} size={11} />
          <span>What it does</span>
        </div>
        <p class="summary-body">{summary}</p>
      </div>

      {#if hook.matcher}
        <div class="kv-row">
          <span class="kv-key">Tool matcher</span>
          <code class="kv-value">{hook.matcher}</code>
        </div>
      {/if}

      {#if conditions.length > 0}
        <div class="detail-block">
          <div class="detail-label">
            <Icon name="sort" size={11} />
            <span>Conditions ({conditions.length})</span>
          </div>
          <ul class="conditions-list">
            {#each conditions as cond, i (i)}
              <li>
                <code class="cond-field">{cond.field}</code>
                <span class="cond-op">{cond.operator.replace(/_/g, ' ')}</span>
                <code class="cond-value">{cond.value}</code>
              </li>
            {/each}
          </ul>
        </div>
      {/if}

      {#if testResult !== null}
        <div class="test-banner" class:failed={testFailed} role="status">
          <Icon name={testFailed ? 'warning' : 'terminal'} size={11} />
          <span>{testResult}</span>
        </div>
      {/if}

      {#if showLog}
        <div class="log-block" transition:slide={DROPDOWN_TRANSITION} role="status">
          <div class="detail-label">
            <Icon name="clock" size={11} />
            <span>Recent runs</span>
          </div>
          {#if logLoading}
            <p class="log-note">Loading runs…</p>
          {:else if !logEntries || logEntries.length === 0}
            <p class="log-note">No runs recorded yet. This hook has not fired.</p>
          {:else}
            <ul class="log-list">
              {#each logEntries as entry (entry.id)}
                {@const status = logStatusMeta(entry.status)}
                <li class="log-row">
                  <span class="log-status tone-{status.tone}">{status.label}</span>
                  <span class="log-when">{relativeTime(entry.timestamp)}</span>
                  {#if formatDuration(entry.duration_seconds)}
                    <span class="log-duration">{formatDuration(entry.duration_seconds)}</span>
                  {/if}
                  {#if entry.tool_name}
                    <code class="log-tool">{entry.tool_name}</code>
                  {/if}
                  {#if entry.detail}
                    <span class="log-detail" use:tooltipWhenClipped={entry.detail}>
                      {truncate(entry.detail, 120)}
                    </span>
                  {/if}
                </li>
              {/each}
            </ul>
          {/if}
        </div>
      {/if}

      <div class="action-bar">
        <button class="ghost-btn" onclick={handleTest} disabled={testing} type="button">
          <Icon name="terminal" size={12} />
          <span>{testing ? 'Testing…' : 'Test'}</span>
        </button>
        {#if onEdit}
          <button class="ghost-btn" onclick={() => onEdit?.(hook)} type="button">
            <Icon name="edit" size={12} />
            <span>Edit</span>
          </button>
        {/if}
        <button
          class="ghost-btn"
          class:pressed={showLog}
          onclick={handleLogToggle}
          disabled={logLoading}
          type="button"
          aria-expanded={showLog}
        >
          <Icon name="clock" size={12} />
          <span>Log</span>
        </button>
        <div class="spacer"></div>
        {#if !confirmDelete}
          <button
            class="ghost-btn danger"
            onclick={() => (confirmDelete = true)}
            type="button"
            aria-label={isSystem ? 'Reset hook to defaults' : 'Delete hook'}
          >
            <Icon name={isSystem ? 'refresh' : 'trash'} size={12} />
            {#if isSystem}<span>Reset</span>{/if}
          </button>
        {:else}
          <span class="confirm-label">{isSystem ? 'Reset to defaults?' : 'Delete?'}</span>
          <button
            class="ghost-btn danger solid"
            onclick={handleDelete}
            type="button"
            aria-label={isSystem ? 'Confirm reset' : 'Confirm delete'}
          >
            <Icon name="check" size={12} />
          </button>
          <button
            class="ghost-btn"
            onclick={() => (confirmDelete = false)}
            type="button"
            aria-label="Cancel"
          >
            <Icon name="x" size={12} />
          </button>
        {/if}
      </div>
    </div>
  {/if}
</div>

<style>
  .hook-card {
    /* Per-category accent, consumed by the badge + chips below. */
    --cat-color: var(--text-muted);
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    border-radius: var(--radius-sm);
    padding: var(--spacing-sm-plus) var(--spacing-sm);
    background: transparent;
    animation: cardIn var(--transition-normal) both;
    transition: background var(--transition-fast);
  }

  .hook-card.cat-guardrails { --cat-color: var(--warning); }
  .hook-card.cat-context { --cat-color: var(--accent-primary); }
  .hook-card.cat-reactions { --cat-color: var(--info, var(--accent-secondary, var(--text-secondary))); }
  .hook-card.cat-commands { --cat-color: var(--accent-secondary, var(--text-secondary)); }

  .hook-card:not(:first-child) {
    border-top: 1px solid var(--border-subtle);
  }

  @keyframes cardIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  /* Hover tint only while the pointer is over a disclosure control (title
     button or chevron): the full card lighting up used to promise whole-card
     clickability, which the a11y restructure removed. (:has is fine in
     WebView2; without it the tint is simply absent, cosmetic only.) */
  .hook-card:has(.header-main:hover),
  .hook-card:has(.expand-btn:hover) {
    background: var(--bg-hover);
  }

  .hook-card.disabled {
    opacity: 0.62;
  }

  .card-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    min-width: 0;
  }

  /* The real disclosure control: badge + name + chips, keyboard-operable as
     one button (Enter/Space native), replacing the old whole-card
     role="button" that illegally nested the toggle and action buttons. */
  .header-main {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 0;
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .header-main:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
    border-radius: var(--radius-sm);
  }

  .cat-badge {
    flex-shrink: 0;
    width: 24px;
    height: 24px;
    margin-left: -4px;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: color-mix(in srgb, var(--cat-color) 16%, transparent);
    border: 1px solid color-mix(in srgb, var(--cat-color) 30%, transparent);
    color: var(--cat-color);
  }

  .cat-badge :global(svg) {
    width: 12px;
    height: 12px;
  }

  .disabled .cat-badge {
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

  .hook-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    letter-spacing: -0.005em;
    line-height: 1.35;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
    max-width: 100%;
  }

  .chip-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    flex-wrap: wrap;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    font-size: var(--font-size-3xs);
    font-weight: 500;
    border-radius: var(--radius-md);
    white-space: nowrap;
    line-height: 1.5;
  }

  .event-chip {
    color: var(--text-muted);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
  }

  .action-chip {
    color: var(--cat-color);
    background: color-mix(in srgb, var(--cat-color) 12%, transparent);
  }

  .scope-chip {
    color: var(--text-secondary);
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
  }

  /* Built-in system definition (replaces the Global chip: built-in implies
     every thread). Outlined, not filled: it marks provenance, not category. */
  .system-chip {
    color: var(--text-secondary);
    background: transparent;
    border: 1px dashed color-mix(in srgb, var(--cat-color) 45%, transparent);
  }

  .header-actions {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  /* Secondary pointer target for expand/collapse. The header title button is
     the primary, keyboard-reachable control; this one sits at tabindex="-1"
     so keyboard users get one stop per card, while pointer users keep the
     conventional chevron affordance. */
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
    transition: transform 120ms var(--ease-out);
    cursor: pointer;
  }

  .expand-btn:hover {
    background: var(--bg-elevated-2);
    color: var(--text-primary);
  }

  .expand-btn.rotated {
    transform: rotate(90deg);
  }

  .expanded-wrap {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    min-width: 0;
    cursor: default;
  }

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

  .thread-pill.clickable { cursor: pointer; }

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
    max-width: 200px;
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
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .detail-label :global(svg) {
    color: var(--cat-color);
  }

  .summary-body {
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

  .kv-row {
    display: flex;
    align-items: baseline;
    gap: var(--spacing-sm);
    font-size: var(--font-size-2xs);
    min-width: 0;
  }

  .kv-key {
    flex-shrink: 0;
    font-size: var(--font-size-3xs);
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .kv-value {
    color: var(--text-secondary);
    font-family: var(--font-mono);
    font-size: var(--font-size-2xs);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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

  .test-banner {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm) var(--spacing-sm);
    font-size: var(--font-size-2xs);
    line-height: 1.35;
    border-radius: var(--radius-sm);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 30%, transparent);
    color: var(--text-secondary);
    background: color-mix(in srgb, var(--accent-primary) 8%, transparent);
  }

  .test-banner span {
    word-break: break-word;
    flex: 1;
    min-width: 0;
  }

  /* A failed dry run must not wear the success styling. */
  .test-banner.failed {
    border-color: color-mix(in srgb, var(--error) 35%, transparent);
    background: color-mix(in srgb, var(--error) 8%, transparent);
    color: color-mix(in srgb, var(--error) 85%, var(--text-secondary));
  }

  /* Recent-runs log (fetched on demand via the Log button). */
  .log-block {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    min-width: 0;
  }

  .log-note {
    margin: 0;
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
  }

  .log-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-2xs);
    max-height: 180px;
    overflow-y: auto;
  }

  .log-row {
    display: flex;
    align-items: baseline;
    gap: var(--spacing-sm);
    font-size: var(--font-size-2xs);
    line-height: 1.4;
    min-width: 0;
  }

  .log-status {
    flex-shrink: 0;
    min-width: 52px;
    font-size: var(--font-size-3xs);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .log-status.tone-ok { color: var(--success); }
  .log-status.tone-muted { color: var(--text-muted); }
  .log-status.tone-error { color: var(--error); }
  .log-status.tone-warn { color: var(--warning); }

  .log-when,
  .log-duration {
    flex-shrink: 0;
    color: var(--text-muted);
  }

  .log-tool {
    flex-shrink: 0;
    font-family: var(--font-mono);
    color: var(--text-secondary);
  }

  .log-detail {
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }

  .action-error {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-2xs);
    line-height: 1.35;
    border-radius: var(--radius-sm);
    color: var(--error);
    background: color-mix(in srgb, var(--error) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--error) 30%, transparent);
  }

  .action-error span {
    word-break: break-word;
    flex: 1;
    min-width: 0;
  }

  .action-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--spacing-xs);
    padding-top: var(--spacing-sm);
    border-top: 1px dashed var(--border-subtle, var(--border-default));
  }

  .spacer { flex: 1; }

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

  .ghost-btn.pressed {
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    border-color: var(--border-default);
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
    /* bg-base, not white: the light themes' dark-red error passes with a
       near-white glyph AND the dark themes' light-red error passes with a
       dark glyph, where white-on-light-red fell under 3:1. */
    color: var(--bg-base);
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
