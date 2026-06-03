<script lang="ts">
  import type { UserRole } from '$lib/types';

  interface Props {
    role: UserRole | null | undefined;
    size?: 'sm' | 'xs';
  }

  let { role, size = 'sm' }: Props = $props();
</script>

{#if role}
  <span
    class="role-chip"
    class:role-admin={role === 'admin'}
    class:role-user={role === 'user'}
    class:size-xs={size === 'xs'}
  >
    {role}
  </span>
{/if}

<style>
  .role-chip {
    display: inline-flex;
    align-items: center;
    /* Canonical chip padding — every status/role badge in Nymeria should
       use this pair (--spacing-2xs / --spacing-sm) so chips read as the
       same family across AccountMenu, UsersTab, TriggerItem, etc.
       Asymmetric vertical: -1 top / +1 bottom shifts the text up by 1px
       within the chip without changing total chip height. */
    padding: 1px var(--spacing-sm) 3px;
    font-size: var(--font-size-3xs);
    font-weight: 700;
    letter-spacing: 0.08em;
    /* Compensates for trailing letter-spacing on uppercase text — the
       0.08em added after the last letter would otherwise make the chip
       look left-weighted. Matching text-indent restores visual centering. */
    text-indent: 0.08em;
    text-transform: uppercase;
    border-radius: var(--radius-sm);
    line-height: 1.4;
    flex-shrink: 0;
  }

  .role-chip.size-xs {
    /* Tighter than the canonical chip — used inline beside small text. The
       horizontal 6px is sub-token (under --spacing-sm 8px) because the
       letterforms at 9px already feel compact.
       Asymmetric vertical: -1 top / +1 bottom shifts the text up by 1px
       within the chip without changing total chip height. */
    padding: 0 6px 2px;
    font-size: var(--font-size-3xs);
    letter-spacing: 0.06em;
    text-indent: 0.06em;
  }

  .role-admin {
    color: var(--warning, #fbbf24);
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(251, 191, 36, 0.4);
  }

  .role-user {
    color: var(--text-muted);
    background: var(--bg-elevated, rgba(255, 255, 255, 0.04));
    border: 1px solid var(--border-subtle);
  }
</style>
