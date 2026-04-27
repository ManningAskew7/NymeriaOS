<script lang="ts">
  import { skillsStore } from '$lib/stores/skills.svelte';
  import type { SkillMetadata, SkillScope } from '$lib/types';
  import SkillsMarketplacePanel from './SkillsMarketplacePanel.svelte';

  let showMarketplace = $state(false);
  let expanded = $state<Record<string, boolean>>({});

  $effect(() => {
    skillsStore.loadInstalled();
    skillsStore.loadGlobal();
  });

  const scopeOrder: SkillScope[] = ['user', 'global', 'bundled'];
  const scopeLabel: Record<SkillScope, string> = {
    user: 'User-installed',
    global: 'Global (all users)',
    bundled: 'Bundled (ships with Nymeria)',
  };

  const grouped = $derived.by(() => {
    const out: Record<SkillScope, SkillMetadata[]> = { user: [], global: [], bundled: [] };
    for (const s of skillsStore.installed) out[s.scope].push(s);
    return out;
  });

  async function handleToggleGlobal(skill: SkillMetadata) {
    try {
      await skillsStore.toggleGlobal(skill.name);
    } catch (e) {
      console.error('toggle global skill failed', e);
    }
  }

  async function handleUninstall(skill: SkillMetadata) {
    if (skill.scope === 'bundled') return;
    if (!confirm(`Remove skill "${skill.name}" from disk? This cannot be undone.`)) return;
    try {
      await skillsStore.uninstall(skill.name, skill.scope as 'user' | 'global');
    } catch (e) {
      alert(`Uninstall failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  function toggleExpand(name: string) {
    expanded = { ...expanded, [name]: !expanded[name] };
  }
</script>

<div class="skills-panel">
  <div class="panel-header">
    <div>
      <h3 class="panel-title">Agent Skills</h3>
      <p class="panel-hint">
        Skills are bundles of procedural knowledge (SKILL.md + optional scripts).
        When enabled on a thread, the agent sees their name + description and
        loads the full body via a single <code>Skill(name)</code> tool call,
        with no context cost for skills the agent doesn't activate this turn.
      </p>
    </div>
    <button class="btn btn-primary" onclick={() => (showMarketplace = true)} type="button">
      Browse Marketplace
    </button>
  </div>

  {#if skillsStore.installedError}
    <div class="banner banner-error">{skillsStore.installedError}</div>
  {/if}

  {#if skillsStore.installedLoading && !skillsStore.installedLoaded}
    <p class="status">Loading skills…</p>
  {:else if skillsStore.installed.length === 0}
    <div class="empty">
      <p>No skills installed yet.</p>
      <p class="hint">
        Click <strong>Browse Marketplace</strong> to install skills from
        <code>anthropics/skills</code>. Start with <code>skill-creator</code>
        to let the agent help you write your own.
      </p>
    </div>
  {:else}
    {#each scopeOrder as scope}
      {#if grouped[scope].length > 0}
        <section class="scope-section">
          <h4 class="scope-heading">
            {scopeLabel[scope]}
            <span class="scope-count">({grouped[scope].length})</span>
          </h4>
          <div class="skills-list">
            {#each grouped[scope] as skill (skill.name)}
              {@const isGlobal = skillsStore.enabledGlobal.includes(skill.name)}
              {@const pendingState = skillsStore.isPending(skill.name)}
              <div class="skill-row" class:global-enabled={isGlobal}>
                <div class="skill-main">
                  <div class="skill-title-row">
                    <span class="skill-name">{skill.name}</span>
                    {#if skill.has_scripts}<span class="chip">scripts</span>{/if}
                    {#if skill.has_references}<span class="chip">references</span>{/if}
                    {#if skill.has_assets}<span class="chip">assets</span>{/if}
                    {#if skill.allowed_tools.length > 0}
                      <span class="chip chip-tools" title={skill.allowed_tools.join(', ')}>
                        {skill.allowed_tools.length} allowed-tool{skill.allowed_tools.length > 1 ? 's' : ''}
                      </span>
                    {/if}
                  </div>
                  <p class="skill-desc" class:collapsed={!expanded[skill.name]}>
                    {skill.description}
                  </p>
                  <button
                    class="expand-btn"
                    onclick={() => toggleExpand(skill.name)}
                    type="button"
                  >
                    {expanded[skill.name] ? 'Less' : 'More'}
                  </button>
                </div>
                <div class="skill-actions">
                  <label class="toggle-wrap" title="Enable this skill by default on every new thread">
                    <input
                      type="checkbox"
                      checked={isGlobal}
                      onchange={() => handleToggleGlobal(skill)}
                    />
                    <span class="toggle-text">Enable globally</span>
                  </label>
                  {#if skill.scope !== 'bundled'}
                    <button
                      class="btn btn-danger-ghost"
                      onclick={() => handleUninstall(skill)}
                      disabled={pendingState === 'uninstalling'}
                      type="button"
                    >
                      {pendingState === 'uninstalling' ? 'Removing…' : 'Remove'}
                    </button>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        </section>
      {/if}
    {/each}
  {/if}
</div>

{#if showMarketplace}
  <SkillsMarketplacePanel onClose={() => (showMarketplace = false)} />
{/if}

<style>
  .skills-panel {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--spacing-md);
  }

  .panel-title {
    margin: 0 0 4px 0;
    font-size: var(--font-size-base);
    font-weight: 600;
    color: var(--text-primary);
  }

  .panel-hint {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.5;
    max-width: 640px;
  }
  .panel-hint code {
    background: var(--bg-base);
    padding: 1px 4px;
    border-radius: var(--radius-sm);
    font-family: var(--font-mono, monospace);
    font-size: 0.9em;
  }

  .banner {
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
  }
  .banner-error {
    background: color-mix(in srgb, var(--danger, #ef4444) 10%, transparent);
    color: var(--danger, #ef4444);
  }

  .empty {
    text-align: center;
    padding: var(--spacing-xl, 24px);
    color: var(--text-secondary);
  }
  .empty .hint {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    max-width: 480px;
    margin: var(--spacing-sm) auto 0;
    line-height: 1.5;
  }
  .empty code {
    background: var(--bg-base);
    padding: 1px 4px;
    border-radius: var(--radius-sm);
    font-family: var(--font-mono, monospace);
  }

  .status {
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }

  .scope-section + .scope-section {
    margin-top: var(--spacing-md);
  }

  .scope-heading {
    margin: 0 0 var(--spacing-sm) 0;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .scope-count {
    font-weight: 400;
    color: var(--text-muted);
  }

  .skills-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .skill-row {
    display: flex;
    gap: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-base);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    align-items: flex-start;
  }
  .skill-row.global-enabled {
    border-color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 5%, var(--bg-base));
  }

  .skill-main {
    flex: 1;
    min-width: 0;
  }

  .skill-title-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
    margin-bottom: 4px;
  }

  .skill-name {
    font-weight: 600;
    color: var(--text-primary);
    font-family: var(--font-mono, monospace);
    font-size: var(--font-size-sm);
  }

  .chip {
    font-size: var(--font-size-xs);
    padding: 1px 6px;
    border-radius: var(--radius-full);
    background: var(--bg-elevated);
    color: var(--text-muted);
    border: 1px solid var(--border-subtle);
  }
  .chip-tools {
    cursor: help;
  }

  .skill-desc {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.45;
  }
  .skill-desc.collapsed {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .expand-btn {
    margin-top: 4px;
    background: transparent;
    border: none;
    color: var(--accent-primary);
    font-size: var(--font-size-xs);
    padding: 0;
    cursor: pointer;
  }
  .expand-btn:hover {
    text-decoration: underline;
  }

  .skill-actions {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    align-items: flex-end;
    flex-shrink: 0;
  }

  .toggle-wrap {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    cursor: pointer;
    user-select: none;
  }

  .btn {
    padding: 4px 10px;
    font-size: var(--font-size-xs);
    font-weight: 500;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
    border: 1px solid transparent;
  }
  .btn:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }

  .btn-primary {
    color: white;
    background: var(--accent-primary);
    border-color: var(--accent-primary);
    padding: 6px 14px;
    font-size: var(--font-size-sm);
  }
  .btn-primary:hover {
    filter: brightness(1.1);
  }

  .btn-danger-ghost {
    color: var(--danger, #ef4444);
    background: transparent;
    border-color: color-mix(in srgb, var(--danger, #ef4444) 30%, transparent);
  }
  .btn-danger-ghost:hover:not(:disabled) {
    background: color-mix(in srgb, var(--danger, #ef4444) 8%, transparent);
  }
</style>
