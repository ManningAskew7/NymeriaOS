<script lang="ts">
  import type { CustomTool, CustomToolCreateRequest, ToolParameter } from '$lib/types';
  import Button from '../common/Button.svelte';

  interface Props {
    tool?: CustomTool;
    onSubmit: (request: CustomToolCreateRequest) => void;
    onCancel: () => void;
  }

  let { tool, onSubmit, onCancel }: Props = $props();

  // Form state
  let id = $state(tool?.id || '');
  let name = $state(tool?.name || '');
  let description = $state(tool?.description || '');
  let implementationType = $state<'http' | 'mcp'>(tool?.implementationType || 'http');
  let enabled = $state(tool?.enabled ?? true);
  let tagsInput = $state(tool?.tags?.join(', ') || '');

  // HTTP config
  let httpMethod = $state<'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH'>(
    tool?.httpConfig?.method || 'GET'
  );
  let httpUrl = $state(tool?.httpConfig?.url || '');
  let httpHeaders = $state(
    Object.entries(tool?.httpConfig?.headers || {})
      .map(([k, v]) => `${k}: ${v}`)
      .join('\n')
  );
  let httpBodyTemplate = $state(tool?.httpConfig?.bodyTemplate || '');
  let httpTimeoutSeconds = $state(tool?.httpConfig?.timeoutSeconds || 30);
  let httpResponsePath = $state(tool?.httpConfig?.responsePath || '');

  // MCP config
  let mcpServerCommand = $state(tool?.mcpConfig?.serverCommand || '');
  let mcpServerArgs = $state(tool?.mcpConfig?.serverArgs?.join(' ') || '');
  let mcpToolName = $state(tool?.mcpConfig?.toolName || '');
  let mcpEnvVars = $state(
    Object.entries(tool?.mcpConfig?.envVars || {})
      .map(([k, v]) => `${k}=${v}`)
      .join('\n')
  );
  let mcpIdleTimeout = $state(tool?.mcpConfig?.idleTimeoutSeconds || 300);

  // Parameters
  let parametersJson = $state(
    tool?.parameters
      ? JSON.stringify(
          Object.fromEntries(
            Object.entries(tool.parameters).map(([k, v]) => [
              k,
              { type: v.type, description: v.description, required: v.required }
            ])
          ),
          null,
          2
        )
      : '{}'
  );
  let parametersError = $state('');

  function parseHeaders(input: string): Record<string, string> {
    const headers: Record<string, string> = {};
    input.split('\n').forEach((line) => {
      const [key, ...valueParts] = line.split(':');
      if (key && valueParts.length > 0) {
        headers[key.trim()] = valueParts.join(':').trim();
      }
    });
    return headers;
  }

  function parseEnvVars(input: string): Record<string, string> {
    const vars: Record<string, string> = {};
    input.split('\n').forEach((line) => {
      const [key, ...valueParts] = line.split('=');
      if (key && valueParts.length > 0) {
        vars[key.trim()] = valueParts.join('=').trim();
      }
    });
    return vars;
  }

  function validateParameters(): Record<string, ToolParameter> | null {
    try {
      const parsed = JSON.parse(parametersJson);
      parametersError = '';
      const result: Record<string, ToolParameter> = {};
      for (const [key, value] of Object.entries(parsed)) {
        const v = value as Record<string, unknown>;
        result[key] = {
          type: (v.type as ToolParameter['type']) || 'string',
          description: (v.description as string) || '',
          required: (v.required as boolean) || false,
          default: v.default as string | undefined
        };
      }
      return result;
    } catch (e) {
      parametersError = 'Invalid JSON format';
      return null;
    }
  }

  function handleSubmit() {
    const parameters = validateParameters();
    if (!parameters) return;

    const request: CustomToolCreateRequest = {
      id: tool ? tool.id : id,
      name,
      description,
      implementationType,
      parameters,
      enabled,
      tags: tagsInput
        .split(',')
        .map((t) => t.trim())
        .filter((t) => t)
    };

    if (implementationType === 'http') {
      request.httpConfig = {
        method: httpMethod,
        url: httpUrl,
        headers: parseHeaders(httpHeaders),
        body_template: httpBodyTemplate || undefined,
        timeout_seconds: httpTimeoutSeconds,
        response_path: httpResponsePath || undefined,
        response_format: 'auto'
      };
    } else {
      request.mcpConfig = {
        server_command: mcpServerCommand,
        server_args: mcpServerArgs.split(' ').filter((a) => a),
        tool_name: mcpToolName,
        env_vars: parseEnvVars(mcpEnvVars),
        idle_timeout_seconds: mcpIdleTimeout
      };
    }

    onSubmit(request);
  }
</script>

<form class="tool-form" onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
  <div class="form-section">
    <h4>Basic Info</h4>

    {#if !tool}
      <div class="field">
        <label for="tool-id">ID</label>
        <input
          id="tool-id"
          type="text"
          bind:value={id}
          pattern="^[a-zA-Z][a-zA-Z0-9_-]*$"
          placeholder="my_tool"
          required
        />
        <p class="hint">Unique identifier (letters, numbers, underscores)</p>
      </div>
    {/if}

    <div class="field">
      <label for="tool-name">Name</label>
      <input
        id="tool-name"
        type="text"
        bind:value={name}
        placeholder="My Custom Tool"
        required
      />
    </div>

    <div class="field">
      <label for="tool-description">Description</label>
      <textarea
        id="tool-description"
        bind:value={description}
        placeholder="Describe what this tool does..."
        rows="3"
        required
      ></textarea>
      <p class="hint">This description is shown to the LLM for tool selection</p>
    </div>

    <div class="field">
      <label for="tool-tags">Tags</label>
      <input
        id="tool-tags"
        type="text"
        bind:value={tagsInput}
        placeholder="api, utility, search"
      />
      <p class="hint">Comma-separated tags for categorization</p>
    </div>

    <div class="field checkbox-field">
      <input id="tool-enabled" type="checkbox" bind:checked={enabled} />
      <label for="tool-enabled">Enabled</label>
    </div>
  </div>

  <div class="form-section">
    <h4>Implementation Type</h4>

    <div class="type-selector">
      <button
        type="button"
        class="type-btn"
        class:active={implementationType === 'http'}
        onclick={() => (implementationType = 'http')}
      >
        <strong>HTTP</strong>
        <span>REST API calls</span>
      </button>
      <button
        type="button"
        class="type-btn"
        class:active={implementationType === 'mcp'}
        onclick={() => (implementationType = 'mcp')}
      >
        <strong>MCP</strong>
        <span>Model Context Protocol</span>
      </button>
    </div>
  </div>

  {#if implementationType === 'http'}
    <div class="form-section">
      <h4>HTTP Configuration</h4>

      <div class="field-row">
        <div class="field" style="flex: 0 0 120px;">
          <label for="http-method">Method</label>
          <select id="http-method" bind:value={httpMethod}>
            <option value="GET">GET</option>
            <option value="POST">POST</option>
            <option value="PUT">PUT</option>
            <option value="DELETE">DELETE</option>
            <option value="PATCH">PATCH</option>
          </select>
        </div>

        <div class="field" style="flex: 1;">
          <label for="http-url">URL</label>
          <input
            id="http-url"
            type="text"
            bind:value={httpUrl}
            placeholder="https://api.example.com/endpoint/$&#123;param&#125;"
            required
          />
        </div>
      </div>

      <div class="field">
        <label for="http-headers">Headers</label>
        <textarea
          id="http-headers"
          bind:value={httpHeaders}
          placeholder="Content-Type: application/json&#10;Authorization: Bearer $&#123;env:API_KEY&#125;"
          rows="3"
        ></textarea>
        <p class="hint">One header per line. Use $&#123;env:VAR&#125; for secrets.</p>
      </div>

      {#if httpMethod !== 'GET'}
        <div class="field">
          <label for="http-body">Body Template</label>
          <textarea
            id="http-body"
            bind:value={httpBodyTemplate}
            placeholder='&#123;"query": "$&#123;query&#125;", "limit": $&#123;limit&#125;&#125;'
            rows="4"
          ></textarea>
          <p class="hint">JSON template with $&#123;param&#125; placeholders</p>
        </div>
      {/if}

      <div class="field-row">
        <div class="field">
          <label for="http-timeout">Timeout (seconds)</label>
          <input
            id="http-timeout"
            type="number"
            min="1"
            max="300"
            bind:value={httpTimeoutSeconds}
          />
        </div>

        <div class="field">
          <label for="http-response-path">Response Path</label>
          <input
            id="http-response-path"
            type="text"
            bind:value={httpResponsePath}
            placeholder="$.data.items"
          />
          <p class="hint">JSONPath to extract result</p>
        </div>
      </div>
    </div>
  {:else}
    <div class="form-section">
      <h4>MCP Configuration</h4>

      <div class="field">
        <label for="mcp-command">Server Command</label>
        <input
          id="mcp-command"
          type="text"
          bind:value={mcpServerCommand}
          placeholder="npx"
          required
        />
        <p class="hint">Command to start the MCP server</p>
      </div>

      <div class="field">
        <label for="mcp-args">Server Arguments</label>
        <input
          id="mcp-args"
          type="text"
          bind:value={mcpServerArgs}
          placeholder="-y @anthropic/mcp-server-filesystem /path"
        />
        <p class="hint">Space-separated command arguments</p>
      </div>

      <div class="field">
        <label for="mcp-tool">Tool Name</label>
        <input
          id="mcp-tool"
          type="text"
          bind:value={mcpToolName}
          placeholder="read_file"
          required
        />
        <p class="hint">Name of the tool exposed by the MCP server</p>
      </div>

      <div class="field">
        <label for="mcp-env">Environment Variables</label>
        <textarea
          id="mcp-env"
          bind:value={mcpEnvVars}
          placeholder="API_KEY=$&#123;env:MY_API_KEY&#125;&#10;DEBUG=true"
          rows="3"
        ></textarea>
        <p class="hint">One variable per line (KEY=value)</p>
      </div>

      <div class="field">
        <label for="mcp-idle">Idle Timeout (seconds)</label>
        <input
          id="mcp-idle"
          type="number"
          min="30"
          max="3600"
          bind:value={mcpIdleTimeout}
        />
        <p class="hint">Server shutdown after idle period</p>
      </div>
    </div>
  {/if}

  <div class="form-section">
    <h4>Parameters</h4>

    <div class="field">
      <label for="tool-params">Parameters (JSON)</label>
      <textarea
        id="tool-params"
        bind:value={parametersJson}
        placeholder='&#123;&#10;  "query": &#123;"type": "string", "description": "Search query", "required": true&#125;,&#10;  "limit": &#123;"type": "integer", "description": "Max results"&#125;&#10;&#125;'
        rows="6"
        class="monospace"
        class:error={parametersError}
      ></textarea>
      {#if parametersError}
        <p class="error-text">{parametersError}</p>
      {:else}
        <p class="hint">Define tool parameters as JSON object</p>
      {/if}
    </div>
  </div>

  <div class="form-actions">
    <Button variant="secondary" onclick={onCancel}>
      Cancel
    </Button>
    <Button variant="primary" type="submit">
      {tool ? 'Update Tool' : 'Create Tool'}
    </Button>
  </div>
</form>

<style>
  .tool-form {
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

  .field-row {
    display: flex;
    gap: var(--spacing-md);
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
  select,
  textarea {
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
  select:focus,
  textarea:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.15);
  }

  textarea {
    resize: vertical;
    min-height: 60px;
  }

  textarea.monospace {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
  }

  textarea.error,
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

  .type-selector {
    display: flex;
    gap: var(--spacing-sm);
  }

  .type-btn {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 2px solid var(--border-subtle);
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .type-btn:hover {
    border-color: var(--border-default);
  }

  .type-btn.active {
    border-color: var(--accent-primary);
    background: rgba(34, 211, 238, 0.1);
  }

  .type-btn strong {
    color: var(--text-primary);
  }

  .type-btn span {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .form-actions {
    display: flex;
    gap: var(--spacing-sm);
    justify-content: flex-end;
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }
</style>
