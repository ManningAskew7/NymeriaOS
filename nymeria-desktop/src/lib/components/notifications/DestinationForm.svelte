<script lang="ts">
  import type {
    NotificationChannelType,
    NotificationDestination,
    NotificationDestinationCreate,
    NotificationDestinationUpdate
  } from '$lib/types';
  import Button from '$lib/components/common/Button.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';

  interface Props {
    channelTypes: NotificationChannelType[];
    existing?: NotificationDestination | null;
    onSubmit: (create: NotificationDestinationCreate | NotificationDestinationUpdate) => Promise<void>;
    onCancel: () => void;
    onTest?: (destId: string, message?: string) => Promise<{ ok: boolean; detail: string }>;
  }

  let { channelTypes, existing = null, onSubmit, onCancel, onTest }: Props = $props();

  const isEditing = $derived(existing !== null);

  // svelte-ignore state_referenced_locally - intentional one-time form initialization
  let name = $state(existing?.name ?? '');
  // svelte-ignore state_referenced_locally - intentional one-time form initialization
  let selectedType = $state<string>(existing?.type ?? channelTypes[0]?.name ?? '');
  // svelte-ignore state_referenced_locally - intentional one-time form initialization
  let enabled = $state(existing?.enabled ?? true);
  // svelte-ignore state_referenced_locally - intentional one-time form initialization
  let configValues = $state<Record<string, string>>(
    Object.fromEntries(
      Object.entries(existing?.config ?? {}).map(([k, v]) => [k, String(v ?? '')])
    )
  );
  // Secret values are write-only; on edit we show "(set)" placeholders and
  // only emit changed entries.
  let secretValues = $state<Record<string, string>>({});
  let secretsCleared = $state<Record<string, boolean>>({});

  let submitting = $state(false);
  let submitError = $state<string | null>(null);
  let testResult = $state<{ ok: boolean; detail: string } | null>(null);
  let testing = $state(false);

  const selectedChannel = $derived(
    channelTypes.find((c) => c.name === selectedType) ?? null
  );

  function isSecretSet(key: string): boolean {
    if (!existing) return false;
    return existing.secretFieldNames.includes(key) && !secretsCleared[key];
  }

  async function handleSubmit(event: Event) {
    event.preventDefault();
    if (!selectedChannel) return;
    submitting = true;
    submitError = null;
    try {
      if (isEditing && existing) {
        const update: NotificationDestinationUpdate = {
          name,
          config: { ...configValues },
          enabled
        };
        // Only ship secret fields that changed: new value, or cleared.
        const secretPayload: Record<string, string | null> = {};
        for (const [k, v] of Object.entries(secretValues)) {
          if (v !== '') secretPayload[k] = v;
        }
        for (const k of Object.keys(secretsCleared)) {
          if (secretsCleared[k]) secretPayload[k] = null;
        }
        if (Object.keys(secretPayload).length > 0) {
          update.secretFields = secretPayload;
        }
        await onSubmit(update);
      } else {
        const create: NotificationDestinationCreate = {
          name,
          type: selectedType,
          config: { ...configValues },
          secretFields: Object.fromEntries(
            Object.entries(secretValues).filter(([, v]) => v !== '')
          ),
          enabled
        };
        await onSubmit(create);
      }
    } catch (e) {
      submitError = humanizeErrorText(e, { action: 'save', resource: 'the destination' });
    } finally {
      submitting = false;
    }
  }

  async function handleTest() {
    if (!existing || !onTest) return;
    testing = true;
    testResult = null;
    try {
      testResult = await onTest(existing.id);
    } catch (e) {
      testResult = {
        ok: false,
        detail: humanizeErrorText(e, { action: 'test', resource: 'the destination' })
      };
    } finally {
      testing = false;
    }
  }
</script>

<form class="destination-form" onsubmit={handleSubmit}>
  <div class="field">
    <label for="dest-name">Destination name</label>
    <input
      id="dest-name"
      type="text"
      bind:value={name}
      required
      placeholder="e.g. my-phone, work-email"
    />
    <p class="hint">A short label used to reference this destination from profiles.</p>
  </div>

  <div class="field">
    <label for="dest-type">Channel type</label>
    <select
      id="dest-type"
      bind:value={selectedType}
      disabled={isEditing}
    >
      {#each channelTypes as ct (ct.name)}
        <option value={ct.name}>{ct.name}</option>
      {/each}
    </select>
    {#if selectedChannel}
      <p class="hint">{selectedChannel.description}</p>
    {/if}
  </div>

  {#if selectedChannel}
    {#each selectedChannel.configFields as field (field.key)}
      <div class="field">
        <label for="dest-cfg-{field.key}">
          {field.label}
          {#if field.required}<span class="required">*</span>{/if}
        </label>
        {#if field.secret}
          {#if isSecretSet(field.key)}
            <div class="secret-set-row">
              <span class="secret-set-label">(value is set)</span>
              <button
                type="button"
                class="link-btn"
                onclick={() => {
                  secretsCleared[field.key] = true;
                }}
              >
                Replace
              </button>
              <button
                type="button"
                class="link-btn danger"
                onclick={() => {
                  secretsCleared[field.key] = true;
                  secretValues[field.key] = '';
                }}
              >
                Clear
              </button>
            </div>
          {:else}
            <input
              id="dest-cfg-{field.key}"
              type="password"
              autocomplete="new-password"
              bind:value={secretValues[field.key]}
              required={field.required && !isEditing}
              placeholder="(write-only)"
            />
          {/if}
        {:else}
          <input
            id="dest-cfg-{field.key}"
            type="text"
            bind:value={configValues[field.key]}
            required={field.required}
          />
        {/if}
        {#if field.help}
          <p class="hint">{field.help}</p>
        {/if}
      </div>
    {/each}
  {/if}

  <div class="field row">
    <label class="check">
      <input type="checkbox" bind:checked={enabled} />
      Enabled
    </label>
  </div>

  {#if submitError}
    <div class="error">{submitError}</div>
  {/if}

  {#if testResult}
    <div class="test-result" class:ok={testResult.ok} class:err={!testResult.ok}>
      {testResult.ok ? '✓' : '✗'} {testResult.detail}
    </div>
  {/if}

  <div class="actions">
    {#if isEditing && onTest}
      <Button
        variant="ghost"
        type="button"
        onclick={handleTest}
        disabled={testing}
      >
        {testing ? 'Sending…' : 'Send test'}
      </Button>
    {/if}
    <span class="spacer"></span>
    <Button variant="ghost" type="button" onclick={onCancel}>Cancel</Button>
    <Button variant="primary" type="submit" disabled={submitting}>
      {submitting ? 'Saving…' : isEditing ? 'Save changes' : 'Create destination'}
    </Button>
  </div>
</form>

<style>
  .destination-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .field.row {
    flex-direction: row;
    align-items: center;
    gap: var(--spacing-sm);
  }

  label {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    font-weight: 500;
  }

  label.check {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
  }

  .required {
    color: var(--error);
    margin-left: 2px;
  }

  input,
  select {
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

  .secret-set-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-sm);
  }

  .secret-set-label {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .link-btn {
    background: none;
    border: 0;
    color: var(--accent-primary);
    font-size: var(--font-size-xs);
    cursor: pointer;
    padding: 0;
  }

  .link-btn.danger {
    color: var(--error);
  }

  .error {
    padding: var(--spacing-sm);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--error) 12%, transparent);
    color: var(--error);
    font-size: var(--font-size-sm);
  }

  .test-result {
    padding: var(--spacing-sm);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
  }

  .test-result.ok {
    background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
    color: var(--accent-primary);
  }

  .test-result.err {
    background: color-mix(in srgb, var(--error) 12%, transparent);
    color: var(--error);
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
