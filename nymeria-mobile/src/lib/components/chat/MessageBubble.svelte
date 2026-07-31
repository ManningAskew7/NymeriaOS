<script lang="ts">
  import { SvelteSet } from 'svelte/reactivity';
  import type { Message, MessageStep, ToolCall, FileAttachment } from '$lib/types';
  import { Icon, ThinkingIndicator } from '$lib/components/common';
  import { formatFileSize, getFileExtension } from '$lib/utils/fileProcessing';
  import { formatMessageTime } from '$lib/utils/time';
  import { renderMarkdown, renderMarkdownStreaming } from '$lib/utils/markdown';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { errorsStore } from '$lib/stores/errors.svelte';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import { parseUserMessage } from '$lib/utils/messageParsing';
  import { hapticImpact } from '$lib/utils/haptics';
  import ToolCallCard from './ToolCallCard.svelte';
  import ThinkingBlock from './ThinkingBlock.svelte';
  import ImageModal from './ImageModal.svelte';

  interface Props {
    message: Message;
  }

  let { message }: Props = $props();

  // Patterns to detect AI compaction summary response (follows the compaction request)
  // These typically contain structured summary headers from the COMPACT_PROMPT
  // We check for multiple indicators to avoid false positives
  const AI_COMPACTION_PATTERNS = [
    /^\*\*1\.\s+What we were just doing\*\*/m,   // Numbered bold format
    /^##?\s+What we were just doing/mi,          // Markdown header format
    /^\*\*What we were just doing\*\*/m,         // Bold format at line start
  ];
  // Also check for the presence of multiple compaction-specific sections
  const COMPACTION_SECTION_KEYWORDS = [
    'what we were just doing',
    'key context',
    'files to read',
    'save persistent facts',
    'current state',
  ];

  // Parse assistant message to detect if it should be hidden (compaction summary response)
  function parseAssistantMessage(content: string): { hidden: boolean } {
    // Check if this is a compaction summary response (AI's response to COMPACT_PROMPT)
    // First, check for any of the specific header patterns
    for (const pattern of AI_COMPACTION_PATTERNS) {
      if (pattern.test(content)) {
        return { hidden: true };
      }
    }

    // Second, check if multiple compaction-specific section keywords appear
    // This catches summaries that use different formatting
    const lowerContent = content.toLowerCase();
    const matchingKeywords = COMPACTION_SECTION_KEYWORDS.filter(kw => lowerContent.includes(kw));
    if (matchingKeywords.length >= 3) {
      // If 3+ compaction section keywords appear, it's likely a compaction summary
      return { hidden: true };
    }

    return { hidden: false };
  }

  function thinkingContentFromSteps(steps: MessageStep[] | undefined): string {
    return (steps || [])
      .filter((step) => step.type === 'thinking')
      .map((step) => step.content || '')
      .join('');
  }

  function toolCallsFromSteps(steps: MessageStep[] | undefined): ToolCall[] {
    return (steps || [])
      .filter((step): step is MessageStep & { type: 'tool_call' } => step.type === 'tool_call')
      .map((step) => ({
        id: step.id || '',
        name: step.name || '',
        arguments: step.arguments || {},
        result: step.result,
        artifacts: step.artifacts,
        status: step.status || 'pending',
        startTime: step.startTime,
        endTime: step.endTime,
        durationMs: step.durationMs,
        timeoutSeconds: step.timeoutSeconds
      }));
  }

  // Computed: parsed user message (extracts summary if embedded in content)
  let parsedUserContent = $derived(
    message.role === 'user'
      ? parseUserMessage(message.content)
      : { text: message.content, contextSummary: null, hidden: false, isSmartwatch: false, isAutoCompact: false }
  );

  // Computed: parsed assistant message (checks if it should be hidden)
  let parsedAssistantContent = $derived(
    message.role === 'assistant'
      ? parseAssistantMessage(message.content || '')
      : { hidden: false }
  );

  // Should this entire message be hidden from the UI?
  // Never hide messages explicitly tagged as autonomous prompts (user opted in to see them)
  let isHiddenMessage = $derived(
    (message.role === 'user' && parsedUserContent.hidden && !message.autonomousSource) ||
    (message.role === 'assistant' && parsedAssistantContent.hidden)
  );


  let isUser = $derived(message.role === 'user');
  let isStreaming = $derived(message.status === 'streaming');
  let hasSteps = $derived(!!message.steps?.length);
  let fallbackIntermediateContent = $derived(
    message.intermediateContent || thinkingContentFromSteps(message.steps)
  );
  let fallbackToolCalls = $derived(
    message.toolCalls && message.toolCalls.length > 0
      ? message.toolCalls
      : toolCallsFromSteps(message.steps)
  );
  let hasToolCalls = $derived(!hasSteps && fallbackToolCalls.length > 0);
  let hasIntermediateContent = $derived(!hasSteps && !!fallbackIntermediateContent);
  // Support attachments field
  let allAttachments = $derived(message.attachments || []);
  let hasAttachments = $derived(allAttachments.length > 0);

  // Long-press on a user bubble opens the edit/rewind action sheet (backlog
  // #12, mobile's touch idiom for desktop's hover buttons). Suppressed while a
  // turn is streaming or queued (the backend refuses rewinds on a locked
  // thread, so the affordance goes away instead of failing) and on autonomous
  // wake-up prompts (their display text is not an editable user prompt).
  let canLongPressUser = $derived(
    isUser &&
      !isHiddenMessage &&
      !message.autonomousSource &&
      !chatStore.isStreaming &&
      !chatStore.isQueued
  );

  const LONG_PRESS_MS = 500;
  // Finger jitter during a deliberate stationary hold can fire pointermove;
  // only movement beyond this slop cancels the press (scrolls exceed it).
  const LONG_PRESS_SLOP_PX = 10;
  let longPressTimer: ReturnType<typeof setTimeout> | null = null;
  let pressOriginX = 0;
  let pressOriginY = 0;
  // Set true when the timer fires; used to swallow the click that a completed
  // long-press would otherwise deliver to an attachment button underneath.
  let longPressFired = false;

  function clearLongPress() {
    if (longPressTimer !== null) {
      clearTimeout(longPressTimer);
      longPressTimer = null;
    }
  }

  // A pending timer must not outlive the bubble (e.g. truncation mid-press):
  // it would fire against a stale message id.
  $effect(() => {
    return () => clearLongPress();
  });

  function handleBubblePointerDown(event: PointerEvent) {
    if (!canLongPressUser) return;
    longPressFired = false;
    clearLongPress();
    pressOriginX = event.clientX;
    pressOriginY = event.clientY;
    longPressTimer = setTimeout(() => {
      longPressTimer = null;
      longPressFired = true;
      void hapticImpact('medium');
      chatStore.openActionSheet(message.id);
    }, LONG_PRESS_MS);
  }

  // Real movement, lift, or scroll cancels an in-flight press. Attaching these
  // to the bubble container means a press that started on an attachment button
  // still cancels when the finger slides or the list scrolls.
  function handleBubblePressCancel() {
    clearLongPress();
  }

  function handleBubblePointerMove(event: PointerEvent) {
    if (longPressTimer === null) return;
    const dx = event.clientX - pressOriginX;
    const dy = event.clientY - pressOriginY;
    if (dx * dx + dy * dy > LONG_PRESS_SLOP_PX * LONG_PRESS_SLOP_PX) {
      clearLongPress();
    }
  }

  function handleBubbleClickCapture(event: MouseEvent) {
    if (longPressFired) {
      event.stopPropagation();
      event.preventDefault();
      longPressFired = false;
    }
  }

  // Modal state for image preview
  let modalFile = $state<FileAttachment | null>(null);

  // Tracks in-flight document downloads so a second tap on the same pill
  // doesn't double-fetch and the button can show a busy state.
  let downloadingIds = $state(new SvelteSet<string>());

  function openFileModal(file: FileAttachment) {
    if (file.type === 'image') {
      modalFile = file;
    }
  }

  function closeModal() {
    modalFile = null;
  }

  function getFileIcon(mimeType: string): string {
    if (mimeType.startsWith('image/')) return 'image';
    if (mimeType === 'application/pdf') return 'fileText';
    return 'fileText';
  }

  function providerLabel(provider?: string, model?: string): string {
    if (provider && model) return `${provider}/${model}`;
    return model || provider || 'provider';
  }

  function formatProviderDuration(seconds?: number | null): string {
    if (!seconds || seconds <= 0) return 'this turn';
    if (seconds >= 3600) {
      const hours = Math.round(seconds / 3600);
      return `${hours} hour${hours === 1 ? '' : 's'}`;
    }
    if (seconds >= 60) {
      const minutes = Math.round(seconds / 60);
      return `${minutes} minute${minutes === 1 ? '' : 's'}`;
    }
    return `${Math.round(seconds)} second${Math.round(seconds) === 1 ? '' : 's'}`;
  }

  function providerStatusText(step: MessageStep): string {
    if (step.providerStatus === 'retry') {
      const retryLabel = providerLabel(step.provider, step.model);
      const attempt = step.attempt && step.maxRetries
        ? ` (${step.attempt}/${step.maxRetries})`
        : '';
      const delay = step.delaySeconds && step.delaySeconds > 0
        ? ` in ${formatProviderDuration(step.delaySeconds)}`
        : ' now';
      const rewind = step.rewound ? ' Rewound to the last stable step.' : '';
      return `Provider error. Retrying ${retryLabel}${delay}${attempt}.${rewind}`;
    }
    const fallbackLabel = providerLabel(step.toProvider, step.toModel);
    const hold = step.permanent
      ? 'until reverted'
      : `for ${formatProviderDuration(step.holdSeconds)}`;
    const rewind = step.rewound ? ' Rewound to the last stable step.' : '';
    // A refusal swap is not a provider error: the model returned a clean
    // response with no content because its safety classifier flagged the
    // request (often a false positive). Keep the copy distinct from the
    // transport-failure fallback.
    if (step.reason === 'refusal') {
      const primaryLabel = providerLabel(step.fromProvider, step.fromModel);
      return `${primaryLabel} refused this turn (safety classifier, not an error). Using ${fallbackLabel} ${hold}.${rewind}`;
    }
    return `Using fallback ${fallbackLabel} ${hold}.${rewind}`;
  }

  async function downloadAttachment(file: FileAttachment) {
    const threadId = threadsStore.currentThreadId;
    if (!threadId) {
      console.warn('[MessageBubble] No active thread; cannot download attachment');
      return;
    }
    if (downloadingIds.has(file.id)) return;
    downloadingIds.add(file.id);
    try {
      const { blob, filename } = await api.downloadAttachment(threadId, file.id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename || file.name;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (error) {
      console.error('[MessageBubble] Attachment download failed:', error);
      // File chips have no inline error slot; surface through the toast layer.
      errorsStore.push({
        kind: 'generic',
        message: humanizeErrorText(error, { action: 'download', resource: 'the attachment' }),
      });
    } finally {
      downloadingIds.delete(file.id);
    }
  }

  // Check if content looks like raw JSON (hide it during streaming, will be parsed at end)
  let isRawJson = $derived(message.content?.trimStart().startsWith('{'));
  let showStreamingContent = $derived(isStreaming && message.content && !isRawJson);
  // Check if steps contain response steps (content is rendered there, not in bottom section)
  let hasResponseSteps = $derived(message.steps?.some(s => s.type === 'response') || false);
  // Check if the last step is NOT a response step (show dots during tool calls and thinking,
  // but not while response text is actively streaming with its own cursor indicator)
  let lastStepIsNotResponse = $derived(
    message.steps && message.steps.length > 0 &&
    message.steps[message.steps.length - 1].type !== 'response'
  );

  // Check if a step is the last step and actively streaming (show cursor after it)
  let streamingLastStepIndex = $derived(
    isStreaming && message.steps && message.steps.length > 0
      ? message.steps.length - 1
      : -1
  );
</script>

{#if message.kind === 'command_result'}
<div class="command-result">
  <div class="command-icon">
    <Icon name="terminal" size={18} />
  </div>
  <div class="command-body">
    {#if message.commandInput}
      <div class="command-input"><code>{message.commandInput}</code></div>
    {/if}
    <div class="command-output">
      {@html renderMarkdown(message.content)}
    </div>
  </div>
</div>
{:else if message.kind === 'compaction_notice'}
<div class="compaction-notice">
  <div class="compaction-icon">
    <Icon name="info" size={18} />
  </div>
  <div class="compaction-body">
    <div class="compaction-title">Context compacted</div>
    <div class="compaction-meta">
      {#if message.messagesRemoved}
        {message.messagesRemoved} messages summarized
      {:else}
        Older messages summarized
      {/if}
    </div>
    {#if message.contextSummary}
      <details class="context-summary-collapsible">
        <summary>View summary</summary>
        <div class="context-summary-content">
          {@html renderMarkdown(message.contextSummary)}
        </div>
      </details>
    {/if}
  </div>
</div>
{:else if message.kind === 'turn_rewound'}
<div class="compaction-notice turn-rewound-notice">
  <div class="compaction-icon">
    <Icon name="info" size={18} />
  </div>
  <div class="compaction-body">
    <div class="compaction-title">Turn rewound</div>
    <div class="compaction-meta">{message.content}</div>
  </div>
</div>
{:else if message.kind === 'fallback_notice'}
<!-- Note from history (llm-fallback-consent Phase 2): the runtime left a
     persisted note in the conversation saying the EFFECTIVE config differed
     from the requested one, and history re-emits it as this typed entry.
     `noteKind` says which: a model swap, or a per-thread endpoint the server
     refused to send its credential to (E10-01). -->
<div class="compaction-notice fallback-notice">
  <div class="compaction-icon">
    <Icon name={message.noteKind === 'destination' ? 'info' : 'refresh'} size={18} />
  </div>
  <div class="compaction-body">
    <div class="compaction-title">
      {message.noteKind === 'destination' ? 'Endpoint not used' : 'Model switch'}
    </div>
    <div class="compaction-meta">{message.content}</div>
  </div>
</div>
{:else if !isHiddenMessage}
<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
<div
  class="message-bubble"
  class:user={isUser}
  class:assistant={!isUser}
  class:autonomous-prompt={!!message.autonomousSource}
  onpointerdown={handleBubblePointerDown}
  onpointerup={handleBubblePressCancel}
  onpointermove={handleBubblePointerMove}
  onpointercancel={handleBubblePressCancel}
  onpointerleave={handleBubblePressCancel}
  onclickcapture={handleBubbleClickCapture}
>
  <!--
    Crit 4 — aria-busy silences AT browse-mode reads of mid-stream content
    on the assistant bubble. Cleared when status leaves 'streaming', after
    which the bubble is normal navigable content. ChatContainer's status
    live region handles the start/end announcements.
  -->
  <div class="bubble-content" aria-busy={!isUser && isStreaming ? 'true' : undefined}>
    {#if isUser}
      {#if hasAttachments}
        <div class="user-attachments">
          {#each allAttachments as file (file.id)}
            {#if file.type === 'image'}
              <button
                type="button"
                class="user-image-button"
                onclick={() => openFileModal(file)}
              >
                <img src={file.dataUrl} alt={file.name} />
              </button>
            {:else}
              <button
                type="button"
                class="user-document"
                onclick={() => downloadAttachment(file)}
                disabled={downloadingIds.has(file.id)}
              >
                <Icon name={getFileIcon(file.mimeType)} size={20} />
                <span class="doc-name">{file.name}</span>
                <span class="doc-ext">{getFileExtension(file.name)}</span>
                {#if downloadingIds.has(file.id)}
                  <span class="doc-spinner" aria-hidden="true">…</span>
                {:else}
                  <Icon name="download" size={14} />
                {/if}
              </button>
            {/if}
          {/each}
        </div>
      {/if}
      {#if parsedUserContent.isSmartwatch}
        <div class="autonomous-badge smartwatch-badge">
          <span class="autonomous-source-label">⌚ Smartwatch</span>
        </div>
      {:else if message.autonomousSource}
        <div class="autonomous-badge">
          <span class="autonomous-source-label">
            {#if message.autonomousSource === 'scheduler'}
              Scheduled Task
            {:else if message.autonomousSource === 'watchdog'}
              Watchdog Nudge
            {:else if message.autonomousSource === 'trigger'}
              Trigger
            {:else}
              Autonomous
            {/if}
          </span>
        </div>
      {/if}
      {#if parsedUserContent.text}
        <p class="user-text">{parsedUserContent.text}</p>
      {/if}
    {:else}
      <!-- Assistant message: render steps in arrival order for proper interleaving -->
      {#if message.dispatchInfo}
        <button
          type="button"
          class="dispatch-badge"
          onclick={() => message.dispatchInfo && threadsStore.selectThread(message.dispatchInfo.threadId)}
          aria-label={`Open thread: ${message.dispatchInfo.title || message.dispatchInfo.threadId}`}
        >
          <Icon name="info" size={14} />
          <span>Response from {message.dispatchInfo.title || message.dispatchInfo.threadId}</span>
          <span class="dispatch-jump"><Icon name="chevronRight" size={14} /></span>
        </button>
      {/if}

      {#if hasSteps}
        <!-- New: Render steps in order (thinking and tool_calls interleaved) -->
        {#each message.steps || [] as step, i (i)}
          {#if step.type === 'thinking' && step.content}
            <ThinkingBlock
              content={step.content}
              isActivelyStreaming={i === streamingLastStepIndex}
            />
          {:else if step.type === 'tool_call'}
            <div class="tool-calls">
              <ToolCallCard toolCall={{
                id: step.id || '',
                name: step.name || '',
                arguments: step.arguments || {},
                result: step.result,
                artifacts: step.artifacts,
                status: step.status || 'pending',
                startTime: step.startTime,
                endTime: step.endTime,
                durationMs: step.durationMs,
                timeoutSeconds: step.timeoutSeconds,
                pendingApproval: step.pendingApproval
              }} />
            </div>
          {:else if step.type === 'provider_status'}
            <div class="provider-status-step">
              <Icon name={step.providerStatus === 'fallback' ? 'tool' : 'refresh'} size={14} />
              <span>{providerStatusText(step)}</span>
            </div>
          {:else if step.type === 'response' && step.content}
            <div class="message-content">
              <div class="markdown-content">
                {@html i === streamingLastStepIndex ? renderMarkdownStreaming(step.content) : renderMarkdown(step.content)}
              </div>
            </div>
          {/if}
        {/each}
      {:else}
        <!-- Legacy fallback: render old-style intermediate -> tools -> content -->
        {#if hasIntermediateContent}
          <div class="intermediate-content">
            <div class="markdown-content">
              {@html renderMarkdown(fallbackIntermediateContent)}
            </div>
          </div>
        {/if}

        {#if hasToolCalls}
          <div class="tool-calls">
            {#each fallbackToolCalls as toolCall (toolCall.id)}
              <ToolCallCard {toolCall} />
            {/each}
          </div>
        {/if}
      {/if}

      <!-- Final content at bottom (after all steps) -->
      {#if isStreaming && !message.content && !hasSteps && !hasToolCalls && !hasIntermediateContent}
        <!-- Only show streaming indicator if nothing else is visible -->
        <ThinkingIndicator />
      {:else if showStreamingContent && !hasResponseSteps}
        <!-- Show streaming content only if it's not raw JSON and not already in response steps -->
        <div class="message-content">
          <div class="markdown-content">
            {@html renderMarkdownStreaming(message.content)}
          </div>
        </div>
      {:else if isStreaming && lastStepIsNotResponse}
        <!-- Show activity indicator while streaming (tool calls, thinking) but not during response text -->
        <div class="generating-indicator">
          <span class="dot"></span>
          <span class="dot"></span>
          <span class="dot"></span>
        </div>
      {:else if message.content && !hasResponseSteps}
        <!-- Legacy fallback: render message.content only if not already in response steps -->
        <div class="message-content">
          <div class="markdown-content">
            {@html renderMarkdown(message.content)}
          </div>
        </div>
      {/if}
    {/if}
  </div>

  <time class="timestamp">
    {formatMessageTime(message.timestamp)}
  </time>
</div>
{/if}

<ImageModal image={modalFile} onClose={closeModal} />

<style>
  .compaction-notice {
    align-self: center;
    display: flex;
    gap: var(--spacing-sm);
    width: 100%;
    margin: var(--spacing-sm) 0 var(--spacing-md);
    padding: var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--bg-elevated) 88%, var(--accent-primary));
    color: var(--text-primary);
    animation: msgIn var(--transition-normal);
  }

  .command-result {
    align-self: center;
    display: flex;
    gap: var(--spacing-sm);
    width: min(100%, 720px);
    margin: var(--spacing-md) 0;
    padding: var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-primary);
    animation: msgIn var(--transition-normal);
  }

  .command-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: var(--radius-sm);
    color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 14%, transparent);
    flex-shrink: 0;
  }

  .command-body {
    flex: 1;
    min-width: 0;
  }

  .command-input {
    margin-bottom: var(--spacing-sm);
    color: var(--text-muted);
  }

  .command-input code {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
  }

  .command-output :global(:first-child) {
    margin-top: 0;
  }

  .command-output :global(:last-child) {
    margin-bottom: 0;
  }

  .compaction-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: var(--radius-sm);
    color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 14%, transparent);
    flex-shrink: 0;
  }

  .compaction-body {
    flex: 1;
    min-width: 0;
  }

  .compaction-title {
    font-weight: 600;
    font-size: var(--font-size-sm);
  }

  .compaction-meta {
    margin-top: 2px;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .message-bubble {
    display: flex;
    flex-direction: column;
    max-width: 85%;
    margin-bottom: var(--spacing-md);
    animation: msgIn var(--transition-normal);
  }

  .message-bubble.user {
    align-self: flex-end;
  }

  .message-bubble.assistant {
    align-self: flex-start;
  }

  .bubble-content {
    padding: var(--spacing-md);
    border-radius: var(--radius-lg);
    line-height: 1.6;
  }

  .user .bubble-content {
    background: var(--bubble-user);
    border-bottom-right-radius: var(--radius-sm);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  }

  .autonomous-prompt .bubble-content {
    background: color-mix(in srgb, var(--accent-secondary) 10%, var(--bg-elevated));
    border: 1px dashed color-mix(in srgb, var(--accent-secondary) 50%, transparent);
    border-bottom-right-radius: var(--radius-sm);
    box-shadow: none;
  }

  .autonomous-badge {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    margin-bottom: var(--spacing-sm);
    padding-bottom: var(--spacing-xs);
    border-bottom: 1px solid color-mix(in srgb, var(--accent-secondary) 25%, transparent);
  }

  .autonomous-source-label {
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--accent-secondary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .smartwatch-badge .autonomous-source-label {
    color: var(--accent-primary);
  }

  .smartwatch-badge {
    border-bottom-color: color-mix(in srgb, var(--accent-primary) 25%, transparent);
  }

  .dispatch-badge {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    width: 100%;
    margin-bottom: var(--spacing-sm);
    padding: 0 0 var(--spacing-xs);
    background: none;
    border: none;
    border-bottom: 1px solid color-mix(in srgb, var(--accent-primary) 25%, transparent);
    color: var(--accent-primary);
    font-family: inherit;
    font-size: var(--font-size-xs);
    font-weight: 600;
    text-align: left;
    cursor: pointer;
    transition: color var(--transition-fast), border-color var(--transition-fast);
  }

  .dispatch-badge:hover {
    color: color-mix(in srgb, var(--accent-primary) 82%, var(--text-secondary));
    border-bottom-color: color-mix(in srgb, var(--accent-primary) 50%, transparent);
  }

  .dispatch-badge:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
    border-radius: var(--radius-sm);
  }

  .dispatch-jump {
    display: inline-flex;
    align-items: center;
    margin-left: auto;
    opacity: 0.65;
    transition: opacity var(--transition-fast);
  }

  .dispatch-badge:hover .dispatch-jump {
    opacity: 1;
  }

  .provider-status-step {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    margin: var(--spacing-xs) 0 var(--spacing-sm);
    padding-left: var(--spacing-sm);
    border-left: 2px solid color-mix(in srgb, var(--accent-primary) 45%, transparent);
    color: var(--text-muted);
    font-size: var(--font-size-xs);
    line-height: 1.4;
  }

  .provider-status-step :global(.icon) {
    color: var(--accent-primary);
    flex-shrink: 0;
  }

  .assistant .bubble-content {
    background: var(--glass-bg);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    border: 1px solid var(--glass-border);
    border-bottom-left-radius: var(--radius-sm);
  }

  .user-text {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .user-attachments {
    display: flex;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
    margin-bottom: var(--spacing-sm);
  }

  .user-attachments:last-child {
    margin-bottom: 0;
  }

  .user-image-button {
    display: block;
    padding: 0;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    overflow: hidden;
    cursor: pointer;
    background: transparent;
    transition: border-color var(--transition-fast);
    max-width: 200px;
    max-height: 200px;
  }

  .user-image-button:hover {
    border-color: var(--accent-primary);
  }

  .user-image-button img {
    display: block;
    max-width: 100%;
    max-height: 200px;
    object-fit: contain;
  }

  .user-document {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    max-width: 240px;
    cursor: pointer;
    transition: border-color var(--transition-fast), background var(--transition-fast);
    font-family: inherit;
  }

  .user-document:active:not(:disabled) {
    border-color: var(--accent-primary);
    background: var(--bg-elevated-2);
  }

  .user-document:disabled {
    cursor: wait;
    opacity: 0.7;
  }

  .doc-spinner {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .user-document .doc-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .user-document .doc-ext {
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    flex-shrink: 0;
  }

  .markdown-content {
    color: var(--text-primary);
  }

  .markdown-content :global(p) {
    margin: 0 0 var(--spacing-sm) 0;
  }

  .markdown-content :global(p:last-child) {
    margin-bottom: 0;
  }

  .markdown-content :global(pre) {
    margin: var(--spacing-sm) 0;
    padding: var(--spacing-md);
    background: var(--bg-elevated-2);
    border-radius: var(--radius-md);
    overflow-x: auto;
  }

  .markdown-content :global(code) {
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
  }

  .markdown-content :global(ul),
  .markdown-content :global(ol) {
    margin: var(--spacing-sm) 0;
    padding-left: var(--spacing-lg);
  }

  .markdown-content :global(li) {
    margin-bottom: var(--spacing-xs);
  }

  .markdown-content :global(a) {
    color: var(--accent-secondary);
  }

  .markdown-content :global(blockquote) {
    margin: var(--spacing-sm) 0;
    padding-left: var(--spacing-md);
    border-left: 3px solid var(--accent-primary);
    color: var(--text-secondary);
  }

  /* Images: constrain to the bubble so a wide image scales down instead of
     overflowing on narrow viewports (AI-UI §10). */
  .markdown-content :global(img) {
    max-width: 100%;
    height: auto;
  }

  /* Tables */
  .markdown-content :global(.md-table-wrap) {
    max-width: 100%;
    overflow-x: auto;
  }

  .markdown-content :global(table) {
    border-collapse: collapse;
    width: 100%;
    margin: var(--spacing-sm) 0;
    font-size: var(--font-size-sm);
  }

  .markdown-content :global(th) {
    font-weight: 600;
    padding: var(--spacing-xs) var(--spacing-sm);
    border-bottom: 2px solid var(--border-subtle);
    text-align: left;
    color: var(--text-primary);
  }

  .markdown-content :global(td) {
    padding: var(--spacing-xs) var(--spacing-sm);
    border-bottom: 1px solid var(--border-subtle);
  }

  .markdown-content :global(tr:last-child td) {
    border-bottom: none;
  }

  /* Headings */
  .markdown-content :global(h1),
  .markdown-content :global(h2),
  .markdown-content :global(h3),
  .markdown-content :global(h4) {
    margin: var(--spacing-md) 0 var(--spacing-sm) 0;
    font-weight: 600;
    color: var(--text-primary);
  }

  .markdown-content :global(h1:first-child),
  .markdown-content :global(h2:first-child),
  .markdown-content :global(h3:first-child),
  .markdown-content :global(h4:first-child) {
    margin-top: 0;
  }

  .markdown-content :global(h1) {
    font-size: 1.5em;
  }

  .markdown-content :global(h2) {
    font-size: 1.3em;
  }

  .markdown-content :global(h3) {
    font-size: 1.1em;
  }

  /* Horizontal rules */
  .markdown-content :global(hr) {
    border: none;
    border-top: 1px solid var(--border-subtle);
    margin: var(--spacing-md) 0;
    opacity: 0.6;
  }

  .intermediate-content {
    margin-bottom: var(--spacing-sm);
    color: var(--text-secondary);
    font-style: italic;
  }

  /* Separator appears AFTER tool calls, grouping thinking with its tool */
  .tool-calls {
    margin-top: var(--spacing-sm);
    margin-bottom: var(--spacing-md);
    padding-bottom: var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .message-content {
    margin-top: var(--spacing-sm);
  }

  /* Injected via {@html} so must be :global to bypass scoping */
  .markdown-content :global(.streaming-cursor) {
    display: inline-block;
    width: 2px;
    height: 1.1em;
    background: var(--accent-primary);
    margin-left: 2px;
    vertical-align: text-bottom;
    animation: cursorBlink 0.8s ease-in-out infinite;
  }

  @keyframes cursorBlink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0; }
  }

  .generating-indicator {
    display: flex;
    gap: 4px;
    padding: var(--spacing-sm) 0;
  }

  .generating-indicator .dot {
    width: 6px;
    height: 6px;
    background: var(--text-muted);
    border-radius: 50%;
    animation: dotBounce 1.4s infinite ease-in-out both;
  }

  .generating-indicator .dot:nth-child(1) {
    animation-delay: -0.32s;
  }

  .generating-indicator .dot:nth-child(2) {
    animation-delay: -0.16s;
  }

  @keyframes dotBounce {
    0%, 80%, 100% {
      transform: scale(0.6);
      opacity: 0.4;
    }
    40% {
      transform: scale(1);
      opacity: 1;
    }
  }


  /* Text style (size/weight/color) is the global .timestamp utility in app.css;
     only the placement is owned here. */
  .timestamp {
    margin-top: var(--spacing-xs);
    padding: 0 var(--spacing-sm);
  }

  .user .timestamp {
    text-align: right;
  }

  /* Intentional variant of the global slideUp: adds a scale-in from 0.98. */
  @keyframes msgIn {
    from {
      opacity: 0;
      transform: translateY(10px) scale(0.98);
    }
    to {
      opacity: 1;
      transform: translateY(0) scale(1);
    }
  }

  /* Collapsible Context Summary */
  .context-summary-collapsible {
    margin-top: var(--spacing-sm);
    padding-top: var(--spacing-sm);
    border-top: 1px solid var(--border-subtle);
  }

  .context-summary-collapsible summary {
    cursor: pointer;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    user-select: none;
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .context-summary-collapsible summary:hover {
    color: var(--accent-primary);
  }

  .context-summary-collapsible summary::marker {
    color: var(--text-muted);
  }

  .context-summary-collapsible[open] summary {
    margin-bottom: var(--spacing-sm);
  }

  .context-summary-content {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    background: var(--bg-elevated-2);
    padding: var(--spacing-sm);
    border-radius: var(--radius-md);
    max-height: 300px;
    overflow-y: auto;
  }

  .context-summary-content :global(h1),
  .context-summary-content :global(h2),
  .context-summary-content :global(h3) {
    font-size: var(--font-size-sm);
    font-weight: 600;
    margin: var(--spacing-sm) 0 var(--spacing-xs) 0;
  }

  .context-summary-content :global(h1:first-child),
  .context-summary-content :global(h2:first-child),
  .context-summary-content :global(h3:first-child) {
    margin-top: 0;
  }

  .context-summary-content :global(p) {
    margin: var(--spacing-xs) 0;
  }

  .context-summary-content :global(ul),
  .context-summary-content :global(ol) {
    margin: var(--spacing-xs) 0;
    padding-left: var(--spacing-lg);
  }
</style>
