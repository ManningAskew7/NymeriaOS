<script lang="ts">
  import type { CustomTool, CustomToolTestResponse } from '$lib/types';
  import { toolsStore } from '$lib/stores/tools.svelte';
  import Button from '../common/Button.svelte';

  interface Props {
    tool: CustomTool;
    onClose: () => void;
  }

  let { tool, onClose }: Props = $props();

  // Parameter values
  let paramValues = $state<Record<string, string>>({});
  let result = $state<CustomToolTestResponse | null>(null);
  let testing = $state(false);
  let error = $state<string | null>(null);

  // Initialize param values with defaults
  $effect(() => {
    const values: Record<string, string> = {};
    for (const [key, param] of Object.entries(tool.parameters)) {
      values[key] = param.default || '';
    }
    paramValues = values;
  });

  async function runTest() {
    testing = true;
    error = null;
    result = null;

    try {
      // Convert string values to appropriate types
      const params: Record<string, unknown> = {};
      for (const [key, param] of Object.entries(tool.parameters)) {
        const value = paramValues[key];
        if (value === '' && !param.required) continue;

        switch (param.type) {
          case 'integer':
            params[key] = parseInt(value, 10);
            break;
          case 'number':
            params[key] = parseFloat(value);
            break;
          case 'boolean':
            params[key] = value.toLowerCase() === 'true';
            break;
          default:
            params[key] = value;
        }
      }

      result = await toolsStore.testTool(tool.id, params);
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
  <div class="tool-info">
    <div class="tool-header">
      <span class="tool-type" class:http={tool.implementationType === 'http'}>
        {tool.implementationType.toUpperCase()}
      </span>
      <span class="tool-id">{tool.id}</span>
    </div>
    <p class="tool-description">{tool.description}</p>
  </div>

  <div class="parameters-section">
    <h4>Parameters</h4>
    {#if Object.keys(tool.parameters).length === 0}
      <p class="no-params">This tool has no parameters</p>
    {:else}
      <div class="params-list">
        {#each Object.entries(tool.parameters) as [key, param]}
          <div class="param-field">
            <label for={`param-${key}`}>
              {key}
              {#if param.required}
                <span class="required">*</span>
              {/if}
              <span class="param-type">({param.type})</span>
            </label>
            {#if param.type === 'boolean'}
              <select
                id={`param-${key}`}
                bind:value={paramValues[key]}
              >
                <option value="">-- Select --</option>
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            {:else}
              <input
                id={`param-${key}`}
                type={param.type === 'integer' || param.type === 'number' ? 'number' : 'text'}
                bind:value={paramValues[key]}
                placeholder={param.description}
              />
            {/if}
            <p class="param-hint">{param.description}</p>
          </div>
        {/each}
      </div>
    {/if}
  </div>

  <div class="actions">
    <Button
      variant="primary"
      onclick={runTest}
      disabled={testing}
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
        <strong>Output:</strong>
        <pre>{typeof result.result === 'string' ? result.result : JSON.stringify(result.result, null, 2)}</pre>
      </div>
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

  .tool-info {
    background: var(--bg-elevated-2);
    padding: var(--spacing-md);
    border-radius: var(--radius-md);
  }

  .tool-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-bottom: var(--spacing-xs);
  }

  .tool-type {
    padding: 2px 6px;
    background: var(--bg-elevated-3);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    text-transform: uppercase;
  }

  .tool-type.http {
    background: rgba(var(--accent-primary-rgb), 0.15);
    color: var(--accent-primary);
  }

  .tool-id {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .tool-description {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .parameters-section h4,
  .result-section h4 {
    margin: 0 0 var(--spacing-sm);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .no-params {
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    font-style: italic;
  }

  .params-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .param-field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .param-field label {
    font-weight: 500;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .param-field .required {
    color: var(--error);
  }

  .param-field .param-type {
    color: var(--text-muted);
    font-weight: normal;
    font-size: var(--font-size-xs);
  }

  .param-field input,
  .param-field select {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-family: inherit;
  }

  .param-field input:focus,
  .param-field select:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(var(--accent-primary-rgb), 0.15);
  }

  .param-hint {
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

  .footer-actions {
    display: flex;
    justify-content: flex-end;
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }
</style>
