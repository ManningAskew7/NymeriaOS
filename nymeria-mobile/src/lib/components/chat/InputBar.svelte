<script lang="ts">
  import { Button, Icon } from '$lib/components/common';
  import { api } from '$lib/services/api.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import type { AttachmentLimits, FileAttachment, SlashCommandInfo } from '$lib/types';
  import FilePreview from './FilePreview.svelte';
  import ImageModal from './ImageModal.svelte';
  import {
    processFile,
    getFilesFromClipboard,
    getMaxImagesErrorMessage,
    getSupportedFileExtensions,
    DEFAULT_ATTACHMENT_LIMITS,
    type FileProcessingError,
  } from '$lib/utils/fileProcessing';
  import { hapticImpact } from '$lib/utils/haptics';

  interface Props {
    onSend: (message: string, attachments?: FileAttachment[]) => void;
    disabled?: boolean;
    placeholder?: string;
    filesEnabled?: boolean;
  }

  let {
    onSend,
    disabled = false,
    placeholder = 'Message Nymeria...',
    filesEnabled = true,
  }: Props = $props();

  let inputValue = $state('');
  let textareaRef = $state<HTMLTextAreaElement | null>(null);
  let fileInputRef = $state<HTMLInputElement | null>(null);
  let pendingFiles = $state<FileAttachment[]>([]);
  let modalFile = $state<FileAttachment | null>(null);
  let errorMessage = $state<string | null>(null);
  let commands = $state<SlashCommandInfo[]>([]);
  let commandsLoaded = $state(false);
  let commandsLoading = $state(false);
  let highlightedCommandIndex = $state(0);

  let isStreaming = $derived(chatStore.isStreaming);
  let pendingImageCount = $derived(pendingFiles.filter((f) => f.type === 'image').length);
  // After Phase B, non-image attachments are sandboxed at ingress and ride
  // inside the message text (which queues fine). Only image attachments still
  // can't be queued (PendingPrompt schema doesn't carry image_url blocks).
  let canSendWhileStreaming = $derived(inputValue.trim().length > 0 && pendingImageCount === 0);
  let canSend = $derived(
    isStreaming
      ? !disabled && canSendWhileStreaming
      : (inputValue.trim().length > 0 || pendingFiles.length > 0) && !disabled
  );

  // Per-model attachment caps fetched once per thread. Default to the
  // conservative DEFAULT_ATTACHMENT_LIMITS until the backend reply lands.
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
  let filteredCommands = $derived(
    isCommandNameEntry
      ? commands
          .filter((command) => (
            command.name.includes(slashQuery) ||
            command.description.toLowerCase().includes(slashQuery)
          ))
          .slice(0, 8)
      : []
  );
  let showCommandPalette = $derived(
    isCommandNameEntry &&
    filteredCommands.length > 0 &&
    !disabled &&
    !isStreaming
  );

  $effect(() => {
    if (isCommandNameEntry && !commandsLoaded && !commandsLoading) {
      void loadCommands();
    }
  });

  $effect(() => {
    if (highlightedCommandIndex >= filteredCommands.length) {
      highlightedCommandIndex = 0;
    }
  });

  async function loadCommands() {
    commandsLoading = true;
    try {
      commands = await api.listCommands();
      commandsLoaded = true;
    } catch (error) {
      console.warn('[InputBar] Failed to load slash commands:', error);
    } finally {
      commandsLoading = false;
    }
  }

  function insertCommand(command: SlashCommandInfo) {
    inputValue = `/${command.name} `;
    highlightedCommandIndex = 0;
    requestAnimationFrame(() => {
      if (!textareaRef) return;
      textareaRef.style.height = 'auto';
      textareaRef.style.height = Math.min(textareaRef.scrollHeight, 120) + 'px';
      textareaRef.focus();
    });
  }

  function handleSubmit() {
    if (!canSend) return;
    hapticImpact('light');
    onSend(inputValue.trim(), pendingFiles.length > 0 ? pendingFiles : undefined);
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
        commands = [];
        commandsLoaded = false;
        return;
      }
    }

    // Mobile: Enter sends, Shift+Enter for newline
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      handleSubmit();
    }
  }

  function handleInput() {
    if (isCommandNameEntry && !commandsLoaded && !commandsLoading) {
      void loadCommands();
    }
    if (textareaRef) {
      textareaRef.style.height = 'auto';
      textareaRef.style.height = Math.min(textareaRef.scrollHeight, 120) + 'px';
    }
  }

  function showError(message: string) {
    errorMessage = message;
    setTimeout(() => {
      errorMessage = null;
    }, 4000);
  }

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

  function removeFile(id: string) {
    pendingFiles = pendingFiles.filter((f) => f.id !== id);
  }

  function openFileModal(file: FileAttachment) {
    if (file.type === 'image') {
      modalFile = file;
    }
  }

  function closeModal() {
    modalFile = null;
  }

  async function handlePaste(event: ClipboardEvent) {
    if (!filesEnabled || !event.clipboardData) return;
    const pastedFiles = getFilesFromClipboard(event.clipboardData);
    if (pastedFiles.length > 0) {
      event.preventDefault();
      await addFiles(pastedFiles);
    }
  }

  async function handleFileSelect(event: Event) {
    const input = event.target as HTMLInputElement;
    if (!input.files) return;
    const files = Array.from(input.files);
    await addFiles(files);
    input.value = '';
  }

  function openFilePicker() {
    fileInputRef?.click();
  }

  async function openCamera() {
    try {
      const { Camera, CameraResultType, CameraSource } = await import('@capacitor/camera');
      const photo = await Camera.getPhoto({
        quality: 85,
        allowEditing: false,
        resultType: CameraResultType.DataUrl,
        source: CameraSource.Camera,
      });

      if (photo.dataUrl) {
        const attachment: FileAttachment = {
          id: `photo-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          type: 'image',
          dataUrl: photo.dataUrl,
          mimeType: `image/${photo.format || 'jpeg'}`,
          name: `photo_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.${photo.format || 'jpg'}`,
          size: Math.round((photo.dataUrl.length * 3) / 4),
        };
        pendingFiles = [...pendingFiles, attachment];
      }
    } catch (error) {
      if (error instanceof Error && error.message.includes('User cancelled')) {
        // User cancelled camera — not an error
      } else {
        console.warn('[InputBar] Camera not available:', error);
        showError('Camera not available. Use the attach button instead.');
      }
    }
  }
</script>

<div class="input-container">
  {#if errorMessage}
    <div class="error-banner">
      <Icon name="error" size={14} />
      <span>{errorMessage}</span>
    </div>
  {/if}

  <FilePreview files={pendingFiles} onRemove={removeFile} onFileClick={openFileModal} />

  {#if showCommandPalette}
    <div class="command-palette">
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
        class="attach-btn"
        onclick={openFilePicker}
        disabled={disabled || isStreaming}
        title="Attach files"
      >
        <Icon name="paperclip" size={22} />
      </button>
      <button
        class="attach-btn"
        onclick={openCamera}
        disabled={disabled || isStreaming}
        title="Take photo"
      >
        <Icon name="camera" size={22} />
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
        class="send-btn streaming"
        onclick={handleStopClick}
        title="Stop the current turn"
      >
        <Icon name="stop" size={16} />
      </button>
    {/if}
    <button
      class="send-btn"
      onclick={handleSendClick}
      disabled={!canSend}
      title={isStreaming ? 'Queue this message until the agent halts' : 'Send'}
    >
      <Icon name="send" size={20} />
    </button>
  </div>
</div>

<ImageModal image={modalFile} onClose={closeModal} />

<style>
  .input-container {
    position: relative;
    border-top: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    padding: var(--spacing-sm) var(--spacing-md);
    padding-bottom: calc(var(--spacing-sm) + var(--keyboard-height, 0px));
    transition: padding-bottom var(--transition-fast);
    flex-shrink: 0;
  }

  .input-bar {
    display: flex;
    align-items: flex-end;
    gap: var(--spacing-sm);
  }

  .command-palette {
    position: absolute;
    left: var(--spacing-sm);
    right: var(--spacing-sm);
    bottom: calc(100% + var(--spacing-xs));
    z-index: 20;
    max-height: 260px;
    overflow-y: auto;
    padding: var(--spacing-xs);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    box-shadow: var(--shadow-lg);
  }

  .command-option {
    display: grid;
    grid-template-columns: 92px 1fr;
    gap: 2px var(--spacing-sm);
    width: 100%;
    min-height: 48px;
    padding: var(--spacing-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-primary);
    text-align: left;
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
    grid-column: 1 / -1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .attach-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    flex-shrink: 0;
    background: none;
    border: none;
  }

  .attach-btn:active:not(:disabled) {
    background: var(--bg-hover);
    color: var(--accent-primary);
  }

  .attach-btn:disabled {
    opacity: 0.4;
  }

  .message-input {
    flex: 1;
    min-height: var(--touch-target-min);
    max-height: 120px;
    padding: 10px var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    color: var(--text-primary);
    font-family: inherit;
    font-size: 16px; /* Prevents iOS zoom */
    line-height: 1.4;
    resize: none;
    overflow-y: auto;
    transition: border-color var(--transition-fast);
  }

  .message-input:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: none;
  }

  .message-input::placeholder {
    color: var(--text-muted);
  }

  .message-input:disabled {
    opacity: 0.5;
  }

  .send-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    border-radius: 50%;
    background: var(--accent-primary);
    color: var(--bg-base);
    flex-shrink: 0;
    border: none;
    transition: all var(--transition-fast);
  }

  .send-btn.streaming {
    background: var(--error);
    color: white;
  }

  .send-btn:disabled {
    opacity: 0.3;
  }

  .send-btn:active:not(:disabled) {
    transform: scale(0.95);
  }

  .file-input-hidden {
    position: absolute;
    width: 0;
    height: 0;
    opacity: 0;
    pointer-events: none;
  }

  .error-banner {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    margin-bottom: var(--spacing-xs);
    background: rgba(239, 68, 68, 0.1);
    color: var(--error);
    font-size: var(--font-size-sm);
    border-radius: var(--radius-md);
  }
</style>
