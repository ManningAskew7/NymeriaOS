<script lang="ts">
  interface Recipe {
    name: string;
    source: string;
    description: string;
    kind: 'stdio' | 'registry';
  }

  interface Props {
    onUse: (source: string) => void;
    disabled?: boolean;
  }

  let { onUse, disabled = false }: Props = $props();

  const recipes: Recipe[] = [
    {
      name: 'Filesystem',
      source: 'npx -y @modelcontextprotocol/server-filesystem /workspace',
      description: 'Read/write files in /workspace. Paths must exist on the machine NymeriaOS runs on; edit the path before installing if you want a different folder.',
      kind: 'stdio',
    },
    {
      name: 'Fetch',
      source: 'uvx mcp-server-fetch',
      description: 'Let the agent fetch and read arbitrary URLs',
      kind: 'stdio',
    },
    {
      name: 'Sequential Thinking',
      source: 'npx -y @modelcontextprotocol/server-sequential-thinking',
      description: 'A structured "think step by step" workspace for the agent',
      kind: 'stdio',
    },
    {
      name: 'Memory (via registry)',
      source: 'io.github.modelcontextprotocol/server-memory',
      description: 'Persistent memory; installs from the official registry by ID',
      kind: 'registry',
    },
  ];
</script>

<div class="recipes">
  <div class="recipes-header">
    <span class="recipes-title">Try an example</span>
    <span class="recipes-subtitle">One click to prefill the paste box</span>
  </div>

  <div class="recipes-list">
    {#each recipes as recipe (recipe.name)}
      <button
        type="button"
        class="recipe-card"
        class:registry={recipe.kind === 'registry'}
        {disabled}
        onclick={() => onUse(recipe.source)}
      >
        <div class="recipe-main">
          <div class="recipe-name">{recipe.name}</div>
          <div class="recipe-description">{recipe.description}</div>
          <code class="recipe-source">{recipe.source}</code>
        </div>
        <div class="recipe-cta">
          <span class="kind-tag">{recipe.kind === 'registry' ? 'registry' : 'stdio'}</span>
          <span class="use-label">Use this →</span>
        </div>
      </button>
    {/each}
  </div>
</div>

<style>
  .recipes {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .recipes-header {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding-bottom: var(--spacing-xs);
  }

  .recipes-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: 0.02em;
  }

  .recipes-subtitle {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .recipes-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .recipe-card {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-left: 3px solid var(--accent-secondary);
    border-radius: var(--radius-md);
    text-align: left;
    cursor: pointer;
    transition: all var(--transition-fast);
    font: inherit;
    color: inherit;
  }

  .recipe-card:hover:not(:disabled) {
    background: var(--bg-hover);
    border-left-color: var(--accent-primary);
    transform: translateX(2px);
  }

  .recipe-card:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
  }

  .recipe-card:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .recipe-card.registry {
    border-left-color: var(--accent-primary);
  }

  .recipe-main {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .recipe-name {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .recipe-description {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.35;
  }

  .recipe-source {
    font-family: var(--font-mono);
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    background: var(--bg-base);
    padding: 4px var(--spacing-sm);
    border-radius: var(--radius-sm);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 100%;
  }

  .recipe-cta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm);
  }

  .kind-tag {
    font-family: var(--font-mono);
    font-size: var(--font-size-3xs);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    /* §1 text-indent compensation — matches the uppercase tracking so the
       label sits visually centered rather than left-weighted. */
    text-indent: 0.08em;
    color: var(--accent-secondary);
    background: rgba(var(--accent-primary-rgb), 0.1);
    padding: 2px var(--spacing-sm);
    /* §3 — text chips use --radius-sm, not full pill. Full pill is reserved
       for dot indicators and count badges; a label like STDIO / REGISTRY
       reads as a tag, not a pill. */
    border-radius: var(--radius-sm);
  }

  .recipe-card.registry .kind-tag {
    color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.18);
  }

  .use-label {
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--accent-primary);
    opacity: 0;
    transition: opacity var(--transition-fast);
  }

  .recipe-card:hover:not(:disabled) .use-label {
    opacity: 1;
  }
</style>
