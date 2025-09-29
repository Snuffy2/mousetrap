"""Shared backend application state and supporting utilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import threading
import time
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from fastapi import FastAPI

from backend.port_monitor import PortMonitorStackManager
from backend.services.automation_service import AutomationService
from backend.services.notifications_service import NotificationsService


@dataclass(slots=True)
class WarningThrottler:
    """Utility for rate-limiting log messages or other repeated actions."""

    _last_seen: dict[str, float] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def should_log(self, key: str, min_interval_seconds: float) -> bool:
        """Return True when the minimum interval has elapsed for *key*."""
        now = time.monotonic()
        with self._lock:
            last = self._last_seen.get(key)
            if last is None or (now - last) >= min_interval_seconds:
                self._last_seen[key] = now
                return True
            return False


@dataclass(slots=True)
class NotificationDeduplicator:
    """Deduplicate notifications over a configurable time window."""

    _cache: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def should_send(
        self,
        mam_id: str,
        event_type: str,
        old_count: int,
        new_count: int,
        *,
        dedup_window_minutes: int = 60,
    ) -> bool:
        """Return True if the notification should be emitted for the count change."""
        if not mam_id:
            return True

        count_change_key = f"{old_count}→{new_count}"
        now = time.time()
        dedup_window_seconds = dedup_window_minutes * 60

        with self._lock:
            event_cache = self._cache.setdefault(mam_id, {}).setdefault(event_type, {})
            last_notification_time = event_cache.get(count_change_key)

            if last_notification_time and (now - last_notification_time) < dedup_window_seconds:
                return False

            event_cache[count_change_key] = now
            cutoff_time = now - dedup_window_seconds
            stale_keys = [key for key, ts in event_cache.items() if ts < cutoff_time]
            for key in stale_keys:
                del event_cache[key]
            return True


@dataclass(slots=True)
class SessionStatusCache:
    """Thread-safe in-memory cache for session status responses."""

    _entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def clear(self) -> None:
        """Remove all cached session entries."""
        with self._lock:
            self._entries.clear()

    def contains(self, label: str) -> bool:
        """Return True when the cache contains an entry for *label*."""
        with self._lock:
            return label in self._entries

    def get_entry(self, label: str) -> dict[str, Any]:
        """Return a shallow copy of the cached entry for *label* if present."""
        with self._lock:
            entry = self._entries.get(label, {})
            return dict(entry)

    def get_status(self, label: str) -> dict[str, Any] | None:
        """Return the cached status for *label*, if any."""
        with self._lock:
            entry = self._entries.get(label)
            if not entry:
                return None
            status = entry.get("status")
            return dict(status) if isinstance(status, dict) else status

    def get_last_check_time(self, label: str) -> str | None:
        """Return the last check timestamp recorded for *label*."""
        with self._lock:
            entry = self._entries.get(label)
            if not entry:
                return None
            return entry.get("last_check_time")

    def has_status(self, label: str) -> bool:
        """Return True when a status object is cached for *label*."""
        with self._lock:
            entry = self._entries.get(label)
            return bool(entry and entry.get("status"))

    def ensure_entry(self, label: str) -> dict[str, Any]:
        """Return the existing entry for *label*, creating it if necessary."""
        with self._lock:
            return self._entries.setdefault(label, {})

    def mark_suppress_next_event(self, label: str) -> None:
        """Mark the cached entry so the next event log is suppressed."""
        with self._lock:
            entry = self._entries.setdefault(label, {})
            entry["suppress_next_event"] = True

    def should_suppress_next_event(self, label: str) -> bool:
        """Return True if the next event log for *label* should be suppressed."""
        with self._lock:
            entry = self._entries.get(label)
            return bool(entry and entry.get("suppress_next_event"))

    def pop_suppress_next_event(self, label: str) -> bool:
        """Clear the suppression flag for *label*, returning its previous value."""
        with self._lock:
            entry = self._entries.get(label)
            if not entry:
                return False
            return bool(entry.pop("suppress_next_event", None))

    def set_status(self, label: str, status: dict[str, Any], timestamp: datetime | str) -> None:
        """Persist a new status payload for *label* with the provided timestamp."""
        iso_timestamp = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
        with self._lock:
            self._entries[label] = {"status": status, "last_check_time": iso_timestamp}


@dataclass(slots=True)
class BackendState:
    """Aggregates long-lived backend services and shared state."""

    scheduler: BackgroundScheduler = field(default_factory=BackgroundScheduler)
    port_monitor_manager: PortMonitorStackManager = field(default_factory=PortMonitorStackManager)
    session_status_cache: SessionStatusCache = field(default_factory=SessionStatusCache)
    notification_deduplicator: NotificationDeduplicator = field(
        default_factory=NotificationDeduplicator
    )
    warning_throttler: WarningThrottler = field(default_factory=WarningThrottler)
    notifications_service: NotificationsService = field(init=False)
    automation_service: AutomationService = field(init=False)

    def __post_init__(self) -> None:
        """Initialize dependent services that require the state instance."""

        self.notifications_service = NotificationsService(state=self)
        self.automation_service = AutomationService(
            state=self, notifications_service=self.notifications_service
        )

    def shutdown(self) -> None:
        """Stop background services gracefully."""
        if getattr(self.scheduler, "running", False):
            self.scheduler.shutdown(wait=False)
        self.port_monitor_manager.stop()


def ensure_backend_state(app: FastAPI) -> BackendState:
    """Attach and return a reusable :class:`BackendState` for ``app``."""
    state: BackendState | None = getattr(app.state, "backend_state", None)
    if state is None:
        state = BackendState()
        app.state.backend_state = state
    return state
