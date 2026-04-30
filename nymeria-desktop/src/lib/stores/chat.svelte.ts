import type {
  Message,
  MessageStep,
  ToolCall,
  ToolCallStatus,
  ToolReloadInfo,
  FileAttachment,
  WorkspaceArtifact,
  ContextStats
} from '$lib/types';
import { abortCurrentStream, api } from '$lib/services/api.svelte';

function generateId(): string {
  return crypto.randomUUID();
}

function mergeArtifacts(
  existing: WorkspaceArtifact[] | undefined,
  incoming: WorkspaceArtifact[] | undefined
): WorkspaceArtifact[] | undefined {
  if (!incoming?.length) return existing;
  if (!existing?.length) return [...incoming];

  const merged = new Map(existing.map((artifact) => [artifact.path, artifact]));
  for (const artifact of incoming) {
    merged.set(artifact.path, artifact);
  }
  return Array.from(merged.values());
}

/**
 * Result of parsing Nymeria's response format.
 * Now simplified - just returns content as-is since we no longer use structured output.
 */
interface ParsedResponse {
  content: string;
  intermediateContent?: string;
}

/**
 * Parse Nymeria's response - now just returns content as-is.
 * Visibility is controlled by tools, not embedded in response format.
 */
function parseNymeriaResponse(content: string): ParsedResponse {
  return { content: content || '' };
}

const FLUSH_INTERVAL = 48; // ~20 updates/sec

function createChatStore() {
  let messages = $state<Message[]>([]);
  let isStreaming = $state(false);
  let activeToolCalls = $state<Map<string, ToolCall>>(new Map());
  let isCompacting = $state(false);
  let compactingMessage = $state('');
  let lastCompactResult = $state<{ messagesRemoved: number } | null>(null);
  let contextAttachedMessage = $state<string | null>(null);
  let contextStats = $state<ContextStats | null>(null);
  let activeModel = $state<string | null>(null);
  let isQueued = $state(false);
  let isLoadingHistory = $state(false);
  let _instantScroll = $state(false);

  // Throttle state for streaming buffers
  let _responseBuffer = '';
  let _thinkingBuffer = '';
  let _lastFlushTime = 0;
  let _flushTimer: ReturnType<typeof setTimeout> | null = null;

  return {
    get messages() {
      return messages;
    },
    get isStreaming() {
      return isStreaming;
    },
    get activeToolCalls() {
      return activeToolCalls;
    },
    get isCompacting() {
      return isCompacting;
    },
    get compactingMessage() {
      return compactingMessage;
    },
    get lastCompactResult() {
      return lastCompactResult;
    },
    get contextAttachedMessage() {
      return contextAttachedMessage;
    },
    get contextStats() {
      return contextStats;
    },
    get activeModel() {
      return activeModel;
    },
    get isQueued() {
      return isQueued;
    },
    get isLoadingHistory() {
      return isLoadingHistory;
    },
    get instantScroll() {
      return _instantScroll;
    },

    addUserMessage(content: string, attachments?: FileAttachment[]): string {
      const id = generateId();
      const message: Message = {
        id,
        role: 'user',
        content,
        attachments,
        timestamp: new Date(),
        status: 'complete'
      };
      messages = [...messages, message];
      return id;
    },

    addAutonomousPromptMessage(prompt: string, source: string): string {
      const id = generateId();
      const message: Message = {
        id,
        role: 'user',
        content: prompt,
        timestamp: new Date(),
        status: 'complete',
        autonomousSource: source,
      };
      messages = [...messages, message];
      return id;
    },

    addAssistantMessage(): string {
      const id = generateId();
      const message: Message = {
        id,
        role: 'assistant',
        content: '',
        steps: [],                    // Ordered steps array
        intermediateContent: '',      // Legacy field (computed from steps)
        timestamp: new Date(),
        status: 'streaming',
        toolCalls: []                 // Legacy field (computed from steps)
      };
      messages = [...messages, message];
      return id;
    },

    appendToLastMessage(content: string) {
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            content: lastMessage.content + content,
            intermediateContent: lastMessage.intermediateContent || undefined
          }
        ];
      }
    },

    setLastMessageContent(content: string) {
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            content,
            intermediateContent: lastMessage.intermediateContent || undefined
          }
        ];
      }
    },

    setLastMessageComplete() {
      this._forceFlush();
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        // No parsing needed - just mark as complete
        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            status: lastMessage.status === 'error' ? 'error' : 'complete',
            intermediateContent: lastMessage.intermediateContent || undefined
          }
        ];
      }
    },

    /** Re-mark the last assistant message as streaming (used by thread switch recovery). */
    setLastMessageStreaming() {
      if (messages.length === 0) return;
      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];
      if (lastMessage.role === 'assistant' && lastMessage.status !== 'streaming') {
        messages = [
          ...messages.slice(0, lastIndex),
          { ...lastMessage, status: 'streaming' as const }
        ];
      }
    },

    setLastMessageError(error: string) {
      this._forceFlush();
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        const existing = (lastMessage.content || '').trim();
        const errorText = (error || 'Unknown error').trim();
        let mergedContent = errorText;
        let updatedSteps = lastMessage.steps;

        if (existing) {
          mergedContent = existing.includes(errorText)
            ? existing
            : `${existing}\n\n---\n**Error:** ${errorText}`;
        }

        if (lastMessage.steps) {
          const errorStepText = `\n\n---\n**Error:** ${errorText}`;
          const steps = [...lastMessage.steps];
          const lastStep = steps[steps.length - 1];

          if (lastStep && lastStep.type === 'response') {
            const existingStepText = lastStep.content || '';
            if (!existingStepText.includes(errorText)) {
              steps[steps.length - 1] = {
                ...lastStep,
                content: existingStepText + errorStepText
              };
            }
          } else {
            steps.push({ type: 'response', content: errorStepText });
          }

          updatedSteps = steps;
        }

        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            content: mergedContent,
            status: 'error',
            steps: updatedSteps,
            intermediateContent: lastMessage.intermediateContent || undefined
          }
        ];
      }
    },

    addToolCall(id: string, name: string, args: Record<string, unknown>) {
      const toolCall: ToolCall = {
        id,
        name,
        arguments: args,
        status: 'running',
        startTime: new Date()
      };

      activeToolCalls = new Map(activeToolCalls).set(id, toolCall);

      // Also add to the last assistant message
      if (messages.length > 0) {
        const lastIndex = messages.length - 1;
        const lastMessage = messages[lastIndex];

        if (lastMessage.role === 'assistant') {
          const existingToolCalls = lastMessage.toolCalls || [];

          // If this is the first tool call and there's content, move it to intermediateContent
          // This makes the "thinking" text appear above tool calls during streaming
          let newIntermediate = lastMessage.intermediateContent;
          let newContent = lastMessage.content;

          if (existingToolCalls.length === 0 && lastMessage.content && !lastMessage.intermediateContent) {
            // First tool call - move current content to intermediate
            newIntermediate = lastMessage.content;
            newContent = '';
          }

          messages = [
            ...messages.slice(0, lastIndex),
            {
              ...lastMessage,
              content: newContent,
              toolCalls: [...existingToolCalls, toolCall],
              intermediateContent: newIntermediate || undefined
            }
          ];
        }
      }
    },

    updateToolCallResult(id: string, result: string, status: ToolCallStatus) {
      const existing = activeToolCalls.get(id);
      if (existing) {
        const updated: ToolCall = {
          ...existing,
          result,
          status,
          endTime: new Date()
        };

        const newMap = new Map(activeToolCalls);
        newMap.set(id, updated);
        activeToolCalls = newMap;

        // Update in the message as well
        messages = messages.map((msg) => {
          if (msg.role === 'assistant' && msg.toolCalls) {
            return {
              ...msg,
              toolCalls: msg.toolCalls.map((tc) =>
                tc.id === id ? updated : tc
              ),
              intermediateContent: msg.intermediateContent || undefined
            };
          }
          return msg;
        });
      }
    },

    updateToolCallResultByName(name: string, result: string, status: ToolCallStatus, id?: string) {
      // If ID is provided, use it directly for matching
      if (id && activeToolCalls.has(id)) {
        this.updateToolCallResult(id, result, status);
        return;
      }

      // Fallback: Find the first tool call with this name that's still running
      let foundId: string | null = null;
      for (const [tcId, tc] of activeToolCalls) {
        if (tc.name === name && tc.status === 'running') {
          foundId = tcId;
          break;
        }
      }

      if (foundId) {
        const existing = activeToolCalls.get(foundId)!;
        const updated: ToolCall = {
          ...existing,
          result,
          status,
          endTime: new Date()
        };

        const newMap = new Map(activeToolCalls);
        newMap.set(foundId, updated);
        activeToolCalls = newMap;

        // Update in the message as well
        messages = messages.map((msg) => {
          if (msg.role === 'assistant' && msg.toolCalls) {
            return {
              ...msg,
              toolCalls: msg.toolCalls.map((tc) =>
                tc.id === foundId ? updated : tc
              ),
              intermediateContent: msg.intermediateContent || undefined
            };
          }
          return msg;
        });
      }
    },

    clearActiveToolCalls() {
      activeToolCalls = new Map();
    },

    setStreaming(streaming: boolean) {
      if (!streaming) this._forceFlush();
      isStreaming = streaming;
    },

    setLoadingHistory(loading: boolean) {
      isLoadingHistory = loading;
    },

    setIntermediateContent(content: string) {
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        // Append to existing intermediateContent (thinking can stream in chunks)
        const existing = lastMessage.intermediateContent || '';
        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            intermediateContent: existing + content
          }
        ];
      }
    },

    // === New step-based methods for interleaved thinking/tool ordering ===

    /**
     * Add a thinking step to the last assistant message.
     * Thinking steps preserve order with tool calls for proper interleaving.
     * Uses leading-edge throttle: first chunk flushes immediately, subsequent
     * chunks buffer for FLUSH_INTERVAL ms to reduce array reconstructions.
     */
    addThinkingStep(content: string) {
      if (messages.length === 0) return;

      _thinkingBuffer += content;

      const now = performance.now();
      if (now - _lastFlushTime >= FLUSH_INTERVAL) {
        this._flushThinking();
      } else if (!_flushTimer) {
        _flushTimer = setTimeout(() => {
          _flushTimer = null;
          this._flushThinking();
        }, FLUSH_INTERVAL - (now - _lastFlushTime));
      }
    },

    /** Flush buffered thinking content into the message steps. */
    _flushThinking() {
      if (!_thinkingBuffer || messages.length === 0) return;

      const buffered = _thinkingBuffer;
      _thinkingBuffer = '';
      _lastFlushTime = performance.now();

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        const steps = lastMessage.steps || [];
        const lastStep = steps[steps.length - 1];

        if (lastStep && lastStep.type === 'thinking') {
          const updatedSteps = [
            ...steps.slice(0, -1),
            { ...lastStep, content: (lastStep.content || '') + buffered }
          ];
          messages = [
            ...messages.slice(0, lastIndex),
            {
              ...lastMessage,
              steps: updatedSteps,
              intermediateContent: this._computeIntermediateContent(updatedSteps)
            }
          ];
        } else {
          const newStep: MessageStep = { type: 'thinking', content: buffered };
          const updatedSteps = [...steps, newStep];
          messages = [
            ...messages.slice(0, lastIndex),
            {
              ...lastMessage,
              steps: updatedSteps,
              intermediateContent: this._computeIntermediateContent(updatedSteps)
            }
          ];
        }
      }
    },

    /**
     * Add a tool call step to the last assistant message.
     * Tool call steps are ordered with thinking for proper interleaving.
     */
    addToolCallStep(id: string, name: string, args: Record<string, unknown>) {
      this._forceFlush();
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        const toolCall: ToolCall = {
          id,
          name,
          arguments: args,
          status: 'running',
          startTime: new Date()
        };

        // Add to active tool calls
        activeToolCalls = new Map(activeToolCalls).set(id, toolCall);

        // Add as a step
        const newStep: MessageStep = {
          type: 'tool_call',
          id,
          name,
          arguments: args,
          status: 'running',
          startTime: new Date()
        };
        const updatedSteps = [...(lastMessage.steps || []), newStep];

        // Compute legacy toolCalls array from steps
        const legacyToolCalls = this._computeToolCalls(updatedSteps);

        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            steps: updatedSteps,
            toolCalls: legacyToolCalls,
          }
        ];
      }
    },

    /**
     * Update a tool call step with its result.
     */
    updateToolCallStepResult(id: string, result: string, status: ToolCallStatus) {
      // Update active tool calls
      const existing = activeToolCalls.get(id);
      if (existing) {
        const updated: ToolCall = {
          ...existing,
          result,
          status,
          endTime: new Date()
        };
        const newMap = new Map(activeToolCalls);
        newMap.set(id, updated);
        activeToolCalls = newMap;
      }

      // Update in steps array
      messages = messages.map((msg) => {
        if (msg.role === 'assistant' && msg.steps) {
          const updatedSteps = msg.steps.map((step) => {
            if (step.type === 'tool_call' && step.id === id) {
              return { ...step, result, status, endTime: new Date() };
            }
            return step;
          });
          return {
            ...msg,
            steps: updatedSteps,
            toolCalls: this._computeToolCalls(updatedSteps)
          };
        }
        return msg;
      });
    },

    addToolCallArtifacts(id: string, artifacts: WorkspaceArtifact[]) {
      if (!artifacts.length) return;

      const existing = activeToolCalls.get(id);
      if (existing) {
        const updated: ToolCall = {
          ...existing,
          artifacts: mergeArtifacts(existing.artifacts, artifacts)
        };
        const newMap = new Map(activeToolCalls);
        newMap.set(id, updated);
        activeToolCalls = newMap;
      }

      messages = messages.map((msg) => {
        if (msg.role === 'assistant' && msg.steps) {
          const updatedSteps = msg.steps.map((step) => {
            if (step.type === 'tool_call' && step.id === id) {
              return {
                ...step,
                artifacts: mergeArtifacts(step.artifacts, artifacts)
              };
            }
            return step;
          });

          return {
            ...msg,
            steps: updatedSteps,
            toolCalls: this._computeToolCalls(updatedSteps)
          };
        }

        return msg;
      });
    },

    /**
     * Add a response step to the last assistant message.
     * Response steps preserve order with thinking and tool calls for proper interleaving.
     * Uses leading-edge throttle: first chunk flushes immediately, subsequent
     * chunks buffer for FLUSH_INTERVAL ms to reduce array reconstructions + re-parses.
     */
    addResponseStep(content: string) {
      if (messages.length === 0) return;

      if (_thinkingBuffer) {
        this._forceFlush();
      }

      _responseBuffer += content;

      const now = performance.now();
      if (now - _lastFlushTime >= FLUSH_INTERVAL) {
        this._flushResponse();
      } else if (!_flushTimer) {
        _flushTimer = setTimeout(() => {
          _flushTimer = null;
          this._flushResponse();
        }, FLUSH_INTERVAL - (now - _lastFlushTime));
      }
    },

    /** Flush buffered response content into the message steps. */
    _flushResponse() {
      if (!_responseBuffer || messages.length === 0) return;

      const buffered = _responseBuffer;
      _responseBuffer = '';
      _lastFlushTime = performance.now();

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        const steps = lastMessage.steps || [];
        const lastStep = steps[steps.length - 1];

        let updatedSteps: MessageStep[];
        if (lastStep && lastStep.type === 'response') {
          updatedSteps = [
            ...steps.slice(0, -1),
            { ...lastStep, content: (lastStep.content || '') + buffered }
          ];
        } else {
          updatedSteps = [...steps, { type: 'response' as const, content: buffered }];
        }

        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            steps: updatedSteps
          }
        ];
      }
    },

    /** Force-flush all pending buffers immediately. Called on state transitions. */
    _forceFlush() {
      if (_flushTimer) {
        clearTimeout(_flushTimer);
        _flushTimer = null;
      }
      if (_thinkingBuffer) this._flushThinking();
      if (_responseBuffer) this._flushResponse();
    },

    /**
     * Set the final response content (after all steps).
     * Legacy method - kept for backwards compatibility.
     */
    setResponseContent(content: string) {
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            content: lastMessage.content + content
          }
        ];
      }
    },

    /**
     * Finalize the last assistant message after streaming completes.
     * Computes message.content from response steps (for history/search)
     * and intermediateContent from thinking steps (legacy compatibility).
     *
     * Note: thinking steps are NOT reclassified — the backend already
     * distinguishes actual thinking (type: "thinking") from preamble/
     * response text (type: "response") at the SSE level.
     */
    reclassifyThinkingAsResponse() {
      this._forceFlush();
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant' && lastMessage.steps) {
        const steps = lastMessage.steps;

        // Compute message.content from all response steps (for history/search)
        const responseContent = steps
          .filter((s) => s.type === 'response')
          .map((s) => s.content || '')
          .join('');

        if (responseContent) {
          messages = [
            ...messages.slice(0, lastIndex),
            {
              ...lastMessage,
              content: responseContent,
              intermediateContent: this._computeIntermediateContent(steps)
            }
          ];
        }
      }
    },

    // Helper to compute legacy intermediateContent from steps
    _computeIntermediateContent(steps: MessageStep[]): string {
      return steps
        .filter((s) => s.type === 'thinking')
        .map((s) => s.content || '')
        .join('');
    },

    // Helper to compute legacy toolCalls array from steps
    _computeToolCalls(steps: MessageStep[]): ToolCall[] {
      return steps
        .filter((s): s is MessageStep & { type: 'tool_call' } => s.type === 'tool_call')
        .map((s) => ({
          id: s.id || '',
          name: s.name || '',
          arguments: s.arguments || {},
          result: s.result,
          artifacts: s.artifacts,
          status: s.status || 'pending',
          startTime: s.startTime,
          endTime: s.endTime
        }));
    },

    setMessages(newMessages: Message[]) {
      _instantScroll = true;
      messages = newMessages;
    },

    setInstantScroll(v: boolean) {
      _instantScroll = v;
    },

    clearMessages() {
      this._forceFlush();
      messages = [];
      activeToolCalls = new Map();
      isStreaming = false;
      contextStats = null;
      activeModel = null;
      isQueued = false;
    },

    /**
     * Lightweight reset for thread switches. Clears streaming state but keeps
     * current messages visible until setMessages() overwrites them, avoiding
     * the empty-state flash that clearMessages() would cause.
     */
    prepareForThreadSwitch() {
      this._forceFlush();
      activeToolCalls = new Map();
      isStreaming = false;
      isQueued = false;
      isCompacting = false;
      compactingMessage = '';
      lastCompactResult = null;
      contextAttachedMessage = null;
      _lastFlushTime = 0;
    },

    removeMessage(messageId: string) {
      messages = messages.filter((msg) => msg.id !== messageId);
    },

    /**
     * Stop the current generation and mark the message as stopped.
     * Aborts the active stream and appends context for the AI.
     */
    stopGenerating(threadId?: string) {
      if (!isStreaming) return;

      this._forceFlush();
      abortCurrentStream();

      // Mark last assistant message: update running tool calls to 'cancelled'
      const lastIndex = messages.length - 1;
      if (lastIndex >= 0) {
        const lastMessage = messages[lastIndex];
        if (lastMessage.role === 'assistant') {
          const updatedSteps = lastMessage.steps?.map((step) => {
            if (step.type === 'tool_call' && step.status === 'running') {
              return { ...step, status: 'cancelled' as ToolCallStatus, endTime: new Date() };
            }
            return step;
          });

          const updatedToolCalls = lastMessage.toolCalls?.map((tc) => {
            if (tc.status === 'running') {
              return { ...tc, status: 'cancelled' as ToolCallStatus, endTime: new Date() };
            }
            return tc;
          });

          const stoppedContent = lastMessage.content
            ? lastMessage.content + '\n\n[User stopped this output]'
            : '[User stopped this output]';

          messages = [
            ...messages.slice(0, lastIndex),
            {
              ...lastMessage,
              content: stoppedContent,
              steps: updatedSteps ?? lastMessage.steps,
              toolCalls: updatedToolCalls ?? lastMessage.toolCalls,
              status: 'complete' as const,
            }
          ];
        }
      }

      isStreaming = false;
      activeToolCalls = new Map();

      // Signal the backend to abort and clean up checkpoint (fire-and-forget)
      if (threadId) {
        api.stopThread(threadId).catch(() => {});
      }
    },

    // Compaction status methods
    setCompacting(compacting: boolean, message: string = '') {
      isCompacting = compacting;
      compactingMessage = message;
      if (!compacting) {
        // Clear after a short delay
        setTimeout(() => {
          compactingMessage = '';
        }, 2000);
      }
    },

    setCompactResult(messagesRemoved: number) {
      lastCompactResult = { messagesRemoved };
      // Auto-clear after showing
      setTimeout(() => {
        lastCompactResult = null;
      }, 5000);
    },

    setContextAttached(message: string) {
      contextAttachedMessage = message;
      // Auto-clear after showing
      setTimeout(() => {
        contextAttachedMessage = null;
      }, 5000);
    },

    setLastUserMessageContextSummary(summary: string) {
      // Find the last user message and attach the context summary
      const lastUserIndex = messages.findLastIndex((m) => m.role === 'user');
      if (lastUserIndex >= 0) {
        messages = [
          ...messages.slice(0, lastUserIndex),
          { ...messages[lastUserIndex], contextSummary: summary },
          ...messages.slice(lastUserIndex + 1)
        ];
      }
    },

    clearCompactingState() {
      isCompacting = false;
      compactingMessage = '';
      lastCompactResult = null;
      contextAttachedMessage = null;
    },

    /**
     * Handle a "compacted" event from the backend.
     * Clears the UI chat history and shows a system-style notification.
     */
    handleCompacted(messagesRemoved: number, summary?: string, autoResumed: boolean = false) {
      this._forceFlush();
      // Clear all messages and streaming state
      activeToolCalls = new Map();
      isStreaming = autoResumed;
      isQueued = false;
      isCompacting = false;
      compactingMessage = '';

      const notice: Message = {
        id: generateId(),
        role: 'system' as const,
        kind: 'compaction_notice',
        content: 'Context compacted',
        contextSummary: summary,
        messagesRemoved,
        autoResumed,
        timestamp: new Date(),
        status: 'complete' as const
      };

      if (autoResumed) {
        messages = [
          notice,
          {
            id: generateId(),
            role: 'assistant' as const,
            content: '',
            steps: [],
            intermediateContent: '',
            timestamp: new Date(),
            status: 'streaming' as const,
            toolCalls: []
          }
        ];
      } else {
        messages = [notice];
      }

      // Show the compact result indicator for 5s
      lastCompactResult = { messagesRemoved };
      setTimeout(() => {
        lastCompactResult = null;
      }, 5000);
    },

    handleToolReload(tools: string[], ttl: string, ttlSeconds: number | null) {
      this._forceFlush();

      const lastIndex = messages.length - 1;
      if (lastIndex >= 0 && messages[lastIndex].role === 'assistant') {
        messages = [
          ...messages.slice(0, lastIndex),
          { ...messages[lastIndex], status: 'complete' as const }
        ];
      }

      messages = [
        ...messages,
        {
          id: generateId(),
          role: 'assistant' as const,
          content: '',
          steps: [],
          timestamp: new Date(),
          status: 'streaming' as const,
          toolReloadInfo: { tools, ttl } as ToolReloadInfo,
        }
      ];
    },

    // Context stats methods
    setContextStats(stats: ContextStats | null) {
      contextStats = stats;
    },

    setActiveModel(model: string | null) {
      activeModel = model;
    },

    setQueued(queued: boolean) {
      isQueued = queued;
    }
  };
}

export const chatStore = createChatStore();
