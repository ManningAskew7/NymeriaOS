<script lang="ts">
  import type { QueuedBatch } from '$lib/types';

  let { batch }: { batch: QueuedBatch } = $props();
</script>

<section class="queued-batch" aria-label={batch.total > 1 ? 'Batched inputs' : 'Queued input'}>
  <h3>{batch.total > 1 ? 'Batched inputs' : 'Queued input'}</h3>
  <p class="batch-explanation">
    {batch.total > 1
      ? `${batch.total} separate requests were sent together for one continuation.`
      : 'This request was added at a tool-round boundary.'}
  </p>
  <ol>
    {#each batch.inputs as input (input.promptId)}
      <li>
        <h4>Request {input.position} of {batch.total} · {input.sourceLabel} ({input.source})</h4>
        <pre>{input.modelContent}</pre>
      </li>
    {/each}
  </ol>
</section>

<style>
  .queued-batch {
    margin: var(--spacing-sm) 0;
    padding: var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-primary);
  }
  h3, h4 { margin: 0; font-size: var(--font-size-sm); font-weight: 600; }
  .batch-explanation {
    margin: var(--spacing-xs) 0 var(--spacing-sm);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }
  ol { list-style: none; margin: 0; padding: 0; }
  li + li { margin-top: var(--spacing-md); padding-top: var(--spacing-sm); border-top: 1px solid var(--border-subtle); }
  pre {
    margin: var(--spacing-xs) 0 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font: inherit;
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }
</style>
