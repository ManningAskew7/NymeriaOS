<script lang="ts">
  import type { ToolCall } from '$lib/types';
  import { Collapsible } from '$lib/components/common';
  import { renderMarkdown } from '$lib/utils/markdown';

  interface Props {
    toolCall: ToolCall;
  }

  let { toolCall }: Props = $props();

  const skillName = $derived(
    (toolCall.arguments?.name as string | undefined) ?? '(unknown skill)'
  );

  const resultText = $derived(toolCall.result ?? '');
  const isError = $derived(
    toolCall.status === 'error' ||
      (typeof resultText === 'string' && resultText.startsWith('[skill not found]'))
  );

  const duration = $derived.by(() => {
    if (!toolCall.startTime || !toolCall.endTime) return null;
    const ms = toolCall.endTime.getTime() - toolCall.startTime.getTime();
    return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
  });
</script>

<div class="skill-card" class:running={toolCall.status === 'running'} class:error={isError}>
  <Collapsible title="Skill" chevronIcon="bolt" chevronSize={16}>
    {#snippet header()}
      <div class="skill-header">
        <span class="skill-label">Skill activated</span>
        <span class="skill-pill">{skillName}</span>
        {#if duration}
          <span class="skill-meta">{duration}</span>
        {:else if toolCall.status === 'running'}
          <span class="skill-meta">loading…</span>
        {/if}
      </div>
    {/snippet}

    <div class="skill-body">
      {#if resultText}
        <div class="markdown-content">
          {@html renderMarkdown(resultText)}
        </div>
      {:else if toolCall.status === 'running'}
        <p class="hint">Loading skill instructions…</p>
      {/if}
    </div>
  </Collapsible>
</div>

<style>
  .skill-card {
    position: relative;
    background: color-mix(in srgb, var(--accent-primary) 6%, var(--bg-elevated));
    border-radius: var(--radius-md);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 40%, var(--glass-border));
    margin: var(--spacing-sm) 0;
    overflow: hidden;
  }
  .skill-card.running {
    border-color: var(--accent-primary);
  }
  .skill-card.error {
    border-color: var(--error);
    background: color-mix(in srgb, var(--error) 5%, var(--glass-bg));
  }

  .skill-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex: 1;
    min-width: 0;
  }

  .skill-label {
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    font-weight: 600;
  }

  .skill-pill {
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
    background: var(--bg-base);
    color: var(--text-primary);
    padding: 2px 8px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
  }

  .skill-meta {
    margin-left: auto;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .skill-body {
    padding: var(--spacing-md);
    max-height: 520px;
    overflow-y: auto;
    background: var(--bg-base);
  }

  .skill-body .hint {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }

  .skill-body :global(.markdown-content) {
    font-size: var(--font-size-sm);
    line-height: 1.55;
    color: var(--text-primary);
  }
  .skill-body :global(.markdown-content h1),
  .skill-body :global(.markdown-content h2),
  .skill-body :global(.markdown-content h3) {
    margin-top: var(--spacing-md);
    margin-bottom: var(--spacing-xs);
  }
  .skill-body :global(.markdown-content pre) {
    background: var(--bg-elevated);
    border-radius: var(--radius-sm);
    padding: var(--spacing-sm);
    overflow-x: auto;
  }

  /* Override the Collapsible's chevron behavior for the skill bolt:
     - Nudge down 1px for optical centering against the SKILL ACTIVATED label.
     - Never rotate — the bolt is a static "activated" indicator, not a
       collapse-state arrow, so rotating it on open reads as wrong.
     The same transform value is set on both the resting and .open states so
     the .open .chevron rotation override never kicks in. */
  .skill-card :global(.collapsible > .header > .chevron),
  .skill-card :global(.collapsible.open > .header > .chevron) {
    transform: translateY(1px);
  }
</style>
