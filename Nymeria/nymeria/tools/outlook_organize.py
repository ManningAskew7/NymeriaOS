"""Outlook organisation tools (Microsoft Graph): folders, master categories,
follow-up flags, inbox rules, mailbox settings and auto-reply, Focused/Other
sender overrides, and List-Unsubscribe handling.

The message tools live in :mod:`outlook_email`; the shared plumbing (account
selection, ``graph_request``, ``$batch``, folder resolution, the scope gate and
the multi-target contract) lives in :mod:`outlook_graph` and is used here
rather than re-implemented. Every tool acts on the signed-in account's own
mailbox unless ``mailbox`` names a shared mailbox that account has Full Access
to (Graph ``/users/{upn}``).

Permissions: folder, flag and Focused-override tools need only the
``Mail.ReadWrite`` scope every connected account carries. Master categories,
inbox rules, mailbox settings and auto-reply need ``MailboxSettings.ReadWrite``;
those tools call ``require_scopes`` first so an account connected before that
scope joined the registry gets a reconnect instruction instead of a bare 403.

Egress: every Graph call goes through :mod:`outlook_graph` (module-constant
host). The one address that does NOT come from a module constant is the
RFC 8058 one-click unsubscribe URL, which arrives in a message header and is
therefore attacker-supplied; it is issued through ``policy_http_client`` plus
``request_with_policy`` so the private-address block and the DNS pin apply
(``tests/test_service_integration_egress.py``). Graph's beta ``unsubscribe``
message action is deliberately not used: this module stays on v1.0.
"""
from .registry import ToolGroup, register_tool_group

import json
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.http_policy import policy_http_client as _http_client
from .outlook_graph import (
    FOLDER_ALIASES,
    SCOPE_MAILBOX_SETTINGS,
    batch_patch_messages,
    graph_batch,
    graph_request,
    require_scopes,
    resolve_folder,
    resolve_targets,
    summarize_batch,
)
from .service_integration_base import request_with_policy as _request_with_policy
from .utils import get_user_id

logger = logging.getLogger(__name__)

_FOLDER_SELECT = "id,displayName,childFolderCount,unreadItemCount,totalItemCount"
_RULES_ENDPOINT = "/me/mailFolders/inbox/messageRules"
_OVERRIDES_ENDPOINT = "/me/inferenceClassification/overrides"
_CATEGORIES_ENDPOINT = "/me/outlook/masterCategories"

# Well-known folders a mailbox may carry, in the order they are listed. Graph
# v1.0 does not expose ``wellKnownName`` on a folder, so the tree lookup asks
# for each of these by name (one ``$batch``) and tags the matching ids.
_WELL_KNOWN_FOLDERS: tuple[str, ...] = (
    "inbox", "drafts", "sentitems", "deleteditems", "junkemail", "archive",
    "outbox", "clutter", "conversationhistory", "scheduled",
)
_WELL_KNOWN_SEGMENTS = set(FOLDER_ALIASES.values())

_MAX_FOLDER_DEPTH = 4

# Graph ``categoryColor`` presets and the colour Outlook maps each one to
# (learn.microsoft.com, outlookCategory resource, v1.0).
_PRESET_COLOURS: dict[str, str] = {
    "none": "no colour",
    "preset0": "red",
    "preset1": "orange",
    "preset2": "brown",
    "preset3": "yellow",
    "preset4": "green",
    "preset5": "teal",
    "preset6": "olive",
    "preset7": "blue",
    "preset8": "purple",
    "preset9": "cranberry",
    "preset10": "steel",
    "preset11": "dark steel",
    "preset12": "gray",
    "preset13": "dark gray",
    "preset14": "black",
    "preset15": "dark red",
    "preset16": "dark orange",
    "preset17": "dark brown",
    "preset18": "dark yellow",
    "preset19": "dark green",
    "preset20": "dark teal",
    "preset21": "dark olive",
    "preset22": "dark blue",
    "preset23": "dark purple",
    "preset24": "dark cranberry",
}
_COLOUR_TO_PRESET: dict[str, str] = {
    re.sub(r"[\s_\-]+", "", colour): preset for preset, colour in _PRESET_COLOURS.items()
}
_COLOUR_TO_PRESET.update({"grey": "preset12", "darkgrey": "preset13", "nocolour": "none", "nocolor": "none"})

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_ANGLE_ADDR_RE = re.compile(r"^(.*?)\s*<([^<>]+)>\s*$")
_TAG_RE = re.compile(r"<[^>]+>")

# messageRulePredicates and messageRuleActions field names with the value shape
# each takes (learn.microsoft.com, v1.0). Unknown keys are refused before any
# request so a typo cannot create a rule that silently matches nothing.
_PREDICATE_FIELDS: dict[str, str] = {
    "bodyContains": "strings",
    "bodyOrSubjectContains": "strings",
    "categories": "strings",
    "headerContains": "strings",
    "recipientContains": "strings",
    "senderContains": "strings",
    "subjectContains": "strings",
    "fromAddresses": "recipients",
    "sentToAddresses": "recipients",
    "hasAttachments": "bool",
    "isApprovalRequest": "bool",
    "isAutomaticForward": "bool",
    "isAutomaticReply": "bool",
    "isEncrypted": "bool",
    "isMeetingRequest": "bool",
    "isMeetingResponse": "bool",
    "isNonDeliveryReport": "bool",
    "isPermissionControlled": "bool",
    "isReadReceipt": "bool",
    "isSigned": "bool",
    "isVoicemail": "bool",
    "notSentToMe": "bool",
    "sentCcMe": "bool",
    "sentOnlyToMe": "bool",
    "sentToMe": "bool",
    "sentToOrCcMe": "bool",
    "importance": "enum",
    "sensitivity": "enum",
    "messageActionFlag": "enum",
    "withinSizeRange": "object",
}
_ACTION_FIELDS: dict[str, str] = {
    "assignCategories": "strings",
    "copyToFolder": "folder",
    "moveToFolder": "folder",
    "delete": "bool",
    "markAsRead": "bool",
    "permanentDelete": "bool",
    "stopProcessingRules": "bool",
    "forwardAsAttachmentTo": "recipients",
    "forwardTo": "recipients",
    "redirectTo": "recipients",
    "markImportance": "enum",
}

_AUTO_REPLY_STATUSES = {"disabled": "disabled", "alwaysenabled": "alwaysEnabled", "scheduled": "scheduled"}
_EXTERNAL_AUDIENCES = {"none": "none", "contactsonly": "contactsOnly", "contacts": "contactsOnly", "all": "all"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _gate(
    user_id: str,
    account_id: Optional[str],
    mailbox: Optional[str],
    *,
    purpose: str,
    settings: bool = False,
) -> Optional[str]:
    """The up-front permission check every tool here runs.

    ``settings`` adds ``MailboxSettings.ReadWrite``; the ``.Shared`` scope is
    added by ``require_scopes`` itself whenever ``mailbox`` is set. Returns the
    reconnect text to hand back, or ``None`` when the account may proceed.
    """
    needed = [SCOPE_MAILBOX_SETTINGS] if settings else []
    return require_scopes(user_id, needed, account_id, mailbox=mailbox, purpose=purpose)


def _mailbox_time_zone(user_id: str, account_id: Optional[str], mailbox: Optional[str]) -> str:
    """The mailbox's configured time zone, ``UTC`` when it cannot be read.

    A token without ``MailboxSettings.Read`` gets a 403 here; a flag due date is
    still worth setting, so the fallback is silent by design.
    """
    ok, result = graph_request(
        user_id, "GET", "/me/mailboxSettings", account_id=account_id,
        params={"$select": "timeZone"}, mailbox=mailbox,
    )
    if ok and isinstance(result, dict) and result.get("timeZone"):
        return str(result["timeZone"])
    return "UTC"


def _graph_datetime(value: str, time_zone: str, field: str) -> tuple[bool, Any]:
    """Parse ``YYYY-MM-DD`` or an ISO datetime into a Graph ``dateTimeTimeZone``.

    A value carrying an offset (or ``Z``) is converted to UTC and sent with
    ``timeZone: UTC``, because a wall-clock time plus a mailbox zone AND an
    offset would describe two different instants.
    """
    raw = (value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False, f"{field} must be YYYY-MM-DD or an ISO datetime such as 2026-09-20T09:00 (got '{value}')."
    zone = time_zone
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        zone = "UTC"
    return True, {"dateTime": parsed.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": zone}


def _recipient(value: Any) -> Optional[dict]:
    """One Graph recipient from ``addr``, ``Name <addr>`` or a recipient dict."""
    if isinstance(value, dict):
        inner = value.get("emailAddress") if isinstance(value.get("emailAddress"), dict) else value
        address = str(inner.get("address", "")).strip()
        if not address:
            return None
        out: dict[str, Any] = {"address": address}
        if inner.get("name"):
            out["name"] = str(inner["name"])
        return {"emailAddress": out}
    text = str(value or "").strip()
    if not text:
        return None
    match = _ANGLE_ADDR_RE.match(text)
    if match:
        name, address = match.group(1).strip().strip('"'), match.group(2).strip()
        entry: dict[str, Any] = {"address": address}
        if name:
            entry["name"] = name
        return {"emailAddress": entry}
    return {"emailAddress": {"address": text}}


def _recipient_list(value: Any) -> list[dict]:
    items: list[Any]
    if isinstance(value, str):
        items = [p for p in value.split(",") if p.strip()]
    elif isinstance(value, list):
        items = value
    else:
        items = [value]
    return [r for r in (_recipient(v) for v in items) if r]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _addresses(recipients: Any) -> str:
    out = []
    for r in recipients or []:
        addr = ((r or {}).get("emailAddress") or {}).get("address")
        if addr:
            out.append(str(addr))
    return ", ".join(out)


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text or "")).strip()


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


def _child_listing_url(folder_id: str) -> str:
    return f"/me/mailFolders/{folder_id}/childFolders?$top=250&$select={_FOLDER_SELECT}"


def _folder_line(folder: dict, alias: Optional[str], indent: int, expanded: bool) -> str:
    tag = f" [{alias}]" if alias else ""
    unread = folder.get("unreadItemCount") or 0
    total = folder.get("totalItemCount") or 0
    line = f"{'  ' * indent}{folder.get('displayName')}{tag} ({unread} unread, {total} total) id={folder.get('id')}"
    children = folder.get("childFolderCount") or 0
    if children and not expanded:
        line += f" [+{children} subfolder(s) not shown]"
    return line


def _render_tree(folders: list[dict], aliases: dict[str, str], indent: int, lines: list[str]) -> None:
    for folder in folders:
        expanded = "_children" in folder
        lines.append(_folder_line(folder, aliases.get(str(folder.get("id"))), indent, expanded))
        if folder.get("_error"):
            lines.append(f"{'  ' * (indent + 1)}(could not list subfolders: {folder['_error']})")
        elif expanded:
            _render_tree(folder["_children"], aliases, indent + 1, lines)


@tool
def outlook_list_folders(
    parent: str = "",
    depth: int = 2,
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Show the mail folder tree with ids, unread and total counts, and well-known names.

    Lists the folders under the mailbox root (or under `parent`) and their
    subfolders down to `depth` levels, one line per folder: display name, the
    well-known name in brackets when Graph has one (inbox, sentitems,
    deleteditems, junkemail, archive, drafts...), unread and total item counts,
    and the folder id. A folder whose subfolders fall below `depth` shows how many
    were not listed. Folder names, slash paths and ids shown here are accepted by
    every other Outlook tool's `folder` argument. Acts on the signed-in account's
    own mailbox by default. Needs the Mail.ReadWrite permission the connected
    account already carries.

    Args:
        parent: Start below this folder instead of the root: a well-known name
                (inbox, archive...), a custom folder's display name ("Clients"),
                a slash path from the root ("Clients/Acme"), or a folder id.
                Empty (default) lists from the root. An unknown name is refused.
        depth: How many levels to show (default 2, minimum 1, maximum 4). Level 1
               is the folders directly under the start point.
        account_id: Microsoft account ID (optional; defaults to the thread's bound
                    or only connected account)
        mailbox: Address (UPN) of a SHARED mailbox to read instead of the
                 signed-in account's own mailbox (default). Needs Full Access plus
                 the Mail.ReadWrite.Shared permission on the connected account.

    Returns:
        The indented folder tree, or an error naming the folder or permission problem.
    """
    user_id = get_user_id(config)
    gate = _gate(user_id, account_id, mailbox, purpose="listing folders")
    if gate:
        return f"[Error]: {gate}"
    depth = min(max(1, int(depth)), _MAX_FOLDER_DEPTH)

    if parent.strip():
        ok, segment = resolve_folder(user_id, parent, account_id, mailbox=mailbox)
        if not ok:
            return f"[Error]: {segment}"
        endpoint = f"/me/mailFolders/{segment}/childFolders"
        where = f"'{parent.strip()}'"
    else:
        endpoint = "/me/mailFolders"
        where = "the mailbox root"

    ok, result = graph_request(
        user_id, "GET", endpoint, account_id=account_id,
        params={"$top": 250, "$select": _FOLDER_SELECT}, mailbox=mailbox,
    )
    if not ok:
        return f"[Error]: {result}"
    top = list(result.get("value") or [])
    if not top:
        return f"[Info]: No folders under {where}."

    aliases: dict[str, str] = {}
    level = top
    for current_depth in range(1, depth + 1):
        requests: list[dict] = []
        if current_depth == 1 and not parent.strip():
            requests.extend(
                {"id": f"wk:{name}", "method": "GET", "url": f"/me/mailFolders/{name}?$select=id"}
                for name in _WELL_KNOWN_FOLDERS
            )
        expandable = [f for f in level if (f.get("childFolderCount") or 0) > 0] if current_depth < depth else []
        requests.extend(
            {"id": f"child:{i}", "method": "GET", "url": _child_listing_url(str(f.get("id")))}
            for i, f in enumerate(expandable)
        )
        if not requests:
            break
        outcomes = graph_batch(user_id, requests, account_id=account_id, mailbox=mailbox)
        next_level: list[dict] = []
        for req, outcome in zip(requests, outcomes):
            rid = str(req["id"])
            if rid.startswith("wk:"):
                if outcome["ok"] and isinstance(outcome["body"], dict) and outcome["body"].get("id"):
                    aliases[str(outcome["body"]["id"])] = rid[3:]
                continue
            folder = expandable[int(rid.split(":", 1)[1])]
            if outcome["ok"]:
                folder["_children"] = list(((outcome["body"] or {}).get("value")) or [])
                next_level.extend(folder["_children"])
            else:
                folder["_children"] = []
                folder["_error"] = outcome["error"]
        level = next_level

    lines = [f"[Success]: Folder tree under {where} (depth {depth}):"]
    _render_tree(top, aliases, 0, lines)
    return "\n".join(lines)


@tool
def outlook_manage_folder(
    action: str,
    name: str = "",
    parent: str = "",
    folder: str = "",
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Create, rename, or delete a mail folder.

    `create` makes a folder named `name` under the root, or under `parent`.
    `rename` gives the folder identified by `folder` the new display name `name`.
    `delete` removes the folder identified by `folder` together with everything
    in it. Treat that as permanent from this tool's point of view: the folder
    does not reappear under Deleted Items (verified live), so move any mail you
    want to keep out first with outlook_move_email. Well-known folders (inbox, sent items, drafts, deleted
    items, junk, archive, outbox) are refused for rename and delete by name;
    Graph itself refuses them by id. Creating a folder never moves mail:
    outlook_move_email files messages into it. Acts on the signed-in account's
    own mailbox by default. Needs the Mail.ReadWrite permission the connected
    account already carries.

    Args:
        action: "create", "rename", or "delete".
        name: The folder name to create, or the new name for a rename.
        parent: For create only: the folder to create under, as a well-known name
                (inbox, archive), a display name, a slash path ("Clients/Acme"),
                or a folder id. Empty (default) creates at the mailbox root.
        folder: For rename and delete: the folder to act on, as a display name,
                slash path, or folder id. A name matching two folders is refused
                with both ids listed.
        account_id: Microsoft account ID (optional)
        mailbox: Address (UPN) of a SHARED mailbox to act on instead of the
                 signed-in account's own mailbox (default). Needs Full Access plus
                 the Mail.ReadWrite.Shared permission.

    Returns:
        Success with the folder id, or an error naming the folder or permission problem.
    """
    user_id = get_user_id(config)
    act = action.strip().lower()
    if act not in ("create", "rename", "delete"):
        return "[Error]: action must be 'create', 'rename', or 'delete'."
    gate = _gate(user_id, account_id, mailbox, purpose="folder management")
    if gate:
        return f"[Error]: {gate}"

    if act == "create":
        if not name.strip():
            return "[Error]: name is required to create a folder."
        endpoint = "/me/mailFolders"
        where = "the mailbox root"
        if parent.strip():
            ok, segment = resolve_folder(user_id, parent, account_id, mailbox=mailbox)
            if not ok:
                return f"[Error]: {segment}"
            endpoint = f"/me/mailFolders/{segment}/childFolders"
            where = f"'{parent.strip()}'"
        ok, result = graph_request(
            user_id, "POST", endpoint, account_id=account_id,
            json_data={"displayName": name.strip()}, mailbox=mailbox,
        )
        if not ok:
            return f"[Error]: {result}"
        return f"[Success]: Folder '{name.strip()}' created under {where}. id={result.get('id', '')}"

    if not folder.strip():
        return f"[Error]: folder is required to {act} a folder."
    if act == "rename" and not name.strip():
        return "[Error]: name (the new display name) is required to rename a folder."
    if folder.strip().lower() in FOLDER_ALIASES:
        return (
            f"[Error]: '{folder.strip()}' is a well-known folder; Outlook does not allow it to be "
            f"{'renamed' if act == 'rename' else 'deleted'}."
        )
    ok, segment = resolve_folder(user_id, folder, account_id, mailbox=mailbox)
    if not ok:
        return f"[Error]: {segment}"
    if segment in _WELL_KNOWN_SEGMENTS:
        return f"[Error]: '{folder.strip()}' is a well-known folder and cannot be {act}d."

    if act == "rename":
        ok, result = graph_request(
            user_id, "PATCH", f"/me/mailFolders/{segment}", account_id=account_id,
            json_data={"displayName": name.strip()}, mailbox=mailbox,
        )
        if not ok:
            return f"[Error]: {result}"
        return f"[Success]: Folder '{folder.strip()}' renamed to '{name.strip()}'. id={segment}"

    ok, result = graph_request(
        user_id, "DELETE", f"/me/mailFolders/{segment}", account_id=account_id, mailbox=mailbox,
    )
    if not ok:
        return f"[Error]: {result}"
    return f"[Success]: Folder '{folder.strip()}' and its contents deleted."


# ---------------------------------------------------------------------------
# Master categories
# ---------------------------------------------------------------------------


def _colour_to_preset(colour: str) -> Optional[str]:
    """``preset7``, ``blue``, ``Dark Blue``, ``dark-blue``, ``grey`` and ``none`` all resolve."""
    key = re.sub(r"[\s_\-]+", "", (colour or "").strip().lower())
    if not key:
        return None
    if key in _PRESET_COLOURS:
        return key
    return _COLOUR_TO_PRESET.get(key)


def _colour_label(preset: str) -> str:
    if preset == "none":
        return "none (no colour)"
    colour = _PRESET_COLOURS.get(preset)
    return f"{colour} ({preset})" if colour else preset


def _accepted_colours() -> str:
    names = [c for p, c in _PRESET_COLOURS.items() if p != "none"]
    return "none, preset0 to preset24, or a colour name: " + ", ".join(names) + " (grey is accepted for gray)"


def _find_category(
    user_id: str, name: str, account_id: Optional[str], mailbox: Optional[str]
) -> tuple[bool, Any]:
    """``(True, category_dict)`` for a master-list name or id, else ``(False, error)``."""
    wanted = name.strip()
    if _GUID_RE.match(wanted):
        return True, {"id": wanted, "displayName": wanted}
    ok, result = graph_request(user_id, "GET", _CATEGORIES_ENDPOINT, account_id=account_id, mailbox=mailbox)
    if not ok:
        return False, str(result)
    for cat in result.get("value") or []:
        if str(cat.get("displayName", "")).strip().lower() == wanted.lower():
            return True, cat
    return False, f"No category named '{wanted}' in the master list. outlook_list_categories shows what exists."


@tool
def outlook_list_categories(
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    List the mailbox's master category list: every defined category with its colour.

    Categories are the coloured labels Outlook shows on messages (and events,
    contacts, tasks). This is the master list of definitions; a message can carry
    a category name that is not defined here, but it then renders without a
    colour. outlook_manage_categories creates, recolours and deletes entries;
    outlook_set_category applies them to messages. Acts on the signed-in
    account's own mailbox by default. Needs the MailboxSettings.ReadWrite
    permission; an account connected without it is told how to reconnect.

    Args:
        account_id: Microsoft account ID (optional; defaults to the thread's bound
                    or only connected account)
        mailbox: Address (UPN) of a SHARED mailbox to read instead of the
                 signed-in account's own mailbox (default). Needs Full Access plus
                 the Mail.ReadWrite.Shared permission.

    Returns:
        One line per category (name, colour, preset constant, id), or a permission error.
    """
    user_id = get_user_id(config)
    gate = _gate(user_id, account_id, mailbox, purpose="listing master categories", settings=True)
    if gate:
        return f"[Error]: {gate}"
    ok, result = graph_request(user_id, "GET", _CATEGORIES_ENDPOINT, account_id=account_id, mailbox=mailbox)
    if not ok:
        return f"[Error]: {result}"
    categories = list(result.get("value") or [])
    if not categories:
        return '[Info]: The master category list is empty. outlook_manage_categories(action="create") adds one.'
    lines = [f"[Success]: {len(categories)} categories in the master list:"]
    for cat in categories:
        lines.append(f"  - {cat.get('displayName')}: {_colour_label(str(cat.get('color', 'none')))} id={cat.get('id')}")
    return "\n".join(lines)


@tool
def outlook_manage_categories(
    action: str,
    name: str,
    color: str = "",
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Create, recolour, or delete a category in the mailbox's master list.

    `create` adds `name` with `color`; `update` changes the colour of an existing
    category (Graph does not allow renaming: delete and recreate to rename);
    `delete` removes the definition, after which messages already tagged keep
    the label text but lose its colour. Applying a category to messages is
    outlook_set_category's job. Acts on the signed-in account's own mailbox by
    default. Needs the MailboxSettings.ReadWrite permission; an account connected
    without it is told how to reconnect.

    Args:
        action: "create", "update", or "delete".
        name: The category's display name (unique in the master list; matched
              case-insensitively for update and delete). A category id (GUID) is
              also accepted for update and delete.
        color: A Graph preset constant ("preset0" to "preset24", or "none") or a
               colour name: red, orange, brown, yellow, green, teal, olive, blue,
               purple, cranberry, steel, dark steel, gray (or grey), dark gray,
               black, dark red, dark orange, dark brown, dark yellow, dark green,
               dark teal, dark olive, dark blue, dark purple, dark cranberry.
               Required for update; defaults to "none" (no colour) for create;
               ignored for delete. An unknown value is refused with the list.
        account_id: Microsoft account ID (optional)
        mailbox: Address (UPN) of a SHARED mailbox to act on instead of the
                 signed-in account's own mailbox (default). Needs Full Access plus
                 the Mail.ReadWrite.Shared permission.

    Returns:
        Success naming the category, colour and id, or an error.
    """
    user_id = get_user_id(config)
    act = action.strip().lower()
    if act not in ("create", "update", "delete"):
        return "[Error]: action must be 'create', 'update', or 'delete'."
    if not name.strip():
        return "[Error]: name is required."

    preset: Optional[str] = None
    if act != "delete":
        if act == "update" and not color.strip():
            return "[Error]: color is required to update a category (names cannot be changed in Graph)."
        preset = _colour_to_preset(color) if color.strip() else "none"
        if preset is None:
            return f"[Error]: Unknown colour '{color.strip()}'. Accepted: {_accepted_colours()}."

    gate = _gate(user_id, account_id, mailbox, purpose="category management", settings=True)
    if gate:
        return f"[Error]: {gate}"

    if act == "create":
        ok, result = graph_request(
            user_id, "POST", _CATEGORIES_ENDPOINT, account_id=account_id,
            json_data={"displayName": name.strip(), "color": preset}, mailbox=mailbox,
        )
        if not ok:
            return f"[Error]: {result}"
        return f"[Success]: Category '{name.strip()}' created with colour {_colour_label(str(preset))}. id={result.get('id', '')}"

    found, category = _find_category(user_id, name, account_id, mailbox)
    if not found:
        return f"[Error]: {category}"
    label = category.get("displayName") or name.strip()
    if act == "update":
        ok, result = graph_request(
            user_id, "PATCH", f"{_CATEGORIES_ENDPOINT}/{category['id']}", account_id=account_id,
            json_data={"color": preset}, mailbox=mailbox,
        )
        if not ok:
            return f"[Error]: {result}"
        return (
            f"[Success]: Category '{label}' colour changed to {_colour_label(str(preset))}. "
            "(Graph cannot rename a category; delete and recreate to change the name.)"
        )

    ok, result = graph_request(
        user_id, "DELETE", f"{_CATEGORIES_ENDPOINT}/{category['id']}", account_id=account_id, mailbox=mailbox,
    )
    if not ok:
        return f"[Error]: {result}"
    return (
        f"[Success]: Category '{label}' deleted from the master list. Messages already tagged "
        "keep the label text but lose its colour."
    )


# ---------------------------------------------------------------------------
# Follow-up flags
# ---------------------------------------------------------------------------


@tool
def outlook_flag_email(
    email_id: str = "",
    email_ids: str = "",
    conversation_id: str = "",
    status: str = "flagged",
    due: str = "",
    start: str = "",
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Flag email(s) for follow-up (optionally with dates), mark the flag complete, or clear it.

    Target one message (email_id), several (email_ids), or a whole conversation
    (conversation_id, every message in the thread outside Deleted Items). Up to
    50 messages per call; the result reports each failure individually. Dates
    are sent in the mailbox's configured time zone (UTC when that setting cannot
    be read); a value carrying its own offset or "Z" is converted to UTC. Graph
    requires a start date whenever a due date is set, so `due` alone also sets
    `start` to the same day. Acts on the signed-in account's own mailbox by
    default. Needs the Mail.ReadWrite permission the connected account already
    carries.

    Args:
        email_id: ID of the email to flag
        email_ids: Comma-separated email IDs to flag together (overrides email_id)
        conversation_id: Flag every message in this conversation instead
        status: "flagged" (default) to set the follow-up flag, "complete" to mark
                the follow-up done, "clear" to remove the flag.
        due: Follow-up due date, "YYYY-MM-DD" or an ISO datetime
             ("2026-09-20T09:00"). Only with status "flagged".
        start: Follow-up start date, same formats. Only with status "flagged";
               defaults to `due` when only `due` is given.
        account_id: Microsoft account ID (optional)
        mailbox: Address (UPN) of a SHARED mailbox to act on instead of the
                 signed-in account's own mailbox (default). Needs Full Access plus
                 the Mail.ReadWrite.Shared permission.

    Returns:
        Success or per-message error report.
    """
    user_id = get_user_id(config)
    mode = status.strip().lower()
    if mode == "notflagged":
        mode = "clear"
    if mode not in ("flagged", "complete", "clear"):
        return "[Error]: status must be 'flagged', 'complete', or 'clear'."
    if mode != "flagged" and (due.strip() or start.strip()):
        return '[Error]: due and start apply only to status="flagged".'
    gate = _gate(user_id, account_id, mailbox, purpose="flagging messages")
    if gate:
        return f"[Error]: {gate}"

    flag: dict[str, Any] = {"flagStatus": {"flagged": "flagged", "complete": "complete", "clear": "notFlagged"}[mode]}
    verb = {"flagged": "flagged for follow-up", "complete": "marked follow-up complete", "clear": "unflagged"}[mode]
    if mode == "flagged" and (due.strip() or start.strip()):
        zone = _mailbox_time_zone(user_id, account_id, mailbox)
        due_dt: Optional[dict] = None
        if due.strip():
            ok, due_dt = _graph_datetime(due, zone, "due")
            if not ok:
                return f"[Error]: {due_dt}"
        if start.strip():
            ok, start_dt = _graph_datetime(start, zone, "start")
            if not ok:
                return f"[Error]: {start_dt}"
        else:
            start_dt = due_dt  # Graph rejects a due date without a start date
        flag["startDateTime"] = start_dt
        if due_dt:
            flag["dueDateTime"] = due_dt
            verb += f" (due {due_dt['dateTime'][:10]})"

    ok, targets = resolve_targets(
        user_id, email_id=email_id, email_ids=email_ids, conversation_id=conversation_id,
        account_id=account_id, mailbox=mailbox,
    )
    if not ok:
        return f"[Error]: {targets}"
    results = batch_patch_messages(user_id, targets, {"flag": flag}, account_id=account_id, mailbox=mailbox)
    return summarize_batch(results, targets, verb)


# ---------------------------------------------------------------------------
# Inbox rules
# ---------------------------------------------------------------------------


def _parse_json_object(field: str, text: str) -> tuple[bool, Any]:
    """``(True, dict)`` for an empty or JSON-object string, else ``(False, error)``."""
    if not (text or "").strip():
        return True, {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as e:
        return False, f"{field} is not valid JSON: {e}."
    if not isinstance(value, dict):
        return False, f"{field} must be a JSON object (got {type(value).__name__})."
    return True, value


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _normalise_predicates(field: str, raw: dict) -> tuple[bool, Any]:
    """Validate and reshape a conditions/exceptions object against Graph's field list."""
    unknown = sorted(k for k in raw if k not in _PREDICATE_FIELDS)
    if unknown:
        return False, (
            f"{field} has unknown field(s): {', '.join(unknown)}. "
            f"Known fields: {', '.join(sorted(_PREDICATE_FIELDS))}."
        )
    out: dict[str, Any] = {}
    for key, value in raw.items():
        kind = _PREDICATE_FIELDS[key]
        if kind == "strings":
            out[key] = _string_list(value)
        elif kind == "recipients":
            out[key] = _recipient_list(value)
            if not out[key]:
                return False, f"{field} field '{key}' needs at least one email address."
        elif kind == "bool":
            flag = _coerce_bool(value)
            if flag is None:
                return False, f"{field} field '{key}' must be true or false."
            out[key] = flag
        elif kind == "object":
            if not isinstance(value, dict):
                return False, f"{field} field '{key}' must be a JSON object."
            out[key] = value
        else:
            out[key] = str(value)
    return True, out


def _folder_id_for_rule(
    user_id: str, value: str, account_id: Optional[str], mailbox: Optional[str]
) -> tuple[bool, Any]:
    """A rule action needs a folder ID: resolve names, paths and well-known aliases to one.

    ``resolve_folder`` hands back the well-known NAME for an alias (``archive``),
    which Graph accepts in a URL but not inside ``messageRuleActions``; one more
    GET turns it into the id. Returns ``(True, (id, display_name))``.
    """
    ok, segment = resolve_folder(user_id, value, account_id, mailbox=mailbox)
    if not ok:
        return False, segment
    if segment not in _WELL_KNOWN_SEGMENTS:
        return True, (segment, value.strip().rsplit("/", 1)[-1])
    ok, result = graph_request(
        user_id, "GET", f"/me/mailFolders/{segment}", account_id=account_id,
        params={"$select": "id,displayName"}, mailbox=mailbox,
    )
    if not ok or not isinstance(result, dict) or not result.get("id"):
        return False, f"Could not resolve folder '{value}' to an id: {result}"
    return True, (str(result["id"]), str(result.get("displayName") or value))


_FORWARDING_ACTIONS = ("forwardTo", "redirectTo", "forwardAsAttachmentTo")


def _normalise_actions(raw: dict, *, allow_forwarding: bool = False) -> tuple[bool, Any]:
    """Validate and reshape an actions object against Graph's field list (no requests).

    Folder-valued actions keep their raw name here; :func:`_resolve_action_folders`
    turns them into ids once the account gate has passed. Forwarding actions are
    refused unless ``allow_forwarding``: a rule that forwards every future
    message to an outside address is the classic mailbox-compromise foothold,
    one injected email body away, so it needs the caller to say so explicitly.
    """
    unknown = sorted(k for k in raw if k not in _ACTION_FIELDS)
    if unknown:
        return False, (
            f"actions_json has unknown field(s): {', '.join(unknown)}. "
            f"Known fields: {', '.join(sorted(_ACTION_FIELDS))}."
        )
    forwarding = [k for k in _FORWARDING_ACTIONS if k in raw]
    if forwarding and not allow_forwarding:
        return False, (
            f"actions_json uses {', '.join(forwarding)}, which would forward or redirect future mail "
            "to another address automatically. Refused unless allow_forwarding=True is passed, and "
            "only after the user has explicitly asked for that forwarding."
        )
    out: dict[str, Any] = {}
    for key, value in raw.items():
        kind = _ACTION_FIELDS[key]
        if kind == "strings":
            out[key] = _string_list(value)
        elif kind == "recipients":
            out[key] = _recipient_list(value)
            if not out[key]:
                return False, f"actions_json field '{key}' needs at least one email address."
        elif kind == "bool":
            flag = _coerce_bool(value)
            if flag is None:
                return False, f"actions_json field '{key}' must be true or false."
            out[key] = flag
        elif kind == "folder":
            if not isinstance(value, str) or not value.strip():
                return False, f"actions_json field '{key}' must be a folder name, path, or id."
            out[key] = value.strip()
        else:
            out[key] = str(value)
    return True, out


def _resolve_action_folders(
    actions: dict, user_id: str, account_id: Optional[str], mailbox: Optional[str]
) -> tuple[bool, Any]:
    """Replace folder names in ``moveToFolder``/``copyToFolder`` with ids.

    Returns ``(True, folder_names)`` (id to display name, for the summary) after
    mutating ``actions`` in place, or ``(False, error)`` naming the field.
    """
    folder_names: dict[str, str] = {}
    for key, kind in _ACTION_FIELDS.items():
        if kind != "folder" or key not in actions:
            continue
        ok, resolved = _folder_id_for_rule(user_id, actions[key], account_id, mailbox)
        if not ok:
            return False, f"actions_json field '{key}': {resolved}"
        actions[key], folder_names[resolved[0]] = resolved[0], resolved[1]
    return True, folder_names


def _describe_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        if value and all(isinstance(v, dict) for v in value):
            return _addresses(value)
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _describe_predicates(pred: Optional[dict]) -> str:
    if not pred:
        return "(always)"
    return "; ".join(f"{k} {_describe_value(v)}" for k, v in pred.items())


def _describe_actions(actions: Optional[dict], folder_names: dict[str, str]) -> str:
    if not actions:
        return "(nothing)"
    parts = []
    for key, value in actions.items():
        if key in ("moveToFolder", "copyToFolder"):
            parts.append(f"{key} {folder_names.get(str(value), str(value))}")
        else:
            parts.append(f"{key} {_describe_value(value)}")
    return "; ".join(parts)


def _render_rule(rule: dict, folder_names: dict[str, str]) -> list[str]:
    state = "on" if rule.get("isEnabled") else "off"
    flags = "".join(
        f" [{f}]" for f, present in (("error", rule.get("hasError")), ("read-only", rule.get("isReadOnly"))) if present
    )
    lines = [f"[{state}] '{rule.get('displayName')}' (sequence {rule.get('sequence')}){flags} id={rule.get('id')}"]
    lines.append(f"    when: {_describe_predicates(rule.get('conditions'))}")
    if rule.get("exceptions"):
        lines.append(f"    except: {_describe_predicates(rule.get('exceptions'))}")
    lines.append(f"    do: {_describe_actions(rule.get('actions'), folder_names)}")
    return lines


def _rule_folder_names(
    user_id: str, rules: list[dict], account_id: Optional[str], mailbox: Optional[str]
) -> dict[str, str]:
    """Display names for every folder id the rules move or copy into (one ``$batch``)."""
    ids: list[str] = []
    for rule in rules:
        for key in ("moveToFolder", "copyToFolder"):
            fid = (rule.get("actions") or {}).get(key)
            if fid and fid not in ids:
                ids.append(str(fid))
    if not ids:
        return {}
    requests = [
        {"id": str(i), "method": "GET", "url": f"/me/mailFolders/{fid}?$select=id,displayName"}
        for i, fid in enumerate(ids)
    ]
    names: dict[str, str] = {}
    for fid, outcome in zip(ids, graph_batch(user_id, requests, account_id=account_id, mailbox=mailbox)):
        if outcome["ok"] and isinstance(outcome["body"], dict) and outcome["body"].get("displayName"):
            names[fid] = str(outcome["body"]["displayName"])
    return names


def _list_rules(user_id: str, account_id: Optional[str], mailbox: Optional[str]) -> tuple[bool, Any]:
    ok, result = graph_request(user_id, "GET", _RULES_ENDPOINT, account_id=account_id, mailbox=mailbox)
    if not ok:
        return False, str(result)
    return True, list(result.get("value") or [])


def _find_rule(rules: list[dict], name: str, rule_id: str) -> tuple[bool, Any]:
    if rule_id.strip():
        for rule in rules:
            if str(rule.get("id")) == rule_id.strip():
                return True, rule
        return False, f"No inbox rule with id '{rule_id.strip()}'. outlook_list_rules shows them."
    if not name.strip():
        return False, "name (or rule_id) is required to identify the rule."
    exact = [r for r in rules if str(r.get("displayName", "")) == name.strip()]
    matches = exact or [r for r in rules if str(r.get("displayName", "")).lower() == name.strip().lower()]
    if not matches:
        return False, f"No inbox rule named '{name.strip()}'. outlook_list_rules shows them."
    if len(matches) > 1:
        listing = "; ".join(f"{m.get('displayName')} (id {m.get('id')})" for m in matches)
        return False, f"Rule name '{name.strip()}' is ambiguous: {listing}. Pass rule_id."
    return True, matches[0]


@tool
def outlook_list_rules(
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    List the server-side inbox rules: conditions, exceptions, actions, order, and state.

    Rules run on the Exchange server against mail arriving in the Inbox, whether
    or not any client is open. Each rule shows enabled/disabled, its display
    name, execution sequence, id, the conditions (Graph field names, e.g.
    senderContains), exceptions, and actions; folders in moveToFolder and
    copyToFolder actions are shown by name. outlook_manage_rule creates and
    edits rules. Acts on the signed-in account's own mailbox by default. Needs
    the MailboxSettings.ReadWrite permission; an account connected without it is
    told how to reconnect.

    Args:
        account_id: Microsoft account ID (optional; defaults to the thread's bound
                    or only connected account)
        mailbox: Address (UPN) of a SHARED mailbox to read instead of the
                 signed-in account's own mailbox (default). Needs Full Access plus
                 the Mail.ReadWrite.Shared permission.

    Returns:
        The rules in sequence order, or a permission error.
    """
    user_id = get_user_id(config)
    gate = _gate(user_id, account_id, mailbox, purpose="listing inbox rules", settings=True)
    if gate:
        return f"[Error]: {gate}"
    ok, rules = _list_rules(user_id, account_id, mailbox)
    if not ok:
        return f"[Error]: {rules}"
    if not rules:
        return '[Info]: No inbox rules are defined. outlook_manage_rule(action="create") adds one.'
    rules.sort(key=lambda r: (r.get("sequence") is None, r.get("sequence") or 0))
    folder_names = _rule_folder_names(user_id, rules, account_id, mailbox)
    lines = [f"[Success]: {len(rules)} inbox rule(s):"]
    for rule in rules:
        lines.extend(_render_rule(rule, folder_names))
    return "\n".join(lines)


@tool
def outlook_manage_rule(
    action: str,
    name: str = "",
    rule_id: str = "",
    conditions_json: str = "",
    actions_json: str = "",
    exceptions_json: str = "",
    sequence: int = 0,
    enabled: bool = True,
    new_name: str = "",
    allow_forwarding: bool = False,
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Create, update, enable, disable, or delete a server-side inbox rule.

    Rules use Microsoft Graph's own messageRulePredicates and messageRuleActions
    field names, passed as JSON object strings. Conditions (all must match):
    senderContains, subjectContains, bodyContains, bodyOrSubjectContains,
    headerContains, recipientContains, categories (each a list of strings; a
    single string is accepted), fromAddresses and sentToAddresses (lists of
    addresses or "Name <addr>"; recipient objects are built for you),
    hasAttachments, isAutomaticReply, isMeetingRequest, isMeetingResponse,
    isReadReceipt, isNonDeliveryReport, isVoicemail, sentToMe, sentOnlyToMe,
    sentCcMe, sentToOrCcMe, notSentToMe (booleans), importance (low, normal,
    high), sensitivity, messageActionFlag, withinSizeRange ({"minimumSize",
    "maximumSize"} in KB). Actions: moveToFolder and copyToFolder (a folder NAME,
    slash path, well-known name or id; resolved to the id before the rule is
    created, and refused if no such folder exists), assignCategories (list),
    markAsRead, delete, permanentDelete, stopProcessingRules (booleans),
    forwardTo, redirectTo, forwardAsAttachmentTo (address lists; REFUSED unless
    allow_forwarding=True, because a rule that forwards future mail elsewhere is
    the classic mailbox-compromise foothold: pass it only when the user has
    explicitly asked for that forwarding), markImportance (low, normal, high).
    Unknown field names and malformed JSON are refused, naming the argument,
    before anything is sent. Rules apply to the Inbox only.
    Acts on the signed-in account's own mailbox by default. Needs the
    MailboxSettings.ReadWrite permission; an account connected without it is
    told how to reconnect.

    Args:
        action: "create", "update", "enable", "disable", or "delete".
        name: The rule's display name. For create it names the new rule; for the
              other actions it identifies the rule (exact match, then
              case-insensitive; an ambiguous name is refused with the ids).
        rule_id: Identify the rule by id instead of name (update, enable,
                 disable, delete).
        conditions_json: JSON object of conditions, e.g.
                         '{"senderContains": ["newsletter"], "hasAttachments": false}'.
                         Empty on create means the rule matches every message.
                         On update it REPLACES the whole condition set.
        actions_json: JSON object of actions, e.g.
                      '{"moveToFolder": "Newsletters", "markAsRead": true}'.
                      Required for create; on update it replaces the action set.
        exceptions_json: JSON object of exception conditions (same fields as
                         conditions); a matching exception stops the rule.
        sequence: Execution order among the rules (1 runs first). 0 (default) on
                  create appends after the current highest; on update 0 leaves
                  the order unchanged.
        enabled: Whether a created rule starts enabled (default True).
        new_name: For update only: a new display name for the rule.
        allow_forwarding: Permit forwardTo / redirectTo / forwardAsAttachmentTo
                          actions (default False). Set it only when the user
                          explicitly asked for mail to be forwarded elsewhere.
        account_id: Microsoft account ID (optional)
        mailbox: Address (UPN) of a SHARED mailbox to act on instead of the
                 signed-in account's own mailbox (default). Needs Full Access plus
                 the Mail.ReadWrite.Shared permission.

    Returns:
        Success with the rule's id and a summary of what it does, or an error.
    """
    user_id = get_user_id(config)
    act = action.strip().lower()
    if act not in ("create", "update", "enable", "disable", "delete"):
        return "[Error]: action must be 'create', 'update', 'enable', 'disable', or 'delete'."

    ok, conditions = _parse_json_object("conditions_json", conditions_json)
    if not ok:
        return f"[Error]: {conditions}"
    ok, exceptions = _parse_json_object("exceptions_json", exceptions_json)
    if not ok:
        return f"[Error]: {exceptions}"
    ok, actions_raw = _parse_json_object("actions_json", actions_json)
    if not ok:
        return f"[Error]: {actions_raw}"
    if act == "create":
        if not name.strip():
            return "[Error]: name is required to create a rule."
        if not actions_raw:
            return "[Error]: actions_json is required to create a rule (at least one action)."
    if conditions:
        ok, conditions = _normalise_predicates("conditions_json", conditions)
        if not ok:
            return f"[Error]: {conditions}"
    if exceptions:
        ok, exceptions = _normalise_predicates("exceptions_json", exceptions)
        if not ok:
            return f"[Error]: {exceptions}"
    actions: dict[str, Any] = {}
    if actions_raw:
        ok, actions = _normalise_actions(actions_raw, allow_forwarding=allow_forwarding)
        if not ok:
            return f"[Error]: {actions}"

    gate = _gate(user_id, account_id, mailbox, purpose="inbox rule management", settings=True)
    if gate:
        return f"[Error]: {gate}"

    folder_names: dict[str, str] = {}
    if actions:
        ok, folder_names = _resolve_action_folders(actions, user_id, account_id, mailbox)
        if not ok:
            return f"[Error]: {folder_names}"

    if act == "create":
        seq = int(sequence)
        if seq <= 0:
            ok, rules = _list_rules(user_id, account_id, mailbox)
            if not ok:
                return f"[Error]: {rules}"
            seq = max((int(r.get("sequence") or 0) for r in rules), default=0) + 1
        body: dict[str, Any] = {"displayName": name.strip(), "sequence": seq, "isEnabled": bool(enabled), "actions": actions}
        if conditions:
            body["conditions"] = conditions
        if exceptions:
            body["exceptions"] = exceptions
        ok, result = graph_request(user_id, "POST", _RULES_ENDPOINT, account_id=account_id, json_data=body, mailbox=mailbox)
        if not ok:
            return f"[Error]: {result}"
        created = {**body, "id": result.get("id", "")}
        return "\n".join([f"[Success]: Rule '{name.strip()}' created."] + _render_rule(created, folder_names))

    ok, rules = _list_rules(user_id, account_id, mailbox)
    if not ok:
        return f"[Error]: {rules}"
    found, rule = _find_rule(rules, name, rule_id)
    if not found:
        return f"[Error]: {rule}"
    label = rule.get("displayName")
    endpoint = f"{_RULES_ENDPOINT}/{rule['id']}"

    if act == "delete":
        ok, result = graph_request(user_id, "DELETE", endpoint, account_id=account_id, mailbox=mailbox)
        return f"[Success]: Rule '{label}' deleted." if ok else f"[Error]: {result}"

    if act in ("enable", "disable"):
        ok, result = graph_request(
            user_id, "PATCH", endpoint, account_id=account_id,
            json_data={"isEnabled": act == "enable"}, mailbox=mailbox,
        )
        return f"[Success]: Rule '{label}' {act}d." if ok else f"[Error]: {result}"

    patch: dict[str, Any] = {}
    if new_name.strip():
        patch["displayName"] = new_name.strip()
    if conditions_json.strip():
        patch["conditions"] = conditions
    if exceptions_json.strip():
        patch["exceptions"] = exceptions
    if actions_json.strip():
        patch["actions"] = actions
    if int(sequence) > 0:
        patch["sequence"] = int(sequence)
    if not patch:
        return "[Error]: Nothing to update. Provide new_name, conditions_json, exceptions_json, actions_json, or sequence."
    ok, result = graph_request(user_id, "PATCH", endpoint, account_id=account_id, json_data=patch, mailbox=mailbox)
    if not ok:
        return f"[Error]: {result}"
    return f"[Success]: Rule '{label}' updated ({', '.join(patch)})."


# ---------------------------------------------------------------------------
# Mailbox settings and automatic replies
# ---------------------------------------------------------------------------


def _describe_auto_reply(setting: Optional[dict]) -> list[str]:
    if not setting:
        return ["Automatic replies: (not reported)"]
    status = setting.get("status", "disabled")
    lines = [f"Automatic replies: {status}"]
    if status == "scheduled":
        start = setting.get("scheduledStartDateTime") or {}
        end = setting.get("scheduledEndDateTime") or {}
        lines.append(
            f"  Window: {start.get('dateTime', '?')[:16]} to {end.get('dateTime', '?')[:16]} ({end.get('timeZone') or start.get('timeZone') or '?'})"
        )
    if status != "disabled":
        lines.append(f"  External audience: {setting.get('externalAudience', 'none')}")
        internal = _strip_html(setting.get("internalReplyMessage", ""))
        external = _strip_html(setting.get("externalReplyMessage", ""))
        if internal:
            lines.append(f"  Internal message: {internal[:300]}")
        if external:
            lines.append(f"  External message: {external[:300]}")
    return lines


@tool
def outlook_get_mailbox_settings(
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Read the mailbox's settings: time zone, language, formats, working hours,
    automatic-reply state, archive folder, and mailbox type.

    Use it before scheduling anything for the mailbox owner (the time zone and
    working hours are theirs, not the server's) and to check whether an
    out-of-office reply is active. outlook_set_auto_reply changes the automatic
    reply. Acts on the signed-in account's own mailbox by default. Needs the
    MailboxSettings.ReadWrite permission; an account connected without it is
    told how to reconnect.

    Args:
        account_id: Microsoft account ID (optional; defaults to the thread's bound
                    or only connected account)
        mailbox: Address (UPN) of a SHARED mailbox to read instead of the
                 signed-in account's own mailbox (default). Needs Full Access plus
                 the Mail.ReadWrite.Shared permission.

    Returns:
        The settings, one per line, or a permission error.
    """
    user_id = get_user_id(config)
    gate = _gate(user_id, account_id, mailbox, purpose="reading mailbox settings", settings=True)
    if gate:
        return f"[Error]: {gate}"
    ok, result = graph_request(user_id, "GET", "/me/mailboxSettings", account_id=account_id, mailbox=mailbox)
    if not ok or not isinstance(result, dict):
        return f"[Error]: {result}"

    lines = ["[Success]: Mailbox settings:"]
    lines.append(f"Time zone: {result.get('timeZone') or '(not set)'}")
    language = result.get("language") or {}
    if language:
        lines.append(f"Language: {language.get('displayName') or language.get('locale')} ({language.get('locale')})")
    if result.get("dateFormat") or result.get("timeFormat"):
        lines.append(f"Date/time format: {result.get('dateFormat') or '?'} / {result.get('timeFormat') or '?'}")
    hours = result.get("workingHours") or {}
    if hours:
        days = ", ".join(str(d) for d in hours.get("daysOfWeek") or []) or "(none)"
        zone = (hours.get("timeZone") or {}).get("name") or result.get("timeZone") or "?"
        lines.append(
            f"Working hours: {days}, {str(hours.get('startTime', ''))[:5]} to {str(hours.get('endTime', ''))[:5]} ({zone})"
        )
    lines.extend(_describe_auto_reply(result.get("automaticRepliesSetting")))
    if result.get("archiveFolder"):
        lines.append(f"Archive folder id: {result['archiveFolder']}")
    if result.get("userPurpose"):
        lines.append(f"Mailbox type: {result['userPurpose']}")
    if result.get("delegateMeetingMessageDeliveryOptions"):
        lines.append(f"Delegate meeting messages: {result['delegateMeetingMessageDeliveryOptions']}")
    return "\n".join(lines)


@tool
def outlook_set_auto_reply(
    status: str,
    internal_message: str = "",
    external_message: str = "",
    start: str = "",
    end: str = "",
    external_audience: str = "",
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Turn the mailbox's automatic (out-of-office) reply on, off, or on for a window.

    Only the automaticRepliesSetting is written; every other mailbox setting is
    left alone. "disabled" turns replies off (the request carries nothing but the
    status, so any stored schedule stops applying). "alwaysEnabled" replies until
    turned off. "scheduled" replies between `start` and `end`, given in the
    mailbox's configured time zone (UTC if that cannot be read; a value with its
    own offset or "Z" is converted to UTC). Acts on the signed-in account's own
    mailbox by default. Needs the MailboxSettings.ReadWrite permission; an
    account connected without it is told how to reconnect.

    Args:
        status: "disabled", "alwaysEnabled", or "scheduled".
        internal_message: Reply sent to people inside the organisation. Required
                          unless status is "disabled". Plain text or HTML.
        external_message: Reply sent to people outside the organisation. Defaults
                          to `internal_message` when the external audience is not
                          "none".
        start: Window start for "scheduled", "YYYY-MM-DD" or ISO datetime.
        end: Window end for "scheduled", same formats; must be after `start`.
        external_audience: Who outside the organisation gets the external reply:
                           "none", "contactsOnly", or "all". Empty (default) means
                           "all" when an external message is supplied, otherwise
                           the mailbox's current setting is kept.
        account_id: Microsoft account ID (optional)
        mailbox: Address (UPN) of a SHARED mailbox to act on instead of the
                 signed-in account's own mailbox (default). Needs Full Access plus
                 the Mail.ReadWrite.Shared permission.

    Returns:
        Success describing the resulting state, or an error.
    """
    user_id = get_user_id(config)
    mode = _AUTO_REPLY_STATUSES.get(status.strip().lower())
    if mode is None:
        return "[Error]: status must be 'disabled', 'alwaysEnabled', or 'scheduled'."
    audience: Optional[str] = None
    if external_audience.strip():
        audience = _EXTERNAL_AUDIENCES.get(external_audience.strip().lower())
        if audience is None:
            return "[Error]: external_audience must be 'none', 'contactsOnly', or 'all'."
    if mode != "disabled" and not internal_message.strip():
        return f'[Error]: internal_message is required for status="{mode}".'
    if mode == "scheduled" and not (start.strip() and end.strip()):
        return '[Error]: start and end are required for status="scheduled".'

    gate = _gate(user_id, account_id, mailbox, purpose="setting the automatic reply", settings=True)
    if gate:
        return f"[Error]: {gate}"

    setting: dict[str, Any] = {"status": mode}
    summary = "[Success]: Automatic replies turned off."
    if mode != "disabled":
        setting["internalReplyMessage"] = internal_message
        if audience is None and external_message.strip():
            audience = "all"
        if audience is not None:
            setting["externalAudience"] = audience
        if audience != "none":
            setting["externalReplyMessage"] = external_message if external_message.strip() else internal_message
        audience_note = f"; external audience: {audience}" if audience is not None else ""
        summary = f"[Success]: Automatic replies enabled until turned off{audience_note}."
        if mode == "scheduled":
            zone = _mailbox_time_zone(user_id, account_id, mailbox)
            ok, start_dt = _graph_datetime(start, zone, "start")
            if not ok:
                return f"[Error]: {start_dt}"
            ok, end_dt = _graph_datetime(end, zone, "end")
            if not ok:
                return f"[Error]: {end_dt}"
            if end_dt["dateTime"] <= start_dt["dateTime"] and end_dt["timeZone"] == start_dt["timeZone"]:
                return "[Error]: end must be after start."
            setting["scheduledStartDateTime"] = start_dt
            setting["scheduledEndDateTime"] = end_dt
            summary = (
                f"[Success]: Automatic replies scheduled from {start_dt['dateTime']} to {end_dt['dateTime']} "
                f"({end_dt['timeZone']}){audience_note}."
            )

    ok, result = graph_request(
        user_id, "PATCH", "/me/mailboxSettings", account_id=account_id,
        json_data={"automaticRepliesSetting": setting}, mailbox=mailbox,
    )
    if not ok:
        return f"[Error]: {result}"
    return summary


# ---------------------------------------------------------------------------
# Focused / Other sender overrides
# ---------------------------------------------------------------------------


@tool
def outlook_focused_overrides(
    action: str = "list",
    sender: str = "",
    classify: str = "",
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    List, set, or remove per-sender Focused Inbox overrides.

    Outlook's Focused Inbox sorts incoming mail into Focused or Other by its own
    model; an override pins one sender's future mail to one side for good. `set`
    creates the override, or updates it when one already exists for that address
    (Graph upserts by SMTP address; a mailbox holds up to 1000). `remove` deletes
    the override so Outlook's own classification applies again. Overrides affect
    future mail only; outlook_move_email files what is already there. Acts on the
    signed-in account's own mailbox by default. Needs the Mail.ReadWrite
    permission the connected account already carries.

    Args:
        action: "list" (default), "set", or "remove".
        sender: The sender's email address, or "Name <address>". Required for
                set and remove.
        classify: For set: "focused" or "other".
        account_id: Microsoft account ID (optional)
        mailbox: Address (UPN) of a SHARED mailbox to act on instead of the
                 signed-in account's own mailbox (default). Needs Full Access plus
                 the Mail.ReadWrite.Shared permission.

    Returns:
        The override list, or success naming the sender and classification, or an error.
    """
    user_id = get_user_id(config)
    act = action.strip().lower() or "list"
    if act not in ("list", "set", "remove"):
        return "[Error]: action must be 'list', 'set', or 'remove'."
    recipient = _recipient(sender) if sender.strip() else None
    if act != "list" and recipient is None:
        return f"[Error]: sender is required to {act} an override."
    side = classify.strip().lower()
    if act == "set" and side not in ("focused", "other"):
        return "[Error]: classify must be 'focused' or 'other'."
    gate = _gate(user_id, account_id, mailbox, purpose="Focused Inbox overrides")
    if gate:
        return f"[Error]: {gate}"

    if act == "set":
        assert recipient is not None
        ok, result = graph_request(
            user_id, "POST", _OVERRIDES_ENDPOINT, account_id=account_id,
            json_data={"classifyAs": side, "senderEmailAddress": recipient["emailAddress"]}, mailbox=mailbox,
        )
        if not ok:
            return f"[Error]: {result}"
        address = recipient["emailAddress"]["address"]
        return f"[Success]: Mail from {address} will always land in {side.capitalize()}. id={result.get('id', '')}"

    ok, result = graph_request(user_id, "GET", _OVERRIDES_ENDPOINT, account_id=account_id, mailbox=mailbox)
    if not ok:
        return f"[Error]: {result}"
    overrides = list(result.get("value") or [])

    if act == "list":
        if not overrides:
            return "[Info]: No Focused/Other sender overrides are set."
        lines = [f"[Success]: {len(overrides)} sender override(s):"]
        for item in overrides:
            addr = item.get("senderEmailAddress") or {}
            who = addr.get("address", "?")
            if addr.get("name"):
                who += f" ({addr['name']})"
            lines.append(f"  - {who}: {item.get('classifyAs')} id={item.get('id')}")
        return "\n".join(lines)

    assert recipient is not None
    address = recipient["emailAddress"]["address"]
    match = next(
        (o for o in overrides if str((o.get("senderEmailAddress") or {}).get("address", "")).lower() == address.lower()),
        None,
    )
    if match is None:
        return f"[Info]: No override exists for {address}."
    ok, result = graph_request(
        user_id, "DELETE", f"{_OVERRIDES_ENDPOINT}/{match['id']}", account_id=account_id, mailbox=mailbox,
    )
    if not ok:
        return f"[Error]: {result}"
    return f"[Success]: Override for {address} removed; Outlook's own Focused/Other classification applies again."


# ---------------------------------------------------------------------------
# Unsubscribe
# ---------------------------------------------------------------------------


def _header_values(headers: Any, name: str) -> list[str]:
    return [
        str(h.get("value", ""))
        for h in (headers or [])
        if isinstance(h, dict) and str(h.get("name", "")).lower() == name.lower()
    ]


def _unsubscribe_targets(values: list[str]) -> tuple[list[str], list[str]]:
    """Split List-Unsubscribe header value(s) into ``(mailto_uris, http_urls)``."""
    mailtos: list[str] = []
    urls: list[str] = []
    for value in values:
        parts = re.findall(r"<([^<>]+)>", value) or [p.strip() for p in value.split(",")]
        for part in parts:
            uri = part.strip()
            low = uri.lower()
            if low.startswith("mailto:"):
                mailtos.append(uri)
            elif low.startswith("http://") or low.startswith("https://"):
                urls.append(uri)
    return mailtos, urls


def _mailto_parts(uri: str) -> tuple[str, str, str]:
    """``(address, subject, body)`` from a ``mailto:`` URI; defaults to 'unsubscribe'."""
    parsed = urlparse(uri)
    address = unquote(parsed.path).split(",")[0].strip()
    query = parse_qs(parsed.query)
    subject = " ".join((query.get("subject") or ["unsubscribe"])[0].split())[:120] or "unsubscribe"
    body = (query.get("body") or ["unsubscribe"])[0].strip()[:500] or "unsubscribe"
    return address, subject, body


def _one_click_post(url: str) -> tuple[bool, str]:
    """RFC 8058 one-click POST through the egress policy. ``(True, 'HTTP 200')`` or ``(False, reason)``."""
    try:
        with _http_client(timeout=20) as client:
            response = _request_with_policy(client, "POST", url, data={"List-Unsubscribe": "One-Click"})
    except RuntimeError as e:
        return False, str(e)
    except Exception as e:  # transport failure (DNS, TLS, timeout)
        return False, f"request to {url} failed: {e}"
    status = int(getattr(response, "status_code", 0) or 0)
    if 200 <= status < 300:
        return True, f"HTTP {status}"
    if 300 <= status < 400:
        return False, (
            f"the unsubscribe endpoint answered HTTP {status} (a redirect, which RFC 8058 forbids "
            "for one-click), so the request was not honoured; open the link in a browser instead"
        )
    return False, f"the unsubscribe endpoint answered HTTP {status}"


@tool
def outlook_unsubscribe(
    email_id: str,
    method: str = "auto",
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Unsubscribe from a mailing list using the message's List-Unsubscribe headers.

    Reads the message's List-Unsubscribe and List-Unsubscribe-Post headers and
    acts on them. one_click: an https URL plus a List-Unsubscribe-Post header
    (RFC 8058) gets a POST of "List-Unsubscribe=One-Click" through Nymeria's
    egress policy, which refuses private, loopback and metadata addresses.
    mailto: an unsubscribe email is SENT from the selected account to the
    header's address with the header's subject (or "unsubscribe"). An https link
    without one-click support is returned for you to open (it is never fetched
    blindly). A message without the header cannot be unsubscribed this way. The
    result names the method used. Senders may take days to stop; a rule that
    deletes their mail (outlook_manage_rule) is the immediate fix. Acts on the
    signed-in account's own mailbox by default. Needs the Mail.ReadWrite
    permission, plus Mail.Send for the mailto method.

    Args:
        email_id: ID of the message (from outlook_list_emails or outlook_search_emails).
        method: "auto" (default; one_click when available, else mailto, else
                return the link), "mailto", or "one_click".
        account_id: Microsoft account ID (optional)
        mailbox: Address (UPN) of a SHARED mailbox the message lives in (and the
                 mailto request is sent from), when not the signed-in account's
                 own mailbox (default). Needs Full Access plus the
                 Mail.ReadWrite.Shared and Mail.Send.Shared permissions.

    Returns:
        Success naming the method and target, an info line with the link to open,
        or an error saying why nothing was sent.
    """
    user_id = get_user_id(config)
    how = method.strip().lower() or "auto"
    if how not in ("auto", "mailto", "one_click"):
        return "[Error]: method must be 'auto', 'mailto', or 'one_click'."
    if not email_id.strip():
        return "[Error]: email_id is required."
    gate = _gate(user_id, account_id, mailbox, purpose="unsubscribing")
    if gate:
        return f"[Error]: {gate}"

    ok, message = graph_request(
        user_id, "GET", f"/me/messages/{email_id.strip()}", account_id=account_id,
        params={"$select": "internetMessageHeaders,subject,from"}, mailbox=mailbox,
    )
    if not ok or not isinstance(message, dict):
        return f"[Error]: {message}"
    subject = message.get("subject") or "(no subject)"
    sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address") or "unknown sender"
    headers = message.get("internetMessageHeaders") or []
    mailtos, urls = _unsubscribe_targets(_header_values(headers, "List-Unsubscribe"))
    one_click_ok = bool(_header_values(headers, "List-Unsubscribe-Post"))
    https_urls = [u for u in urls if u.lower().startswith("https://")]

    if not mailtos and not urls:
        return (
            f"[Info]: '{subject}' from {sender} carries no List-Unsubscribe header, so there is nothing to "
            "unsubscribe from automatically. Look for an unsubscribe link in the body, or stop the sender "
            "with outlook_manage_rule."
        )

    if how == "one_click" and not (https_urls and one_click_ok):
        found = ", ".join(mailtos + urls)
        return (
            f"[Error]: '{subject}' does not support one-click unsubscribe (needs an https List-Unsubscribe "
            f"URL and a List-Unsubscribe-Post header). Found: {found}. Try method=\"mailto\" or open the link."
        )
    if how == "mailto" and not mailtos:
        return (
            f"[Error]: The List-Unsubscribe header of '{subject}' has no mailto address. Found: "
            f"{', '.join(urls)}. Try method=\"one_click\" or open the link."
        )

    if how in ("auto", "one_click") and https_urls and one_click_ok:
        url = https_urls[0]
        sent, detail = _one_click_post(url)
        if sent:
            return f"[Success]: One-click unsubscribe sent for '{subject}' from {sender} (POST {url}, {detail}). Method: one_click."
        return f"[Error]: One-click unsubscribe for '{subject}' was not sent: {detail}."

    if mailtos:
        address, mail_subject, body = _mailto_parts(mailtos[0])
        if not address:
            return f"[Error]: The mailto unsubscribe address in '{subject}' could not be parsed: {mailtos[0]}"
        ok, result = graph_request(
            user_id, "POST", "/me/sendMail", account_id=account_id,
            json_data={
                "message": {
                    "subject": mail_subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [{"emailAddress": {"address": address}}],
                },
            },
            mailbox=mailbox,
        )
        if not ok:
            return f"[Error]: {result}"
        return (
            f"[Success]: Unsubscribe email for '{subject}' from {sender} sent to {address} with subject "
            f"'{mail_subject}'. Method: mailto."
        )

    return (
        f"[Info]: '{subject}' from {sender} offers a link-based unsubscribe only (no one-click support). "
        f"Open this link to complete it: {urls[0]}"
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

OUTLOOK_ORGANIZE_TOOLS = [
    outlook_list_folders,
    outlook_manage_folder,
    outlook_list_categories,
    outlook_manage_categories,
    outlook_flag_email,
    outlook_list_rules,
    outlook_manage_rule,
    outlook_get_mailbox_settings,
    outlook_set_auto_reply,
    outlook_focused_overrides,
    outlook_unsubscribe,
]

register_tool_group(ToolGroup(name="outlook_organize", tools=tuple(OUTLOOK_ORGANIZE_TOOLS)))
