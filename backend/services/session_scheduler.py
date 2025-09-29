"""Scheduler orchestration for session status checks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import re

from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

from backend.app_state import BackendState
from backend.config import list_sessions, load_session, save_session
from backend.event_log import append_ui_event_log
from backend.ip_lookup import get_asn_and_timezone_from_ip, get_ipinfo_with_fallback
from backend.mam_api import get_proxied_public_ip, get_status
from backend.proxy_config import resolve_proxy_from_session_cfg
from backend.services.session_status import SessionStatusService, get_auto_update_val
from backend.utils import build_status_message

_ASN_PATTERN = re.compile(r"(AS)?(\d+)")


@dataclass(slots=True)
class SessionSchedulerService:
    """Manage background session-status checks and scheduler registration."""

    state: BackendState
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))
    _check_delay_seconds: float = 2.0

    async def run_session_check(self, label: str, *, trigger_source: str = "scheduled") -> None:
        """Execute a single session status check and persist results."""
        service = SessionStatusService(state=self.state, logger=self.logger)

        try:
            self.logger.info("[SessionCheck] label=%s source=%s", label, trigger_source)
            cfg = load_session(label)
            mam_id = cfg.get("mam", {}).get("mam_id", "")
            mam_ip_override = cfg.get("mam_ip", "").strip()
            proxy_cfg = resolve_proxy_from_session_cfg(cfg)

            detected_ipinfo_data = await get_ipinfo_with_fallback()
            detected_public_ip = detected_ipinfo_data.get("ip")

            if proxy_cfg and proxy_cfg.get("host"):
                proxied_ip = await get_proxied_public_ip(proxy_cfg)
                if proxied_ip:
                    cfg["proxied_public_ip"] = proxied_ip
                    save_session(cfg, old_label=label)
            else:
                proxied_ip = cfg.get("proxied_public_ip")

            ip_to_use: str | None = mam_ip_override or proxied_ip or detected_public_ip
            asn: str | None = None
            if ip_to_use:
                proxy_for_lookup = (
                    proxy_cfg
                    if proxy_cfg
                    and proxy_cfg.get("host")
                    and ip_to_use == cfg.get("proxied_public_ip")
                    else None
                )
                asn_full, _ = await get_asn_and_timezone_from_ip(ip_to_use, proxy_for_lookup)
                match = _ASN_PATTERN.search(asn_full or "")
                asn = match.group(2) if match else asn_full

            now = datetime.now(UTC)
            if mam_id:
                proxy_cfg = resolve_proxy_from_session_cfg(cfg)
                prev_ip = cfg.get("last_seedbox_ip")
                prev_asn = cfg.get("last_seedbox_asn")
                proxied_ip = cfg.get("proxied_public_ip")
                new_ip = proxied_ip or detected_public_ip
                asn_full, _ = await get_asn_and_timezone_from_ip(new_ip) if new_ip else (None, None)
                match = _ASN_PATTERN.search(asn_full or "") if asn_full else None
                new_asn = match.group(2) if match else asn_full

                status = await get_status(mam_id=mam_id, proxy_cfg=proxy_cfg)
                self.state.session_status_cache.set_status(label, status, now)
                cfg["last_check_time"] = now.isoformat()

                await service.check_and_notify_count_increments(cfg, status, label)

                _, auto_result = await service.auto_update_seedbox_if_needed(
                    cfg, label, ip_to_use, asn, now
                )
                if auto_result is not None:
                    status["auto_update_seedbox"] = auto_result
                    message_key = "msg" if auto_result.get("success") else "error"
                    self.logger.info(
                        "[AutoUpdate] label=%s update result: %s reason=%s",
                        label,
                        auto_result.get(message_key, "N/A"),
                        auto_result.get("reason"),
                    )
                else:
                    status["auto_update_seedbox"] = "N/A"

                status["status_message"] = build_status_message(status)
                cfg["last_status"] = status
                save_session(cfg, old_label=label)

                auto_update_val = get_auto_update_val(status)
                rate_limit_result = status.get("auto_update_seedbox")
                is_rate_limited = False
                message_override: str | None = None
                if isinstance(rate_limit_result, dict):
                    err = (rate_limit_result.get("error") or "").lower()
                    if "rate limit" in err or "try again in" in err:
                        is_rate_limited = True
                        message_override = rate_limit_result.get("error") or (
                            "Rate limited, waiting to update IP/ASN in config."
                        )

                event_details = {
                    "ip_compare": f"{prev_ip} -> {new_ip}",
                    "asn_compare": f"{prev_asn} -> {new_asn}",
                    "auto_update": auto_update_val,
                }

                if is_rate_limited:
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "scheduled",
                            "details": event_details,
                            "status_message": message_override
                            or "Rate limited, waiting to update IP/ASN in config.",
                        }
                    )
                    self.logger.info("[SessionCheck][INFO] label=%s %s", label, message_override)
                elif prev_ip is None or prev_asn is None or new_ip is None or new_asn is None:
                    warn_msg = (
                        "Unable to determine current or new IP/ASN—check connectivity or configuration."
                        " No update performed."
                    )
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "scheduled",
                            "details": event_details,
                            "status_message": warn_msg,
                        }
                    )
                    self.logger.warning("[SessionCheck][WARNING] label=%s %s", label, warn_msg)
                else:
                    append_ui_event_log(
                        {
                            "timestamp": now.isoformat(),
                            "label": label,
                            "event_type": "scheduled",
                            "details": event_details,
                            "status_message": status.get(
                                "status_message", status.get("message", "OK")
                            ),
                        }
                    )
        except Exception as exc:  # pragma: no cover - defensive logging
            self.logger.error("[APScheduler] Error in job for '%s': %s", label, exc)

    def run_session_check_sync(self, label: str) -> None:
        """Run :meth:`run_session_check` in a fresh event loop (scheduler hook)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.run_session_check(label))
        finally:
            loop.close()

    def register_session_job(self, label: str) -> None:
        """Register or refresh the scheduler job for ``label``."""
        cfg = load_session(label)
        check_freq = cfg.get("check_freq")
        mam_id = cfg.get("mam", {}).get("mam_id", "")

        if not check_freq or not isinstance(check_freq, int) or check_freq < 1 or not mam_id:
            self.logger.info(
                "[APScheduler] Skipping job registration for session '%s' (missing or invalid input)",
                label,
            )
            return

        job_id = f"session_check_{label}"
        if self.state.scheduler.get_job(job_id):
            self.state.scheduler.remove_job(job_id)

        self.state.scheduler.add_job(
            self.run_session_check_sync,
            trigger=IntervalTrigger(minutes=check_freq),
            args=[label],
            id=job_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.logger.info(
            "[APScheduler] Registered job for session '%s' every %s min",
            label,
            check_freq,
        )

    def register_all_session_jobs(self) -> None:
        """Register scheduler jobs for every configured session."""
        for label in list_sessions():
            self.register_session_job(label)

    def reset_all_last_check_times(self) -> None:
        """Sync ``last_check_time`` across sessions during application startup."""
        now_iso = datetime.now(UTC).isoformat()
        for label in list_sessions():
            try:
                cfg = load_session(label)
                cfg["last_check_time"] = now_iso
                save_session(cfg, old_label=label)
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.warning(
                    "[Startup] Failed to reset last_check_time for session '%s': %s", label, exc
                )

    async def run_initial_session_checks(self) -> None:
        """Kick off initial checks for all sessions with throttling."""
        session_labels = list_sessions()
        for index, label in enumerate(session_labels):
            try:
                if index > 0:
                    await asyncio.sleep(self._check_delay_seconds)
                self.logger.info("[Startup] Running initial session check for '%s'", label)
                await self.run_session_check(label, trigger_source="startup")
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.warning(
                    "[Startup] Initial session check failed for '%s': %s", label, exc
                )
