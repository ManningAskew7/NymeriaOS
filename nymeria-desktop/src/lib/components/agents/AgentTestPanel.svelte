<script lang="ts">
  import type { SubAgent, SubAgentTestResponse } from '$lib/types';
  import { agentsStore } from '$lib/stores/agents.svelte';
  import Button from '../common/Button.svelte';

  interface Props {
    agent: SubAgent;
    onClose: () => void;
  }

  let { agent, onClose }: Props = $props();

  // Test state
  let instruction = $state('');
  let result = $state<SubAgentTestResponse | null>(null);
  let testing = $state(false);
  let error = $state<string | null>(null);

  async function runTest() {
    if (!instruction.trim()) return;

    testing = true;
    error = null;
    result = null;

    try {
      result = await agentsStore.testAgent(agent.name, instruction);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Test failed';
    } finally {
      testing = false;
    }
  }

  function formatDuration(ms: number): string {
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
  }
</script>

<div class="test-panel">
  <div class="agent-info">
    <div class="agent-header">
      <span class="agent-name">{agent.name}</span>
      <span class="agent-tools">{agent.tools.length} tools</span>
    </div>
    <p class="agent-description">{agent.description}</p>
  </div>

  <div class="prompt-preview">
    <h4>System Prompt</h4>
    <pre>{agent.systemPrompt}</pre>
  </div>

  <div class="test-section">
    <h4>Test Instruction</h4>
    <div class="field">
      <textarea
        bind:value={instruction}
        placeholder="Enter an instruction to test the agent..."
        rows="4"
      ></textarea>
      <p class="hint">This instruction will be sent to the agent for processing</p>
    </div>
  </div>

  <div class="actions">
    <Button
      variant="primary"
      onclick={runTest}
      disabled={testing || !instruction.trim()}
    >
      {testing ? 'Running...' : 'Run Test'}
    </Button>
  </div>

  {#if error}
    <div class="result-section error">
      <h4>Error</h4>
      <pre>{error}</pre>
    </div>
  {/if}

  {#if result}
    <div class="result-section" class:success={result.success} class:failure={!result.success}>
      <div class="result-header">
        <h4>Result</h4>
        <div class="result-meta">
          <span class="status" class:success={result.success}>
            {result.success ? 'Success' : 'Failed'}
          </span>
          <span class="duration">{formatDuration(result.executionTimeMs)}</span>
        </div>
      </div>

      {#if result.error}
        <div class="result-error">
          <strong>Error:</strong> {result.error}
        </div>
      {/if}

      <div class="result-output">
        <strong>Response:</strong>
        <pre>{result.response}</pre>
      </div>

      {#if result.toolsUsed && result.toolsUsed.length > 0}
        <div class="tools-used">
          <strong>Tools Used:</strong>
          <div class="tool-tags">
            {#each result.toolsUsed as tool}
              <span class="tool-tag">{tool}</span>
            {/each}
          </div>
        </div>
      {/if}
    </div>
  {/if}

  <div class="footer-actions">
    <Button variant="secondary" onclick={onClose}>
      Close
    </Button>
  </div>
</div>

<style>
  .test-panel {
    padding: var(--spacing-md);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .agent-info {
    background: var(--bg-elevated-2);
    padding: var(--spacing-md);
    border-radius: var(--radius-md);
  }

  .agent-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-bottom: var(--spacing-xs);
  }

  .agent-name {
    font-weight: 500;
    color: var(--text-primary);
  }

  .agent-tools {
    padding: 2px 6px;
    background: var(--bg-elevated-3);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .agent-description {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .prompt-preview {
    background: var(--bg-elevated-2);
    padding: var(--spacing-md);
    border-radius: var(--radius-md);
  }

  .prompt-preview h4,
  .test-section h4,
  .result-section h4 {
    margin: 0 0 var(--spacing-sm);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .prompt-preview pre {
    margin: 0;
    padding: var(--spacing-sm);
    background: var(--bg-elevated-3);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 150px;
    overflow-y: auto;
  }

  .test-section .field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .test-section textarea {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-family: inherit;
    resize: vertical;
    min-height: 80px;
  }

  .test-section textarea:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.15);
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .actions {
    display: flex;
    justify-content: flex-start;
  }

  .result-section {
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--spacing-md);
  }

  .result-section.success {
    border-color: var(--success);
  }

  .result-section.failure {
    border-color: var(--error);
  }

  .result-section.error {
    border-color: var(--error);
    background: rgba(248, 113, 113, 0.1);
  }

  .result-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--spacing-sm);
  }

  .result-meta {
    display: flex;
    gap: var(--spacing-md);
    font-size: var(--font-size-xs);
  }

  .status {
    padding: 2px 8px;
    border-radius: var(--radius-sm);
    background: rgba(248, 113, 113, 0.15);
    color: var(--error);
  }

  .status.success {
    background: rgba(74, 222, 128, 0.15);
    color: var(--success);
  }

  .duration {
    color: var(--text-muted);
  }

  .result-error {
    margin-bottom: var(--spacing-sm);
    padding: var(--spacing-sm);
    background: rgba(248, 113, 113, 0.1);
    border-radius: var(--radius-sm);
    color: var(--error);
    font-size: var(--font-size-sm);
  }

  .result-output {
    font-size: var(--font-size-sm);
  }

  .result-output strong {
    display: block;
    margin-bottom: var(--spacing-xs);
    color: var(--text-primary);
  }

  .result-output pre,
  .result-section.error pre {
    margin: 0;
    padding: var(--spacing-sm);
    background: var(--bg-elevated-3);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 300px;
    overflow-y: auto;
  }

  .tools-used {
    margin-top: var(--spacing-sm);
    font-size: var(--font-size-sm);
  }

  .tools-used strong {
    display: block;
    margin-bottom: var(--spacing-xs);
    color: var(--text-primary);
  }

  .tool-tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-xs);
  }

  .tool-tag {
    padding: 2px 8px;
    background: rgba(34, 211, 238, 0.15);
    color: var(--accent-primary);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    font-family: var(--font-mono);
  }

  .footer-actions {
    display: flex;
    justify-content: flex-end;
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }
</style>
