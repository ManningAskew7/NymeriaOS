<script lang="ts">
  import type { AccountIdentity } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';
  import { profilePics } from '$lib/stores/profilePics.svelte';

  interface Props {
    identity: AccountIdentity | null | undefined;
    size?: number;
    /** Outline state — surfaces account health on the avatar itself. */
    state?: 'connected' | 'disabled' | 'unverified' | 'loading' | 'plain';
  }

  let { identity, size = 32, state = 'plain' }: Props = $props();

  // Default avatar is a Lucide `user` glyph on the theme's bg-base with
  // the accent colour as the icon stroke. Icon scales with the circle
  // (≈ 56% reads well at any size). If the user has uploaded a profile
  // picture (per-account, stored client-side in profilePics), it replaces
  // the icon — the bg circle stays the same so the chrome reads the same.
  let iconSize = $derived(Math.max(12, Math.round(size * 0.56)));
  let pictureUrl = $derived(profilePics.get(identity?.id));
</script>

<span
  class="avatar"
  class:state-disabled={state === 'disabled'}
  class:state-unverified={state === 'unverified'}
  class:state-loading={state === 'loading'}
  style="--avatar-size: {size}px;"
  title={identity ? `${identity.display_name || identity.email} (${identity.role})` : 'No identity'}
>
  {#if state === 'loading'}
    <span class="skeleton"></span>
  {:else if pictureUrl}
    <img class="picture" src={pictureUrl} alt={identity?.display_name || identity?.email || 'Account avatar'} />
  {:else}
    <span class="account-icon">
      <Icon name="user" size={iconSize} />
    </span>
  {/if}
</span>

<style>
  .avatar {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: var(--avatar-size);
    height: var(--avatar-size);
    border-radius: 50%;
    /* Background matches the theme base so the avatar reads as a recessed
       circle in the surrounding chrome; the icon itself carries the accent
       colour. Both swap automatically when the theme changes. */
    background: var(--bg-base);
    flex-shrink: 0;
    user-select: none;
    position: relative;
    box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.15) inset;
    transition: box-shadow var(--transition-fast), background var(--transition-fast);
  }

  .account-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--accent-primary);
  }

  .picture {
    width: 100%;
    height: 100%;
    object-fit: cover;
    border-radius: 50%;
    display: block;
  }

  .skeleton {
    display: block;
    width: 100%;
    height: 100%;
    border-radius: 50%;
    background: linear-gradient(
      90deg,
      var(--bg-elevated) 0%,
      var(--bg-hover) 50%,
      var(--bg-elevated) 100%
    );
    background-size: 200% 100%;
    animation: avatar-shimmer 1.4s ease-in-out infinite;
  }

  .avatar.state-disabled {
    box-shadow: 0 0 0 2px var(--error);
    opacity: 0.85;
  }

  .avatar.state-unverified {
    box-shadow: 0 0 0 1px var(--text-muted);
    filter: grayscale(0.4);
  }

  @keyframes avatar-shimmer {
    0%, 100% { background-position: 0% 0%; }
    50% { background-position: -200% 0%; }
  }
</style>
