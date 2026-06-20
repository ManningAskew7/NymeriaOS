<script lang="ts">
  import type { ActivityEntry } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';
  import { formatRelativeTime } from '$lib/utils/time';

  interface Props {
    entry: ActivityEntry;
    threadTitle?: string;
    onNavigate?: () => void;
  }

  let { entry, threadTitle, onNavigate }: Props = $props();

  // --- Type -> icon (centralized; a new activity type inherits the whole row
  // treatment just by adding a case here). The icon carries the event type, so
  // the body text below never repeats it. ---
  let icon = $derived.by(() => {
    switch (entry.type) {
      case 'self_invoke':
        return 'clock';
      case 'watchdog_nudge':
        return 'warning';
      case 'task_completed':
        return 'success';
      case 'task_failed':
        return 'close';
      case 'todo_added':
        return 'plus';
      case 'todo_updated':
        return 'edit';
      case 'todo_completed':
        return 'success';
      case 'todo_deleted':
        return 'trash';
      case 'trigger_completed':
        return 'bolt';
      default:
        return 'info';
    }
  });

  // Colour mapping unchanged from before (status colours plus the mobile feed's
  // accent for self-invoke / trigger runs).
  let color = $derived.by(() => {
    switch (entry.type) {
      case 'task_completed':
      case 'todo_completed':
        return 'var(--success)';
      case 'task_failed':
        return 'var(--error)';
      case 'watchdog_nudge':
        return 'var(--warning)';
      case 'self_invoke':
        return 'var(--accent-primary)';
      case 'trigger_completed':
        return 'var(--accent-secondary, var(--accent-primary))';
      default:
        return 'var(--text-muted)';
    }
  });

  // --- Body: emphasized lead + secondary detail ---
  // Known backend message shapes ("TODO updated: <task> (status: x)",
  // "Scheduled TODO started: <task>", ...) are reshaped into a short
  // emphasized lead label plus the task / result as secondary reference text,
  // so the body never restates the type the icon already shows. Free-form
  // results lift their leading outcome clause into the lead. Anything
  // unrecognised falls back to the raw message as plain detail.
  function stripPrefix(s: string, prefix: string): string {
    const t = s.trimStart();
    return t.toLowerCase().startsWith(prefix.toLowerCase())
      ? t.slice(prefix.length).trimStart()
      : s.trim();
  }

  function afterColon(s: string): string {
    const i = s.indexOf(': ');
    return i === -1 ? s.trim() : s.slice(i + 2).trim();
  }

  // Free-form result: pull a short leading clause (e.g. "No change") into the
  // emphasized lead, leaving the rest as detail. No clear short clause -> the
  // whole result stays as plain detail (the green check already says "done").
  function splitOutcome(msg: string): { lead: string | null; detail: string } {
    const text = (msg ?? '').replace(/\s+/g, ' ').trim();
    if (!text) return { lead: null, detail: '' };
    const m = text.match(/^(.{1,56}?)[.!?:]\s+(.+)$/);
    if (m) return { lead: m[1].trim(), detail: m[2].trim() };
    return { lead: null, detail: text };
  }

  let body = $derived.by((): { lead: string | null; detail: string } => {
    const msg = entry.message ?? '';
    const lower = msg.trimStart().toLowerCase();
    switch (entry.type) {
      case 'todo_added':
        return { lead: 'Added', detail: stripPrefix(msg, 'TODO added:') };
      case 'todo_completed':
        return { lead: 'Completed', detail: stripPrefix(msg, 'TODO completed:') };
      case 'todo_deleted':
        return { lead: 'Deleted', detail: stripPrefix(msg, 'TODO deleted:') };
      case 'todo_updated': {
        const rest = stripPrefix(msg, 'TODO updated:');
        const m = rest.match(/^(.*?)\s*\(status:\s*([^)]+)\)\s*$/);
        if (m) return { lead: `Updated to ${m[2].trim()}`, detail: m[1].trim() };
        return { lead: 'Updated', detail: rest };
      }
      case 'self_invoke': {
        if (lower.startsWith('scheduled todo started:'))
          return { lead: 'Scheduled run started', detail: stripPrefix(msg, 'Scheduled TODO started:') };
        if (lower.startsWith('scheduled handoff'))
          return { lead: 'Scheduled handoff', detail: afterColon(msg) };
        return { lead: 'Run started', detail: msg.trim() };
      }
      case 'watchdog_nudge':
        return { lead: 'Watchdog nudge', detail: afterColon(msg) };
      case 'trigger_completed': {
        // Surface the outcome in the lead (e.g. "Check 46 · No change") so the
        // thing people scan for survives the 2-line clamp instead of being
        // buried in the clamped detail. Pull a short leading clause as the
        // outcome; if the whole remainder is short, lift it entire; only a long
        // remainder stays as secondary detail under the "<check> ·" lead.
        const i = msg.indexOf(': ');
        if (i === -1) return { lead: null, detail: msg.trim() };
        const label = msg.slice(0, i).trim();
        const rest = msg.slice(i + 2).trim();
        const split = splitOutcome(rest);
        if (split.lead) return { lead: `${label} · ${split.lead}`, detail: split.detail };
        if (rest.length <= 56) return { lead: `${label} · ${rest}`, detail: '' };
        return { lead: label, detail: rest };
      }
      case 'task_failed':
      case 'task_completed':
        return splitOutcome(msg);
      default:
        return { lead: null, detail: msg.trim() };
    }
  });

  // Relative "time ago" via the shared formatter (see lib/utils/time.ts).
  let timeAgo = $derived(formatRelativeTime(entry.timestamp));

  // --- Expand / collapse ---
  // Collapsed entries clamp the body to 2 lines for a uniform row height. The
  // chevron + tap-to-expand appear ONLY when the body actually overflows those
  // 2 lines; short entries stay flat and non-interactive. Expansion is sticky:
  // tap (or Enter/Space on the row) again to collapse. The thread badge stays a
  // separate nav control, so navigation and expand don't fight.
  let bodyEl = $state<HTMLElement | null>(null);
  let overflowing = $state(false);
  let expanded = $state(false);

  $effect(() => {
    // Re-measure when the text changes or the element (re)mounts.
    void body;
    const el = bodyEl;
    if (!el) return;
    const measure = () => {
      if (expanded) return; // overflow is only meaningful while clamped
      overflowing = el.scrollHeight - el.clientHeight > 1;
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  });

  function toggle() {
    if (!overflowing) return;
    expanded = !expanded;
  }

  function handleKeydown(e: KeyboardEvent) {
    // Only the row itself toggles; nested controls (the badge) handle their own keys.
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggle();
    }
  }

  function navClick(e: MouseEvent) {
    e.stopPropagation();
    onNavigate?.();
  }
</script>

{#snippet activityContent()}
  <div class="activity-icon" style="color: {color}">
    <Icon name={icon} size={14} />
  </div>

  <div class="activity-content">
    <div class="activity-body" class:clamped={!expanded} bind:this={bodyEl}>
      {#if body.lead}<span class="activity-lead">{body.lead}</span>{#if body.detail}<span class="activity-detail">{' · '}{body.detail}</span>{/if}{:else}{body.detail}{/if}
    </div>

    <div class="activity-meta">
      {#if threadTitle}
        {#if onNavigate}
          <button
            class="thread-badge clickable"
            type="button"
            onclick={navClick}
          >{threadTitle}</button>
        {:else}
          <span class="thread-badge">{threadTitle}</span>
        {/if}
      {/if}
      <span class="activity-time">{timeAgo}</span>
      {#if overflowing}
        <span class="activity-chevron" class:rotated={expanded} aria-hidden="true">
          <Icon name="chevronDown" size={14} />
        </span>
      {/if}
    </div>
  </div>
{/snippet}

<!-- Expandable rows are a role="button" (aria-expanded, Enter/Space) so the
     whole row toggles; short rows that fit in 2 lines render as a plain,
     non-interactive div. Either way the thread badge is the only navigation
     control. -->
{#if overflowing}
  <div
    class="activity-item expandable"
    class:expanded
    role="button"
    tabindex="0"
    aria-expanded={expanded}
    onclick={toggle}
    onkeydown={handleKeydown}
  >
    {@render activityContent()}
  </div>
{:else}
  <div class="activity-item">
    {@render activityContent()}
  </div>
{/if}

<style>
  .activity-item {
    display: flex;
    gap: var(--spacing-sm);
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    min-height: var(--touch-target-min);
    align-items: flex-start;
    /* Reset chrome — the row can be a role="button" element. */
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: left;
  }

  .activity-item.expandable {
    cursor: pointer;
  }

  .activity-item.expandable:active {
    background: var(--bg-hover);
  }

  /* Inset ring: rows are edge-to-edge in the feed, so the app.css outset
     baseline (+2px) is clip-prone. Matches the ThreadItem row treatment. */
  .activity-item.expandable:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .activity-icon {
    flex-shrink: 0;
    margin-top: 3px;
    opacity: 0.8;
  }

  .activity-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  /* Body clamps to 2 lines when collapsed (uniform row height). The native
     -webkit-line-clamp ellipsis inherits this element's colour, so the "…"
     matches the detail text it truncates. */
  .activity-body {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .activity-body.clamped {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .activity-body:not(.clamped) {
    word-break: break-word;
  }

  /* Emphasized lead label; the icon + this label carry the meaning, the rest
     is secondary reference text. */
  .activity-lead {
    font-weight: 600;
    color: var(--text-primary);
  }

  .activity-meta {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    /* Stable row height so the feed rhythm doesn't jump between entry types. The
       thread chip's 1px vertical padding makes it ~2px taller than the bare
       timestamp, so a row that carries a chip reads taller than one that doesn't
       (and only some entry types resolve to a thread). Floor the row to the
       padded-chip height (xs text line box at line-height 1.5, plus 2px padding)
       so every entry's meta row is the same height whether or not it carries a
       chip or the expand chevron. */
    min-height: 20px;
  }

  .thread-badge {
    display: inline-block;
    max-width: 120px;
    padding: 1px 6px;
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--accent-primary);
    background: var(--accent-tint-bg);
    border: 0;
    border-radius: var(--radius-sm);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* When the entry is navigable the badge is the tap target that opens the
     thread (the row tap is reserved for expand/collapse). */
  .thread-badge.clickable {
    cursor: pointer;
    font: inherit;
  }

  .thread-badge.clickable:active {
    filter: brightness(1.1);
  }

  .thread-badge.clickable:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 1px;
  }

  .activity-time {
    /* Push the timestamp to the right of the meta row; the chevron (when the
       entry is expandable) sits just after it at the row's right edge. */
    margin-left: auto;
    flex-shrink: 0;
    white-space: nowrap;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  /* Expand affordance: chevron-down that flips to point up when open, matching
     the trigger-group chevron in this same feed. */
  .activity-chevron {
    display: flex;
    align-items: center;
    flex-shrink: 0;
    color: var(--text-muted);
    transition: transform var(--transition-fast);
  }

  .activity-chevron.rotated {
    transform: rotate(180deg);
  }
</style>
