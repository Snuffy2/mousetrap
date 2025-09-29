"""FastAPI dependency helpers for backend services."""

from __future__ import annotations

from fastapi import Request

from backend.app_state import BackendState, ensure_backend_state


def get_backend_state(request: Request) -> BackendState:
    """Return the shared :class:`BackendState` for the current FastAPI app."""
    return ensure_backend_state(request.app)
