<script lang="ts">
  import { untrack } from 'svelte';
  import type { PlatformIdentity } from '$lib/types';
  import { api } from '$lib/services/api.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';

  interface Props {
    /**
     * The user whose platform identities are managed. Linking is admin-only on
     * the backend (POST /admin/users/{id}/platforms), so non-admin self use of
     * this component will succeed at GET but error on POST. The Account tab
     * therefore only renders this section for admins.
     */
    userId: string;
    userLabel?: string;
    /**
     * Whether the caller is allowed to add/remove links. False renders the
     * list read-only — useful if we ever want to surface the linked platforms
     * to non-admin self users without offering edit affordances.
     */
    canEdit?: boolean;
  }

  let { userId, userLabel, canEdit = true }: Props = $props();

  type Provider = 'discord' | 'telegram' | 'twitch';
  const PROVIDERS: { value: Provider; label: string }[] = [
    { value: 'discord', label: 'Discord' },
    { value: 'telegram', label: 'Telegram' },
    { value: 'twitch', label: 'Twitch' },
  ];

  let identities = $state<PlatformIdentity[]>([]);
  let loading = $state(false);
  let loadError = $state<string | null>(null);

  let linking = $state(false);
  let linkProvider = $state<Provider>('discord');
  let linkProviderUserId = $state('');
  let linkError = $state<string | null>(null);

  let unlinkingKey = $state<string | null>(null);

  async function load() {
    if (loading) return;
    loading = true;
    loadError = null;
    try {
      identities = await api.listUserPlatforms(userId);
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to load platform links';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void userId;
    untrack(() => {
      void load();
    });
  });

  async function handleLink() {
    if (linking) return;
    const trimmed = linkProviderUserId.trim();
    if (!trimmed) {
      linkError = 'Provider user ID is required';
      return;
    }
    linking = true;
    linkError = null;
    try {
      await api.linkUserPlatform(userId, {
        provider: linkProvider,
        provider_user_id: trimmed,
      });
      linkProviderUserId = '';
      await load();
    } catch (e) {
      linkError = e instanceof Error ? e.message : 'Failed to link platform';
    } finally {
      linking = false;
    }
  }

  function rowKey(p: PlatformIdentity): string {
    return `${p.provider}:${p.provider_user_id}`;
  }

  async function handleUnlink(p: PlatformIdentity) {
    const key = rowKey(p);
    if (unlinkingKey === key) return;
    const subject = userLabel || 'this user';
    const ok = window.confirm(
      `Unlink ${p.provider} (${p.provider_user_id}) from ${subject}? Bot messages from that account will stop routing to this user until re-linked.`
    );
    if (!ok) return;
    unlinkingKey = key;
    try {
      await api.unlinkUserPlatform(userId, p.provider, p.provider_user_id);
      await load();
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to unlink';
    } finally {
      unlinkingKey = null;
    }
  }

  function providerLabel(p: Provider): string {
    return PROVIDERS.find((x) => x.value === p)?.label ?? p;
  }

  function providerColour(p: Provider): string {
    if (p === 'discord') return '#5865F2';
    if (p === 'telegram') return '#229ED9';
    if (p === 'twitch') return '#9146FF';
    return 'var(--text-muted)';
  }
</script>

<div class="platform-section">
  {#if loadError}
    <div class="error-banner">
      <Icon name="error" size={14} />
      <span>{loadError}</span>
      <button class="banner-action" type="button" onclick={load}>Retry</button>
    </div>
  {/if}

  {#if loading && identities.length === 0}
    <div class="empty-state">
      <Icon name="loading" size={20} />
      <span>Loading platforms…</span>
    </div>
  {:else if identities.length === 0}
    <div class="empty-state">
      <Icon name="info" size={20} />
      <span>
        No platform identities linked. Link Discord/Telegram/Twitch IDs so the
        bots can route those platform users' messages to this account.
      </span>
    </div>
  {:else}
    <ul class="platform-list">
      {#each identities as identity (rowKey(identity))}
        {@const key = rowKey(identity)}
        {@const isUnlinking = unlinkingKey === key}
        <li class="platform-row">
          <span class="provider-badge" style="--provider-color: {providerColour(identity.provider)};">
            {providerLabel(identity.provider)}
          </span>
          <code class="provider-id">{identity.provider_user_id}</code>
          {#if canEdit}
            <button
              class="row-unlink"
              type="button"
              onclick={() => handleUnlink(identity)}
              disabled={isUnlinking}
              aria-label="Unlink {identity.provider} {identity.provider_user_id}"
            >
              {#if isUnlinking}
                <Icon name="loading" size={14} />
              {:else}
                <Icon name="trash" size={14} />
              {/if}
            </button>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}

  {#if canEdit}
    <div class="link-form">
      <div class="link-row">
        <select
          class="provider-select"
          bind:value={linkProvider}
          disabled={linking}
        >
          {#each PROVIDERS as p}
            <option value={p.value}>{p.label}</option>
          {/each}
        </select>
        <input
          class="provider-input"
          type="text"
          bind:value={linkProviderUserId}
          placeholder="Provider user ID"
          disabled={linking}
          autocomplete="off"
          onkeydown={(e) => {
            if (e.key === 'Enter') handleLink();
          }}
        />
        <Button size="sm" onclick={handleLink} disabled={linking || !linkProviderUserId.trim()}>
          {linking ? 'Linking…' : 'Link'}
        </Button>
      </div>
      {#if linkError}
        <div class="form-error">
          <Icon name="error" size={14} />
          <span>{linkError}</span>
        </div>
      {/if}
      <p class="form-hint">
        Provider user IDs are the platform's own snowflake/numeric ID — Discord
        right-click → Copy User ID, Telegram via <code>/start</code> with a bot,
        Twitch via the Helix API.
      </p>
    </div>
  {/if}
</div>

<style>
  .platform-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .error-banner {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px var(--spacing-sm);
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.3);
    border-radius: var(--radius-sm);
    color: var(--error);
    font-size: var(--font-size-sm);
  }

  .banner-action {
    margin-left: auto;
    color: var(--error);
    background: transparent;
    border: 1px solid currentColor;
    border-radius: var(--radius-sm);
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
  }

  .empty-state {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: var(--spacing-md);
    color: var(--text-muted);
    background: var(--bg-elevated);
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .platform-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .platform-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 8px var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
  }

  .provider-badge {
    flex-shrink: 0;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 2px 8px;
    border-radius: 4px;
    color: var(--provider-color);
    border: 1px solid var(--provider-color);
    background: color-mix(in srgb, var(--provider-color) 12%, transparent);
  }

  .provider-id {
    flex: 1;
    min-width: 0;
    font-family: var(--font-mono, ui-monospace, 'SF Mono', monospace);
    font-size: 12px;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-unlink {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    color: var(--text-muted);
    background: transparent;
    border-radius: var(--radius-sm);
    transition: all var(--transition-fast);
  }

  .row-unlink:hover:not(:disabled) {
    color: var(--error);
    background: rgba(239, 68, 68, 0.1);
  }

  .row-unlink:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .link-form {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-sm);
  }

  .link-row {
    display: flex;
    gap: 6px;
    align-items: center;
  }

  .provider-select,
  .provider-input {
    padding: 6px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .provider-input {
    flex: 1;
    min-width: 0;
    font-family: var(--font-mono, ui-monospace, 'SF Mono', monospace);
  }

  .provider-input:focus,
  .provider-select:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(34, 211, 238, 0.15));
  }

  .form-error {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--error);
    font-size: 12px;
  }

  .form-hint {
    margin: 0;
    font-size: 11px;
    color: var(--text-muted);
    line-height: 1.5;
  }

  .form-hint code {
    font-family: var(--font-mono, ui-monospace, 'SF Mono', monospace);
    background: var(--bg-base);
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 11px;
  }
</style>
