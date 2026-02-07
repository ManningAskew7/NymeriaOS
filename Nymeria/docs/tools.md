# Nymeria Tools Reference

Nymeria includes 45+ built-in tools organized into multiple categories, plus support for **Custom Tools** that you can create and manage through the desktop UI or API.

| Category | Tools | Purpose |
|----------|-------|---------|
| **Core Tools** | 6 | Shell execution, file operations, web search, Claude Code |
| **Memory Tools** | 5 | User memories and personality preferences |
| **RAG Tools** | 2 | Semantic search across conversations and memories |
| **TODO Tools** | 5 | Task management for autonomous operation |
| **Self-Modification Tools** | 4 | Modify Nymeria's own tools at runtime |
| **Sub-Agent Tools** | 4 | Invoke specialized sub-agents (also available as direct tools) |
| **Visibility Tools** | 1 | Control response display (mute_response) |
| **Browser Tools** | 8 | Native Playwright browser automation |
| **Outlook Tools** | 13 | Microsoft Graph email and authentication |
| **Custom Tools** | ∞ | User-defined HTTP or MCP tools |

---

## Core Tools

### bash_execute

Execute shell commands on the local system.

```python
bash_execute(command: str, working_directory: Optional[str] = None, timeout_seconds: int = 120)
```

**Parameters:**
- `command`: Shell command to execute
- `working_directory`: Directory to run the command in (optional)
- `timeout_seconds`: Maximum execution time (default: 120s)

**Returns:** Command output (stdout + stderr) or error message

---

### file_read

Read contents of a file.

```python
file_read(file_path: str, encoding: str = "utf-8", max_lines: Optional[int] = None)
```

**Parameters:**
- `file_path`: Path to the file to read
- `encoding`: File encoding (default: utf-8)
- `max_lines`: Limit number of lines to read (optional)

**Returns:** File contents or error message

**Limits:** 10MB maximum file size

---

### file_write

Write content to a file.

```python
file_write(file_path: str, content: str, encoding: str = "utf-8", create_directories: bool = True, append: bool = False)
```

**Parameters:**
- `file_path`: Path to the file to write
- `content`: Content to write
- `encoding`: File encoding (default: utf-8)
- `create_directories`: Create parent directories if needed (default: true)
- `append`: Append to existing file instead of overwriting (default: false)

**Returns:** Success/error message

---

### file_list

List files in a directory.

```python
file_list(directory: str, pattern: str = "*", recursive: bool = False)
```

**Parameters:**
- `directory`: Directory to list
- `pattern`: Glob pattern to filter files (default: *)
- `recursive`: Include subdirectories (default: false)

**Returns:** List of files with sizes

---

### web_search

Search the web using Perplexity API.

```python
web_search(query: str)
```

**Parameters:**
- `query`: Search query

**Returns:** Search results with citations

**Requires:** `PERPLEXITY_API_KEY` environment variable

---

### claude_code

Invoke Claude Code in headless mode to create, modify, or analyze code in a specific directory.

```python
claude_code(prompt: str, working_dir: Optional[str] = None, model: str = "sonnet",
            allow_edit: bool = True, allow_bash: bool = True, timeout: int = 300)
```

**Parameters:**
- `prompt`: The coding task or question for Claude Code
- `working_dir`: Directory to run in (defaults to current directory)
- `model`: Model to use - `"sonnet"`, `"opus"`, or `"haiku"` (default: "sonnet")
- `allow_edit`: Allow Claude Code to edit files (default: True)
- `allow_bash`: Allow Claude Code to run commands (default: True)
- `timeout`: Timeout in seconds (default: 300)

**Returns:** Claude Code's response or error message

**Examples:**
```python
claude_code("Create a Python script that sorts a CSV file")
claude_code("Fix the bug in main.py", working_dir="/path/to/project")
claude_code("Analyze the code structure", allow_edit=False)
```

**Requires:** Claude Code extension installed (VSCode)

---

## Memory Tools

Memories are **automatically injected** into Nymeria's system prompt. There's no need for a "recall" tool - Nymeria always knows stored memories without explicit retrieval.

### memory_save

Save a memory about the user to persistent storage.

```python
memory_save(key: str, value: str)
```

**Parameters:**
- `key`: Short identifier (e.g., "name", "occupation", "preference_tone")
- `value`: Information to remember (max 1000 characters)

**Returns:** Confirmation message

**Examples:**
```python
memory_save("name", "Alex")
memory_save("occupation", "Software engineer at Acme Corp")
memory_save("preference_communication", "Prefers concise responses")
```

**Limits:** 100 memories per user

---

### memory_forget

Remove a memory from persistent storage.

```python
memory_forget(key: str)
```

**Parameters:**
- `key`: Memory key to delete

**Returns:** Confirmation message or error with available keys

**Examples:**
```python
memory_forget("old_project")
memory_forget("previous_employer")
```

---

### memory_list

List all memories stored for the current user.

```python
memory_list()
```

**Returns:** All stored memories and personality preferences

---

### memory_clear_all

Clear ALL memories for the current user. This is irreversible.

```python
memory_clear_all()
```

**Returns:** Confirmation with count of deleted memories

**Use case:** When user explicitly asks to "forget everything" about them

---

### personality_set

Set a communication/personality preference.

```python
personality_set(trait: str, value: str)
```

**Parameters:**
- `trait`: Preference category (e.g., "tone", "verbosity", "expertise_level")
- `value`: Desired behavior

**Returns:** Confirmation message

**Examples:**
```python
personality_set("tone", "casual and friendly")
personality_set("verbosity", "be very concise, bullet points preferred")
personality_set("expertise_level", "assume advanced programming knowledge")
personality_set("humor", "include occasional dry humor")
```

---

## RAG Tools

RAG (Retrieval Augmented Generation) enables semantic search across past conversations, memories, and TODOs to provide relevant context in responses.

### rag_search

Search past conversations and memories for relevant context.

```python
rag_search(query: str, max_results: int = 5)
```

**Parameters:**
- `query`: What to search for (e.g., "user's project", "deadline", "preferences")
- `max_results`: Maximum number of results to return (1-10, default: 5)

**Returns:** Relevant context from past conversations and memories, or a message if nothing relevant was found.

**Examples:**
```python
rag_search("what project is the user working on")
rag_search("user's programming language preferences")
rag_search("previous conversations about deadlines")
```

**Note:** RAG must be enabled for the user via `rag_settings(enabled=True)`.

---

### rag_settings

Configure RAG (Retrieval Augmented Generation) settings.

```python
rag_settings(enabled: Optional[bool] = None, max_chunks: Optional[int] = None,
             include_conversations: Optional[bool] = None, include_memories: Optional[bool] = None,
             include_todos: Optional[bool] = None, auto_flush: Optional[bool] = None)
```

**Parameters:**
- `enabled`: Turn RAG on/off (main toggle)
- `max_chunks`: Maximum context chunks to include per message (1-10)
- `include_conversations`: Include past conversation snippets in search
- `include_memories`: Include saved memories in search
- `include_todos`: Include completed TODO outcomes in search
- `auto_flush`: Automatically preserve context before window trims

**Returns:** Current RAG settings after any changes

**Examples:**
```python
rag_settings(enabled=True)  # Enable RAG
rag_settings(max_chunks=3)  # Limit context chunks
rag_settings(include_todos=False)  # Exclude TODOs from search
```

**RAG Features:**
- **Semantic Search**: Uses vector embeddings for meaning-based retrieval
- **Content Types**: Conversations, memories, and TODOs are indexed separately
- **Auto-flush**: Important context is preserved before context window trims
- **User Opt-in**: RAG is disabled by default and requires explicit enabling

---

## TODO Tools

TODOs serve as the **primary driver for autonomous operation**. Active TODOs are automatically injected into the system prompt, so Nymeria always knows what tasks need attention.

### todo_add

Create a new TODO item, optionally scheduled for future autonomous execution.

```python
todo_add(task: str, priority: Optional[str] = None, deadline: Optional[str] = None, scheduled_for: Optional[str] = None)
```

**Parameters:**
- `task`: Description of the task (max 500 characters)
- `priority`: Priority level - `"high"`, `"medium"`, or `"low"` (optional)
- `deadline`: Due date in `YYYY-MM-DD` format (optional)
- `scheduled_for`: Schedule for autonomous execution (optional). Formats:
  - Relative: `"30m"`, `"1h"`, `"2d"` (minutes, hours, days)
  - Absolute: `"2024-03-15 14:30"` or `"tomorrow 9am"`

**Returns:** Confirmation with TODO ID

**Examples:**
```python
# Simple TODO (manual execution)
todo_add("Research Python async patterns")
todo_add("Deploy new feature to production", priority="high")
todo_add("Review quarterly reports", deadline="2024-03-15")

# Scheduled TODO (autonomous execution)
todo_add("Check inbox for new emails", scheduled_for="30m")
todo_add("Remind about meeting", scheduled_for="1h", priority="high")
```

**Limits:** 50 active TODOs per user

**Scheduling:** TODOs with `scheduled_for` are automatically executed by the Ticker when due. See "Scheduled TODO Execution" section for details.

---

### todo_update

Update an existing TODO item.

```python
todo_update(todo_id: str, status: Optional[str] = None, notes: Optional[str] = None,
            blocked_reason: Optional[str] = None, priority: Optional[str] = None)
```

**Parameters:**
- `todo_id`: The 8-character TODO ID
- `status`: New status - `"pending"`, `"in_progress"`, `"done"`, or `"blocked"`
- `notes`: Add or update notes (max 1000 characters)
- `blocked_reason`: Why the task is blocked (auto-sets status to blocked)
- `priority`: Update priority level

**Returns:** Confirmation message

**Examples:**
```python
todo_update("abc12345", status="in_progress")
todo_update("abc12345", notes="Completed first 3 sections")
todo_update("abc12345", blocked_reason="Waiting for API access")
```

---

### todo_complete

Mark a TODO item as completed.

```python
todo_complete(todo_id: str)
```

**Parameters:**
- `todo_id`: The 8-character TODO ID

**Returns:** Confirmation message

**Note:** Completed items are automatically archived after 7 days.

---

### todo_delete

Delete a TODO item permanently.

```python
todo_delete(todo_id: str)
```

**Parameters:**
- `todo_id`: The 8-character TODO ID

**Returns:** Confirmation message

**Note:** Unlike completing, deleted items are immediately removed with no archive.

---

### todo_list

List all TODO items for the current user.

```python
todo_list(filter_status: Optional[str] = None)
```

**Parameters:**
- `filter_status`: Filter by status - `"pending"`, `"in_progress"`, `"blocked"`, `"done"`, or `"all"`

**Returns:** Formatted list of TODO items with status icons

**Default behavior:** Shows all active (non-done) items, sorted by priority and creation date.

---

## Self-Modification Tools

Nymeria can modify its own tools at runtime. All modifications are:
- Restricted to `nymeria/tools/` directory only
- Backed up before changes (auto-rollback available)
- Validated for Python syntax before saving

### self_modify

Invoke Nymeria's self-modification sub-agent.

```python
self_modify(instruction: str, category: Literal["add_tool", "remove_tool", "fix_bug", "explain"])
```

**Parameters:**
- `instruction`: Detailed description of what to do
- `category`: Type of modification:
  - `"add_tool"`: Create a new tool
  - `"remove_tool"`: Remove an existing tool
  - `"fix_bug"`: Fix an issue in an existing tool
  - `"explain"`: Explain how something works (read-only)

**Returns:** Result of modification or explanation

**Examples:**
```python
self_modify("Create a tool that generates random numbers between min and max", "add_tool")
self_modify("Remove the calculator tool", "remove_tool")
self_modify("The file_read tool crashes when the file is empty, fix it", "fix_bug")
self_modify("How does the memory_save tool work?", "explain")
```

**Important:** After creating or modifying tools, call `tools_reload()` to make them available.

---

### self_modify_rollback

Rollback a file to its previous version from backup.

```python
self_modify_rollback(file_path: str)
```

**Parameters:**
- `file_path`: Path to file to rollback (e.g., "nymeria/tools/my_tool.py")

**Returns:** Success or error message

**Use case:** When a self-modification broke something

---

### tools_reload

Reload all tools to pick up newly created or modified tools.

```python
tools_reload()
```

**Returns:** List of available tools after reload

**Important:** Call this after using `self_modify` to create or fix tools. This makes new tools immediately available without restarting Nymeria.

---

### invoke_tool

Dynamically invoke a tool by name. Useful for testing newly created tools in the same conversation turn.

```python
invoke_tool(tool_name: str, args: str)
```

**Parameters:**
- `tool_name`: Name of the tool to invoke (e.g., "roll_dice")
- `args`: JSON string of arguments to pass (e.g., '{"sides": 6}')

**Returns:** The result of the tool invocation, or an error message

**Examples:**
```python
invoke_tool("roll_dice", '{"sides": 20}')
invoke_tool("random_color", '{}')
```

**Use case:** After `self_modify` creates a tool and `tools_reload` is called, the new tool won't be in the current turn's graph. Use `invoke_tool` to call it anyway.

---

## Scheduled TODO Execution

Nymeria operates autonomously 24/7 through **scheduled TODOs** - TODOs with a `scheduled_for` datetime that are automatically executed when due.

**Durable Scheduling:** Scheduled TODOs are persisted to SQLite (`data/schedules.db`), so they survive application restarts. If Nymeria is restarted, missed scheduled TODOs are automatically recovered and executed.

### Creating Scheduled TODOs

Use `todo_add` with the `scheduled_for` parameter:

```python
todo_add(task: str, scheduled_for: str, priority: str = None)
```

**Parameters:**
- `task`: What to do when the scheduled time arrives
- `scheduled_for`: When to execute. Formats:
  - Relative: `"30m"`, `"1h"`, `"2d"` (minutes, hours, days)
  - Absolute: `"2024-03-15 14:30"` or `"tomorrow 9am"`
- `priority`: Optional priority level

**Examples:**
```python
todo_add("Check inbox for new emails", scheduled_for="30m")
todo_add("Remind about meeting", scheduled_for="1h", priority="high")
todo_add("Weekly report review", scheduled_for="monday 9am")
```

**Behavior:**
- Scheduled TODOs are tracked in `TodoScheduleDB`
- Ticker polls every 5 seconds for due TODOs
- Auto-cancelled if user sends a message to the same thread
- **Rate limited**: Default 50 autonomous executions per hour per user
- **Sliding window**: Context automatically trimmed after execution

**Rate Limiting:**
To prevent runaway autonomous loops, scheduled TODO execution is rate limited to 50 executions per hour per user (configurable via `MAX_SELF_INVOKES_PER_HOUR`). The rate limiter uses a sliding window algorithm extracted to `rate_limiter.py`.

---

## Legacy: self_invoke (Deprecated)

> **Note:** The `self_invoke` tool is deprecated. Use `todo_add` with `scheduled_for` instead.

The legacy scheduler system is retained for backwards compatibility but emits deprecation warnings.

```python
# Deprecated
self_invoke("Check inbox", "30m")

# Preferred
todo_add("Check inbox", scheduled_for="30m")
```

Old scheduled tasks are automatically migrated to the TODO system on startup via `migration.py`.

---

## Visibility Tools

Control how responses are displayed to the user.

### mute_response

Move the current response to the activity log instead of showing in chat.

```python
mute_response(reason: str = "")
```

**Parameters:**
- `reason`: Brief explanation of why this response is being muted (e.g., "routine check, no changes")

**Returns:** Confirmation message

**Behavior:**
- Response is moved from chat thread to activity log
- **Response is still saved to conversation history** - the LLM can see muted messages in subsequent turns
- Works for both autonomous (scheduled) and interactive (user-initiated) messages
- The `done` SSE event includes `muted: true` so the frontend can hide the message

**When to use `mute_response`:**
- ✅ Routine background checks with nothing to report
- ✅ Scheduled monitoring tasks with no changes detected
- ✅ Autonomous work where the user didn't ask for updates

**When NOT to use `mute_response`:**
- ❌ User explicitly asked a question
- ❌ Something important or interesting happened
- ❌ Significant actions were taken
- ❌ User is actively engaged and expecting a response

**Example:**
```python
# During a scheduled "check for updates" task where nothing changed
mute_response(reason="hourly check, no new updates found")
```

**Implementation Notes:**
- Uses thread-local storage to track mute flag during tool execution
- Flag is checked after streaming completes, before sending `done` event
- Muted messages are logged to activity feed for user review if needed

**Autonomous Task Integration:**
When `mute_response` is called during autonomous execution (scheduled TODO):
1. The `task_completed` event includes `visibility: "activity"`
2. Frontend removes the streaming message from chat
3. Response appears only in the activity feed
4. If `visibility: "full"`, the message stays in chat

---

## Browser Tools

Native Playwright browser automation tools. The browser runs in a dedicated thread to avoid Playwright's threading restrictions.

### browser_navigate

Navigate browser to a URL. Opens browser if not already open.

```python
browser_navigate(url: str)
```

**Parameters:**
- `url`: URL to navigate to (e.g., "https://google.com")

**Returns:** Page title and URL on success

---

### browser_click

Click an element on the page.

```python
browser_click(selector: str)
```

**Parameters:**
- `selector`: CSS selector or text to click (e.g., "button.submit", "text=Login")

**Returns:** Success/error message

---

### browser_type

Type text into an input field.

```python
browser_type(selector: str, text: str)
```

**Parameters:**
- `selector`: CSS selector for input field (e.g., "input[name='search']", "#email")
- `text`: Text to type

**Returns:** Success/error message

---

### browser_get_content

Get the text content of the current page.

```python
browser_get_content(include_links: bool = True)
```

**Parameters:**
- `include_links`: Whether to include link URLs (default: True)

**Returns:** Page URL, title, text content, and optionally links

---

### browser_screenshot

Take a screenshot of the current page.

```python
browser_screenshot()
```

**Returns:** Base64 encoded PNG image

---

### browser_scroll

Scroll the page.

```python
browser_scroll(direction: str = "down", amount: int = 500)
```

**Parameters:**
- `direction`: "up" or "down"
- `amount`: Pixels to scroll (default: 500)

**Returns:** Success/error message

---

### browser_press_key

Press a keyboard key.

```python
browser_press_key(key: str)
```

**Parameters:**
- `key`: Key to press (e.g., "Enter", "Tab", "Escape", "ArrowDown")

**Returns:** Success/error message

---

### browser_close

Close the browser.

```python
browser_close()
```

**Returns:** Success/error message

**Note:** The browser persists between calls until explicitly closed or Nymeria stops.

---

## Outlook Tools

Microsoft Graph API integration for Outlook email operations. Uses OAuth device code flow for authentication.

### Authentication Tools

#### outlook_auth_start

Start the Microsoft OAuth authentication flow using device code.

```python
outlook_auth_start()
```

**Returns:** Device code and URL for user to visit to complete authentication.

**Flow:**
1. Call `outlook_auth_start()` to get a device code
2. User visits the provided URL and enters the code
3. Call `outlook_auth_complete()` to finish authentication

---

#### outlook_auth_complete

Complete the OAuth authentication after user has entered the device code.

```python
outlook_auth_complete()
```

**Returns:** Success with account info, or error if authentication failed.

---

#### outlook_list_authenticated_accounts

List all Microsoft accounts that have been authenticated.

```python
outlook_list_authenticated_accounts()
```

**Returns:** List of authenticated accounts with IDs and email addresses.

---

### Email Tools

#### outlook_list_emails

List recent emails from Outlook.

```python
outlook_list_emails(account_id: Optional[str] = None, limit: int = 10,
                    folder: str = "inbox", unread_only: bool = False)
```

**Parameters:**
- `account_id`: Microsoft account ID (optional, uses first account if not specified)
- `limit`: Maximum number of emails to return (default: 10, max: 50)
- `folder`: Mail folder to list from (default: "inbox"). Options: inbox, sentitems, drafts, deleteditems, archive, junkemail
- `unread_only`: If True, only show unread emails

**Returns:** List of emails with sender, subject, date, and ID for each.

---

#### outlook_get_email

Get full details of a specific email by ID.

```python
outlook_get_email(email_id: str, account_id: Optional[str] = None)
```

**Parameters:**
- `email_id`: The email ID (from outlook_list_emails or outlook_search_emails)
- `account_id`: Microsoft account ID (optional)

**Returns:** Full email details including body content.

---

#### outlook_search_emails

Search emails using keywords.

```python
outlook_search_emails(query: str, account_id: Optional[str] = None, limit: int = 10)
```

**Parameters:**
- `query`: Search query (searches subject, body, sender)
- `account_id`: Microsoft account ID (optional)
- `limit`: Maximum results (default: 10)

**Returns:** List of matching emails.

---

#### outlook_send_email

Send a new email.

```python
outlook_send_email(to: str, subject: str, body: str, account_id: Optional[str] = None,
                   cc: Optional[str] = None, bcc: Optional[str] = None, is_html: bool = False)
```

**Parameters:**
- `to`: Recipient email address(es), comma-separated for multiple
- `subject`: Email subject line
- `body`: Email body content
- `account_id`: Microsoft account ID (optional)
- `cc`: CC recipients, comma-separated (optional)
- `bcc`: BCC recipients, comma-separated (optional)
- `is_html`: Set to True if body contains HTML (default: False)

**Returns:** Success or error message.

---

#### outlook_reply_email

Reply to an email.

```python
outlook_reply_email(email_id: str, body: str, account_id: Optional[str] = None, reply_all: bool = False)
```

**Parameters:**
- `email_id`: ID of the email to reply to
- `body`: Reply message body
- `account_id`: Microsoft account ID (optional)
- `reply_all`: If True, reply to all recipients (default: False)

**Returns:** Success or error message.

---

#### outlook_create_draft

Create an email draft without sending it.

```python
outlook_create_draft(to: str, subject: str, body: str, account_id: Optional[str] = None, cc: Optional[str] = None)
```

**Parameters:**
- `to`: Recipient email address(es), comma-separated
- `subject`: Email subject line
- `body`: Email body content
- `account_id`: Microsoft account ID (optional)
- `cc`: CC recipients, comma-separated (optional)

**Returns:** Success message with draft ID.

---

#### outlook_delete_email

Delete an email (moves to Deleted Items, or permanently deletes).

```python
outlook_delete_email(email_id: str, account_id: Optional[str] = None, permanent: bool = False)
```

**Parameters:**
- `email_id`: ID of the email to delete
- `account_id`: Microsoft account ID (optional)
- `permanent`: If True, permanently delete. If False, move to trash (default)

**Returns:** Success or error message.

---

#### outlook_mark_email

Mark an email as read or unread.

```python
outlook_mark_email(email_id: str, is_read: bool, account_id: Optional[str] = None)
```

**Parameters:**
- `email_id`: ID of the email to update
- `is_read`: True to mark as read, False to mark as unread
- `account_id`: Microsoft account ID (optional)

**Returns:** Success or error message.

---

#### outlook_move_email

Move an email to a different folder.

```python
outlook_move_email(email_id: str, folder: str, account_id: Optional[str] = None)
```

**Parameters:**
- `email_id`: ID of the email to move
- `folder`: Destination folder (inbox, archive, deleteditems, junkemail, etc.)
- `account_id`: Microsoft account ID (optional)

**Returns:** Success or error message.

---

#### outlook_forward_email

Forward an email to another recipient.

```python
outlook_forward_email(email_id: str, to: str, comment: Optional[str] = None, account_id: Optional[str] = None)
```

**Parameters:**
- `email_id`: ID of the email to forward
- `to`: Recipient email address(es), comma-separated
- `comment`: Optional message to include with the forward
- `account_id`: Microsoft account ID (optional)

**Returns:** Success or error message.

---

## Custom Tools

Custom tools allow you to extend Nymeria's capabilities without writing Python code. You can create tools via the **Desktop UI** (Settings → Tools) or the **REST API**.

### Tool Types

| Type | Description | Use Case |
|------|-------------|----------|
| **HTTP** | Makes REST API calls to external services | Integrate with APIs, webhooks, web services |
| **MCP** | Connects to Model Context Protocol servers | Use existing MCP tools, complex integrations |

### HTTP Tools

HTTP tools make REST API calls with configurable:
- **Method**: GET, POST, PUT, DELETE, PATCH
- **URL**: Supports `${param}` interpolation for dynamic URLs
- **Headers**: Including `${env:VAR_NAME}` for secrets from environment
- **Body Template**: JSON template with parameter placeholders
- **Response Path**: JSONPath to extract specific data from response

**Example: Weather API Tool**
```json
{
  "id": "get_weather",
  "name": "Get Weather",
  "description": "Get current weather for a city",
  "implementation_type": "http",
  "parameters": {
    "city": {
      "type": "string",
      "description": "City name",
      "required": true
    }
  },
  "http_config": {
    "method": "GET",
    "url": "https://api.weather.com/v1/current?city=${city}",
    "headers": {
      "Authorization": "Bearer ${env:WEATHER_API_KEY}"
    },
    "response_path": "$.data.temperature"
  }
}
```

**Parameter Interpolation:**
- `${param_name}` - Replaced with the parameter value
- `${env:VAR_NAME}` - Replaced with environment variable (for secrets)

### MCP Tools

MCP (Model Context Protocol) tools connect to external MCP servers via JSON-RPC over stdio. This allows you to use any existing MCP server as a Nymeria tool.

**Configuration:**
- **Server Command**: Command to start the MCP server (e.g., `npx`, `python`)
- **Server Args**: Command-line arguments
- **Tool Name**: The specific tool exposed by the MCP server
- **Environment Variables**: Variables to pass to the server
- **Idle Timeout**: Server shutdown after inactivity (default: 5 minutes)

**Example: Filesystem MCP Tool**
```json
{
  "id": "read_file_mcp",
  "name": "Read File (MCP)",
  "description": "Read a file using the MCP filesystem server",
  "implementation_type": "mcp",
  "parameters": {
    "path": {
      "type": "string",
      "description": "File path to read",
      "required": true
    }
  },
  "mcp_config": {
    "server_command": "npx",
    "server_args": ["-y", "@anthropic/mcp-server-filesystem", "/allowed/path"],
    "tool_name": "read_file",
    "idle_timeout_seconds": 300
  }
}
```

**MCP Server Lifecycle:**
- Servers start on-demand when the tool is first called
- Servers stay alive for the configured idle timeout
- Multiple tools can share the same MCP server
- Servers are gracefully shutdown when Nymeria stops

### Managing Custom Tools

**Via Desktop UI:**
1. Open Settings → Tools tab
2. Click "+ New Tool" to create
3. Fill in the form (HTTP or MCP configuration)
4. Test the tool with sample parameters
5. Enable/disable tools as needed

**Via REST API:**
```bash
# List all custom tools
GET /tools/custom

# Create a new tool
POST /tools/custom

# Update a tool
PUT /tools/custom/{tool_id}

# Delete a tool
DELETE /tools/custom/{tool_id}

# Test a tool
POST /tools/custom/{tool_id}/test
```

### Custom Tool Storage

Custom tools are stored as JSON files in `data/custom_tools/`. Each tool is a separate `.json` file named by its ID.

---

## Sub-Agents

Sub-agents are specialized assistants with their own system prompts and tool restrictions. They enable focused, domain-specific behavior.

**Direct Tool Invocation:** Sub-agents are now available as directly callable tools in Nymeria's tool list. For example, if you create a `BrowserAgent`, it appears as a tool that can be called directly:

```python
# Direct invocation (preferred)
BrowserAgent(task="Go to google.com and search for cats")

# Wrapper invocation (still supported)
sub_agent("BrowserAgent", "Go to google.com and search for cats")
```

This makes sub-agents discoverable without calling `list_agents()` first.

### sub_agent

Invoke a sub-agent to perform a specialized task.

```python
sub_agent(agent_name: str, instruction: str)
```

**Parameters:**
- `agent_name`: Name of the sub-agent
- `instruction`: What you want the sub-agent to do

**Returns:** The sub-agent's response

**Examples:**
```python
sub_agent("CodeReviewer", "Review this Python function for bugs")
sub_agent("EmailDrafter", "Write a professional follow-up email")
```

**Note:** Consider using the direct tool invocation (e.g., `CodeReviewer(task="...")`) instead of this wrapper.

---

### list_agents

List all available sub-agents with detailed information.

```python
list_agents()
```

**Returns:** List of sub-agents with their names, descriptions, and configuration details

**Note:** Sub-agents now appear directly in the tool list, so this function is primarily useful for getting detailed configuration information like required environment variables.

---

### clear_agent_context

Clear the conversation context for a sub-agent.

```python
clear_agent_context(agent_name: str)
```

**Parameters:**
- `agent_name`: Name of the sub-agent

**Returns:** Confirmation message

---

### reload_agents

Reload all sub-agents from the agents directory. Also regenerates the direct tool bindings for each agent.

```python
reload_agents()
```

**Returns:** Number of agents loaded and their names

**Note:** After reloading, agents will appear as directly callable tools in the tool list. Due to how LangGraph works, newly created agent tools are available on the NEXT message turn, not the current one.

---

### Managing Sub-Agents

**Via Desktop UI:**
1. Open Settings → Sub-Agents tab
2. Click "+ New Agent" to create
3. Configure: name, description, system prompt, allowed tools
4. Test the agent with sample instructions
5. Enable/disable agents as needed

**Via REST API:**
```bash
# List all sub-agents
GET /agents

# Create a new sub-agent
POST /agents

# Update a sub-agent
PUT /agents/{agent_name}

# Delete a sub-agent
DELETE /agents/{agent_name}

# Test a sub-agent
POST /agents/{agent_name}/test
```

**Sub-Agent Configuration:**
- **Name**: Unique identifier (lowercase, underscores allowed)
- **Description**: Brief description of the agent's purpose
- **System Prompt**: Instructions defining the agent's behavior
- **Tools**: List of allowed tool names (empty = all tools)
- **Context Turns**: Number of conversation turns to include

---

## Adding New Tools

### 1. Create a Tool File

```python
# nymeria/tools/my_tool.py
from langchain_core.tools import tool

@tool
def my_tool(param: str) -> str:
    """
    Description shown to the LLM. Be specific about when to use this tool.

    Args:
        param: What this parameter does

    Returns:
        What the tool returns
    """
    # Implementation
    return f"[Success]: Result is {result}"
```

### 2. Export in `__init__.py`

```python
# nymeria/tools/__init__.py
from .my_tool import my_tool

ALL_TOOLS = [
    # ... existing tools
    my_tool,
]
```

### 3. Tool is Automatically Available

The tool is immediately available to the agent on next startup, or call `tools_reload()` for hot-reload.

---

## Tool Design Guidelines

| Guideline | Example |
|-----------|---------|
| **Clear docstrings** | The LLM uses these to decide when to call the tool |
| **Return strings** | All outputs should be serializable strings |
| **Handle errors gracefully** | Return `"[Error]: ..."`, don't raise exceptions |
| **Prefix results** | Use `[Success]`, `[Error]`, `[Info]` prefixes |
| **Be specific** | One tool = one job |
| **Log operations** | Use `logger.info()` for audit trail |
