<script lang="ts">
  import { onMount } from 'svelte';
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

  // Auto-switch to Global tab when no thread is selected
  $effect(() => {
    if (activeTab === 'thread' && !currentThreadId) {
      activeTab = 'global';
    }
  });

  // Tab key toggles between "This Thread" and "Global" panes.
  // Guarded so it never hijacks typing, modifier-Tab shortcuts, or focus
  // movement inside a modal.
  function handleTabKey(e: KeyboardEvent) {
    if (e.key !== 'Tab') return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if (isCollapsed) return;

    // Use e.target — by the time the bubble-phase listener runs, the
    // browser may have already moved focus to the next element, so
    // document.activeElement no longer points at the originally-focused
    // textarea/input.
    const target = e.target as HTMLElement | null;
    if (target) {
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') return;
      if (target.isContentEditable) return;
      // Walk up for an editable ancestor (some rich editors put the
      // contenteditable on a wrapper above the actual event target).
      if (target.closest('[contenteditable="true"], [contenteditable=""]')) return;
      // Inside an open modal / dialog, leave Tab alone so it cycles focus
      // within the dialog the way users expect.
      if (target.closest('[role="dialog"]')) return;
    }

    e.preventDefault();
    if (activeTab === 'thread' && currentThreadId) {
      activeTab = 'global';
    } else if (activeTab === 'global' && currentThreadId) {
      activeTab = 'thread';
    }
  }

  onMount(() => {
    // capture: true so we observe the keydown before the browser's default
    // focus-change kicks in and we still see the originally-focused element.
    window.addEventListener('keydown', handleTabKey, true);
    return () => window.removeEventListener('keydown', handleTabKey, true);
  });

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
  <div class="panel-header">
    <h2>Dashboard</h2>
    <div class="tab-buttons">
      <button
        class="tab-btn"
        class:active={activeTab === 'thread'}
        onclick={() => (activeTab = 'thread')}
        type="button"
      >
        This Thread
      </button>
      <button
        class="tab-btn"
        class:active={activeTab === 'global'}
        onclick={() => (activeTab = 'global')}
        type="button"
      >
        Global
      </button>
    </div>
  </div>

  <div class="panel-body">
    {#key activeTab}
    <div class="dashboard-sections tab-fade">
      <!-- Tasks Section -->
      <Collapsible title="Tasks" defaultOpen={true}>
        {#snippet header()}
          <span class="section-title">Tasks</span>
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
          <span class="section-title">Triggers</span>
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
          <span class="section-title">Activity</span>
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
    gap: var(--spacing-xs);
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
    color: var(--text-primary);
    border-color: var(--border-default);
    background: var(--bg-elevated-2);
    box-shadow: none;
  }

  .tab-fade {
    animation: tabFade 180ms ease-out;
  }

  @keyframes tabFade {
    from { opacity: 0; transform: translateY(2px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .panel-body {
    flex: 1;
    overflow-y: auto;
    padding: var(--spacing-sm);
  }

  .dashboard-sections {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    height: 100%;
  }

  .section-title {
    flex: 1;
    font-weight: 500;
    font-size: var(--font-size-sm);
  }

  .section-count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 20px;
    height: 20px;
    padding: 0 6px;
    font-size: 11px;
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
