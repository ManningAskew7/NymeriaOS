<script lang="ts">
  import Icon from '$lib/components/common/Icon.svelte';
  import { ChatContainer, InputBar, ContextStatusBar, QueuedPromptsBar, MessageActionSheet } from '$lib/components/chat';
  import { ThreadSettingsPanel } from '$lib/components/threads';
  import { uiStore } from '$lib/stores/ui.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { commandsStore } from '$lib/stores/commands.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { errorsStore } from '$lib/stores/errors.svelte';
  import { rewindToMessage } from '$lib/utils/rewind';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { activityStore } from '$lib/stores/activity.svelte';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { skillsStore } from '$lib/stores/skills.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText, isConnectivityError } from '$lib/services/api/humanizeError';
  import { isTodoTool } from '$lib/utils/todoTools';
  import { untrack } from 'svelte';
  import type { DispatchInfo, FileAttachment, RestoredPrompt, SSEEvent, ThreadStatus } from '$lib/types';

  let currentTitle = $derived(threadsStore.currentThread?.title ?? 'New Thread');
  let showThreadSettings = $state(false);

  // Load global stores for header badges
  $effect(() => {
    if (configStore.isConfigured) {
      untrack(() => {
        if (!defaultToolsStore.loaded && !defaultToolsStore.loading) defaultToolsStore.load();
        if (!serverSettingsStore.loaded && !serverSettingsStore.loading) serverSettingsStore.load();
        if (!triggersStore.loaded && !triggersStore.loading) triggersStore.loadTriggers();
        if (!skillsStore.enabledGlobalLoaded && !skillsStore.enabledGlobalLoading) skillsStore.loadGlobal();
      });
    }
  });

  // Load thread config when thread changes
  $effect(() => {
    const tid = threadsStore.currentThreadId;
    const thread = threadsStore.currentThread;
    if (tid && !thread?.recovered) {
      untrack(() => {
        threadConfigStore.loadConfig(tid).catch((err) => {
          console.warn('[ChatPanel] Failed to load thread config:', err);
        });
      });
    }
  });

  const currentThreadConfig = $derived(
    threadsStore.currentThreadId
      ? threadConfigStore.getConfig(threadsStore.currentThreadId) ?? null
      : null
  );

  function shortModelName(modelId: string): string {
    const parts = modelId.split('/');
    return parts[parts.length - 1];
  }

  const effectiveModel = $derived.by(() => {
    if (currentThreadConfig?.llmConfig?.model) {
      return { name: shortModelName(currentThreadConfig.llmConfig.model), isOverride: true };
    }
    if (serverSettingsStore.model) {
      return { name: shortModelName(serverSettingsStore.model), isOverride: false };
    }
    return null;
  });

  function isMcpToolName(name: string): boolean {
    return name.startsWith('mcp__');
  }

  const disabledNonMcpCount = $derived(
    (currentThreadConfig?.disabledTools ?? []).filter((name) => !isMcpToolName(name)).length
  );
  const enabledOptionalNonMcpCount = $derived(
    (currentThreadConfig?.enabledTools ?? []).filter((name) => !isMcpToolName(name)).length
  );
  const disabledMcpCount = $derived(
    (currentThreadConfig?.disabledTools ?? []).filter(isMcpToolName).length
  );
  const enabledOptionalMcpCount = $derived(
    (currentThreadConfig?.enabledTools ?? []).filter(isMcpToolName).length
  );

  const activeToolCount = $derived.by(() => {
    if (!defaultToolsStore.loaded) return null;
    const defaultNonMcpCount = defaultToolsStore.defaultToolNames.filter((name) => !isMcpToolName(name)).length;
    return defaultNonMcpCount - disabledNonMcpCount + enabledOptionalNonMcpCount;
  });

  const activeMcpToolCount = $derived.by(() => {
    if (!defaultToolsStore.loaded) return null;
    const defaultMcpCount = defaultToolsStore.defaultToolNames.filter(isMcpToolName).length;
    return defaultMcpCount - disabledMcpCount + enabledOptionalMcpCount;
  });

  let activeSkillCount = $state<number | null>(null);
  let activeSkillRequestId = 0;
  let callableCount = $state<number | null>(null);
  let callableRequestId = 0;

  $effect(() => {
    const threadId = threadsStore.currentThreadId;
    const enabledSkillsKey = (currentThreadConfig?.enabledSkills ?? []).join('\x1f');
    const disabledSkillsKey = (currentThreadConfig?.disabledSkills ?? []).join('\x1f');
    const globalSkillsKey = skillsStore.enabledGlobal.join('\x1f');
    const requestId = ++activeSkillRequestId;
    activeSkillCount = null;

    if (!threadId) {
      return;
    }

    api.getThreadActiveSkills(threadId)
      .then((response) => {
        if (requestId !== activeSkillRequestId) return;
        activeSkillCount = response.skills.length;
      })
      .catch((err) => {
        if (requestId !== activeSkillRequestId) return;
        console.warn('[ChatPanel] Failed to load active skills:', err);
        activeSkillCount = null;
      });

    void enabledSkillsKey;
    void disabledSkillsKey;
    void globalSkillsKey;
  });

  $effect(() => {
    const threadId = threadsStore.currentThreadId;
    const disabledToolsKey = (currentThreadConfig?.disabledTools ?? []).join('\x1f');
    const callableTeamKey = `${currentThreadConfig?.callableTeamId ?? ''}\x1f${currentThreadConfig?.callableTeamName ?? ''}`;
    const callableStateKey = `${currentThreadConfig?.callable ?? false}\x1f${currentThreadConfig?.callableName ?? ''}`;
    const callableThreadsKey = threadsStore.threads
      .map((item) => `${item.id}:${item.callable ?? false}:${item.platform ?? ''}`)
      .join('\x1f');
    const requestId = ++callableRequestId;
    callableCount = null;

    if (!threadId) {
      return;
    }

    api.getThreadCallableTools(threadId)
      .then((response) => {
        if (requestId !== callableRequestId) return;
        callableCount = response.callable_thread_count;
      })
      .catch((err) => {
        if (requestId !== callableRequestId) return;
        console.warn('[ChatPanel] Failed to load callable tools:', err);
        callableCount = null;
      });

    void disabledToolsKey;
    void callableTeamKey;
    void callableStateKey;
    void callableThreadsKey;
  });

  const triggerCount = $derived(
    threadsStore.currentThreadId
      ? triggersStore.triggers.filter(t => t.enabled && t.thread_id === threadsStore.currentThreadId).length
      : 0
  );

  const hasInstructions = $derived(!!currentThreadConfig?.instructions);
  const isCallable = $derived(currentThreadConfig?.callable ?? false);
  const hasBadges = $derived(
    effectiveModel !== null || activeToolCount !== null || (callableCount !== null && callableCount > 0) ||
    activeMcpToolCount !== null || activeSkillCount !== null ||
    triggerCount > 0 || hasInstructions || isCallable
  );
  // Slash commands whose execution_kind is `chat_stream` on the backend must
  // route through the /chat SSE endpoint, not /commands/execute. The root
  // set comes from the shared commands store (one catalog fetch also serving
  // the palette), which degrades to a static fallback on a failed fetch.

  async function handleSend(message: string, attachments?: FileAttachment[]) {
    if (!message.trim() && (!attachments || attachments.length === 0)) return;

    // Edit-and-resend (backlog #12): the composer was seeded from a prior user
    // message; sending commits the deferred rewind, then streams the edited
    // prompt as a fresh turn. Checked before the queue/streaming gates so an
    // edit-send never silently queues behind a turn that started mid-edit.
    if (chatStore.isEditing) {
      await performEditResend(message, attachments);
      return;
    }

    const trimmed = message.trim();
    const slashRoot = trimmed.startsWith('/') ? trimmed.split(/\s+/)[0].toLowerCase() : '';
    // Snapshot the streaming state BEFORE the first-send catalog await: a
    // turn starting mid-await must not reroute this send into the queue
    // gate (the backend's busy-thread prompt queue covers the stale-read
    // race in the other direction).
    const streamingAtSend = chatStore.isStreaming;
    const isChatStreamCommand = slashRoot !== '' && (await commandsStore.chatStreamRoots()).has(slashRoot);

    // While streaming: queue the prompt sub-turn-style. Slash commands and
    // attachments cannot be queued (backend rejects), so fall back to the
    // pre-existing gate for those cases.
    if (streamingAtSend) {
      if (attachments && attachments.length > 0) return;
      if (trimmed.startsWith('/') && !isChatStreamCommand) return;
      if (!threadsStore.currentThreadId) return;
      void queueOnBusyThread(trimmed);
      return;
    }

    if (trimmed.startsWith('/') && !isChatStreamCommand && (!attachments || attachments.length === 0)) {
      if (!threadsStore.currentThreadId) {
        const thread = threadsStore.createThread();
        threadsStore.selectThread(thread.id);
      }
      const threadId = threadsStore.currentThreadId || undefined;
      try {
        const result = await api.executeCommand(trimmed, threadId);
        chatStore.addCommandResult(trimmed, result.markdown, result.success, result.level);
      } catch (error) {
        // The card is the ONE error surface for a failed command (backlog
        // #135): humanized copy, error accent from the store's level
        // fallback, no toast duplicate (the API layer no longer pushes one).
        chatStore.addCommandResult(
          trimmed,
          humanizeErrorText(error, { action: 'run', resource: `the ${slashRoot} command` }),
          false
        );
      }
      return;
    }

    await streamMessage(message, attachments);
  }

  /** Add the user message and stream the agent's reply. Shared by the normal
   *  send path and the edit-and-resend flow (after its rewind lands). */
  async function streamMessage(
    message: string,
    attachments?: FileAttachment[],
    opts: { echoUser?: boolean } = {}
  ) {
    // echoUser=false runs a turn without a user bubble or title change: the
    // /resume re-drive (backlog #27) adds nothing to the conversation.
    const echoUser = opts.echoUser !== false;
    // Create thread if needed
    if (!threadsStore.currentThreadId) {
      const thread = threadsStore.createThread();
      threadsStore.selectThread(thread.id);
    }

    const threadId = threadsStore.currentThreadId!;

    if (echoUser) {
      // Auto-title from first message
      threadsStore.autoTitleFromMessage(threadId, message);

      // Add user message to chat
      chatStore.addUserMessage(message, attachments);
    }
    chatStore.addAssistantMessage();
    chatStore.setStreaming(true);

    // Holder-turn id from the stream's turn_started event; the re-attach
    // handle if this connection drops mid-turn.
    let activeTurnId: string | null = null;
    let recovering = false;

    try {
      for await (const event of api.chatStream(message, threadId, attachments)) {
        if (event.type === 'turn_started') {
          activeTurnId = ((event.data as { turnId?: string })?.turnId) || null;
          continue;
        }
        handleSSEEvent(event, threadId);
      }
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        // User cancelled
      } else if (isConnectivityError(error) && activeTurnId) {
        // The backend keeps the turn running after a dropped connection.
        // Hand off to the recovery loop, which re-attaches to the turn's
        // buffered stream (or reconciles from history when it is gone).
        // Recovery owns all terminal state transitions from here.
        // Gated on activeTurnId (from turn_started, the holder turn's first
        // frame): without it the turn never demonstrably started server-side
        // (connect failure at send, or this stream was a queued observer of
        // another turn), so recovery could wipe the unsent user message or
        // replay a foreign turn; keep the plain error instead.
        recovering = true;
        void recoverInterruptedTurn(threadId, activeTurnId);
        return;
      } else {
        chatStore.setLastMessageError(
          humanizeErrorText(error, { action: 'send', resource: 'your message' })
        );
      }
    } finally {
      if (!recovering) {
        finalizeStreamCleanup();
      }
    }
  }

  // Resume request from the pause card (backlog #27): run the message-less
  // /resume turn. The "/resume" text is the chat_stream command the backend
  // intercepts (no HumanMessage is recorded); echoUser=false keeps it out of
  // the visible conversation too. The high-water mark initializes from the
  // store (an untracked read), not 0: the store counter is a session-long
  // singleton, so a panel remount after an earlier resume must not see a
  // stale delta and auto-fire an unrequested /resume.
  let consumedResumeRequest = chatStore.resumeRequest;
  $effect(() => {
    const req = chatStore.resumeRequest;
    if (req > consumedResumeRequest) {
      consumedResumeRequest = req;
      if (!chatStore.isStreaming) {
        void streamMessage('/resume', undefined, { echoUser: false });
      }
    }
  });

  // Live-attach request (backlog #87): navigation (thread open) or the
  // autonomous store (task_started / turn-output signals on the open thread,
  // backlog #90 slice 3) saw an in-flight holder turn this client did not
  // start. Same consumed-counter pattern as resume: the store value is a
  // session-long singleton, so initialize the high-water mark from it to
  // keep a panel remount from replaying a stale request.
  let consumedViewerAttachSeq = chatStore.viewerAttachRequest?.seq ?? 0;
  $effect(() => {
    const req = chatStore.viewerAttachRequest;
    if (!req || req.seq <= consumedViewerAttachSeq) return;
    // Defer, without consuming, while a thread switch is loading history:
    // watchLiveTurn would bail on isLoadingHistory and the request would be
    // swallowed (navigation's own status snapshot predates a turn that
    // started mid-load, and mobile has no sync poll to re-fire it). Reading
    // isLoadingHistory here makes it a dependency: the effect re-runs when
    // the load completes and consumes the request then (backlog #90 slice 3
    // review).
    if (chatStore.isLoadingHistory) return;
    consumedViewerAttachSeq = req.seq;
    if (!chatStore.isStreaming && threadsStore.currentThreadId === req.threadId) {
      void watchLiveTurn(req.threadId, req.turnId, req.userMessageId);
    }
  });

  /**
   * Watch a holder turn this client did not start (backlog #87): anchor the
   * hydrated history to the turn start, bind a streaming reply bubble, then
   * reuse the recovery loop to replay the turn buffer from seq 0 and tail
   * it live. The composer keeps its normal busy behavior (sends queue into
   * the running turn) and Stop/Resume act on the real turn: it is the same
   * user's turn on another device.
   */
  async function watchLiveTurn(
    threadId: string,
    turnId: string,
    userMessageId: string | null
  ) {
    if (chatStore.isStreaming || chatStore.isLoadingHistory) return;
    // Gate the composer before any await so a concurrent send queues
    // instead of racing the attach.
    chatStore.setStreaming(true);

    // The replay carries only assistant-side events, so the viewer keeps
    // history up to the turn's initiating user message and lets the replay
    // rebuild everything after it. Without the trim, the persisted
    // turn-so-far would render twice.
    let reconcileOnFinish = false;
    if (userMessageId) {
      let anchored = chatStore.trimAfterGraphMessageId(userMessageId);
      if (!anchored) {
        // The local view predates the turn or the message has not reached
        // a checkpoint yet: refresh history once and re-anchor.
        try {
          const history = await api.getThreadHistory(threadId);
          if (threadsStore.currentThreadId !== threadId || chatStore.isStopping) return;
          chatStore.setMessages(history.messages);
          anchored = chatStore.trimAfterGraphMessageId(userMessageId);
        } catch {
          // Unreachable backend: the recovery loop below owns retries.
        }
      }
      if (!anchored) {
        // Replay cannot re-render the user bubble; settle from history at
        // the end instead.
        reconcileOnFinish = true;
      }
      chatStore.addAssistantMessage();
    } else {
      // Message-less turn (/resume continuation): the replay carries only
      // the continuation, so reuse the trailing assistant entry (the
      // replay reset clears it) and reconcile at the end to restore the
      // pre-halt steps.
      const last = chatStore.messages[chatStore.messages.length - 1];
      if (last?.role === 'assistant') {
        chatStore.setLastMessageStreaming();
      } else {
        chatStore.addAssistantMessage();
      }
      reconcileOnFinish = true;
    }

    await recoverInterruptedTurn(threadId, turnId, {
      silentFirstAttempt: true,
      reconcileOnFinish
    });
  }

  /** Standard end-of-stream cleanup, shared by the live stream and recovery. */
  function finalizeStreamCleanup() {
    // Stream closed while a stop was still pending (no cancelled frame
    // arrived, e.g. the server tore the stream down first): finalize
    // the stopped rendering before the generic completion cleanup.
    if (chatStore.isStopping) chatStore.finalizeStopped();
    chatStore.reclassifyThinkingAsResponse();
    chatStore.setLastMessageComplete();
    chatStore.setStreaming(false);
    chatStore.clearActiveToolCalls();
  }

  // Interactive-stream recovery (re-attachable turns): backoff mirrors the
  // autonomous store's reconnect posture (linear ramp to a ceiling), bounded
  // like the CLI's recovery loop so the UI can never reconnect forever.
  const RECOVERY_BASE_DELAY_MS = 2000;
  const RECOVERY_MAX_DELAY_MS = 15000;
  const RECOVERY_MAX_ATTEMPTS = 20;
  const TURN_LOST_MESSAGE =
    'Lost connection while this reply was streaming and could not rejoin it. Showing the last saved state; the turn may still finish on the server.';

  /**
   * Rejoin a dropped interactive turn. Polls thread status with backoff;
   * when the turn's buffer is attachable, replays it (rebuilding the reply
   * bubble from the byte-identical replay) and tails it live; when the turn
   * is gone (API restart, buffer expired, foreign turn), reconciles from
   * persisted history instead.
   *
   * Also the tail half of a viewer attach (backlog #87), with two opts:
   * `silentFirstAttempt` keeps the "Reconnecting" phase off a fresh attach
   * (nothing dropped; it shows only if the first pass fails), and
   * `reconcileOnFinish` replaces the terminal cleanup with a history
   * reconcile when the viewer rendered without the turn-start anchor.
   */
  async function recoverInterruptedTurn(
    threadId: string,
    turnId: string | null,
    opts: { silentFirstAttempt?: boolean; reconcileOnFinish?: boolean } = {}
  ) {
    let reconnectingShown = false;
    const showReconnecting = () => {
      if (!reconnectingShown) {
        reconnectingShown = true;
        chatStore.setReconnecting(true);
      }
    };
    if (!opts.silentFirstAttempt) showReconnecting();
    // The buffer replay is this thread's single renderer for the duration.
    // The autonomous store never paints transcripts from the bus anymore
    // (backlog #90 slice 3), so no double-render is possible; the flag now
    // serves as this recovery loop's self-guard (see the finally below) and
    // marks the attach for anything that wants to know.
    chatStore.setBufferAttachedThread(threadId);
    let attempt = 0;
    try {
      while (true) {
        // Stop conditions: user switched threads (thread-switch machinery
        // owns the messages now) or a stop is finalizing the turn.
        if (threadsStore.currentThreadId !== threadId) return;
        if (chatStore.isStopping || !chatStore.isStreaming) return;
        if (attempt >= RECOVERY_MAX_ATTEMPTS) {
          // Bounded give-up: fall back to persisted history (which itself
          // degrades to the turn-lost error if even that is unreachable)
          // instead of reconnecting forever.
          await reconcileFromHistory(threadId);
          return;
        }

        attempt += 1;
        let status: ThreadStatus | null = null;
        try {
          status = await api.getThreadStatus(threadId);
        } catch {
          // Backend unreachable; keep backing off like the autonomous stream.
        }

        if (status) {
          const turn = status.turn;
          const turnMatches = turn && (!turnId || turn.turnId === turnId);
          if (turn && turnMatches && !turn.truncated) {
            const outcome = await replayAndTailTurn(threadId, turn.turnId);
            if (outcome === 'abandon') return;
            if (outcome === 'finished') {
              if (threadsStore.currentThreadId === threadId) {
                if (opts.reconcileOnFinish) {
                  // The viewer rendered without the turn-start anchor (user
                  // bubble or pre-halt steps missing from the replay), so
                  // settle on the canonical persisted state.
                  await reconcileFromHistory(threadId);
                } else {
                  finalizeStreamCleanup();
                }
              }
              return;
            }
            if (outcome === 'reconcile') {
              await reconcileFromHistory(threadId);
              return;
            }
            // 'retry': fall through to backoff.
          } else if (!status.processing) {
            // The turn is gone (API restart, buffer expired, or another turn
            // already ran): reconcile from persisted history.
            await reconcileFromHistory(threadId);
            return;
          }
          // Still processing but unattachable (truncated buffer or a foreign
          // turn holds the thread): keep polling until it finishes.
        }

        showReconnecting();
        await new Promise((resolve) =>
          setTimeout(resolve, Math.min(RECOVERY_BASE_DELAY_MS * attempt, RECOVERY_MAX_DELAY_MS))
        );
      }
    } finally {
      // Only clear our own claim: a switch to another live-turn thread can
      // start a newer recovery loop (which set the flag to its thread)
      // before this one notices the thread change and returns.
      if (chatStore.bufferAttachedThreadId === threadId) {
        chatStore.setBufferAttachedThread(null);
      }
      chatStore.setReconnecting(false);
    }
  }

  /** One re-attach pass: replay the buffered turn, then tail it live. */
  async function replayAndTailTurn(
    threadId: string,
    turnId: string
  ): Promise<'finished' | 'reconcile' | 'retry' | 'abandon'> {
    let sawTerminal = false;
    try {
      for await (const event of api.reattachTurnStream(threadId, turnId)) {
        if (threadsStore.currentThreadId !== threadId) return 'abandon';
        if (event.type === 'turn_attach') {
          // Full-turn replay follows: rebuild the reply from scratch so the
          // re-rendered turn is exactly what the original stream carried.
          chatStore.resetLastMessageForReplay();
          continue;
        }
        if (event.type === 'turn_started') continue;
        if (event.type === 'error') {
          const code = (event.data as { code?: string })?.code;
          if (code === 'turn_not_found' || code === 'turn_replay_gap') return 'reconcile';
          if (code === 'reattach_failed') return 'retry';
          sawTerminal = true;
        }
        if (event.type === 'done') sawTerminal = true;
        handleSSEEvent(event, threadId);
      }
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') return 'abandon';
      return 'retry';
    }
    // A clean stream end without a terminal event means the turn's writer
    // died without finishing (buffer state aborted): fall back to history.
    return sawTerminal ? 'finished' : 'reconcile';
  }

  /** Recovery fallback: replace local state with the persisted thread. */
  async function reconcileFromHistory(threadId: string) {
    try {
      const [history, stats] = await Promise.all([
        api.getThreadHistory(threadId),
        api.getThreadContextStats(threadId)
      ]);
      if (threadsStore.currentThreadId !== threadId) return;
      chatStore.setStreaming(false);
      chatStore.setLastMessageComplete();
      chatStore.clearActiveToolCalls();
      chatStore.setMessages(history.messages);
      chatStore.setContextStats(stats);
      chatStore.setActiveModel(stats?.model ?? null);
    } catch (error) {
      console.error('Turn recovery reconciliation failed:', error);
      if (threadsStore.currentThreadId !== threadId) return;
      chatStore.setLastMessageError(TURN_LOST_MESSAGE);
      finalizeStreamCleanup();
    }
  }

  /**
   * Commit a deferred prompt edit: rewind to the edited message, then send
   * the edited text (plus any carried image attachments) as a fresh turn.
   * Refusals keep edit mode, and with it the composer content (InputBar's
   * wasEditing guard skips the post-send clear), so nothing typed is lost.
   */
  // Reentrancy guard: the edit path deliberately keeps the composer populated
  // (a refused send must not lose the draft), so unlike a normal send the
  // Send button stays live during the rewind round-trip. A double submit
  // would race two rewinds: the loser 404s and reloads the transcript over
  // the winner's live stream.
  let editResendInFlight = false;

  async function performEditResend(message: string, attachments?: FileAttachment[]) {
    if (editResendInFlight) return;
    editResendInFlight = true;
    try {
      await performEditResendInner(message, attachments);
    } finally {
      editResendInFlight = false;
    }
  }

  async function performEditResendInner(message: string, attachments?: FileAttachment[]) {
    const threadId = threadsStore.currentThreadId;
    const targetId = chatStore.editingMessageId;
    if (!threadId || !targetId) {
      chatStore.cancelEdit();
      return;
    }
    // Slash commands are not prompts: an edit-send would bypass the command
    // routing above (executeCommand vs chat-stream) and silently rewind
    // before a config command. Refuse and keep the edit + composer intact.
    if (message.trim().startsWith('/')) {
      errorsStore.push({
        kind: 'generic',
        message:
          'Slash commands cannot be sent as an edited prompt. Cancel the edit to run a command.',
      });
      return;
    }
    if (chatStore.isStreaming || chatStore.isQueued) {
      errorsStore.push({
        kind: 'generic',
        message: 'The agent is busy on this thread. Wait for the turn to finish, then send your edit.',
      });
      return;
    }

    const outcome = await rewindToMessage(threadId, targetId);
    if (!outcome.ok) {
      if (outcome.reason === 'stale_target') {
        chatStore.cancelEdit();
        errorsStore.push({
          kind: 'generic',
          message:
            'The conversation changed on the backend, so it was reloaded and your edit was cancelled.',
        });
      } else {
        errorsStore.push({
          kind: 'generic',
          message: humanizeErrorText(outcome.error, {
            action: 'rewind',
            resource: 'the conversation',
          }),
        });
      }
      return;
    }

    // rewindToMessage truncated the transcript and cleared edit state; stream
    // the edited prompt as a normal fresh turn.
    await streamMessage(message, attachments);
  }

  /**
   * Send a follow-up prompt that the agent should pick up at its next sub-turn
   * halt. Adds the prompt to chatStore.pendingPrompts and POSTs in the
   * background using the queue lifecycle endpoint.
   */
  async function queueOnBusyThread(message: string) {
    const threadId = threadsStore.currentThreadId;
    if (!threadId) return;
    const promptId = chatStore.addPendingPrompt(message);
    const controller = new AbortController();
    chatStore.registerPendingPromptAbort(promptId, controller);
    try {
      for await (const event of api.queuePromptStream(message, threadId, controller)) {
        switch (event.type) {
          case 'prompt_queued': {
            const data = event.data as { position: number };
            chatStore.setPendingPromptStatus(promptId, 'queued', undefined, data.position);
            break;
          }
          case 'prompt_absorbed':
            chatStore.removePendingPrompt(promptId);
            return;
          case 'error': {
            const data = event.data as { message: string; code?: string };
            if (data.code === 'restored') {
              // A stop handed this prompt back (backlog #16); the stop path
              // restores it to the composer, so drop the bar entry silently.
              chatStore.removePendingPrompt(promptId);
              return;
            }
            chatStore.setPendingPromptStatus(promptId, 'error', data.message);
            return;
          }
        }
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return;
      const msg = err instanceof Error ? err.message : 'Could not queue your prompt. Try again in a moment.';
      chatStore.setPendingPromptStatus(promptId, 'error', msg);
    }
  }

  function handleSSEEvent(event: SSEEvent, threadId: string) {
    // Stale event guard
    if (threadsStore.currentThreadId !== threadId) return;

    switch (event.type) {
      case 'thinking':
        chatStore.addThinkingStep((event.data as { message: string }).message);
        break;

      case 'response':
        chatStore.addResponseStep((event.data as { content: string }).content);
        break;

      case 'dispatched': {
        const dispatch = event.data as DispatchInfo;
        chatStore.setLastAssistantDispatchInfo(dispatch);
        // The dispatch target (e.g. a fresh /quick thread) may not be in the
        // local list yet: the originating client is filtered out of its own
        // thread_created sync event, so ensure it exists here so the inline
        // "Response from <thread>" jump button can select it immediately.
        if (dispatch?.threadId) {
          threadsStore.ensureThread(dispatch.threadId, dispatch.title || dispatch.threadId);
        }
        break;
      }

      case 'tool_call': {
        const tc = event.data as {
          id: string;
          name: string;
          arguments: Record<string, unknown>;
          timeoutSeconds?: number;
        };
        chatStore.addToolCallStep(tc.id, tc.name, tc.arguments, tc.timeoutSeconds);
        break;
      }

      case 'tool_result': {
        const tr = event.data as {
          id?: string;
          name: string;
          result: string;
          status: string;
          durationMs?: number;
        };
        if (tr.id) {
          chatStore.updateToolCallStepResult(tr.id, tr.result, tr.status === 'error' ? 'error' : 'success', tr.durationMs);
        } else {
          chatStore.updateToolCallResultByName(tr.name, tr.result, tr.status === 'error' ? 'error' : 'success');
        }
        if (isTodoTool(tr.name)) {
          todosStore.onTodoToolCompleted();
          activityStore.fetch();
        }
        break;
      }

      case 'provider_retry': {
        const data = event.data as {
          provider?: string;
          model?: string;
          attempt?: number;
          maxRetries?: number;
          delaySeconds?: number;
          reason?: string;
          httpStatus?: number | null;
          rewound?: boolean;
          streamChunks?: number;
        };
        if (data.rewound) chatStore.rewindLastAssistantToStablePoint();
        chatStore.addProviderStatusStep({
          providerStatus: 'retry',
          provider: data.provider,
          model: data.model,
          attempt: data.attempt,
          maxRetries: data.maxRetries,
          delaySeconds: data.delaySeconds,
          reason: data.reason,
          httpStatus: data.httpStatus,
          rewound: data.rewound,
          streamChunks: data.streamChunks,
        });
        break;
      }

      case 'provider_fallback': {
        const data = event.data as {
          fromProvider?: string;
          fromModel?: string;
          toProvider?: string;
          toModel?: string;
          holdSeconds?: number;
          permanent?: boolean;
          expiresAt?: string | null;
          reason?: string;
          httpStatus?: number | null;
          rewound?: boolean;
          streamChunks?: number;
        };
        if (data.rewound) chatStore.rewindLastAssistantToStablePoint();
        chatStore.addProviderStatusStep({
          providerStatus: 'fallback',
          fromProvider: data.fromProvider,
          fromModel: data.fromModel,
          toProvider: data.toProvider,
          toModel: data.toModel,
          holdSeconds: data.holdSeconds,
          permanent: data.permanent,
          expiresAt: data.expiresAt,
          reason: data.reason,
          httpStatus: data.httpStatus,
          rewound: data.rewound,
          streamChunks: data.streamChunks,
        });
        // The swap pinned active_llm_fallback onto the thread config; refresh
        // it so the settings panel's Revert row appears live. The stale-event
        // guard above already pinned this handler to its stream's thread, so
        // the handler param is the right key. (A dispatched turn's hold lands
        // on the dispatch target instead; that panel self-corrects on open.)
        void threadConfigStore.loadConfig(threadId).catch(() => {});
        break;
      }

      case 'error': {
        const errData = event.data as { message: string; code?: string };
        if (errData.code === 'cancelled') {
          // The backend confirmed the abort (backlog #11): finalize the
          // stopped rendering from the server's frame instead of the old
          // optimistic client-side edit. Also covers stops initiated from
          // another client or surface.
          chatStore.finalizeStopped();
          break;
        }
        // Server-authored failure copy renders VERBATIM (review 2026-08-04):
        // the backend classifies stream failures into purpose-written display
        // text (rate limits, credits, tool-name faults), which the humanizer's
        // presentability filter would discard and whose "connection reset"
        // phrasing its connectivity heuristic would misread as a LOCAL drop.
        // The alert block (backlog #98) supplies the error styling.
        chatStore.setLastMessageError(
          errData.code ? `${errData.message}\n(code: ${errData.code})` : errData.message
        );
        break;
      }

      case 'done': {
        const doneData = event.data as {
          threadId: string;
          contextStats?: unknown;
          model?: string;
          title?: string;
          dispatchedTo?: DispatchInfo;
        };
        if (!doneData.dispatchedTo && doneData.contextStats) {
          chatStore.setContextStats(doneData.contextStats as import('$lib/types').ContextStats);
        }
        if (!doneData.dispatchedTo && doneData.model) {
          chatStore.setActiveModel(doneData.model);
        }
        // Ensure thread exists in sidebar
        if (doneData.dispatchedTo?.threadId) {
          threadsStore.setThreadFromApi(
            doneData.dispatchedTo.threadId,
            doneData.title || doneData.dispatchedTo.title || doneData.dispatchedTo.threadId
          );
        } else if (event.threadId) {
          threadsStore.setThreadFromApi(event.threadId, currentTitle);
        }
        // The turn completed normally; a stop that raced it has nothing
        // left to cancel, so drop the stopping state without the
        // cancelled-visuals finalization.
        if (chatStore.isStopping) chatStore.clearStopping();
        break;
      }

      case 'queued':
        chatStore.setQueued(true);
        break;

      case 'turn_halted':
        break;

      case 'prompt_injected': {
        const data = event.data as {
          count: number;
          sources?: string[];
          prompts?: RestoredPrompt[];
        };
        // Local entries still get consumed (pending-bar cleanup), but the
        // bubbles render from the wire texts when present: a prompt queued
        // on ANOTHER client (or watched by a live-attach viewer) has no
        // local copy, so the local list alone would drop its user bubble.
        // The wire list is index-parallel with `sources`; only user-source
        // prompts are user bubbles (matches history filtering), and a
        // `system` entry (mid-turn tool-expiry notice, backlog #320) is
        // the typed card. Autonomous sources render nothing here.
        const consumed = chatStore.consumeQueuedPrompts(data.count);
        chatStore.flushStreamingBuffers();
        chatStore.setLastMessageComplete();
        chatStore.clearActiveToolCalls();
        const sources = data.sources ?? [];
        if (data.prompts) {
          data.prompts.forEach((p, i) => {
            const source = sources[i] ?? 'user';
            if (source === 'system') {
              chatStore.addToolExpiryNotice(p.text);
            } else if (source === 'user' && p.text.trim()) {
              chatStore.addUserMessage(p.text);
            }
          });
        } else {
          for (const p of consumed) {
            chatStore.addUserMessage(p.content);
          }
        }
        chatStore.addAssistantMessage();
        break;
      }

      case 'prompt_absorbed':
      case 'fanout_dropped':
        break;

      case 'compacting':
        chatStore.setCompacting(true, (event.data as { message: string }).message);
        break;

      case 'compact_result':
        chatStore.setCompactResult((event.data as { messagesRemoved: number }).messagesRemoved);
        break;

      case 'compacted': {
        const cd = event.data as { messagesRemoved: number; autoResumed?: boolean; summary?: string };
        chatStore.handleCompacted(cd.messagesRemoved, cd.summary, cd.autoResumed ?? false);
        break;
      }

      case 'context_attached':
        chatStore.setContextAttached((event.data as { summary: string }).summary);
        chatStore.setLastUserMessageContextSummary((event.data as { summary: string }).summary);
        break;

      case 'iteration_limit':
        {
          // Turn-safety halt (backlog #27).
          const data = event.data as {
            message: string;
            maxIterations: number;
            reason?: 'max_iterations' | 'repeated_tool_result';
            scope?: 'main_agent' | 'sub_agent';
            agentName?: string;
            toolCallCount?: number;
            repeatedToolName?: string;
            repeatedCount?: number;
            resumable?: boolean;
          };
          if (data.scope === 'sub_agent') {
            // A sub-agent limit is a MID-TURN event (it rides a tool result;
            // the main turn keeps streaming after it), so it must stay an
            // inline note: the terminal pause card would mark the streaming
            // reply complete and swallow the rest of the live output.
            const agentName = data.agentName || 'Sub-agent';
            const countText = data.toolCallCount
              ? `${data.toolCallCount}/${data.maxIterations}`
              : `${data.maxIterations}`;
            const title = data.reason === 'repeated_tool_result'
              ? `${agentName} stopped a repeated tool loop`
              : `${agentName} hit its iteration limit`;
            chatStore.addResponseStep(
              `\n\n---\n**${title} (${countText} steps).** ` +
              `${data.message || 'The sub-agent was stopped before finishing.'}`
            );
            break;
          }
          // Main-agent halt: the turn is over; render the pause card. The
          // Resume button shows only when the backend says the halt is
          // resumable (graceful cap halt).
          chatStore.handleTurnPaused({
            reason: data.reason || 'max_iterations',
            scope: data.scope || 'main_agent',
            content:
              data.message ||
              'My task may be incomplete. You can ask me to continue where I left off.',
            maxIterations: data.maxIterations,
            toolCallCount: data.toolCallCount,
            repeatedToolName: data.repeatedToolName,
            repeatedCount: data.repeatedCount,
            agentName: data.agentName,
            resumable: data.resumable === true,
          });
        }
        break;

      case 'turn_resumed':
        // A /resume re-drive started (from this client or another one
        // watching the thread): flip the pause card to its resumed state.
        chatStore.markTurnPausedResumed();
        break;

      case 'turn_rewound': {
        // A pre-output provider refusal (Fable 5 safety classifier) was
        // rewound server-side (backlog #105): drop the refused exchange
        // locally, restore the prompt to the composer, and show the notice.
        const data = event.data as {
          content: string;
          prompt?: string;
          toMessageId?: string;
          reason?: string;
          model?: string;
          autonomous?: boolean;
        };
        chatStore.handleTurnRewound({
          toMessageId: data.toMessageId,
          prompt: data.prompt,
          content: data.content,
          autonomous: data.autonomous,
        });
        break;
      }

      case 'tool_reload': {
        const data = event.data as {
          tools: string[];
          ttl: string;
          ttlSeconds: number | null;
          source?: string;
          skillName?: string | null;
          reason?: string | null;
        };
        chatStore.handleToolReload(data.tools, data.ttl, data.ttlSeconds, data.source, data.skillName, data.reason);
        break;
      }
    }
  }
</script>

<div class="chat-panel">
  <!-- Thread header -->
  <div class="chat-header">
    <button
      class="header-btn"
      onclick={() => uiStore.goToPanel('left')}
      aria-label="Threads"
    >
      <Icon name="menu" size={22} />
    </button>

    <div class="header-center">
      <div class="header-title">
        <span class="title-text">{currentTitle}</span>
      </div>
      {#if threadsStore.currentThreadId && hasBadges}
        <div class="header-badges">
          {#if effectiveModel}
            <span
              class="badge model-badge"
              class:default={!effectiveModel.isOverride}
              class:override={effectiveModel.isOverride}
            >
              {effectiveModel.name}
            </span>
          {/if}
          {#if activeToolCount !== null}
            <span class="badge tools-badge" class:reduced={disabledNonMcpCount > 0}>
              {activeToolCount} tools
            </span>
          {/if}
          {#if activeMcpToolCount !== null}
            <span class="badge mcp-badge" class:reduced={disabledMcpCount > 0}>
              {activeMcpToolCount} MCP
            </span>
          {/if}
          {#if callableCount !== null && callableCount > 0}
            <span class="badge callables-badge">
              {callableCount} callable{callableCount !== 1 ? 's' : ''}
            </span>
          {/if}
          {#if activeSkillCount !== null}
            <span class="badge skills-badge">
              {activeSkillCount} skill{activeSkillCount !== 1 ? 's' : ''}
            </span>
          {/if}
          {#if triggerCount > 0}
            <span class="badge triggers-badge">
              {triggerCount} trigger{triggerCount !== 1 ? 's' : ''}
            </span>
          {/if}
          {#if hasInstructions}
            <span class="badge instructions-badge">instructions</span>
          {/if}
          {#if isCallable}
            <span class="badge callable-badge">&lt; Callable</span>
          {/if}
        </div>
      {/if}
    </div>

    <div class="header-actions">
      {#if threadsStore.currentThreadId}
        <button
          class="header-btn"
          onclick={() => (showThreadSettings = true)}
          aria-label="Thread Settings"
        >
          <Icon name="settings" size={20} />
        </button>
      {/if}
      <button
        class="header-btn"
        onclick={() => uiStore.goToPanel('right')}
        aria-label="Dashboard"
      >
        <Icon name="bolt" size={22} />
      </button>
    </div>
  </div>

  <!-- Messages area -->
  <div class="messages-area">
    <ChatContainer />
  </div>

  <!-- Context stats (model, tokens, usage) -->
  <ContextStatusBar />

  <QueuedPromptsBar />

  <!-- Input bar -->
  <InputBar
    onSend={handleSend}
    disabled={!configStore.isConfigured}
    filesEnabled={configStore.isConfigured}
  />
</div>

{#if threadsStore.currentThreadId}
  <ThreadSettingsPanel
    open={showThreadSettings}
    threadId={threadsStore.currentThreadId}
    onClose={() => (showThreadSettings = false)}
  />
{/if}

<!-- Long-press action sheet for user bubbles (edit / rewind). -->
<MessageActionSheet />

<style>
  .chat-panel {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-base);
  }

  .chat-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 var(--spacing-sm);
    height: var(--header-height);
    border-bottom: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    flex-shrink: 0;
  }

  .header-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    transition: all var(--transition-fast);
  }

  .header-btn:active {
    background: var(--bg-hover);
    color: var(--accent-primary);
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 0;
  }

  .header-center {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    overflow: hidden;
    min-width: 0;
    gap: 2px;
  }

  .header-title {
    width: 100%;
    text-align: center;
    overflow: hidden;
  }

  .title-text {
    display: block;
    font-size: var(--font-size-base);
    font-weight: 600;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .header-badges {
    display: flex;
    align-items: center;
    gap: 3px;
    overflow-x: auto;
    max-width: 100%;
    scrollbar-width: none;
    -ms-overflow-style: none;
  }

  .header-badges::-webkit-scrollbar {
    display: none;
  }

  .badge {
    display: inline-flex;
    align-items: center;
    padding: 0px 4px;
    font-size: 8px;
    font-weight: 500;
    border-radius: var(--radius-full);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .model-badge.override {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid var(--accent-tint-border);
  }

  .model-badge.default {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
    border: 1px solid color-mix(in srgb, var(--text-muted) 25%, transparent);
  }

  .tools-badge {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid var(--accent-tint-border);
  }

  .tools-badge.reduced {
    background: color-mix(in srgb, var(--warning) 20%, transparent);
    color: var(--warning);
    border: 1px solid color-mix(in srgb, var(--warning) 30%, transparent);
  }

  .mcp-badge {
    background: color-mix(in srgb, var(--info) 18%, transparent);
    color: var(--info);
    border: 1px solid color-mix(in srgb, var(--info) 30%, transparent);
  }

  .mcp-badge.reduced {
    background: color-mix(in srgb, var(--warning) 18%, transparent);
    color: var(--warning);
    border: 1px solid color-mix(in srgb, var(--warning) 30%, transparent);
  }

  .callables-badge {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid var(--accent-tint-border);
  }

  .skills-badge {
    background: color-mix(in srgb, var(--success) 16%, transparent);
    color: var(--success);
    border: 1px solid color-mix(in srgb, var(--success) 28%, transparent);
  }

  .triggers-badge {
    background: color-mix(in srgb, var(--success) 20%, transparent);
    color: var(--success);
    border: 1px solid color-mix(in srgb, var(--success) 30%, transparent);
  }

  .instructions-badge {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
    border: 1px solid color-mix(in srgb, var(--text-muted) 25%, transparent);
  }

  .callable-badge {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid var(--accent-tint-border);
  }

  .messages-area {
    flex: 1;
    min-height: 0;
    overflow: hidden;
  }
</style>
