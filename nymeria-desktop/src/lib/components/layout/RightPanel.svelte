<script lang="ts">
  import { fly } from 'svelte/transition';
  import { TAB_FADE } from '$lib/utils/transitions';
  import { Collapsible } from '$lib/components/common';
  import TodoFeed from '$lib/components/todos/TodoFeed.svelte';
  import TriggerFeed from '$lib/components/triggers/TriggerFeed.svelte';
  import { ActivityFeed, ConnectionStatus } from '$lib/components/dashboard';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { switchToThread } from '$lib/stores/navigation.svelte';
  import { uiStore } from '$lib/stores/ui.svelte';

  let isCollapsed = $derived(uiStore.rightPanelCollapsed);

  let activeTab = $state<'thread' | 'global'>('thread');
  let currentThreadId = $derived(threadsStore.currentThreadId);

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
    <h2>Dashboard</h2>
    <div class="tab-buttons" role="tablist" aria-label="Dashboard view">
      <button
        id="dashboard-tab-thread"
        class="tab-btn"
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
        class="tab-btn"
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
      <!-- Tasks Section -->
      <Collapsible title="Tasks" defaultOpen={true}>
        {#snippet header()}
          <span class="section-heading">Tasks</span>
          {#if todosStore.todos.length > 0}
            <span class="section-count">{todosStore.todos.length}</span>
          {/if}
        {/snippet}
        {#if activeTab === 'thread' && currentThreadId}
          <TodoFeed threadId={currentThreadId} />
        {:else}
          <TodoFeed {threadTitleMap} onNavigateToThread={navigateToThread} />
        {/if}
      </Collapsible>

      <!-- Triggers Section -->
      <Collapsible title="Triggers" defaultOpen={true}>
        {#snippet header()}
          <span class="section-heading">Triggers</span>
          {#if triggersStore.enabledCount > 0}
            <span class="section-count">{triggersStore.enabledCount}</span>
          {/if}
        {/snippet}
        {#if activeTab === 'thread' && currentThreadId}
          <TriggerFeed threadId={currentThreadId} />
        {:else}
          <TriggerFeed {threadTitleMap} onNavigateToThread={navigateToThread} />
        {/if}
      </Collapsible>

      <!-- Activity Section — always visible -->
      <div class="section-divider"></div>
      <div class="activity-section">
        <div class="activity-header">
          <span class="section-heading">Activity</span>
        </div>
        {#if activeTab === 'thread' && currentThreadId}
          <ActivityFeed threadId={currentThreadId} />
        {:else}
          <ActivityFeed {threadTitleMap} onNavigateToThread={navigateToThread} />
        {/if}
      </div>
    </div>
    {/key}
  </div>
  <ConnectionStatus />
</div>

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
    padding: var(--spacing-md);
    border-bottom: 1px solid var(--glass-border);
  }

  .panel-header h2 {
    margin: 0;
    font-size: var(--font-size-lg);
    font-weight: 700;
    color: var(--text-primary);
    letter-spacing: -0.01em;
  }

  .tab-buttons {
    display: flex;
    /* §3 chip-row gap: ≥8px so adjacent tabs don't crowd edge-to-edge. */
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
  }

  .tab-btn {
    flex: 1;
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .tab-btn:hover {
    color: var(--text-primary);
    border-color: var(--text-muted);
  }

  .tab-btn.active {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
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
    padding: var(--spacing-sm);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
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

  /* Nudge the chevron inside Tasks/Triggers headers down by 1px for better
     vertical centering. Scoped to Collapsibles so Activity (which is not
     collapsible) is unaffected. The open state combines the nudge with the
     90deg rotation — using a single `transform` for both means we have to
     respecify the full value here (otherwise the rotate would be lost). */
  :global(.dashboard-sections .collapsible > .header .chevron) {
    transform: translateY(1px);
  }
  :global(.dashboard-sections .collapsible.open > .header .chevron) {
    transform: translateY(1px) rotate(90deg);
  }

  .section-heading {
    flex: 1;
    font-weight: 600;
    font-size: var(--font-size-md);
    color: var(--text-primary);
  }

  .section-count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 20px;
    height: 20px;
    padding: 0 6px;
    font-size: var(--font-size-2xs);
    font-weight: 500;
    background: transparent;
    color: var(--text-muted);
    border-radius: var(--radius-full);
  }

  .section-divider {
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--glass-border), transparent);
    margin: var(--spacing-xs) 0;
  }

  .activity-section {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow-y: auto;
    overflow-x: hidden;
  }

  .activity-header {
    display: flex;
    align-items: center;
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--text-primary);
  }
</style>
