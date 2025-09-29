"""Service layer orchestration for port monitor operations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import threading

from backend.app_state import BackendState
from backend.event_log import append_ui_event_log
from backend.port_monitor import PortMonitorStack

_CONTAINERS_WARN_INTERVAL_SECONDS = 60


@dataclass(slots=True)
class PortMonitorService:
    """Encapsulate orchestration logic for port monitor stack management."""

    state: BackendState
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))

    def list_containers(self) -> list[str]:
        """Return the list of running Docker containers available to the monitor."""

        manager = self.state.port_monitor_manager
        client = manager.get_docker_client()
        if not client:
            throttle_key = "port_monitor.containers"
            if self.state.warning_throttler.should_log(
                throttle_key, _CONTAINERS_WARN_INTERVAL_SECONDS
            ):
                self.logger.warning(
                    "[PortMonitorService] Docker client unavailable; returning empty container list"
                )
            return []
        try:
            containers = client.containers.list()
        except Exception as exc:  # pragma: no cover - defensive logging
            self.logger.exception("[PortMonitorService] Failed to list containers")
            raise RuntimeError(f"Error listing containers: {exc}") from exc
        return [container.name for container in containers]

    def list_stacks(self) -> list[PortMonitorStack]:
        """Return all configured port monitor stacks."""

        return self.state.port_monitor_manager.list_stacks()

    def add_stack(
        self,
        *,
        name: str,
        primary_container: str,
        primary_port: int,
        secondary_containers: list[str],
        interval: int = 60,
        public_ip: str | None = None,
    ) -> None:
        """Create a new port monitor stack and emit a creation event."""

        self.state.port_monitor_manager.add_stack(
            name,
            primary_container,
            primary_port,
            secondary_containers,
            interval,
            public_ip,
        )
        append_ui_event_log(
            {
                "event": "port_monitor_created",
                "event_type": "port_monitor_create",
                "label": name,
                "stack": name,
                "timestamp": datetime.now(UTC).isoformat(),
                "status_message": (
                    f"Stack '{name}' created: primary={primary_container}:{primary_port}, "
                    f"secondaries={secondary_containers}, interval={interval} minutes."
                ),
                "details": {
                    "primary_container": primary_container,
                    "primary_port": primary_port,
                    "secondary_containers": secondary_containers,
                    "interval": interval,
                },
                "message": f"Stack '{name}' created.",
                "level": "info",
            }
        )

    def update_stack(
        self,
        *,
        name: str,
        primary_container: str,
        primary_port: int,
        secondary_containers: list[str],
        interval: int,
        public_ip: str | None,
    ) -> bool:
        """Update the configuration for an existing stack and trigger a recheck.

        Returns True if any stack fields changed from their previous values.
        """

        manager = self.state.port_monitor_manager
        stack = manager.get_stack(name)
        if not stack:
            raise ValueError(f"Stack '{name}' not found")

        changed = (
            stack.primary_container != primary_container
            or stack.primary_port != primary_port
            or stack.secondary_containers != secondary_containers
            or stack.interval != interval
            or getattr(stack, "public_ip", None) != public_ip
        )
        old_values = {
            "primary_container": stack.primary_container,
            "primary_port": stack.primary_port,
            "secondary_containers": stack.secondary_containers,
            "interval": getattr(stack, "interval", interval),
            "public_ip": getattr(stack, "public_ip", None),
        }

        stack.primary_container = primary_container
        stack.primary_port = primary_port
        stack.secondary_containers = secondary_containers
        stack.interval = interval
        stack.public_ip = public_ip
        manager.save_stacks()
        manager.recheck_stack(name)

        if changed:
            append_ui_event_log(
                {
                    "event": "port_monitor_edit",
                    "event_type": "port_monitor_edit",
                    "label": name,
                    "stack": name,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "status_message": (
                        f"Stack '{name}' edited: primary={primary_container}:{primary_port}, "
                        f"secondaries={secondary_containers}, interval={interval} minutes."
                    ),
                    "details": {
                        "old": old_values,
                        "new": {
                            "primary_container": primary_container,
                            "primary_port": primary_port,
                            "secondary_containers": secondary_containers,
                            "interval": interval,
                            "public_ip": public_ip,
                        },
                    },
                    "message": f"Stack '{name}' edited",
                    "level": "info",
                }
            )
        return changed

    def recheck_stack(self, name: str) -> bool:
        """Trigger an immediate recheck for the named stack."""

        return self.state.port_monitor_manager.recheck_stack(name)

    def delete_stack(self, name: str) -> None:
        """Remove a stack and emit a deletion event."""

        self.state.port_monitor_manager.remove_stack(name)
        append_ui_event_log(
            {
                "event": "port_monitor_deleted",
                "event_type": "port_monitor_delete",
                "label": name,
                "stack": name,
                "timestamp": datetime.now(UTC).isoformat(),
                "status_message": f"Stack '{name}' deleted.",
                "details": {},
                "message": f"Stack '{name}' deleted.",
                "level": "info",
            }
        )

    def restart_stack(self, name: str) -> None:
        """Start an asynchronous restart workflow for the specified stack."""

        manager = self.state.port_monitor_manager
        stack = manager.get_stack(name)
        if not stack:
            raise ValueError(f"Stack '{name}' not found")

        stack.status = "Restarting"
        manager.save_stacks()
        append_ui_event_log(
            {
                "event": "port_monitor_status",
                "stack": name,
                "status": "Restarting",
                "message": f"Stack '{name}' status set to 'Restarting'",
                "level": "info",
            }
        )

        async def _perform_restart() -> None:
            """Run the restart coroutine and recheck workflow."""

            try:
                self.logger.info("[PortMonitorService] Restart thread started for stack '%s'", name)
                append_ui_event_log(
                    {
                        "event": "port_monitor_restart_started",
                        "stack": name,
                        "message": f"Background restart thread started for stack '{name}'",
                        "level": "info",
                    }
                )
                await manager.restart_stack(stack)
                append_ui_event_log(
                    {
                        "event": "port_monitor_restart_complete",
                        "stack": name,
                        "message": f"Restart complete for stack '{name}', rechecking status...",
                        "level": "info",
                    }
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.error(
                    "[PortMonitorService] Restart failed for stack '%s': %s", name, exc
                )
                append_ui_event_log(
                    {
                        "event": "port_monitor_restart_error",
                        "stack": name,
                        "message": f"Restart failed for stack '{name}': {exc}",
                        "level": "error",
                    }
                )
            else:
                manager.recheck_stack(stack.name)
                append_ui_event_log(
                    {
                        "event": "port_monitor_status_rechecked",
                        "stack": name,
                        "message": f"Status recheck complete for stack '{name}'",
                        "level": "info",
                    }
                )

        threading.Thread(target=lambda: asyncio.run(_perform_restart()), daemon=True).start()
