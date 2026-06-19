<script lang="ts">
  import type { ActivityEntry } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { tooltipWhenClipped } from '$lib/actions/tooltip';
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

  // Icon colour is reserved for genuine status: green = completed, red =
  // failed, amber = needs attention. Every other event type (self-invoke,
  // trigger run, todo add/edit/delete) is a neutral muted gray, so the feed
  // reads as one calm column instead of a rainbow. The event is still named by
  // its icon shape and body text. (Colour mapping unchanged from before.)
  let color = $derived.by(() => {
    switch (entry.type) {
      case 'task_completed':
      case 'todo_completed':
        return 'var(--success)';
      case 'task_failed':
        return 'var(--error)';
      case 'watchdog_nudge':
        return 'var(--warning)';
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
  // chevron + click-to-expand appear ONLY when the body actually overflows
  // those 2 lines; short entries stay flat and non-interactive. Expansion is
  // sticky: click (or Enter/Space on the row) again to collapse. The thread
  // badge stays a separate nav control, so navigation and expand don't fight.
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
  <div
    class="activity-icon"
    class:clock-icon={icon === 'clock'}
    class:success-icon={icon === 'success'}
    style="color: {color}"
  >
    <Icon name={icon} size={12} />
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
            use:tooltipWhenClipped={threadTitle}
          >{threadTitle}</button>
        {:else}
          <span class="thread-badge" use:tooltipWhenClipped={threadTitle}>{threadTitle}</span>
        {/if}
      {/if}
      <span class="activity-time">{timeAgo}</span>
      {#if overflowing}
        <span class="activity-chevron" class:rotated={expanded} aria-hidden="true">
          <Icon name="chevronDown" size={12} />
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
    padding: var(--spacing-xs) var(--spacing-sm);
    border: 0;
    border-radius: var(--radius-sm);
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: left;
    transition: background var(--transition-fast);
    animation: staggerFadeIn var(--transition-slow) backwards;
  }

  .activity-item:nth-child(1) { animation-delay: 0.03s; }
  .activity-item:nth-child(2) { animation-delay: 0.06s; }
  .activity-item:nth-child(3) { animation-delay: 0.09s; }
  .activity-item:nth-child(4) { animation-delay: 0.12s; }
  .activity-item:nth-child(5) { animation-delay: 0.15s; }
  .activity-item:nth-child(6) { animation-delay: 0.18s; }
  .activity-item:nth-child(7) { animation-delay: 0.21s; }
  .activity-item:nth-child(8) { animation-delay: 0.24s; }
  .activity-item:nth-child(9) { animation-delay: 0.27s; }
  .activity-item:nth-child(10) { animation-delay: 0.3s; }

  .activity-item:hover {
    background: var(--bg-hover);
  }

  .activity-item.expandable {
    cursor: pointer;
  }

  /* Inset focus ring keeps the cue inside the row edges so it doesn't overlap
     the row dividers (matches the Trigger/Thread list-row treatment). */
  .activity-item.expandable:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .activity-icon {
    flex-shrink: 0;
    margin-top: -3px;
    /* Nudge right so the 12px icon's centre lands on the section chevron's
       column, level with the task checkboxes and trigger badges. */
    margin-left: 2px;
    opacity: 0.8;
  }

  .activity-icon.clock-icon {
    margin-top: -3px;
  }

  .activity-icon.success-icon {
    margin-top: -3px;
  }

  .activity-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  /* Body clamps to 2 lines when collapsed (uniform row height). The native
     -webkit-line-clamp ellipsis inherits this element's colour, so the "…"
     matches the detail text it truncates. */
  .activity-body {
    font-size: var(--font-size-xs);
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
  }

  .thread-badge {
    display: inline-block;
    max-width: 120px;
    padding: 1px 6px;
    font-size: var(--font-size-3xs);
    font-weight: 500;
    /* Neutral chip: a thread name is a label, not an active/selected state, so
       it sits on the muted gray scale rather than the accent tint. */
    color: var(--text-secondary);
    background: var(--bg-elevated);
    border: 0;
    border-radius: var(--radius-sm);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* When the entry is navigable the badge is the click target that opens the
     thread (the row click is reserved for expand/collapse). */
  .thread-badge.clickable {
    cursor: pointer;
    font: inherit;
    transition: background var(--transition-fast), color var(--transition-fast);
  }

  .thread-badge.clickable:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
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
    font-size: var(--font-size-3xs);
    color: var(--text-muted);
  }

  /* Expand affordance: chevron-down that flips to point up when open, signalling
     inline expand (not navigation), matching the trigger-group chevron in this
     same feed. */
  .activity-chevron {
    display: flex;
    align-items: center;
    flex-shrink: 0;
    color: var(--text-muted);
    transition: transform 120ms var(--ease-out);
  }

  .activity-chevron.rotated {
    transform: rotate(180deg);
  }
</style>
