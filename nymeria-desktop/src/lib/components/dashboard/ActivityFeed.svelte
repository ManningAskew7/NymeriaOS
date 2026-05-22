<script lang="ts">
  import { activityStore } from '$lib/stores/activity.svelte';
  import type { ActivityEntry } from '$lib/types';
  import ActivityItem from './ActivityItem.svelte';
  import { Icon } from '$lib/components/common';
  import { onMount } from 'svelte';
  import { slide } from 'svelte/transition';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';

  interface Props {
    threadId?: string;
    threadTitleMap?: Record<string, string>;
    onNavigateToThread?: (threadId: string) => void;
  }

  let { threadId, threadTitleMap, onNavigateToThread }: Props = $props();

  // Track which trigger groups are expanded
  let expandedGroups = $state<Set<string>>(new Set());

  onMount(() => {
    activityStore.startPolling();
    return () => activityStore.stopPolling();
  });

  // Re-fetch when threadId changes
  $effect(() => {
    activityStore.fetch(50, threadId);
  });

  // Group trigger entries by trigger_id, keep non-trigger entries flat
  type FeedItem =
    | { kind: 'single'; entry: ActivityEntry }
    | { kind: 'group'; triggerId: string; triggerName: string; entries: ActivityEntry[] };

  let feedItems = $derived.by((): FeedItem[] => {
    const entries = activityStore.entries;
    if (entries.length === 0) return [];

    const items: FeedItem[] = [];
    const triggerGroups = new Map<string, ActivityEntry[]>();
    const triggerGroupOrder: string[] = [];

    for (const entry of entries) {
      if (entry.type === 'trigger_completed' && entry.metadata?.trigger_id) {
        const tid = entry.metadata.trigger_id as string;
        if (!triggerGroups.has(tid)) {
          triggerGroups.set(tid, []);
          triggerGroupOrder.push(tid);
        }
        triggerGroups.get(tid)!.push(entry);
      }
    }

    // Build feed: walk entries, emit groups at position of first (most recent) occurrence
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
        // Skip individual trigger entries — they're in the group
      } else {
        items.push({ kind: 'single', entry });
      }
    }

    return items;
  });

  function toggleGroup(triggerId: string) {
    const next = new Set(expandedGroups);
    if (next.has(triggerId)) {
      next.delete(triggerId);
    } else {
      next.add(triggerId);
    }
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
            <button class="trigger-group-header" onclick={() => toggleGroup(item.triggerId)} type="button">
              <span class="trigger-group-icon">
                <Icon name="bolt" size={12} />
              </span>
              <span class="trigger-name">{item.triggerName}</span>
              <span class="trigger-count">{item.entries.length}</span>
              <span class="trigger-last-time">{formatTimeAgo(item.entries[0].timestamp)}</span>
              <span class="trigger-chevron" class:rotated={expanded}>
                <Icon name="chevronRight" size={12} />
              </span>
            </button>
            {#if expanded}
              <div class="trigger-group-items" transition:slide={DROPDOWN_TRANSITION}>
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
    color: color-mix(in srgb, var(--error) 80%, var(--text-muted));
    font-size: var(--font-size-sm);
  }

  .error-state p {
    margin: 0;
  }

  .loading-text {
    font-size: var(--font-size-sm);
  }

  .activity-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    overflow-y: auto;
    overflow-x: hidden;
  }

  /* Trigger group styles */
  .trigger-group {
    border-radius: var(--radius-sm);
    overflow: hidden;
  }

  .trigger-group-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    width: 100%;
    padding: var(--spacing-xs) var(--spacing-sm);
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    font-family: inherit;
    cursor: pointer;
    transition: background var(--transition-fast);
    text-align: left;
  }

  .trigger-group-header:hover {
    background: var(--bg-hover);
  }

  .trigger-group-icon {
    color: var(--accent-secondary, var(--accent-primary));
    display: flex;
    align-items: center;
    flex-shrink: 0;
    opacity: 0.8;
  }

  .trigger-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 500;
  }

  .trigger-count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    font-size: 10px;
    font-weight: 600;
    background: rgba(var(--accent-primary-rgb), 0.15);
    color: var(--accent-primary);
    border-radius: var(--radius-full);
    flex-shrink: 0;
  }

  .trigger-last-time {
    font-size: 10px;
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .trigger-chevron {
    display: flex;
    align-items: center;
    color: var(--text-muted);
    transition: transform 120ms cubic-bezier(0.33, 1, 0.68, 1);
    flex-shrink: 0;
  }

  .trigger-chevron.rotated {
    transform: rotate(90deg);
  }

  .trigger-group-items {
    padding-left: var(--spacing-sm);
    border-left: 2px solid rgba(var(--accent-primary-rgb), 0.2);
    margin-left: var(--spacing-md);
  }
</style>
