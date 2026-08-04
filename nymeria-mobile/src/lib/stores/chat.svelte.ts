import type { Message, MessageStep, ToolCall, ToolCallStatus, CommandResultLevel, FileAttachment, ContextStats, ToolReloadInfo, TurnPausedInfo, FallbackPromptInfo, WorkspaceArtifact, DispatchInfo, PendingPrompt, PendingPromptStatus, StopThreadResult, ThreadTurnStatus, ViewerAttachRequest } from '$lib/types';
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

// How long stopGenerating waits for the backend's cancelled frame before
// force-finalizing locally (dead backend / SSE channel already gone).
const STOP_FALLBACK_MS = 8000;

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
  // Stop lifecycle (backlog #11): true from the stop tap until the backend's
  // cancelled frame (or the fallback timer) finalizes the turn.
  let isStopping = $state(false);
  let stopFallbackTimer: ReturnType<typeof setTimeout> | null = null;
  // Composer restore channel (backlog #16): texts a stop handed back, consumed
  // by InputBar, which appends them to the current composer draft.
  let composerRestore = $state('');
  // Resume request channel (backlog #27): bumped by the pause card's Resume
  // button, consumed by ChatPanel, which runs the message-less /resume turn.
  let resumeRequest = $state(0);
  // Live-attach request channel (backlog #87): set by navigation (thread
  // open) when the thread has an in-flight holder turn this client does not
  // own; consumed by ChatPanel, which replays and tails the turn buffer as a
  // viewer.
  let viewerAttachRequest = $state<ViewerAttachRequest | null>(null);
  let viewerAttachSeq = 0;
  // Thread the chat panel is currently rendering from the turn buffer
  // (viewer attach or dropped-stream recovery). While set, the autonomous
  // store must not apply bus transcript events for that thread: the buffer
  // replay is the single renderer, and a stale autonomous-store binding
  // could otherwise double-render the turn (backlog #90 slice 2).
  let bufferAttachedThreadId = $state<string | null>(null);
  // Texts already restored during the current stop cycle: the fallback timer,
  // the unreachable-backend path, and the (possibly late) stop response can
  // each restore, so dedupe across them. Reset when a stop starts.
  let stopRestoredTexts: Set<string> = new Set();
  // Interactive-stream recovery: true while ChatPanel's re-attach loop is
  // trying to rejoin a dropped turn. isStreaming stays true throughout so
  // the autonomous store keeps standing down and the composer stays gated.
  let isReconnecting = $state(false);
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
    get isStopping() {
      return isStopping;
    },
    get isReconnecting() {
      return isReconnecting;
    },
    get composerRestore() {
      return composerRestore;
    },
    get resumeRequest() {
      return resumeRequest;
    },
    get viewerAttachRequest() {
      return viewerAttachRequest;
    },
    get bufferAttachedThreadId() {
      return bufferAttachedThreadId;
    },
    setBufferAttachedThread(threadId: string | null) {
      bufferAttachedThreadId = threadId;
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

    addCommandResult(
      commandInput: string,
      content: string,
      success: boolean,
      level?: CommandResultLevel
    ): string {
      const id = generateId();
      const message: Message = {
        id,
        role: 'system',
        kind: 'command_result',
        commandInput,
        content,
        timestamp: new Date(),
        status: success ? 'complete' : 'error',
        // Typed outcome accent (backlog #135); derives from the success
        // boolean for callers without a wire level.
        commandLevel: level ?? (success ? 'success' : 'error')
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

    /**
     * Mark the in-flight reply as reconnecting after a dropped interactive
     * stream. The message stays streaming (the recovery loop owns the
     * terminal transition). Un-flushed token buffers are discarded: the
     * turn replay rebuilds the message content from scratch.
     */
    setReconnecting(value: boolean) {
      isReconnecting = value;
      if (value) {
        if (_flushTimer) {
          clearTimeout(_flushTimer);
          _flushTimer = null;
        }
        _responseBuffer = '';
        _thinkingBuffer = '';
      }
    },

    /**
     * Reset the tail assistant message to an empty streaming placeholder so
     * a turn re-attach can rebuild the whole reply from the replayed stream
     * (the replay is the full turn from seq 0, byte-identical to the
     * original). Preserves message identity/dispatch info.
     */
    resetLastMessageForReplay() {
      if (_flushTimer) {
        clearTimeout(_flushTimer);
        _flushTimer = null;
      }
      _responseBuffer = '';
      _thinkingBuffer = '';
      if (messages.length === 0) return;
      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];
      if (lastMessage.role !== 'assistant') return;
      activeToolCalls = new Map();
      // No legacy intermediateContent/toolCalls writes here: the mobile
      // store never materializes them (pinned by
      // test_mobile_legacy_message_fields.py); the streaming tail carries
      // steps only and MessageBubble derives fallbacks at render time.
      messages = [
        ...messages.slice(0, lastIndex),
        {
          ...lastMessage,
          content: '',
          steps: [],
          errorText: undefined,
          status: 'streaming' as const
        }
      ];
    },

    /**
     * Store the turn error as structured state on the tail assistant message
     * (backlog #98): the renderer draws it as an in-bubble alert block, so
     * the text is NOT appended into content/steps markdown (the old idiom,
     * which a separate alert block would double-render). Whatever streamed
     * before the failure stays untouched above the block.
     */
    setLastMessageError(error: string) {
      this._forceFlush();
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            errorText: (error || 'The reply could not be completed.').trim(),
            status: 'error'
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

      // Ask-mode post-chunk recovery: the fallback consent card's carrier
      // sits between the failed attempt and this event (the failed partial
      // output lives in the PREVIOUS bubble, completed when the card was
      // pushed; the re-drive streams into the carrier). Walk back past
      // still-empty carriers so the trim hits the bubble that actually
      // holds the failed attempt's steps.
      let targetIndex = messages.length - 1;
      while (
        targetIndex > 0 &&
        messages[targetIndex].fallbackPromptInfo &&
        (messages[targetIndex].steps || []).length === 0 &&
        messages[targetIndex - 1].role === 'assistant'
      ) {
        targetIndex--;
      }
      const lastMessage = messages[targetIndex];
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
        ...messages.slice(0, targetIndex),
        {
          ...lastMessage,
          content: responseContent,
          steps
        },
        ...messages.slice(targetIndex + 1)
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
      // New-thread flows call this without prepareForThreadSwitch: stop
      // state, an unconsumed composer restore, a seeded edit, or an open
      // action sheet must not survive into the fresh transcript.
      this.clearStopping();
      composerRestore = '';
      this.clearPendingPrompts();
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
      this.clearStopping();
      composerRestore = '';
      _lastFlushTime = 0;
      this.clearPendingPrompts();
      this.cancelEdit();
      this.closeActionSheet();
    },

    removeMessage(messageId: string) {
      messages = messages.filter((msg) => msg.id !== messageId);
    },

    /**
     * Stop the current generation (backlog #11 + #16).
     *
     * Marks the store "stopping" and asks the backend to abort, then
     * finalizes when the backend's cancelled frame arrives (the SSE error
     * handler calls finalizeStopped), when the backend reports the thread
     * was already idle, or when the fallback timer fires (unreachable
     * backend). Queued prompts the backend hands back are appended to the
     * composer instead of being discarded.
     */
    async stopGenerating(threadId?: string) {
      if (!isStreaming || isStopping) return;
      isStopping = true;
      stopRestoredTexts = new Set();
      this._forceFlush();

      if (!threadId) {
        // Nothing to signal: local force-stop (the pre-#11 behavior).
        abortCurrentStream();
        this._restoreLocalPendingForStop();
        this.finalizeStopped();
        return;
      }

      stopFallbackTimer = setTimeout(() => {
        stopFallbackTimer = null;
        if (isStopping) {
          abortCurrentStream();
          this._restoreLocalPendingForStop();
          this.finalizeStopped();
        }
      }, STOP_FALLBACK_MS);

      let result: StopThreadResult;
      try {
        result = await api.stopThread(threadId);
      } catch {
        // Backend unreachable: force-stop locally with what we know.
        if (isStopping) {
          abortCurrentStream();
          this._restoreLocalPendingForStop();
          this.finalizeStopped();
        }
        return;
      }

      // Restore what the backend handed back REGARDLESS of the stopping
      // flag: the cancelled frame (or the fallback timer) may have
      // finalized during the await, but these prompts are already drained
      // server-side and this response is the only copy the initiating
      // client gets (the queue_restored sync event is origin-suppressed).
      // Local entries that never reached the backend queue come back too;
      // _restoreForStop dedupes texts across the restore paths (a prompt
      // can be backend-queued but still 'sending' locally when its ack
      // has not landed yet).
      const texts = result.restoredPrompts.map((p) => p.text);
      for (const p of pendingPrompts) {
        texts.push(p.content);
      }
      this._restoreForStop(texts);
      this.clearPendingPrompts();

      if (!isStopping) return; // the turn already finalized during the await

      if (result.status === 'idle') {
        // No turn was running server-side; the stream is already dead.
        abortCurrentStream();
        this.finalizeStopped();
      }
      // status 'stopping': the running stream keeps draining until the
      // cancelled frame (or stream close) finalizes, so in-flight events
      // are consumed instead of racing an optimistic edit.
    },

    /**
     * Restore texts to the composer once per stop cycle: the fallback
     * timer, the unreachable-backend path, and a late stop response can
     * all attempt a restore for the same stop, so texts already handed
     * back this cycle are skipped.
     */
    _restoreForStop(texts: string[]) {
      const fresh: string[] = [];
      for (const text of texts) {
        if (!text || !text.trim() || stopRestoredTexts.has(text)) continue;
        stopRestoredTexts.add(text);
        fresh.push(text);
      }
      if (fresh.length) this.restoreToComposer(fresh);
    },

    /** Stop-cycle variant of restoreLocalPendingToComposer (deduped). */
    _restoreLocalPendingForStop() {
      this._restoreForStop(pendingPrompts.map((p) => p.content));
      this.clearPendingPrompts();
    },

    /**
     * Finalize a stopped turn's rendering: mark running tool-call steps
     * cancelled, append the stopped note, and clear the stopping and
     * streaming flags. Runs when the backend's cancelled frame arrives,
     * when the stream closes while stopping, or from the local fallback
     * paths. Idempotent.
     *
     * Steps only, deliberately: the mobile store never writes the legacy
     * message.toolCalls field (render-time derivation from steps; pinned
     * by test_mobile_legacy_message_fields.py), and a streaming message
     * here is always store-created with steps, so cancelled step statuses
     * are what MessageBubble renders.
     */
    finalizeStopped() {
      this.clearStopping();
      if (!isStreaming) return;
      this._forceFlush();

      // Mark last assistant message: update running tool-call steps to 'cancelled'
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
    },

    /** Clear the stopping flag and its fallback timer without finalizing. */
    clearStopping() {
      if (stopFallbackTimer) {
        clearTimeout(stopFallbackTimer);
        stopFallbackTimer = null;
      }
      isStopping = false;
    },

    /**
     * Queue restored-prompt texts for the composer, joined by `---` lines.
     * InputBar consumes the channel and appends to the current draft.
     */
    restoreToComposer(texts: string[]) {
      const cleaned = texts.map((t) => t.trim()).filter((t) => t.length > 0);
      if (cleaned.length === 0) return;
      const joined = cleaned.join('\n---\n');
      composerRestore = composerRestore ? `${composerRestore}\n---\n${joined}` : joined;
    },

    /** Pop the composer-restore channel (InputBar's consume side). */
    consumeComposerRestore(): string {
      const value = composerRestore;
      composerRestore = '';
      return value;
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

    // Turn-safety halt card (backlog #27): the turn stopped at its iteration
    // limit (or a repeated tool loop). Unlike handleToolReload, the pushed
    // message is complete: the turn is over until the user resumes or sends.
    handleTurnPaused(info: TurnPausedInfo) {
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
          status: 'complete' as const,
          turnPausedInfo: { ...info },
        }
      ];
    },

    // LLM fallback consent prompt (llm-fallback-consent Phase 2): the turn
    // parked before a model switch and is waiting for the user's decision
    // (auto-swap on timeout). Follows the handleToolReload carrier idiom: the
    // pushed message is a STREAMING assistant carrier, so when the turn
    // continues (swap approved, or the timeout auto-swap) the retried model's
    // output flows into this carrier's bubble right under the card.
    handleFallbackPrompt(info: FallbackPromptInfo) {
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
          fallbackPromptInfo: { ...info },
        }
      ];
    },

    // Stamp the card's outcome when fallback_prompt_resolved arrives (from
    // this client's resolve, another client's, or the backend timeout /
    // abort). Global, not thread-gated, so a late thread switch never
    // strands live buttons; a stale record id no-ops.
    resolveFallbackPromptCard(
      recordId: string,
      resolved: NonNullable<FallbackPromptInfo['resolved']>
    ) {
      for (let i = messages.length - 1; i >= 0; i--) {
        const info = messages[i].fallbackPromptInfo;
        if (info && info.recordId === recordId) {
          if (info.resolved) return; // First resolution wins (optimistic clear vs SSE)
          messages = [
            ...messages.slice(0, i),
            { ...messages[i], fallbackPromptInfo: { ...info, resolved: { ...resolved } } },
            ...messages.slice(i + 1)
          ];
          return;
        }
      }
    },

    /**
     * A pre-output provider refusal was rewound server-side (backlog #105):
     * the backend removed the refused user+assistant exchange from the
     * checkpoint. Mirror that locally so the transcript matches the server,
     * append the explanation notice, and hand the refused prompt back to the
     * composer (no auto-resend).
     *
     * Two truncation modes, in order:
     * 1. The backend anchor (`toMessageId` == the refused user message's
     *    graphMessageId): precise, and inherently safe against buffer replay
     *    (backlog #87 re-attach) because it no-ops once the refused exchange
     *    is gone from history.
     * 2. Structural fallback (drop from the last user message onward) ONLY
     *    when that user message is the fresh optimistic send (no
     *    graphMessageId yet). A settled prior turn always carries a
     *    graphMessageId, so a stale `turn_rewound` replayed against
     *    already-rewound history is a no-op here.
     */
    handleTurnRewound(info: {
      toMessageId?: string;
      prompt?: string;
      content: string;
      autonomous?: boolean;
    }) {
      // Autonomous refusals (TODO/trigger/dream) are rewound server-side; the
      // GUI must not truncate its interactive transcript or push the
      // autonomous prompt into the composer.
      if (info.autonomous) return;

      this._forceFlush();

      let cutIndex = -1;
      if (info.toMessageId) {
        cutIndex = messages.findIndex((m) => m.graphMessageId === info.toMessageId);
      }
      if (cutIndex < 0) {
        for (let i = messages.length - 1; i >= 0; i--) {
          const msg = messages[i];
          if (msg.role === 'user') {
            if (!msg.graphMessageId) {
              cutIndex = i;
              // A queued-prompt batch renders as CONSECUTIVE optimistic user
              // bubbles and the server rewinds the whole run, so cut from
              // the first of the run. A settled bubble (graphMessageId set)
              // stops the walk: the anchor path owns settled history.
              while (
                cutIndex > 0 &&
                messages[cutIndex - 1].role === 'user' &&
                !messages[cutIndex - 1].graphMessageId
              ) {
                cutIndex--;
              }
            }
            break;
          }
        }
      }
      if (cutIndex < 0) {
        // Nothing local to rewind (e.g. a buffered event replayed after the
        // rewind already settled into history). Leave the transcript and
        // composer untouched.
        return;
      }

      messages = messages.slice(0, cutIndex);
      if (editingMessageId && !messages.some((msg) => msg.id === editingMessageId)) {
        this.cancelEdit();
      }

      messages = [
        ...messages,
        {
          id: generateId(),
          role: 'system' as const,
          kind: 'turn_rewound' as const,
          content: info.content || 'The turn was rewound after a provider refusal.',
          timestamp: new Date(),
          status: 'complete' as const,
        }
      ];

      isStreaming = false;
      isQueued = false;
      activeToolCalls = new Map();
      // Like the other terminal handlers (compaction, thread switch): a
      // local-only pending prompt has no live turn left to drain it; a
      // server-acked one re-renders via prompt_injected when its own turn
      // fires.
      this.clearPendingPrompts();

      if (info.prompt) {
        this.restoreToComposer([info.prompt]);
      }
    },

    // Flip the most recent un-resumed pause card when the turn_resumed event
    // arrives (from this client's /resume or another client's).
    markTurnPausedResumed() {
      for (let i = messages.length - 1; i >= 0; i--) {
        const info = messages[i].turnPausedInfo;
        if (info && !info.resumed) {
          messages = [
            ...messages.slice(0, i),
            { ...messages[i], turnPausedInfo: { ...info, resumed: true } },
            ...messages.slice(i + 1)
          ];
          return;
        }
      }
    },

    // Bumped by the pause card's Resume button; ChatPanel consumes it and
    // runs the message-less /resume turn through the normal stream path.
    requestResume() {
      resumeRequest += 1;
    },

    // Ask the chat panel to live-attach to a holder turn this client did
    // not start (backlog #87). Callers pass the status `turn` block; the
    // panel replays the turn buffer from seq 0 and tails it live.
    requestViewerAttach(threadId: string, turn: ThreadTurnStatus) {
      viewerAttachSeq += 1;
      viewerAttachRequest = {
        seq: viewerAttachSeq,
        threadId,
        turnId: turn.turnId,
        userMessageId: turn.userMessageId ?? null,
        holderKind: turn.holderKind ?? 'user',
        sourceLabel: turn.sourceLabel ?? null
      };
    },

    /**
     * Drop every message after the one carrying this LangGraph message id
     * (the live turn's initiating user message). A viewer trims the
     * hydrated turn-so-far before replaying the turn buffer, which
     * re-renders the whole assistant side; without the trim the persisted
     * partial turn would render twice. Returns false when the anchor is
     * not in the current message list (nothing is trimmed).
     */
    trimAfterGraphMessageId(graphMessageId: string): boolean {
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].graphMessageId === graphMessageId) {
          if (i < messages.length - 1) {
            messages = messages.slice(0, i + 1);
          }
          return true;
        }
      }
      return false;
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
