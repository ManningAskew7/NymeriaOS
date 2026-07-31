<script lang="ts">
  import { SvelteSet } from 'svelte/reactivity';
  import type { Message, MessageStep, FileAttachment } from '$lib/types';
  import { Button, Icon, Modal } from '$lib/components/common';
  import { computeVisibleBlastRadius, rewindToMessage } from '$lib/utils/rewind';
  import { formatFileSize, getFileExtension } from '$lib/utils/fileProcessing';
  import { formatMessageTime } from '$lib/utils/time';
  import { renderMarkdown, renderMarkdownStreaming } from '$lib/utils/markdown';
  import { messageToMarkdown, messageToResponseText } from '$lib/utils/messageToMarkdown';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { errorsStore } from '$lib/stores/errors.svelte';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import ToolCallCard from './ToolCallCard.svelte';
  import ThinkingBlock from './ThinkingBlock.svelte';
  import AgentActivityIndicator from './AgentActivityIndicator.svelte';
  import SkillCard from '../skills/SkillCard.svelte';
  import ImageModal from './ImageModal.svelte';

  interface Props {
    message: Message;
  }

  let { message }: Props = $props();

  // Pattern to detect time context prefix (added by backend to all messages)
  // Format: [Current Time: ...]\n[Trigger: ...]\n\n{actual message}
  const TIME_CONTEXT_PATTERN = /^\[(?:Current )?Time:[^\]]+\]\n\[Trigger:[^\]]+\]\n\n/;

  // Pattern to detect smartwatch trigger source
  const SMARTWATCH_TRIGGER_PATTERN = /\[Trigger: Smartwatch[^\]]*\]/;

  // Pattern to detect autonomous wake-up messages (internal system triggers - should be hidden)
  // Format: [Current Time: ...]\n[Trigger: Autonomous Wake-up...]\n\nWork on TODO ...
  const AUTONOMOUS_WAKEUP_PATTERN = /^\[(?:Current )?Time:[^\]]+\]\n\[Trigger: Autonomous Wake-up[^\]]*\]\n\n/;

  // Pattern to detect compaction system request (should be hidden)
  const COMPACTION_REQUEST_PATTERN = /^\*\*System Request: Context Compaction\*\*/;

  // Pattern to detect manual /compact context summary suffix
  // Format: {user message}\n\n---\n*This conversation is resuming...*\n\n{summary}\n\n---
  const MANUAL_COMPACT_PATTERN = /\n\n---\n\*This conversation is resuming from a previous session that exceeded context limits\. Summary of prior context:\*\n\n([\s\S]*?)\n\n---$/;

  // Pattern to detect auto-compact message
  // Format: [Auto-compact: ...]\n\n---\n*Context Summary (auto-compact):*\n\n{summary}\n\n---\n\nContinue...
  const AUTO_COMPACT_PATTERN = /^\[Auto-compact: Context limit reached, conversation summarized\]\n\n---\n\*Context Summary \(auto-compact\):\*\n\n([\s\S]*?)\n\n---\n\n[\s\S]*$/;

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

  // Parse user message to extract actual content, context summary, and hidden flag
  function parseUserMessage(content: string): { text: string; contextSummary: string | null; hidden: boolean; isSmartwatch: boolean; isAutoCompact: boolean } {
    let text = content;
    let contextSummary: string | null = null;

    // Detect smartwatch trigger before stripping metadata
    const isSmartwatch = SMARTWATCH_TRIGGER_PATTERN.test(text);

    // Check if this is an autonomous wake-up message (should be hidden entirely)
    if (AUTONOMOUS_WAKEUP_PATTERN.test(text)) {
      return { text: '', contextSummary: null, hidden: true, isSmartwatch: false, isAutoCompact: false };
    }

    // Check if this is a compaction system request (should be hidden entirely)
    if (COMPACTION_REQUEST_PATTERN.test(text)) {
      return { text: '', contextSummary: null, hidden: true, isSmartwatch: false, isAutoCompact: false };
    }

    // Strip time context prefix unless user opted to show metadata
    const tid = threadsStore.currentThreadId;
    const showMeta = tid ? threadConfigStore.getConfig(tid)?.showPromptMetadata : false;
    if (!showMeta) {
      text = text.replace(TIME_CONTEXT_PATTERN, '');
    }

    // Check for auto-compact message format
    const autoCompactMatch = text.match(AUTO_COMPACT_PATTERN);
    if (autoCompactMatch) {
      contextSummary = autoCompactMatch[1].trim();
      text = '[Auto-compact: Thread summarized]';
      return { text, contextSummary, hidden: false, isSmartwatch, isAutoCompact: true };
    }

    // Check for manual /compact summary suffix
    const manualCompactMatch = text.match(MANUAL_COMPACT_PATTERN);
    if (manualCompactMatch) {
      contextSummary = manualCompactMatch[1].trim();
      text = text.replace(MANUAL_COMPACT_PATTERN, '').trim();
    }

    return { text, contextSummary, hidden: false, isSmartwatch, isAutoCompact: false };
  }

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
  let hasSteps = $derived(message.steps && message.steps.length > 0);
  // Legacy fallbacks for messages without steps array
  let hasToolCalls = $derived(message.toolCalls && message.toolCalls.length > 0);
  let hasIntermediateContent = $derived(!!message.intermediateContent);
  // Support attachments field
  let allAttachments = $derived(message.attachments || []);
  let hasAttachments = $derived(allAttachments.length > 0);

  // Modal state for image preview
  let modalFile = $state<FileAttachment | null>(null);

  // Tracks in-flight document downloads so a second click on the same pill
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

  const HOOK_EVENT_LABELS: Record<string, string> = {
    prompt_submit: 'prompt',
    pre_tool_use: 'pre-tool',
    post_tool_use: 'post-tool',
    done: 'done',
  };

  // One-line summary for an ephemeral lifecycle-hook activity step. Reads like a
  // Claude Code hook line: "Hook <name>: <what it did>", with the tool in
  // parentheses when the hook fired around a tool call.
  function hookActivityText(step: MessageStep): string {
    const name = step.hookName || 'hook';
    const where = step.hookEvent ? (HOOK_EVENT_LABELS[step.hookEvent] || step.hookEvent) : '';
    const tool = step.hookToolName ? ` (${step.hookToolName})` : '';
    const isFault = step.hookStatus && step.hookStatus !== 'ok';
    const detail = isFault
      ? `${step.hookStatus}${step.hookDetail ? `: ${step.hookDetail}` : ''}`
      : (step.hookDetail || where || 'ran');
    return `Hook ${name}${tool}: ${detail}`;
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
  // Check if the last step is NOT a response step (show activity text during tool calls and thinking).
  let lastStepIsNotResponse = $derived(
    message.steps && message.steps.length > 0 &&
    message.steps[message.steps.length - 1].type !== 'response'
  );
  let lastStepIsResponse = $derived(
    message.steps && message.steps.length > 0 &&
    message.steps[message.steps.length - 1].type === 'response'
  );
  let showActivityAfterResponse = $derived(
    isStreaming &&
    lastStepIsResponse &&
    message.activityPhase === 'formulating'
  );

  // Check if a step is the last step and actively streaming.
  let streamingLastStepIndex = $derived(
    isStreaming && message.steps && message.steps.length > 0
      ? message.steps.length - 1
      : -1
  );

  // Action buttons (copy + report) — only on completed assistant messages
  let showActions = $derived(!isUser && !isStreaming && !isHiddenMessage);

  // User-bubble actions (edit and resend / rewind to here, backlog #12).
  // Hidden while a turn is streaming or queued (the backend refuses rewinds
  // on a locked thread, so the affordance disappears instead of failing) and
  // on autonomous wake-up prompts (their text is not an editable user prompt).
  let showUserActions = $derived(
    isUser &&
      !isHiddenMessage &&
      !message.autonomousSource &&
      !chatStore.isStreaming &&
      !chatStore.isQueued
  );

  function handleEditMessage() {
    // Re-attach image attachments on resend: rewinding drops them from model
    // context, unlike document files which persist in the thread sandbox.
    // Skip images whose dataUrl did not survive serialization.
    const images = (message.attachments || []).filter(
      (file) => file.type === 'image' && file.dataUrl
    );
    chatStore.beginEdit(message.id, parsedUserContent.text, images);
  }

  // Rewind-to-here confirm flow (destructive: needs the house confirm modal).
  let rewindConfirmOpen = $state(false);
  let rewindBusy = $state(false);
  let rewindError = $state<string | null>(null);
  let rewindBlastRadius = $derived(
    computeVisibleBlastRadius(
      chatStore.messages,
      chatStore.messages.findIndex((m) => m.id === message.id)
    )
  );

  function openRewindConfirm() {
    rewindError = null;
    rewindConfirmOpen = true;
  }

  function closeRewindConfirm() {
    if (rewindBusy) return;
    rewindConfirmOpen = false;
    rewindError = null;
  }

  async function confirmRewind() {
    const threadId = threadsStore.currentThreadId;
    if (!threadId || rewindBusy) return;
    rewindBusy = true;
    rewindError = null;
    const outcome = await rewindToMessage(threadId, message.id);
    rewindBusy = false;
    if (outcome.ok) {
      rewindConfirmOpen = false;
      return;
    }
    if (outcome.reason === 'stale_target') {
      rewindError =
        'The conversation changed on the backend and was reloaded. Close this dialog and pick a message again.';
      return;
    }
    rewindError = humanizeErrorText(outcome.error, {
      action: 'rewind',
      resource: 'the conversation'
    });
  }

  // Copy response only
  let copyResponseIcon = $state<'copy' | 'check'>('copy');

  async function handleCopyResponse() {
    const text = messageToResponseText(message);
    await navigator.clipboard.writeText(text);
    copyResponseIcon = 'check';
    setTimeout(() => { copyResponseIcon = 'copy'; }, 2000);
  }

  // Copy full markdown (thinking + tool calls + response)
  let copyFullIcon = $state<'fileText' | 'check'>('fileText');

  async function handleCopyFull() {
    const md = messageToMarkdown(message);
    await navigator.clipboard.writeText(md);
    copyFullIcon = 'check';
    setTimeout(() => { copyFullIcon = 'fileText'; }, 2000);
  }

  // Report Error
  let reportModalOpen = $state(false);
  let reportDescription = $state('');
  let reportSending = $state(false);
  let reportSent = $state(false);
  let reportError = $state<string | null>(null);

  function openReportModal() {
    reportDescription = '';
    reportSent = false;
    reportError = null;
    reportModalOpen = true;
  }

  function closeReportModal() {
    reportModalOpen = false;
  }

  async function submitReport() {
    reportSending = true;
    reportError = null;
    try {
      const recentMessages = chatStore.messages.slice(-10).map(m => ({
        role: m.role,
        content: (m.content || '').substring(0, 500),
        timestamp: m.timestamp.toISOString(),
        id: m.id
      }));

      await api.reportProblem({
        thread_id: threadsStore.currentThreadId,
        message_id: message.id,
        description: reportDescription,
        messages: recentMessages,
        timestamp: new Date().toISOString(),
        client_info: {
          platform: typeof window !== 'undefined' && '__TAURI__' in window ? 'desktop' : 'browser',
          userAgent: navigator.userAgent,
          url: window.location.href
        }
      });
      reportSent = true;
    } catch (err) {
      console.error('Failed to send report:', err);
      reportError = humanizeErrorText(err, { action: 'send', resource: 'the report' });
      reportSent = false;
    } finally {
      reportSending = false;
    }
  }
</script>

{#if message.kind === 'command_result'}
<div class="command-result">
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
<div class="message-bubble" class:user={isUser} class:assistant={!isUser} class:autonomous-prompt={!!message.autonomousSource}>
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
                data-tooltip={file.name}
              >
                <img src={file.dataUrl} alt={file.name} />
              </button>
            {:else}
              <button
                type="button"
                class="user-document"
                onclick={() => downloadAttachment(file)}
                disabled={downloadingIds.has(file.id)}
                data-tooltip={`Download ${file.name} (${formatFileSize(file.size)})`}
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
          title="Open this thread"
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
              <!-- A held Skill call renders as ToolCallCard: SkillCard has no
                   approval bar, and the hold is time-boxed (deny-on-timeout). -->
              {#if step.name === 'Skill' && !step.pendingApproval}
                <SkillCard toolCall={{
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
              {:else}
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
              {/if}
            </div>
          {:else if step.type === 'provider_status'}
            <div class="provider-status-step">
              <Icon name={step.providerStatus === 'fallback' ? 'server' : 'refresh'} size={14} />
              <span>{providerStatusText(step)}</span>
            </div>
          {:else if step.type === 'hook_activity'}
            <div class="hook-activity-step" class:is-fault={step.hookStatus && step.hookStatus !== 'ok'}>
              <span class="elbow" aria-hidden="true"></span>
              <span class="hook-activity-text">{hookActivityText(step)}</span>
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
              {@html renderMarkdown(message.intermediateContent || '')}
            </div>
          </div>
        {/if}

        {#if hasToolCalls}
          <div class="tool-calls">
            {#each message.toolCalls || [] as toolCall (toolCall.id)}
              {#if toolCall.name === 'Skill'}
                <SkillCard {toolCall} />
              {:else}
                <ToolCallCard {toolCall} />
              {/if}
            {/each}
          </div>
        {/if}
      {/if}

      <!-- Final content at bottom (after all steps) -->
      {#if isStreaming && !message.content && !hasSteps && !hasToolCalls && !hasIntermediateContent}
        <!-- Only show streaming indicator if nothing else is visible -->
        <AgentActivityIndicator {message} />
      {:else if showStreamingContent && !hasResponseSteps}
        <!-- Show streaming content only if it's not raw JSON and not already in response steps -->
        <div class="message-content">
          <div class="markdown-content">
            {@html renderMarkdownStreaming(message.content)}
          </div>
        </div>
      {:else if isStreaming && lastStepIsNotResponse}
        <!-- Show activity indicator while streaming tool calls or thinking. -->
        <AgentActivityIndicator {message} />
      {:else if showActivityAfterResponse}
        <!-- Show activity text only when a response preamble is followed by tool-argument streaming. -->
        <AgentActivityIndicator {message} />
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

  <div class="message-footer">
    <time class="timestamp">
      {formatMessageTime(message.timestamp)}
    </time>
    {#if showUserActions}
      <div class="message-actions">
        <!-- Auto-compact bubbles render a placeholder, not the stored prompt:
             editing one would resend the literal placeholder string, so only
             Rewind is offered there. -->
        {#if !parsedUserContent.isAutoCompact}
          <button type="button" class="action-btn" data-tooltip="Edit and resend" aria-label="Edit and resend this message" onclick={handleEditMessage}>
            <Icon name="edit" size={14} />
          </button>
        {/if}
        <button type="button" class="action-btn danger" data-tooltip="Rewind to here" aria-label="Rewind conversation to this message" onclick={openRewindConfirm}>
          <Icon name="rewind" size={14} />
        </button>
      </div>
    {/if}
    {#if showActions}
      <div class="message-actions">
        <button type="button" class="action-btn" data-tooltip="Copy response" aria-label="Copy response" onclick={handleCopyResponse}>
          <Icon name={copyResponseIcon} size={14} />
        </button>
        <button type="button" class="action-btn" data-tooltip="Copy full (thinking + tools + response)" aria-label="Copy full message" onclick={handleCopyFull}>
          <Icon name={copyFullIcon} size={14} />
        </button>
        <button type="button" class="action-btn" data-tooltip="Report problem" aria-label="Report problem" onclick={openReportModal}>
          <Icon name="flag" size={14} />
        </button>
      </div>
    {/if}
  </div>
</div>
{/if}

<ImageModal image={modalFile} onClose={closeModal} />

<Modal title="Report Problem" isOpen={reportModalOpen} onClose={closeReportModal}>
  {#snippet children()}
    {#if reportSent}
      <div class="report-success">
        <Icon name="check" size={24} />
        <p>Report sent successfully!</p>
      </div>
    {:else}
      <p class="report-info">This will send debug info to the support team.</p>
      <p class="report-detail">Includes: thread ID, last 10 messages, timestamp, browser info.</p>
      <textarea
        class="report-textarea"
        placeholder="Describe the problem (optional)…"
        bind:value={reportDescription}
        rows="4"
      ></textarea>
      {#if reportError}
        <p class="report-error" role="alert">{reportError}</p>
      {/if}
      <button
        type="button"
        class="report-submit"
        onclick={submitReport}
        disabled={reportSending}
      >
        {reportSending ? 'Sending…' : 'Send Report'}
      </button>
    {/if}
  {/snippet}
</Modal>

<Modal title="Rewind conversation?" isOpen={rewindConfirmOpen} onClose={closeRewindConfirm}>
  {#snippet children()}
    <p class="rewind-confirm-text">
      {#if rewindBlastRadius === 0}
        This removes this message. This cannot be undone.
      {:else}
        This removes this message and
        {rewindBlastRadius === 1 ? '1 later message' : `${rewindBlastRadius} later messages`},
        including the agent's replies. This cannot be undone.
      {/if}
    </p>
    {#if rewindError}
      <p class="rewind-error" role="alert">{rewindError}</p>
    {/if}
    <div class="rewind-confirm-actions">
      <Button variant="secondary" onclick={closeRewindConfirm} disabled={rewindBusy}>Cancel</Button>
      <Button variant="danger" onclick={confirmRewind} disabled={rewindBusy} loading={rewindBusy}>
        {rewindBusy ? 'Rewinding…' : 'Rewind'}
      </Button>
    </div>
  {/snippet}
</Modal>

<style>
  .compaction-notice {
    align-self: center;
    display: flex;
    gap: var(--spacing-sm);
    width: min(720px, 92%);
    margin: var(--spacing-md) 0;
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
    width: min(760px, 92%);
    margin: var(--spacing-md) 0;
    padding: var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-primary);
    animation: msgIn var(--transition-normal);
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
    max-width: min(70ch, 85%);
    /* Inter-message gap follows the Appearance > Spacing setting; falls back to
       --spacing-md (the original 16px) when no override is set. */
    margin-bottom: var(--ui-density-gap, var(--spacing-md));
    animation: msgIn var(--transition-normal);
  }

  .message-bubble.user {
    align-self: flex-end;
  }

  .message-bubble.assistant {
    align-self: stretch;
    max-width: min(70ch, 100%);
  }

  :global(html[data-chat-bubbles="on"]) .message-bubble.assistant {
    align-self: flex-start;
    max-width: min(70ch, 85%);
  }

  .bubble-content {
    padding: var(--spacing-md);
    border-radius: var(--radius-lg);
    /* Message reading text matches the chat-title size (--font-size-sm, 14px)
       used by the sidebar thread titles, the thread header, and task titles,
       so body copy and titles share one size across the app. */
    font-size: var(--font-size-sm);
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

  /* Ephemeral lifecycle-hook line. Uses the same curved elbow the prompt-bar
     hints draw (InputHintTips.svelte), so it reads as a soft connector rather
     than a carbon copy of Claude Code's square L. */
  .hook-activity-step {
    position: relative;
    margin: 2px 0 var(--spacing-xs);
    padding-left: 20px;
    color: var(--text-muted);
    font-size: var(--font-size-xs);
    line-height: 1.4;
  }

  .hook-activity-step .elbow {
    position: absolute;
    left: 4px;
    top: -2px;
    bottom: 8px;
    width: 9px;
    border-left: 1.5px solid var(--border-default);
    border-bottom: 1.5px solid var(--border-default);
    border-bottom-left-radius: 6px;
    pointer-events: none;
  }

  .hook-activity-step.is-fault {
    color: color-mix(in srgb, var(--error) 80%, var(--text-muted));
  }

  .hook-activity-step.is-fault .elbow {
    border-color: color-mix(in srgb, var(--error) 55%, var(--border-default));
  }

  .hook-activity-text {
    display: inline-block;
    word-break: break-word;
  }

  .assistant .bubble-content {
    background: transparent;
    border: none;
    padding-left: 0;
    padding-right: 0;
  }

  :global(html[data-chat-bubbles="on"]) .assistant .bubble-content {
    background: var(--bg-elevated);
    border: 1px solid var(--glass-border);
    border-bottom-left-radius: var(--radius-sm);
    padding-left: var(--spacing-md);
    padding-right: var(--spacing-md);
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

  .user-document:hover:not(:disabled) {
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
    /* Allow the browser to break unbreakable strings (long hashes, Draft IDs,
       URLs without separators, etc.) wherever needed so they wrap inside the
       message bubble instead of overflowing horizontally. min-width: 0 is
       required when the bubble lives inside a flex parent — without it, the
       child refuses to shrink below its intrinsic content width. */
    min-width: 0;
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  .markdown-content :global(code) {
    overflow-wrap: anywhere;
    word-break: break-all;
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

  .message-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-xs);
    margin-top: var(--spacing-xs);
    padding: 0 var(--spacing-sm);
  }

  .user .message-footer {
    justify-content: flex-end;
  }

  /* .timestamp text style is the global utility in app.css (size/weight/color);
     its placement here is owned by .message-footer above. */

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

  /* Action buttons (copy, copy-full, report) — hidden until the message is
     hovered or one of them is focused, so the reading surface stays clean.
     :focus-within keeps them keyboard-reachable (tabbing into the message
     reveals them). */
  .message-actions {
    display: flex;
    gap: var(--spacing-xs);
    opacity: 0;
    transition: opacity var(--transition-fast);
  }

  .message-bubble:hover .message-actions,
  .message-bubble:focus-within .message-actions {
    opacity: 1;
  }

  .action-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    background: transparent;
    border: none;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .action-btn:hover {
    color: var(--text-secondary);
    background: color-mix(in srgb, var(--text-muted) 10%, transparent);
  }

  .action-btn:focus-visible {
    opacity: 1;
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
  }

  /* Destructive action: hover telegraphs data loss by shifting toward the
     error color instead of the neutral hover tint. */
  .action-btn.danger:hover {
    color: var(--error);
    background: color-mix(in srgb, var(--error) 12%, transparent);
  }

  .action-btn.danger:focus-visible {
    outline-color: var(--error);
  }

  /* Rewind confirm modal */
  .rewind-confirm-text {
    margin: 0 0 var(--spacing-md) 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .rewind-error {
    margin: 0 0 var(--spacing-md) 0;
    font-size: var(--font-size-sm);
    color: var(--error);
  }

  .rewind-confirm-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
  }

  /* Report modal */
  .report-info {
    margin: 0 0 var(--spacing-xs) 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .report-detail {
    margin: 0 0 var(--spacing-md) 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .report-textarea {
    width: 100%;
    padding: var(--spacing-sm);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-family: inherit;
    font-size: var(--font-size-sm);
    resize: vertical;
    margin-bottom: var(--spacing-md);
    box-sizing: border-box;
  }

  .report-textarea:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .report-submit {
    width: 100%;
    padding: var(--spacing-sm);
    background: var(--accent-primary);
    color: var(--text-on-accent);
    border: none;
    border-radius: var(--radius-md);
    font-weight: 600;
    font-size: var(--font-size-sm);
    cursor: pointer;
    transition: background var(--transition-fast), transform var(--transition-fast), box-shadow var(--transition-fast);
  }

  /* §6 — hover used to dim opacity, which is the rule's textbook
     "not just opacity changes" case. Match the canonical Button.svelte
     primary pattern: darker bg + subtle lift + accent glow. */
  .report-submit:hover:not(:disabled) {
    background: var(--accent-hover);
    transform: translateY(-1px);
    box-shadow: var(--accent-glow-sm);
  }

  .report-submit:active:not(:disabled) {
    transform: translateY(0);
    box-shadow: none;
  }

  .report-submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .report-error {
    margin: 0 0 var(--spacing-md) 0;
    font-size: var(--font-size-sm);
    color: var(--error);
  }

  .report-success {
    text-align: center;
    padding: var(--spacing-lg);
    color: var(--accent-primary);
  }

  .report-success p {
    margin: var(--spacing-sm) 0 0 0;
    color: var(--text-secondary);
  }
</style>
