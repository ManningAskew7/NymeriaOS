import type { Message } from '$lib/types';

/**
 * Serialize an assistant Message to markdown, including thinking blocks,
 * tool calls (with arguments and results), and response text.
 */
export function messageToMarkdown(message: Message): string {
  const sections: string[] = [];

  if (message.steps && message.steps.length > 0) {
    // Modern path: ordered steps
    for (const step of message.steps) {
      if (step.type === 'thinking' && step.content) {
        const quoted = step.content
          .split('\n')
          .map((line) => `> ${line}`)
          .join('\n');
        sections.push(`> *Thinking:*\n${quoted}`);
      } else if (step.type === 'tool_call') {
        let block = `### Tool: ${step.name || 'unknown'}`;
        if (step.arguments && Object.keys(step.arguments).length > 0) {
          block += `\n\n**Arguments:**\n\`\`\`json\n${JSON.stringify(step.arguments, null, 2)}\n\`\`\``;
        }
        if (step.result) {
          block += `\n\n**Result:**\n\`\`\`\n${step.result}\n\`\`\``;
        }
        sections.push(block);
      } else if (step.type === 'response' && step.content) {
        sections.push(step.content);
      }
    }
  } else {
    // Legacy path: intermediateContent + toolCalls + content
    if (message.intermediateContent) {
      const quoted = message.intermediateContent
        .split('\n')
        .map((line) => `> ${line}`)
        .join('\n');
      sections.push(`> *Thinking:*\n${quoted}`);
    }

    if (message.toolCalls) {
      for (const tc of message.toolCalls) {
        let block = `### Tool: ${tc.name}`;
        if (tc.arguments && Object.keys(tc.arguments).length > 0) {
          block += `\n\n**Arguments:**\n\`\`\`json\n${JSON.stringify(tc.arguments, null, 2)}\n\`\`\``;
        }
        if (tc.result) {
          block += `\n\n**Result:**\n\`\`\`\n${tc.result}\n\`\`\``;
        }
        sections.push(block);
      }
    }

    if (message.content) {
      sections.push(message.content);
    }
  }

  return sections.length > 0 ? sections.join('\n\n---\n\n') : '(empty message)';
}

/**
 * Extract only the final response text from a Message — the response
 * steps that come after the last thinking/tool_call step. Skips
 * interleaved preamble text between tool calls.
 */
export function messageToResponseText(message: Message): string {
  if (message.steps && message.steps.length > 0) {
    // Find the last non-response step index
    let lastNonResponseIdx = -1;
    for (let i = message.steps.length - 1; i >= 0; i--) {
      if (message.steps[i].type !== 'response') {
        lastNonResponseIdx = i;
        break;
      }
    }

    // Collect only response steps after the last thinking/tool_call
    const parts: string[] = [];
    for (let i = lastNonResponseIdx + 1; i < message.steps.length; i++) {
      const step = message.steps[i];
      if (step.type === 'response' && step.content) {
        parts.push(step.content);
      }
    }

    if (parts.length > 0) return parts.join('\n\n');
  }

  // Legacy fallback or no trailing response steps
  if (message.content) return message.content;
  return '(empty message)';
}
