"""One fingerprint format for secrets rendered back to a reader.

A masked secret has to answer exactly one question ("is the value I am holding
the one that is configured?") while being useless to replay. A fingerprint of
the head and tail does that: two different secrets almost never share one, and
the middle never appears.

This lives in ``core`` because two unrelated surfaces need the SAME format:
``api/routers/settings.py`` (the masked env read model behind ``GET
/settings/env``) and the trigger config renderers (``triggers/sources`` ->
the agent's ``trigger_info`` detail view and the REST ``TriggerResponse``,
backlog #307). Two spellings of a fingerprint would mean a value masked one way
on one surface and another way on the next, and the trigger round trip
(``restore_unchanged_secrets``) compares an incoming value against the mask it
handed out, so the two sides MUST agree byte for byte.
"""


def mask_secret_value(val: str) -> str:
    """Mask a secret value, showing the first 4 and last 3 characters.

    Short values reveal progressively less, because head-and-tail on a short
    string is most of the string: 4 to 10 characters show only the first 2 and
    the last 1, and 3 or fewer show nothing at all.
    """
    s = str(val)
    if len(s) <= 10:
        return s[:2] + "..." + s[-1:] if len(s) > 3 else "***"
    return s[:4] + "..." + s[-3:]
