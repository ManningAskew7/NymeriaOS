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
  <!-- Intro: plain framing with a divider beneath, no card, no mid-sentence
       bolding. The section headings below carry the structure. -->
  <header class="panel-intro">
    <h2 class="panel-title">Notification routing</h2>
    <p class="panel-lede">Where the notify tool sends messages.</p>
  </header>

  {#if notificationStore.configLoading}
    <div class="status-line"><Icon name="loading" size={16} /> Loading…</div>
  {/if}

  {#if notificationStore.configError}
    <div class="status-line error">{notificationStore.configError}</div>
  {/if}

  <!-- Set-once preference: a quiet inline row bracketed by the header divider
       above and its own divider below, deliberately lighter than the managed
       sections (the thing you set once should not look like the thing you
       manage often). -->
  <div class="default-row">
    <div class="default-label">
      <label class="default-title" for="default-profile">Default profile</label>
      <span class="default-desc">Used when a call sets no override</span>
    </div>
    <div class="default-control">
      <select
        id="default-profile"
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
        <span class="saving">Saving…</span>
      {/if}
    </div>
  </div>

  <!-- Destinations: the primary action keeps the accent fill; everything else
       (Add profile, row Edit/Delete) sits quiet. -->
  <section class="entity-section">
    <div class="entity-head">
      <h3>Destinations</h3>
      {#if !creatingDestination && !editingDestinationId}
        <Button
          variant="primary"
          size="sm"
          onclick={() => {
            closeAllForms();
            creatingDestination = true;
          }}
        >
          <Icon name="plus" size={14} /> Add destination
        </Button>
      {/if}
    </div>

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
      <p class="empty">No destinations yet.</p>
    {:else}
      <ul class="entity-list">
        {#each notificationStore.destinations as dest (dest.id)}
          <li class="entity-row" class:disabled={!dest.enabled}>
            <div class="entity-info">
              <div class="entity-title">
                <span class="entity-name">{dest.name}</span>
                <span class="entity-type">{dest.type}</span>
                {#if Object.keys(dest.config).length > 0}
                  <span class="entity-config">
                    {#each Object.entries(dest.config) as [k, v], i (k)}
                      {#if i > 0} · {/if}<span class="cfg-key">{k}</span> <span class="cfg-val">{String(v)}</span>
                    {/each}
                  </span>
                {/if}
                {#if !dest.enabled}
                  <span class="status-pill off">disabled</span>
                {/if}
              </div>
              {#if dest.secretFieldNames.length > 0}
                <p class="entity-meta secret-meta">
                  secrets set: {dest.secretFieldNames.join(', ')}
                </p>
              {/if}
            </div>
            <div class="entity-actions">
              <Button
                variant="ghost"
                size="sm"
                onclick={() => {
                  closeAllForms();
                  editingDestinationId = dest.id;
                }}
              >
                Edit
              </Button>
              <Button variant="ghost" size="sm" onclick={() => handleDestinationDelete(dest)}>
                Delete
              </Button>
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <!-- Routing profiles: secondary action (outlined), so the page has a single
       focal CTA. -->
  <section class="entity-section">
    <div class="entity-head">
      <h3>Routing profiles</h3>
      {#if !creatingProfile && !editingProfileId}
        <Button
          variant="secondary"
          size="sm"
          onclick={() => {
            closeAllForms();
            creatingProfile = true;
          }}
        >
          <Icon name="plus" size={14} /> Add profile
        </Button>
      {/if}
    </div>

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
      <p class="empty">
        No profiles yet. A profile bundles destinations together (e.g. an
        <code>urgent</code> profile that fires both phone and email).
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
                {#if profile.destinationNames.length === 0}
                  <span class="profile-targets muted">→ in-app log only</span>
                {:else}
                  <span class="profile-targets">→ {profile.destinationNames.join(', ')}</span>
                {/if}
              </div>
            </div>
            <div class="entity-actions">
              <Button
                variant="ghost"
                size="sm"
                onclick={() => {
                  closeAllForms();
                  editingProfileId = profile.id;
                }}
              >
                Edit
              </Button>
              <Button variant="ghost" size="sm" onclick={() => handleProfileDelete(profile)}>
                Delete
              </Button>
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
</section>

<style>
  .notifications-panel {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
  }

  /* Intro is not a card: the title is the page's main heading, the lede is one
     muted line, and a hairline divider sits directly beneath it. */
  .panel-intro {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-2xs);
    padding-bottom: var(--spacing-sm);
    border-bottom: 1px solid var(--border-subtle);
  }

  .panel-title {
    margin: 0;
    font-size: var(--font-size-lg);
    font-weight: 600;
    color: var(--text-primary);
  }

  .panel-lede {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }

  .status-line {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .status-line.error {
    color: var(--error);
  }

  /* Set-once preference: a plain labelled row, no card. A divider beneath
     brackets it (with the intro divider above) so it reads as a single quiet
     setting, distinct from the managed lists below. */
  .default-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding-bottom: var(--spacing-lg);
    border-bottom: 1px solid var(--border-subtle);
  }

  .default-label {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .default-title {
    font-weight: 600;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .default-desc {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
  }

  .default-control {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .default-row select {
    padding: var(--spacing-xs) var(--spacing-sm);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    min-width: 200px;
  }

  .saving {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  /* Managed sections are flat: a heading + action on a row with a divider
     beneath, then a flat list with hairline dividers between rows (DESIGN.md
     §7 — flat lists with dividers, not stacked identically-bordered cards). */
  .entity-section {
    display: flex;
    flex-direction: column;
  }

  .entity-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding-bottom: var(--spacing-sm);
    border-bottom: 1px solid var(--border-subtle);
  }

  .entity-head h3 {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
  }

  .empty {
    margin: 0;
    padding: var(--spacing-sm) 0;
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }

  .entity-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  /* Flat rows: no per-row border or fill, just a hairline divider between rows
     and a hover wash. */
  .entity-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-xs);
    border-bottom: 1px solid var(--border-subtle);
    transition: background var(--transition-fast);
  }

  .entity-row:last-child {
    border-bottom: none;
  }

  .entity-row:hover {
    background: var(--bg-hover);
  }

  .entity-row.disabled {
    opacity: 0.6;
  }

  .entity-info {
    flex: 1;
    min-width: 0;
  }

  .entity-title {
    display: flex;
    align-items: baseline;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
  }

  .entity-name {
    font-weight: 600;
    color: var(--text-primary);
  }

  /* Channel type: an outlined monospace chip (data, not a label) — §3 chip at
     --radius-sm, smaller than body, not a pill. */
  .entity-type {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    padding: 1px 6px;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
  }

  /* Inline config (e.g. chat_id 884213709) reads as data, so it's monospace
     and muted, sitting on the same line as the name and type. */
  .entity-config {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    min-width: 0;
    word-break: break-word;
  }

  .cfg-key {
    color: var(--text-secondary);
  }

  .cfg-val {
    color: var(--text-primary);
  }

  /* Profile targets (→ dest-a, dest-b): data, so monospace + muted, inline. */
  .profile-targets {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    word-break: break-word;
  }

  .profile-targets.muted {
    font-style: italic;
  }

  .status-pill {
    font-size: var(--font-size-xs);
    padding: 1px 6px;
    /* §3 — text chip uses --radius-sm token, not full pill. */
    border-radius: var(--radius-sm);
    background: var(--bg-elevated-2);
  }

  .status-pill.off {
    color: var(--error);
  }

  .status-pill.default {
    color: var(--accent-primary);
    background: var(--accent-tint-bg);
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

  .entity-actions {
    display: flex;
    gap: var(--spacing-xs);
    flex-shrink: 0;
  }
</style>
