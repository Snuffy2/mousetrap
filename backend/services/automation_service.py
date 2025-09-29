"""Service-layer orchestration for MaM perk automation jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any

from backend.config import list_sessions, load_session, save_session
from backend.event_log import append_ui_event_log
from backend.mam_api import get_status
from backend.perk_automation import buy_upload_credit, buy_vip, buy_wedge
from backend.proxy_config import resolve_proxy_from_session_cfg
from backend.services.notifications_service import NotificationsService

if TYPE_CHECKING:
    from backend.app_state import BackendState


@dataclass(slots=True)
class AutomationService:
    """Coordinate automated MaM perk purchases across all sessions."""

    state: BackendState | None = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))
    notifications_service: NotificationsService | None = None

    def __post_init__(self) -> None:
        """Ensure a notifications service is available for the automation jobs."""

        if self.notifications_service is None:
            if self.state and getattr(self.state, "notifications_service", None):
                self.notifications_service = self.state.notifications_service
            else:
                self.notifications_service = NotificationsService(state=self.state)
        if self.notifications_service is None:  # pragma: no cover - defensive check
            raise RuntimeError("Failed to initialize notifications service for automation")

    async def run_all_jobs(self) -> None:
        """Execute every automation job sequentially."""

        await self.run_upload_credit_job()
        await self.run_wedge_job()
        await self.run_vip_job()

    async def run_upload_credit_job(self) -> None:
        """Evaluate and run upload credit automation for all sessions."""

        notifier = self._get_notifier()
        session_labels = list_sessions()
        now = datetime.now(UTC)
        for label in session_labels:
            try:
                cfg = load_session(label)
                if not cfg:
                    continue
                mam_id = cfg.get("mam", {}).get("mam_id", "")
                if not mam_id:
                    continue
                automation = cfg.get("perk_automation", {}).get("upload_credit", {})
                enabled = automation.get("enabled", False)
                if not enabled:
                    continue
                trigger_type = automation.get("trigger_type", "points")
                trigger_days = automation.get("trigger_days", 7)
                trigger_point_threshold = automation.get("trigger_point_threshold", 50000)
                gb_amount = automation.get("gb", 10)

                valid_amounts = [1, 2.5, 5, 20, 100]
                if gb_amount not in valid_amounts:
                    self.logger.error(
                        "[UploadAuto] Invalid upload credit amount configured: %sGB. Skipping session '%s'. Valid amounts are: %s",
                        gb_amount,
                        label,
                        ", ".join(map(str, valid_amounts)),
                    )
                    continue

                proxy_cfg = resolve_proxy_from_session_cfg(cfg)
                status = await get_status(mam_id=mam_id, proxy_cfg=proxy_cfg)
                points = status.get("points", 0) if isinstance(status, dict) else 0
                if points is None:
                    points = 0
                session_min_points = cfg.get("perk_automation", {}).get("min_points")
                if session_min_points is not None and int(points) < int(session_min_points):
                    guardrail_reason = (
                        f"Below session minimum points: {points} < {session_min_points}"
                    )
                    log_msg = (
                        "[AutoUpload] SKIP: Automated Upload Credit purchase for session '%s' "
                        "skipped: %s"
                    )
                    self.logger.info(log_msg, label, guardrail_reason)
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "automation",
                            "trigger": "automation",
                            "purchase_type": "upload_credit",
                            "amount": gb_amount,
                            "details": {"points_before": points},
                            "result": "skipped",
                            "status_message": (
                                f"Automated Upload Credit purchase skipped: {guardrail_reason}"
                            ),
                        }
                    )
                    continue
                last_upload_time = (
                    cfg.get("perk_automation", {}).get("upload_credit", {}).get("last_upload_time")
                )
                last_purchase = None
                if last_upload_time:
                    try:
                        last_purchase = datetime.fromisoformat(last_upload_time)
                    except Exception:  # pragma: no cover - defensive parsing
                        last_purchase = None
                now_dt = now if isinstance(now, datetime) else datetime.now(UTC)
                time_trigger_ok = True
                if trigger_type in ("time", "both"):
                    if last_purchase:
                        next_allowed = last_purchase + timedelta(days=int(trigger_days))
                        if now_dt < next_allowed:
                            time_trigger_ok = False
                    else:
                        time_trigger_ok = False
                if not time_trigger_ok:
                    if last_purchase:
                        next_allowed = last_purchase + timedelta(days=int(trigger_days))
                        next_allowed_str = next_allowed.isoformat()
                        guardrail_reason = f"Time-based trigger not satisfied: next allowed after {next_allowed_str}"
                    else:
                        guardrail_reason = (
                            "No previous purchase timestamp found. Please toggle and save the "
                            "automation to start the timer. (Time-based trigger not satisfied.)"
                        )
                    log_msg = (
                        "[AutoUpload] SKIP: Automated Upload Credit purchase for session '%s' "
                        "skipped: %s"
                    )
                    self.logger.info(log_msg, label, guardrail_reason)
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "automation",
                            "trigger": "automation",
                            "purchase_type": "upload_credit",
                            "amount": gb_amount,
                            "details": {"points_before": points},
                            "result": "skipped",
                            "status_message": (
                                f"Automated Upload Credit purchase skipped: {guardrail_reason}"
                            ),
                        }
                    )
                    continue
                if trigger_type in ("points", "both") and int(points) < int(
                    trigger_point_threshold
                ):
                    guardrail_reason = (
                        f"Below automation point threshold: {points} < {trigger_point_threshold}"
                    )
                    log_msg = (
                        "[AutoUpload] SKIP: Automated Upload Credit purchase for session '%s' "
                        "skipped: %s"
                    )
                    self.logger.info(log_msg, label, guardrail_reason)
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "automation",
                            "trigger": "automation",
                            "purchase_type": "upload_credit",
                            "amount": gb_amount,
                            "details": {"points_before": points},
                            "result": "skipped",
                            "status_message": (
                                f"Automated Upload Credit purchase skipped: {guardrail_reason}"
                            ),
                        }
                    )
                    continue
                result = await buy_upload_credit(gb_amount, mam_id=mam_id, proxy_cfg=proxy_cfg)
                success = result.get("success", False) if isinstance(result, dict) else False
                status_message = (
                    f"Automated purchase: Upload Credit ({gb_amount} GB)"
                    if success
                    else f"Automated Upload Credit purchase failed ({gb_amount} GB)"
                )
                event: dict[str, Any] = {
                    "timestamp": now.isoformat(),
                    "label": label,
                    "event_type": "automation",
                    "trigger": "automation",
                    "purchase_type": "upload_credit",
                    "amount": gb_amount,
                    "details": {"points_before": points},
                    "result": "success" if success else "failed",
                    "error": None
                    if success
                    else (result.get("error") if isinstance(result, dict) else "Unknown error"),
                    "status_message": status_message,
                }

                if success:
                    self.logger.info(
                        "[UploadAuto] Automated purchase: Upload Credit (%s GB) for session '%s' succeeded.",
                        gb_amount,
                        label,
                    )
                    cfg.setdefault("perk_automation", {}).setdefault("upload_credit", {})[
                        "last_upload_time"
                    ] = now_dt.isoformat()
                    save_session(cfg, old_label=label)
                    await notifier.notify_event(
                        event_type="automation_success",
                        label=label,
                        status="SUCCESS",
                        message=(f"Automated Upload Credit purchase succeeded: {gb_amount} GB"),
                        details={"amount": gb_amount, "points_before": points},
                    )
                else:
                    error_message = None
                    if isinstance(result, dict):
                        error_message = result.get("error") or result.get("response")
                    self.logger.warning(
                        "[UploadAuto] Automated purchase: Upload Credit (%s GB) for session '%s' FAILED. Error: %s",
                        gb_amount,
                        label,
                        error_message or "Unknown error",
                    )
                    await notifier.notify_event(
                        event_type="automation_failure",
                        label=label,
                        status="FAILED",
                        message=(f"Automated Upload Credit purchase failed: {gb_amount} GB"),
                        details={
                            "amount": gb_amount,
                            "points_before": points,
                            "error": error_message or event["error"],
                        },
                    )
                append_ui_event_log(event)
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.error("[UploadAuto] Error for '%s': %s", label, exc)

    async def run_vip_job(self) -> None:
        """Evaluate and run VIP automation for all sessions."""

        notifier = self._get_notifier()
        session_labels = list_sessions()
        now = datetime.now(UTC)
        for label in session_labels:
            try:
                cfg = load_session(label)
                if not cfg:
                    continue
                mam_id = cfg.get("mam", {}).get("mam_id", "")
                if not mam_id:
                    continue
                automation = cfg.get("perk_automation", {}).get("vip_automation", {})
                enabled = automation.get("enabled", False)
                if not enabled:
                    continue
                trigger_type = automation.get("trigger_type", "points")
                trigger_days = automation.get("trigger_days", 7)
                trigger_point_threshold = automation.get("trigger_point_threshold", 50000)

                proxy_cfg = resolve_proxy_from_session_cfg(cfg)
                weeks = automation.get("weeks", 4)
                status = await get_status(mam_id=mam_id, proxy_cfg=proxy_cfg)
                points = status.get("points", 0) if isinstance(status, dict) else 0
                if points is None:
                    points = 0
                session_min_points = cfg.get("perk_automation", {}).get("min_points")
                if session_min_points is not None and int(points) < int(session_min_points):
                    guardrail_reason = (
                        f"Below session minimum points: {points} < {session_min_points}"
                    )
                    log_msg = "[AutoVIP] SKIP: Automated VIP purchase for session '%s' skipped: %s"
                    self.logger.info(log_msg, label, guardrail_reason)
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "automation",
                            "trigger": "automation",
                            "purchase_type": "vip",
                            "amount": weeks,
                            "details": {"points_before": points},
                            "result": "skipped",
                            "status_message": (
                                f"Automated VIP purchase skipped: {guardrail_reason}"
                            ),
                        }
                    )
                    if "retry" in automation:
                        automation.pop("retry", None)
                        automation.pop("cooldown_until", None)
                        save_session(cfg, old_label=label)
                    continue
                last_vip_time = (
                    cfg.get("perk_automation", {}).get("vip_automation", {}).get("last_vip_time")
                )
                last_purchase = None
                if last_vip_time:
                    try:
                        last_purchase = datetime.fromisoformat(last_vip_time)
                    except Exception:  # pragma: no cover - defensive parsing
                        last_purchase = None
                now_dt = now if isinstance(now, datetime) else datetime.now(UTC)
                time_trigger_ok = True
                if trigger_type in ("time", "both"):
                    if last_purchase:
                        next_allowed = last_purchase + timedelta(days=int(trigger_days))
                        if now_dt < next_allowed:
                            time_trigger_ok = False
                    else:
                        time_trigger_ok = False
                if not time_trigger_ok:
                    if last_purchase:
                        next_allowed = last_purchase + timedelta(days=int(trigger_days))
                        next_allowed_str = next_allowed.isoformat()
                        guardrail_reason = f"Time-based trigger not satisfied: next allowed after {next_allowed_str}"
                    else:
                        guardrail_reason = (
                            "No previous purchase timestamp found. Please toggle and save the "
                            "automation to start the timer. (Time-based trigger not satisfied.)"
                        )
                    log_msg = "[AutoVIP] SKIP: Automated VIP purchase for session '%s' skipped: %s"
                    self.logger.info(log_msg, label, guardrail_reason)
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "automation",
                            "trigger": "automation",
                            "purchase_type": "vip",
                            "amount": weeks,
                            "details": {"points_before": points},
                            "result": "skipped",
                            "status_message": (
                                f"Automated VIP purchase skipped: {guardrail_reason}"
                            ),
                        }
                    )
                    if "retry" in automation:
                        automation.pop("retry", None)
                        automation.pop("cooldown_until", None)
                        save_session(cfg, old_label=label)
                    continue
                if trigger_type in ("points", "both") and int(points) < int(
                    trigger_point_threshold
                ):
                    guardrail_reason = (
                        f"Below automation point threshold: {points} < {trigger_point_threshold}"
                    )
                    log_msg = "[AutoVIP] SKIP: Automated VIP purchase for session '%s' skipped: %s"
                    self.logger.info(log_msg, label, guardrail_reason)
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "automation",
                            "trigger": "automation",
                            "purchase_type": "vip",
                            "amount": weeks,
                            "details": {"points_before": points},
                            "result": "skipped",
                            "status_message": (
                                f"Automated VIP purchase skipped: {guardrail_reason}"
                            ),
                        }
                    )
                    if "retry" in automation:
                        automation.pop("retry", None)
                        automation.pop("cooldown_until", None)
                        save_session(cfg, old_label=label)
                    continue
                retry = automation.get("retry", 0)
                cooldown_until = automation.get("cooldown_until")
                now_ts = int(time.time())
                if cooldown_until and now_ts < cooldown_until:
                    self.logger.info(
                        "[VIPAuto] label=%s trigger=automation result=skipped reason=cooldown active until %s",
                        label,
                        cooldown_until,
                    )
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "automation",
                            "trigger": "automation",
                            "purchase_type": "vip",
                            "amount": weeks,
                            "details": {"points_before": points},
                            "result": "skipped",
                            "status_message": f"Cooldown active until {cooldown_until}",
                        }
                    )
                    continue
                last_fail_time = automation.get("last_fail_time", 0)
                if retry > 0 and (now_ts - last_fail_time) < 60:
                    self.logger.info(
                        "[VIPAuto] label=%s trigger=automation result=skipped reason=waiting_between_retries retry=%s",
                        label,
                        retry,
                    )
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "automation",
                            "trigger": "automation",
                            "purchase_type": "vip",
                            "amount": weeks,
                            "details": {"points_before": points},
                            "result": "skipped",
                            "status_message": f"Waiting between retries (retry {retry})",
                        }
                    )
                    continue
                is_max = str(weeks).lower() in ["max", "90"]
                duration = "max" if is_max else str(weeks)
                result = await buy_vip(mam_id, duration=duration, proxy_cfg=proxy_cfg)
                success = result.get("success", False) if isinstance(result, dict) else False
                status_message = (
                    f"Automated purchase: VIP ({'Max me out!' if is_max else f'{weeks} weeks'})"
                    if success
                    else f"Automated VIP purchase failed ({'Max me out!' if is_max else f'{weeks} weeks'})"
                )
                event: dict[str, Any] = {
                    "timestamp": now.isoformat(),
                    "label": label,
                    "event_type": "automation",
                    "trigger": "automation",
                    "purchase_type": "vip",
                    "amount": weeks,
                    "details": {"points_before": points},
                    "result": "success" if success else "failed",
                    "error": None
                    if success
                    else (result.get("error") if isinstance(result, dict) else "Unknown error"),
                    "status_message": status_message,
                }

                if success:
                    self.logger.info(
                        "[VIPAuto] Automated purchase: VIP (%s) for session '%s' succeeded.",
                        "max" if is_max else weeks,
                        label,
                    )
                    cfg.setdefault("perk_automation", {}).setdefault("vip_automation", {})[
                        "last_vip_time"
                    ] = now_dt.isoformat()
                    automation["retry"] = 0
                    automation.pop("cooldown_until", None)
                    automation.pop("last_fail_time", None)
                    save_session(cfg, old_label=label)
                    await notifier.notify_event(
                        event_type="automation_success",
                        label=label,
                        status="SUCCESS",
                        message=(
                            "Automated VIP purchase succeeded: "
                            + ("Max me out!" if is_max else f"{weeks} weeks")
                        ),
                        details={"amount": weeks, "points_before": points},
                    )
                else:
                    error_message = None
                    if isinstance(result, dict):
                        error_message = result.get("error") or result.get("response")
                    self.logger.warning(
                        "[VIPAuto] Automated purchase: VIP (%s) for session '%s' FAILED. Error: %s",
                        "max" if is_max else weeks,
                        label,
                        error_message or "Unknown error",
                    )
                    retry = automation.get("retry", 0) + 1
                    automation["retry"] = retry
                    automation["last_fail_time"] = now_ts
                    if retry >= 3:
                        automation["cooldown_until"] = now_ts + 600
                        self.logger.warning(
                            "[VIPAuto] Automated purchase: VIP (%s) for session '%s' retries_exceeded, cooldown_until=%s",
                            "max" if is_max else weeks,
                            label,
                            automation["cooldown_until"],
                        )
                    save_session(cfg, old_label=label)
                    await notifier.notify_event(
                        event_type="automation_failure",
                        label=label,
                        status="FAILED",
                        message=(
                            "Automated VIP purchase failed: "
                            + ("Max me out!" if is_max else f"{weeks} weeks")
                        ),
                        details={
                            "amount": weeks,
                            "points_before": points,
                            "error": error_message or event["error"],
                        },
                    )
                append_ui_event_log(event)
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.error(
                    "[VIPAuto] label=%s trigger=automation result=exception error=%s",
                    label,
                    exc,
                )

    async def run_wedge_job(self) -> None:
        """Evaluate and run wedge automation for all sessions."""

        notifier = self._get_notifier()
        session_labels = list_sessions()
        now = datetime.now(UTC)
        for label in session_labels:
            try:
                cfg = load_session(label)
                if not cfg:
                    continue
                mam_id = cfg.get("mam", {}).get("mam_id", "")
                if not mam_id:
                    continue
                automation = cfg.get("perk_automation", {}).get("wedge_automation", {})
                enabled = automation.get("enabled", False)
                if not enabled:
                    continue
                trigger_type = automation.get("trigger_type", "points")
                trigger_days = automation.get("trigger_days", 7)
                trigger_point_threshold = automation.get("trigger_point_threshold", 50000)

                proxy_cfg = resolve_proxy_from_session_cfg(cfg)
                status = await get_status(mam_id=mam_id, proxy_cfg=proxy_cfg)
                points = status.get("points", 0) if isinstance(status, dict) else 0
                if points is None:
                    points = 0
                session_min_points = cfg.get("perk_automation", {}).get("min_points")
                self.logger.debug(
                    "[AutoWedge][DEBUG] Session '%s': points=%s, session_min_points=%s",
                    label,
                    points,
                    session_min_points,
                )
                if session_min_points is not None and int(points) < int(session_min_points):
                    guardrail_reason = (
                        f"Below session minimum points: {points} < {session_min_points}"
                    )
                    log_msg = (
                        "[AutoWedge] SKIP: Automated Wedge purchase for session '%s' skipped: %s"
                    )
                    self.logger.info(log_msg, label, guardrail_reason)
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "automation",
                            "trigger": "automation",
                            "purchase_type": "wedge",
                            "amount": 1,
                            "details": {"points_before": points},
                            "result": "skipped",
                            "status_message": (
                                f"Automated Wedge purchase skipped: {guardrail_reason}"
                            ),
                        }
                    )
                    continue
                last_wedge_time = (
                    cfg.get("perk_automation", {})
                    .get("wedge_automation", {})
                    .get("last_wedge_time")
                )
                last_purchase = None
                if last_wedge_time:
                    try:
                        last_purchase = datetime.fromisoformat(last_wedge_time)
                    except Exception:  # pragma: no cover - defensive parsing
                        last_purchase = None
                now_dt = now if isinstance(now, datetime) else datetime.now(UTC)
                time_trigger_ok = True
                if trigger_type in ("time", "both"):
                    if last_purchase:
                        next_allowed = last_purchase + timedelta(days=int(trigger_days))
                        if now_dt < next_allowed:
                            time_trigger_ok = False
                    else:
                        time_trigger_ok = False
                if not time_trigger_ok:
                    if last_purchase:
                        next_allowed = last_purchase + timedelta(days=int(trigger_days))
                        next_allowed_str = next_allowed.isoformat()
                        guardrail_reason = f"Time-based trigger not satisfied: next allowed after {next_allowed_str}"
                    else:
                        guardrail_reason = (
                            "No previous purchase timestamp found. Please toggle and save the "
                            "automation to start the timer. (Time-based trigger not satisfied.)"
                        )
                    log_msg = (
                        "[AutoWedge] SKIP: Automated Wedge purchase for session '%s' skipped: %s"
                    )
                    self.logger.info(log_msg, label, guardrail_reason)
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "automation",
                            "trigger": "automation",
                            "purchase_type": "wedge",
                            "amount": 1,
                            "details": {"points_before": points},
                            "result": "skipped",
                            "status_message": (
                                f"Automated Wedge purchase skipped: {guardrail_reason}"
                            ),
                        }
                    )
                    continue
                if trigger_type in ("points", "both") and int(points) < int(
                    trigger_point_threshold
                ):
                    guardrail_reason = (
                        f"Below automation point threshold: {points} < {trigger_point_threshold}"
                    )
                    log_msg = (
                        "[AutoWedge] SKIP: Automated Wedge purchase for session '%s' skipped: %s"
                    )
                    self.logger.info(log_msg, label, guardrail_reason)
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "automation",
                            "trigger": "automation",
                            "purchase_type": "wedge",
                            "amount": 1,
                            "details": {"points_before": points},
                            "result": "skipped",
                            "status_message": (
                                f"Automated Wedge purchase skipped: {guardrail_reason}"
                            ),
                        }
                    )
                    continue
                result = await buy_wedge(mam_id, proxy_cfg=proxy_cfg)
                success = result.get("success", False) if isinstance(result, dict) else False
                status_message = (
                    "Automated purchase: Wedge (points)"
                    if success
                    else "Automated Wedge purchase failed (points)"
                )
                event: dict[str, Any] = {
                    "timestamp": now.isoformat(),
                    "label": label,
                    "event_type": "automation",
                    "trigger": "automation",
                    "purchase_type": "wedge",
                    "amount": 1,
                    "details": {"points_before": points},
                    "result": "success" if success else "failed",
                    "error": None
                    if success
                    else (result.get("error") if isinstance(result, dict) else "Unknown error"),
                    "status_message": status_message,
                }

                if success:
                    cfg.setdefault("perk_automation", {}).setdefault("wedge_automation", {})[
                        "last_wedge_time"
                    ] = now_dt.isoformat()
                    save_session(cfg, old_label=label)
                    self.logger.info(
                        "[WedgeAuto] Automated purchase: Wedge (points) for session '%s' succeeded.",
                        label,
                    )
                    await notifier.notify_event(
                        event_type="automation_success",
                        label=label,
                        status="SUCCESS",
                        message="Automated Wedge purchase succeeded: 1",
                        details={"amount": 1, "points_before": points},
                    )
                else:
                    error_message = None
                    if isinstance(result, dict):
                        error_message = result.get("error") or result.get("response")
                    self.logger.warning(
                        "[WedgeAuto] Automated purchase: Wedge (points) for session '%s' FAILED. Error: %s",
                        label,
                        error_message or "Unknown error",
                    )
                    await notifier.notify_event(
                        event_type="automation_failure",
                        label=label,
                        status="FAILED",
                        message="Automated Wedge purchase failed: 1",
                        details={
                            "amount": 1,
                            "points_before": points,
                            "error": error_message or event["error"],
                        },
                    )
                append_ui_event_log(event)
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.error(
                    "[WedgeAuto] label=%s trigger=automation result=exception error=%s",
                    label,
                    exc,
                )

    def _get_notifier(self) -> NotificationsService:
        """Return the notifications service, ensuring it is initialized."""

        if self.notifications_service is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Notifications service unavailable")
        return self.notifications_service


__all__ = ["AutomationService"]
