/**
 * The rotating composer tip copy, shared by both apps so the drift gate can
 * see the DATA stay in sync while each InputHintTips component keeps its own
 * presentation (desktop: hover-pause, clip tooltip, elbow connector; mobile:
 * a plain single line). Style guide and change log:
 * nymeria-desktop/src/lib/components/chat/input-hint-tips.md.
 *
 * `desktopOnly` marks tips that reference desktop-only chrome (sidebars,
 * dashboard clicks, the context dot); mobile filters them out.
 */

export interface InputTip {
  text: string;
  desktopOnly?: boolean;
}

export const INPUT_TIPS: InputTip[] = [
  { text: 'Type / to browse slash commands.' },
  { text: 'Use /skill <name> to load a skill for this thread.' },
  { text: 'Use /kit <name> to bind a Skill Kit and its tools.' },
  { text: 'Each thread keeps its own model, tools, and memory.' },
  { text: 'Switch the model for a thread from its settings.' },
  { text: 'Schedule a task and Nymeria will run it on its own.' },
  { text: 'Set up triggers to start threads from email, RSS, webhooks, and HTTP polls.' },
  { text: 'Give a thread its own custom instructions in settings.' },
  { text: 'Create callable threads to hand work to a sub-agent.' },
  { text: 'Click the dot to minimise the context usage above.', desktopOnly: true },
  { text: 'Click any Global Dashboard item to open its thread.', desktopOnly: true },
  { text: 'Collapse either sidebar to free up room.', desktopOnly: true },
  { text: 'Skill Kits hot-load tools into the agent instantly.' },
  { text: 'Nymeria helps providers cache prompts to cut costs.' },
  { text: 'Nymeria can build new tools with tool_create mid-turn.' },
  { text: 'Use @<title> to message another thread in place.' },
  { text: 'Enable rag_search so Nymeria recalls details on demand.' },
  { text: 'Set the compaction threshold by tokens, not percentage.' },
  { text: 'Start a new thread when you switch tasks.' },
  { text: 'Use cheap models per thread for repetitive tasks.' },
  { text: 'Toggle dreaming to let a thread manage its own work.' },
  { text: 'Group threads into a team to hide them from the rest.' },
  { text: 'Use /orchestrate to spin up and manage an agent swarm.' },
];
