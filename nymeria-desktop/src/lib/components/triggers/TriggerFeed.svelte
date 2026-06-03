<script lang="ts">
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import TriggerItem from './TriggerItem.svelte';
  import TriggerSetupWizard from './TriggerSetupWizard.svelte';
  import TriggerHistoryPanel from './TriggerHistoryPanel.svelte';
  import { Icon } from '$lib/components/common';
  import { onMount, onDestroy } from 'svelte';
  import type { Trigger } from '$lib/types';

  interface Props {
    threadId?: string;
    threadTitleMap?: Record<string, string>;
    onNavigateToThread?: (threadId: string) => void;
  }

  let { threadId, threadTitleMap, onNavigateToThread }: Props = $props();

  let showWizard = $state(false);
  let editTarget = $state<Trigger | undefined>(undefined);
  let historyTarget = $state<Trigger | undefined>(undefined);

  onMount(() => {
    if (!triggersStore.loaded && !triggersStore.loading) {
      triggersStore.loadTriggers();
    }
    if (Object.keys(triggersStore.sources).length === 0) {
      triggersStore.loadSources();
    }
    triggersStore.startPolling();
  });

  onDestroy(() => {
    triggersStore.stopPolling();
  });

  const filteredTriggers = $derived(
    threadId
      ? triggersStore.triggers.filter(t => t.thread_id === threadId)
      : triggersStore.triggers
  );

  const activeTriggers = $derived(filteredTriggers.filter(t => t.enabled));
  const pausedTriggers = $derived(filteredTriggers.filter(t => !t.enabled));

  function handleEdit(trigger: Trigger) {
    editTarget = trigger;
    showWizard = true;
  }

  function handleHistory(trigger: Trigger) {
    historyTarget = trigger;
  }

  function closeWizard() {
    showWizard = false;
    editTarget = undefined;
  }

  function openNewWizard() {
    editTarget = undefined;
    showWizard = true;
  }
</script>

<div class="trigger-feed">
  <div class="feed-header">
    <button class="add-trigger-btn" onclick={openNewWizard} type="button">
      <Icon name="plus" size={14} />
      <span>New Trigger</span>
    </button>
  </div>

  {#if triggersStore.loading && filteredTriggers.length === 0}
    <div class="loading-state">
      <span class="loading-text">Loading...</span>
    </div>
  {:else if triggersStore.error}
    <div class="error-state">
      <p>{triggersStore.error}</p>
      <button class="retry-btn" onclick={() => triggersStore.loadTriggers()} type="button">
        Retry
      </button>
    </div>
  {:else if filteredTriggers.length === 0}
    <div class="empty-state">
      {#if threadId}
        <p>No triggers in this thread</p>
        <p class="hint">Create a trigger to automate responses to events</p>
      {:else}
        <p>No triggers yet</p>
        <p class="hint">Create your first automation to react to webhooks, emails, and more</p>
      {/if}
    </div>
  {:else}
    {#if activeTriggers.length > 0}
      <div class="trigger-group">
        <h3 class="group-label">
          <span class="label-text">Active</span>
          <span class="count highlight">{activeTriggers.length}</span>
        </h3>
        <div class="group-items">
          {#each activeTriggers as trigger, i (trigger.id)}
            <TriggerItem
              {trigger}
              onEdit={handleEdit}
              onHistory={handleHistory}
              animationDelay={i * 30}
              threadTitle={threadTitleMap && trigger.thread_id ? threadTitleMap[trigger.thread_id] : undefined}
              onNavigateToThread={onNavigateToThread && trigger.thread_id ? () => onNavigateToThread!(trigger.thread_id) : undefined}
            />
          {/each}
        </div>
      </div>
    {/if}

    {#if pausedTriggers.length > 0}
      <div class="trigger-group">
        <h3 class="group-label">
          <span class="label-text">Paused</span>
          <span class="count">{pausedTriggers.length}</span>
        </h3>
        <div class="group-items">
          {#each pausedTriggers as trigger, i (trigger.id)}
            <TriggerItem
              {trigger}
              onEdit={handleEdit}
              onHistory={handleHistory}
              animationDelay={i * 30}
              threadTitle={threadTitleMap && trigger.thread_id ? threadTitleMap[trigger.thread_id] : undefined}
              onNavigateToThread={onNavigateToThread && trigger.thread_id ? () => onNavigateToThread!(trigger.thread_id) : undefined}
            />
          {/each}
        </div>
      </div>
    {/if}
  {/if}
</div>

{#if showWizard}
  <TriggerSetupWizard
    {threadId}
    editTrigger={editTarget}
    onClose={closeWizard}
  />
{/if}

{#if historyTarget}
  <TriggerHistoryPanel
    trigger={historyTarget}
    onClose={() => (historyTarget = undefined)}
  />
{/if}

<style>
  .trigger-feed {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
  }

  .feed-header {
    display: flex;
    width: 100%;
  }

  .add-trigger-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-xs);
    width: 100%;
    padding: var(--spacing-xs) var(--spacing-sm);
    background: transparent;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    font-weight: 500;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .add-trigger-btn:hover {
    border-color: var(--accent-primary);
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .add-trigger-btn span {
    transform: translateY(-1px);
  }

  .loading-state,
  .error-state,
  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-lg);
    /* Dashed border signals "placeholder, not interactive" — distinguishes
       these containers from the New Trigger button (solid border) sitting
       directly above them in the panel. */
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-md);
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
    margin: 0 0 var(--spacing-sm);
  }

  .retry-btn {
    padding: var(--spacing-xs) var(--spacing-md);
    background: transparent;
    border: 1px solid color-mix(in srgb, var(--error) 50%, transparent);
    border-radius: var(--radius-md);
    color: color-mix(in srgb, var(--error) 80%, var(--text-muted));
    font-size: var(--font-size-xs);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .retry-btn:hover {
    background: color-mix(in srgb, var(--error) 15%, transparent);
    border-color: var(--error);
    color: var(--error);
  }

  .loading-text {
    font-size: var(--font-size-sm);
  }

  .trigger-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .group-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin: 0 0 var(--spacing-xs) 0;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 16px;
    height: 16px;
    padding: 0 5px;
    font-size: var(--font-size-3xs);
    font-weight: 500;
    background: var(--bg-hover);
    color: var(--text-secondary);
    border-radius: var(--radius-full);
  }

  .count.highlight {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .group-items {
    display: flex;
    flex-direction: column;
    /* Matches TodoFeed.group-items — both lists of cards in the right
       panel share the same between-card breathing so they read as the
       same family of feed. */
    gap: var(--spacing-sm);
  }
</style>
