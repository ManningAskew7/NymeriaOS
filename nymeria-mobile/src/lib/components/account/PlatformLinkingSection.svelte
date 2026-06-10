<script lang="ts">
  import { untrack } from 'svelte';
  import type { ChatAppProvider, PlatformIdentity } from '$lib/types';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';

  interface Props {
    userId: string;
    userLabel?: string;
    canEdit?: boolean;
  }

  let { userId, userLabel, canEdit = true }: Props = $props();

  type Provider = ChatAppProvider;
  const PROVIDERS: { value: Provider; label: string }[] = [
    { value: 'discord', label: 'Discord' },
    { value: 'telegram', label: 'Telegram' },
    { value: 'slack', label: 'Slack' },
    { value: 'matrix', label: 'Matrix' },
    { value: 'whatsapp', label: 'WhatsApp' },
    { value: 'messenger', label: 'Messenger' },
    { value: 'instagram', label: 'Instagram' },
    { value: 'webex', label: 'Webex' },
    { value: 'mattermost', label: 'Mattermost' },
    { value: 'zulip', label: 'Zulip' },
    { value: 'rocketchat', label: 'Rocket.Chat' },
    { value: 'teams', label: 'Microsoft Teams' },
    { value: 'googlechat', label: 'Google Chat' },
    { value: 'line', label: 'LINE' },
    { value: 'signal', label: 'Signal' },
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
      loadError = humanizeErrorText(e, { action: 'load', resource: 'platform links' });
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
      linkError = humanizeErrorText(e, { action: 'connect', resource: 'the platform' });
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
      `Unlink ${p.provider} (${p.provider_user_id}) from ${subject}?`
    );
    if (!ok) return;
    unlinkingKey = key;
    try {
      await api.unlinkUserPlatform(userId, p.provider, p.provider_user_id);
      await load();
    } catch (e) {
      loadError = humanizeErrorText(e, { action: 'delete', resource: 'the platform link' });
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
    if (p === 'slack') return '#E01E5A';
    if (p === 'matrix') return '#0DBD8B';
    if (p === 'whatsapp') return '#25D366';
    if (p === 'messenger') return '#0084FF';
    if (p === 'instagram') return '#E4405F';
    if (p === 'webex') return '#00BCEB';
    if (p === 'mattermost') return '#0058CC';
    if (p === 'zulip') return '#6492FE';
    if (p === 'rocketchat') return '#F5455C';
    if (p === 'teams') return '#6264A7';
    if (p === 'googlechat') return '#1A73E8';
    if (p === 'line') return '#06C755';
    if (p === 'signal') return '#3A76F0';
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
      <span>No platform identities linked yet.</span>
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
                <Icon name="loading" size={16} />
              {:else}
                <Icon name="trash" size={16} />
              {/if}
            </button>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}

  {#if canEdit}
    <div class="link-form">
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
      />
      <Button onclick={handleLink} disabled={linking || !linkProviderUserId.trim()}>
        {linking ? 'Linking…' : 'Link platform'}
      </Button>
      {#if linkError}
        <div class="form-error">
          <Icon name="error" size={14} />
          <span>{linkError}</span>
        </div>
      {/if}
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
    padding: 10px var(--spacing-sm);
    background: rgba(var(--error-rgb), 0.08);
    border: 1px solid rgba(var(--error-rgb), 0.3);
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
    padding: 4px 10px;
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
    padding: 12px var(--spacing-sm);
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
    font-family: var(--font-mono);
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
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    color: var(--text-muted);
    background: transparent;
    border-radius: var(--radius-sm);
  }

  .row-unlink:active {
    color: var(--error);
    background: rgba(var(--error-rgb), 0.1);
  }

  .row-unlink:disabled {
    opacity: 0.5;
  }

  .link-form {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-sm);
  }

  .provider-select,
  .provider-input {
    padding: 10px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-md);
  }

  .provider-input {
    font-family: var(--font-mono);
  }

  .form-error {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--error);
    font-size: 12px;
  }
</style>
