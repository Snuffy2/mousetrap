"""API routes for port monitoring management.

Provides endpoints to list Docker containers, and to create, update,
delete, recheck and restart port monitoring stacks. Events are emitted to
the UI event log for important actions.
"""

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.app_state import BackendState
from backend.dependencies import get_backend_state
from backend.services.port_monitor_service import PortMonitorService

_logger: logging.Logger = logging.getLogger(__name__)
router = APIRouter()


# List Docker containers endpoint
@router.get("/containers", response_model=list[str])
def list_containers(state: BackendState = Depends(get_backend_state)) -> list[str]:
    """Returns a list of running Docker container names."""
    service = PortMonitorService(state=state, logger=_logger)
    try:
        return service.list_containers()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class UpdatePortMonitorStackRequest(BaseModel):
    """Request schema for updating an existing port monitor stack.

    Fields correspond to the saved stack configuration used by the
    port monitor manager.
    """

    primary_container: str
    primary_port: int
    secondary_containers: list[str] = []
    interval: int = 60
    public_ip: str | None = None


@router.put("/stacks", response_model=dict)
def update_stack(
    name: str = Query(..., description="Stack name"),
    req: UpdatePortMonitorStackRequest = Body(...),
    state: BackendState = Depends(get_backend_state),
) -> dict[str, Any]:
    """Update fields for a named port monitor stack and trigger a recheck.

    The endpoint updates stack configuration, persists it, and triggers an
    immediate status recheck. Returns a success dict or raises HTTP errors
    for invalid stack names.
    """
    service = PortMonitorService(state=state, logger=_logger)
    try:
        service.update_stack(
            name=name,
            primary_container=req.primary_container,
            primary_port=req.primary_port,
            secondary_containers=req.secondary_containers,
            interval=req.interval,
            public_ip=req.public_ip,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Stack not found") from exc
    return {"success": True}


class PortMonitorStackModel(BaseModel):
    """Response model representing the runtime and configuration state of a stack."""

    name: str = Field(..., description="Stack name")
    primary_container: str = Field(..., description="Primary container name")
    primary_port: int = Field(..., ge=1, le=65535, description="Primary container port")
    secondary_containers: list[str] = Field([], description="Secondary containers")
    interval: int = Field(60, description="Check interval in minutes")
    status: str = Field(..., description="Current status")
    last_checked: float | None = Field(None, description="Last checked timestamp (epoch)")
    last_result: bool | None = Field(None, description="Last check result (True=OK, False=Failed)")
    public_ip: str | None = Field(
        None, description="Manual public IP override for this stack, if set."
    )
    public_ip_detected: bool | None = Field(
        None, description="Was a public IP detected for this stack's primary container?"
    )


class AddPortMonitorStackRequest(BaseModel):
    """Request schema for creating a new port monitor stack."""

    name: str
    primary_container: str
    primary_port: int
    secondary_containers: list[str] = []
    interval: int = 60
    public_ip: str | None = None


# Add update model and endpoint after router definition


@router.get("/stacks", response_model=list[PortMonitorStackModel])
def list_stacks(state: BackendState = Depends(get_backend_state)) -> list[PortMonitorStackModel]:
    """Return the configured port monitor stacks in the API response model."""
    service = PortMonitorService(state=state, logger=_logger)
    return [
        PortMonitorStackModel(
            name=stack.name,
            primary_container=stack.primary_container,
            primary_port=stack.primary_port,
            secondary_containers=stack.secondary_containers,
            interval=getattr(stack, "interval", 60),
            status=stack.status,
            last_checked=stack.last_checked,
            last_result=stack.last_result,
            public_ip=getattr(stack, "public_ip", None),
            public_ip_detected=getattr(stack, "public_ip_detected", None),
        )
        for stack in service.list_stacks()
    ]


@router.post("/stacks", response_model=dict)
def add_stack(
    req: AddPortMonitorStackRequest, state: BackendState = Depends(get_backend_state)
) -> dict[str, Any]:
    """Create a new port monitor stack and emit a UI event about it."""
    service = PortMonitorService(state=state, logger=_logger)
    service.add_stack(
        name=req.name,
        primary_container=req.primary_container,
        primary_port=req.primary_port,
        secondary_containers=req.secondary_containers,
        interval=req.interval,
        public_ip=req.public_ip,
    )
    return {"success": True}


@router.post("/stacks/recheck", response_model=dict)
def recheck_stack(
    name: str = Query(..., description="Stack name"),
    state: BackendState = Depends(get_backend_state),
) -> dict[str, Any]:
    """Trigger an immediate recheck of the named stack.

    Returns success if the stack exists and was rechecked; otherwise raises
    HTTP 404.
    """
    service = PortMonitorService(state=state, logger=_logger)
    if not service.recheck_stack(name):
        raise HTTPException(status_code=404, detail="Stack not found")
    return {"success": True}


@router.delete("/stacks", response_model=dict)
def delete_stack(
    name: str = Query(..., description="Stack name"),
    state: BackendState = Depends(get_backend_state),
) -> dict[str, Any]:
    """Remove a configured stack by name and emit a UI event about deletion."""
    service = PortMonitorService(state=state, logger=_logger)
    service.delete_stack(name)
    return {"success": True}


@router.post("/stacks/restart", response_model=dict)
def restart_stack(
    name: str = Query(..., description="Stack name"),
    state: BackendState = Depends(get_backend_state),
) -> dict[str, Any]:
    """Initiate a restart for a stack's primary container in background.

    Marks the stack restarting and runs the restart work in a daemon
    thread so the API call returns immediately.
    """
    service = PortMonitorService(state=state, logger=_logger)
    try:
        service.restart_stack(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Stack not found") from exc
    return {"success": True}
