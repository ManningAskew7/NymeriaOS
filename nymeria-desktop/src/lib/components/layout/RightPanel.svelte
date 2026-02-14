<script lang="ts">
  import { Collapsible } from '$lib/components/common';
  import TodoFeed from '$lib/components/todos/TodoFeed.svelte';
  import { ActivityFeed, ConnectionStatus } from '$lib/components/dashboard';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { api } from '$lib/services/api.svelte';

  let activeTab = $state<'thread' | 'global'>('thread');
  let currentThreadId = $derived(threadsStore.currentThreadId);

  // Build thread title lookup for global view
  let threadTitleMap = $derived(
    Object.fromEntries(threadsStore.threads.map(t => [t.id, t.title]))
  );

  // Navigate to a thread (for clicking activity/todo items in global view)
  function navigateToThread(threadId: string) {
    if (threadId === threadsStore.currentThreadId) return;
    threadsStore.selectThread(threadId);
    chatStore.clearMessages();
    Promise.all([
      api.getThreadHistory(threadId),
      api.getThreadContextStats(threadId),
    ]).then(([history, stats]) => {
      chatStore.setMessages(history.messages);
      chatStore.setContextStats(stats);
    });
    activeTab = 'thread';
  }
</script>

<div class="right-panel-content">
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
    <div class="dashboard-sections">
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
  </div>
  <ConnectionStatus />
</div>

<style>
  .right-panel-content {
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  .panel-header {
    padding: var(--spacing-md);
    border-bottom: 1px solid var(--glass-border);
  }

  .panel-header h2 {
    margin: 0;
    font-size: var(--font-size-lg);
    font-weight: 600;
    color: var(--text-primary);
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
    color: var(--accent-primary);
    border-color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.08);
    box-shadow: var(--accent-glow-sm);
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
    font-weight: 600;
    background: var(--accent-primary);
    color: var(--bg-base);
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
  }

  .activity-header {
    display: flex;
    align-items: center;
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--text-primary);
  }
</style>
