<script lang="ts">
  import { workflowsStore } from '$lib/stores/workflows.svelte';
  import WorkflowApprovalItem from './WorkflowApprovalItem.svelte';
  import WorkflowRunItem from './WorkflowRunItem.svelte';
  import { onMount } from 'svelte';

  onMount(() => {
    if (!workflowsStore.loaded && !workflowsStore.loading) {
      workflowsStore.loadWorkflows();
    }
  });

  let hasContent = $derived(
    workflowsStore.approvals.length > 0 ||
    workflowsStore.liveRuns.length > 0 ||
    workflowsStore.runs.length > 0
  );
</script>

<div class="workflow-feed">
  {#if workflowsStore.error}
    <div class="error-state">
      <p>{workflowsStore.error}</p>
      <button class="retry-btn" onclick={() => workflowsStore.loadWorkflows()} type="button">Retry</button>
    </div>
  {:else if !hasContent}
    <div class="empty-state">
      <p>No workflow activity yet</p>
      <p class="hint">Runs and approval requests from your workflow tools appear here</p>
    </div>
  {:else}
    {#if workflowsStore.approvals.length > 0}
      <div class="workflow-group">
        <h3 class="group-label section-label">
          <span class="label-text">Approvals</span>
          <span class="count">{workflowsStore.approvals.length}</span>
        </h3>
        <div class="group-items">
          {#each workflowsStore.approvals as approval (approval.record_id)}
            <WorkflowApprovalItem {approval} />
          {/each}
        </div>
      </div>
    {/if}

    {#if workflowsStore.liveRuns.length > 0}
      <div class="workflow-group">
        <h3 class="group-label section-label">
          <span class="label-text">Running</span>
          <span class="count">{workflowsStore.liveRuns.length}</span>
        </h3>
        <div class="group-items">
          {#each workflowsStore.liveRuns as live (live.run_id)}
            {@const latest = live.steps[live.steps.length - 1]}
            <div class="live-run">
              <span class="live-dot" aria-hidden="true"></span>
              <span class="live-name" title={live.workflow_id}>{live.workflow_id}</span>
              {#if latest}
                <span class="live-step">step {latest.step} · {latest.verb}</span>
              {/if}
            </div>
          {/each}
        </div>
      </div>
    {/if}

    {#if workflowsStore.runs.length > 0}
      <div class="workflow-group">
        <h3 class="group-label section-label">
          <span class="label-text">Recent runs</span>
          <span class="count">{workflowsStore.runs.length}</span>
        </h3>
        <div class="group-items">
          {#each workflowsStore.runs as run (run.run_id)}
            <WorkflowRunItem {run} />
          {/each}
        </div>
      </div>
    {/if}
  {/if}
</div>

<style>
  .workflow-feed {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
  }

  .error-state,
  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-lg);
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .empty-state p { margin: 0; }

  .empty-state .hint {
    font-size: var(--font-size-xs);
    margin-top: var(--spacing-xs);
  }

  .error-state {
    color: color-mix(in srgb, var(--error) 80%, var(--text-muted));
    font-size: var(--font-size-sm);
  }

  .error-state p { margin: 0 0 var(--spacing-sm); }

  .retry-btn {
    min-height: var(--touch-target-min);
    padding: var(--spacing-xs) var(--spacing-md);
    background: transparent;
    border: 1px solid color-mix(in srgb, var(--error) 50%, transparent);
    border-radius: var(--radius-md);
    color: color-mix(in srgb, var(--error) 80%, var(--text-muted));
    font-size: var(--font-size-xs);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .retry-btn:active {
    background: color-mix(in srgb, var(--error) 15%, transparent);
    border-color: var(--error);
    color: var(--error);
  }

  .workflow-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .group-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin: 0 0 var(--spacing-xs) 0;
    padding-left: var(--spacing-sm);
  }

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

  .group-items {
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  /* Live (in-flight) run row: pulsing accent dot + latest step. */
  .live-run {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
  }

  .live-dot {
    flex-shrink: 0;
    width: 6px;
    height: 6px;
    margin-left: 4px;
    border-radius: var(--radius-full);
    background: var(--accent-primary);
    animation: pulse 2s ease-in-out infinite;
  }

  .live-name {
    /* Flex child with nowrap: without min-width:0 it refuses to shrink and a
       long workflow name overflows the row instead of ellipsizing. */
    min-width: 0;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .live-step {
    margin-left: auto;
    flex-shrink: 0;
    font-size: var(--font-size-3xs);
    color: var(--text-muted);
    font-family: var(--font-mono);
    white-space: nowrap;
    max-width: 50%;
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>
