"""
Account provisioning CLI — ``python run.py users <action> ...``.

Operates directly on :class:`AccountsRepo`; no running API required. This is
intentional so the admin can create/rotate users even when the API is down.
"""

from __future__ import annotations

import argparse
import sys
from typing import List

from nymeria.config import get_settings
from nymeria.core.accounts import (
    AccountsRepo,
    UserAlreadyExists,
    UserNotFound,
)


VALID_PROVIDERS = ("discord", "telegram", "twitch")


def _repo() -> AccountsRepo:
    settings = get_settings()
    return AccountsRepo(settings.data_dir / "accounts.db")


def _resolve_user_id_by_email(repo: AccountsRepo, email: str) -> str:
    user = repo.get_user_by_email(email)
    if user is None:
        print(f"[error] No user with email: {email}", file=sys.stderr)
        sys.exit(2)
    return user.id


def _default_slug(email: str) -> str:
    # e.g. "jake@example.com" -> "jake". Used as a sensible default for --id.
    local = email.split("@", 1)[0]
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in local)
    return safe or "user"


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_add(args: argparse.Namespace) -> int:
    repo = _repo()
    user_id = args.id or _default_slug(args.email)
    display_name = args.display_name or user_id
    try:
        user = repo.create_user(
            user_id=user_id,
            email=args.email,
            display_name=display_name,
            role=args.role,
        )
    except UserAlreadyExists as e:
        print(f"[error] User already exists: {e}", file=sys.stderr)
        return 2
    token = repo.issue_token(user.id, label=args.label)
    print(f"Created {user.role} user: {user.id}  ({user.email})")
    print(f"Display name: {user.display_name}")
    print()
    print(f"  Token: {token}")
    print()
    print("Save this token now — it will not be shown again.")
    return 0


def _cmd_list(_: argparse.Namespace) -> int:
    repo = _repo()
    users = repo.list_users()
    if not users:
        print("(no users)")
        return 0
    rows: List[List[str]] = [["id", "email", "role", "disabled", "tokens", "last_used", "created"]]
    for u in users:
        tokens = repo.list_tokens_for_user(u.id)
        active = [t for t in tokens if t.revoked_at is None]
        last_used = max(
            (t.last_used_at for t in active if t.last_used_at),
            default="-",
        )
        rows.append(
            [
                u.id,
                u.email,
                u.role,
                "yes" if u.disabled else "no",
                str(len(active)),
                last_used or "-",
                u.created_at,
            ]
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for i, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            print("  ".join("-" * widths[j] for j in range(len(row))))
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    repo = _repo()
    user_id = _resolve_user_id_by_email(repo, args.email)
    try:
        repo.set_disabled(user_id, True)
    except UserNotFound:
        print(f"[error] User not found: {args.email}", file=sys.stderr)
        return 2
    print(f"Disabled user {user_id} ({args.email}). Existing tokens will be rejected.")
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    repo = _repo()
    user_id = _resolve_user_id_by_email(repo, args.email)
    repo.set_disabled(user_id, False)
    print(f"Enabled user {user_id} ({args.email}).")
    return 0


def _cmd_rotate_token(args: argparse.Namespace) -> int:
    repo = _repo()
    user_id = _resolve_user_id_by_email(repo, args.email)
    revoked = repo.revoke_all_tokens(user_id)
    token = repo.issue_token(user_id, label=args.label)
    print(f"Revoked {revoked} existing token(s) for {user_id}.")
    print()
    print(f"  New token: {token}")
    print()
    print("Save this token now — it will not be shown again.")
    return 0


def _cmd_link_platform(args: argparse.Namespace) -> int:
    repo = _repo()
    user_id = _resolve_user_id_by_email(repo, args.email)
    if args.provider not in VALID_PROVIDERS:
        print(
            f"[error] Unknown provider '{args.provider}'. Use one of: {', '.join(VALID_PROVIDERS)}",
            file=sys.stderr,
        )
        return 2
    repo.link_platform(args.provider, args.provider_user_id, user_id)
    print(
        f"Linked {args.provider}:{args.provider_user_id} -> {user_id} ({args.email})."
    )
    return 0


def _cmd_unlink_platform(args: argparse.Namespace) -> int:
    repo = _repo()
    if args.provider not in VALID_PROVIDERS:
        print(
            f"[error] Unknown provider '{args.provider}'. Use one of: {', '.join(VALID_PROVIDERS)}",
            file=sys.stderr,
        )
        return 2
    removed = repo.unlink_platform(args.provider, args.provider_user_id)
    if removed:
        print(f"Unlinked {args.provider}:{args.provider_user_id}.")
        return 0
    print(f"(no mapping existed for {args.provider}:{args.provider_user_id})")
    return 0


def _cmd_list_platforms(args: argparse.Namespace) -> int:
    repo = _repo()
    user_id = _resolve_user_id_by_email(repo, args.email)
    identities = repo.list_platforms_for_user(user_id)
    if not identities:
        print("(no linked platforms)")
        return 0
    for i in identities:
        print(f"  {i.provider:10} {i.provider_user_id:20}  linked {i.created_at}")
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring — called from run.py
# ---------------------------------------------------------------------------


def build_parser(subparsers: argparse._SubParsersAction) -> None:
    users = subparsers.add_parser("users", help="Account provisioning (admin)")
    actions = users.add_subparsers(dest="action", required=True)

    p_add = actions.add_parser("add", help="Create a user and mint a token")
    p_add.add_argument("email")
    p_add.add_argument("--role", choices=("user", "admin"), default="user")
    p_add.add_argument("--display-name", default=None)
    p_add.add_argument("--id", default=None, help="Slug id (default: local-part of email)")
    p_add.add_argument("--label", default=None, help="Optional label for the minted token")

    actions.add_parser("list", help="List all users")

    p_disable = actions.add_parser("disable", help="Disable a user")
    p_disable.add_argument("email")

    p_enable = actions.add_parser("enable", help="Re-enable a disabled user")
    p_enable.add_argument("email")

    p_rotate = actions.add_parser("rotate-token", help="Revoke all tokens, mint a fresh one")
    p_rotate.add_argument("email")
    p_rotate.add_argument("--label", default=None)

    p_link = actions.add_parser(
        "link-platform",
        help="Link a Discord/Telegram/Twitch identity to a user",
    )
    p_link.add_argument("email")
    p_link.add_argument("provider", choices=VALID_PROVIDERS)
    p_link.add_argument("provider_user_id")

    p_unlink = actions.add_parser(
        "unlink-platform",
        help="Remove a platform identity mapping",
    )
    p_unlink.add_argument("provider", choices=VALID_PROVIDERS)
    p_unlink.add_argument("provider_user_id")

    p_platforms = actions.add_parser("platforms", help="List linked platform identities")
    p_platforms.add_argument("email")


def dispatch(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None)
    handlers = {
        "add": _cmd_add,
        "list": _cmd_list,
        "disable": _cmd_disable,
        "enable": _cmd_enable,
        "rotate-token": _cmd_rotate_token,
        "link-platform": _cmd_link_platform,
        "unlink-platform": _cmd_unlink_platform,
        "platforms": _cmd_list_platforms,
    }
    handler = handlers.get(action)
    if handler is None:
        print(f"[error] Unknown users action: {action}", file=sys.stderr)
        return 2
    return handler(args)
