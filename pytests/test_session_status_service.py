"""Tests for backend.services.session_status helpers."""

from __future__ import annotations

import pytest  # type: ignore[import-not-found]

pytest.importorskip("apscheduler")

from backend.services.session_status import get_auto_update_val


@pytest.mark.parametrize(
    "status_payload,expected",
    [
        ({}, "N/A"),
        ({"auto_update_seedbox": None}, "N/A"),
        (
            {
                "auto_update_seedbox": {
                    "success": True,
                    "msg": "Updated",
                    "reason": "New IP detected",
                }
            },
            "Updated (New IP detected)",
        ),
        (
            {
                "auto_update_seedbox": {
                    "success": False,
                    "error": "Rate limit",
                    "reason": "Try again later",
                }
            },
            "Rate limit (Try again later)",
        ),
        ({"auto_update_seedbox": "Manual override"}, "Manual override"),
    ],
)
def test_get_auto_update_val(status_payload: dict[str, object], expected: str) -> None:
    """Ensure ``get_auto_update_val`` produces human-readable summaries."""

    assert get_auto_update_val(status_payload) == expected
