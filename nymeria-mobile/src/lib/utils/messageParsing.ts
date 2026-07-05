// Shared user-message parsing for the chat surface. Extracted from
// MessageBubble so the long-press action sheet (Edit and resend) prefills the
// composer with exactly the text the bubble displays (backend metadata
// stripped, compaction summaries pulled out), keeping the two in lockstep.

import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
import { threadsStore } from '$lib/stores/threads.svelte';

// Pattern to detect time context prefix (added by backend to all messages)
// Format: [Current Time: ...]\n[Trigger: ...]\n\n{actual message}
const TIME_CONTEXT_PATTERN = /^\[(?:Current )?Time:[^\]]+\]\n\[Trigger:[^\]]+\]\n\n/;

// Pattern to detect smartwatch trigger source
const SMARTWATCH_TRIGGER_PATTERN = /\[Trigger: Smartwatch[^\]]*\]/;

// Pattern to detect autonomous wake-up messages (internal system triggers - should be hidden)
// Format: [Current Time: ...]\n[Trigger: Autonomous Wake-up...]\n\nWork on TODO ...
const AUTONOMOUS_WAKEUP_PATTERN = /^\[(?:Current )?Time:[^\]]+\]\n\[Trigger: Autonomous Wake-up[^\]]*\]\n\n/;

// Pattern to detect compaction system request (should be hidden)
const COMPACTION_REQUEST_PATTERN = /^\*\*System Request: Context Compaction\*\*/;

// Pattern to detect manual /compact context summary suffix
// Format: {user message}\n\n---\n*This conversation is resuming...*\n\n{summary}\n\n---
const MANUAL_COMPACT_PATTERN = /\n\n---\n\*This conversation is resuming from a previous session that exceeded context limits\. Summary of prior context:\*\n\n([\s\S]*?)\n\n---$/;

// Pattern to detect auto-compact message
// Format: [Auto-compact: ...]\n\n---\n*Context Summary (auto-compact):*\n\n{summary}\n\n---\n\nContinue...
const AUTO_COMPACT_PATTERN = /^\[Auto-compact: Context limit reached, conversation summarized\]\n\n---\n\*Context Summary \(auto-compact\):\*\n\n([\s\S]*?)\n\n---\n\n[\s\S]*$/;

export interface ParsedUserMessage {
  text: string;
  contextSummary: string | null;
  hidden: boolean;
  isSmartwatch: boolean;
  /** True when text is the auto-compact placeholder, not the stored prompt. */
  isAutoCompact: boolean;
}

// Parse user message to extract actual content, context summary, and hidden flag
export function parseUserMessage(content: string): ParsedUserMessage {
  let text = content;
  let contextSummary: string | null = null;

  // Detect smartwatch trigger before stripping metadata
  const isSmartwatch = SMARTWATCH_TRIGGER_PATTERN.test(text);

  // Check if this is an autonomous wake-up message (should be hidden entirely)
  if (AUTONOMOUS_WAKEUP_PATTERN.test(text)) {
    return { text: '', contextSummary: null, hidden: true, isSmartwatch: false, isAutoCompact: false };
  }

  // Check if this is a compaction system request (should be hidden entirely)
  if (COMPACTION_REQUEST_PATTERN.test(text)) {
    return { text: '', contextSummary: null, hidden: true, isSmartwatch: false, isAutoCompact: false };
  }

  // Strip time context prefix unless user opted to show metadata
  const tid = threadsStore.currentThreadId;
  const showMeta = tid ? threadConfigStore.getConfig(tid)?.showPromptMetadata : false;
  if (!showMeta) {
    text = text.replace(TIME_CONTEXT_PATTERN, '');
  }

  // Check for auto-compact message format
  const autoCompactMatch = text.match(AUTO_COMPACT_PATTERN);
  if (autoCompactMatch) {
    contextSummary = autoCompactMatch[1].trim();
    text = '[Auto-compact: Thread summarized]';
    return { text, contextSummary, hidden: false, isSmartwatch, isAutoCompact: true };
  }

  // Check for manual /compact summary suffix
  const manualCompactMatch = text.match(MANUAL_COMPACT_PATTERN);
  if (manualCompactMatch) {
    contextSummary = manualCompactMatch[1].trim();
    text = text.replace(MANUAL_COMPACT_PATTERN, '').trim();
  }

  return { text, contextSummary, hidden: false, isSmartwatch, isAutoCompact: false };
}
