<script lang="ts">
  import { untrack } from 'svelte';
  import { Button, Icon } from '$lib/components/common';
  import { api } from '$lib/services/api.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { commandsStore } from '$lib/stores/commands.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { filterCommands } from '$lib/utils/commandSearch';
  import type { AttachmentLimits, FileAttachment, SlashCommandInfo } from '$lib/types';
  import FilePreview from './FilePreview.svelte';
  import ImageModal from './ImageModal.svelte';
  import InputHintTips from './InputHintTips.svelte';
  import {
    processFile,
    getFilesFromClipboard,
    getFilesFromDrop,
    getSupportedFileExtensions,
    getMaxImagesErrorMessage,
    DEFAULT_ATTACHMENT_LIMITS,
    type FileProcessingError
  } from '$lib/utils/fileProcessing';

  interface Props {
    onSend: (message: string, attachments?: FileAttachment[]) => void;
    disabled?: boolean;
    placeholder?: string;
    filesEnabled?: boolean;
    insertText?: string;
    onInsertConsumed?: () => void;
  }

  let {
    onSend,
    disabled = false,
    placeholder = 'Type a message…',
    filesEnabled = true,
    insertText = '',
    onInsertConsumed,
  }: Props = $props();

  let inputValue = $state('');
  let textareaRef = $state<HTMLTextAreaElement | null>(null);
  let fileInputRef = $state<HTMLInputElement | null>(null);
  let pendingFiles = $state<FileAttachment[]>([]);
  let isDragOver = $state(false);
  let modalFile = $state<FileAttachment | null>(null);
  let errorMessage = $state<string | null>(null);
  // Escape closes the palette until the "/" name-entry context is left and
  // re-entered. (Resetting the shared catalog instead would re-arm the fetch
  // effect, so the palette reopened on the next tick.)
  let paletteDismissed = $state(false);
  let highlightedCommandIndex = $state(0);
  let paletteRef = $state<HTMLDivElement | null>(null);

  // When insertText changes, append it to the input and notify parent
  $effect(() => {
    if (insertText) {
      inputValue = inputValue + insertText;
      onInsertConsumed?.();
      // Trigger auto-resize after insertion
      if (textareaRef) {
        requestAnimationFrame(() => {
          if (textareaRef) {
            textareaRef.style.height = 'auto';
            textareaRef.style.height = Math.min(textareaRef.scrollHeight, 200) + 'px';
            textareaRef.focus();
          }
        });
      }
    }
  });

  // Edit-previous-prompt seeding (backlog #12): entering edit REPLACES the
  // composer content with the edited prompt and its image attachments,
  // unlike the append-only insertText channel above; leaving edit clears
  // whatever the edit flow left behind. Keyed on the editing id so ordinary
  // typing does not retrigger the seed.
  let _lastEditingId: string | null = null;
  $effect(() => {
    const editingId = chatStore.editingMessageId;
    if (editingId === _lastEditingId) return;
    _lastEditingId = editingId;
    if (editingId !== null) {
      inputValue = chatStore.editingDraft;
      pendingFiles = [...chatStore.editingImageAttachments];
      if (textareaRef) {
        requestAnimationFrame(() => {
          if (textareaRef) {
            textareaRef.style.height = 'auto';
            textareaRef.style.height = Math.min(textareaRef.scrollHeight, 200) + 'px';
            textareaRef.focus();
          }
        });
      }
    } else {
      inputValue = '';
      pendingFiles = [];
      if (textareaRef) {
        textareaRef.style.height = 'auto';
      }
    }
  });

  // Composer restore (backlog #16): a stopped turn hands queued prompts
  // back; append them below whatever is already drafted, separated by ---.
  // The draft read is untracked so the effect only re-runs on the channel,
  // not on every keystroke.
  $effect(() => {
    const restored = chatStore.composerRestore;
    if (!restored) return;
    chatStore.consumeComposerRestore();
    untrack(() => {
      inputValue = inputValue.trim() ? `${inputValue}\n---\n${restored}` : restored;
    });
    if (textareaRef) {
      requestAnimationFrame(() => {
        if (textareaRef) {
          textareaRef.style.height = 'auto';
          textareaRef.style.height = Math.min(textareaRef.scrollHeight, 200) + 'px';
          textareaRef.focus();
        }
      });
    }
  });

  let isStreaming = $derived(chatStore.isStreaming);
  let isStopping = $derived(chatStore.isStopping);
  let pendingImageCount = $derived(pendingFiles.filter((f) => f.type === 'image').length);
  // After Phase B, non-image attachments are sandboxed at ingress and ride
  // inside the message text (which queues fine). Only image attachments still
  // can't be queued (PendingPrompt schema doesn't carry image_url blocks), so
  // the streaming guard only blocks pending images, not pending documents.
  let canSendWhileStreaming = $derived(inputValue.trim().length > 0 && pendingImageCount === 0);
  let canSend = $derived(
    isStreaming
      ? !disabled && canSendWhileStreaming
      : (inputValue.trim().length > 0 || pendingFiles.length > 0) && !disabled
  );

  // Per-model attachment caps fetched once per thread. Default to the
  // conservative DEFAULT_ATTACHMENT_LIMITS until the backend reply lands so
  // the user can still drop files on a fresh thread.
  let attachmentLimits = $state<AttachmentLimits>(DEFAULT_ATTACHMENT_LIMITS);
  let maxImages = $derived(attachmentLimits.max_images_per_request ?? Infinity);
  let remainingImageSlots = $derived(Math.max(0, maxImages - pendingImageCount));

  $effect(() => {
    const threadId = threadsStore.currentThreadId;
    if (!threadId) {
      attachmentLimits = DEFAULT_ATTACHMENT_LIMITS;
      return;
    }
    let cancelled = false;
    api.getAttachmentLimits(threadId).then(
      (response) => {
        if (!cancelled) {
          attachmentLimits = response.limits;
        }
      },
      (error) => {
        console.warn('[InputBar] Failed to fetch attachment limits:', error);
        if (!cancelled) {
          attachmentLimits = DEFAULT_ATTACHMENT_LIMITS;
        }
      }
    );
    return () => {
      cancelled = true;
    };
  });
  let isCommandNameEntry = $derived(inputValue.startsWith('/') && !/\s/.test(inputValue.slice(1)));
  let slashQuery = $derived(
    isCommandNameEntry ? inputValue.slice(1).toLowerCase() : ''
  );
  // Tiered ranking lives in the shared commandSearch util (prefix-on-name >
  // substring-on-name > description-only); the catalog comes from the shared
  // commands store, which also feeds the send-path routing in MainPanel.
  let filteredCommands = $derived(
    isCommandNameEntry ? filterCommands(commandsStore.commands, slashQuery) : []
  );
  let showCommandPalette = $derived(
    isCommandNameEntry &&
    !paletteDismissed &&
    filteredCommands.length > 0 &&
    !disabled &&
    !isStreaming
  );

  $effect(() => {
    if (isCommandNameEntry) {
      void commandsStore.ensureLoaded();
    }
  });

  $effect(() => {
    if (!isCommandNameEntry) {
      paletteDismissed = false;
    }
  });

  $effect(() => {
    if (highlightedCommandIndex >= filteredCommands.length) {
      highlightedCommandIndex = 0;
    }
  });

  $effect(() => {
    void highlightedCommandIndex;
    void filteredCommands;
    if (!paletteRef) return;
    const active = paletteRef.children[highlightedCommandIndex] as HTMLElement | undefined;
    active?.scrollIntoView({ block: 'nearest' });
  });

  function insertCommand(command: SlashCommandInfo) {
    inputValue = `/${command.name} `;
    highlightedCommandIndex = 0;
    requestAnimationFrame(() => {
      if (!textareaRef) return;
      textareaRef.style.height = 'auto';
      textareaRef.style.height = Math.min(textareaRef.scrollHeight, 200) + 'px';
      textareaRef.focus();
    });
  }

  function handleSubmit() {
    if (!canSend) return;
    const wasEditing = chatStore.isEditing;
    onSend(inputValue.trim(), pendingFiles.length > 0 ? pendingFiles : undefined);
    if (wasEditing) {
      // Keep the draft: a successful edit-send exits edit mode, which clears
      // the composer via the $effect above; a refused one (rewind failed,
      // thread became busy) keeps edit mode so nothing typed is lost.
      return;
    }
    inputValue = '';
    pendingFiles = [];
    if (textareaRef) {
      textareaRef.style.height = 'auto';
    }
  }

  function handleSendClick() {
    handleSubmit();
  }

  function handleStopClick() {
    chatStore.stopGenerating(threadsStore.currentThreadId ?? undefined);
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (showCommandPalette) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        highlightedCommandIndex = (highlightedCommandIndex + 1) % filteredCommands.length;
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        highlightedCommandIndex =
          (highlightedCommandIndex - 1 + filteredCommands.length) % filteredCommands.length;
        return;
      }
      if (event.key === 'Tab' || event.key === 'Enter') {
        event.preventDefault();
        insertCommand(filteredCommands[highlightedCommandIndex]);
        return;
      }
      if (event.key === 'Escape') {
        paletteDismissed = true;
        return;
      }
    }

    // Escape cancels an in-progress prompt edit (command palette handled it
    // above when open, so this never fights the palette dismiss).
    if (event.key === 'Escape' && chatStore.isEditing) {
      event.preventDefault();
      chatStore.cancelEdit();
      return;
    }

    // Cmd/Ctrl + Enter to send (queues if a turn is already streaming)
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      handleSubmit();
      return;
    }

    // Plain Tab is left to its default: move focus out of the composer (e.g.
    // to the Send button) so the message bar is keyboard-navigable. It used
    // to insert a literal tab character, which silently trapped focus here.
  }

  function handleInput() {
    if (textareaRef) {
      // Auto-resize textarea
      textareaRef.style.height = 'auto';
      textareaRef.style.height = Math.min(textareaRef.scrollHeight, 200) + 'px';
    }
  }

  // Clear error message after a delay
  function showError(message: string) {
    errorMessage = message;
    setTimeout(() => {
      errorMessage = null;
    }, 4000);
  }

  // Add files with validation. Image count is capped per-model; documents are
  // uncapped (they go to the sandbox, not the model's context). The shared
  // MAX_SIZE ceiling in fileProcessing still applies per file.
  async function addFiles(files: File[]) {
    let imagesRemaining = remainingImageSlots;
    let droppedImages = 0;
    for (const file of files) {
      try {
        const attachment = await processFile(file);
        if (attachment.type === 'image') {
          if (imagesRemaining <= 0) {
            droppedImages += 1;
            continue;
          }
          imagesRemaining -= 1;
        }
        pendingFiles = [...pendingFiles, attachment];
      } catch (error) {
        const processingError = error as FileProcessingError;
        showError(processingError.message);
      }
    }
    if (droppedImages > 0) {
      showError(getMaxImagesErrorMessage(maxImages));
    }
  }

  // Remove a file from pending
  function removeFile(id: string) {
    pendingFiles = pendingFiles.filter((f) => f.id !== id);
  }

  // Open file in modal (only for images)
  function openFileModal(file: FileAttachment) {
    if (file.type === 'image') {
      modalFile = file;
    }
  }

  // Close modal
  function closeModal() {
    modalFile = null;
  }

  // Handle paste event
  async function handlePaste(event: ClipboardEvent) {
    if (!filesEnabled || !event.clipboardData) return;

    const pastedFiles = getFilesFromClipboard(event.clipboardData);
    if (pastedFiles.length > 0) {
      event.preventDefault();
      await addFiles(pastedFiles);
    }
  }

  // Handle drag enter - required by some browsers
  function handleDragEnter(event: DragEvent) {
    if (!filesEnabled) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy';
    }
    isDragOver = true;
  }

  // Handle drag over
  function handleDragOver(event: DragEvent) {
    if (!filesEnabled) return;
    event.preventDefault();
    event.stopPropagation();
    // Set dropEffect to show the correct cursor
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy';
    }
    isDragOver = true;
  }

  // Handle drag leave
  function handleDragLeave(event: DragEvent) {
    // Only set isDragOver to false if we're leaving the drop zone entirely
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    const x = event.clientX;
    const y = event.clientY;

    if (x < rect.left || x >= rect.right || y < rect.top || y >= rect.bottom) {
      isDragOver = false;
    }
  }

  // Handle drop
  async function handleDrop(event: DragEvent) {
    event.preventDefault();
    event.stopPropagation();
    isDragOver = false;

    if (!filesEnabled || !event.dataTransfer) return;

    const droppedFiles = getFilesFromDrop(event.dataTransfer);
    if (droppedFiles.length > 0) {
      await addFiles(droppedFiles);
    }
  }

  // Handle file input change
  async function handleFileSelect(event: Event) {
    const input = event.target as HTMLInputElement;
    if (!input.files) return;

    const files = Array.from(input.files);
    await addFiles(files);

    // Reset input so same file can be selected again
    input.value = '';
  }

  // Open file picker
  function openFilePicker() {
    fileInputRef?.click();
  }
</script>

<div
  class="input-container"
  class:drag-over={isDragOver}
  role="region"
  aria-label="Message composer"
  ondragenter={handleDragEnter}
  ondragover={handleDragOver}
  ondragleave={handleDragLeave}
  ondrop={handleDrop}
>
  {#if chatStore.isEditing}
    <div class="editing-banner">
      <Icon name="edit" size={14} />
      <span>Editing your message. Sending rewinds the conversation to this point.</span>
      <button
        type="button"
        class="editing-cancel"
        onclick={() => chatStore.cancelEdit()}
        aria-label="Cancel editing"
        data-tooltip="Cancel editing (Esc)"
      >
        <Icon name="x" size={14} />
      </button>
    </div>
  {/if}

  {#if errorMessage}
    <div class="error-banner">
      <Icon name="error" size={14} />
      <span>{errorMessage}</span>
    </div>
  {/if}

  <FilePreview
    files={pendingFiles}
    onRemove={removeFile}
    onFileClick={openFileModal}
  />

  {#if showCommandPalette}
    <div class="command-palette" bind:this={paletteRef}>
      {#each filteredCommands as command, index (command.name)}
        <button
          type="button"
          class="command-option"
          class:active={index === highlightedCommandIndex}
          onmousedown={(event) => event.preventDefault()}
          onclick={() => insertCommand(command)}
        >
          <span class="command-name">/{command.name}</span>
          <span class="command-description">{command.description}</span>
          <code class="command-usage">{command.usage}</code>
        </button>
      {/each}
    </div>
  {/if}

  <div class="input-bar">
    {#if filesEnabled}
      <input
        bind:this={fileInputRef}
        type="file"
        accept={getSupportedFileExtensions()}
        multiple
        class="file-input-hidden"
        onchange={handleFileSelect}
      />
      <button
        type="button"
        class="plus-btn"
        onclick={openFilePicker}
        disabled={disabled || isStreaming}
        data-tooltip="Attach files"
        aria-label="Attach files"
      >
        <Icon name="plus" size={18} />
      </button>
    {/if}

    <textarea
      bind:this={textareaRef}
      bind:value={inputValue}
      onkeydown={handleKeyDown}
      oninput={handleInput}
      onpaste={handlePaste}
      {placeholder}
      {disabled}
      rows="1"
      class="message-input"
    ></textarea>

    {#if isStreaming}
      <button
        type="button"
        class="plus-btn danger"
        class:stopping={isStopping}
        onclick={handleStopClick}
        disabled={isStopping}
        data-tooltip={isStopping ? 'Stopping…' : 'Stop the current turn'}
        aria-label={isStopping ? 'Stopping' : 'Stop'}
      >
        <Icon name="stop" size={14} />
      </button>
    {/if}
    <button
      type="button"
      class="send-btn"
      onclick={handleSendClick}
      disabled={!canSend}
      data-tooltip={isStreaming ? 'Queue this message until the agent halts' : 'Send'}
      aria-label="Send"
    >
      <Icon name="arrowUp" size={14} />
    </button>
  </div>

  {#if isDragOver}
    <div class="drop-overlay">
      <Icon name="paperclip" size={48} />
      <span>Drop files here</span>
    </div>
  {/if}
</div>

<div class="hint">
  <InputHintTips paused={inputValue.trim().length > 0} />
  <p class="hint-keys">
    Press <kbd>Ctrl</kbd> + <kbd>Enter</kbd> to send
    {#if filesEnabled}
      &middot; Paste or drag files to attach
    {/if}
  </p>
</div>

<ImageModal image={modalFile} onClose={closeModal} />

<style>
  .input-container {
    position: relative;
    background: var(--bg-elevated-2);
    border-radius: 18px;
    border: 1px solid var(--border-subtle);
    transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
  }

  .input-container:focus-within {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px var(--accent-tint-bg);
  }

  .input-container.drag-over {
    border-color: var(--accent-primary);
    border-style: dashed;
  }

  .input-bar {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 10px 10px;
  }

  .plus-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    background: transparent;
    border: none;
    border-radius: 50%;
    color: var(--text-muted);
    cursor: pointer;
    flex-shrink: 0;
    transition: color var(--transition-fast), background var(--transition-fast);
  }

  .plus-btn:hover:not(:disabled) {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .plus-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .plus-btn.danger {
    color: var(--error);
  }

  /* Stop-in-flight state (backlog #11): pulse until the backend confirms
     the abort; the global reduced-motion floor disables the animation. */
  .plus-btn.danger.stopping:disabled {
    opacity: 1;
    cursor: progress;
    animation: pulse 1.2s var(--ease-out) infinite;
  }

  .plus-btn.danger:hover:not(:disabled) {
    background: color-mix(in srgb, var(--error) 15%, transparent);
  }

  .send-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    background: var(--accent-primary);
    color: var(--text-on-accent);
    border: 1px solid transparent;
    border-radius: 50%;
    cursor: pointer;
    flex-shrink: 0;
    transition: background var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast), box-shadow var(--transition-fast), transform var(--transition-fast);
  }

  .send-btn:hover:not(:disabled) {
    background: var(--accent-hover);
    box-shadow: 0 0 12px var(--accent-tint-border);
  }

  .send-btn:active:not(:disabled) {
    transform: scale(var(--press-scale));
  }

  .send-btn:disabled {
    background: var(--bg-active);
    color: var(--text-secondary);
    border-color: var(--border-subtle);
    cursor: not-allowed;
    opacity: 1;
  }

  .command-palette {
    position: absolute;
    left: var(--spacing-sm);
    right: var(--spacing-sm);
    bottom: calc(100% + var(--spacing-xs));
    z-index: 20;
    max-height: 280px;
    overflow-y: auto;
    padding: var(--spacing-xs);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    /* §7 — floating slash-command popover: shadow alone defines
       elevation; border would be redundant chrome. */
    box-shadow: var(--shadow-lg);
  }

  .command-option {
    display: grid;
    grid-template-columns: minmax(96px, auto) 1fr auto;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    min-height: 40px;
    padding: var(--spacing-xs) var(--spacing-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-primary);
    text-align: left;
    cursor: pointer;
  }

  .command-option:hover,
  .command-option.active {
    background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
  }

  .command-name {
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
    color: var(--accent-primary);
  }

  .command-description {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .command-usage {
    max-width: 240px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .message-input {
    flex: 1;
    min-width: 0;
    /* One full line needs 29px under border-box: 21px line box (14px font
       x 1.5 line-height) + 4px top and bottom padding. The old 22px floor
       let the empty textarea render 7px short, shaving the placeholder's
       descenders (the "y" in "Type a message…") flat at the clip edge; the
       auto-resize JS hid the bug the moment a first character set an
       explicit ~29px height. */
    min-height: 29px;
    max-height: 160px;
    padding: 4px 6px;
    background: transparent;
    border: none;
    color: var(--text-primary);
    font-family: inherit;
    /* Match the message reading size (--font-size-sm, 14px in .bubble-content)
       so what you type and the placeholder ghost read at the same size as the
       conversation above. The placeholder inherits this size (its rule only
       sets color). */
    font-size: var(--font-size-sm);
    line-height: 1.5;
    resize: none;
    overflow-y: auto;
  }

  .message-input:focus {
    outline: none;
    box-shadow: none;
  }

  .message-input::placeholder {
    color: var(--text-muted);
  }

  .message-input:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .file-input-hidden {
    position: absolute;
    width: 0;
    height: 0;
    opacity: 0;
    pointer-events: none;
  }

  .drop-overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(var(--accent-primary-rgb), 0.1);
    border-radius: var(--radius-lg);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm);
    color: var(--accent-primary);
    font-weight: 500;
    pointer-events: none;
  }

  .error-banner {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    background: rgba(var(--error-rgb), 0.1);
    color: var(--error);
    font-size: var(--font-size-sm);
    border-radius: var(--radius-md) var(--radius-md) 0 0;
    animation: slideDown var(--transition-fast);
  }

  .editing-banner {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    border-radius: var(--radius-md) var(--radius-md) 0 0;
    animation: slideDown var(--transition-fast);
  }

  .editing-banner span {
    flex: 1;
  }

  .editing-cancel {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border: none;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .editing-cancel:hover {
    color: var(--text-primary);
    background: color-mix(in srgb, var(--text-muted) 12%, transparent);
  }

  .editing-cancel:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
  }

  @keyframes slideDown {
    from {
      opacity: 0;
      transform: translateY(-10px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }

  /* The hint row keeps the same class/contract MainPanel relies on: it owns
     the gap below the input bar (margin-top, overridden to --prompt-stack-gap)
     and the sidebar-collapse animation, both keyed on :global(.hint). It now
     lays out the rotating tip (left) and the key hint (right) on one line. */
  .hint {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    margin: 18px 0 0 0;
  }

  .hint-keys {
    flex: 0 0 auto;
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    text-align: right;
    white-space: nowrap;
  }

  kbd {
    display: inline-block;
    padding: 2px 6px;
    font-family: var(--font-mono);
    font-size: 0.75em;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    /* Nudge the key chips (and their labels) up 1px so they sit optically
       centred against the surrounding "Press … to send" text. */
    transform: translateY(-1px);
  }
</style>
