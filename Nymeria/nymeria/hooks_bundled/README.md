# Bundled hook templates

Canned lifecycle-hook recipes that ship with Nymeria. Nothing here fires on
its own: a template becomes a real hook only when a user installs it
(`/hook install <id>`, `hook_config(action="install", ...)`, or
`POST /hooks/templates/{id}/install`), which runs the ordinary hook-create
path with the template's pre-filled fields. Installs are idempotent per
(user, template, scope binding).

One JSON file per template:

```json
{
  "id": "kebab-case-unique-id",
  "title": "Human title",
  "description": "One line shown in the template list",
  "notes": "Longer guidance shown on detail surfaces",
  "hook": {
    "name": "hook display name (defaults to the template id)",
    "event": "prompt_submit | pre_tool_use | post_tool_use | done",
    "action": "inject_context | notify | ... (any authoring-surface action)",
    "params": {"text": "..."},
    "matcher": null,
    "fire_conditions": [{"field": "...", "operator": "gte", "value": "85"}],
    "once": false,
    "scope": "global | thread",
    "enabled": true
  }
}
```

`core/hook_templates.py` scans this directory, dry-validates every file
against the real `HookDefinition` model, and skips invalid ones with a log
line (a broken file here is a packaging bug, never user data). Keep template
ids stable: they are recorded on installed hooks as provenance.
