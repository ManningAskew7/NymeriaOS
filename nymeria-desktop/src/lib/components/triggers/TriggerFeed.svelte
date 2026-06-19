<script lang="ts">
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import TriggerItem from './TriggerItem.svelte';
  import TriggerSetupWizard from './TriggerSetupWizard.svelte';
  import TriggerHistoryPanel from './TriggerHistoryPanel.svelte';
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
</script>

<div class="trigger-feed">
  <!-- No loading-state branch on purpose — same reason as TodoFeed.
       The Svelte slide transition on the wrapping collapsible section measures
       the .content height once at intro-start; if the initial render
       were the small loading-state and the fetch resolves mid-slide,
       the rendered state switches to the (taller) empty-state and the
       element snaps when the slide's inline styles clear at the end.
       Starting at empty-state keeps the measured height stable. -->
  {#if triggersStore.error}
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
    {#if activeTriggers.length > 0}
      <div class="trigger-group">
        <h3 class="group-label section-label">
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
        <h3 class="group-label section-label">
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

  .trigger-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .group-label {
    display: flex;
    align-items: center;
    /* Count sits right after the label text (spaced by the gap), reading as
       "Upcoming 2" rather than floating at the row's right edge. */
    gap: var(--spacing-sm);
    margin: 0 0 var(--spacing-xs) 0;
    /* Type role (uppercase / tracking / weight / muted) is the global .section-label. */
    /* Indent to the 16px icon column (8px section body + 8px here) so the group
       label shares the chevron / row-icon vertical line. */
    padding-left: var(--spacing-sm);
  }

  /* Reserve the shared count-column width so the count begins at the same x as
     every other count in the dashboard panel. Falls back to 0 (count sits right
     after the label) wherever --count-col-label is not defined. */
  .label-text {
    min-width: var(--count-col-label, 0px);
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
    /* Rows sit flush (no gap): the hairline divider on each row but the first
       (see TriggerItem) provides the separation, matching the Tasks feed so
       both read as one continuous list inside the section card. */
    gap: 0;
  }
</style>
