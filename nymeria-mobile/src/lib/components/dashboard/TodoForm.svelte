<script lang="ts">
  import Modal from '$lib/components/common/Modal.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import type { TodoItem, TodoRecurrence } from '$lib/types';

  interface Props {
    isOpen: boolean;
    onClose: () => void;
    editTodo?: TodoItem | null;
  }

  let { isOpen, onClose, editTodo = null }: Props = $props();

  // Form state
  let task = $state('');
  let notes = $state('');
  let scheduledFor = $state('');
  let recurrence = $state<TodoRecurrence | ''>('');
  let selectedThreadId = $state<string>('__new__');
  let saving = $state(false);
  let deleting = $state(false);
  let showDeleteConfirm = $state(false);
  let formError = $state('');

  // Guard to prevent form re-initialization during save
  let formInitialized = $state(false);

  let availableThreads = $derived(threadsStore.threads);

  // Initialize form when modal opens (only once per open)
  $effect(() => {
    if (isOpen && !formInitialized) {
      formInitialized = true;
      if (editTodo) {
        task = editTodo.task;
        notes = editTodo.notes || '';
        scheduledFor = editTodo.scheduledFor ? formatDateTimeForInput(editTodo.scheduledFor) : '';
        recurrence = editTodo.recurrence || '';
        selectedThreadId = editTodo.threadId || '__new__';
      } else {
        task = '';
        notes = '';
        scheduledFor = '';
        recurrence = '';
        selectedThreadId = '__new__';
      }
      formError = '';
      showDeleteConfirm = false;
    }
  });

  // Reset initialization flag when modal closes
  $effect(() => {
    if (!isOpen) {
      formInitialized = false;
    }
  });

  function formatDateTimeForInput(date: Date): string {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    const hours = String(date.getHours()).padStart(2, '0');
    const minutes = String(date.getMinutes()).padStart(2, '0');
    return `${year}-${month}-${day}T${hours}:${minutes}`;
  }

  let isEditMode = $derived(!!editTodo);
  let modalTitle = $derived(isEditMode ? 'Edit Task' : 'New Task');
  let showThreadSelector = $derived(!!scheduledFor);

  async function handleSubmit(e: SubmitEvent) {
    e.preventDefault();
    formError = '';

    if (!task.trim()) {
      formError = 'Task description is required';
      return;
    }

    saving = true;

    try {
      let threadId: string | undefined;

      if (scheduledFor) {
        if (selectedThreadId === '__new__') {
          const taskPreview = task.trim().slice(0, 30);
          const newThread = threadsStore.createThread(`Scheduled: ${taskPreview}${task.length > 30 ? '...' : ''}`);
          threadId = newThread.id;
        } else {
          threadId = selectedThreadId;
        }
      }

      let scheduledForValue: string | undefined;
      if (scheduledFor) {
        const localDate = new Date(scheduledFor);
        scheduledForValue = localDate.toISOString();
      }

      if (isEditMode && editTodo) {
        await todosStore.update(editTodo.id, {
          task: task.trim(),
          notes: notes.trim() || undefined,
          scheduledFor: scheduledForValue,
          recurrence: recurrence || undefined,
          threadId: threadId,
          clearSchedule: !scheduledFor && !!editTodo.scheduledFor,
          clearRecurrence: !recurrence && !!editTodo.recurrence
        });
      } else {
        await todosStore.create({
          task: task.trim(),
          notes: notes.trim() || undefined,
          scheduledFor: scheduledForValue,
          recurrence: recurrence || undefined,
          threadId: threadId
        });
      }
      onClose();
    } catch (e) {
      formError = e instanceof Error ? e.message : 'Failed to save task';
    } finally {
      saving = false;
    }
  }

  async function handleDelete() {
    if (!editTodo) return;

    deleting = true;
    try {
      await todosStore.delete(editTodo.id);
      onClose();
    } catch (e) {
      formError = e instanceof Error ? e.message : 'Failed to delete task';
    } finally {
      deleting = false;
      showDeleteConfirm = false;
    }
  }

  function handleClose() {
    if (!saving && !deleting) {
      onClose();
    }
  }
</script>

<Modal title={modalTitle} {isOpen} onClose={handleClose}>
  <form class="todo-form" onsubmit={handleSubmit}>
    {#if formError}
      <div class="form-error">
        <Icon name="warning" size={14} />
        {formError}
      </div>
    {/if}

    <div class="form-group">
      <label for="task">Task *</label>
      <input
        id="task"
        type="text"
        bind:value={task}
        placeholder="What needs to be done?"
        maxlength={500}
        required
        disabled={saving || deleting}
      />
    </div>

    <div class="form-group">
      <label for="notes">Notes</label>
      <textarea
        id="notes"
        bind:value={notes}
        placeholder="Additional details..."
        rows={3}
        maxlength={1000}
        disabled={saving || deleting}
      ></textarea>
    </div>

    <div class="form-group">
      <label for="scheduledFor">Schedule</label>
      <input
        id="scheduledFor"
        type="datetime-local"
        bind:value={scheduledFor}
        disabled={saving || deleting}
      />
      <span class="form-hint">When Nymeria should work on this</span>
    </div>

    <div class="form-group">
      <label for="recurrence">Repeat</label>
      <select id="recurrence" bind:value={recurrence} disabled={saving || deleting}>
        <option value="">Never</option>
        <option value="5min">Every 5 minutes</option>
        <option value="10min">Every 10 minutes</option>
        <option value="15min">Every 15 minutes</option>
        <option value="30min">Every 30 minutes</option>
        <option value="hourly">Hourly</option>
        <option value="daily">Daily</option>
        <option value="weekly">Weekly</option>
        <option value="monthly">Monthly</option>
      </select>
    </div>

    {#if showThreadSelector}
      <div class="form-group">
        <label for="threadId">Output to conversation</label>
        <select id="threadId" bind:value={selectedThreadId} disabled={saving || deleting}>
          <option value="__new__">+ New conversation</option>
          {#each availableThreads as thread (thread.id)}
            <option value={thread.id}>{thread.title}</option>
          {/each}
        </select>
        <span class="form-hint">Where the response will appear when the task runs</span>
      </div>
    {/if}

    <div class="form-actions">
      {#if isEditMode}
        {#if showDeleteConfirm}
          <div class="delete-confirm">
            <span>Delete this task?</span>
            <Button variant="danger" size="sm" onclick={handleDelete} loading={deleting}>
              Yes, Delete
            </Button>
            <Button variant="ghost" size="sm" onclick={() => (showDeleteConfirm = false)} disabled={deleting}>
              Cancel
            </Button>
          </div>
        {:else}
          <Button variant="ghost" size="sm" onclick={() => (showDeleteConfirm = true)} disabled={saving}>
            <Icon name="trash" size={14} />
            Delete
          </Button>
        {/if}
      {/if}

      <div class="primary-actions">
        <Button variant="secondary" onclick={handleClose} disabled={saving || deleting}>
          Cancel
        </Button>
        <Button variant="primary" type="submit" loading={saving} disabled={deleting}>
          {isEditMode ? 'Save Changes' : 'Create Task'}
        </Button>
      </div>
    </div>
  </form>
</Modal>

<style>
  .todo-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .form-error {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm);
    background: color-mix(in srgb, var(--error) 10%, transparent);
    border: 1px solid var(--error);
    border-radius: var(--radius-md);
    color: var(--error);
    font-size: var(--font-size-sm);
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  label {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-secondary);
  }

  input,
  select,
  textarea {
    padding: var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: 16px; /* prevents iOS zoom */
    font-family: inherit;
    transition: border-color var(--transition-fast);
  }

  input:focus,
  select:focus,
  textarea:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  input:disabled,
  select:disabled,
  textarea:disabled {
    opacity: 0.6;
  }

  textarea {
    resize: vertical;
    min-height: 80px;
  }

  .form-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .form-actions {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }

  .primary-actions {
    display: flex;
    gap: var(--spacing-sm);
    justify-content: flex-end;
  }

  .delete-confirm {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--error);
    flex-wrap: wrap;
  }
</style>
