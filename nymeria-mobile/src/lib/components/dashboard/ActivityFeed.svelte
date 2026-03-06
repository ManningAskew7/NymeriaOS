<script lang="ts">
  import { activityStore } from '$lib/stores/activity.svelte';
  import type { ActivityEntry } from '$lib/types';
  import ActivityItem from './ActivityItem.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import { onMount } from 'svelte';

  interface Props {
    threadId?: string;
    threadTitleMap?: Record<string, string>;
    onNavigateToThread?: (threadId: string) => void;
  }

  let { threadId, threadTitleMap, onNavigateToThread }: Props = $props();

  // Track which trigger groups are expanded
  let expandedGroups = $state<Set<string>>(new Set());

  onMount(() => {
    activityStore.startPolling(threadId);
    return () => activityStore.stopPolling();
  });

  // Re-fetch when threadId changes
  $effect(() => {
    activityStore.fetch(50, threadId);
  });

  // Group trigger entries by trigger_id
  type FeedItem =
    | { kind: 'single'; entry: ActivityEntry }
    | { kind: 'group'; triggerId: string; triggerName: string; entries: ActivityEntry[] };

  let feedItems = $derived.by((): FeedItem[] => {
    const entries = activityStore.entries;
    if (entries.length === 0) return [];

    const items: FeedItem[] = [];
    const triggerGroups = new Map<string, ActivityEntry[]>();

    for (const entry of entries) {
      if (entry.type === 'trigger_completed' && entry.metadata?.trigger_id) {
        const tid = entry.metadata.trigger_id as string;
        if (!triggerGroups.has(tid)) triggerGroups.set(tid, []);
        triggerGroups.get(tid)!.push(entry);
      }
    }

    const emittedGroups = new Set<string>();
    for (const entry of entries) {
      if (entry.type === 'trigger_completed' && entry.metadata?.trigger_id) {
        const tid = entry.metadata.trigger_id as string;
        if (!emittedGroups.has(tid)) {
          emittedGroups.add(tid);
          const group = triggerGroups.get(tid)!;
          if (group.length === 1) {
            items.push({ kind: 'single', entry: group[0] });
          } else {
            items.push({
              kind: 'group',
              triggerId: tid,
              triggerName: (group[0].metadata?.trigger_name as string) || 'Trigger',
              entries: group,
            });
          }
        }
      } else {
        items.push({ kind: 'single', entry });
      }
    }

    return items;
  });

  function toggleGroup(triggerId: string) {
    const next = new Set(expandedGroups);
    if (next.has(triggerId)) next.delete(triggerId);
    else next.add(triggerId);
    expandedGroups = next;
  }

  function formatTimeAgo(timestamp: Date): string {
    const now = new Date();
    const diff = now.getTime() - timestamp.getTime();
    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);
    if (days > 0) return `${days}d ago`;
    if (hours > 0) return `${hours}h ago`;
    if (minutes > 0) return `${minutes}m ago`;
    return 'Just now';
  }

  function getThreadTitle(entry: ActivityEntry): string | undefined {
    if (!threadTitleMap || !entry.threadId) return undefined;
    return threadTitleMap[entry.threadId];
  }

  function makeNavigateHandler(entry: ActivityEntry): (() => void) | undefined {
    if (!onNavigateToThread || !entry.threadId) return undefined;
    const tid = entry.threadId;
    return () => onNavigateToThread!(tid);
  }
</script>

<div class="activity-feed">
  {#if activityStore.loading && activityStore.entries.length === 0}
    <div class="feed-state">
      <span>Loading...</span>
    </div>
  {:else if activityStore.error}
    <div class="feed-state error">
      <p>{activityStore.error}</p>
    </div>
  {:else if activityStore.entries.length === 0}
    <div class="feed-state">
      {#if threadId}
        <p>No activity in this thread</p>
        <span class="hint">Autonomous task activity for this thread will appear here</span>
      {:else}
        <p>No recent activity</p>
        <span class="hint">Activity will appear here as Nymeria works</span>
      {/if}
    </div>
  {:else}
    <div class="activity-list">
      {#each feedItems as item}
        {#if item.kind === 'single'}
          <ActivityItem
            entry={item.entry}
            threadTitle={getThreadTitle(item.entry)}
            onNavigate={makeNavigateHandler(item.entry)}
          />
        {:else}
          {@const expanded = expandedGroups.has(item.triggerId)}
          <div class="trigger-group">
            <button class="trigger-group-header" onclick={() => toggleGroup(item.triggerId)}>
              <Icon name="bolt" size={14} />
              <span class="trigger-name">{item.triggerName}</span>
              <span class="trigger-count">{item.entries.length}</span>
              <span class="trigger-time">{formatTimeAgo(item.entries[0].timestamp)}</span>
              <span class="trigger-chevron" class:rotated={expanded}>
                <Icon name="chevronDown" size={12} />
              </span>
            </button>
            {#if expanded}
              <div class="trigger-group-items">
                {#each item.entries as entry (entry.id)}
                  <ActivityItem
                    {entry}
                    threadTitle={getThreadTitle(entry)}
                    onNavigate={makeNavigateHandler(entry)}
                  />
                {/each}
              </div>
            {/if}
          </div>
        {/if}
      {/each}
    </div>
  {/if}
</div>

<style>
  .activity-feed {
    display: flex;
    flex-direction: column;
  }

  .feed-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-lg);
    font-size: var(--font-size-sm);
  }

  .feed-state.error {
    color: var(--error);
  }

  .feed-state p {
    margin: 0;
  }

  .hint {
    font-size: var(--font-size-xs);
    display: block;
    margin-top: var(--spacing-xs);
  }

  .activity-list {
    display: flex;
    flex-direction: column;
  }

  .trigger-group {
    overflow: hidden;
  }

  .trigger-group-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--accent-secondary, var(--accent-primary));
    font-size: var(--font-size-sm);
    text-align: left;
    min-height: 40px;
  }

  .trigger-group-header:active {
    background: var(--bg-hover);
  }

  .trigger-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 500;
    color: var(--text-secondary);
  }

  .trigger-count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 20px;
    height: 20px;
    padding: 0 6px;
    font-size: var(--font-size-xs);
    font-weight: 600;
    background: var(--accent-primary-alpha);
    color: var(--accent-primary);
    border-radius: 10px;
    flex-shrink: 0;
  }

  .trigger-time {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .trigger-chevron {
    display: flex;
    align-items: center;
    color: var(--text-muted);
    transition: transform var(--transition-fast);
    flex-shrink: 0;
  }

  .trigger-chevron.rotated {
    transform: rotate(180deg);
  }

  .trigger-group-items {
    padding-left: var(--spacing-md);
    border-left: 2px solid var(--accent-primary-alpha);
    margin-left: var(--spacing-lg);
  }
</style>
