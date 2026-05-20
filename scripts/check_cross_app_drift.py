#!/usr/bin/env python3
"""Cross-app drift checker for nymeria-desktop vs nymeria-mobile.

Compares files that exist in both apps under src/ and enforces:
- EXACT_MATCH: files that must be byte-identical
- KNOWN_DRIFT: files expected to differ (platform-specific reasons)
- Unclassified overlap: new shared files that need categorization

Exit codes:
  0 - all checks pass
  1 - exact-match files have drifted or unclassified overlap found
"""

import argparse
import filecmp
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_SRC = REPO_ROOT / "nymeria-desktop" / "src"
MOBILE_SRC = REPO_ROOT / "nymeria-mobile" / "src"

# ── Files that must stay byte-identical ──────────────────────────────
# If any of these drift, the change was likely an accidental one-sided
# edit. Fix by copying the intended version to both apps.

EXACT_MATCH: set[str] = {
    "lib/actions/focus.ts",
    "lib/components/account/RoleChip.svelte",
    "lib/components/chat/FilePreview.svelte",
    "lib/components/chat/StreamingText.svelte",
    "lib/components/common/Collapsible.svelte",
    "lib/components/common/Spinner.svelte",
    "lib/components/common/ThinkingIndicator.svelte",
    "lib/components/credentials/CredentialManagerPanel.svelte",
    "lib/components/credentials/index.ts",
    "lib/components/notifications/index.ts",
    "lib/components/triggers/TriggerHistoryPanel.svelte",
    "lib/stores/chatAppBindings.svelte.ts",
    "lib/stores/credentials.svelte.ts",
    "lib/stores/health.svelte.ts",
    "lib/stores/models.svelte.ts",
    "lib/stores/serverSettings.svelte.ts",
    "lib/stores/threadConfig.svelte.ts",
    "lib/themes.ts",
    "lib/services/api.svelte.ts",
    "lib/services/api/commands.ts",
    "lib/services/api/credentials.ts",
    "lib/services/api/index.ts",
    "lib/utils/fileProcessing.ts",
    "lib/utils/ids.ts",
    "lib/utils/models.ts",
    "lib/utils/todoTools.ts",
    "lib/utils/toolSearch.ts",
}

# ── Files with documented platform-specific differences ──────────────
# See Nymeria/docs/desktop-vs-mobile.md for why each file diverges.
# These are checked for existence but not for content equality.

KNOWN_DRIFT: set[str] = {
    # Completely different implementations (ui paradigm)
    "lib/stores/ui.svelte.ts",
    # Platform-specific layouts and entry points
    "lib/components/layout/RightPanel.svelte",
    "lib/components/layout/index.ts",
    "routes/+page.svelte",
    "routes/+layout.ts",
    "app.css",
    # Different platform defaults (apiUrl)
    "lib/stores/config.svelte.ts",
    # Chat: different interaction models (keyboard vs touch)
    "lib/components/chat/InputBar.svelte",
    "lib/components/chat/ChatContainer.svelte",
    "lib/components/chat/MessageBubble.svelte",
    "lib/components/chat/ThinkingBlock.svelte",
    "lib/components/chat/ToolCallCard.svelte",
    "lib/components/chat/WorkspaceArtifactModal.svelte",
    "lib/components/chat/QueuedPromptsBar.svelte",
    "lib/components/chat/ToolReloadIndicator.svelte",
    "lib/components/chat/ContextStatusBar.svelte",
    "lib/components/chat/ImageModal.svelte",
    "lib/components/chat/index.ts",
    # Settings: different sizing, touch targets, tab labels
    "lib/components/common/SettingsPanel.svelte",
    "lib/components/common/SetupWizard.svelte",
    "lib/components/common/Button.svelte",
    "lib/components/common/ErrorToast.svelte",
    "lib/components/common/Icon.svelte",
    "lib/components/common/Modal.svelte",
    "lib/components/common/WizardShell.svelte",
    "lib/components/common/index.ts",
    # Thread list: most divergent (desktop 1258 lines, mobile 145)
    "lib/components/threads/ThreadList.svelte",
    "lib/components/threads/ThreadItem.svelte",
    "lib/components/threads/ThreadSettingsPanel.svelte",
    "lib/components/threads/ConnectTelegramWizard.svelte",
    "lib/components/threads/ConnectMyTelegramBotWizard.svelte",
    "lib/components/threads/index.ts",
    # Account: mobile touch adaptations, single-connection
    "lib/components/account/AccountBadge.svelte",
    "lib/components/account/AccountMenu.svelte",
    "lib/components/account/AccountTab.svelte",
    "lib/components/account/Avatar.svelte",
    "lib/components/account/CopyOnceTokenDialog.svelte",
    "lib/components/account/PlatformLinkingSection.svelte",
    "lib/components/account/TokenManagementSection.svelte",
    "lib/components/account/UsersTab.svelte",
    "lib/components/account/avatar.ts",
    "lib/components/account/index.ts",
    # Dashboard: different presentation
    "lib/components/dashboard/ActivityFeed.svelte",
    "lib/components/dashboard/ActivityItem.svelte",
    "lib/components/dashboard/ScheduledTasksFeed.svelte",
    "lib/components/dashboard/ScheduledTodoItem.svelte",
    "lib/components/dashboard/index.ts",
    # Tools and MCP
    "lib/components/tools/MCPInstallModal.svelte",
    "lib/components/tools/MCPManagementPanel.svelte",
    "lib/components/tools/MCPServerForm.svelte",
    "lib/components/tools/MCPServerPanel.svelte",
    "lib/components/tools/ToolCountWarning.svelte",
    "lib/components/tools/ToolManagementPanel.svelte",
    "lib/components/tools/index.ts",
    # Triggers
    "lib/components/triggers/TriggerConfigTab.svelte",
    "lib/components/triggers/TriggerFeed.svelte",
    "lib/components/triggers/TriggerItem.svelte",
    "lib/components/triggers/TriggerSetupWizard.svelte",
    # Notifications: different prop contracts (MOB-003)
    "lib/components/notifications/NotificationCenter.svelte",
    "lib/components/notifications/NotificationItem.svelte",
    # API service modules: same domain split, different platform/API surface
    "lib/services/api/accounts.ts",
    "lib/services/api/base.ts",
    "lib/services/api/chat.ts",
    "lib/services/api/mcp.ts",
    "lib/services/api/reporting.ts",
    "lib/services/api/skills.ts",
    "lib/services/api/system.ts",
    "lib/services/api/thread-config.ts",
    "lib/services/api/threads.ts",
    "lib/services/api/todos.ts",
    "lib/services/api/tools.ts",
    "lib/services/api/triggers.ts",
    # Types: desktop is superset
    "lib/types/index.ts",
    # Stores with platform-specific behavior
    "lib/stores/activity.svelte.ts",
    "lib/stores/autonomous.svelte.ts",
    "lib/stores/chat.svelte.ts",
    "lib/stores/clientId.svelte.ts",
    "lib/stores/defaultTools.svelte.ts",
    "lib/stores/errors.svelte.ts",
    "lib/stores/mcpServers.svelte.ts",
    "lib/stores/navigation.svelte.ts",
    "lib/stores/notifications.svelte.ts",
    "lib/stores/skills.svelte.ts",
    "lib/stores/threads.svelte.ts",
    "lib/stores/todos.svelte.ts",
    "lib/stores/tools.svelte.ts",
    "lib/stores/triggers.svelte.ts",
    "lib/stores/unifiedTools.svelte.ts",
    # Utils with platform tweaks
    "lib/utils/markdown.ts",
    "lib/utils/modelOptions.ts",
}

SCAN_EXTENSIONS = {".ts", ".svelte", ".css"}
EXTRA_ROOT_FILES = {"app.css"}
EXTRA_ROOT_DIRS = {"routes"}


def discover_overlap() -> set[str]:
    """Find all files that exist in both desktop and mobile src/."""
    overlap: set[str] = set()

    for desktop_file in DESKTOP_SRC.rglob("*"):
        if not desktop_file.is_file():
            continue
        if desktop_file.suffix not in SCAN_EXTENSIONS:
            continue
        rel = desktop_file.relative_to(DESKTOP_SRC)
        mobile_file = MOBILE_SRC / rel
        if mobile_file.is_file():
            overlap.add(str(rel))

    return overlap


def run_check(verbose: bool = False) -> int:
    if not DESKTOP_SRC.is_dir():
        print(f"ERROR: desktop src not found: {DESKTOP_SRC}")
        return 1
    if not MOBILE_SRC.is_dir():
        print(f"ERROR: mobile src not found: {MOBILE_SRC}")
        return 1

    overlap = discover_overlap()
    all_classified = EXACT_MATCH | KNOWN_DRIFT

    drifted: list[str] = []
    missing_desktop: list[str] = []
    missing_mobile: list[str] = []
    unclassified: list[str] = []
    stale_exact: list[str] = []
    stale_drift: list[str] = []
    now_identical: list[str] = []

    for f in sorted(EXACT_MATCH):
        d = DESKTOP_SRC / f
        m = MOBILE_SRC / f
        if not d.is_file():
            missing_desktop.append(f)
            continue
        if not m.is_file():
            missing_mobile.append(f)
            continue
        if not filecmp.cmp(str(d), str(m), shallow=False):
            drifted.append(f)

    for f in sorted(KNOWN_DRIFT):
        d = DESKTOP_SRC / f
        m = MOBILE_SRC / f
        if not d.is_file() and not m.is_file():
            stale_drift.append(f)
        elif d.is_file() and m.is_file() and filecmp.cmp(str(d), str(m), shallow=False):
            now_identical.append(f)

    for f in sorted(overlap):
        if f not in all_classified:
            unclassified.append(f)

    for f in sorted(EXACT_MATCH):
        if f not in overlap and f not in missing_desktop and f not in missing_mobile:
            stale_exact.append(f)

    errors = 0

    if drifted:
        errors += len(drifted)
        print(f"\nFAIL: {len(drifted)} exact-match file(s) have drifted:")
        for f in drifted:
            print(f"  - {f}")
        if verbose:
            for f in drifted:
                print(f"\n  diff: {f}")
                import subprocess
                subprocess.run(
                    ["diff", "-u", "--label", f"desktop/{f}", "--label", f"mobile/{f}",
                     str(DESKTOP_SRC / f), str(MOBILE_SRC / f)],
                    cwd=REPO_ROOT,
                )

    if unclassified:
        errors += len(unclassified)
        print(f"\nFAIL: {len(unclassified)} overlapping file(s) not classified:")
        for f in unclassified:
            d = DESKTOP_SRC / f
            m = MOBILE_SRC / f
            identical = filecmp.cmp(str(d), str(m), shallow=False)
            tag = "identical" if identical else "differs"
            print(f"  - {f}  ({tag})")
        print("  Add each to EXACT_MATCH or KNOWN_DRIFT in this script.")

    if missing_desktop:
        print(f"\nWARN: {len(missing_desktop)} exact-match file(s) missing from desktop:")
        for f in missing_desktop:
            print(f"  - {f}")

    if missing_mobile:
        print(f"\nWARN: {len(missing_mobile)} exact-match file(s) missing from mobile:")
        for f in missing_mobile:
            print(f"  - {f}")

    if stale_exact:
        print(f"\nWARN: {len(stale_exact)} exact-match entries no longer overlap:")
        for f in stale_exact:
            print(f"  - {f}")

    if stale_drift:
        print(f"\nINFO: {len(stale_drift)} known-drift entries no longer exist in either app:")
        for f in stale_drift:
            print(f"  - {f}")

    if now_identical:
        print(f"\nINFO: {len(now_identical)} known-drift file(s) are now byte-identical:")
        for f in now_identical:
            print(f"  - {f}  (consider promoting to EXACT_MATCH)")

    exact_ok = len(EXACT_MATCH) - len(drifted) - len(missing_desktop) - len(missing_mobile) - len(stale_exact)
    drift_ok = len(KNOWN_DRIFT) - len(stale_drift)
    print(f"\nSummary: {exact_ok} exact-match OK, {drift_ok} known-drift OK, "
          f"{len(drifted)} drifted, {len(unclassified)} unclassified")

    return 1 if errors > 0 else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check cross-app drift between nymeria-desktop and nymeria-mobile"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show unified diffs for drifted exact-match files"
    )
    args = parser.parse_args()
    sys.exit(run_check(verbose=args.verbose))


if __name__ == "__main__":
    main()
