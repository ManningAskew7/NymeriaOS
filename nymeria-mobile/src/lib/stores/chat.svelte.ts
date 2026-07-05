import type { Message, MessageStep, ToolCall, ToolCallStatus, FileAttachment, ContextStats, ToolReloadInfo, WorkspaceArtifact, DispatchInfo, PendingPrompt, PendingPromptStatus } from '$lib/types';
import { abortCurrentStream, api } from '$lib/services/api.svelte';
import { generateId } from '$lib/utils/ids';

function mergeArtifacts(
  existing: WorkspaceArtifact[] | undefined,
  incoming: WorkspaceArtifact[] | undefined
): WorkspaceArtifact[] | undefined {
  if (!incoming?.length) return existing;
  if (!existing?.length) return [...incoming];

  const seen = new Set(existing.map((artifact) => `${artifact.path}:${artifact.name}`));
  const merged = [...existing];
  for (const artifact of incoming) {
    const key = `${artifact.path}:${artifact.name}`;
    if (!seen.has(key)) {
      seen.add(key);
      merged.push(artifact);
    }
  }
  return merged;
}

const FLUSH_INTERVAL = 48; // ~20 updates/sec

export function createChatStore() {
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
  let pendingPrompts = $state<PendingPrompt[]>([]);
  const pendingPromptAborts = new Map<string, AbortController>();
  let isLoadingHistory = $state(false);
  let _instantScroll = $state(false);

  // Edit-previous-prompt state (backlog #12). Entering edit only prefills the
  // composer; the backend rewind is deferred until the user actually sends
  // (the CLI /retry composition), so cancelling an edit loses nothing.
  let editingMessageId = $state<string | null>(null);
  let editingDraft = $state('');
  let editingImageAttachments = $state<FileAttachment[]>([]);

  // Action-sheet state (mobile only): the id of the user bubble whose
  // long-press action sheet is open, or null when none is showing.
  let actionSheetMessageId = $state<string | null>(null);

  // Throttle state for streaming buffers
  let _responseBuffer = '';
  let _thinkingBuffer = '';
  let _lastFlushTime = 0;
  let _flushTimer: ReturnType<typeof setTimeout> | null = null;

  // Streaming-time mutators must refuse to touch an assistant message that has
  // already been marked 'complete'. Without this guard, a stream that arrives
  // after a thread-switch recovery flaw could graft tool calls and response
  // steps onto the previous turn's reply.
  function isLastAssistantStreaming(): boolean {
    if (messages.length === 0) return false;
    const last = messages[messages.length - 1];
    return last.role === 'assistant' && last.status === 'streaming';
  }

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
    get pendingPrompts() {
      return pendingPrompts;
    },
    get isLoadingHistory() {
      return isLoadingHistory;
    },
    get instantScroll() {
      return _instantScroll;
    },
    get editingMessageId() {
      return editingMessageId;
    },
    get isEditing() {
      return editingMessageId !== null;
    },
    get editingDraft() {
      return editingDraft;
    },
    get editingImageAttachments() {
      return editingImageAttachments;
    },
    get actionSheetMessageId() {
      return actionSheetMessageId;
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

    addCommandResult(commandInput: string, content: string, success: boolean): string {
      const id = generateId();
      const message: Message = {
        id,
        role: 'system',
        kind: 'command_result',
        commandInput,
        content,
        timestamp: new Date(),
        status: success ? 'complete' : 'error'
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
        steps: [],
        timestamp: new Date(),
        status: 'streaming'
      };
      messages = [...messages, message];
      return id;
    },

    appendToLastMessage(content: string) {
      if (!isLastAssistantStreaming()) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];
      messages = [
        ...messages.slice(0, lastIndex),
        {
          ...lastMessage,
          content: lastMessage.content + content
        }
      ];
    },

    setLastMessageContent(content: string) {
      if (!isLastAssistantStreaming()) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];
      messages = [
        ...messages.slice(0, lastIndex),
        {
          ...lastMessage,
          content
        }
      ];
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
            status: lastMessage.status === 'error' ? 'error' : 'complete'
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
        const errorText = (error || 'The reply could not be completed.').trim();
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
            steps: updatedSteps
          }
        ];
      }
    },

    setLastAssistantDispatchInfo(dispatchInfo: DispatchInfo) {
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            dispatchInfo
          }
        ];
      }
    },

    updateToolCallResultByName(name: string, result: string, status: ToolCallStatus, id?: string) {
      // If ID is provided, use it directly for matching
      if (id && activeToolCalls.has(id)) {
        this.updateToolCallStepResult(id, result, status);
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
        this.updateToolCallStepResult(foundId, result, status);
      }
    },

    clearActiveToolCalls() {
      activeToolCalls = new Map();
    },

    rewindLastAssistantToStablePoint() {
      this._forceFlush();
      if (!isLastAssistantStreaming()) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];
      if (lastMessage.role !== 'assistant') return;

      const steps = [...(lastMessage.steps || [])];
      while (steps.length > 0) {
        const lastStep = steps[steps.length - 1];
        if (lastStep.type !== 'response' && lastStep.type !== 'thinking') break;
        steps.pop();
      }

      const responseContent = steps
        .filter((s) => s.type === 'response')
        .map((s) => s.content || '')
        .join('');

      messages = [
        ...messages.slice(0, lastIndex),
        {
          ...lastMessage,
          content: responseContent,
          steps
        }
      ];
    },

    setStreaming(streaming: boolean) {
      if (!streaming) this._forceFlush();
      isStreaming = streaming;
    },

    /** Public transition hook for stream handlers that need pending text visible now. */
    flushStreamingBuffers() {
      this._forceFlush();
    },

    setLoadingHistory(loading: boolean) {
      isLoadingHistory = loading;
    },

    /**
     * Add a thinking step to the last assistant message.
     * Thinking steps preserve order with tool calls for proper interleaving.
     * Uses leading-edge throttle: first chunk flushes immediately, subsequent
     * chunks buffer for FLUSH_INTERVAL ms to reduce array reconstructions.
     */
    addThinkingStep(content: string) {
      if (!isLastAssistantStreaming()) return;

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
      if (!_thinkingBuffer) return;
      if (!isLastAssistantStreaming()) {
        _thinkingBuffer = '';
        return;
      }

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
              steps: updatedSteps
            }
          ];
        } else {
          const newStep: MessageStep = { type: 'thinking', content: buffered };
          const updatedSteps = [...steps, newStep];
          messages = [
            ...messages.slice(0, lastIndex),
            {
              ...lastMessage,
              steps: updatedSteps
            }
          ];
        }
      }
    },

    /**
     * Add a tool call step to the last assistant message.
     * Tool call steps are ordered with thinking for proper interleaving.
     */
    addToolCallStep(
      id: string,
      name: string,
      args: Record<string, unknown>,
      timeoutSeconds?: number
    ) {
      this._forceFlush();
      if (!isLastAssistantStreaming()) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        const toolCall: ToolCall = {
          id,
          name,
          arguments: args,
          status: 'running',
          startTime: new Date(),
          timeoutSeconds
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
          startTime: new Date(),
          timeoutSeconds
        };
        const updatedSteps = [...(lastMessage.steps || []), newStep];

        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            steps: updatedSteps
          }
        ];
      }
    },

    addProviderStatusStep(
      data: Omit<MessageStep, 'type'> & { providerStatus: 'retry' | 'fallback' }
    ) {
      this._forceFlush();
      if (!isLastAssistantStreaming()) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];
      if (lastMessage.role !== 'assistant') return;

      const newStep: MessageStep = {
        type: 'provider_status',
        ...data
      };
      const updatedSteps = [...(lastMessage.steps || []), newStep];
      messages = [
        ...messages.slice(0, lastIndex),
        {
          ...lastMessage,
          steps: updatedSteps
        }
      ];
    },

    /**
     * Update a tool call step with its result.
     */
    updateToolCallStepResult(
      id: string,
      result: string,
      status: ToolCallStatus,
      durationMs?: number
    ) {
      // Update active tool calls
      const existing = activeToolCalls.get(id);
      if (existing) {
        const updated: ToolCall = {
          ...existing,
          result,
          status,
          endTime: new Date(),
          durationMs
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
              return { ...step, result, status, endTime: new Date(), durationMs };
            }
            return step;
          });
          return {
            ...msg,
            steps: updatedSteps
          };
        }
        return msg;
      });
    },

    /**
     * Mark a tool-call step as held by a require_approval hook (backlog #77).
     * The step is matched by the tool-call id the backend threads end to end;
     * a step that has not arrived yet is a no-op (the REST list and /hook
     * commands stay available as resolve surfaces).
     */
    markToolCallPendingApproval(
      toolCallId: string,
      approval: { recordId: string; prompt: string; expiresAt: string }
    ) {
      if (!toolCallId) return;
      this._forceFlush();
      messages = messages.map((msg) => {
        if (msg.role !== 'assistant' || !msg.steps) return msg;
        let touched = false;
        const updatedSteps = msg.steps.map((step) => {
          if (step.type === 'tool_call' && step.id === toolCallId) {
            touched = true;
            return { ...step, pendingApproval: approval };
          }
          return step;
        });
        if (!touched) return msg;
        return { ...msg, steps: updatedSteps };
      });
    },

    /**
     * Drop the approval hold from whichever tool-call step carries it.
     * Fired by hook_approval_resolved for every outcome (approved, denied,
     * timeout, aborted, stale), so the buttons always retract.
     */
    clearToolCallPendingApproval(recordId: string, toolCallId?: string) {
      if (!recordId && !toolCallId) return;
      messages = messages.map((msg) => {
        if (msg.role !== 'assistant' || !msg.steps) return msg;
        let touched = false;
        const updatedSteps = msg.steps.map((step) => {
          if (step.type !== 'tool_call' || !step.pendingApproval) return step;
          const matches =
            step.pendingApproval.recordId === recordId ||
            (!!toolCallId && step.id === toolCallId);
          if (!matches) return step;
          touched = true;
          return { ...step, pendingApproval: null };
        });
        if (!touched) return msg;
        return { ...msg, steps: updatedSteps };
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
            steps: updatedSteps
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
      if (!isLastAssistantStreaming()) return;

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
      if (!_responseBuffer) return;
      if (!isLastAssistantStreaming()) {
        _responseBuffer = '';
        return;
      }

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

    /** Set the final response content after all steps. */
    setResponseContent(content: string) {
      if (!isLastAssistantStreaming()) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];
      messages = [
        ...messages.slice(0, lastIndex),
        {
          ...lastMessage,
          content: lastMessage.content + content
        }
      ];
    },

    /**
     * Finalize the last assistant message after streaming completes.
     * Computes message.content from response steps for history/search.
     *
     * Note: thinking steps are NOT reclassified — the backend already
     * distinguishes actual thinking (type: "thinking") from preamble/
     * response text (type: "response") at the SSE level.
     */
    reclassifyThinkingAsResponse() {
      this._forceFlush();
      if (!isLastAssistantStreaming()) return;

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
              content: responseContent
            }
          ];
        }
      }
    },

    setMessages(newMessages: Message[]) {
      _instantScroll = true;
      messages = newMessages;
    },

    setInstantScroll(v: boolean) {
      _instantScroll = v;
    },

    /**
     * Enter edit mode for a prior user message. The composer seeds itself
     * from editingDraft/editingImageAttachments; nothing is sent or removed
     * until the user submits the edited prompt.
     */
    beginEdit(
      messageId: string,
      draftText: string,
      imageAttachments: FileAttachment[] = []
    ) {
      editingMessageId = messageId;
      editingDraft = draftText;
      editingImageAttachments = imageAttachments;
    },

    cancelEdit() {
      editingMessageId = null;
      editingDraft = '';
      editingImageAttachments = [];
    },

    /**
     * Local transcript truncation after a successful backend rewind: drops
     * the target message and everything after it. Unrelated to the
     * provider-retry cleanup rewindLastAssistantToStablePoint().
     */
    truncateFromMessage(messageId: string) {
      const index = messages.findIndex((msg) => msg.id === messageId);
      if (index < 0) return;
      messages = messages.slice(0, index);
      if (editingMessageId && !messages.some((msg) => msg.id === editingMessageId)) {
        this.cancelEdit();
      }
    },

    /** Open the long-press action sheet for a user bubble. */
    openActionSheet(messageId: string) {
      actionSheetMessageId = messageId;
    },

    closeActionSheet() {
      actionSheetMessageId = null;
    },

    clearMessages() {
      this._forceFlush();
      messages = [];
      activeToolCalls = new Map();
      isStreaming = false;
      contextStats = null;
      activeModel = null;
      isQueued = false;
      this.clearPendingPrompts();
      // New-thread flows call this without prepareForThreadSwitch: a seeded
      // edit or open action sheet must not survive into the fresh transcript.
      this.cancelEdit();
      this.closeActionSheet();
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
      this.clearPendingPrompts();
      this.cancelEdit();
      this.closeActionSheet();
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

          const stoppedContent = lastMessage.content
            ? lastMessage.content + '\n\n[User stopped this output]'
            : '[User stopped this output]';

          messages = [
            ...messages.slice(0, lastIndex),
            {
              ...lastMessage,
              content: stoppedContent,
              steps: updatedSteps ?? lastMessage.steps,
              status: 'complete' as const,
            }
          ];
        }
      }

      isStreaming = false;
      activeToolCalls = new Map();
      this.clearPendingPrompts();

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
     * Clears the rendered thread messages and shows a system-style notification.
     */
    handleCompacted(messagesRemoved: number, summary?: string, autoResumed: boolean = false) {
      this._forceFlush();
      // Clear all messages and streaming state
      activeToolCalls = new Map();
      isStreaming = autoResumed;
      isQueued = false;
      isCompacting = false;
      compactingMessage = '';
      this.clearPendingPrompts();

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
            timestamp: new Date(),
            status: 'streaming' as const
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

    handleToolReload(
      tools: string[],
      ttl: string,
      ttlSeconds: number | null,
      source?: string,
      skillName?: string | null,
      reason?: string | null
    ) {
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
          toolReloadInfo: { tools, ttl, ttlSeconds, source, skillName, reason } as ToolReloadInfo,
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
    },

    addPendingPrompt(content: string, attachments?: FileAttachment[]): string {
      const id = generateId();
      pendingPrompts = [
        ...pendingPrompts,
        {
          id,
          content,
          attachments,
          status: 'sending',
          timestamp: new Date()
        }
      ];
      return id;
    },

    setPendingPromptStatus(
      id: string,
      status: PendingPromptStatus,
      errorMessage?: string,
      position?: number
    ) {
      pendingPrompts = pendingPrompts.map((p) =>
        p.id === id
          ? { ...p, status, errorMessage, position: position ?? p.position }
          : p
      );
    },

    removePendingPrompt(id: string) {
      pendingPrompts = pendingPrompts.filter((p) => p.id !== id);
      const ctrl = pendingPromptAborts.get(id);
      if (ctrl) {
        try { ctrl.abort(); } catch { /* already aborted */ }
        pendingPromptAborts.delete(id);
      }
    },

    consumeQueuedPrompts(n: number): PendingPrompt[] {
      if (n <= 0) return [];
      const consumed: PendingPrompt[] = [];
      const remaining: PendingPrompt[] = [];
      for (const p of pendingPrompts) {
        if (consumed.length < n && p.status !== 'error') {
          consumed.push(p);
        } else {
          remaining.push(p);
        }
      }
      pendingPrompts = remaining;
      for (const p of consumed) {
        const ctrl = pendingPromptAborts.get(p.id);
        if (ctrl) {
          try { ctrl.abort(); } catch { /* already aborted */ }
          pendingPromptAborts.delete(p.id);
        }
      }
      return consumed;
    },

    registerPendingPromptAbort(id: string, controller: AbortController) {
      pendingPromptAborts.set(id, controller);
    },

    clearPendingPrompts() {
      for (const ctrl of pendingPromptAborts.values()) {
        try { ctrl.abort(); } catch { /* already aborted */ }
      }
      pendingPromptAborts.clear();
      pendingPrompts = [];
    }
  };
}

export const chatStore = createChatStore();
