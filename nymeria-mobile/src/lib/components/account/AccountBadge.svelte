<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import Avatar from './Avatar.svelte';
  import AccountMenu from './AccountMenu.svelte';
  import { identityDisplayName } from './avatar';

  interface Props {
    onOpenSettings: (tab?: string) => void;
  }

  let { onOpenSettings }: Props = $props();

  let showMenu = $state(false);

  let identity = $derived(configStore.identity);
  let stillVerifying = $derived(!!configStore.isConfigured && identity === null);

  let avatarState = $derived<'connected' | 'disabled' | 'unverified' | 'loading' | 'plain'>(
    stillVerifying
      ? 'loading'
      : identity == null
      ? 'unverified'
      : 'connected'
  );

  function openMenu() {
    showMenu = true;
  }

  function closeMenu() {
    showMenu = false;
  }
</script>

<button
  class="account-btn"
  type="button"
  onclick={openMenu}
  aria-haspopup="dialog"
  aria-label="Account: {identityDisplayName(identity)}"
>
  <Avatar {identity} size={28} state={avatarState} />
</button>

<AccountMenu isOpen={showMenu} onClose={closeMenu} {onOpenSettings} />

<style>
  .account-btn {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    transition: all var(--transition-fast);
    background: transparent;
    padding: 0;
  }

  .account-btn:active {
    background: var(--bg-hover);
  }
</style>
