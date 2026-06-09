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
       same family across AccountMenu, UsersTab, TriggerItem, etc. */
    padding: var(--spacing-2xs) var(--spacing-sm);
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
       letterforms at 9px already feel compact. */
    padding: 1px 6px;
    font-size: var(--font-size-3xs);
    letter-spacing: 0.06em;
    text-indent: 0.06em;
  }

  .role-admin {
    color: var(--warning);
    background: rgba(var(--warning-rgb), 0.12);
    border: 1px solid rgba(var(--warning-rgb), 0.4);
  }

  .role-user {
    color: var(--text-muted);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
  }
</style>
