<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/services/api.svelte';
  import type { UserMemory } from '$lib/types';
  import Button from './Button.svelte';
  import Icon from './Icon.svelte';
  import InlineLoader from './InlineLoader.svelte';
  import { humanizeErrorText, type HumanErrorContext } from '$lib/services/api/humanizeError';

  const MAX_ENTRIES = 100;
  const MAX_VALUE = 1000;

  let memories = $state<UserMemory[]>([]);
  let drafts = $state<Record<string, string>>({});
  let loading = $state(true);
  let busyKey = $state<string | null>(null);
  let adding = $state(false);
  let status = $state<'idle' | 'success' | 'error'>('idle');
  let message = $state('');

  let newKey = $state('');
  let newValue = $state('');

  let atCapacity = $derived(memories.length >= MAX_ENTRIES);

  onMount(load);

  function clearStatus() { status = 'idle'; message = ''; }
  function ok(msg: string) { status = 'success'; message = msg; }
  function fail(e: unknown, ctx: HumanErrorContext) {
    status = 'error';
    message = humanizeErrorText(e, ctx);
  }

  async function load() {
    loading = true;
    try {
      const rows = await api.getMemories();
      memories = rows;
      const next: Record<string, string> = {};
      for (const m of rows) next[m.key] = m.value;
      drafts = next;
      clearStatus();
    } catch (e) {
      fail(e, { action: 'load', resource: 'your memories' });
    } finally {
      loading = false;
    }
  }

  function isDirty(m: UserMemory): boolean {
    return (drafts[m.key] ?? '') !== m.value;
  }

  async function saveRow(key: string) {
    const value = (drafts[key] ?? '').trim();
    if (!value) { status = 'error'; message = 'Value cannot be empty. Delete the entry instead.'; return; }
    busyKey = key;
    clearStatus();
    try {
      await api.saveMemory(key, value);
      await load();
      ok(`Saved "${key}".`);
    } catch (e) {
      fail(e, { action: 'save', resource: 'the memory' });
    } finally {
      busyKey = null;
    }
  }

  async function deleteRow(key: string) {
    busyKey = key;
    clearStatus();
    try {
      await api.forgetMemory(key);
      await load();
      ok(`Deleted "${key}".`);
    } catch (e) {
      fail(e, { action: 'delete', resource: 'the memory' });
    } finally {
      busyKey = null;
    }
  }

  async function addMemory() {
    const key = newKey.trim();
    const value = newValue.trim();
    if (!key) { status = 'error'; message = 'Give the memory a key, like "timezone".'; return; }
    if (!value) { status = 'error'; message = 'Give the memory a value, like "Europe/Berlin".'; return; }
    adding = true;
    clearStatus();
    try {
      await api.saveMemory(key, value);
      newKey = '';
      newValue = '';
      await load();
      ok(`Added "${key}".`);
    } catch (e) {
      fail(e, { action: 'create', resource: 'the memory' });
    } finally {
      adding = false;
    }
  }
</script>

<div class="memory-editor">
  <header class="editor-header">
    <div class="editor-heading">
      <h3 class="section-heading">Global Memory</h3>
      <p class="field-hint">
        Persistent facts the agent remembers about you across every thread,
        stored as key/value entries. The same memory the agent reads and writes
        with its memory tools.
      </p>
    </div>
    <span class="count" class:full={atCapacity}>{memories.length} / {MAX_ENTRIES}</span>
  </header>

  {#if status !== 'idle'}
    <p class="status" class:error={status === 'error'} class:success={status === 'success'}>{message}</p>
  {/if}

  <div class="add-form">
    <input
      class="key-input"
      bind:value={newKey}
      placeholder="key (e.g. timezone)"
      maxlength={120}
      disabled={atCapacity}
    />
    <input
      class="value-input"
      bind:value={newValue}
      placeholder="value"
      maxlength={MAX_VALUE}
      disabled={atCapacity}
    />
    <Button variant="primary" onclick={addMemory} disabled={adding || atCapacity} loading={adding}>
      Add
    </Button>
  </div>
  {#if atCapacity}
    <p class="field-hint cap-note">Memory is full ({MAX_ENTRIES} entries). Delete an entry to add another.</p>
  {/if}

  {#if loading}
    <p class="loading"><InlineLoader text="Loading memories…" /></p>
  {:else if memories.length === 0}
    <p class="empty-state">No memories stored yet. Add one above, or let the agent learn things over time.</p>
  {:else}
    <ul class="memory-list">
      {#each memories as m (m.key)}
        <li class="memory-row">
          <div class="row-head">
            <span class="row-key" title={m.key}>{m.key}</span>
            <span class="row-count">{(drafts[m.key] ?? '').length} / {MAX_VALUE}</span>
          </div>
          <textarea
            class="value-edit"
            bind:value={drafts[m.key]}
            maxlength={MAX_VALUE}
            rows={2}
          ></textarea>
          <div class="row-actions">
            <Button
              variant="secondary"
              size="sm"
              onclick={() => saveRow(m.key)}
              disabled={busyKey === m.key || !isDirty(m)}
            >
              Save
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onclick={() => deleteRow(m.key)}
              disabled={busyKey === m.key}
              title="Delete this memory"
            >
              <Icon name="trash" size={14} />
            </Button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .memory-editor {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    padding: var(--spacing-lg);
  }

  .editor-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--spacing-md);
  }

  .editor-heading { min-width: 0; }

  .section-heading {
    margin: 0 0 6px 0;
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
  }

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
    margin: 0;
    /* §5 — reading text capped to 60ch so multi-line hints stay readable
       on wide displays instead of stretching the full panel width. */
    max-width: 60ch;
  }

  .cap-note { color: var(--warning); }

  .count {
    flex-shrink: 0;
    padding: 2px 10px;
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--text-muted);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
  }

  .count.full {
    color: var(--warning);
    border-color: var(--warning);
  }

  .status {
    font-size: var(--font-size-xs);
    margin: 0;
  }

  .status.success { color: var(--success); }
  .status.error { color: var(--error); }

  .add-form {
    display: flex;
    gap: var(--spacing-sm);
    align-items: stretch;
  }

  .key-input,
  .value-input,
  .value-edit {
    padding: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    transition: border-color var(--transition-fast);
  }

  .key-input { flex: 0 0 30%; }
  .value-input { flex: 1 1 auto; }

  .key-input:focus,
  .value-input:focus,
  .value-edit:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
  }

  .key-input::placeholder,
  .value-input::placeholder { color: var(--text-muted); }

  .memory-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .memory-row {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: var(--spacing-sm);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated-1, var(--bg-base));
  }

  .row-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm);
  }

  .row-key {
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--accent-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-count {
    flex-shrink: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .value-edit {
    width: 100%;
    resize: vertical;
    font-family: inherit;
    line-height: 1.4;
  }

  .row-actions {
    display: flex;
    gap: var(--spacing-xs);
    justify-content: flex-end;
  }

  .loading,
  .empty-state {
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }
</style>
