<script lang="ts">
  import type { ToolCall, WorkspaceArtifact } from '$lib/types';
  import { Collapsible, Icon } from '$lib/components/common';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { formatFileSize } from '$lib/utils/fileProcessing';
  import { getToolSummary } from '$lib/utils/toolSummary';
  import { formatMessageTime } from '$lib/utils/time';
  import { configStore } from '$lib/stores/config.svelte';
  import WorkspaceArtifactModal from './WorkspaceArtifactModal.svelte';
  import WorkspaceImage from './WorkspaceImage.svelte';

  interface Props {
    toolCall: ToolCall;
  }

  let { toolCall }: Props = $props();
  let modalArtifact = $state<WorkspaceArtifact | null>(null);

  // require_approval hold (backlog #77): the backend paused this call until
  // the user decides. The row clears via the hook_approval_resolved event.
  let approvalBusy = $state(false);
  let approvalError = $state('');

  async function resolveApproval(approved: boolean) {
    const recordId = toolCall.pendingApproval?.recordId;
    if (!recordId || approvalBusy) return;
    approvalBusy = true;
    approvalError = '';
    try {
      await api.resolveHookApproval(recordId, approved);
      // A 200 is authoritative: clear the bar locally so a dropped
      // hook_approval_resolved SSE event cannot strand disabled buttons
      // (the SSE clear then no-ops).
      chatStore.clearToolCallPendingApproval(recordId);
    } catch (e) {
      approvalError = humanizeErrorText(e, {
        action: approved ? 'approve' : 'decline',
        resource: 'the tool call'
      });
      approvalBusy = false;
    }
  }

  // Image artifacts render inline below the card (always visible, even when the
  // card is collapsed); non-image artifacts stay as chips inside the details.
  let imageArtifacts = $derived(
    (toolCall.artifacts ?? []).filter((a) => a.mimeType.startsWith('image/'))
  );

  let summary = $derived(
    configStore.describeToolCalls
      ? getToolSummary(toolCall.name, toolCall.arguments)
      : null
  );

  let duration = $derived.by(() => {
    // Prefer the server-measured execution time (tool_result.duration_ms);
    // the startTime/endTime diff includes network/queue latency.
    const ms =
      toolCall.durationMs ??
      (toolCall.startTime && toolCall.endTime
        ? toolCall.endTime.getTime() - toolCall.startTime.getTime()
        : null);
    if (ms === null || ms === undefined) return null;
    if (ms < 1000) return `${ms}ms`;
    const s = ms / 1000;
    return s < 10 ? `${s.toFixed(1)}s` : `${Math.round(s)}s`;
  });

  // Live elapsed clock while the tool runs; paired with the backend's kill
  // budget (tool_call.timeout_seconds) as "12s/300s" when known.
  let elapsedSeconds = $state(0);
  $effect(() => {
    if (toolCall.status !== 'running' || !toolCall.startTime) return;
    const start = toolCall.startTime.getTime();
    const tick = () => {
      elapsedSeconds = Math.max(0, Math.floor((Date.now() - start) / 1000));
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  });

  let runningTimer = $derived.by(() => {
    if (toolCall.status !== 'running' || !toolCall.startTime) return null;
    return toolCall.timeoutSeconds
      ? `${elapsedSeconds}s/${toolCall.timeoutSeconds}s`
      : `${elapsedSeconds}s`;
  });

  function formatArgs(args: Record<string, unknown>): string {
    try {
      return JSON.stringify(args, null, 2);
    } catch {
      return String(args);
    }
  }

  function formatResult(result: string | undefined): string {
    if (!result) return '';
    try {
      const parsed = JSON.parse(result);
      return JSON.stringify(parsed, null, 2);
    } catch {
      return result;
    }
  }

  function getArtifactIcon(mimeType: string): string {
    return mimeType.startsWith('image/') ? 'image' : 'fileText';
  }
</script>

<div class="tool-call-card" class:running={toolCall.status === 'running'} class:cancelled={toolCall.status === 'cancelled'}>
  <Collapsible title={toolCall.name} chevronIcon="terminal" chevronSize={20}>
    {#snippet header()}
      <div class="tool-header">
        <span class="tool-name">{toolCall.name}</span>
        {#if summary}
          <span class="tool-summary" title={summary}>
            <span class="tool-summary-paren">(</span>
            <span class="tool-summary-text">{summary}</span>
            <span class="tool-summary-paren">)</span>
          </span>
        {/if}
        {#if duration}
          <span class="duration-badge">{duration}</span>
        {:else if toolCall.status === 'cancelled'}
          <span class="duration-badge cancelled-badge">Cancelled</span>
        {:else if toolCall.pendingApproval}
          <span class="duration-badge approval-badge">Awaiting approval</span>
        {:else if runningTimer}
          <span class="duration-badge running-badge">{runningTimer}</span>
        {/if}
      </div>
    {/snippet}

    <div class="tool-details">
      <div class="detail-section">
        <h3 class="section-label">Arguments</h3>
        <pre class="code-block">{formatArgs(toolCall.arguments)}</pre>
      </div>

      {#if toolCall.result}
        <div class="detail-section">
          <h3 class="section-label">Result</h3>
          <pre class="code-block result" class:error={toolCall.status === 'error'}>
            {formatResult(toolCall.result)}
          </pre>
        </div>
      {/if}

      {#if toolCall.artifacts?.length}
        <div class="detail-section">
          <h3 class="section-label">Artifacts</h3>
          <div class="artifact-list">
            {#each toolCall.artifacts as artifact (artifact.path)}
              <button
                type="button"
                class="artifact-chip"
                onclick={() => { modalArtifact = artifact; }}
              >
                <Icon name={getArtifactIcon(artifact.mimeType)} size={20} />
                <span class="artifact-copy">
                  <span class="artifact-name">{artifact.name}</span>
                  <span class="artifact-meta">{formatFileSize(artifact.sizeBytes)} • {artifact.mimeType}</span>
                </span>
                <Icon name="chevronRight" size={18} />
              </button>
            {/each}
          </div>
        </div>
      {/if}

      {#if toolCall.startTime}
        <div class="timing">
          <span>Started: {formatMessageTime(toolCall.startTime)}</span>
          {#if toolCall.endTime}
            <span>
              Duration: {Math.round((toolCall.durationMs ?? (toolCall.endTime.getTime() - toolCall.startTime.getTime())) / 1000)}s
            </span>
          {/if}
        </div>
      {/if}
    </div>
  </Collapsible>

  {#if toolCall.pendingApproval}
    <!-- Always visible (outside the collapsible): a held call is time-boxed,
         so the decision must not hide behind an expand. No answer = deny. -->
    <div class="approval-bar">
      <div class="approval-copy">
        <Icon name="flag" size={16} />
        <span class="approval-prompt">
          {toolCall.pendingApproval.prompt || `Approve tool call ${toolCall.name}?`}
        </span>
      </div>
      <div class="approval-actions">
        <button
          type="button"
          class="approval-btn approve"
          disabled={approvalBusy}
          onclick={() => resolveApproval(true)}
        >
          Approve
        </button>
        <button
          type="button"
          class="approval-btn deny"
          disabled={approvalBusy}
          onclick={() => resolveApproval(false)}
        >
          Deny
        </button>
      </div>
      {#if approvalError}
        <p class="approval-error" role="alert">{approvalError}</p>
      {/if}
    </div>
  {/if}

  {#if imageArtifacts.length}
    <div class="tool-call-images">
      {#each imageArtifacts as artifact (artifact.path)}
        <WorkspaceImage {artifact} onClick={() => { modalArtifact = artifact; }} />
      {/each}
    </div>
  {/if}
</div>

<WorkspaceArtifactModal artifact={modalArtifact} onClose={() => { modalArtifact = null; }} />

<style>
  .tool-call-card {
    position: relative;
    background: var(--glass-bg);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    border-radius: var(--radius-md);
    border: 1px solid var(--glass-border);
    overflow: hidden;
    transition: border-color var(--transition-fast);
  }

  .tool-call-card.running {
    border-color: color-mix(in srgb, var(--accent-primary) 30%, var(--glass-border));
  }

  .tool-call-card.cancelled {
    border-color: color-mix(in srgb, var(--text-muted) 30%, var(--glass-border));
    opacity: 0.7;
  }

  .cancelled-badge {
    color: var(--text-muted);
    font-style: italic;
  }

  .approval-badge {
    color: var(--warning);
  }

  /* require_approval hold: decision bar pinned below the header, visible even
     while the card is collapsed (the hold is time-boxed; no answer = deny). */
  .approval-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-top: 1px solid var(--glass-border);
    background: color-mix(in srgb, var(--warning) 6%, var(--bg-elevated));
  }

  .approval-copy {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex: 1;
    min-width: 0;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .approval-prompt {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .approval-actions {
    display: flex;
    gap: var(--spacing-sm);
    flex-shrink: 0;
  }

  .approval-btn {
    min-height: var(--touch-target-min);
    padding: 4px 14px;
    border-radius: var(--radius-sm);
    border: 1px solid transparent;
    font-size: var(--font-size-sm);
    font-weight: 600;
    cursor: pointer;
    touch-action: manipulation;
    transition: background var(--transition-fast), border-color var(--transition-fast);
  }

  .approval-btn:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .approval-btn.approve {
    background: color-mix(in srgb, var(--success) 18%, var(--bg-elevated-2));
    border-color: color-mix(in srgb, var(--success) 45%, transparent);
    color: var(--text-primary);
  }

  .approval-btn.approve:active:not(:disabled) {
    background: color-mix(in srgb, var(--success) 30%, var(--bg-elevated-2));
  }

  .approval-btn.deny {
    background: color-mix(in srgb, var(--error) 14%, var(--bg-elevated-2));
    border-color: color-mix(in srgb, var(--error) 40%, transparent);
    color: var(--text-primary);
  }

  .approval-btn.deny:active:not(:disabled) {
    background: color-mix(in srgb, var(--error) 26%, var(--bg-elevated-2));
  }

  .approval-error {
    flex-basis: 100%;
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--error);
  }

  /* Gradient wave animation — accent-colored band sweeps left to right */
  .tool-call-card.running::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(
      90deg,
      transparent 0%,
      transparent 30%,
      var(--accent-primary) 50%,
      transparent 70%,
      transparent 100%
    );
    opacity: 0.1;
    transform: translateX(-100%);
    animation: toolWave 2s ease-in-out infinite;
    pointer-events: none;
  }

  @keyframes toolWave {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(100%); }
  }

  .tool-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex: 1;
    min-width: 0;
  }

  /* Reduce header height ~15% by tightening vertical padding */
  .tool-call-card :global(.header) {
    padding-top: 6px;
    padding-bottom: 6px;
  }

  .tool-call-card :global(.chevron) {
    color: var(--accent-primary);
  }

  .tool-name {
    font-weight: 500;
    color: var(--text-primary);
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
    flex-shrink: 0;
  }

  /* Plain-English label next to the tool name. Takes the middle space and
     ellipsis-truncates so the card header stays a single tidy line. */
  .tool-summary {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    /* Roomy line-height so descenders (g, y, p, q) are not clipped by the
       overflow:hidden text below. */
    line-height: 1.6;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    /* Soft italic aside so the label reads as a description, clearly distinct
       from the upright bold monospace tool name beside it. */
    font-style: italic;
    font-weight: 400;
  }

  /* Light theme: secondary and muted are both dark browns close in value, so
     the label reads heavier here than on the dark themes. Soften it by blending
     toward the card background for a lower-contrast aside. Light theme only. */
  :global(html[data-theme='light']) .tool-summary {
    color: color-mix(in srgb, var(--text-secondary) 60%, var(--bg-elevated));
  }

  /* Parentheses sit outside the truncating text so the closing ")" survives
     when the value is ellipsized at the card edge. */
  .tool-summary-paren {
    flex-shrink: 0;
  }

  .tool-summary-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .duration-badge {
    margin-left: auto;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    font-family: var(--font-mono);
  }

  .running-badge {
    /* Ticking elapsed/max clock: keep digits fixed-width so it doesn't jitter. */
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .tool-details {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .detail-section h3 {
    /* type role from global .section-label */
    margin: 0 0 var(--spacing-xs) 0;
  }

  .code-block {
    margin: 0;
    padding: var(--spacing-sm);
    background: var(--bg-elevated-2);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 200px;
    overflow-y: auto;
  }

  .code-block.result.error {
    border-left: 3px solid var(--error);
  }

  .timing {
    display: flex;
    gap: var(--spacing-md);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  /* Inline generated images, shown directly beneath the tool call card. */
  .tool-call-images {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm);
    border-top: 1px solid var(--glass-border);
  }

  .artifact-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .artifact-chip {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    min-height: var(--touch-target-min);
    text-align: left;
    padding: 0.75rem 0.8rem;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    cursor: pointer;
    touch-action: manipulation;
  }

  .artifact-chip:active {
    border-color: color-mix(in srgb, var(--accent-primary) 50%, var(--border-default));
    transform: scale(0.99);
  }

  .artifact-copy {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }

  .artifact-name {
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .artifact-meta {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
