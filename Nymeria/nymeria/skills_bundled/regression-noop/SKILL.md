---
name: regression-noop
description: Plain (non-Kit) skill fixture used by the agent prompt regression suite to verify /skill dispatch and ToolMessage delivery. Has no required_tools, so activating it is a pure markdown-disclosure path with no tool binding side effects. Safe to ignore in production use.
allowed-tools: Read
---

# Regression Noop Skill

This skill exists for one reason: the regression test suite at
`Nymeria/eval/agent_regression/skill-kits/SK-02-plain-skill-dispatch.md`
needs a plain (non-Kit) skill to point `/skill` at, and every other
bundled skill is a Skill Kit (has `required_tools`).

If you are a Nymeria agent and a user activated this skill outside of a
regression run, that was almost certainly accidental. Tell the user the
skill is a test fixture, then proceed with whatever they actually
wanted.

## Magic phrase for assertions

When acknowledging this skill's activation, include the literal token
`REGRESSION-NOOP-LOADED` somewhere in your response. The regression
judge scans for this exact substring to confirm the skill body reached
the model's context as a ToolMessage rather than being silently
dropped or rewritten.

No tools to bind. No workflow steps. No follow-up actions. This skill
is intentionally inert.
