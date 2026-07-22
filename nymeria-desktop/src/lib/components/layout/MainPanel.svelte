<script lang="ts">
  import ChatContainer from '$lib/components/chat/ChatContainer.svelte';
  import InputBar from '$lib/components/chat/InputBar.svelte';
  import ContextStatusBar from '$lib/components/chat/ContextStatusBar.svelte';
  import QueuedPromptsBar from '$lib/components/chat/QueuedPromptsBar.svelte';
  import QuickActions from '$lib/components/outlook/QuickActions.svelte';
  import { ThreadHeader, ThreadSettingsPanel } from '$lib/components/threads';
  import { Button, Modal } from '$lib/components/common';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { activityStore } from '$lib/stores/activity.svelte';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { outlookStore } from '$lib/stores/outlook.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { healthStore } from '$lib/stores/health.svelte';
  import { uiStore } from '$lib/stores/ui.svelte';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText, isConnectivityError } from '$lib/services/api/humanizeError';
  import { debugLog } from '$lib/utils/debug';
  import { isTodoTool } from '$lib/utils/todoTools';
  import { isNonDesktopThreadId } from '$lib/utils/platform';
  import { refreshThreadSyncBaseline } from '$lib/stores/syncPoll.svelte';
  import { errorsStore } from '$lib/stores/errors.svelte';
  import { rewindToMessage } from '$lib/utils/rewind';
  import {
    isSkillMutationReloadSource,
    isSkillMutationToolName,
    refreshSkillStateAfterMutation
  } from '$lib/utils/skillRefresh';
  import { untrack } from 'svelte';
  import type {
    SSEEvent,
    FileAttachment,
    ContextStats,
    ThreadConfig,
    ThreadStatus,
    AttachmentValidationResult,
    DispatchInfo,
    RestoredPrompt
  } from '$lib/types';

  let showThreadSettings = $state(false);
  let showAttachmentWarningModal = $state(false);
  let pendingInsertText = $state('');
  let attachmentValidationResult = $state<AttachmentValidationResult | null>(null);
  let warningSuppressChecked = $state(false);
  let pendingSend = $state<{ message: string; attachments?: FileAttachment[] } | null>(null);

  let bothSidebarsOpen = $derived(!uiStore.sidebarCollapsed && !uiStore.rightPanelCollapsed);

  // Ensure the global stores behind the thread-header badges are loaded.
  // Tracked deps: the current thread (stats hydrate on thread open, not on
  // first settings-panel visit) and the loaded flags that reset on an
  // identity switch (defaultTools/triggers latch `loaded = true` even on a
  // failed fetch, so their flips are bounded; serverSettings does NOT latch
  // on error, but its `loading` flip is untracked here, so a failed load
  // simply leaves `loaded` false until another tracked dep re-runs this
  // effect — a bounded retry, not a loop). The load calls run untracked so
  // a load's own `loading` flip can never re-trigger this effect.
  $effect(() => {
    if (!configStore.isConfigured) return;
    void threadsStore.currentThreadId;
    void defaultToolsStore.loaded;
    void triggersStore.loaded;
    void serverSettingsStore.loaded;
    untrack(() => {
      if (!defaultToolsStore.loaded && !defaultToolsStore.loading) void defaultToolsStore.load();
      if (!serverSettingsStore.loaded && !serverSettingsStore.loading) void serverSettingsStore.load();
      if (!triggersStore.loaded && !triggersStore.loading) void triggersStore.loadTriggers();
    });
  });

  // When the backend link comes back, retry header-stat loads that latched a
  // failure (their catch blocks mark `loaded` to stop effect loops, so they
  // never self-retry). Tracked dep is the connection flag alone: one retry
  // per reconnect, no loops on a persistently failing endpoint.
  $effect(() => {
    if (!healthStore.connected) return;
    untrack(() => {
      if (defaultToolsStore.error && !defaultToolsStore.loading) void defaultToolsStore.reload();
      if (triggersStore.error && !triggersStore.loading) void triggersStore.loadTriggers();
      if (!serverSettingsStore.loaded && !serverSettingsStore.loading) void serverSettingsStore.load();
    });
  });

  // Load thread config when thread changes
  $effect(() => {
    const tid = threadsStore.currentThreadId;
    const thread = threadsStore.currentThread;
    if (tid && !thread?.recovered) {
      untrack(() => {
        threadConfigStore.loadConfig(tid).catch((err) => {
          console.warn('[MainPanel] Failed to load thread config:', err);
        });
      });
    }
  });

  const currentThreadConfig = $derived(
    threadsStore.currentThreadId
      ? threadConfigStore.getConfig(threadsStore.currentThreadId) ?? null
      : null
  );

  function handleConfigSaved(config: ThreadConfig) {
    // Config is already in the store via updateConfig/deleteConfig
  }

  function closeAttachmentWarningModal() {
    showAttachmentWarningModal = false;
    attachmentValidationResult = null;
    warningSuppressChecked = false;
    pendingSend = null;
  }

  // Interactive-stream recovery (re-attachable turns): backoff mirrors the
  // autonomous store's reconnect posture (linear ramp to a ceiling), bounded
  // like the CLI's recovery loop so the UI can never reconnect forever.
  const RECOVERY_BASE_DELAY_MS = 2000;
  const RECOVERY_MAX_DELAY_MS = 15000;
  const RECOVERY_MAX_ATTEMPTS = 20;
  const TURN_LOST_MESSAGE =
    'Lost connection while this reply was streaming and could not rejoin it. Showing the last saved state; the turn may still finish on the server.';

  /** Standard end-of-stream cleanup, shared by the live stream and recovery. */
  function finalizeStreamCleanup(threadId: string | undefined) {
    // Stream closed while a stop was still pending (no cancelled frame
    // arrived, e.g. the server tore the stream down first): finalize
    // the stopped rendering before the generic completion cleanup.
    if (chatStore.isStopping) chatStore.finalizeStopped();
    chatStore.setStreaming(false);
    chatStore.setLastMessageComplete();
    chatStore.clearActiveToolCalls();
    // Update sync poll baseline so it doesn't re-fetch what we just streamed
    if (threadId) void refreshThreadSyncBaseline(threadId);
  }

  async function streamMessage(
    message: string,
    attachments?: FileAttachment[],
    forceUnsupportedAttachments: boolean = false,
    opts: { echoUser?: boolean } = {}
  ) {
    // echoUser=false runs a turn without a user bubble or title change: the
    // /resume re-drive (backlog #27) adds nothing to the conversation.
    const echoUser = opts.echoUser !== false;
    if ((!message.trim() && (!attachments || attachments.length === 0)) || chatStore.isStreaming) return;

    // Ensure a thread exists before sending — prevents the backend from
    // generating a fallback ID that triggers syncThreadIdFromEvent's replace logic
    if (!threadsStore.currentThreadId) {
      threadsStore.createThread();
    }

    // Auto-title the thread from the first message if it's still the default
    // ("New Thread" for new threads, or "New Chat" for threads created before
    // the §9 terminology canon sweep).
    const currentThread = threadsStore.currentThread;
    if (
      echoUser &&
      currentThread &&
      (currentThread.title === 'New Thread' || currentThread.title === 'New Chat') &&
      message.trim()
    ) {
      threadsStore.autoTitleFromMessage(currentThread.id, message);
    }

    // Add user message with optional attachments
    if (echoUser) {
      chatStore.addUserMessage(message, attachments);
    }

    // Create assistant message placeholder
    chatStore.addAssistantMessage();
    chatStore.setStreaming(true);

    const threadId = threadsStore.currentThreadId || undefined;
    // Holder-turn id from the stream's turn_started event; the re-attach
    // handle if this connection drops mid-turn.
    let activeTurnId: string | null = null;
    let recovering = false;

    try {
      for await (const event of api.chatStream(message, threadId, attachments, forceUnsupportedAttachments)) {
        if (event.type === 'turn_started') {
          activeTurnId = ((event.data as { turnId?: string })?.turnId) || null;
          continue;
        }
        handleSSEEvent(event);
      }
    } catch (error) {
      // Check if this was an intentional abort (user clicked stop)
      if (error instanceof DOMException && error.name === 'AbortError') {
        // User stopped - already handled in stopGenerating()
        return;
      }
      console.error('Chat error:', error);
      if (isConnectivityError(error) && threadId && activeTurnId) {
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
      }
      chatStore.setLastMessageError(
        humanizeErrorText(error, { action: 'send', resource: 'your message' })
      );
    } finally {
      // Only touch chat store if we're still on the stream's original thread —
      // a thread switch during streaming replaces messages, so touching them here
      // would corrupt the new thread's state.
      if (!recovering && threadsStore.currentThreadId === threadId) {
        finalizeStreamCleanup(threadId);
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
        void streamMessage('/resume', undefined, false, { echoUser: false });
      }
    }
  });

  // Live-attach request (backlog #87): navigation (thread open), the sync
  // poll, or the autonomous store (task_started / turn-output signals on the
  // open thread, backlog #90 slice 3) saw an in-flight holder turn this
  // client did not start. Same consumed-counter pattern as resume: the store
  // value is a session-long singleton, so initialize the high-water mark
  // from it to keep a panel remount from replaying a stale request.
  let consumedViewerAttachSeq = chatStore.viewerAttachRequest?.seq ?? 0;
  $effect(() => {
    const req = chatStore.viewerAttachRequest;
    if (!req || req.seq <= consumedViewerAttachSeq) return;
    // Defer, without consuming, while a thread switch is loading history:
    // watchLiveTurn would bail on isLoadingHistory and the request would be
    // swallowed (navigation's own status snapshot predates a turn that
    // started mid-load, so nothing re-fires). Reading isLoadingHistory here
    // makes it a dependency: the effect re-runs when the load completes and
    // consumes the request then (backlog #90 slice 3 review).
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
    // Gate the composer/poll before any await so a concurrent send queues
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
        // The local view predates the turn (poll-triggered attach) or the
        // message has not reached a checkpoint yet: refresh history once
        // and re-anchor.
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

  /**
   * Rejoin a dropped interactive turn. Polls thread status with backoff;
   * when the turn's buffer is attachable, replays it (rebuilding the reply
   * bubble from the byte-identical replay) and tails it live; when the turn
   * is gone (API restart, buffer expired, foreign turn), reconciles from
   * persisted history instead. Honest states throughout: the message shows
   * "Reconnecting" while recovery is real, and the turn-lost copy only when
   * recovery genuinely failed.
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
                  finalizeStreamCleanup(threadId);
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
        handleSSEEvent(event);
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return 'abandon';
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
      void refreshThreadSyncBaseline(threadId);
    } catch (error) {
      console.error('Turn recovery reconciliation failed:', error);
      if (threadsStore.currentThreadId !== threadId) return;
      chatStore.setLastMessageError(TURN_LOST_MESSAGE);
      finalizeStreamCleanup(threadId);
    }
  }

  async function handleConfirmUnsupportedSend() {
    const send = pendingSend;
    if (!send) {
      closeAttachmentWarningModal();
      return;
    }

    if (warningSuppressChecked) {
      configStore.suppressAttachmentWarnings = true;
    }

    closeAttachmentWarningModal();
    await streamMessage(send.message, send.attachments, true);
  }

  // Slash commands whose execution_kind is `chat_stream` on the backend must
  // be routed through the /chat SSE endpoint, not /commands/execute (which
  // rejects them with "handled outside the command service"). The chat-stream
  // intercept in nymeria/api/routers/chat.py handles their state work + agent
  // kickoff in one round-trip. Keep this list in sync with the
  // `execution_kind="chat_stream"` registrations in command_service.py.
  // TODO: make this data-driven via api.listCommands() with execution_kind.
  const CHAT_STREAM_COMMAND_ROOTS = new Set(['/compact', '/orchestrate', '/goal', '/skill', '/kit', '/quick']);

  async function handleSendMessage(message: string, attachments?: FileAttachment[]) {
    if (!message.trim() && (!attachments || attachments.length === 0)) return;

    // Edit-and-resend (backlog #12): the composer was seeded from a prior
    // user message; sending commits the deferred rewind, then streams the
    // edited prompt as a fresh turn. Checked before the queue gate so an
    // edit-send never silently queues behind a turn that started mid-edit.
    if (chatStore.isEditing) {
      await performEditResend(message, attachments);
      return;
    }

    const trimmed = message.trim();
    const slashRoot = trimmed.startsWith('/') ? trimmed.split(/\s+/)[0].toLowerCase() : '';
    const isChatStreamCommand = CHAT_STREAM_COMMAND_ROOTS.has(slashRoot);

    // While streaming: queue the prompt sub-turn-style. Slash commands and
    // attachments cannot be queued (backend rejects), so fall back to today's
    // "do nothing" gate for those cases.
    if (chatStore.isStreaming) {
      if (attachments && attachments.length > 0) return;
      if (trimmed.startsWith('/') && !isChatStreamCommand) return;
      if (!threadsStore.currentThreadId) return;
      void queueOnBusyThread(trimmed);
      return;
    }

    if (trimmed.startsWith('/') && !isChatStreamCommand && (!attachments || attachments.length === 0)) {
      if (!threadsStore.currentThreadId) {
        threadsStore.createThread();
      }
      const threadId = threadsStore.currentThreadId || undefined;
      try {
        const result = await api.executeCommand(trimmed, threadId);
        chatStore.addCommandResult(trimmed, result.markdown, result.success);
      } catch (error) {
        chatStore.addCommandResult(
          trimmed,
          `**Error:** ${error instanceof Error ? error.message : 'The command did not complete.'}`,
          false
        );
      }
      return;
    }

    if (attachments && attachments.length > 0) {
      if (configStore.suppressAttachmentWarnings) {
        await streamMessage(message, attachments, true);
        return;
      }

      const threadId = threadsStore.currentThreadId || 'preview';
      try {
        const validation = await api.validateThreadAttachments(threadId, attachments);
        if (!validation.compatible) {
          attachmentValidationResult = validation;
          pendingSend = { message, attachments };
          warningSuppressChecked = false;
          showAttachmentWarningModal = true;
          return;
        }
      } catch (e) {
        console.warn('Attachment preflight validation failed; proceeding without preflight:', e);
      }
    }

    await streamMessage(message, attachments);
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
          'Slash commands cannot be sent as an edited prompt. Cancel the edit (Esc) to run a command.',
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

    // rewindToMessage truncated the transcript and cleared edit state;
    // stream the edited prompt as a normal fresh turn.
    await streamMessage(message, attachments);
  }

  /**
   * Send a follow-up prompt that the agent should pick up at its next sub-turn
   * halt. Adds the prompt to chatStore.pendingPrompts and POSTs in the
   * background using the queue lifecycle endpoint. Lifecycle events update the
   * prompt's status; the primary stream's `prompt_injected` handler converts
   * it into a real user message at injection time.
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
            // primary stream already converted this prompt to a user message
            chatStore.removePendingPrompt(promptId);
            return;
          case 'error': {
            const data = event.data as { message: string; code?: string };
            if (data.code === 'restored') {
              // A stop handed this prompt back (backlog #16); the stop path
              // (or the queue_restored sync event) restores it to the
              // composer, so drop the bar entry instead of showing an error.
              chatStore.removePendingPrompt(promptId);
              return;
            }
            chatStore.setPendingPromptStatus(promptId, 'error', data.message);
            return;
          }
          // queued / turn_halted / fanout_dropped / prompt_injected: ignore
        }
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return;
      const msg = err instanceof Error ? err.message : 'Could not queue your prompt. Try again in a moment.';
      chatStore.setPendingPromptStatus(promptId, 'error', msg);
    }
  }

  /**
   * Returns true if the thread ID belongs to a non-desktop platform
   * or a callable agent thread — these must never be replaced by
   * syncThreadIdFromEvent.
   */
  function isNonDesktopThread(id: string): boolean {
    return isNonDesktopThreadId(id);
  }

  /**
   * Sync thread ID from any SSE event.
   * This ensures the frontend knows the backend's thread ID even if the stream is stopped early.
   */
  function syncThreadIdFromEvent(event: SSEEvent) {
    const backendThreadId = event.threadId;
    if (!backendThreadId) return;

    const currentId = threadsStore.currentThreadId;

    // If we don't have a local thread, create one with backend's ID
    if (!currentId) {
      const firstUserMessage = chatStore.messages.find((m) => m.role === 'user');
      const title = firstUserMessage?.content || 'New Thread';
      threadsStore.setThreadFromApi(backendThreadId, title);
      threadsStore.autoTitleFromMessage(backendThreadId, title);
      return;
    }

    // If we have a local thread with different ID, update it to use backend's ID
    if (currentId !== backendThreadId) {
      // Guard: never replace a desktop thread with a trigger/system thread
      // and never replace a trigger thread with another trigger thread.
      // Only allow replacement for backend-generated 8-char fallback IDs.
      if (isNonDesktopThread(backendThreadId) || isNonDesktopThread(currentId)) {
        return;
      }

      const currentThread = threadsStore.currentThread;
      if (currentThread) {
        threadsStore.deleteThread(currentId);
        threadsStore.setThreadFromApi(backendThreadId, currentThread.title);
      }
    }
  }

  function handleSSEEvent(event: SSEEvent) {
    // Guard: skip events from a different thread (cross-thread SSE pollution).
    // Must run BEFORE syncThreadIdFromEvent to prevent destructive thread replacement.
    // When currentThreadId is null (initial sync case), allow through.
    if (event.threadId && threadsStore.currentThreadId && event.threadId !== threadsStore.currentThreadId) {
      return;
    }

    // Sync thread ID from any event (not just 'done')
    // This ensures context is preserved even if user stops generation
    syncThreadIdFromEvent(event);

    switch (event.type) {
      case 'thinking': {
        // Add thinking as a step (preserves order with tool calls)
        // Backend sends 'content', but we support 'message' for backwards compatibility
        const data = event.data as { content?: string; message?: string };
        chatStore.addThinkingStep(data.content || data.message || '');
        break;
      }

      case 'tool_call_delta': {
        chatStore.flushStreamingBuffers();
        chatStore.setAssistantActivityPhase('formulating');
        break;
      }

      case 'tool_call': {
        // Add tool call as a step (preserves order with thinking)
        const data = event.data as {
          id: string;
          name: string;
          arguments: Record<string, unknown>;
          timeoutSeconds?: number;
        };
        chatStore.addToolCallStep(data.id, data.name, data.arguments, data.timeoutSeconds);

        // Refresh scheduled todos when self_invoke is called (legacy)
        if (data.name === 'self_invoke') {
          todosStore.fetch();
        }
        break;
      }

      case 'tool_result': {
        // Update the tool call step with its result
        const data = event.data as {
          id?: string;
          name: string;
          result: string;
          status: 'success' | 'error';
          durationMs?: number;
        };
        if (data.id) {
          chatStore.updateToolCallStepResult(data.id, data.result, data.status, data.durationMs);
        } else {
          // Fallback: use legacy method if no ID provided
          chatStore.updateToolCallResultByName(data.name, data.result, data.status, data.id);
        }

        // Refresh TODOs when a TODO tool completes.
        if (isTodoTool(data.name)) {
          todosStore.onTodoToolCompleted();
          // Also refresh activity since todo changes are logged
          activityStore.fetch();
        }

        // Refresh scheduled todos when self_invoke completes (legacy)
        if (data.name === 'self_invoke') {
          todosStore.fetch();
          activityStore.fetch();
        }
        if (isSkillMutationToolName(data.name)) {
          refreshSkillStateAfterMutation(event.threadId);
        }
        break;
      }

      case 'workspace_artifact': {
        const data = event.data as {
          toolCallId?: string;
          artifact: import('$lib/types').WorkspaceArtifact;
        };
        if (data.toolCallId) {
          chatStore.addToolCallArtifacts(data.toolCallId, [data.artifact]);
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
          expiresAt: data.expiresAt,
          reason: data.reason,
          httpStatus: data.httpStatus,
          rewound: data.rewound,
          streamChunks: data.streamChunks,
        });
        break;
      }

      case 'hook_activity': {
        // Ephemeral lifecycle-hook line interleaved into the assistant step
        // stream (Claude Code style). Backend only emits meaningful runs.
        const data = event.data as {
          name?: string;
          event?: string;
          status?: string;
          detail?: string;
          toolName?: string | null;
        };
        chatStore.addHookActivityStep({
          hookName: data.name,
          hookEvent: data.event,
          hookStatus: data.status,
          hookDetail: data.detail,
          hookToolName: data.toolName ?? undefined,
        });
        break;
      }

      case 'response': {
        // Add response as a step (preserves order with thinking and tool calls)
        const data = event.data as { content: string; isComplete: boolean };
        chatStore.addResponseStep(data.content || '');
        break;
      }

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

      case 'error': {
        const data = event.data as {
          message: string;
          code?: string;
          details?: Record<string, unknown>;
        };
        if (data.code === 'cancelled') {
          // The backend confirmed the abort (backlog #11): finalize the
          // stopped rendering from the server's frame instead of the old
          // optimistic client-side edit. Also covers stops initiated from
          // another client or surface.
          chatStore.finalizeStopped();
          break;
        }
        const message = data.code
          ? `${data.message}\n\n(code: ${data.code})`
          : data.message;
        chatStore.setLastMessageError(message);
        break;
      }

      case 'done': {
        const data = event.data as {
          threadId: string;
          contextStats?: ContextStats;
          model?: string;
          title?: string;
          title_source?: string;
          dispatchedTo?: DispatchInfo;
        };

        // Reclassify any trailing thinking as response (if no tool calls followed it)
        chatStore.reclassifyThinkingAsResponse();

        // Update context stats and active model
        if (!data.dispatchedTo && data.contextStats) {
          chatStore.setContextStats(data.contextStats);
        }
        if (!data.dispatchedTo && data.model) {
          chatStore.setActiveModel(data.model);
        }

        // Apply backend-generated title (auto-title from first message)
        if (data.title && data.dispatchedTo?.threadId) {
          threadsStore.applyBackendTitle(data.dispatchedTo.threadId, data.title);
        } else if (data.title && data.threadId) {
          threadsStore.applyBackendTitle(data.threadId, data.title);
        }

        // Clear queued state
        chatStore.setQueued(false);
        // The turn completed normally; a stop that raced it has nothing
        // left to cancel, so drop the stopping state without the
        // cancelled-visuals finalization.
        if (chatStore.isStopping) chatStore.clearStopping();
        // Note: Thread ID syncing is handled by syncThreadIdFromEvent() called at top of handleSSEEvent

        // A completed turn may have changed this thread's tool bindings — e.g.
        // a Skill Kit activation binds its required_tools into temporary_tools.
        // In dynamic-binding mode no `tool_reload` SSE event fires for that, so
        // refresh the thread config here to keep the effective tool count (and
        // anything else derived from the config) current without a manual reload.
        const completedThreadId = data.dispatchedTo?.threadId || data.threadId;
        if (completedThreadId) {
          void threadConfigStore.loadConfig(completedThreadId).catch(() => {});
        }
        break;
      }

      case 'queued': {
        // Thread is busy with an autonomous task - show waiting indicator
        const data = event.data as { message: string; holder?: string; heldSeconds?: number };
        chatStore.setQueued(true);
        // Log context for debugging
        if (data.holder) {
          debugLog(`[MainPanel] Queued: held by ${data.holder} for ${data.heldSeconds ?? '?'}s`);
        }
        break;
      }

      case 'turn_halted': {
        const data = event.data as { reason: string; count: number };
        debugLog(`[MainPanel] turn_halted reason=${data.reason} count=${data.count}`);
        break;
      }

      case 'prompt_injected': {
        // Holder is draining queued prompts. Close the current assistant
        // bubble, materialize each queued prompt as a user message in FIFO
        // order, and open a new assistant placeholder for the sub-turn.
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
        // prompts are user-visible (matches history filtering).
        const consumed = chatStore.consumeQueuedPrompts(data.count);
        chatStore.flushStreamingBuffers();
        chatStore.setLastMessageComplete();
        chatStore.clearActiveToolCalls();
        const sources = data.sources ?? [];
        const wireTexts = data.prompts
          ?.filter((p, i) => (sources[i] ?? 'user') === 'user' && p.text.trim())
          .map((p) => p.text);
        const texts = wireTexts ?? consumed.map((p) => p.content);
        for (const text of texts) {
          chatStore.addUserMessage(text);
        }
        chatStore.addAssistantMessage();
        break;
      }

      case 'prompt_absorbed':
      case 'fanout_dropped':
        break;

      case 'compacting': {
        const data = event.data as { message: string };
        chatStore.setCompacting(true, data.message);
        break;
      }

      case 'compact_result':
        // No longer used - result shown via 'response' event in AI bubble
        break;

      case 'compacted': {
        // Conversation was compacted - clear UI and show notification
        const data = event.data as { messagesRemoved: number; autoResumed: boolean; summary?: string };
        chatStore.handleCompacted(data.messagesRemoved, data.summary, data.autoResumed);
        break;
      }

      case 'context_attached': {
        // Attach context summary to the last user message for collapsible display
        const data = event.data as { summary: string };
        if (data.summary) {
          chatStore.setLastUserMessageContextSummary(data.summary);
        }
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
        if (isSkillMutationReloadSource(data.source)) {
          refreshSkillStateAfterMutation(event.threadId);
        }
        break;
      }

      case 'iteration_limit': {
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
        break;
      }

      case 'turn_resumed': {
        // A /resume re-drive started (from this client or another one
        // watching the thread): flip the pause card to its resumed state.
        chatStore.markTurnPausedResumed();
        break;
      }
    }
  }
</script>

<div class="main-panel-content">
  {#if threadsStore.currentThread}
    <ThreadHeader
      thread={threadsStore.currentThread}
      threadConfig={currentThreadConfig}
      onOpenSettings={() => (showThreadSettings = true)}
    />
  {/if}

  {#if outlookStore.isOutlook}
    <QuickActions
      onAction={(msg) => handleSendMessage(msg)}
      onInsert={(text) => { pendingInsertText = text; }}
      disabled={chatStore.isStreaming}
    />
  {/if}

  <div class="chat-area">
    <ChatContainer />
  </div>

  <!-- Wrap context bar + input together so the elevated background slides
       in/out as a single unit when sidebars collapse, instead of leaving
       the context bar visually orphaned with its own elevated colour. -->
  <div class="input-section" class:both-open={bothSidebarsOpen}>
    <ContextStatusBar />
    <div class="input-area">
      <QueuedPromptsBar />
      <InputBar
        onSend={handleSendMessage}
        disabled={false}
        insertText={pendingInsertText}
        onInsertConsumed={() => { pendingInsertText = ''; }}
        placeholder={chatStore.isQueued
          ? 'Type to queue (sends when current turn finishes)'
          : chatStore.isStreaming
            ? 'Type to queue (sends at the next sub-turn halt)'
            : 'Type a message…'}
      />
    </div>
  </div>
</div>

{#if showThreadSettings && threadsStore.currentThread}
  <ThreadSettingsPanel
    thread={threadsStore.currentThread}
    threadConfig={currentThreadConfig}
    onClose={() => (showThreadSettings = false)}
    onSaved={handleConfigSaved}
  />
{/if}

<Modal
  title="Attachment Compatibility Warning"
  isOpen={showAttachmentWarningModal}
  onClose={closeAttachmentWarningModal}
>
  <div class="attachment-warning-modal">
    <p>
      The current model <code>{attachmentValidationResult?.effective_model || 'unknown'}</code>
      is likely incompatible with one or more attached files.
    </p>

    {#if attachmentValidationResult?.unsupported_modalities.length}
      <p>
        Unsupported modalities:
        <strong>{attachmentValidationResult.unsupported_modalities.join(', ')}</strong>
      </p>
    {/if}

    {#if attachmentValidationResult?.warnings.length}
      <ul class="warning-list">
        {#each attachmentValidationResult.warnings as warning}
          <li>{warning}</li>
        {/each}
      </ul>
    {/if}

    <p class="warning-note">
      Sending anyway will likely return an API error.
    </p>

    <label class="suppress-warning">
      <input type="checkbox" bind:checked={warningSuppressChecked} />
      <span>Don't show this warning again</span>
    </label>

    <div class="warning-actions">
      <Button variant="ghost" onclick={closeAttachmentWarningModal}>
        Cancel
      </Button>
      <Button variant="danger" onclick={handleConfirmUnsupportedSend}>
        Send anyway
      </Button>
    </div>
  </div>
</Modal>

<style>
  .main-panel-content {
    position: relative;
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-base);
  }

  .chat-area {
    flex: 1;
    overflow: hidden;
    min-height: 0;
  }

  /* Scroll padding lives INSIDE ChatContainer's .chat-container so the chat
     scroll content can extend behind the absolutely-positioned input-section.
     Reserving the room here (instead of as padding on .chat-area) keeps
     chat-area's box flush with the bottom of main-panel-content — when the
     input-section's ::before sheet slides away on sidebar collapse, the
     newly transparent section reveals chat scroll content underneath
     instead of an empty bg-base slab. */
  .chat-area :global(.chat-container) {
    /* Must clear the absolutely-positioned input-section's full natural
       height: 20px bar + 14px top padding + ~50px pill + 14px bottom
       padding + ~24px hint + ~10px safety = ~140px. Lower values let the
       tail of the chat scroll under the bar/pill area, hiding messages. */
    padding-bottom: 140px;
  }

  /* Wrapper for ContextStatusBar + InputBar. Owns the sliding elevated
     background so both halves move together when a sidebar collapses.
     The section itself stays TRANSPARENT — the prompt-window surface is
     painted by ::before (starts at top: 20px), and the bar surface is
     painted by ContextStatusBar's .bar-content. When the bar is collapsed,
     the 20px above the prompt window must read-through to the chat area
     behind it, so this wrapper must NOT set its own background. */
  .input-section {
    /* Absolutely positioned over the bottom of the chat area so that when
       ::before slides away on sidebar collapse, the now-transparent section
       reveals chat content underneath — instead of main-panel-content's
       bg-base, which would read as a dark slab. */
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    background: transparent;
  }

  /* Two sliding sheets, both sized to the full section (inset: 0) so a
     single translateY(100%) actually clears the section. Each pseudo
     paints only the portion of bg it owns via a clipped linear-gradient;
     the rest of the pseudo is transparent.

     ::before = BAR  (paints top 20px + has its own border-top divider at
                      y=0). Earlier in source order so it renders UNDER
                      ::after — meaning the bar slides BEHIND the prompt
                      window, hidden by ::after's opaque bg during the
                      slide instead of passing in front of it. Slides on
                      local chevron collapse (via :has()) AND on sidebar
                      collapse.
     ::after  = PROMPT WINDOW (paints from y=20 down + has a 1px under-bar
                      divider line at y=20-21). Later in source order so
                      it renders ON TOP, providing the opaque mask that
                      hides ::before's slide. Slides on sidebar collapse
                      only.

     Because both pseudos are the same height and translate by the same
     distance (= section_height) on sidebar collapse, they move together
     at a single velocity — unified slide, not a desync.

     Each bg stacks a glass-bg-strong tint over a solid bg-base so the
     result is fully opaque (no chat bleed-through when both sidebars
     are open). */
  /* ::before = BAR (renders under ::after) */
  .input-section::before {
    content: '';
    position: absolute;
    inset: 0;
    background:
      linear-gradient(to bottom, var(--glass-bg-strong) 0 20px, transparent 20px),
      linear-gradient(to bottom, var(--bg-base) 0 20px, transparent 20px);
    /* Bar's top divider painted via inset box-shadow at the top edge. */
    box-shadow: inset 0 1px 0 var(--border-subtle);
    transform: translateY(100%);
    transition: transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
    pointer-events: none;
    z-index: 0;
  }

  /* ::after = PROMPT WINDOW (renders on top, masking the bar's slide) */
  .input-section::after {
    content: '';
    position: absolute;
    inset: 0;
    background:
      linear-gradient(
        to bottom,
        transparent 0 20px,
        var(--border-subtle) 20px 21px,
        var(--glass-bg-strong) 21px
      ),
      linear-gradient(to bottom, transparent 0 20px, var(--bg-base) 20px);
    transform: translateY(100%);
    transition: transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
    pointer-events: none;
    z-index: 0;
  }

  .input-section.both-open::after {
    transform: translateY(0);
  }

  /* ::before (bar bg) at rest only when sidebars are both open AND the bar
     isn't locally collapsed. Chevron click toggles
     .context-status-bar.collapsed which trips the :has() check, dropping
     ::before back to translateY(100%) so the bar's bg + top divider slide
     down behind ::after (the prompt window) alongside the text. */
  .input-section.both-open:not(:has(.context-status-bar.collapsed))::before {
    transform: translateY(0);
  }

  .input-section > :global(*) {
    position: relative;
    z-index: 1;
  }

  .input-area {
    /* Source of truth for the three vertical gaps that flank the prompt
       input bar:
         1. above the input-container  (padding-top of .input-area)
         2. between input-container and the "Press Ctrl+Enter…" hint
            (the hint's margin-top, overridden via :global below)
         3. below the hint  (padding-bottom of .input-area)
       Base gap is --prompt-stack-gap (8px). Gaps 1 and 2 carry a +2px lift so
       the space ABOVE the pill equals the space ABOVE the hint (both 10px),
       which is what the eye reads as the prompt's top and middle gaps matching.
       Gap 3 stays at the base 8px so the hint's bottom keeps landing 8px above
       the section bottom, lining the hint up with the RightPanel
       ConnectionStatus bar. The +2px is the same lift applied to the hint's
       margin-top below (it raises the pill flush with the "API Connected"
       border); adding it to padding-top too only grows the space above the
       pill, it does not move the pill, because the section is bottom-anchored. */
    --prompt-stack-gap: 8px;
    padding: calc(var(--prompt-stack-gap) + 2px) var(--spacing-md) var(--prompt-stack-gap);
    /* The InputBar component handles its own internal padding. The divider
       between context bar and input is drawn by the sliding pseudo in
       .input-section::before so it moves with the bg. */
  }

  /* Override InputBar's default 18px hint margin-top. Normally this equals the
     shared --prompt-stack-gap; the +2px lifts the prompt pill's bottom edge up
     to sit flush with the right panel's ConnectionStatus top border (the line
     above "API Connected"). The pill is otherwise ~2px below that line because
     the connection strip's text row is ~2px taller than this hint row. Because
     the hint stays bottom-anchored, growing this gap raises only the pill — the
     hint itself stays where it lines up with the "API Connected" text. */
  .input-area :global(.hint) {
    margin-top: calc(var(--prompt-stack-gap) + 2px);
  }

  /* When a sidebar collapses, the "Press Ctrl+Enter…" hint that lives at the
     bottom of the InputBar slides down + collapses out of view. Two effects
     combine: (1) transform translates it down so it visually slides off the
     elevated surface, (2) max-height + margin-top + opacity go to 0 so the
     space it occupied also shrinks — which pulls the prompt input bar lower
     on the screen (the .input-section is bottom-anchored in MainPanel's
     flex column, so a shorter section means the top of the bar drops). */
  .input-section :global(.hint) {
    max-height: 48px;
    overflow: hidden;
    transition:
      max-height var(--sidebar-collapse-duration) var(--sidebar-collapse-easing),
      margin-top var(--sidebar-collapse-duration) var(--sidebar-collapse-easing),
      opacity var(--sidebar-collapse-duration) var(--sidebar-collapse-easing),
      transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
  }
  .input-section:not(.both-open) :global(.hint) {
    max-height: 0;
    margin-top: 0;
    opacity: 0;
    transform: translateY(20px);
    pointer-events: none;
  }

  /* Sidebar-closed bar drop-down: when a sidebar is closed, the bg sheets
     have slid away, leaving the bar's text + dot orphaned at the top of an
     otherwise transparent section. Translate the whole .context-status-bar
     down so its contents sit just above the prompt input field instead of
     floating up where the bar used to live. The whole bar element moves as
     one unit, so the bar's own overflow:hidden clipping moves with it — no
     content gets cut off, and the text+dot stay in their normal relative
     positions inside the (now relocated) bar. */
  .input-section :global(.context-status-bar) {
    transition: transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
    /* Promote above the input-area (both default to z-index:1 from the
       global `.input-section > *` rule, and source order would put
       input-area on top — swallowing dot clicks once the sidebar-closed
       translateY(16px) below pushes the bar's footprint over the prompt
       input). Bumping to 2 keeps the dot reachable in every state. */
    z-index: 2;
  }
  .input-section:not(.both-open) :global(.context-status-bar) {
    transform: translateY(16px);
  }

  /* Sidebar collapse: only the ::before / ::after sheets slide. The bar
     text and chevron stay put and fully functional — the chevron can
     still toggle the bar text via the bar's own .collapsed state even
     when both sidebars are closed.

     SIDEBAR-CLOSED CHEVRON ANIMATION:
     When sidebars are CLOSED and the chevron is clicked to uncollapse
     the bar, the text slides in left-to-right via a clip-path wipe
     (collapse goes the other way, clipping right-to-left). This swaps
     out the bar's normal translateY+opacity slide ONLY in the closed
     state — when sidebars are open, the bar text still uses its
     internal vertical slide. */
  /* Default visible clip-path lives on a rule that's ALWAYS active (no
     :not() gate) — otherwise the transition has nothing to interpolate
     from (clip-path defaults to `none`, which can't animate to `inset()`).
     The transition declaration lives here too so it remains in effect
     regardless of sidebar state. */
  .input-section :global(.context-status-bar .bar-content) {
    visibility: visible;
    clip-path: inset(0 0 0 0);
    /* Must list transform + opacity here too, not just clip-path —
       this :global rule's selector is more specific than the bar's
       internal .bar-content rule, so it replaces (not augments) the
       transition shorthand. Dropping transform/opacity would kill the
       sidebar-open chevron's vertical slide.
       Visibility flips to visible INSTANTLY on this rule (0s/0s delay) so
       chevron-uncollapse reveals the text right at the start of the
       slide-up / clip-path wipe — no fade-in delay needed. */
    transition:
      visibility 0s linear 0s,
      clip-path 360ms cubic-bezier(0.22, 1, 0.36, 1),
      transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing),
      opacity var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
  }
  /* Whenever the bar is collapsed — regardless of sidebar state — the text
     must read as invisible. Visibility flips to hidden AFTER the slide-down
     completes (delay = sidebar-collapse-duration) so the chevron-collapse
     animation still plays out, but once hidden, sidebar transitions can't
     re-show it. This is what kills the "text flashes during sidebar
     collapse" bug: opacity/transform/clip-path are free to animate to their
     new staged values behind a visibility:hidden mask, so the user never
     sees the in-between frames. */
  .input-section :global(.context-status-bar.collapsed .bar-content) {
    visibility: hidden;
    transition:
      visibility 0s linear var(--sidebar-collapse-duration),
      clip-path 360ms cubic-bezier(0.22, 1, 0.36, 1),
      transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing),
      opacity var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
  }
  .input-section:not(.both-open) :global(.context-status-bar.collapsed .bar-content) {
    /* Cancel the bar's internal translateY/opacity so only the clip-path
       wipe is visible when collapsing with sidebars closed. */
    transform: translateY(0);
    opacity: 1;
    clip-path: inset(0 100% 0 0);
  }

  /* When sidebars are collapsed, the bar sits without its elevated bg
     behind it — the text reads slightly low against the transparent chat
     area. Nudge the inner .details container up 2px so it optically
     centers on the bar's vertical mid-line in this state. */
  .input-section:not(.both-open) :global(.context-status-bar .details) {
    transform: translateY(-2px);
    transition: transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
  }
  .input-section :global(.context-status-bar .details) {
    transition: transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
  }


  .attachment-warning-modal {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .attachment-warning-modal p {
    margin: 0;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .warning-list {
    margin: 0;
    padding-left: var(--spacing-lg);
    color: var(--warning);
  }

  .warning-note {
    color: var(--error);
    font-weight: 500;
  }

  .suppress-warning {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .warning-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
  }
</style>
