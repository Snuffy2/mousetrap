"""Service layer modules for Mousetrap backend."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "NotificationsService",
    "PortMonitorService",
    "SessionSchedulerService",
    "SessionStatusService",
]


def __getattr__(name: str) -> Any:
    """Lazily import service classes on first access."""

    if name == "NotificationsService":
        module = import_module("backend.services.notifications_service")
        return module.NotificationsService
    if name == "PortMonitorService":
        module = import_module("backend.services.port_monitor_service")
        return module.PortMonitorService
    if name == "SessionSchedulerService":
        module = import_module("backend.services.session_scheduler")
        return module.SessionSchedulerService
    if name == "SessionStatusService":
        module = import_module("backend.services.session_status")
        return module.SessionStatusService
    raise AttributeError(name)
