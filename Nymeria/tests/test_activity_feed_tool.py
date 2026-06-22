"""Tests for the watchdog activity_feed tool's summary rendering."""

from __future__ import annotations

from nymeria.core.activity_log import ActivityEntry, ActivityType
from nymeria.tools.activity_feed import _build_thread_summary


def _notification(delivered_to=None) -> ActivityEntry:
    metadata = {"profile": "default", "errors": []}
    if delivered_to is not None:
        metadata["delivered_to"] = delivered_to
    return ActivityEntry(
        type=ActivityType.NOTIFICATION_SENT,
        message="Notification sent",
        metadata=metadata,
    )


def test_notification_summary_lists_delivered_channels():
    # notify() records the delivered channels under "delivered_to"; the feed must
    # surface them (the old code read a never-written "platforms" key and so
    # always fell through to the bare count).
    entries = [
        _notification(delivered_to=["telegram", "discord"]),
        _notification(delivered_to=["email"]),
    ]

    summary = _build_thread_summary(entries)

    assert "Notifications sent: telegram, discord, email" in summary


def test_notification_summary_falls_back_to_count_without_channels():
    entries = [_notification(delivered_to=None), _notification(delivered_to=[])]

    summary = _build_thread_summary(entries)

    assert "Notifications sent: 2" in summary
