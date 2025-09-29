"""Tests for the port monitor service orchestration."""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

pytest.importorskip("apscheduler")

from backend.app_state import BackendState
from backend.port_monitor import PortMonitorStack, PortMonitorStackManager
from backend.services import port_monitor_service
from backend.services.port_monitor_service import PortMonitorService


class FakePortMonitorManager:
    """Lightweight stand-in for ``PortMonitorStackManager`` used in tests."""

    def __init__(self) -> None:
        """Initialize the fake manager with a default stack."""

        self._stacks: list[PortMonitorStack] = [
            PortMonitorStack(
                "alpha",
                "alpha-primary",
                8080,
                ["alpha-secondary"],
                30,
                None,
                None,
            )
        ]
        self.saved_count: int = 0
        self.recheck_calls: list[str] = []
        self.restart_calls: list[str] = []

    def get_stack(self, name: str) -> PortMonitorStack | None:
        """Return the stack matching ``name`` or ``None`` if absent."""

        for stack in self._stacks:
            if stack.name == name:
                return stack
        return None

    def save_stacks(self) -> None:
        """Record that stacks were persisted."""

        self.saved_count += 1

    def recheck_stack(self, name: str) -> bool:
        """Simulate a recheck and note the invocation."""

        self.recheck_calls.append(name)
        return self.get_stack(name) is not None

    def list_stacks(self) -> list[PortMonitorStack]:
        """Return a shallow copy of configured stacks."""

        return list(self._stacks)

    def add_stack(
        self,
        name: str,
        primary_container: str,
        primary_port: int,
        secondary_containers: list[str],
        interval: int = 60,
        public_ip: str | None = None,
    ) -> None:
        """Append a new stack definition to the manager."""

        self._stacks.append(
            PortMonitorStack(
                name,
                primary_container,
                primary_port,
                secondary_containers,
                interval,
                public_ip,
                None,
            )
        )

    def remove_stack(self, name: str) -> None:
        """Remove stacks matching ``name``."""

        self._stacks = [stack for stack in self._stacks if stack.name != name]

    async def restart_stack(self, stack: PortMonitorStack) -> None:
        """Record requests to restart the provided stack."""

        self.restart_calls.append(stack.name)

    def get_docker_client(self) -> None:
        """Return ``None`` to mimic a missing docker client."""

        return


@pytest.fixture
def service(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PortMonitorService, FakePortMonitorManager, list[dict[str, Any]]]:
    """Provide a service instance backed by the fake manager and captured events."""

    state = BackendState()
    fake_manager = FakePortMonitorManager()
    state.port_monitor_manager = cast("PortMonitorStackManager", fake_manager)

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(port_monitor_service, "append_ui_event_log", events.append)

    svc = PortMonitorService(state=state, logger=logging.getLogger("test.port-monitor"))
    return svc, fake_manager, events


def test_update_stack_logs_when_changed(
    service: tuple[PortMonitorService, FakePortMonitorManager, list[dict[str, Any]]],
) -> None:
    """Updating stack fields should persist changes and emit an event when modified."""

    svc, manager, events = service
    initial_event_count = len(events)

    changed = svc.update_stack(
        name="alpha",
        primary_container="alpha-primary",
        primary_port=9090,
        secondary_containers=["alpha-secondary", "alpha-tertiary"],
        interval=45,
        public_ip="203.0.113.10",
    )

    assert changed is True
    assert manager.saved_count == 1
    assert manager.recheck_calls == ["alpha"]
    assert len(events) == initial_event_count + 1
    payload = events[-1]
    assert payload["event"] == "port_monitor_edit"
    assert payload["details"]["new"]["public_ip"] == "203.0.113.10"

    # Second update with identical values should not log a new event.
    changed_again = svc.update_stack(
        name="alpha",
        primary_container="alpha-primary",
        primary_port=9090,
        secondary_containers=["alpha-secondary", "alpha-tertiary"],
        interval=45,
        public_ip="203.0.113.10",
    )

    assert changed_again is False
    assert len(events) == initial_event_count + 1


def test_add_and_delete_stack_emit_events(
    service: tuple[PortMonitorService, FakePortMonitorManager, list[dict[str, Any]]],
) -> None:
    """Adding and deleting stacks should push corresponding UI events."""

    svc, manager, events = service

    svc.add_stack(
        name="beta",
        primary_container="beta-primary",
        primary_port=7000,
        secondary_containers=["beta-secondary"],
        interval=25,
        public_ip=None,
    )
    assert any(event["event"] == "port_monitor_created" for event in events)
    assert manager.get_stack("beta") is not None

    svc.delete_stack("beta")
    assert any(event["event"] == "port_monitor_deleted" for event in events)
    assert manager.get_stack("beta") is None


def test_restart_stack_missing_stack_raises(
    service: tuple[PortMonitorService, FakePortMonitorManager, list[dict[str, Any]]],
) -> None:
    """Restarting a missing stack should raise a ``ValueError``."""

    svc, _, _ = service

    with pytest.raises(ValueError):
        svc.restart_stack("missing")
