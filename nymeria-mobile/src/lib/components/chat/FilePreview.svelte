<script lang="ts">
  import type { FileAttachment } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { formatFileSize, getFileExtension } from '$lib/utils/fileProcessing';

  interface Props {
    files: FileAttachment[];
    onRemove: (id: string) => void;
    onFileClick?: (file: FileAttachment) => void;
  }

  let { files, onRemove, onFileClick }: Props = $props();

  function getFileIcon(mimeType: string): string {
    if (mimeType.startsWith('image/')) return 'image';
    if (mimeType === 'application/pdf') return 'fileText';
    return 'fileText';
  }
</script>

{#if files.length > 0}
  <div class="file-preview-strip">
    {#each files as file (file.id)}
      <div class="file-thumbnail" class:is-document={file.type === 'document'}>
        {#if file.type === 'image'}
          <button
            type="button"
            class="thumbnail-button"
            onclick={() => onFileClick?.(file)}
            title={`${file.name} (${formatFileSize(file.size)})`}
          >
            <img src={file.dataUrl} alt={file.name} />
          </button>
        {:else}
          <button
            type="button"
            class="thumbnail-button document"
            onclick={() => onFileClick?.(file)}
            title={`${file.name} (${formatFileSize(file.size)})`}
          >
            <div class="file-icon-wrapper">
              <Icon name={getFileIcon(file.mimeType)} size={24} />
              <span class="file-ext">{getFileExtension(file.name)}</span>
            </div>
          </button>
        {/if}
        <button
          type="button"
          class="remove-button"
          onclick={() => onRemove(file.id)}
          title="Remove file"
        >
          <Icon name="x" size={12} />
        </button>
      </div>
    {/each}
  </div>
{/if}

<style>
  .file-preview-strip {
    display: flex;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm);
    padding-bottom: 0;
    overflow-x: auto;
    flex-wrap: wrap;
  }

  .file-thumbnail {
    position: relative;
    width: 64px;
    height: 64px;
    flex-shrink: 0;
  }

  .thumbnail-button {
    width: 100%;
    height: 100%;
    padding: 0;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    overflow: hidden;
    cursor: pointer;
    background: var(--bg-elevated);
    transition: border-color var(--transition-fast);
  }

  .thumbnail-button:hover {
    border-color: var(--accent-primary);
  }

  .thumbnail-button img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .thumbnail-button.document {
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg-elevated-2);
  }

  .file-icon-wrapper {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    color: var(--text-secondary);
  }

  .file-ext {
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    color: var(--text-muted);
    max-width: 56px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .remove-button {
    position: absolute;
    top: -6px;
    right: -6px;
    width: 20px;
    height: 20px;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
    border-radius: 50%;
    cursor: pointer;
    color: var(--text-secondary);
    transition: all var(--transition-fast);
  }

  .remove-button:hover {
    background: var(--error);
    border-color: var(--error);
    color: white;
  }
</style>
