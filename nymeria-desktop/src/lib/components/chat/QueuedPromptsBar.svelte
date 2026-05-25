<script lang="ts">
  import { Icon } from '$lib/components/common';
  import { chatStore } from '$lib/stores/chat.svelte';
  import type { PendingPrompt } from '$lib/types';

  let prompts = $derived(chatStore.pendingPrompts);

  function dismiss(id: string) {
    chatStore.removePendingPrompt(id);
  }

  function statusLabel(p: PendingPrompt): string {
    switch (p.status) {
      case 'sending':
        return 'Sending…';
      case 'queued':
        return p.position && p.position > 0
          ? `Queued · position ${p.position}`
          : 'Queued · will send at next halt';
      case 'error':
        return p.errorMessage ? `Failed · ${p.errorMessage}` : 'Failed';
    }
  }

  function statusIcon(p: PendingPrompt): string {
    switch (p.status) {
      case 'sending':
        return 'loading';
      case 'queued':
        return 'clock';
      case 'error':
        return 'error';
    }
  }
</script>

{#if prompts.length > 0}
  <div class="queued-prompts-bar" role="region" aria-label="Queued prompts">
    <div class="queued-header">
      <Icon name="clock" size={12} />
      <span>{prompts.length === 1 ? '1 prompt queued' : `${prompts.length} prompts queued`}</span>
      <span class="queued-hint">(will send at the next sub-turn halt)</span>
    </div>
    <ul class="queued-list">
      {#each prompts as prompt (prompt.id)}
        <li class="queued-item" class:error={prompt.status === 'error'}>
          <div class="queued-row">
            <span class="queued-status" class:queued={prompt.status === 'queued'} class:sending={prompt.status === 'sending'} class:errored={prompt.status === 'error'}>
              <Icon name={statusIcon(prompt)} size={12} />
              <span>{statusLabel(prompt)}</span>
            </span>
            <button
              type="button"
              class="dismiss"
              onclick={() => dismiss(prompt.id)}
              title="Cancel this queued prompt"
              aria-label="Cancel this queued prompt"
            >
              <Icon name="x" size={12} />
            </button>
          </div>
          <div class="queued-content">{prompt.content}</div>
        </li>
      {/each}
    </ul>
  </div>
{/if}

<style>
  .queued-prompts-bar {
    margin-bottom: var(--spacing-xs);
    padding: var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    max-height: 200px;
    overflow-y: auto;
  }

  .queued-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-bottom: var(--spacing-xs);
  }

  .queued-hint {
    color: var(--text-muted);
  }

  .queued-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .queued-item {
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: var(--spacing-xs) var(--spacing-sm);
  }

  .queued-item.error {
    border-color: var(--error);
  }

  .queued-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-xs);
    font-size: var(--font-size-xs);
  }

  .queued-status {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: var(--text-secondary);
  }
  .queued-status.queued { color: var(--accent-primary); }
  .queued-status.sending { color: var(--text-secondary); }
  .queued-status.errored { color: var(--error); }

  .queued-content {
    margin-top: 4px;
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .dismiss {
    background: transparent;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    padding: 2px;
    border-radius: var(--radius-sm);
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .dismiss:hover {
    color: var(--text-primary);
    background: var(--bg-elevated-2);
  }
</style>
