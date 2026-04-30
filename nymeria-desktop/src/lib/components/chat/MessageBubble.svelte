<script lang="ts">
  import type { Message, FileAttachment } from '$lib/types';
  import { Icon, Modal } from '$lib/components/common';
  import { formatFileSize, getFileExtension } from '$lib/utils/fileProcessing';
  import { renderMarkdown, renderMarkdownStreaming } from '$lib/utils/markdown';
  import { messageToMarkdown, messageToResponseText } from '$lib/utils/messageToMarkdown';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { api } from '$lib/services/api.svelte';
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
  function parseUserMessage(content: string): { text: string; contextSummary: string | null; hidden: boolean; isSmartwatch: boolean } {
    let text = content;
    let contextSummary: string | null = null;

    // Detect smartwatch trigger before stripping metadata
    const isSmartwatch = SMARTWATCH_TRIGGER_PATTERN.test(text);

    // Check if this is an autonomous wake-up message (should be hidden entirely)
    if (AUTONOMOUS_WAKEUP_PATTERN.test(text)) {
      return { text: '', contextSummary: null, hidden: true, isSmartwatch: false };
    }

    // Check if this is a compaction system request (should be hidden entirely)
    if (COMPACTION_REQUEST_PATTERN.test(text)) {
      return { text: '', contextSummary: null, hidden: true, isSmartwatch: false };
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
      text = '[Auto-compact: Conversation summarized]';
      return { text, contextSummary, hidden: false, isSmartwatch };
    }

    // Check for manual /compact summary suffix
    const manualCompactMatch = text.match(MANUAL_COMPACT_PATTERN);
    if (manualCompactMatch) {
      contextSummary = manualCompactMatch[1].trim();
      text = text.replace(MANUAL_COMPACT_PATTERN, '').trim();
    }

    return { text, contextSummary, hidden: false, isSmartwatch };
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
      : { text: message.content, contextSummary: null, hidden: false, isSmartwatch: false }
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

  // Use contextSummary from message prop OR parsed from content
  let effectiveContextSummary = $derived(
    message.contextSummary || parsedUserContent.contextSummary
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

  // Action buttons (copy + report) — only on completed assistant messages
  let showActions = $derived(!isUser && !isStreaming && !isHiddenMessage);

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

  function openReportModal() {
    reportDescription = '';
    reportSent = false;
    reportModalOpen = true;
  }

  function closeReportModal() {
    reportModalOpen = false;
  }

  async function submitReport() {
    reportSending = true;
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
      reportSent = false;
    } finally {
      reportSending = false;
    }
  }
</script>

{#if message.kind === 'compaction_notice'}
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
{:else if !isHiddenMessage}
<div class="message-bubble" class:user={isUser} class:assistant={!isUser} class:autonomous-prompt={!!message.autonomousSource}>
  <div class="bubble-content">
    {#if isUser}
      {#if hasAttachments}
        <div class="user-attachments">
          {#each allAttachments as file (file.id)}
            {#if file.type === 'image'}
              <button
                type="button"
                class="user-image-button"
                onclick={() => openFileModal(file)}
                title={file.name}
              >
                <img src={file.dataUrl} alt={file.name} />
              </button>
            {:else}
              <div
                class="user-document"
                title={`${file.name} (${formatFileSize(file.size)})`}
              >
                <Icon name={getFileIcon(file.mimeType)} size={20} />
                <span class="doc-name">{file.name}</span>
                <span class="doc-ext">{getFileExtension(file.name)}</span>
              </div>
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
      {#if effectiveContextSummary}
        <details class="context-summary-collapsible">
          <summary>Context Summary</summary>
          <div class="context-summary-content">
            {@html renderMarkdown(effectiveContextSummary)}
          </div>
        </details>
      {/if}
    {:else}
      <!-- Assistant message: render steps in arrival order for proper interleaving -->

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
              {#if step.name === 'Skill'}
                <SkillCard toolCall={{
                  id: step.id || '',
                  name: step.name || '',
                  arguments: step.arguments || {},
                  result: step.result,
                  artifacts: step.artifacts,
                  status: step.status || 'pending',
                  startTime: step.startTime,
                  endTime: step.endTime
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
                  endTime: step.endTime
                }} />
              {/if}
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
        <!-- Show activity indicator while streaming (tool calls, thinking) but not during response text -->
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
      {message.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
    </time>
    {#if showActions}
      <div class="message-actions">
        <button type="button" class="action-btn" title="Copy response" onclick={handleCopyResponse}>
          <Icon name={copyResponseIcon} size={14} />
        </button>
        <button type="button" class="action-btn" title="Copy full (thinking + tools + response)" onclick={handleCopyFull}>
          <Icon name={copyFullIcon} size={14} />
        </button>
        <button type="button" class="action-btn" title="Report problem" onclick={openReportModal}>
          <Icon name="warning" size={14} />
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
        placeholder="Describe the problem (optional)..."
        bind:value={reportDescription}
        rows="4"
      ></textarea>
      <button
        type="button"
        class="report-submit"
        onclick={submitReport}
        disabled={reportSending}
      >
        {reportSending ? 'Sending...' : 'Send Report'}
      </button>
    {/if}
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
    animation: slideUp var(--transition-normal);
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
    animation: slideUp var(--transition-normal);
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
    max-width: 200px;
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

  /* Tables */
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

  .intermediate-content .markdown-content {
    opacity: 0.85;
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

  .timestamp {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  @keyframes slideUp {
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

  /* Action buttons (copy, report) */
  .message-actions {
    display: flex;
    gap: var(--spacing-xs);
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
    opacity: 0.4;
  }

  .action-btn:hover {
    opacity: 1;
    color: var(--text-secondary);
    background: color-mix(in srgb, var(--text-muted) 10%, transparent);
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
    color: white;
    border: none;
    border-radius: var(--radius-md);
    font-weight: 600;
    font-size: var(--font-size-sm);
    cursor: pointer;
    transition: opacity var(--transition-fast);
  }

  .report-submit:hover {
    opacity: 0.9;
  }

  .report-submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
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
