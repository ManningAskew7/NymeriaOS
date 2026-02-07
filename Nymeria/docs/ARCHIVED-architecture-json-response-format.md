# Nymeria Response Format Architecture

> **⚠️ DEPRECATED**: This document describes the OLD JSON-based response format that has been **replaced** by the tool-based visibility control system.
>
> **New Approach**: The LLM now streams responses naturally without any special formatting. To control visibility, it calls the `mute_response()` tool. See [tools.md](./tools.md#visibility-tools) for the current implementation.
>
> **Why the change?**
> - Simpler: No complex JSON parsing with multiple fallback strategies
> - More compatible: Works with any LLM without special output training
> - Explicit: Visibility control is an intentional tool call, not embedded formatting
> - Extensible: Easy to add future tools like `send_notification`
>
> This document is preserved for historical reference only.

---

# (ARCHIVED) Original JSON Response Format

## Overview

Nymeria is an AI agent that can operate in two modes:
1. **Interactive** - Responding to direct user messages
2. **Autonomous** - Executing scheduled tasks in the background

The challenge: When running autonomously, the agent needs to decide whether its response should be **shown to the user** or **logged silently**. We ~~currently~~ *previously* solved this by forcing the LLM to output structured JSON with a visibility field.

---

## The Problem Being Solved

When Nymeria executes a scheduled task autonomously:
- Sometimes the result is important and should appear in the chat thread
- Sometimes nothing meaningful happened and showing output would be noise

**Example scenarios:**

| Scheduled Task | What Happened | Ideal Visibility |
|----------------|---------------|------------------|
| "Check if any emails need attention" | Found 3 urgent emails | Show in thread + notify |
| "Check if any emails need attention" | No new emails | Log silently |
| "Summarize today's news" | Generated summary | Show in thread |
| "Monitor server status every hour" | All systems normal | Log silently |
| "Monitor server status every hour" | Server down! | Show in thread + notify |

The LLM needs to make this judgment call based on context.

---

## Current Implementation

### Forced JSON Response Format

Every LLM response must be formatted as:

```json
{"visibility": "full", "notify": false, "content": "The actual response text here"}
```

**Fields:**
- `visibility`: `"full"` (show in chat) or `"activity"` (log silently)
- `notify`: `true` to send push notification, `false` otherwise
- `summary`: Required if `notify=true`, shown in notification (max 100 chars)
- `content`: The actual response content (supports markdown)

### System Prompts

Three prompts control this behavior:

#### 1. Base Output Format (Always Included)

```
## Response Format

You MUST format ALL your final responses as a raw JSON object (NO markdown code blocks):

{"visibility": "full", "notify": false, "content": "Your response here"}

**Fields:**
- `visibility`: Controls where your response appears
  - `"full"`: Shown in the chat thread (normal response)
  - `"activity"`: Only logged to activity feed (silent/background)
- `notify`: If true, sends a push notification to the user
- `summary`: Required when notify=true, shown in notification (max 100 chars)
- `content`: Your actual response content (supports markdown)

CRITICAL: Output ONLY the raw JSON object. Do NOT wrap it in ```json``` code blocks.
Do NOT include any text before or after the JSON.
```

#### 2. Interactive Mode Rules (User Messages)

```
### Current Mode: Interactive (User Message)

You are responding to a direct user message. **Always use `"visibility": "full"`** -
the user is actively waiting for your response and expects to see it in the chat.

Example:
{"visibility": "full", "notify": false, "content": "Here's what you asked for..."}
```

#### 3. Autonomous Mode Rules (Scheduled Tasks)

```
### Current Mode: Autonomous (Scheduled Task)

You are executing a SCHEDULED TASK autonomously. The conversation history above
contains context about what the user wants.

**CRITICAL RULES**:
1. Only do what the scheduled task prompt asks for. Do NOT go beyond the scope.
2. If the user previously requested that you output to the thread, show results,
   or keep them informed - you MUST respect that preference and use "full" visibility.
3. If the task says "do Task #1", only do Task #1 - do NOT complete other tasks.

### Visibility Guidelines

**DEFAULT to "full"** (shown in thread) - use this unless you have a specific reason not to:
- You completed a task (even routine ones)
- You have any results or updates to share
- The user asked to be kept informed
- You performed any actions (tool calls)
- Something happened (success or failure)

**Use "activity"** (silent, logged only) ONLY when ALL of these are true:
- Nothing meaningful happened (e.g., "checked X, nothing changed")
- The user did NOT ask to see outputs
- No actions were taken
- This is purely a status check with no news

**Set "notify": true** when:
- Time-sensitive information
- Something went wrong that needs attention
- Important alerts
```

---

## Response Parsing (Backend)

The backend parses responses with multiple fallback strategies:

```python
class NymeriaResponse(BaseModel):
    """Structured response from autonomous agent execution."""
    visibility: Literal["activity", "full"] = "full"
    notify: bool = False
    summary: Optional[str] = None
    content: str
    category: Optional[Literal["status", "alert", "result", "question"]] = None


def parse_agent_response(raw_response: str) -> NymeriaResponse:
    """
    Parse agent response with multiple fallback strategies.

    Strategies (in order):
    1. Direct JSON parse - if entire response is valid JSON
    2. Extract JSON from markdown code block (```json ... ```)
    3. Look for embedded JSON object in surrounding text
    4. Fallback: treat entire response as full visibility content
    """
    # ... implementation with try/except chains
```

---

## How Visibility Routing Works

After parsing the response, the ticker (scheduler) routes based on visibility:

```python
if response.visibility == "activity":
    # Activity log only - minimal output
    log_activity(ActivityType.TASK_COMPLETED, response.content[:200], ...)

    # Show minimal console feedback
    console.print("[dim]Background task complete - nothing to report[/dim]")

else:  # visibility == "full"
    # Show in thread
    log_activity(ActivityType.TASK_COMPLETED, response.summary or response.content[:200], ...)

    # Display full response
    console.print("[bold green]Nymeria:[/bold green]")
    console.print(Markdown(response.content))

    # Send notification if requested
    if response.notify and response.summary:
        create_notification(user_id, summary=response.summary, ...)
```

---

## Pain Points & Limitations

### 1. LLMs Don't Always Follow JSON Formatting
- Sometimes wrap in ```json``` code blocks despite being told not to
- Sometimes add explanatory text before/after the JSON
- Sometimes malform the JSON (missing quotes, trailing commas)
- Requires complex fallback parsing with regex

### 2. Unnatural Output Format
- Forces the LLM to think about output structure rather than content
- The JSON wrapper feels like fighting against the LLM's natural behavior
- "content" field needs to handle markdown inside JSON strings (escaping issues)

### 3. Visibility Judgment is Subtle
- The rules for when to use "activity" vs "full" are nuanced
- LLMs tend to default to "full" even when "activity" would be appropriate
- Or they use "activity" for things the user actually wanted to see

### 4. Notify Decision is Hard
- When is something "urgent enough" to notify?
- LLMs are inconsistent about this judgment

### 5. Loss of Streaming Benefits
- Must wait for complete JSON to parse visibility
- Can't determine visibility until response is fully generated
- Streaming the content requires parsing partial JSON

### 6. Cognitive Overhead
- Every response requires the LLM to think about the wrapper format
- Takes tokens away from actual reasoning about the task

---

## Alternative Approaches to Consider

### A. Post-Processing Classification
Instead of forcing JSON output, let the LLM respond naturally. Then use a fast classifier to determine visibility:
- Pros: Natural LLM output, simpler prompts
- Cons: Extra API call, latency, classifier accuracy

### B. Separate Metadata Call
Two-step process: (1) LLM generates response naturally, (2) Ask LLM to classify its own response
- Pros: Separation of concerns
- Cons: Double the API calls, latency

### C. Tool-Based Signaling
Instead of JSON format, give the LLM a tool to call when it wants to signal visibility:
```python
@tool
def set_response_visibility(visibility: str, notify: bool, summary: str = None):
    """Call this to set how your response should be displayed."""
```
- Pros: Fits naturally into tool-calling paradigm
- Cons: Tool call overhead, must be called before response

### D. Streaming Metadata Prefix
Allow the LLM to output a simple prefix before the response:
```
[ACTIVITY] Nothing to report, all systems normal.
[FULL] Here's the summary you requested...
[NOTIFY:urgent] Server is down!
```
- Pros: Simple to parse, minimal overhead
- Cons: Still unnatural, prefix detection issues

### E. Separate Visibility Prompt
After the response is generated, make a quick follow-up asking just about visibility:
"Based on your response above, should this be shown in the thread (full) or logged silently (activity)?"
- Pros: Main response is natural
- Cons: Extra API call

### F. Context-Based Defaults
Set visibility based on what happened during execution rather than LLM judgment:
- Used tools → full visibility
- No tools used → activity
- Error occurred → full + notify
- Pros: Deterministic, no LLM judgment needed
- Cons: Loses nuance (checking something important with no tools still matters)

---

## Questions for Discussion

1. Is forcing structured JSON output the right approach, or is there a better way?

2. Should visibility be an LLM decision at all, or should it be determined by the system based on execution context?

3. How can we maintain streaming while still having visibility control?

4. What's the right balance between LLM agency (deciding visibility) and system control (consistent behavior)?

5. Are there patterns from other agentic systems that handle this better?

---

## File References

For implementation details, see:
- `nymeria/core/agent.py` (lines 49-126) - System prompts
- `nymeria/core/response_handler.py` - Response parsing
- `nymeria/core/ticker.py` (lines 290-470) - Visibility routing
