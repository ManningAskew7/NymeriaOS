<script lang="ts">
  import { Button, Icon } from '$lib/components/common';
  import { chatStore } from '$lib/stores/chat.svelte';
  import type { FileAttachment } from '$lib/types';
  import FilePreview from './FilePreview.svelte';
  import ImageModal from './ImageModal.svelte';
  import {
    processFile,
    getFilesFromClipboard,
    getFilesFromDrop,
    FILE_CONSTRAINTS,
    getMaxFilesErrorMessage,
    getSupportedFileExtensions,
    type FileProcessingError
  } from '$lib/utils/fileProcessing';

  interface Props {
    onSend: (message: string, attachments?: FileAttachment[]) => void;
    disabled?: boolean;
    placeholder?: string;
    filesEnabled?: boolean;
  }

  let {
    onSend,
    disabled = false,
    placeholder = 'Type a message...',
    filesEnabled = true
  }: Props = $props();

  let inputValue = $state('');
  let textareaRef = $state<HTMLTextAreaElement | null>(null);
  let fileInputRef = $state<HTMLInputElement | null>(null);
  let pendingFiles = $state<FileAttachment[]>([]);
  let isDragOver = $state(false);
  let modalFile = $state<FileAttachment | null>(null);
  let errorMessage = $state<string | null>(null);

  let isStreaming = $derived(chatStore.isStreaming);
  let canSend = $derived(
    (inputValue.trim().length > 0 || pendingFiles.length > 0) && !disabled && !isStreaming
  );

  function handleSubmit() {
    if ((inputValue.trim() || pendingFiles.length > 0) && !disabled && !isStreaming) {
      onSend(inputValue.trim(), pendingFiles.length > 0 ? pendingFiles : undefined);
      inputValue = '';
      pendingFiles = [];
      if (textareaRef) {
        textareaRef.style.height = 'auto';
      }
    }
  }

  function handleButtonClick() {
    if (isStreaming) {
      chatStore.stopGenerating();
    } else {
      handleSubmit();
    }
  }

  function handleKeyDown(event: KeyboardEvent) {
    // Cmd/Ctrl + Enter to send
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      handleSubmit();
    }
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

  // Add files with validation
  async function addFiles(files: File[]) {
    const remainingSlots = FILE_CONSTRAINTS.MAX_FILES_PER_MESSAGE - pendingFiles.length;

    if (remainingSlots <= 0) {
      showError(getMaxFilesErrorMessage());
      return;
    }

    const filesToProcess = files.slice(0, remainingSlots);

    for (const file of filesToProcess) {
      try {
        const attachment = await processFile(file);
        pendingFiles = [...pendingFiles, attachment];
      } catch (error) {
        const processingError = error as FileProcessingError;
        showError(processingError.message);
      }
    }

    if (files.length > remainingSlots) {
      showError(getMaxFilesErrorMessage());
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
  ondragenter={handleDragEnter}
  ondragover={handleDragOver}
  ondragleave={handleDragLeave}
  ondrop={handleDrop}
>
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

  <div class="input-bar">
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

    {#if filesEnabled}
      <input
        bind:this={fileInputRef}
        type="file"
        accept={getSupportedFileExtensions()}
        multiple
        class="file-input-hidden"
        onchange={handleFileSelect}
      />
      <Button
        variant="ghost"
        size="sm"
        onclick={openFilePicker}
        disabled={disabled || isStreaming || pendingFiles.length >= FILE_CONSTRAINTS.MAX_FILES_PER_MESSAGE}
        title="Attach files"
      >
        <Icon name="paperclip" size={18} />
      </Button>
    {/if}

    <Button
      variant={isStreaming ? 'danger' : 'primary'}
      size="md"
      onclick={handleButtonClick}
      disabled={!canSend && !isStreaming}
    >
      {#if isStreaming}
        <Icon name="stop" size={14} />
      {:else}
        <Icon name="send" size={18} />
      {/if}
    </Button>
  </div>

  {#if isDragOver}
    <div class="drop-overlay">
      <Icon name="paperclip" size={48} />
      <span>Drop files here</span>
    </div>
  {/if}
</div>

<p class="hint">
  Press <kbd>Ctrl</kbd>+<kbd>Enter</kbd> to send
  {#if filesEnabled}
    &middot; Paste or drag files to attach
  {/if}
</p>

<ImageModal image={modalFile} onClose={closeModal} />

<style>
  .input-container {
    position: relative;
    background: var(--bg-elevated-2);
    border-radius: var(--radius-lg);
    border: 1px solid var(--border-subtle);
    transition: border-color var(--transition-fast);
  }

  .input-container:focus-within {
    border-color: var(--accent-primary);
  }

  .input-container.drag-over {
    border-color: var(--accent-primary);
    border-style: dashed;
  }

  .input-bar {
    display: flex;
    align-items: flex-end;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm);
  }

  .message-input {
    flex: 1;
    min-height: 24px;
    max-height: 200px;
    padding: var(--spacing-sm);
    background: transparent;
    border: none;
    color: var(--text-primary);
    font-family: inherit;
    font-size: var(--font-size-base);
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
    background: rgba(var(--accent-primary-rgb, 59, 130, 246), 0.1);
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
    background: rgba(239, 68, 68, 0.1);
    color: var(--accent-danger);
    font-size: var(--font-size-sm);
    border-radius: var(--radius-md) var(--radius-md) 0 0;
    animation: slideDown var(--transition-fast);
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

  .hint {
    margin: var(--spacing-xs) 0 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    text-align: right;
  }

  kbd {
    display: inline-block;
    padding: 2px 6px;
    font-family: var(--font-mono);
    font-size: 0.75em;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
  }
</style>
