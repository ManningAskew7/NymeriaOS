<script lang="ts">
  import { outlookStore } from '$lib/stores/outlook.svelte';

  interface Props {
    onAction: (message: string) => void;
    onInsert?: (text: string) => void;
    disabled?: boolean;
  }

  let { onAction, onInsert, disabled = false }: Props = $props();

  const actions = [
    {
      label: 'Process RFQ',
      icon: '>>',
      prompt: (ctx: string) =>
        `Process RFQ from the email I'm currently viewing. Find it using the details below, then follow the full pipeline.\n\n${ctx}`,
    },
    {
      label: 'Analyse Response',
      icon: '$',
      prompt: (ctx: string) =>
        `Analyse supplier response from the email I'm currently viewing. Find it using the details below, then match to original RFQ, extract pricing and lead times, compare with other responses if available.\n\n${ctx}`,
    },
    {
      label: 'Check Parts',
      icon: '?',
      prompt: (ctx: string) =>
        `Quick parts check on the email I'm currently viewing. Find it using the details below, then validate part numbers and check lifecycle status only. No supplier RFQ needed, no tracker logging.\n\n${ctx}`,
    },
  ];

  function handleClick(promptFn: (ctx: string) => string) {
    if (!outlookStore.hasEmail) return;
    const ctx = outlookStore.getEmailContext();
    onAction(promptFn(ctx));
  }

  function handleInsertRef() {
    if (!outlookStore.hasEmail || !onInsert) return;
    const ctx = outlookStore.getEmailContext();
    onInsert(`\n\n${ctx}\n`);
  }
</script>

{#if outlookStore.isOutlook}
  <div class="quick-actions">
    {#if outlookStore.hasEmail}
      <div class="qa-label">
        {outlookStore.currentEmailSubject
          ? `Selected: ${outlookStore.currentEmailSubject.length > 40
              ? outlookStore.currentEmailSubject.slice(0, 37) + '…'
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
        <button
          class="qa-btn qa-btn-ref"
          onclick={handleInsertRef}
          disabled={disabled}
          title="Insert email reference into message"
        >
          <span class="qa-icon">@</span>
          <span class="qa-text">Ref Email</span>
        </button>
      </div>
    {:else}
      <div class="qa-hint">Select an email to use quick actions</div>
      <div class="qa-buttons">
        {#each actions as action}
          <button
            class="qa-btn"
            disabled
            title="Select an email first"
          >
            <span class="qa-icon">{action.icon}</span>
            <span class="qa-text">{action.label}</span>
          </button>
        {/each}
        <button
          class="qa-btn qa-btn-ref"
          disabled
          title="Select an email first"
        >
          <span class="qa-icon">@</span>
          <span class="qa-text">Ref Email</span>
        </button>
      </div>
    {/if}
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

  .qa-btn-ref {
    border-style: dashed;
  }

  .qa-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin-bottom: var(--spacing-xs);
    font-style: italic;
  }
</style>
