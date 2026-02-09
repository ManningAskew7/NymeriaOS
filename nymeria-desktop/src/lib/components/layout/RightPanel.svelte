<script lang="ts">
  import { Collapsible } from '$lib/components/common';
  import TodoFeed from '$lib/components/todos/TodoFeed.svelte';
  import { ActivityFeed, ConnectionStatus } from '$lib/components/dashboard';
  import { todosStore } from '$lib/stores/todos.svelte';
</script>

<div class="right-panel-content">
  <div class="panel-header">
    <h2>Dashboard</h2>
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
        <TodoFeed />
      </Collapsible>

      <!-- Activity Section — always visible -->
      <div class="section-divider"></div>
      <div class="activity-section">
        <div class="activity-header">
          <span class="section-title">Activity</span>
        </div>
        <ActivityFeed />
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
