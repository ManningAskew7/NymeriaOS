/**
 * The connection-override slice (base_url, api_key) of the mobile thread
 * settings Save.
 *
 * The form holds both as plain strings seeded from the saved config
 * (`saved ?? ''`), so the form alone cannot tell a stored "" from null, and
 * they are distinct values: on an Anthropic thread `base_url: ""` is the
 * direct API while null inherits the global route (desktop's "Anthropic
 * (direct)" versus "(via proxy)"), and the backend treats a changed route
 * value as a route edit (it ends an active fallback hold). The Save used to
 * send `field || null`, and only on the OpenAI-compatible path, so any
 * unrelated save flipped a stored "" to null (and dropped a saved key on a
 * provider without that path). Desktop round-trips the value through its
 * display-provider mapping (`utils/threadLlmSeed.ts`); mobile has no such
 * mapping, so the rule here is explicit:
 *
 * - unedited, on an unchanged provider and route: send the saved value back
 *   exactly ("" stays "", null stays null);
 * - otherwise, when the override applies to the chosen provider and route
 *   (the field is shown): the form's value, blank meaning inherit (null);
 * - otherwise null (a provider the override does not apply to).
 */
export function connectionOverrideForSave(args: {
  /** The form field as the user left it. */
  field: string;
  /** The value the thread config currently holds. */
  saved: string | null | undefined;
  /** Provider and provider route are the saved ones. */
  routeUnchanged: boolean;
  /** The override applies to the chosen provider and route (field shown). */
  applies: boolean;
}): string | null {
  const { field, saved, routeUnchanged, applies } = args;
  if (routeUnchanged && field === (saved ?? '')) return saved ?? null;
  if (!applies) return null;
  return field || null;
}
