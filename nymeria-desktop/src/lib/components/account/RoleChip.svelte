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
    padding: 2px 7px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    border-radius: 4px;
    line-height: 1.4;
    flex-shrink: 0;
  }

  .role-chip.size-xs {
    padding: 1px 5px;
    font-size: 9px;
    letter-spacing: 0.06em;
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
