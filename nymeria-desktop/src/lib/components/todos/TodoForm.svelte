<script lang="ts">
  import { Modal, Button, Icon } from '$lib/components/common';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import type { TodoItem } from '$lib/types';

  interface Props {
    isOpen: boolean;
    onClose: () => void;
    editTodo?: TodoItem | null; // If provided, we're in edit mode
  }

  let { isOpen, onClose, editTodo = null }: Props = $props();

  type RecurrenceUnit = '' | 's' | 'm' | 'h' | 'd' | 'w' | 'mo';

  // Mirrors Nymeria.core.todo_constants.LEGACY_RECURRENCE_ALIASES so the form
  // can populate amount/unit when editing a TODO created before arbitrary
  // intervals existed.
  const LEGACY_ALIASES: Record<string, string> = {
    '5min': '5m', '10min': '10m', '15min': '15m', '30min': '30m',
    hourly: '1h', daily: '1d', weekly: '1w', monthly: '1mo'
  };

  function parseRecurrenceForForm(value: string | undefined): { amount: number; unit: RecurrenceUnit } {
    if (!value) return { amount: 1, unit: '' };
    const canonical = LEGACY_ALIASES[value.toLowerCase()] ?? value.toLowerCase();
    const match = /^(\d+)(mo|s|m|h|d|w)$/.exec(canonical);
    if (!match) return { amount: 1, unit: '' };
    return { amount: parseInt(match[1], 10), unit: match[2] as RecurrenceUnit };
  }

  // Form state
  let task = $state('');
  let notes = $state('');
  let scheduledFor = $state('');
  let recurrenceAmount = $state<number>(1);
  let recurrenceUnit = $state<RecurrenceUnit>('');
  let selectedThreadId = $state<string>('__new__'); // '__new__' means create new thread
  let saving = $state(false);
  let deleting = $state(false);
  let showDeleteConfirm = $state(false);
  let formError = $state('');

  // Guard to prevent form re-initialization during save
  let formInitialized = $state(false);

  // Get available threads for dropdown
  let availableThreads = $derived(threadsStore.threads);

  // Initialize form when modal opens (only once per open)
  $effect(() => {
    if (isOpen && !formInitialized) {
      formInitialized = true;
      if (editTodo) {
        // Edit mode: populate form with existing todo data
        task = editTodo.task;
        notes = editTodo.notes || '';
        scheduledFor = editTodo.scheduledFor ? formatDateTimeForInput(editTodo.scheduledFor) : '';
        const parsed = parseRecurrenceForForm(editTodo.recurrence);
        recurrenceAmount = parsed.amount;
        recurrenceUnit = parsed.unit;
        // Use existing thread or default to new
        selectedThreadId = editTodo.threadId || '__new__';
      } else {
        // Create mode: reset form
        task = '';
        notes = '';
        scheduledFor = '';
        recurrenceAmount = 1;
        recurrenceUnit = '';
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
    // Format as local datetime for input
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    const hours = String(date.getHours()).padStart(2, '0');
    const minutes = String(date.getMinutes()).padStart(2, '0');
    return `${year}-${month}-${day}T${hours}:${minutes}`;
  }

  let isEditMode = $derived(!!editTodo);
  let modalTitle = $derived(isEditMode ? 'Edit Task' : 'New Task');

  // Only show thread selector if scheduled
  let showThreadSelector = $derived(!!scheduledFor);

  async function handleSubmit(e: SubmitEvent) {
    e.preventDefault();
    formError = '';

    if (!task.trim()) {
      formError = 'Task description is required';
      return;
    }

    let recurrence: string | undefined;
    if (recurrenceUnit) {
      if (!Number.isFinite(recurrenceAmount) || recurrenceAmount <= 0) {
        formError = 'Repeat amount must be a positive whole number.';
        return;
      }
      const amount = Math.floor(recurrenceAmount);
      if (recurrenceUnit === 's' && amount < 60) {
        formError = 'Repeat interval must be at least 60 seconds.';
        return;
      }
      recurrence = `${amount}${recurrenceUnit}`;
    }

    saving = true;

    try {
      // Determine the thread ID
      let threadId: string | undefined;

      if (scheduledFor) {
        if (selectedThreadId === '__new__') {
          // Create a new thread for this scheduled task
          const taskPreview = task.trim().slice(0, 30);
          const newThread = threadsStore.createThread(`Scheduled: ${taskPreview}${task.length > 30 ? '...' : ''}`);
          threadId = newThread.id;
        } else {
          threadId = selectedThreadId;
        }
      }

      // Convert datetime-local to ISO string with timezone for API
      // This ensures the backend interprets the time correctly
      let scheduledForValue: string | undefined;
      if (scheduledFor) {
        const localDate = new Date(scheduledFor);
        scheduledForValue = localDate.toISOString(); // e.g., "2024-01-01T22:15:00.000Z"
      }

      if (isEditMode && editTodo) {
        // Update existing todo
        await todosStore.update(editTodo.id, {
          task: task.trim(),
          notes: notes.trim() || undefined,
          scheduledFor: scheduledForValue,
          recurrence: recurrence,
          threadId: threadId,
          clearSchedule: !scheduledFor && !!editTodo.scheduledFor,
          clearRecurrence: !recurrence && !!editTodo.recurrence
        });
      } else {
        // Create new todo
        await todosStore.create({
          task: task.trim(),
          notes: notes.trim() || undefined,
          scheduledFor: scheduledForValue,
          recurrence: recurrence,
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

    <div class="form-row">
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
        <label for="recurrenceUnit">Repeat</label>
        <div class="recurrence-row">
          {#if recurrenceUnit}
            <input
              id="recurrenceAmount"
              class="recurrence-amount"
              type="number"
              min="1"
              step="1"
              bind:value={recurrenceAmount}
              disabled={saving || deleting}
              aria-label="Repeat amount"
            />
          {/if}
          <select id="recurrenceUnit" bind:value={recurrenceUnit} disabled={saving || deleting}>
            <option value="">Never</option>
            <option value="s">Seconds</option>
            <option value="m">Minutes</option>
            <option value="h">Hours</option>
            <option value="d">Days</option>
            <option value="w">Weeks</option>
            <option value="mo">Months</option>
          </select>
        </div>
        {#if recurrenceUnit === 's'}
          <span class="form-hint">Minimum 60 seconds.</span>
        {:else if recurrenceUnit === 'mo'}
          <span class="form-hint">Calendar months. Anchored on the 31st clamps to the last day of shorter months.</span>
        {/if}
      </div>
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
    min-width: 400px;
  }

  .form-error {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm);
    background: rgba(var(--error-rgb, 239, 68, 68), 0.1);
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

  .form-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--spacing-md);
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
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-base);
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
    cursor: not-allowed;
  }

  textarea {
    resize: vertical;
    min-height: 80px;
  }

  .form-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .recurrence-row {
    display: flex;
    gap: var(--spacing-xs);
    align-items: center;
  }

  .recurrence-row select {
    flex: 1;
  }

  .recurrence-amount {
    width: 5rem;
  }

  .form-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    margin-top: var(--spacing-sm);
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }

  .primary-actions {
    display: flex;
    gap: var(--spacing-sm);
    margin-left: auto;
  }

  .delete-confirm {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--error);
  }
</style>
