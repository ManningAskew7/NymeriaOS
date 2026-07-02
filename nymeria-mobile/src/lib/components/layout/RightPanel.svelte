<script lang="ts">
  import Icon from '$lib/components/common/Icon.svelte';
  import Collapsible from '$lib/components/common/Collapsible.svelte';
  import { ActivityFeed } from '$lib/components/dashboard';
  import { TodoFeed } from '$lib/components/dashboard';
  import TriggerFeed from '$lib/components/triggers/TriggerFeed.svelte';
  import HookFeed from '$lib/components/hooks/HookFeed.svelte';
  import WorkflowFeed from '$lib/components/workflows/WorkflowFeed.svelte';
  import { uiStore } from '$lib/stores/ui.svelte';
  import { activityStore } from '$lib/stores/activity.svelte';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { hooksStore } from '$lib/stores/hooks.svelte';
  import { workflowsStore } from '$lib/stores/workflows.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { switchToThread } from '$lib/stores/navigation.svelte';

  let activeTab = $state<'thread' | 'global'>('thread');
  let currentThreadId = $derived(threadsStore.currentThreadId);

  // Auto-switch to Global tab when no thread is selected
  $effect(() => {
    if (activeTab === 'thread' && !currentThreadId) {
      activeTab = 'global';
    }
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

  function handleRefresh() {
    const tid = activeTab === 'thread' && currentThreadId ? currentThreadId : undefined;
    activityStore.fetch(50, tid);
    todosStore.fetch(undefined, tid);
    triggersStore.loadTriggers();
    hooksStore.loadHooks();
    workflowsStore.loadWorkflows();
  }
</script>

<div class="right-panel">
  <header class="panel-header">
    <button
      class="back-btn"
      onclick={() => uiStore.goToChat()}
      aria-label="Back to chat"
    >
      <Icon name="chevronLeft" size={22} />
    </button>
    <h2>Dashboard</h2>
    <button
      class="refresh-btn"
      onclick={handleRefresh}
      aria-label="Refresh dashboard"
    >
      <Icon name="refresh" size={20} />
    </button>
  </header>

  <!-- Tab Toggle -->
  <div class="tab-bar" role="tablist" aria-label="Dashboard view">
    <button
      id="dashboard-tab-thread"
      class="tab-btn"
      class:active={activeTab === 'thread'}
      onclick={() => (activeTab = 'thread')}
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
      type="button"
      role="tab"
      aria-selected={activeTab === 'global'}
      aria-controls="dashboard-tabpanel"
    >
      Global
    </button>
  </div>

  <div
    class="panel-body"
    id="dashboard-tabpanel"
    role="tabpanel"
    aria-labelledby={`dashboard-tab-${activeTab}`}
    tabindex="0"
  >
    <!-- Tasks Section -->
    <Collapsible title="Tasks" defaultOpen={true}>
      {#snippet header()}
        <span class="section-heading">Tasks</span>
        {#if todosStore.activeCount > 0}
          <span class="section-count">{todosStore.activeCount}</span>
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

    <!-- Hooks Section -->
    <Collapsible title="Hooks" defaultOpen={true}>
      {#snippet header()}
        <span class="section-heading">Hooks</span>
        {#if hooksStore.enabledCount > 0}
          <span class="section-count">{hooksStore.enabledCount}</span>
        {/if}
      {/snippet}
      {#if activeTab === 'thread' && currentThreadId}
        <HookFeed threadId={currentThreadId} />
      {:else}
        <HookFeed {threadTitleMap} onNavigateToThread={navigateToThread} />
      {/if}
    </Collapsible>

    <!-- Workflows Section (global only: runs and approvals are per-user,
         not per-thread) -->
    {#if activeTab === 'global'}
      <Collapsible title="Workflows" defaultOpen={true}>
        {#snippet header()}
          <span class="section-heading">Workflows</span>
          {#if workflowsStore.pendingCount > 0}
            <span class="section-count">{workflowsStore.pendingCount}</span>
          {/if}
        {/snippet}
        <WorkflowFeed />
      </Collapsible>
    {/if}

    <!-- Activity Section -->
    <div class="section-divider"></div>
    <div class="activity-section">
      <div class="activity-header">
        <Icon name="bolt" size={16} />
        <span class="section-heading">Activity</span>
      </div>
      {#if activeTab === 'thread' && currentThreadId}
        <ActivityFeed threadId={currentThreadId} />
      {:else}
        <ActivityFeed {threadTitleMap} onNavigateToThread={navigateToThread} />
      {/if}
    </div>
  </div>
</div>

<style>
  .right-panel {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-elevated);
  }

  .panel-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 0 var(--spacing-md);
    height: var(--header-height);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .back-btn,
  .refresh-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
  }

  .back-btn:active,
  .refresh-btn:active {
    background: var(--bg-hover);
    color: var(--accent-primary);
  }

  .panel-header h2 {
    flex: 1;
    font-size: var(--font-size-lg);
    font-weight: 600;
  }

  /* Tab Toggle */
  .tab-bar {
    display: flex;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .tab-btn {
    flex: 1;
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    min-height: var(--touch-target-min);
    transition: all var(--transition-fast);
  }

  .tab-btn:active {
    background: var(--bg-hover);
  }

  .tab-btn.active {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
    background: var(--accent-tint-bg);
  }

  .panel-body {
    flex: 1;
    overflow-y: auto;
    overscroll-behavior-y: contain;
  }

  /* Section styling */
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
    font-size: var(--font-size-xs);
    font-weight: 600;
    background: var(--accent-primary);
    color: var(--bg-base);
    border-radius: 10px;
    flex-shrink: 0;
  }

  .section-divider {
    height: 1px;
    background: var(--border-subtle);
    margin: var(--spacing-xs) var(--spacing-md);
  }

  .activity-section {
    display: flex;
    flex-direction: column;
  }

  .activity-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--text-secondary);
  }
</style>
