<script lang="ts">
  import type { SubAgent, SubAgentCreateRequest, LLMProvider } from '$lib/types';
  import { modelOptions } from '$lib/utils/modelOptions';
  import Button from '../common/Button.svelte';

  interface Props {
    agent?: SubAgent;
    onSubmit: (request: SubAgentCreateRequest) => void;
    onCancel: () => void;
  }

  let { agent, onSubmit, onCancel }: Props = $props();

  // Form state
  let name = $state(agent?.name || '');
  let description = $state(agent?.description || '');
  let systemPrompt = $state(agent?.systemPrompt || '');
  let toolsInput = $state(agent?.tools?.join(', ') || '');
  let contextTurns = $state(agent?.contextTurns || 5);
  let enabled = $state(agent?.enabled ?? true);

  // LLM config state
  let useGlobalModel = $state(!agent?.llmProvider && !agent?.llmModel);
  let llmProvider = $state<LLMProvider>(agent?.llmProvider || 'anthropic');
  let llmModel = $state(agent?.llmModel || 'claude-sonnet-4-20250514');
  let llmTemperature = $state(agent?.llmTemperature ?? 0.0);
  let useCustomModel = $state(agent?.llmModel ? !modelOptions[agent.llmProvider || 'anthropic'].includes(agent.llmModel) : false);
  let customModelInput = $state(useCustomModel ? (agent?.llmModel || '') : '');

  // Validation
  let nameError = $state('');

  function validateName(value: string): boolean {
    if (!value) {
      nameError = 'Name is required';
      return false;
    }
    if (!/^[a-z][a-z0-9_]*$/.test(value)) {
      nameError = 'Name must start with lowercase letter and contain only lowercase letters, numbers, and underscores';
      return false;
    }
    nameError = '';
    return true;
  }

  function handleSubmit() {
    if (!agent && !validateName(name)) return;

    const request: SubAgentCreateRequest = {
      name: agent ? agent.name : name,
      description,
      systemPrompt,
      allowedTools: toolsInput
        .split(',')
        .map((t) => t.trim())
        .filter((t) => t),
      contextTurns
    };

    // Add LLM config only if not using global settings
    if (!useGlobalModel) {
      request.llmProvider = llmProvider;
      request.llmModel = useCustomModel ? customModelInput : llmModel;
      request.llmTemperature = llmTemperature;
    } else {
      // Explicitly set to null to clear any existing config
      request.llmProvider = null;
      request.llmModel = null;
      request.llmTemperature = null;
    }

    onSubmit(request);
  }
</script>

<form class="agent-form" onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
  <div class="form-section">
    <h4>Basic Info</h4>

    {#if !agent}
      <div class="field">
        <label for="agent-name">Name</label>
        <input
          id="agent-name"
          type="text"
          bind:value={name}
          oninput={() => validateName(name)}
          placeholder="my_agent"
          class:error={nameError}
          required
        />
        {#if nameError}
          <p class="error-text">{nameError}</p>
        {:else}
          <p class="hint">Unique identifier (lowercase, numbers, underscores)</p>
        {/if}
      </div>
    {/if}

    <div class="field">
      <label for="agent-description">Description</label>
      <textarea
        id="agent-description"
        bind:value={description}
        placeholder="Describe what this agent specializes in..."
        rows="2"
        required
      ></textarea>
      <p class="hint">Brief description of the agent's purpose</p>
    </div>

    <div class="field checkbox-field">
      <input id="agent-enabled" type="checkbox" bind:checked={enabled} />
      <label for="agent-enabled">Enabled</label>
    </div>
  </div>

  <div class="form-section">
    <h4>Configuration</h4>

    <div class="field">
      <label for="agent-prompt">System Prompt</label>
      <textarea
        id="agent-prompt"
        bind:value={systemPrompt}
        placeholder="You are a specialized agent that..."
        rows="6"
        class="monospace"
        required
      ></textarea>
      <p class="hint">Instructions that define the agent's behavior and capabilities</p>
    </div>

    <div class="field">
      <label for="agent-tools">Allowed Tools</label>
      <input
        id="agent-tools"
        type="text"
        bind:value={toolsInput}
        placeholder="file_read, file_write, execute_command"
      />
      <p class="hint">Comma-separated list of tool names this agent can use (leave empty for all tools)</p>
    </div>

    <div class="field">
      <label for="agent-context">Context Turns</label>
      <input
        id="agent-context"
        type="number"
        min="1"
        max="20"
        bind:value={contextTurns}
      />
      <p class="hint">Number of conversation turns to include as context (1-20)</p>
    </div>
  </div>

  <div class="form-section">
    <h4>Model Configuration</h4>

    <div class="field checkbox-field">
      <input id="use-global-model" type="checkbox" bind:checked={useGlobalModel} />
      <label for="use-global-model">Use Global Model Settings</label>
    </div>

    {#if !useGlobalModel}
      <div class="model-config">
        <div class="field">
          <label for="llm-provider">Provider</label>
          <select id="llm-provider" bind:value={llmProvider} onchange={() => {
            llmModel = modelOptions[llmProvider][0];
            useCustomModel = false;
            customModelInput = '';
          }}>
            <option value="anthropic">Anthropic</option>
            <option value="openai">OpenAI</option>
            <option value="openrouter">OpenRouter</option>
          </select>
        </div>

        <div class="field">
          <label for="llm-model">Model</label>
          {#if useCustomModel}
            <input
              id="llm-model"
              type="text"
              bind:value={customModelInput}
              placeholder="Enter custom model name"
            />
          {:else}
            <select id="llm-model" bind:value={llmModel}>
              {#each modelOptions[llmProvider] as model}
                <option value={model}>{model}</option>
              {/each}
            </select>
          {/if}
          <div class="field checkbox-field small">
            <input id="use-custom-model" type="checkbox" bind:checked={useCustomModel} onchange={() => {
              if (!useCustomModel) customModelInput = '';
            }} />
            <label for="use-custom-model">Use custom model</label>
          </div>
        </div>

        <div class="field">
          <label for="llm-temperature">Temperature: {llmTemperature.toFixed(1)}</label>
          <input
            id="llm-temperature"
            type="range"
            min="0"
            max="2"
            step="0.1"
            bind:value={llmTemperature}
          />
          <p class="hint">Higher values make output more random, lower values more focused</p>
        </div>
      </div>
    {:else}
      <p class="hint">This agent will use the global LLM settings configured in Settings.</p>
    {/if}
  </div>

  <div class="form-section">
    <h4>Prompt Guidelines</h4>
    <div class="guidelines">
      <p>A good system prompt should:</p>
      <ul>
        <li>Define the agent's role and expertise</li>
        <li>Specify any constraints or focus areas</li>
        <li>Describe the expected output format</li>
        <li>Include examples if helpful</li>
      </ul>
      <p class="example">
        <strong>Example:</strong> "You are a code review specialist. Analyze code for bugs, security issues, and style problems. Always provide specific line numbers and suggest fixes."
      </p>
    </div>
  </div>

  <div class="form-actions">
    <Button variant="secondary" onclick={onCancel}>
      Cancel
    </Button>
    <Button variant="primary" type="submit">
      {agent ? 'Update Agent' : 'Create Agent'}
    </Button>
  </div>
</form>

<style>
  .agent-form {
    padding: var(--spacing-md);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
  }

  .form-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .form-section h4 {
    margin: 0;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .checkbox-field {
    flex-direction: row;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .checkbox-field label {
    margin: 0;
  }

  label {
    font-weight: 500;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  input[type='text'],
  input[type='number'],
  textarea,
  select {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-family: inherit;
  }

  input:focus,
  textarea:focus,
  select:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.15);
  }

  select {
    cursor: pointer;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%239ca3af' d='M3 4.5L6 7.5L9 4.5'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 12px center;
    padding-right: 36px;
  }

  input[type='range'] {
    width: 100%;
    cursor: pointer;
    accent-color: var(--accent-primary);
  }

  .model-config {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    padding: var(--spacing-md);
    background: var(--bg-elevated-2);
    border-radius: var(--radius-md);
  }

  .checkbox-field.small {
    margin-top: var(--spacing-xs);
  }

  .checkbox-field.small label {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .checkbox-field.small input[type='checkbox'] {
    width: 14px;
    height: 14px;
  }

  textarea {
    resize: vertical;
    min-height: 60px;
  }

  textarea.monospace {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
  }

  input.error {
    border-color: var(--error);
  }

  input[type='checkbox'] {
    width: 18px;
    height: 18px;
    accent-color: var(--accent-primary);
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .error-text {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--error);
  }

  .guidelines {
    background: var(--bg-elevated-2);
    padding: var(--spacing-md);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .guidelines p {
    margin: 0 0 var(--spacing-sm);
  }

  .guidelines ul {
    margin: 0 0 var(--spacing-sm);
    padding-left: var(--spacing-lg);
  }

  .guidelines li {
    margin-bottom: var(--spacing-xs);
  }

  .guidelines .example {
    padding: var(--spacing-sm);
    background: var(--bg-elevated-3);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    font-style: italic;
    margin-bottom: 0;
  }

  .form-actions {
    display: flex;
    gap: var(--spacing-sm);
    justify-content: flex-end;
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }
</style>
