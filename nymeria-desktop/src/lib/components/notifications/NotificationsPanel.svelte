<script lang="ts">
  import { notificationStore } from '$lib/stores/notifications.svelte';
  import type {
    NotificationDestination,
    NotificationDestinationCreate,
    NotificationDestinationUpdate,
    NotificationProfile,
    NotificationProfileCreate,
    NotificationProfileUpdate
  } from '$lib/types';
  import Button from '$lib/components/common/Button.svelte';
  import { Icon } from '$lib/components/common';
  import DestinationForm from './DestinationForm.svelte';
  import ProfileForm from './ProfileForm.svelte';

  let editingDestinationId = $state<string | null>(null);
  let creatingDestination = $state(false);

  let editingProfileId = $state<string | null>(null);
  let creatingProfile = $state(false);

  let prefSaving = $state(false);

  $effect(() => {
    notificationStore.loadConfig();
  });

  const editingDestination = $derived<NotificationDestination | null>(
    editingDestinationId
      ? notificationStore.destinations.find((d) => d.id === editingDestinationId) ?? null
      : null
  );
  const editingProfile = $derived<NotificationProfile | null>(
    editingProfileId
      ? notificationStore.profiles.find((p) => p.id === editingProfileId) ?? null
      : null
  );

  function closeAllForms() {
    creatingDestination = false;
    editingDestinationId = null;
    creatingProfile = false;
    editingProfileId = null;
  }

  async function handleDestinationSubmit(
    payload: NotificationDestinationCreate | NotificationDestinationUpdate
  ) {
    if (editingDestinationId) {
      await notificationStore.updateDestination(
        editingDestinationId,
        payload as NotificationDestinationUpdate
      );
    } else {
      await notificationStore.createDestination(payload as NotificationDestinationCreate);
    }
    closeAllForms();
  }

  async function handleDestinationDelete(dest: NotificationDestination) {
    if (
      !confirm(
        `Delete destination "${dest.name}"? It will be removed from every profile that uses it.`
      )
    )
      return;
    await notificationStore.deleteDestination(dest.id);
  }

  async function handleProfileSubmit(
    payload: NotificationProfileCreate | NotificationProfileUpdate
  ) {
    if (editingProfileId) {
      await notificationStore.updateProfile(
        editingProfileId,
        payload as NotificationProfileUpdate
      );
    } else {
      await notificationStore.createProfile(payload as NotificationProfileCreate);
    }
    closeAllForms();
  }

  async function handleProfileDelete(profile: NotificationProfile) {
    if (!confirm(`Delete profile "${profile.name}"?`)) return;
    await notificationStore.deleteProfile(profile.id);
  }

  async function handleDefaultProfileChange(event: Event) {
    const next = (event.target as HTMLSelectElement).value;
    prefSaving = true;
    try {
      await notificationStore.updatePreferences({ defaultProfile: next });
    } finally {
      prefSaving = false;
    }
  }

  async function handleTestDestination(destId: string) {
    return notificationStore.testDestination(destId);
  }
</script>

<section class="notifications-panel">
  <header class="section-header">
    <div>
      <h2 class="section-title">Notification routing</h2>
      <p class="section-blurb">
        Define <strong>destinations</strong> (where messages go) and bundle them
        into <strong>profiles</strong> (which the agent picks by name). The
        bell sidebar always logs every notification regardless of where it was
        sent.
      </p>
    </div>
  </header>

  {#if notificationStore.configLoading}
    <div class="loading"><Icon name="loading" size={16} /> Loading…</div>
  {/if}

  {#if notificationStore.configError}
    <div class="error">{notificationStore.configError}</div>
  {/if}

  <!-- Preferences -->
  <div class="card">
    <div class="card-header">
      <h3>Default profile</h3>
    </div>
    <div class="card-body">
      <p class="muted">
        The profile the notify tool routes through when no per-call or
        per-thread override is set.
      </p>
      <div class="pref-row">
        <select
          value={notificationStore.preferences?.defaultProfile ?? 'default'}
          onchange={handleDefaultProfileChange}
          disabled={prefSaving || notificationStore.profiles.length === 0}
        >
          {#if notificationStore.profiles.length === 0}
            <option value="default">default (no profiles yet)</option>
          {:else}
            {#each notificationStore.profiles as p (p.id)}
              <option value={p.name}>{p.name}</option>
            {/each}
          {/if}
        </select>
        {#if prefSaving}
          <span class="muted">Saving…</span>
        {/if}
      </div>
    </div>
  </div>

  <!-- Destinations -->
  <div class="card">
    <div class="card-header">
      <h3>Destinations</h3>
      {#if !creatingDestination && !editingDestinationId}
        <Button
          variant="primary"
          onclick={() => {
            closeAllForms();
            creatingDestination = true;
          }}
        >
          <Icon name="plus" size={14} /> Add destination
        </Button>
      {/if}
    </div>
    <div class="card-body">
      {#if creatingDestination}
        <DestinationForm
          channelTypes={notificationStore.channelTypes}
          onSubmit={handleDestinationSubmit}
          onCancel={closeAllForms}
        />
      {:else if editingDestination}
        <DestinationForm
          channelTypes={notificationStore.channelTypes}
          existing={editingDestination}
          onSubmit={handleDestinationSubmit}
          onCancel={closeAllForms}
          onTest={handleTestDestination}
        />
      {:else if notificationStore.destinations.length === 0}
        <p class="muted">
          No destinations yet. Add one to start routing notifications.
        </p>
      {:else}
        <ul class="entity-list">
          {#each notificationStore.destinations as dest (dest.id)}
            <li class="entity-row" class:disabled={!dest.enabled}>
              <div class="entity-info">
                <div class="entity-title">
                  <span class="entity-name">{dest.name}</span>
                  <span class="entity-type">{dest.type}</span>
                  {#if !dest.enabled}
                    <span class="status-pill off">disabled</span>
                  {/if}
                </div>
                {#if Object.keys(dest.config).length > 0}
                  <p class="entity-meta">
                    {#each Object.entries(dest.config) as [k, v], i (k)}
                      {#if i > 0} · {/if}<span class="cfg-key">{k}</span>=<span class="cfg-val">{String(v)}</span>
                    {/each}
                  </p>
                {/if}
                {#if dest.secretFieldNames.length > 0}
                  <p class="entity-meta secret-meta">
                    secrets set: {dest.secretFieldNames.join(', ')}
                  </p>
                {/if}
              </div>
              <div class="entity-actions">
                <Button
                  variant="ghost"
                  onclick={() => {
                    closeAllForms();
                    editingDestinationId = dest.id;
                  }}
                >
                  Edit
                </Button>
                <Button variant="ghost" onclick={() => handleDestinationDelete(dest)}>
                  Delete
                </Button>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  </div>

  <!-- Profiles -->
  <div class="card">
    <div class="card-header">
      <h3>Profiles</h3>
      {#if !creatingProfile && !editingProfileId}
        <Button
          variant="primary"
          onclick={() => {
            closeAllForms();
            creatingProfile = true;
          }}
        >
          <Icon name="plus" size={14} /> Add profile
        </Button>
      {/if}
    </div>
    <div class="card-body">
      {#if creatingProfile}
        <ProfileForm
          destinations={notificationStore.destinations}
          onSubmit={handleProfileSubmit}
          onCancel={closeAllForms}
        />
      {:else if editingProfile}
        <ProfileForm
          destinations={notificationStore.destinations}
          existing={editingProfile}
          onSubmit={handleProfileSubmit}
          onCancel={closeAllForms}
        />
      {:else if notificationStore.profiles.length === 0}
        <p class="muted">
          No profiles yet. Create one to bundle destinations together (e.g. an
          <code>urgent</code> profile that fires both your phone and email).
        </p>
      {:else}
        <ul class="entity-list">
          {#each notificationStore.profiles as profile (profile.id)}
            <li class="entity-row">
              <div class="entity-info">
                <div class="entity-title">
                  <span class="entity-name">{profile.name}</span>
                  {#if notificationStore.preferences?.defaultProfile === profile.name}
                    <span class="status-pill default">default</span>
                  {/if}
                </div>
                {#if profile.destinationNames.length === 0}
                  <p class="entity-meta muted">
                    (no destinations; notifications are logged in-app only)
                  </p>
                {:else}
                  <p class="entity-meta">
                    destinations: {profile.destinationNames.join(', ')}
                  </p>
                {/if}
              </div>
              <div class="entity-actions">
                <Button
                  variant="ghost"
                  onclick={() => {
                    closeAllForms();
                    editingProfileId = profile.id;
                  }}
                >
                  Edit
                </Button>
                <Button variant="ghost" onclick={() => handleProfileDelete(profile)}>
                  Delete
                </Button>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  </div>
</section>

<style>
  .notifications-panel {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--spacing-md);
  }

  .section-title {
    margin: 0 0 4px 0;
    font-size: var(--font-size-lg);
    font-weight: 600;
  }

  .section-blurb {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    max-width: 60ch;
  }

  .loading,
  .error {
    padding: var(--spacing-sm);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
  }

  .error {
    background: color-mix(in srgb, var(--error) 12%, transparent);
    color: var(--error);
  }

  .card {
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-primary);
    overflow: hidden;
  }

  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
  }

  .card-header h3 {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: 600;
  }

  .card-body {
    padding: var(--spacing-md);
  }

  .pref-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .pref-row select {
    padding: var(--spacing-sm);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    min-width: 200px;
  }

  .muted {
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    margin: 0 0 var(--spacing-sm) 0;
  }

  .entity-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .entity-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--spacing-md);
    padding: var(--spacing-sm);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
  }

  .entity-row.disabled {
    opacity: 0.65;
  }

  .entity-info {
    flex: 1;
    min-width: 0;
  }

  .entity-title {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
  }

  .entity-name {
    font-weight: 600;
    color: var(--text-primary);
  }

  .entity-type {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    background: var(--bg-primary);
    padding: 1px 6px;
    border-radius: 4px;
  }

  .status-pill {
    font-size: var(--font-size-xs);
    padding: 1px 6px;
    border-radius: 4px;
    background: var(--bg-primary);
  }

  .status-pill.off {
    color: var(--error);
  }

  .status-pill.default {
    color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
  }

  .entity-meta {
    margin: 4px 0 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    word-break: break-word;
  }

  .entity-meta.secret-meta {
    font-style: italic;
  }

  .cfg-key {
    color: var(--text-secondary);
  }

  .cfg-val {
    color: var(--text-primary);
  }

  .entity-actions {
    display: flex;
    gap: var(--spacing-xs);
    flex-shrink: 0;
  }
</style>
