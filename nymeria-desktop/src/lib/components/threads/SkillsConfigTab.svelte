<script lang="ts">
  import { skillsStore } from '$lib/stores/skills.svelte';

  interface Props {
    threadEnabledSkills: Set<string>;
    threadDisabledSkills: Set<string>;
  }

  let {
    threadEnabledSkills = $bindable(),
    threadDisabledSkills = $bindable(),
  }: Props = $props();

  const resolvedActiveSkillNames = $derived.by(() => {
    const seen = new Set<string>();
    for (const n of skillsStore.enabledGlobal) {
      if (!threadDisabledSkills.has(n)) seen.add(n);
    }
    for (const n of threadEnabledSkills) {
      if (!threadDisabledSkills.has(n)) seen.add(n);
    }
    return seen;
  });

  function toggleThreadSkillEnabled(name: string) {
    const next = new Set(threadEnabledSkills);
    if (next.has(name)) next.delete(name);
    else {
      next.add(name);
      if (threadDisabledSkills.has(name)) {
        const d = new Set(threadDisabledSkills);
        d.delete(name);
        threadDisabledSkills = d;
      }
    }
    threadEnabledSkills = next;
  }

  function toggleThreadSkillDisabled(name: string) {
    const next = new Set(threadDisabledSkills);
    if (next.has(name)) next.delete(name);
    else {
      next.add(name);
      if (threadEnabledSkills.has(name)) {
        const e = new Set(threadEnabledSkills);
        e.delete(name);
        threadEnabledSkills = e;
      }
    }
    threadDisabledSkills = next;
  }
</script>

<div class="tab-panel skills-thread-panel">
  {#if skillsStore.installed.length === 0}
    <div class="skills-empty">
      <p style="margin: 0;">No skills installed yet.</p>
      <p class="field-hint" style="margin-top: 0.5rem;">
        Install skills from Settings → Skills → Browse Marketplace, then return here to enable them for this thread.
      </p>
    </div>
  {:else}
    <p class="field-hint">
      Turn skills on or off for this thread. Skill Kits are skills that bind required tools when activated. Globally-enabled entries are on by default and can be disabled here; other installed entries can be enabled for this thread only.
    </p>
    <div class="skills-list">
      {#each skillsStore.installed as skill (skill.name)}
        {@const defaultOn = skill.default_active}
        {@const globalOn = skillsStore.enabledGlobal.includes(skill.name)}
        {@const threadOn = threadEnabledSkills.has(skill.name)}
        {@const threadOff = threadDisabledSkills.has(skill.name)}
        {@const activeHere = (defaultOn || globalOn || threadOn) && !threadOff}
        <div class="skill-row" class:active={activeHere}>
          <div class="skill-info">
            <div class="skill-head">
              <span class="skill-name">{skill.name}</span>
              <span class="skill-scope">{skill.scope}</span>
              {#if defaultOn}<span class="skill-chip">default</span>{/if}
              {#if globalOn}<span class="skill-chip">global</span>{/if}
              {#if skill.is_skill_kit}<span class="skill-chip">Skill Kit</span>{/if}
              {#each skill.required_tools as toolName}
                <span class="skill-chip skill-required" title={`Required tool: ${toolName} (${skill.tool_ttl})`}>
                  {toolName}
                </span>
              {/each}
            </div>
            <p class="skill-desc">{skill.description}</p>
          </div>
          <div class="skill-toggles">
            {#if defaultOn || globalOn}
              <button
                class="skill-btn"
                class:skill-btn-danger={threadOff}
                onclick={() => toggleThreadSkillDisabled(skill.name)}
                type="button"
                title="Disable for this thread only"
              >
                {threadOff ? 'Disabled here' : 'Disable for thread'}
              </button>
            {:else}
              <button
                class="skill-btn"
                class:skill-btn-active={threadOn}
                onclick={() => toggleThreadSkillEnabled(skill.name)}
                type="button"
                title="Enable for this thread"
              >
                {threadOn ? 'Enabled here' : 'Enable for thread'}
              </button>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .tab-panel {
    padding: var(--spacing-lg);
  }

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin: 0 0 var(--spacing-sm) 0;
  }

  .skills-thread-panel .skills-empty {
    text-align: center;
    padding: var(--spacing-lg);
    color: var(--text-secondary);
  }

  .skills-thread-panel .skills-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-md);
  }

  .skills-thread-panel .skill-row {
    display: flex;
    gap: var(--spacing-md);
    padding: var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }
  .skills-thread-panel .skill-row.active {
    border-color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 6%, var(--bg-base));
  }

  .skills-thread-panel .skill-info {
    flex: 1;
    min-width: 0;
  }

  .skills-thread-panel .skill-head {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
    margin-bottom: 4px;
  }

  .skills-thread-panel .skill-name {
    font-weight: 600;
    color: var(--text-primary);
    font-family: var(--font-mono, monospace);
    font-size: var(--font-size-sm);
  }

  .skills-thread-panel .skill-scope,
  .skills-thread-panel .skill-chip {
    font-size: var(--font-size-xs);
    padding: 1px 6px;
    border-radius: var(--radius-full);
    background: var(--bg-elevated);
    color: var(--text-muted);
    border: 1px solid var(--border-subtle);
  }
  .skills-thread-panel .skill-chip {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
  }
  .skills-thread-panel .skill-required {
    color: var(--text-secondary);
    border-color: color-mix(in srgb, var(--accent-primary) 45%, var(--border-subtle));
  }

  .skills-thread-panel .skill-desc {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .skills-thread-panel .skill-toggles {
    flex-shrink: 0;
    display: flex;
    align-items: flex-start;
  }

  .skills-thread-panel .skill-btn {
    padding: 4px 10px;
    font-size: var(--font-size-xs);
    border-radius: var(--radius-sm);
    background: transparent;
    border: 1px solid var(--border-default);
    color: var(--text-secondary);
    cursor: pointer;
  }
  .skills-thread-panel .skill-btn:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }
  .skills-thread-panel .skill-btn-active {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
  }
  .skills-thread-panel .skill-btn-danger {
    color: var(--danger, #ef4444);
    border-color: color-mix(in srgb, var(--danger, #ef4444) 40%, transparent);
    background: color-mix(in srgb, var(--danger, #ef4444) 8%, transparent);
  }
</style>
