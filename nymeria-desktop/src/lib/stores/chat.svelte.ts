import type { Message, MessageStep, ToolCall, ToolCallStatus, FileAttachment, ContextStats } from '$lib/types';
import { abortCurrentStream } from '$lib/services/api.svelte';

function generateId(): string {
  return crypto.randomUUID();
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
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        // No parsing needed - just mark as complete
        // Visibility is controlled by mute_response tool, not embedded format
        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            status: 'complete' as const,
            intermediateContent: lastMessage.intermediateContent || undefined
          }
        ];
      }
    },

    setLastMessageError(error: string) {
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        messages = [
          ...messages.slice(0, lastIndex),
          {
            ...lastMessage,
            content: lastMessage.content || error,
            status: 'error',
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
      isStreaming = streaming;
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
     */
    addThinkingStep(content: string) {
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        const steps = lastMessage.steps || [];
        const lastStep = steps[steps.length - 1];

        // If the last step is also a thinking step, append to it (streaming chunks)
        if (lastStep && lastStep.type === 'thinking') {
          const updatedSteps = [
            ...steps.slice(0, -1),
            { ...lastStep, content: (lastStep.content || '') + content }
          ];
          messages = [
            ...messages.slice(0, lastIndex),
            {
              ...lastMessage,
              steps: updatedSteps,
              // Update legacy field
              intermediateContent: this._computeIntermediateContent(updatedSteps)
            }
          ];
        } else {
          // Create new thinking step
          const newStep: MessageStep = { type: 'thinking', content };
          const updatedSteps = [...steps, newStep];
          messages = [
            ...messages.slice(0, lastIndex),
            {
              ...lastMessage,
              steps: updatedSteps,
              // Update legacy field
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
          status: 'running'
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
            // Clear content when first tool call arrives (it becomes thinking in steps)
            content: ''
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
              return { ...step, result, status };
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
     * Set the final response content (after all steps).
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
     * Reclassify the last thinking steps as response content.
     * Used when content streamed as "thinking" is actually the final response
     * (no tool calls followed).
     */
    reclassifyThinkingAsResponse() {
      if (messages.length === 0) return;

      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant' && lastMessage.steps) {
        // Find consecutive thinking steps at the end (no tool calls after them)
        const steps = lastMessage.steps;
        let thinkingContent = '';
        const newSteps: MessageStep[] = [];

        // Collect thinking content that should become response
        // If there are tool calls, only thinking AFTER the last tool call becomes response
        let lastToolCallIndex = -1;
        for (let i = steps.length - 1; i >= 0; i--) {
          if (steps[i].type === 'tool_call') {
            lastToolCallIndex = i;
            break;
          }
        }

        for (let i = 0; i < steps.length; i++) {
          const step = steps[i];
          if (i > lastToolCallIndex && step.type === 'thinking') {
            // This thinking content comes after all tool calls, make it response
            thinkingContent += step.content || '';
          } else {
            newSteps.push(step);
          }
        }

        if (thinkingContent) {
          messages = [
            ...messages.slice(0, lastIndex),
            {
              ...lastMessage,
              steps: newSteps,
              content: lastMessage.content + thinkingContent,
              intermediateContent: this._computeIntermediateContent(newSteps)
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
          status: s.status || 'pending'
        }));
    },

    setMessages(newMessages: Message[]) {
      messages = newMessages;
    },

    clearMessages() {
      messages = [];
      activeToolCalls = new Map();
      isStreaming = false;
      contextStats = null;
      activeModel = null;
      isQueued = false;
    },

    removeMessage(messageId: string) {
      messages = messages.filter((msg) => msg.id !== messageId);
    },

    /**
     * Stop the current generation and mark the message as stopped.
     * Aborts the active stream and appends context for the AI.
     */
    stopGenerating() {
      if (!isStreaming) return;

      // Abort the stream
      abortCurrentStream();

      // Mark last message as stopped with context for the AI
      const lastIndex = messages.length - 1;
      if (lastIndex >= 0) {
        const lastMessage = messages[lastIndex];
        if (lastMessage.role === 'assistant') {
          const stoppedContent = lastMessage.content
            ? lastMessage.content + '\n\n[User stopped this output]'
            : '[User stopped this output]';

          messages = [
            ...messages.slice(0, lastIndex),
            { ...lastMessage, content: stoppedContent, status: 'complete' as const }
          ];
        }
      }

      isStreaming = false;
      activeToolCalls = new Map();
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
    handleCompacted(messagesRemoved: number, summary?: string) {
      // Clear all messages and streaming state
      messages = [];
      activeToolCalls = new Map();
      isStreaming = false;
      isQueued = false;

      // Add a system-style message so the user knows what happened
      const summarySnippet = summary ? `\n\n> ${summary.slice(0, 200)}${summary.length > 200 ? '...' : ''}` : '';
      messages = [{
        id: generateId(),
        role: 'system' as const,
        content: `Conversation compacted (${messagesRemoved} messages summarized).${summarySnippet}`,
        timestamp: new Date(),
        status: 'complete' as const
      }];

      // Show the compact result indicator for 5s
      lastCompactResult = { messagesRemoved };
      setTimeout(() => {
        lastCompactResult = null;
      }, 5000);
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
