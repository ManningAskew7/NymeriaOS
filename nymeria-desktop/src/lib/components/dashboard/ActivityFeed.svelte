<script lang="ts">
  import { activityStore } from '$lib/stores/activity.svelte';
  import ActivityItem from './ActivityItem.svelte';
  import { onMount } from 'svelte';

  interface Props {
    threadId?: string;
  }

  let { threadId }: Props = $props();

  onMount(() => {
    activityStore.startPolling();
    return () => activityStore.stopPolling();
  });

  // Re-fetch when threadId changes
  $effect(() => {
    activityStore.fetch(50, threadId);
  });
</script>

<div class="activity-feed">
  {#if activityStore.loading && activityStore.entries.length === 0}
    <div class="loading-state">
      <span class="loading-text">Loading...</span>
    </div>
  {:else if activityStore.error}
    <div class="error-state">
      <p>{activityStore.error}</p>
    </div>
  {:else if activityStore.entries.length === 0}
    <div class="empty-state">
      {#if threadId}
        <p>No activity in this thread</p>
        <p class="hint">Autonomous task activity for this thread will appear here</p>
      {:else}
        <p>No recent activity</p>
        <p class="hint">Activity will appear here as Nymeria works</p>
      {/if}
    </div>
  {:else}
    <div class="activity-list">
      {#each activityStore.entries as entry (entry.id)}
        <ActivityItem {entry} />
      {/each}
    </div>
  {/if}
</div>

<style>
  .activity-feed {
    display: flex;
    flex-direction: column;
  }

  .loading-state,
  .error-state,
  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-md);
  }

  .empty-state p {
    margin: 0;
  }

  .empty-state .hint {
    font-size: var(--font-size-xs);
    margin-top: var(--spacing-xs);
  }

  .error-state {
    color: var(--error);
  }

  .loading-text {
    font-size: var(--font-size-sm);
  }

  .activity-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    overflow-y: auto;
  }
</style>
