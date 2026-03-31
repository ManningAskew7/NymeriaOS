<script lang="ts">
  import { outlookStore } from '$lib/stores/outlook.svelte';

  interface Props {
    onAction: (message: string) => void;
    disabled?: boolean;
  }

  let { onAction, disabled = false }: Props = $props();

  const actions = [
    {
      label: 'Process RFQ',
      icon: '>>',
      prompt: (id: string) =>
        `Process RFQ from email ID: ${id}. Follow the full pipeline.`,
    },
    {
      label: 'Analyse Response',
      icon: '$',
      prompt: (id: string) =>
        `Analyse supplier response from email ID: ${id}. Match to original RFQ, extract pricing and lead times, compare with other responses if available.`,
    },
    {
      label: 'Check Parts',
      icon: '?',
      prompt: (id: string) =>
        `Quick parts check on email ID: ${id}. Validate part numbers and check lifecycle status only. No supplier RFQ needed, no tracker logging.`,
    },
  ];

  function handleClick(promptFn: (id: string) => string) {
    const emailId = outlookStore.currentEmailId;
    if (!emailId) return;
    onAction(promptFn(emailId));
  }
</script>

{#if outlookStore.currentEmailId}
  <div class="quick-actions">
    <div class="qa-label">
      {outlookStore.currentEmailSubject
        ? `Selected: ${outlookStore.currentEmailSubject.length > 40
            ? outlookStore.currentEmailSubject.slice(0, 37) + '...'
            : outlookStore.currentEmailSubject}`
        : 'Email selected'}
    </div>
    <div class="qa-buttons">
      {#each actions as action}
        <button
          class="qa-btn"
          onclick={() => handleClick(action.prompt)}
          disabled={disabled}
          title={action.label}
        >
          <span class="qa-icon">{action.icon}</span>
          <span class="qa-text">{action.label}</span>
        </button>
      {/each}
    </div>
  </div>
{/if}

<style>
  .quick-actions {
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
  }

  .qa-label {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin-bottom: var(--spacing-xs);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .qa-buttons {
    display: flex;
    gap: var(--spacing-xs);
    flex-wrap: wrap;
  }

  .qa-btn {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: 6px 10px;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    font-size: var(--font-size-xs);
    cursor: pointer;
    transition: all 0.15s ease;
    white-space: nowrap;
  }

  .qa-btn:hover:not(:disabled) {
    background: var(--accent-primary);
    color: var(--bg-base);
    border-color: var(--accent-primary);
  }

  .qa-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .qa-icon {
    font-weight: 700;
    font-size: var(--font-size-sm);
  }
</style>
