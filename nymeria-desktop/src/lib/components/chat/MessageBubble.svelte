<script lang="ts">
  import { marked } from 'marked';
  // Import highlight.js core and only common languages to reduce bundle size
  import hljs from 'highlight.js/lib/core';
  import javascript from 'highlight.js/lib/languages/javascript';
  import typescript from 'highlight.js/lib/languages/typescript';
  import python from 'highlight.js/lib/languages/python';
  import json from 'highlight.js/lib/languages/json';
  import bash from 'highlight.js/lib/languages/bash';
  import css from 'highlight.js/lib/languages/css';
  import xml from 'highlight.js/lib/languages/xml';
  import markdown from 'highlight.js/lib/languages/markdown';
  import rust from 'highlight.js/lib/languages/rust';
  import go from 'highlight.js/lib/languages/go';
  import yaml from 'highlight.js/lib/languages/yaml';
  import sql from 'highlight.js/lib/languages/sql';

  import type { Message, FileAttachment } from '$lib/types';
  import { Icon, ThinkingIndicator } from '$lib/components/common';
  import { formatFileSize, getFileExtension } from '$lib/utils/fileProcessing';
  // StreamingText removed — markdown is now always rendered, with an inline cursor for streaming
  import ToolCallCard from './ToolCallCard.svelte';
  import ImageModal from './ImageModal.svelte';

  // Register languages
  hljs.registerLanguage('javascript', javascript);
  hljs.registerLanguage('js', javascript);
  hljs.registerLanguage('typescript', typescript);
  hljs.registerLanguage('ts', typescript);
  hljs.registerLanguage('python', python);
  hljs.registerLanguage('py', python);
  hljs.registerLanguage('json', json);
  hljs.registerLanguage('bash', bash);
  hljs.registerLanguage('sh', bash);
  hljs.registerLanguage('shell', bash);
  hljs.registerLanguage('css', css);
  hljs.registerLanguage('html', xml);
  hljs.registerLanguage('xml', xml);
  hljs.registerLanguage('markdown', markdown);
  hljs.registerLanguage('md', markdown);
  hljs.registerLanguage('rust', rust);
  hljs.registerLanguage('rs', rust);
  hljs.registerLanguage('go', go);
  hljs.registerLanguage('yaml', yaml);
  hljs.registerLanguage('yml', yaml);
  hljs.registerLanguage('sql', sql);

  interface Props {
    message: Message;
  }

  let { message }: Props = $props();

  // Pattern to detect time context prefix (added by backend to all messages)
  // Format: [Current Time: ...]\n[Trigger: ...]\n\n{actual message}
  const TIME_CONTEXT_PATTERN = /^\[Current Time:[^\]]+\]\n\[Trigger:[^\]]+\]\n\n/;

  // Pattern to detect autonomous wake-up messages (internal system triggers - should be hidden)
  // Format: [Current Time: ...]\n[Trigger: Autonomous Wake-up...]\n\nWork on TODO ...
  const AUTONOMOUS_WAKEUP_PATTERN = /^\[Current Time:[^\]]+\]\n\[Trigger: Autonomous Wake-up[^\]]*\]\n\n/;

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
  function parseUserMessage(content: string): { text: string; contextSummary: string | null; hidden: boolean } {
    let text = content;
    let contextSummary: string | null = null;

    // Check if this is an autonomous wake-up message (should be hidden entirely)
    if (AUTONOMOUS_WAKEUP_PATTERN.test(text)) {
      return { text: '', contextSummary: null, hidden: true };
    }

    // Check if this is a compaction system request (should be hidden entirely)
    if (COMPACTION_REQUEST_PATTERN.test(text)) {
      return { text: '', contextSummary: null, hidden: true };
    }

    // Strip time context prefix if present (for normal user messages)
    text = text.replace(TIME_CONTEXT_PATTERN, '');

    // Check for auto-compact message format
    const autoCompactMatch = text.match(AUTO_COMPACT_PATTERN);
    if (autoCompactMatch) {
      contextSummary = autoCompactMatch[1].trim();
      text = '[Auto-compact: Conversation summarized]';
      return { text, contextSummary, hidden: false };
    }

    // Check for manual /compact summary suffix
    const manualCompactMatch = text.match(MANUAL_COMPACT_PATTERN);
    if (manualCompactMatch) {
      contextSummary = manualCompactMatch[1].trim();
      text = text.replace(MANUAL_COMPACT_PATTERN, '').trim();
    }

    return { text, contextSummary, hidden: false };
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
      : { text: message.content, contextSummary: null, hidden: false }
  );

  // Computed: parsed assistant message (checks if it should be hidden)
  let parsedAssistantContent = $derived(
    message.role === 'assistant'
      ? parseAssistantMessage(message.content || '')
      : { hidden: false }
  );

  // Should this entire message be hidden from the UI?
  let isHiddenMessage = $derived(
    (message.role === 'user' && parsedUserContent.hidden) ||
    (message.role === 'assistant' && parsedAssistantContent.hidden)
  );

  // Use contextSummary from message prop OR parsed from content
  let effectiveContextSummary = $derived(
    message.contextSummary || parsedUserContent.contextSummary
  );

  // Configure marked with highlight.js
  marked.setOptions({
    breaks: true,
    gfm: true
  });

  // Custom renderer for code blocks with syntax highlighting
  const renderer = new marked.Renderer();
  const originalCode = renderer.code.bind(renderer);
  renderer.code = function (code: string, infostring: string | undefined, escaped: boolean): string {
    if (typeof code === 'object') {
      // Handle marked v12+ object format
      const { text, lang } = code as { text: string; lang?: string };
      const language = lang && hljs.getLanguage(lang) ? lang : 'plaintext';
      const highlighted = hljs.highlight(text, { language }).value;
      return `<pre><code class="hljs language-${language}">${highlighted}</code></pre>`;
    }
    // Handle legacy string format
    const language = infostring && hljs.getLanguage(infostring) ? infostring : 'plaintext';
    const highlighted = hljs.highlight(code, { language }).value;
    return `<pre><code class="hljs language-${language}">${highlighted}</code></pre>`;
  };

  marked.use({ renderer });

  function renderMarkdown(content: string): string {
    try {
      return marked.parse(content) as string;
    } catch {
      return content;
    }
  }

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
</script>

{#if !isHiddenMessage}
<div class="message-bubble" class:user={isUser} class:assistant={!isUser}>
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
            <div class="intermediate-content">
              <div class="markdown-content">
                {@html renderMarkdown(step.content)}
              </div>
              {#if i === streamingLastStepIndex}
                <span class="streaming-cursor"></span>
              {/if}
            </div>
          {:else if step.type === 'tool_call'}
            <div class="tool-calls">
              <ToolCallCard toolCall={{
                id: step.id || '',
                name: step.name || '',
                arguments: step.arguments || {},
                result: step.result,
                status: step.status || 'pending'
              }} />
            </div>
          {:else if step.type === 'response' && step.content}
            <div class="message-content">
              <div class="markdown-content">
                {@html renderMarkdown(step.content)}
              </div>
              {#if i === streamingLastStepIndex}
                <span class="streaming-cursor"></span>
              {/if}
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
            {@html renderMarkdown(message.content)}
          </div>
          <span class="streaming-cursor"></span>
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
    {message.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
  </time>
</div>
{/if}

<ImageModal image={modalFile} onClose={closeModal} />

<style>
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

  .streaming-cursor {
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


  .timestamp {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin-top: var(--spacing-xs);
    padding: 0 var(--spacing-sm);
  }

  .user .timestamp {
    text-align: right;
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
</style>
