<script lang="ts">
  import type { TurnPausedInfo } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';

  interface Props {
    info: TurnPausedInfo;
  }

  let { info }: Props = $props();

  const isRepeatedLoop = $derived(info.reason === 'repeated_tool_result');
  const isSubAgent = $derived(info.scope === 'sub_agent');

  const labelText = $derived(
    isSubAgent
      ? `${info.agentName || 'Sub-agent'} ${
          isRepeatedLoop ? 'stopped a repeated tool loop' : 'hit its iteration limit'
        }`
      : isRepeatedLoop
        ? 'Repeated tool loop stopped'
        : 'Turn paused at the iteration limit'
  );

  const metaText = $derived(
    isRepeatedLoop && info.repeatedToolName
      ? `${info.repeatedToolName}${info.repeatedCount ? ` x${info.repeatedCount}` : ''}`
      : typeof info.toolCallCount === 'number'
        ? `${info.toolCallCount}/${info.maxIterations} steps`
        : `${info.maxIterations} steps`
  );

  // The button renders only for backend-confirmed resumable halts (graceful
  // main-agent cap halts) that have not been resumed yet; it disables while
  // any turn is streaming so a double click cannot race the re-drive.
  const showResume = $derived(info.resumable && !info.resumed);
  const resumeBusy = $derived(chatStore.isStreaming);

  function handleResume() {
    if (resumeBusy || info.resumed) return;
    chatStore.requestResume();
  }
</script>

<div class="paused-message" class:resumed={info.resumed}>
  <div class="paused-header">
    <span class="icon">
      <Icon name={info.resumed ? 'play' : 'pause'} size={14} />
    </span>
    <span class="label">{labelText}</span>
    <span class="meta">{metaText}</span>
    {#if info.resumed}
      <span class="resumed-chip">Resumed</span>
    {/if}
  </div>
  <div class="paused-text">{info.content}</div>
  {#if showResume}
    <div class="paused-actions">
      <button
        type="button"
        class="resume-btn"
        onclick={handleResume}
        disabled={resumeBusy}
        aria-label="Resume the paused turn"
      >
        <Icon name="play" size={12} />
        Resume
      </button>
    </div>
  {/if}
</div>

<style>
  .paused-message {
    align-self: flex-start;
    display: flex;
    flex-direction: column;
    gap: 8px;
    width: min(760px, 88%);
    margin: var(--spacing-xs) 0 var(--spacing-md);
    padding: 10px 12px;
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--bg-elevated) 88%, var(--warning, var(--accent-primary)));
    border: 1px solid var(--border-subtle);
    color: var(--text-secondary);
    animation: fadeSlide var(--transition-fast);
  }

  .paused-message.resumed {
    background: color-mix(in srgb, var(--bg-elevated) 92%, var(--accent-primary));
  }

  .paused-header {
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
  }

  .icon {
    display: flex;
    align-items: center;
    color: var(--warning, var(--accent-primary));
  }

  .resumed .icon {
    color: var(--accent-primary);
  }

  .label {
    font-weight: 600;
    font-size: var(--font-size-xs);
    color: var(--text-primary);
  }

  .meta {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .resumed-chip {
    margin-left: auto;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--accent-primary);
  }

  .paused-text {
    font-size: var(--font-size-xs);
    line-height: 1.5;
    color: var(--text-secondary);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .paused-actions {
    display: flex;
  }

  .resume-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 12px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-xs);
    font-weight: 600;
    cursor: pointer;
    transition: background var(--transition-fast), border-color var(--transition-fast);
  }

  .resume-btn:hover:not(:disabled) {
    background: color-mix(in srgb, var(--bg-elevated) 80%, var(--accent-primary));
    border-color: var(--accent-primary);
  }

  .resume-btn:disabled {
    opacity: 0.55;
    cursor: default;
  }

  @keyframes fadeSlide {
    from {
      opacity: 0;
      transform: translateY(-4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>
