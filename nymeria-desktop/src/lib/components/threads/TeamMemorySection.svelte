<script lang="ts">
  // Read-only "what the agent sees" view of the thread's shared team memory
  // (backlog #100 phase 3). Self-fetching; editing lives in the sidebar team
  // settings modal (right-click the team header). Desktop-only.
  import ThreadSettingsSection from './ThreadSettingsSection.svelte';
  import InlineLoader from '../common/InlineLoader.svelte';
  import { api } from '$lib/services/api.svelte';
  import type { TeamMemoryEntryApi } from '$lib/types';

  interface Props {
    teamId: string;
    teamName?: string | null;
  }

  let { teamId, teamName = null }: Props = $props();

  let entries = $state<TeamMemoryEntryApi[]>([]);
  let loading = $state(true);
  let loadFailed = $state(false);

  // Keyed on teamId (not onMount) so a team change under a mounted tab
  // refetches instead of showing the previous team's entries.
  $effect(() => {
    const id = teamId;
    loading = true;
    loadFailed = false;
    void (async () => {
      try {
        const rows = await api.listTeamMemories(id);
        if (id === teamId) entries = rows;
      } catch {
        if (id === teamId) loadFailed = true;
      } finally {
        if (id === teamId) loading = false;
      }
    })();
  });
</script>

<ThreadSettingsSection
  title={teamName ? `Team memory (${teamName})` : 'Team memory'}
  description="Shared key/value facts every thread in this team reads at session start. Read-only here; edit them from the team's header in the sidebar (right-click, Team settings), or let the agents share facts with their memory tools."
>
  {#if loading}
    <p class="field-hint"><InlineLoader text="Loading team memory…" /></p>
  {:else if loadFailed}
    <p class="field-hint">Could not load the team memory.</p>
  {:else if entries.length === 0}
    <p class="field-hint">No shared entries yet.</p>
  {:else}
    <ul class="team-memory-list">
      {#each entries as entry (entry.key)}
        <li class="team-memory-row">
          <span class="row-key">{entry.key}</span>
          <span class="row-value">{entry.value}</span>
        </li>
      {/each}
    </ul>
  {/if}
</ThreadSettingsSection>

<style>
  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.45;
    margin: 0;
  }

  .team-memory-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .team-memory-row {
    display: flex;
    gap: var(--spacing-sm);
    align-items: baseline;
    padding: 6px var(--spacing-sm);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated-1, var(--bg-base));
    font-size: var(--font-size-sm);
  }

  .row-key {
    flex: 0 0 auto;
    max-width: 40%;
    font-family: var(--font-mono);
    font-weight: 600;
    color: var(--accent-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-value {
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }
</style>
