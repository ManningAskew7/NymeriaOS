<script lang="ts">
  import { onMount } from 'svelte';
  import { skillsStore } from '$lib/stores/skills.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import type { SkillMetadata, SkillScope } from '$lib/types';
  import SkillsMarketplacePanel from './SkillsMarketplacePanel.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import InlineLoader from '$lib/components/common/InlineLoader.svelte';

  let showMarketplace = $state(false);
  let expanded = $state<Record<string, boolean>>({});
  let actionError = $state<string | null>(null);

  onMount(() => {
    void skillsStore.refreshInstalled();
    void skillsStore.refreshGlobal();
  });

  const scopeOrder: SkillScope[] = ['user', 'global', 'bundled'];
  type SkillKind = 'kits' | 'skills';
  const skillKindOrder: SkillKind[] = ['kits', 'skills'];
  const scopeLabel: Record<SkillScope, string> = {
    user: 'User-installed',
    global: 'Global (all users)',
    bundled: 'Bundled (ships with NymeriaOS)',
  };
  const skillKindLabel: Record<SkillKind, string> = {
    kits: 'Skill Kits',
    skills: 'Skills',
  };

  const grouped = $derived.by(() => {
    const out: Record<SkillScope, SkillMetadata[]> = { user: [], global: [], bundled: [] };
    for (const s of skillsStore.installed) out[s.scope].push(s);
    return out;
  });
  const groupedByKind = $derived.by(() => {
    const out: Record<SkillScope, Record<SkillKind, SkillMetadata[]>> = {
      user: { kits: [], skills: [] },
      global: { kits: [], skills: [] },
      bundled: { kits: [], skills: [] },
    };
    for (const s of skillsStore.installed) {
      out[s.scope][s.is_skill_kit ? 'kits' : 'skills'].push(s);
    }
    return out;
  });

  async function handleToggleGlobal(skill: SkillMetadata) {
    const enabling = !skillsStore.enabledGlobal.includes(skill.name);
    actionError = null;
    try {
      await skillsStore.toggleGlobal(skill.name);
    } catch (e) {
      actionError = humanizeErrorText(e, {
        action: enabling ? 'enable' : 'disable',
        resource: `the "${skill.name}" skill`,
      });
      console.error('toggle global skill failed', e);
    }
  }

  async function handleUninstall(skill: SkillMetadata) {
    if (skill.scope === 'bundled') return;
    if (!confirm(`Remove skill "${skill.name}" from disk? This cannot be undone.`)) return;
    actionError = null;
    try {
      await skillsStore.uninstall(skill.name, skill.scope as 'user' | 'global');
    } catch (e) {
      actionError = humanizeErrorText(e, { action: 'remove', resource: `the "${skill.name}" skill` });
    }
  }

  function toggleExpand(name: string) {
    expanded = { ...expanded, [name]: !expanded[name] };
  }
</script>

<div class="skills-panel">
  <div class="panel-header">
    <div>
      <h3 class="panel-title">Skills</h3>
      <p class="panel-hint">
        Skills are bundles of procedural knowledge (SKILL.md + optional scripts).
        Skill Kits are skills that also bind required Nymeria tools when activated.
        Both stay hidden until the agent calls <code>Skill(name)</code>.
      </p>
    </div>
    <Button variant="primary" onclick={() => (showMarketplace = true)}>
      Browse Marketplace
    </Button>
  </div>

  {#if skillsStore.installedError}
    <div class="banner banner-error">{skillsStore.installedError}</div>
  {/if}

  {#if skillsStore.enabledGlobalError}
    <div class="banner banner-error">{skillsStore.enabledGlobalError}</div>
  {/if}

  {#if actionError}
    <div class="banner banner-error">{actionError}</div>
  {/if}

  {#if skillsStore.installedLoading && !skillsStore.installedLoaded}
    <p class="status"><InlineLoader text="Loading skills…" /></p>
  {:else if skillsStore.installed.length === 0}
    <div class="empty-state">
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
          {#each skillKindOrder as kind}
            {@const kindSkills = groupedByKind[scope][kind]}
            {#if kindSkills.length > 0}
              <div class="kind-section">
                <h5 class="kind-heading">
                  {skillKindLabel[kind]}
                  <span class="scope-count">({kindSkills.length})</span>
                </h5>
                <div class="skills-list">
                  {#each kindSkills as skill (skill.name)}
                    {@const isGlobal = skillsStore.enabledGlobal.includes(skill.name)}
                    {@const pendingState = skillsStore.isPending(skill.name)}
                    <div class="skill-row" class:global-enabled={isGlobal} class:skill-kit-row={skill.is_skill_kit}>
                      <div class="skill-main">
                        <div class="skill-title-row">
                          <span class="skill-name">{skill.name}</span>
                          {#if skill.default_active}<span class="chip chip-default">default</span>{/if}
                          {#if skill.is_skill_kit}<span class="chip chip-kit">Skill Kit</span>{/if}
                          {#if skill.has_scripts}<span class="chip chip-cap"><Icon name="terminal" size={10} /><span>scripts</span></span>{/if}
                          {#if skill.has_references}<span class="chip chip-cap"><Icon name="fileText" size={10} /><span>references</span></span>{/if}
                          {#if skill.has_assets}<span class="chip chip-cap"><Icon name="folder" size={10} /><span>assets</span></span>{/if}
                          {#each skill.required_tools as toolName}
                            <span class="chip chip-required" title={`Required tool: ${toolName} (${skill.tool_ttl})`}>
                              {toolName}
                            </span>
                          {/each}
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
                        <label
                          class="toggle-wrap"
                          title="Enable this skill by default on every new thread"
                        >
                          <input
                            type="checkbox"
                            checked={isGlobal}
                            onchange={() => handleToggleGlobal(skill)}
                          />
                          <span class="toggle-text">Enable globally</span>
                        </label>
                        {#if skill.scope !== 'bundled'}
                          <button
                            class="btn-danger-ghost"
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
              </div>
            {/if}
          {/each}
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
    font-size: var(--font-size-lg);
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
    font-family: var(--font-mono);
    font-size: 0.9em;
  }

  .banner {
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
  }
  .banner-error {
    background: color-mix(in srgb, var(--error) 10%, transparent);
    color: var(--error);
  }

  .empty-state {
    text-align: center;
    padding: var(--spacing-xl);
    color: var(--text-secondary);
  }
  .empty-state .hint {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    max-width: 480px;
    margin: var(--spacing-sm) auto 0;
    line-height: 1.5;
  }
  .empty-state code {
    background: var(--bg-base);
    padding: 1px 4px;
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
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

  .kind-section + .kind-section {
    margin-top: var(--spacing-sm);
  }

  .kind-heading {
    margin: 0 0 6px 0;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-secondary);
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
  .skill-row.skill-kit-row {
    border-left: 3px solid color-mix(in srgb, var(--accent-primary) 70%, var(--border-subtle));
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
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
  }

  .chip {
    font-size: var(--font-size-xs);
    padding: 1px 6px;
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-muted);
    border: 1px solid var(--border-subtle);
    /* Optical centering: lowercase text in a pill-shaped chip reads as
       left-shifted (lowercase glyph weight bottom-left + pill curves
       absorbing right padding). A small positive text-indent nudges the
       text into the optical center without changing chip size or rounding. */
    text-indent: 1px;
  }
  /* Capability chips (scripts / references / assets) carry a small leading
     Lucide icon so the three otherwise-identical pills read as distinct
     categories at a glance — matches §3 differentiation guidance and the
     colored-dot pattern already used by TriggerConfigTab.meta-tag. */
  .chip-cap {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
  }
  .chip-cap :global(svg) {
    color: var(--text-secondary);
    flex-shrink: 0;
  }
  .chip-tools {
    cursor: help;
  }
  .chip-kit {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 8%, var(--bg-elevated));
  }
  .chip-default {
    color: var(--success);
    border-color: color-mix(in srgb, var(--success) 55%, var(--border-subtle));
    background: color-mix(in srgb, var(--success) 8%, var(--bg-elevated));
  }
  .chip-required {
    color: var(--text-secondary);
    border-color: color-mix(in srgb, var(--accent-primary) 45%, var(--border-subtle));
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

  /* The one footer action the shared <Button> can't express: a danger-ghost
     variant. Anatomy mirrors the component's .btn/.btn-sm; the error border
     is this variant's identity. */
  .btn-danger-ghost {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border: 1px solid color-mix(in srgb, var(--error) 30%, transparent);
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--error);
    cursor: pointer;
    transition: all var(--transition-fast);
    white-space: nowrap;
  }
  .btn-danger-ghost:hover:not(:disabled) {
    background: color-mix(in srgb, var(--error) 8%, transparent);
  }
  .btn-danger-ghost:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .btn-danger-ghost:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
  }
</style>
