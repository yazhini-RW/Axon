"""Getting your attention when a reminder is due.

A Windows toast plus a console line. Notification must never be the thing that crashes
the daemon, so every failure here degrades to console output instead of raising.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime

from axon.models import utcnow

APP_TITLE = "Axon"


@dataclass
class Notification:
    title: str
    body: str
    at: datetime = field(default_factory=utcnow)


def _show_toast(notification: Notification) -> bool:
    """Real Windows notification. Returns False if unavailable for any reason."""
    if sys.platform != "win32":
        return False
    try:
        from win11toast import notify as toast

        toast(notification.title, notification.body)
        return True
    except Exception:  # noqa: BLE001 - a missed toast must not kill the scheduler
        return False


def _show_console(notification: Notification) -> None:
    stamp = notification.at.astimezone().strftime("%H:%M")
    print(f"\n  [{stamp}] {notification.title}: {notification.body}", flush=True)


def send(notification: Notification, *, allow_toast: bool = True) -> list[str]:
    """Deliver a notification. Returns the channels that actually worked."""
    channels: list[str] = []

    if allow_toast and _show_toast(notification):
        channels.append("toast")

    # Always printed, even when the toast worked: toasts vanish, terminals scroll back.
    _show_console(notification)
    channels.append("console")

    return channels


def reminder_notification(text: str, *, overdue: bool = False) -> Notification:
    title = f"{APP_TITLE} - reminder" + (" (overdue)" if overdue else "")
    return Notification(title=title, body=text)
