<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import { profilePics } from '$lib/stores/profilePics.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Avatar from './Avatar.svelte';
  import RoleChip from './RoleChip.svelte';
  import TokenManagementSection from './TokenManagementSection.svelte';
  import PlatformLinkingSection from './PlatformLinkingSection.svelte';
  import { identityDisplayName } from './avatar';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';

  let identity = $derived(configStore.identity);
  let isAdmin = $derived(identity?.role === 'admin');

  // Display-name editor
  let editingName = $state(false);
  let nameDraft = $state('');
  let saving = $state(false);
  let nameError = $state<string | null>(null);
  let nameSaved = $state(false);

  // Profile picture upload
  let pictureInputRef = $state<HTMLInputElement | null>(null);
  let pictureError = $state<string | null>(null);
  let hasPicture = $derived(!!profilePics.get(identity?.id));
  const MAX_PICTURE_BYTES = 1_500_000; // ~1.5MB raw — keeps base64 under ~2MB

  function openPicturePicker() {
    pictureError = null;
    pictureInputRef?.click();
  }

  async function handlePictureChange(e: Event) {
    pictureError = null;
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file || !identity) return;

    if (!file.type.startsWith('image/')) {
      pictureError = 'Please pick an image file.';
      input.value = '';
      return;
    }
    if (file.size > MAX_PICTURE_BYTES) {
      pictureError = 'Image is too large. Pick one under 1.5MB.';
      input.value = '';
      return;
    }

    try {
      const dataUrl = await readFileAsDataUrl(file);
      profilePics.set(identity.id, dataUrl);
    } catch (err) {
      pictureError = err instanceof Error ? err.message : 'Could not load image.';
    } finally {
      input.value = ''; // allow re-selecting the same file
    }
  }

  function readFileAsDataUrl(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as string);
      reader.onerror = () => reject(new Error('Failed to read image.'));
      reader.readAsDataURL(file);
    });
  }

  function removePicture() {
    if (!identity) return;
    pictureError = null;
    profilePics.clear(identity.id);
  }

  function startEdit() {
    nameDraft = identity?.display_name ?? '';
    nameError = null;
    nameSaved = false;
    editingName = true;
  }

  function cancelEdit() {
    editingName = false;
    nameError = null;
  }

  async function saveName() {
    if (saving) return;
    nameError = null;
    saving = true;
    try {
      await configStore.updateIdentityDisplayName(nameDraft);
      editingName = false;
      nameSaved = true;
      // Auto-clear the saved indicator after a bit so it doesn't linger.
      setTimeout(() => (nameSaved = false), 2200);
    } catch (e) {
      nameError = humanizeErrorText(e, { action: 'save', resource: 'your name' });
    } finally {
      saving = false;
    }
  }

  function handleSignOut() {
    const onlySaved = connectionsStore.connections.length <= 1;
    if (onlySaved) {
      const ok = window.confirm(
        'This will sign you out and return to the setup wizard. Continue?'
      );
      if (!ok) return;
    }
    connectionsStore.clearActive();
    configStore.signOut();
  }
</script>

<div class="account-tab">
  {#if !identity}
    <div class="empty-state">
      <Icon name="user" size={32} />
      <p>Not connected to a NymeriaOS server.</p>
      <span>Switch to the Connection tab to set an API URL and token.</span>
    </div>
  {:else}
    <!-- Identity section -->
    <section class="section">
      <div class="section-header">
        <h3>Identity</h3>
        <RoleChip role={identity.role} />
      </div>

      <div class="identity-card">
        <div class="avatar-block">
          <button
            type="button"
            class="avatar-button"
            onclick={openPicturePicker}
            title="Click to change profile picture"
          >
            <Avatar {identity} size={72} state="connected" />
            <span class="avatar-overlay">
              <Icon name="edit" size={16} />
            </span>
          </button>
          <div class="avatar-actions">
            <button type="button" class="avatar-link" onclick={openPicturePicker}>
              {hasPicture ? 'Change' : 'Upload'}
            </button>
            {#if hasPicture}
              <button type="button" class="avatar-link danger" onclick={removePicture}>
                Remove
              </button>
            {/if}
          </div>
          {#if pictureError}
            <div class="field-error"><Icon name="error" size={12} />{pictureError}</div>
          {/if}
          <input
            bind:this={pictureInputRef}
            type="file"
            accept="image/*"
            onchange={handlePictureChange}
            class="hidden-file-input"
          />
        </div>
        <div class="identity-fields">
          <div class="field">
            <span class="field-label">Email</span>
            <div class="field-value readonly">{identity.email}</div>
          </div>

          <div class="field">
            <label class="field-label" for="display-name">Display name</label>
            {#if editingName}
              <div class="field-edit-row">
                <input
                  id="display-name"
                  class="field-input"
                  type="text"
                  bind:value={nameDraft}
                  disabled={saving}
                  maxlength="120"
                  placeholder="How should Nymeria refer to you?"
                />
                <Button size="sm" onclick={saveName} disabled={saving}>
                  {saving ? 'Saving…' : 'Save'}
                </Button>
                <Button variant="ghost" size="sm" onclick={cancelEdit} disabled={saving}>
                  Cancel
                </Button>
              </div>
            {:else}
              <div class="field-edit-row">
                <div class="field-value">{identity.display_name || '(none)'}</div>
                <Button variant="ghost" size="sm" onclick={startEdit}>
                  <Icon name="edit" size={14} />
                  Edit
                </Button>
                {#if nameSaved}
                  <span class="saved-pill">
                    <Icon name="check" size={12} />
                    Saved
                  </span>
                {/if}
              </div>
            {/if}
            {#if nameError}
              <div class="field-error"><Icon name="error" size={12} />{nameError}</div>
            {/if}
          </div>

          <div class="field">
            <span class="field-label">Account ID</span>
            <div class="field-value readonly mono">{identity.id}</div>
          </div>
        </div>
      </div>
    </section>

    <!-- API tokens -->
    <section class="section">
      <div class="section-header">
        <h3>API tokens</h3>
      </div>
      <p class="section-hint">
        Each token is a separate sign-in. Issue one for every device or
        script, then revoke them individually if anything goes missing.
      </p>
      <TokenManagementSection mode="self" />
    </section>

    {#if isAdmin}
      <!-- Linked platforms — admin-only because both GET and POST go through
           the /admin/users/{id}/platforms endpoints. -->
      <section class="section">
        <div class="section-header">
          <h3>Linked platforms</h3>
        </div>
        <p class="section-hint">
          Chat-platform IDs that map to this account. Bots route
          messages from these platform users back to this NymeriaOS identity.
        </p>
        <PlatformLinkingSection
          userId={identity.id}
          userLabel={identityDisplayName(identity)}
        />
      </section>
    {/if}

    <!-- Sign out (destructive) -->
    <section class="section danger-section">
      <div class="section-header">
        <h3>Sign out</h3>
      </div>
      <p class="section-hint">
        Returns to the setup wizard. Your saved connections are kept; only the
        current session is cleared.
      </p>
      <Button variant="ghost" onclick={handleSignOut}>
        <Icon name="x" size={14} />
        Sign out of {identity.display_name || identity.email}
      </Button>
    </section>
  {/if}
</div>

<style>
  .account-tab {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
    padding: var(--spacing-md) 0;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-xl);
    color: var(--text-muted);
    text-align: center;
  }
  .empty-state p {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-md);
  }
  .empty-state span {
    font-size: var(--font-size-sm);
  }

  .section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm);
    border-bottom: 1px solid var(--border-subtle);
    padding-bottom: var(--spacing-xs);
  }

  .section-header h3 {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: -0.005em;
  }

  .section-hint {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    line-height: 1.5;
  }

  .identity-card {
    display: flex;
    gap: var(--spacing-md);
    align-items: flex-start;
    padding: var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }

  .avatar-block {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
  }

  /* The avatar itself is the upload affordance — clicking opens the file
     picker. A subtle pencil overlay appears on hover so the action is
     discoverable without crowding the avatar in its resting state. */
  .avatar-button {
    position: relative;
    padding: 0;
    background: transparent;
    border: 0;
    border-radius: 50%;
    cursor: pointer;
    line-height: 0;
  }
  .avatar-button:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
  }
  .avatar-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(0, 0, 0, 0.5);
    color: #fff;
    border-radius: 50%;
    opacity: 0;
    transition: opacity var(--transition-fast);
    pointer-events: none;
  }
  .avatar-button:hover .avatar-overlay,
  .avatar-button:focus-visible .avatar-overlay {
    opacity: 1;
  }

  .avatar-actions {
    display: flex;
    gap: var(--spacing-sm);
  }
  .avatar-link {
    background: none;
    border: 0;
    padding: 0;
    color: var(--accent-primary);
    font-size: var(--font-size-xs);
    font-weight: 500;
    cursor: pointer;
  }
  .avatar-link:hover {
    text-decoration: underline;
  }
  .avatar-link.danger {
    color: var(--error);
  }

  .hidden-file-input {
    display: none;
  }

  .identity-fields {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .field-label {
    font-size: var(--font-size-2xs);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    font-weight: 600;
  }

  .field-value {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    word-break: break-all;
  }

  .field-value.readonly {
    color: var(--text-secondary);
  }

  .field-value.mono {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
  }

  .field-edit-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .field-input {
    flex: 1;
    padding: 6px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .field-input:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
  }

  .field-error {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-xs);
    color: var(--error);
  }

  .saved-pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: var(--font-size-2xs);
    color: var(--success);
    font-weight: 500;
  }

  .danger-section .section-header h3 {
    color: var(--error);
  }
</style>
