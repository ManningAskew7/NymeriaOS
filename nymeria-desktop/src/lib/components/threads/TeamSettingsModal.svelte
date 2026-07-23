<script lang="ts">
  // Team settings modal (backlog #100 phase 3): rename/describe a callable
  // team and edit its shared key-value memory, the operator-side twin of the
  // agent's memory_add/edit(scope="team") tools. Desktop-only (mobile team
  // parity is deferred); memory rows follow the GlobalMemoryEditor pattern.
  import Modal from '../common/Modal.svelte';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import InlineLoader from '../common/InlineLoader.svelte';
  import { api } from '$lib/services/api.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import type { TeamMemoryEntryApi, ThreadTeam } from '$lib/types';
  import { tooltipWhenClipped } from '$lib/actions/tooltip';
  import { humanizeErrorText, type HumanErrorContext } from '$lib/services/api/humanizeError';

  const MAX_VALUE = 1000;
  const MAX_NAME = 120;
  const MAX_DESCRIPTION = 2000;

  interface Props {
    team: ThreadTeam | null;
    isOpen: boolean;
    onClose: () => void;
  }

  let { team, isOpen, onClose }: Props = $props();

  let nameDraft = $state('');
  let descriptionDraft = $state('');
  let memories = $state<TeamMemoryEntryApi[]>([]);
  let drafts = $state<Record<string, string>>({});
  let loading = $state(true);
  let savingDetails = $state(false);
  let busyKey = $state<string | null>(null);
  let adding = $state(false);
  let status = $state<'idle' | 'success' | 'error'>('idle');
  let message = $state('');

  let newKey = $state('');
  let newValue = $state('');

  let loadedTeamId = $state<string | null>(null);

  let detailsDirty = $derived(
    team !== null &&
      (nameDraft.trim() !== team.name ||
        descriptionDraft.trim() !== (team.description ?? ''))
  );

  $effect(() => {
    if (isOpen && team && loadedTeamId !== team.id) {
      loadedTeamId = team.id;
      nameDraft = team.name;
      descriptionDraft = team.description ?? '';
      clearStatus();
      void loadMemories(team.id);
    }
    if (!isOpen) loadedTeamId = null;
  });

  function clearStatus() { status = 'idle'; message = ''; }
  function ok(msg: string) { status = 'success'; message = msg; }
  function fail(e: unknown, ctx: HumanErrorContext) {
    status = 'error';
    message = humanizeErrorText(e, ctx);
  }

  async function loadMemories(teamId: string) {
    loading = true;
    try {
      const rows = await api.listTeamMemories(teamId);
      memories = rows;
      const next: Record<string, string> = {};
      for (const m of rows) next[m.key] = m.value;
      drafts = next;
    } catch (e) {
      fail(e, { action: 'load', resource: 'the team memory' });
    } finally {
      loading = false;
    }
  }

  async function saveDetails() {
    if (!team) return;
    const name = nameDraft.trim();
    if (!name) { status = 'error'; message = 'Team name cannot be empty.'; return; }
    savingDetails = true;
    clearStatus();
    try {
      if (name !== team.name) {
        await threadsStore.renameThreadTeam(team.id, name);
      }
      const description = descriptionDraft.trim();
      if (description !== (team.description ?? '')) {
        await threadsStore.describeThreadTeam(team.id, description);
      }
      ok('Team details saved.');
    } catch (e) {
      fail(e, { action: 'save', resource: 'the team details' });
    } finally {
      savingDetails = false;
    }
  }

  function isDirty(m: TeamMemoryEntryApi): boolean {
    return (drafts[m.key] ?? '') !== m.value;
  }

  async function saveRow(key: string) {
    if (!team) return;
    const value = (drafts[key] ?? '').trim();
    if (!value) { status = 'error'; message = 'Value cannot be empty. Delete the entry instead.'; return; }
    busyKey = key;
    clearStatus();
    try {
      await api.saveTeamMemory(team.id, key, value);
      await loadMemories(team.id);
      ok(`Saved "${key}".`);
    } catch (e) {
      fail(e, { action: 'save', resource: 'the team memory' });
    } finally {
      busyKey = null;
    }
  }

  async function deleteRow(key: string) {
    if (!team) return;
    busyKey = key;
    clearStatus();
    try {
      await api.deleteTeamMemory(team.id, key);
      await loadMemories(team.id);
      ok(`Deleted "${key}".`);
    } catch (e) {
      fail(e, { action: 'delete', resource: 'the team memory' });
    } finally {
      busyKey = null;
    }
  }

  async function addMemory() {
    if (!team) return;
    const key = newKey.trim();
    const value = newValue.trim();
    if (!key) { status = 'error'; message = 'Give the entry a key, like "api_endpoint".'; return; }
    if (!value) { status = 'error'; message = 'Give the entry a value.'; return; }
    adding = true;
    clearStatus();
    try {
      await api.saveTeamMemory(team.id, key, value);
      newKey = '';
      newValue = '';
      await loadMemories(team.id);
      ok(`Added "${key}".`);
    } catch (e) {
      fail(e, { action: 'create', resource: 'the team memory' });
    } finally {
      adding = false;
    }
  }
</script>

<Modal title={team ? `Team: ${team.name}` : 'Team'} {isOpen} {onClose}>
  {#if team}
    <div class="team-settings">
      <section class="details">
        <label class="field">
          <span class="field-label">Name</span>
          <input class="text-input" bind:value={nameDraft} maxlength={MAX_NAME} />
        </label>
        <label class="field">
          <span class="field-label">Description</span>
          <textarea
            class="text-input description-input"
            bind:value={descriptionDraft}
            maxlength={MAX_DESCRIPTION}
            rows={2}
            placeholder="What this team is for (shown to the team's agents in their team memory read)"
          ></textarea>
        </label>
        <div class="details-actions">
          <Button
            variant="secondary"
            size="sm"
            onclick={saveDetails}
            disabled={savingDetails || !detailsDirty}
            loading={savingDetails}
          >
            Save details
          </Button>
        </div>
      </section>

      <section class="memory">
        <header class="memory-header">
          <div>
            <h3 class="section-heading">Team Memory</h3>
            <p class="field-hint">
              Shared key/value facts every thread in this team reads at session
              start and via its memory tools. Running teammates pick up changes
              on their next memory read or compaction.
            </p>
          </div>
          <span class="count">{memories.length}</span>
        </header>

        {#if status !== 'idle'}
          <p class="status" class:error={status === 'error'} class:success={status === 'success'}>{message}</p>
        {/if}

        <div class="add-form">
          <input
            class="text-input key-input"
            bind:value={newKey}
            placeholder="key (e.g. api_endpoint)"
            aria-label="New team memory key"
            maxlength={200}
          />
          <input
            class="text-input value-input"
            bind:value={newValue}
            placeholder="value"
            aria-label="New team memory value"
            maxlength={MAX_VALUE}
          />
          <Button variant="primary" onclick={addMemory} disabled={adding} loading={adding}>
            Add
          </Button>
        </div>

        {#if loading}
          <p class="loading"><InlineLoader text="Loading team memory…" /></p>
        {:else if memories.length === 0}
          <p class="empty-state">No shared entries yet. Add one above, or let the team's agents share facts with memory_add(scope="team").</p>
        {:else}
          <ul class="memory-list">
            {#each memories as m (m.key)}
              <li class="memory-row">
                <div class="row-head">
                  <span class="row-key" use:tooltipWhenClipped={m.key}>{m.key}</span>
                  <span class="row-count">{(drafts[m.key] ?? '').length} / {MAX_VALUE}</span>
                </div>
                <textarea
                  class="text-input value-edit"
                  bind:value={drafts[m.key]}
                  aria-label={`Value for team memory "${m.key}"`}
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
                    dataTooltip="Delete this entry"
                    ariaLabel="Delete this entry"
                  >
                    <Icon name="trash" size={14} />
                  </Button>
                </div>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    </div>
  {/if}
</Modal>

<style>
  .team-settings {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
    min-width: min(520px, 80vw);
  }

  .details,
  .memory {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .field-label {
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-secondary);
  }

  .text-input {
    padding: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    transition: border-color var(--transition-fast);
  }

  .text-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
  }

  .text-input::placeholder { color: var(--text-muted); }

  .description-input,
  .value-edit {
    resize: vertical;
    font-family: inherit;
    line-height: 1.4;
  }

  .details-actions,
  .row-actions {
    display: flex;
    gap: var(--spacing-xs);
    justify-content: flex-end;
  }

  .memory-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--spacing-md);
  }

  .section-heading {
    margin: 0 0 4px 0;
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
  }

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
    margin: 0;
    max-width: 60ch;
  }

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

  .key-input { flex: 0 0 30%; }
  .value-input { flex: 1 1 auto; }

  .memory-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    max-height: 40vh;
    overflow-y: auto;
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

  .value-edit { width: 100%; }

  .loading,
  .empty-state {
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }
</style>
