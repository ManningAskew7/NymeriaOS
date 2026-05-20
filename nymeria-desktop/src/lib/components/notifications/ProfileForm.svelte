<script lang="ts">
  import type {
    NotificationDestination,
    NotificationProfile,
    NotificationProfileCreate,
    NotificationProfileUpdate
  } from '$lib/types';
  import Button from '$lib/components/common/Button.svelte';

  interface Props {
    destinations: NotificationDestination[];
    existing?: NotificationProfile | null;
    onSubmit: (payload: NotificationProfileCreate | NotificationProfileUpdate) => Promise<void>;
    onCancel: () => void;
  }

  let { destinations, existing = null, onSubmit, onCancel }: Props = $props();

  const isEditing = $derived(existing !== null);

  let name = $state(existing?.name ?? '');
  let selected = $state<Record<string, boolean>>(
    Object.fromEntries(
      destinations.map((d) => [d.name, existing?.destinationNames.includes(d.name) ?? false])
    )
  );
  let submitting = $state(false);
  let submitError = $state<string | null>(null);

  async function handleSubmit(event: Event) {
    event.preventDefault();
    submitting = true;
    submitError = null;
    try {
      const destinationNames = destinations
        .map((d) => d.name)
        .filter((n) => selected[n]);
      if (isEditing) {
        await onSubmit({ name, destinationNames });
      } else {
        await onSubmit({ name, destinationNames });
      }
    } catch (e) {
      submitError = e instanceof Error ? e.message : 'Save failed';
    } finally {
      submitting = false;
    }
  }
</script>

<form class="profile-form" onsubmit={handleSubmit}>
  <div class="field">
    <label for="prof-name">Profile name</label>
    <input
      id="prof-name"
      type="text"
      bind:value={name}
      required
      placeholder="e.g. default, urgent, quiet"
    />
    <p class="hint">
      A short label the notify tool can pick by name (e.g. <code>notify(message, profile="urgent")</code>).
    </p>
  </div>

  <div class="field">
    <p class="label-line">Destinations included in this profile</p>
    {#if destinations.length === 0}
      <p class="hint">No destinations configured yet. Add a destination first.</p>
    {:else}
      <div class="dest-list">
        {#each destinations as dest (dest.id)}
          <label class="dest-row">
            <input
              type="checkbox"
              checked={selected[dest.name]}
              onchange={(e) => (selected[dest.name] = (e.currentTarget as HTMLInputElement).checked)}
            />
            <span class="dest-name">{dest.name}</span>
            <span class="dest-type">{dest.type}</span>
            {#if !dest.enabled}
              <span class="dest-disabled">disabled</span>
            {/if}
          </label>
        {/each}
      </div>
    {/if}
  </div>

  {#if submitError}
    <div class="error">{submitError}</div>
  {/if}

  <div class="actions">
    <span class="spacer"></span>
    <Button variant="ghost" type="button" onclick={onCancel}>Cancel</Button>
    <Button variant="primary" type="submit" disabled={submitting}>
      {submitting ? 'Saving…' : isEditing ? 'Save changes' : 'Create profile'}
    </Button>
  </div>
</form>

<style>
  .profile-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  label,
  .label-line {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    font-weight: 500;
    margin: 0;
  }

  input[type='text'] {
    padding: var(--spacing-sm);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .hint code {
    background: var(--bg-elevated);
    padding: 1px 4px;
    border-radius: 3px;
  }

  .dest-list {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }

  .dest-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm);
    cursor: pointer;
    border-bottom: 1px solid var(--border-subtle);
    transition: background var(--transition-fast);
  }

  .dest-row:last-child {
    border-bottom: 0;
  }

  .dest-row:hover {
    background: var(--bg-hover);
  }

  .dest-name {
    font-weight: 500;
    color: var(--text-primary);
  }

  .dest-type {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    background: var(--bg-elevated);
    padding: 1px 6px;
    border-radius: 4px;
  }

  .dest-disabled {
    font-size: var(--font-size-xs);
    color: var(--accent-danger, #ef4444);
    margin-left: auto;
  }

  .error {
    padding: var(--spacing-sm);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--accent-danger, #ef4444) 12%, transparent);
    color: var(--accent-danger, #ef4444);
    font-size: var(--font-size-sm);
  }

  .actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .spacer {
    flex: 1;
  }
</style>
