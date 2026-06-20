<script lang="ts">
  import type { TodoItem as TodoItemType } from '$lib/types';
  import { Icon, KebabMenu } from '$lib/components/common';
  import { tooltipWhenClipped } from '$lib/actions/tooltip';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { onMount, onDestroy } from 'svelte';
  import { slide } from 'svelte/transition';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';

  interface Props {
    todo: TodoItemType;
    highlighted?: boolean;
    onEdit?: (todo: TodoItemType) => void;
    threadTitle?: string;
    onNavigateToThread?: () => void;
  }

  let { todo, highlighted = false, onEdit, threadTitle, onNavigateToThread }: Props = $props();
  let completing = $state(false);

  // Collapsible state - default collapsed, expand to see details
  let expanded = $state(false);

  // Bound to the shared KebabMenu so the row can react to the menu opening
  // (reveal the kebab, fade the disclosure chevron via .menu-open).
  let menuOpen = $state(false);

  // Reactive time for countdown - updates every second when scheduled
  let now = $state(new Date());
  let intervalId: ReturnType<typeof setInterval> | null = null;

  // Start/stop timer based on whether todo is scheduled
  $effect(() => {
    if (todo.scheduledFor) {
      // Start interval if not already running
      if (!intervalId) {
        intervalId = setInterval(() => {
          now = new Date();
        }, 1000);
      }
    } else {
      // Clear interval if todo is no longer scheduled
      if (intervalId) {
        clearInterval(intervalId);
        intervalId = null;
      }
    }
  });

  onDestroy(() => {
    if (intervalId) {
      clearInterval(intervalId);
    }
  });

  let statusIcon = $derived.by(() => {
    switch (todo.status) {
      case 'done':
        return 'check';
      case 'in_progress':
        return 'clock';
      default:
        return 'clock';
    }
  });

  let statusColor = $derived.by(() => {
    switch (todo.status) {
      case 'done':
        return 'var(--success)';
      case 'in_progress':
        return 'var(--accent-primary)';
      default:
        return 'var(--text-muted)';
    }
  });

  // Format scheduled time as countdown - uses 'now' state for live updates
  let scheduledInfo = $derived.by(() => {
    if (!todo.scheduledFor) return null;

    const scheduled = todo.scheduledFor;
    const diff = scheduled.getTime() - now.getTime();

    if (diff <= 0) {
      return 'Due now';
    }

    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);

    if (days > 0) {
      return `${days}d ${hours % 24}h`;
    } else if (hours > 0) {
      return `${hours}h ${minutes % 60}m`;
    } else if (minutes > 0) {
      return `${minutes}m`;
    }
    const seconds = Math.floor(diff / 1000);
    return `${seconds}s`;
  });

  // Check if there are expandable details
  let hasDetails = $derived(!!todo.notes);

  // Check if user-created
  let isUserCreated = $derived(todo.createdBy === 'user');

  // TEMPORARY PREVIEW (no backend `title` field yet): split the task text into a
  // short stand-in title and a body, so the "Title above, task text below" layout
  // can be eyeballed now. Once todos carry a real `title`, this whole block is
  // replaced by `todo.title` (title) + `todo.task` (body).
  let preview = $derived.by(() => {
    const text = todo.task.trim();
    const real = (todo as { title?: string }).title;
    if (real && real.trim()) return { title: real.trim(), body: text };
    // Stand-in: cut at the first natural break (newline, " / ", ": ", "; "), else
    // at ~44 chars on a word boundary; the remainder becomes the body below.
    let idx = -1;
    const m = text.match(/\n|\s\/\s|;\s|:\s/);
    if (m && m.index !== undefined && m.index >= 8) idx = m.index;
    else if (text.length > 48) {
      const cut = text.slice(0, 44);
      const sp = cut.lastIndexOf(' ');
      idx = sp > 20 ? sp : 44;
    }
    if (idx < 0) return { title: text, body: '' };
    return {
      title: text.slice(0, idx).trim(),
      body: text.slice(idx).replace(/^[\s/:;]+/, '').trim(),
    };
  });

  // Recurrence label — mirrors Nymeria.core.todo_constants.format_recurrence_for_display.
  let recurrenceLabel = $derived.by(() => {
    if (!todo.recurrence) return null;
    const legacy: Record<string, string> = {
      '5min': '5m', '10min': '10m', '15min': '15m', '30min': '30m',
      hourly: '1h', daily: '1d', weekly: '1w', monthly: '1mo'
    };
    const canonical = legacy[todo.recurrence.toLowerCase()] ?? todo.recurrence.toLowerCase();
    if (canonical === '1h') return 'Hourly';
    if (canonical === '1d') return 'Daily';
    if (canonical === '1w') return 'Weekly';
    if (canonical === '1mo') return 'Monthly';
    if (/^\d+(mo|s|m|h|d|w)$/.test(canonical)) return `Every ${canonical}`;
    return todo.recurrence;
  });

  function toggleExpand() {
    if (hasDetails) {
      expanded = !expanded;
    }
  }

  async function handleComplete(e: MouseEvent) {
    e.stopPropagation();
    if (todo.status === 'done' || completing) return;

    completing = true;
    try {
      await todosStore.complete(todo.id);
    } catch (err) {
      console.error('Failed to complete todo:', err);
    } finally {
      completing = false;
    }
  }

  // Whole-card click/keyboard toggles expand, but never when the interaction
  // originated on one of the card's own controls (checkbox, kebab, menu item,
  // thread link). Mirrors NotificationItem's guard so the nested buttons keep
  // their own behaviour without double-firing the card.
  function fromControl(e: Event) {
    return !!(e.target as HTMLElement).closest('button, a');
  }

  function handleCardClick(e: MouseEvent) {
    if (fromControl(e)) return;
    toggleExpand();
  }

  function handleCardKeydown(e: KeyboardEvent) {
    if (fromControl(e)) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggleExpand();
    }
  }

  function editTodo() {
    onEdit?.(todo);
  }

  async function deleteTodo() {
    const ok = window.confirm(`Delete "${todo.task}"? This can't be undone.`);
    if (!ok) return;
    try {
      await todosStore.delete(todo.id);
    } catch (err) {
      console.error('Failed to delete todo:', err);
    }
  }

  // Items for the shared kebab menu. Edit is offered only when an onEdit handler
  // is wired; Delete is always present and reads as destructive.
  let menuItems = $derived([
    ...(onEdit ? [{ label: 'Edit', icon: 'edit', onSelect: editTodo }] : []),
    { label: 'Delete', icon: 'trash', onSelect: deleteTodo, destructive: true },
  ]);
</script>

<div
  class="todo-item"
  class:completed={todo.status === 'done'}
  class:scheduled={!!todo.scheduledFor}
  class:highlighted={highlighted}
  class:expandable={hasDetails}
  class:expanded={expanded}
  class:menu-open={menuOpen}
  role="button"
  tabindex="0"
  onclick={handleCardClick}
  onkeydown={handleCardKeydown}
>
  <!-- Complete checkbox -->
  <button
    class="complete-btn"
    class:completing={completing}
    class:done={todo.status === 'done'}
    onclick={handleComplete}
    disabled={todo.status === 'done' || completing}
    type="button"
    data-tooltip={todo.status === 'done' ? 'Completed' : 'Mark as done'}
    aria-label={todo.status === 'done' ? 'Completed' : 'Mark as done'}
  >
    {#if completing}
      <span class="spinner"></span>
    {:else if todo.status === 'done'}
      <Icon name="check" size={12} />
    {:else}
      <span class="checkbox-empty"></span>
    {/if}
  </button>

  <div class="todo-content">
    <div class="todo-header">
      {#if isUserCreated}
        <span class="creator-badge user" data-tooltip="Created by you">
          <Icon name="user" size={10} />
        </span>
      {/if}
      <span class="todo-title" use:tooltipWhenClipped={preview.title}>{preview.title}</span>
    </div>

    {#if preview.body}
      <span class="todo-task">{preview.body}</span>
    {/if}

    {#if threadTitle || recurrenceLabel || scheduledInfo}
      <div class="todo-meta">
        {#if threadTitle}
          {#if onNavigateToThread}
            <button
              class="meta-thread clickable"
              type="button"
              use:tooltipWhenClipped={threadTitle}
              onclick={(e) => { e.stopPropagation(); onNavigateToThread(); }}
            >{threadTitle}</button>
          {:else}
            <span class="meta-thread" use:tooltipWhenClipped={threadTitle}>{threadTitle}</span>
          {/if}
        {/if}
        {#if recurrenceLabel}
          <span class="meta-item">
            <Icon name="refresh" size={10} />
            {recurrenceLabel}
          </span>
        {/if}
        {#if scheduledInfo}
          <span class="meta-item meta-countdown" data-tooltip="Time until activation">
            <Icon name="clock" size={10} />
            {scheduledInfo}
          </span>
        {/if}
      </div>
    {/if}

    <!-- Expandable details — slide-down to match the trigger card animation -->
    {#if expanded}
      <div class="todo-details" transition:slide={DROPDOWN_TRANSITION}>
        {#if todo.notes}
          <div class="detail-row notes">
            <Icon name="fileText" size={12} />
            <span>{todo.notes}</span>
          </div>
        {/if}
      </div>
    {/if}
  </div>

  <!-- Top-right corner: a disclosure chevron at rest (for cards with details)
       that cross-fades to the kebab (⋮) overflow menu on hover/focus. Both
       share one absolutely-positioned cell so the title can use the full width;
       the header reserves a right gutter to clear this cell. -->
  <div class="todo-corner">
    {#if hasDetails}
      <span class="expand-icon" class:rotated={expanded} aria-hidden="true">
        <Icon name="chevronRight" size={12} />
      </span>
    {/if}
    <span class="kebab-slot">
      <KebabMenu items={menuItems} ariaLabel="Task actions" bind:open={menuOpen} />
    </span>
  </div>
</div>

<style>
  .todo-item {
    position: relative;
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    /* 8px side gutter pairs with the section body's 8px; the checkbox's own
       margin (see .complete-btn) then centres it on the header chevron column. */
    padding: var(--spacing-sm-plus) var(--spacing-sm);
    transition: background var(--transition-fast);
    width: 100%;
    text-align: left;
    /* Flat row inside the section card: no per-item background or border. Rows
       are separated by a hairline divider (below) and lifted on hover; the
       hover fill is rounded (radius-sm) to match the Activity items. */
    border-radius: var(--radius-sm);
    background: transparent;
    cursor: default;
    font-family: inherit;
    animation: staggerFadeIn var(--transition-slow) backwards;
  }

  /* Hairline divider between consecutive rows (none above the first). The
     todo-items are the leading siblings in each .group-items list, so
     :first-child reliably matches the top row. */
  .todo-item:not(:first-child) {
    border-top: 1px solid var(--border-subtle);
  }

  .todo-item:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
  }

  .todo-item:nth-child(1) { animation-delay: 0.03s; }
  .todo-item:nth-child(2) { animation-delay: 0.06s; }
  .todo-item:nth-child(3) { animation-delay: 0.09s; }
  .todo-item:nth-child(4) { animation-delay: 0.12s; }
  .todo-item:nth-child(5) { animation-delay: 0.15s; }
  .todo-item:nth-child(6) { animation-delay: 0.18s; }
  .todo-item:nth-child(7) { animation-delay: 0.21s; }
  .todo-item:nth-child(8) { animation-delay: 0.24s; }

  .todo-item.expandable {
    cursor: pointer;
  }

  .todo-item:hover {
    background: var(--bg-hover);
  }

  /* Reveal the kebab (and hide the resting chevron) when the row is hovered,
     when the kebab itself is focused, or while its menu is open. */
  .todo-item:hover .kebab-slot,
  .todo-corner:focus-within .kebab-slot,
  .todo-item.menu-open .kebab-slot {
    opacity: 1;
  }

  .todo-item:hover .expand-icon,
  .todo-corner:focus-within .expand-icon,
  .todo-item.menu-open .expand-icon {
    opacity: 0;
  }

  .todo-item.completed {
    opacity: 0.55;
  }

  .todo-item.completed .todo-title {
    text-decoration: line-through;
    text-decoration-color: var(--text-muted);
    text-decoration-thickness: 1px;
  }

  /* In-progress and scheduled tasks no longer carry a per-item highlight or a
     left accent rail — flattened to plain rows so the section card stays the
     only container. In-progress tasks sit under their own "In Progress" group
     label; scheduled tasks still show their countdown in the meta row. */

  .complete-btn {
    flex-shrink: 0;
    width: 13px;
    height: 13px;
    border-radius: var(--radius-sm);
    border: 1.5px solid var(--border-default);
    background: transparent;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: all var(--transition-fast);
    margin-top: 2px;
    /* Nudge right so the 13px checkbox's centre lands on the section chevron's
       column, level with the trigger badges and activity icons. */
    margin-left: 1.5px;
    padding: 0;
  }

  .complete-btn:hover:not(:disabled) {
    border-color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.12);
  }

  .complete-btn.done {
    border-color: var(--success);
    background: var(--success);
    color: white;
    animation: checkBounce var(--transition-slow);
  }

  .complete-btn:disabled {
    cursor: default;
  }

  .complete-btn .checkbox-empty {
    width: 100%;
    height: 100%;
  }

  .complete-btn .spinner {
    width: 10px;
    height: 10px;
    border: 2px solid transparent;
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  .todo-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .todo-header {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-xs);
    min-width: 0;
    /* Clear the absolute corner cell (chevron/kebab) in the top-right so a
       two-line title never runs underneath it. */
    padding-right: 26px;
  }

  .creator-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    border-radius: var(--radius-full);
    flex-shrink: 0;
    color: var(--accent-primary);
    opacity: 0.7;
    /* Align with the first line of the (now top-aligned) title. */
    margin-top: 2px;
  }

  .todo-title {
    /* The task's name: bold (weight 500), primary ink, sized like the activity
       feed message (xs). Wraps to at most two lines, then ellipsises. */
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--text-primary);
    line-height: 1.35;
    letter-spacing: -0.005em;
    flex: 1;
    min-width: 0;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .todo-task {
    /* The task text, sitting under the title as a quieter body line: regular
       weight and secondary ink so the title reads as the heading. Clamps to two
       lines, then ellipsises. */
    font-size: var(--font-size-xs);
    font-weight: 400;
    color: var(--text-secondary);
    line-height: 1.4;
    min-width: 0;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }


  /* Quiet secondary metadata row — the key to the professional look. The items
     (thread, recurrence, countdown) are distributed across the full width with
     space-between, so they spread out evenly and the countdown reaches the
     right edge instead of bunching at the left. gap is the minimum spacing for
     when the row is too narrow to spread. */
  .todo-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    line-height: 1.5;
    /* Tighten only the space above the meta row (leaving the 6px title/body
       rhythm of .todo-content intact) so the text-to-"thread" gap matches the
       activity feed's. The activity meta row has min-height:18px, which adds
       ~2.8px of internal space above its label, so its 1px content gap renders
       as ~3.8px; this meta row has no such floor, so netting the 6px gap down to
       ~2.6px lands at the same ~3.8px visual gap. */
    margin-top: -3.4px;
  }

  .meta-thread {
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--text-secondary);
    font: inherit;
    font-weight: 500;
    max-width: 140px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex-shrink: 1;
    min-width: 0;
    transition: color var(--transition-fast);
  }

  .meta-thread.clickable {
    cursor: pointer;
  }

  .meta-thread.clickable:hover {
    color: var(--accent-primary);
  }

  .meta-item {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    flex-shrink: 0;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }

  .meta-countdown {
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .highlighted .meta-countdown {
    color: var(--accent-primary);
  }

  /* Top-right corner cell shared by the disclosure chevron and the kebab; both
     children are stacked (absolute, centred) and cross-fade via opacity so the
     control swap stays in one fixed spot. */
  .todo-corner {
    position: absolute;
    /* Tucked into the box's top-right corner with equal 8px gaps to the top and
       right edges. The cell is square and its glyph is centred, so the visible
       chevron/kebab then sits the same distance in from each edge. */
    top: var(--spacing-sm);
    right: var(--spacing-sm);
    width: 22px;
    height: 22px;
    flex-shrink: 0;
  }

  .expand-icon,
  .kebab-slot {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .expand-icon {
    color: var(--text-muted);
    transition: transform 120ms var(--ease-out), opacity var(--transition-fast);
  }

  .expand-icon.rotated {
    transform: rotate(90deg);
  }

  /* Hidden until revealed (see the hover/focus/menu-open rules above); the kebab
     control itself (styling, hover, press) and its dropdown live in KebabMenu. */
  .kebab-slot {
    opacity: 0;
    transition: opacity var(--transition-fast);
  }

  .todo-details {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    padding-top: var(--spacing-xs);
    border-top: 1px solid var(--border-subtle);
    margin-top: 4px;
  }

  .detail-row {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-xs);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.45;
  }

  .detail-row.notes span {
    white-space: pre-wrap;
    word-break: break-word;
  }
</style>
