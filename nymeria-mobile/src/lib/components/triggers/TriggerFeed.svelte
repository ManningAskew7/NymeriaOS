<script lang="ts">
  import { isTriggerRunning, triggersStore } from '$lib/stores/triggers.svelte';
  import TriggerItem from './TriggerItem.svelte';
  import TriggerSetupWizard from './TriggerSetupWizard.svelte';
  import TriggerHistoryPanel from './TriggerHistoryPanel.svelte';
  import { Icon } from '$lib/components/common';
  import InlineLoader from '$lib/components/common/InlineLoader.svelte';
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

  // Grouped by what the backend will actually do, not by the enable switch: a
  // trigger the failure policy auto-paused is still `enabled`, and listing it
  // under Active would promise fires that never come.
  //
  // Three groups rather than two, because one "Paused" heading would have to
  // cover both "you switched this off" and "Nymeria stopped this", and only
  // the second needs a decision. The groups are disjoint and exhaustive; a
  // trigger the user switched off reads as Disabled whether or not it also
  // carries a pause stamp, since its own switch is the nearer explanation.
  const autoPausedTriggers = $derived(
    filteredTriggers.filter(t => t.enabled && !!t.auto_paused_at)
  );
  const activeTriggers = $derived(filteredTriggers.filter(isTriggerRunning));
  const disabledTriggers = $derived(filteredTriggers.filter(t => !t.enabled));

  function handleEdit(trigger: Trigger) {
    editTarget = trigger;
    showWizard = true;
  }

  function handleTest(trigger: Trigger) {
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
      <InlineLoader text="Loading triggers…" />
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
        <p class="hint">Create your first trigger to react to webhooks, emails, and more</p>
      {/if}
    </div>
  {:else}
    <!-- Auto-paused first: the only group that asks the user for a decision,
         and empty on a healthy list, so the usual order is unchanged. -->
    {#if autoPausedTriggers.length > 0}
      <div class="trigger-group">
        <h3 class="group-label section-label">
          <span class="label-text">Auto-paused</span>
          <span class="count attention">{autoPausedTriggers.length}</span>
        </h3>
        <div class="group-items">
          {#each autoPausedTriggers as trigger, i (trigger.id)}
            <TriggerItem
              {trigger}
              onEdit={handleEdit}
              onHistory={handleTest}
              animationDelay={i * 30}
              threadTitle={threadTitleMap && trigger.thread_id ? threadTitleMap[trigger.thread_id] : undefined}
              onNavigateToThread={onNavigateToThread && trigger.thread_id ? () => onNavigateToThread!(trigger.thread_id) : undefined}
            />
          {/each}
        </div>
      </div>
    {/if}

    {#if activeTriggers.length > 0}
      <div class="trigger-group">
        <h3 class="group-label section-label">
          <span class="label-text">Active</span>
          <span class="count highlight">{activeTriggers.length}</span>
        </h3>
        <div class="group-items highlighted">
          {#each activeTriggers as trigger, i (trigger.id)}
            <TriggerItem
              {trigger}
              onEdit={handleEdit}
              onHistory={handleTest}
              animationDelay={i * 30}
              threadTitle={threadTitleMap && trigger.thread_id ? threadTitleMap[trigger.thread_id] : undefined}
              onNavigateToThread={onNavigateToThread && trigger.thread_id ? () => onNavigateToThread!(trigger.thread_id) : undefined}
            />
          {/each}
        </div>
      </div>
    {/if}

    {#if disabledTriggers.length > 0}
      <div class="trigger-group">
        <h3 class="group-label section-label">
          <span class="label-text">Disabled</span>
          <span class="count">{disabledTriggers.length}</span>
        </h3>
        <div class="group-items">
          {#each disabledTriggers as trigger, i (trigger.id)}
            <TriggerItem
              {trigger}
              onEdit={handleEdit}
              onHistory={handleTest}
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
    justify-content: flex-end;
  }

  .add-trigger-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    background: transparent;
    border: 1px dashed var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    font-weight: 500;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .add-trigger-btn:hover {
    border-color: var(--accent-primary);
    color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.05);
  }

  .loading-state,
  .error-state,
  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-lg);
  }

  /* Loading text sits one size down (pairs with the InlineLoader's sm
     spinner); this lived on the removed .loading-text span before. */
  .loading-state {
    font-size: var(--font-size-sm);
  }

  .empty-state p {
    margin: 0;
  }

  .empty-state .hint {
    font-size: var(--font-size-sm);
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

  .trigger-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .group-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin: 0;
    /* Type role (uppercase / tracking / weight / muted) is the global .section-label. */
  }

  .count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 6px;
    font-size: 10px;
    font-weight: 600;
    background: var(--bg-elevated-2);
    border-radius: var(--radius-full);
  }

  .count.highlight {
    background: var(--accent-secondary, var(--accent-primary));
    color: var(--bg-base);
  }

  /* Amber count on the auto-paused group: the same cue the row chip uses, so
     the heading and the rows under it tell one story. */
  .count.attention {
    background: var(--warning);
    color: var(--bg-base);
  }

  .group-items {
    display: flex;
    flex-direction: column;
    background: var(--bg-elevated-2);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .group-items.highlighted {
    background: color-mix(in srgb, var(--accent-secondary) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent-secondary) 20%, transparent);
  }
</style>
