<script lang="ts">
  import { activityStore } from '$lib/stores/activity.svelte';
  import type { ActivityEntry } from '$lib/types';
  import ActivityItem from './ActivityItem.svelte';
  import { Icon } from '$lib/components/common';
  import InlineLoader from '$lib/components/common/InlineLoader.svelte';
  import { onMount } from 'svelte';
  import { slide } from 'svelte/transition';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import { formatRelativeTime } from '$lib/utils/time';

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
      <InlineLoader text="Loading recent activity…" />
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
        <p class="hint">Activity from autonomous tasks and trigger runs will appear here</p>
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
              <span class="trigger-last-time">{formatRelativeTime(item.entries[0].timestamp)}</span>
              <span class="trigger-chevron" class:rotated={expanded}>
                <Icon name="chevronDown" size={12} />
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
    /* Fill the activity section's flex:1 height so the single message states
       below can sit in the vertical centre of the panel. The populated
       .activity-list stays flex-start (top-aligned) and scrolls as before. */
    flex: 1;
    min-height: 0;
  }

  .loading-state,
  .error-state,
  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-md);
    /* Auto top/bottom margins centre the state vertically in the feed's free
       space; they collapse to the top gracefully when the panel is too short. */
    margin: auto 0;
  }

  /* Loading text sits one size down (pairs with the InlineLoader's sm
     spinner); this lived on the removed .loading-text span before. */
  .loading-state {
    font-size: var(--font-size-sm);
  }

  /* Empty-state text matches the Triggers/Tasks empty states (sm, 14px); the
     .hint line keeps its own xs below. Without this the message inherited the
     16px reading size and read larger than its siblings. */
  .empty-state {
    font-size: var(--font-size-sm);
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
    /* Category marker, not state: muted gray like the rest of the feed. */
    color: var(--text-muted);
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
    font-size: var(--font-size-3xs);
    font-weight: 600;
    /* Neutral count chip (was an accent tint); matches the thread-name badge so
       the feed carries no decorative accent. */
    background: var(--bg-elevated);
    color: var(--text-secondary);
    border-radius: var(--radius-full);
    flex-shrink: 0;
  }

  .trigger-last-time {
    font-size: var(--font-size-3xs);
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .trigger-chevron {
    display: flex;
    align-items: center;
    color: var(--text-muted);
    transition: transform 120ms var(--ease-out);
    flex-shrink: 0;
  }

  .trigger-chevron.rotated {
    transform: rotate(180deg);
  }

  .trigger-group-items {
    padding-left: var(--spacing-sm);
    /* Neutral indent rail (was an accent tint): structure, not state. */
    border-left: 2px solid var(--border-default);
    margin-left: var(--spacing-md);
  }
</style>
