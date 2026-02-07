<script lang="ts">
  import type { Thread } from '$lib/types';
  import { Icon } from '$lib/components/common';

  interface Props {
    thread: Thread;
    isActive: boolean;
    onSelect: () => void;
    onDelete: () => void;
    onRename: (newTitle: string) => void;
  }

  let { thread, isActive, onSelect, onDelete, onRename }: Props = $props();

  let showActions = $state(false);
  let isEditing = $state(false);
  let editTitle = $state('');

  function handleDelete(e: MouseEvent) {
    e.stopPropagation();
    e.preventDefault();
    onDelete();
  }

  function handleClick(e: MouseEvent) {
    // Don't select if clicking action buttons or editing
    const target = e.target as HTMLElement;
    if (target.closest('.delete-btn') || target.closest('.edit-btn') || isEditing) return;
    onSelect();
  }

  function startEditing(e: MouseEvent) {
    e.stopPropagation();
    e.preventDefault();
    editTitle = thread.title;
    isEditing = true;
  }

  function cancelEditing() {
    isEditing = false;
    editTitle = '';
  }

  function saveEdit() {
    const trimmed = editTitle.trim();
    if (trimmed && trimmed !== thread.title) {
      onRename(trimmed);
    }
    isEditing = false;
    editTitle = '';
  }

  function handleEditKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      saveEdit();
    } else if (e.key === 'Escape') {
      cancelEditing();
    }
  }

  function handleEditBlur() {
    // Small delay to allow click events to fire first
    setTimeout(() => {
      if (isEditing) {
        saveEdit();
      }
    }, 150);
  }
</script>

<div
  class="thread-item"
  class:active={isActive}
  class:editing={isEditing}
  onclick={handleClick}
  onkeydown={(e) => e.key === 'Enter' && !isEditing && onSelect()}
  onmouseenter={() => (showActions = true)}
  onmouseleave={() => (showActions = false)}
  role="button"
  tabindex="0"
>
  <div class="thread-icon">
    <Icon name="chat" size={16} />
  </div>

  <div class="thread-content">
    {#if isEditing}
      <input
        type="text"
        class="edit-input"
        bind:value={editTitle}
        onkeydown={handleEditKeydown}
        onblur={handleEditBlur}
        autofocus
      />
    {:else}
      <span class="thread-title">{thread.title}</span>
      {#if thread.preview}
        <span class="thread-preview">{thread.preview}</span>
      {/if}
    {/if}
  </div>

  {#if showActions && !isEditing}
    <div class="action-buttons">
      <button class="edit-btn" onclick={startEditing} type="button" title="Rename thread">
        <Icon name="edit" size={14} />
      </button>
      <button class="delete-btn" onclick={handleDelete} type="button" title="Delete thread">
        <Icon name="trash" size={14} />
      </button>
    </div>
  {/if}
</div>

<style>
  .thread-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    text-align: left;
    border-radius: var(--radius-md);
    transition: background var(--transition-fast);
    position: relative;
    cursor: pointer;
  }

  .thread-item:hover {
    background: var(--bg-hover);
  }

  .thread-item.active {
    background: var(--bg-active);
  }

  .thread-item:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .thread-icon {
    flex-shrink: 0;
    color: var(--text-muted);
  }

  .thread-item.active .thread-icon {
    color: var(--accent-primary);
  }

  .thread-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }

  .thread-title {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .thread-preview {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .action-buttons {
    position: absolute;
    right: var(--spacing-sm);
    display: flex;
    gap: 2px;
  }

  .edit-btn,
  .delete-btn {
    padding: var(--spacing-xs);
    color: var(--text-muted);
    border-radius: var(--radius-sm);
    transition: all var(--transition-fast);
    opacity: 0.7;
  }

  .edit-btn:hover {
    color: var(--accent-primary);
    background: var(--bg-elevated-2);
    opacity: 1;
  }

  .delete-btn:hover {
    color: var(--error);
    background: var(--bg-elevated-2);
    opacity: 1;
  }

  .thread-item.editing {
    background: var(--bg-active);
  }

  .edit-input {
    width: 100%;
    padding: 2px 4px;
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--accent-primary);
    border-radius: var(--radius-sm);
    outline: none;
  }

  .edit-input:focus {
    box-shadow: 0 0 0 2px var(--accent-primary-alpha);
  }
</style>
