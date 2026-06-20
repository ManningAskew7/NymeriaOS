<script lang="ts">
  import { fly } from 'svelte/transition';
  import { TAB_FADE } from '$lib/utils/transitions';
  import { SectionHeader } from '$lib/components/common';
  import TodoFeed from '$lib/components/todos/TodoFeed.svelte';
  import TodoForm from '$lib/components/todos/TodoForm.svelte';
  import TriggerFeed from '$lib/components/triggers/TriggerFeed.svelte';
  import TriggerSetupWizard from '$lib/components/triggers/TriggerSetupWizard.svelte';
  import { ActivityFeed, ConnectionStatus } from '$lib/components/dashboard';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { switchToThread } from '$lib/stores/navigation.svelte';
  import { uiStore } from '$lib/stores/ui.svelte';

  let isCollapsed = $derived(uiStore.rightPanelCollapsed);

  let activeTab = $state<'thread' | 'global'>('thread');
  let currentThreadId = $derived(threadsStore.currentThreadId);

  // Create flows are launched from the always-visible section headers, so the
  // modals live here at the panel level rather than inside the collapsible
  // bodies (which unmount when a section is collapsed). Item edits keep their
  // own modal inside each feed.
  let showTaskCreate = $state(false);
  let showTriggerCreate = $state(false);

  // True for the brief window when switching INTO the 'thread' tab. Hides
  // scrollbars on the whole panel during that window so they don't flash /
  // shorten / overlap during the fade.
  let suppressScrollbar = $state(false);
  let suppressTimer: ReturnType<typeof setTimeout> | null = null;

  $effect(() => {
    void activeTab;
    if (activeTab === 'thread') {
      suppressScrollbar = true;
      if (suppressTimer) clearTimeout(suppressTimer);
      // Slightly longer than TAB_FADE's 160ms to be safe.
      suppressTimer = setTimeout(() => (suppressScrollbar = false), 220);
    }
  });

  // Auto-switch to Global tab when no thread is selected
  $effect(() => {
    if (activeTab === 'thread' && !currentThreadId) {
      activeTab = 'global';
    }
  });

  // Arrow keys switch dashboard panes, but only when a tab button itself is
  // focused (the standard ARIA tablist pattern). Tab is deliberately left
  // untouched so it moves focus through the page normally; an earlier
  // window-level Tab hijack here froze keyboard navigation whenever the
  // panel was open.
  function handleTablistKeydown(e: KeyboardEvent) {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    // "This Thread" is only meaningful when a thread is selected; without one
    // the panel is pinned to Global (see the auto-switch effect above), so the
    // arrow is a no-op rather than bouncing selection back and forth.
    if (!currentThreadId) return;
    e.preventDefault();
    const next = e.key === 'ArrowRight' ? 'global' : 'thread';
    activeTab = next;
    // Move focus to follow the new selection (automatic activation).
    requestAnimationFrame(() => {
      document.getElementById(`dashboard-tab-${next}`)?.focus();
    });
  }

  // Build thread title lookup for global view
  let threadTitleMap = $derived(
    Object.fromEntries(threadsStore.threads.map(t => [t.id, t.title]))
  );

  // Navigate to a thread (for clicking activity/todo items in global view)
  async function navigateToThread(threadId: string) {
    activeTab = 'thread';
    const title = threadTitleMap[threadId] || threadId;
    await switchToThread(threadId, { ensureTitle: title });
  }
</script>

<div class="right-panel-content" class:collapsed={isCollapsed} aria-hidden={isCollapsed}>
  <header class="panel-header">
    <h2 class="panel-title">Dashboard</h2>
    <div class="segmented" role="tablist" aria-label="Dashboard view">
      <button
        id="dashboard-tab-thread"
        class="segment"
        class:active={activeTab === 'thread'}
        onclick={() => (activeTab = 'thread')}
        onkeydown={handleTablistKeydown}
        type="button"
        role="tab"
        aria-selected={activeTab === 'thread'}
        aria-controls="dashboard-tabpanel"
      >
        This Thread
      </button>
      <button
        id="dashboard-tab-global"
        class="segment"
        class:active={activeTab === 'global'}
        onclick={() => (activeTab = 'global')}
        onkeydown={handleTablistKeydown}
        type="button"
        role="tab"
        aria-selected={activeTab === 'global'}
        aria-controls="dashboard-tabpanel"
      >
        Global
      </button>
    </div>
  </header>

  <div class="panel-body" class:suppress-scrollbar={suppressScrollbar}>
    {#key activeTab}
    <div
      class="dashboard-sections"
      id="dashboard-tabpanel"
      role="tabpanel"
      aria-labelledby={`dashboard-tab-${activeTab}`}
      tabindex="0"
      in:fly={TAB_FADE}
      out:fly={TAB_FADE}
    >
      <!-- Tasks -->
      <SectionHeader
        title="Tasks"
        count={todosStore.todos.length}
        action={{ label: 'New task', onClick: () => (showTaskCreate = true) }}
      >
        {#if activeTab === 'thread' && currentThreadId}
          <TodoFeed threadId={currentThreadId} />
        {:else}
          <TodoFeed {threadTitleMap} onNavigateToThread={navigateToThread} />
        {/if}
      </SectionHeader>

      <!-- Triggers -->
      <SectionHeader
        title="Triggers"
        count={triggersStore.enabledCount}
        action={{ label: 'New trigger', onClick: () => (showTriggerCreate = true) }}
      >
        {#if activeTab === 'thread' && currentThreadId}
          <TriggerFeed threadId={currentThreadId} />
        {:else}
          <TriggerFeed {threadTitleMap} onNavigateToThread={navigateToThread} />
        {/if}
      </SectionHeader>

      <!-- Activity (no create action; same chevron + title chrome) -->
      <SectionHeader title="Activity">
        {#if activeTab === 'thread' && currentThreadId}
          <ActivityFeed threadId={currentThreadId} />
        {:else}
          <ActivityFeed {threadTitleMap} onNavigateToThread={navigateToThread} />
        {/if}
      </SectionHeader>
    </div>
    {/key}
  </div>
  <ConnectionStatus />
</div>

<!-- Create flows live at the panel level so the section headers' "New task" /
     "New trigger" links work even when a section is collapsed. -->
<TodoForm isOpen={showTaskCreate} onClose={() => (showTaskCreate = false)} editTodo={null} />
{#if showTriggerCreate}
  <TriggerSetupWizard
    threadId={activeTab === 'thread' && currentThreadId ? currentThreadId : undefined}
    onClose={() => (showTriggerCreate = false)}
  />
{/if}

<style>
  .right-panel-content {
    display: flex;
    flex-direction: column;
    height: 100%;
    overflow: hidden;
  }

  .right-panel-content.collapsed {
    visibility: hidden;
  }

  .panel-header {
    display: flex;
    align-items: center;
    /* Title on the left, indented to the 16px content line shared by the chat
       title and the sidebar brand; view toggle hugging the right gutter (its
       right edge lines up with the Tasks/Triggers cards' right edge below,
       same --spacing-sm side gutter). */
    justify-content: space-between;
    gap: var(--spacing-sm);
    box-sizing: border-box;
    /* Height + padding mirror the sidebar header (16px top + ~40px brand row +
       8px bottom = 64px) so the "Dashboard" title and the view toggle sit on
       the same line as the sidebar's New Thread button (all centre ~36px from
       the top). align-items:center holds this whatever the toggle's height is. */
    min-height: 64px;
    padding: var(--spacing-md) var(--spacing-sm) var(--spacing-sm) var(--spacing-md);
    /* Hairline under the header, matching the line above the sidebar account
       row (its footer's border-top uses the same token). */
    border-bottom: 1px solid var(--glass-border);
  }

  .panel-title {
    margin: 0;
    /* Sized so the title's line box matches the segmented toggle's height
       (~25px): 20px text at line-height 1.25. Reads as a proper section
       heading and sits vertically centred against the toggle via the header's
       align-items:center. Shrinks with an ellipsis before the toggle does if
       the panel gets narrow. */
    font-size: var(--font-size-xl);
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: -0.01em;
    line-height: 1.25;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }

  /* Segmented control: one bordered container holding both views as segments,
     instead of two free-standing buttons. The container owns the border + the
     rounded clip; segments are borderless and divided by a single hairline. */
  .segmented {
    display: inline-flex;
    align-items: stretch;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    overflow: hidden;
    /* Keep the labels at full size; the header's flex-wrap is the fallback
       when there isn't room for the title and the control on one row. */
    flex-shrink: 0;
  }

  .segment {
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--text-muted);
    background: transparent;
    border: none;
    cursor: pointer;
    white-space: nowrap;
    transition: all var(--transition-fast);
  }

  .segment + .segment {
    border-left: 1px solid var(--glass-border);
  }

  .segment:hover {
    color: var(--text-primary);
  }

  /* Active segment reuses the previous active-tab fill + text color verbatim
     (accent tint background, accent-primary text); the container border
     replaces the per-button border the old active tab carried. */
  .segment.active {
    color: var(--accent-primary);
    background: var(--accent-tint-bg);
  }

  .panel-body {
    /* Position context for the absolutely-positioned .dashboard-sections so
       both instances of the keyed wrapper overlap in the same physical space
       during the tab fade (instead of stacking vertically, which made the
       scrollbar flicker as the panel briefly doubled in height). The
       dashboard-sections itself handles scrolling now. */
    flex: 1;
    position: relative;
    overflow: hidden;
  }

  .dashboard-sections {
    position: absolute;
    inset: 0;
    overflow-y: auto;
    /* Count alignment: every count badge in this panel (the Tasks/Triggers
       section-header counts AND the In Progress/Upcoming/Active/etc. group
       counts) lines up in one vertical column. This is the reserved width of a
       GROUP label; the count begins right after it. SectionHeader derives its
       title's reserved width from this same value (minus the 24px the chevron +
       header padding add to its prefix) so header and group counts share the
       column. Sized just past the longest label ("IN PROGRESS", ~87px at the
       16px Geist group-label size) so the count stack sits as far left as the
       shared column allows without a longer label colliding into it; only
       defined here, so the column exists in the dashboard and nowhere else. One
       number tunes the whole column. */
    --count-col-label: 90px;
    /* Top padding matches the sides (8px) so the first section's top edge lines
       up with the thread-list "Recent" sort button in the sidebar: both sit 8px
       below their respective 64px headers. Sides keep the --spacing-sm content
       gutter the cards align to. */
    padding: var(--spacing-sm);
    display: flex;
    flex-direction: column;
    /* Gap between the dashboard cards follows the Appearance > Spacing setting;
       falls back to --spacing-sm (the original 8px) when no override is set. */
    gap: var(--ui-density-gap, var(--spacing-sm));
  }

  /* The section cards must never shrink. SectionHeader's wrapper sets
     overflow:hidden (so its rounded corners clip the sliding body), which
     zeroes the flex item's automatic min-height. Without this, once the
     sections' combined height exceeds the panel, the flex column shrinks them
     to fit and the overflow:hidden clips the last card's bottom instead of
     letting the panel scroll. flex-shrink:0 keeps each card at its natural
     height, so the panel scrolls (overflow-y:auto above) rather than clipping. */
  .dashboard-sections > :global(.section) {
    flex-shrink: 0;
  }

  /* When switching INTO the 'thread' tab, hide every scrollbar inside the
     panel-body for the duration of the fade. No transition — they just
     vanish for ~220ms and come back once the new tab is settled. */
  .panel-body.suppress-scrollbar .dashboard-sections {
    scrollbar-width: none;
  }
  .panel-body.suppress-scrollbar .dashboard-sections::-webkit-scrollbar {
    display: none;
  }

</style>
