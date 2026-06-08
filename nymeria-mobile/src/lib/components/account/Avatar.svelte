<script lang="ts">
  import type { AccountIdentity } from '$lib/types';
  import { avatarBackground, avatarInitials, avatarTextColour } from './avatar';

  interface Props {
    identity: AccountIdentity | null | undefined;
    size?: number;
    state?: 'connected' | 'disabled' | 'unverified' | 'loading' | 'plain';
  }

  let { identity, size = 32, state = 'plain' }: Props = $props();

  let initials = $derived(avatarInitials(identity));
  let bg = $derived(avatarBackground(identity?.id));
  let fontSize = $derived(Math.max(10, Math.round(size * 0.4)));
</script>

<span
  class="avatar"
  class:state-disabled={state === 'disabled'}
  class:state-unverified={state === 'unverified'}
  class:state-loading={state === 'loading'}
  style="--avatar-size: {size}px; --avatar-bg: {bg}; --avatar-fg: {avatarTextColour()}; --avatar-font: {fontSize}px;"
>
  {#if state === 'loading'}
    <span class="skeleton"></span>
  {:else}
    <span class="initials">{initials}</span>
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
    background: var(--avatar-bg);
    color: var(--avatar-fg);
    font-size: var(--avatar-font);
    font-weight: 600;
    letter-spacing: 0.02em;
    flex-shrink: 0;
    user-select: none;
    position: relative;
    box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.2) inset;
  }

  .initials {
    line-height: 1;
  }

  .skeleton {
    display: block;
    width: 100%;
    height: 100%;
    border-radius: 50%;
    background: linear-gradient(
      90deg,
      var(--bg-elevated, rgba(255, 255, 255, 0.1)) 0%,
      var(--bg-hover, rgba(255, 255, 255, 0.18)) 50%,
      var(--bg-elevated, rgba(255, 255, 255, 0.1)) 100%
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
