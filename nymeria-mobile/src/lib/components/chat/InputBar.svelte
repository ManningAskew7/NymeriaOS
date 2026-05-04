<script lang="ts">
  import { Button, Icon } from '$lib/components/common';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import type { FileAttachment } from '$lib/types';
  import FilePreview from './FilePreview.svelte';
  import ImageModal from './ImageModal.svelte';
  import {
    processFile,
    getFilesFromClipboard,
    FILE_CONSTRAINTS,
    getMaxFilesErrorMessage,
    getSupportedFileExtensions,
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

  let isStreaming = $derived(chatStore.isStreaming);
  let canSend = $derived(
    (inputValue.trim().length > 0 || pendingFiles.length > 0) && !disabled && !isStreaming
  );

  function handleSubmit() {
    if ((inputValue.trim() || pendingFiles.length > 0) && !disabled && !isStreaming) {
      hapticImpact('light');
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
      chatStore.stopGenerating(threadsStore.currentThreadId ?? undefined);
    } else {
      handleSubmit();
    }
  }

  function handleKeyDown(event: KeyboardEvent) {
    // Mobile: Enter sends, Shift+Enter for newline
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      handleSubmit();
    }
  }

  function handleInput() {
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
        disabled={disabled || isStreaming || pendingFiles.length >= FILE_CONSTRAINTS.MAX_FILES_PER_MESSAGE}
        title="Attach files"
      >
        <Icon name="paperclip" size={22} />
      </button>
      <button
        class="attach-btn"
        onclick={openCamera}
        disabled={disabled || isStreaming || pendingFiles.length >= FILE_CONSTRAINTS.MAX_FILES_PER_MESSAGE}
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

    <button
      class="send-btn"
      class:streaming={isStreaming}
      onclick={handleButtonClick}
      disabled={!canSend && !isStreaming}
    >
      {#if isStreaming}
        <Icon name="stop" size={16} />
      {:else}
        <Icon name="send" size={20} />
      {/if}
    </button>
  </div>
</div>

<ImageModal image={modalFile} onClose={closeModal} />

<style>
  .input-container {
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
