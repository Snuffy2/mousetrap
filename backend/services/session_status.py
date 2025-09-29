"""Session status orchestration and helper utilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
import os
import re
from typing import Any

import aiohttp

from backend.app_state import BackendState
from backend.config import load_session, save_session
from backend.ip_lookup import get_asn_and_timezone_from_ip, get_ipinfo_with_fallback, get_public_ip
from backend.mam_api import get_mam_seen_ip_info, get_status
from backend.proxy_config import resolve_proxy_from_session_cfg
from backend.services.notifications_service import NotificationsService
from backend.utils import build_proxy_dict, build_status_message, extract_asn_number


def get_auto_update_val(status: dict[str, Any]) -> str:
    """Return a human-readable representation of the auto-update status."""

    val = status.get("auto_update_seedbox") if isinstance(status, dict) else None
    if val is None or val == "" or val is False:
        return "N/A"
    if isinstance(val, dict):
        msg = val.get("msg")
        reason = val.get("reason")
        error = val.get("error")
        if val.get("success") and msg:
            if reason:
                return f"{msg} ({reason})"
            return msg
        if error:
            if reason:
                return f"{error} ({reason})"
            return error
        return "N/A"
    return str(val)


@dataclass(slots=True)
class SessionStatusService:
    """Encapsulates session status checks, caching, and notifications."""

    state: BackendState
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))
    notifications_service: NotificationsService = field(init=False)

    def __post_init__(self) -> None:
        """Attach a notifications service bound to the backend state."""

        self.notifications_service = NotificationsService(state=self.state)

    async def get_status_response(self, label: str | None, force: bool) -> dict[str, Any]:
        """Return the latest status payload for ``label``.

        When ``force`` is ``True`` a fresh check is executed even if cached data
        exists. The returned payload mirrors the previous ``/api/status``
        response structure.
        """

        detected_ipinfo_data = await get_ipinfo_with_fallback()
        detected_public_ip = detected_ipinfo_data.get("ip")
        detected_public_ip_asn = None
        if detected_public_ip:
            asn_full_pub = detected_ipinfo_data.get("asn")
            match_pub = re.search(r"(AS)?(\d+)", asn_full_pub or "") if asn_full_pub else None
            detected_public_ip_asn = match_pub.group(2) if match_pub else asn_full_pub

        cfg = load_session(label) if label else None
        if cfg is None:
            self.logger.warning("Session '%s' not found or not configured.", label)
            return {
                "configured": False,
                "status_message": "Session not configured. Please save session details to begin.",
                "last_check_time": None,
                "next_check_time": None,
                "details": {},
            }

        proxy_cfg = resolve_proxy_from_session_cfg(cfg)
        proxied_public_ip, proxied_public_ip_asn, proxied_public_ip_as = None, None, None
        proxy_error = None
        if proxy_cfg and proxy_cfg.get("host"):
            try:
                proxied_ipinfo_data = await get_ipinfo_with_fallback(proxy_cfg=proxy_cfg)
                proxied_public_ip = proxied_ipinfo_data.get("ip")
                asn_full_proxied = proxied_ipinfo_data.get("asn")
                asn_str = str(asn_full_proxied) if asn_full_proxied is not None else ""
                match_proxied = re.search(r"(AS)?(\d+)", asn_str) if asn_str else None
                proxied_public_ip_asn = match_proxied.group(2) if match_proxied else asn_str
                proxied_public_ip_as = asn_full_proxied
                if proxied_public_ip and cfg.get("proxied_public_ip") != proxied_public_ip:
                    cfg["proxied_public_ip"] = proxied_public_ip
                    cfg["proxied_public_ip_asn"] = proxied_public_ip_asn
                    save_session(cfg, old_label=label)
            except Exception as exc:  # pragma: no cover - proxy failures rare in tests
                proxy_error = f"Proxy/VPN connection failed: {exc!s}"
                await self.notifications_service.notify_event(
                    event_type="proxy_failure",
                    label=label,
                    status="FAILED",
                    message=proxy_error,
                    details={"proxy": proxy_cfg.get("label", "unknown"), "error": str(exc)},
                )
        elif cfg.get("proxied_public_ip") or cfg.get("proxied_public_ip_asn"):
            cfg["proxied_public_ip"] = None
            cfg["proxied_public_ip_asn"] = None
            save_session(cfg, old_label=label)

        if not label:
            return {
                "configured": False,
                "status_message": "Session label required.",
                "last_check_time": None,
                "next_check_time": None,
                "details": {},
                "detected_public_ip": detected_public_ip,
                "detected_public_ip_asn": detected_public_ip_asn,
            }

        cfg = load_session(label)
        mam_id = cfg.get("mam", {}).get("mam_id", "")
        mam_ip_override = cfg.get("mam_ip", "").strip()
        ip_monitoring_mode = cfg.get("mam", {}).get("ip_monitoring_mode", "auto")

        proxy_cfg = resolve_proxy_from_session_cfg(cfg)
        if not mam_id:
            return {
                "configured": False,
                "status_message": "Session not configured. Please save session details to begin.",
                "last_check_time": None,
                "next_check_time": None,
                "details": {},
                "detected_public_ip": detected_public_ip,
                "detected_public_ip_asn": detected_public_ip_asn,
            }

        ip_to_use: str | None = mam_ip_override or proxied_public_ip or detected_public_ip
        asn_full, _ = await get_asn_and_timezone_from_ip(ip_to_use) if ip_to_use else (None, None)
        match = re.search(r"(AS)?(\d+)", asn_full or "") if asn_full else None
        asn = match.group(2) if match else asn_full
        mam_session_as = asn_full
        mam_seen = await get_mam_seen_ip_info(mam_id, proxy_cfg=proxy_cfg or {})
        mam_seen_asn = str(mam_seen.get("ASN")) if mam_seen.get("ASN") is not None else None
        mam_seen_as = mam_seen.get("AS")
        tz_env = os.environ.get("TZ")
        timezone_used = tz_env if tz_env else "UTC"
        now = datetime.now(UTC)
        cache = self.state.session_status_cache.get_entry(label) if label else {}
        status = cache.get("status", {})
        last_check_time = cache.get("last_check_time")
        auto_update_result = None
        has_cached_status = bool(label) and self.state.session_status_cache.has_status(label)
        detected_public_ip_as = None
        if detected_public_ip:
            asn_full_pub, _ = await get_asn_and_timezone_from_ip(detected_public_ip)
            match_pub = re.search(r"(AS)?(\d+)", asn_full_pub or "") if asn_full_pub else None
            detected_public_ip_asn = match_pub.group(2) if match_pub else asn_full_pub
            detected_public_ip_as = asn_full_pub

        if not force and not has_cached_status:
            last_status = cfg.get("last_status")
            last_check_time = cfg.get("last_check_time")
            if not last_status or not last_check_time:
                return {
                    "configured": False,
                    "status_message": "Session not configured. Please save session details to begin.",
                    "last_check_time": None,
                    "next_check_time": None,
                    "details": {},
                }

        if not force and status:
            check_freq_minutes = cfg.get("check_freq", 15)
            if last_check_time:
                try:
                    last_check_dt = datetime.fromisoformat(last_check_time)
                    next_check_dt = last_check_dt + timedelta(minutes=check_freq_minutes)
                    next_check_time = next_check_dt.isoformat()
                except Exception:  # pragma: no cover - defensive fallback
                    next_check_dt = now + timedelta(minutes=check_freq_minutes)
                    next_check_time = next_check_dt.isoformat()
            else:
                next_check_dt = now + timedelta(minutes=check_freq_minutes)
                next_check_time = next_check_dt.isoformat()

            return {
                "mam_cookie_exists": status.get("mam_cookie_exists"),
                "points": status.get("points"),
                "cheese": status.get("cheese"),
                "wedge_active": status.get("wedge_active"),
                "vip_active": status.get("vip_active"),
                "current_ip": ip_to_use,
                "current_ip_asn": asn,
                "mam_session_as": mam_session_as,
                "mam_seen_asn": mam_seen_asn,
                "mam_seen_as": mam_seen_as,
                "configured_ip": ip_to_use,
                "configured_asn": asn,
                "mam_id": mam_id,
                "check_freq": check_freq_minutes,
                "last_check_time": last_check_time,
                "next_check_time": next_check_time,
                "configured": True,
                "status_message": status.get("status_message", "OK"),
                "auto_update_seedbox": status.get("auto_update_seedbox"),
                "details": status,
                "detected_public_ip": detected_public_ip,
                "detected_public_ip_asn": detected_public_ip_asn,
                "detected_public_ip_as": detected_public_ip_as,
                "proxied_public_ip": proxied_public_ip,
                "proxied_public_ip_asn": proxied_public_ip_asn,
                "proxied_public_ip_as": proxied_public_ip_as,
                "ip_monitoring_mode": ip_monitoring_mode,
            }

        if force or not status:
            cfg = load_session(label)
            proxy_cfg = resolve_proxy_from_session_cfg(cfg)
            self.logger.debug(
                "[SessionCheck][TRIGGER] label=%s source=%s",
                label,
                "forced_api_status" if force else "auto_api_status",
            )
            mam_status = await get_status(mam_id=mam_id, proxy_cfg=proxy_cfg)
            if "proxy_error" not in mam_status and "proxy_error" in locals() and proxy_error:
                mam_status["proxy_error"] = proxy_error
            mam_status["configured_ip"] = ip_to_use
            mam_status["configured_asn"] = asn
            mam_status["mam_seen_asn"] = mam_seen_asn
            mam_status["mam_seen_as"] = mam_seen_as
            if ip_monitoring_mode == "auto":
                (
                    auto_update_triggered,
                    auto_update_result,
                ) = await self.auto_update_seedbox_if_needed(cfg, label, ip_to_use, asn, now)
            else:
                auto_update_triggered, auto_update_result = False, None
                self.logger.debug(
                    "[Status] Skipping auto-update for session '%s' in %s mode",
                    label,
                    ip_monitoring_mode,
                )

            if auto_update_triggered and auto_update_result:
                mam_status["auto_update_seedbox"] = auto_update_result
                if auto_update_result.get("error"):
                    mam_status["status_message"] = auto_update_result.get("error")
                elif auto_update_result.get("success") is True and (
                    auto_update_result.get("msg") or auto_update_result.get("reason")
                ):
                    mam_status["status_message"] = auto_update_result.get(
                        "msg"
                    ) or auto_update_result.get("reason")
                else:
                    mam_status["status_message"] = build_status_message(
                        mam_status, ip_monitoring_mode
                    )
            else:
                mam_status["status_message"] = build_status_message(mam_status, ip_monitoring_mode)

            if label:
                self.state.session_status_cache.set_status(label, mam_status, now)
                has_cached_status = True
            status = mam_status
            last_check_time = now.isoformat()
            cfg = load_session(label)
            await self.check_and_notify_count_increments(cfg, status, label)
            cfg["last_status"] = status
            cfg["last_check_time"] = last_check_time
            save_session(cfg, old_label=label)

        suppress_next_event = False
        if label and self.state.session_status_cache.should_suppress_next_event(label):
            suppress_next_event = True
            self.state.session_status_cache.pop_suppress_next_event(label)

        just_created_session = False
        try:
            just_created_session = not bool(cfg.get("last_status")) and not bool(
                cfg.get("last_check_time")
            )
        except Exception:  # pragma: no cover - defensive
            just_created_session = False

        if (
            (force or not has_cached_status)
            and not just_created_session
            and not suppress_next_event
        ):
            safe_status = status if isinstance(status, dict) else {}
            prev_ip = cfg.get("last_seedbox_ip")
            prev_asn = cfg.get("last_seedbox_asn")
            proxied_ip = cfg.get("proxied_public_ip")
            mam_ip_override = cfg.get("mam_ip", "").strip()
            detected_ip = detected_public_ip
            curr_ip = mam_ip_override or proxied_ip or detected_ip
            asn_full_current, _ = (
                await get_asn_and_timezone_from_ip(curr_ip) if curr_ip else (None, None)
            )
            match_current = (
                re.search(r"(AS)?(\d+)", asn_full_current or "") if asn_full_current else None
            )
            curr_asn = match_current.group(2) if match_current else asn_full_current

            if curr_asn is None or curr_asn == "Unknown ASN":
                curr_asn = prev_asn

            state_change = {
                "previous_ip": prev_ip,
                "current_ip": curr_ip,
                "previous_asn": prev_asn,
                "current_asn": curr_asn,
                "timestamp": now.isoformat(),
                "timezone": timezone_used,
                "auto_update_seedbox": get_auto_update_val(safe_status),
            }

            if prev_ip != curr_ip or prev_asn != curr_asn:
                message = f"Session '{label}' updated."
                await self.notifications_service.notify_event(
                    event_type="session_status_change",
                    label=label,
                    status="UPDATED",
                    message=message,
                    details=state_change,
                )

        check_freq_minutes = cfg.get("check_freq", 15)
        if last_check_time:
            try:
                last_check_dt = datetime.fromisoformat(last_check_time)
                next_check_dt = last_check_dt + timedelta(minutes=check_freq_minutes)
                next_check_time = next_check_dt.isoformat()
            except Exception:  # pragma: no cover - fallback
                next_check_dt = now + timedelta(minutes=check_freq_minutes)
                next_check_time = next_check_dt.isoformat()
        else:
            next_check_dt = now + timedelta(minutes=check_freq_minutes)
            next_check_time = next_check_dt.isoformat()

        status_message = status.get("status_message", "OK") if isinstance(status, dict) else "OK"

        return {
            "mam_cookie_exists": status.get("mam_cookie_exists")
            if isinstance(status, dict)
            else None,
            "points": status.get("points") if isinstance(status, dict) else None,
            "cheese": status.get("cheese") if isinstance(status, dict) else None,
            "wedge_active": status.get("wedge_active") if isinstance(status, dict) else None,
            "vip_active": status.get("vip_active") if isinstance(status, dict) else None,
            "current_ip": ip_to_use,
            "current_ip_asn": asn,
            "mam_session_as": mam_session_as,
            "mam_seen_asn": mam_seen_asn,
            "mam_seen_as": mam_seen_as,
            "configured_ip": ip_to_use,
            "configured_asn": asn,
            "mam_id": mam_id,
            "check_freq": check_freq_minutes,
            "last_check_time": last_check_time,
            "next_check_time": next_check_time,
            "configured": True,
            "status_message": status_message,
            "auto_update_seedbox": status.get("auto_update_seedbox")
            if isinstance(status, dict)
            else None,
            "details": status,
            "detected_public_ip": detected_public_ip,
            "detected_public_ip_asn": detected_public_ip_asn,
            "detected_public_ip_as": detected_public_ip_as,
            "proxied_public_ip": proxied_public_ip,
            "proxied_public_ip_asn": proxied_public_ip_asn,
            "proxied_public_ip_as": proxied_public_ip_as,
            "ip_monitoring_mode": ip_monitoring_mode,
        }

    async def check_and_notify_count_increments(
        self, cfg: dict[str, Any], new_status: dict[str, Any], label: str
    ) -> None:
        """Emit notifications when tracked status counters increase."""
        old_status = cfg.get("last_status", {})
        if not isinstance(old_status, dict) or not isinstance(new_status, dict):
            return

        mam_id = cfg.get("mam", {}).get("mam_id", "")
        deduper = self.state.notification_deduplicator
        old_raw = old_status.get("raw", {})
        new_raw = new_status.get("raw", {})

        old_inact_hnr_raw = (
            old_raw.get("inactHnr", {}).get("count", 0)
            if isinstance(old_raw.get("inactHnr"), dict)
            else 0
        )
        new_inact_hnr_raw = (
            new_raw.get("inactHnr", {}).get("count", 0)
            if isinstance(new_raw.get("inactHnr"), dict)
            else 0
        )

        old_inact_hnr = (
            int(old_inact_hnr_raw)
            if isinstance(old_inact_hnr_raw, (int, str)) and str(old_inact_hnr_raw).isdigit()
            else 0
        )
        new_inact_hnr = (
            int(new_inact_hnr_raw)
            if isinstance(new_inact_hnr_raw, (int, str)) and str(new_inact_hnr_raw).isdigit()
            else 0
        )

        if new_inact_hnr > old_inact_hnr:
            increment = new_inact_hnr - old_inact_hnr
            if deduper.should_send(mam_id, "inactive_hit_and_run", old_inact_hnr, new_inact_hnr):
                await self.notifications_service.notify_event(
                    event_type="inactive_hit_and_run",
                    label=label,
                    status="INCREMENT",
                    message=(
                        f"Inactive Hit & Run count increased by {increment} "
                        f"(from {old_inact_hnr} to {new_inact_hnr})"
                    ),
                    details={
                        "old_count": old_inact_hnr,
                        "new_count": new_inact_hnr,
                        "increment": increment,
                        "mam_id": mam_id,
                    },
                )

        old_inact_unsat_raw = (
            old_raw.get("inactUnsat", {}).get("count", 0)
            if isinstance(old_raw.get("inactUnsat"), dict)
            else 0
        )
        new_inact_unsat_raw = (
            new_raw.get("inactUnsat", {}).get("count", 0)
            if isinstance(new_raw.get("inactUnsat"), dict)
            else 0
        )

        old_inact_unsat = (
            int(old_inact_unsat_raw)
            if isinstance(old_inact_unsat_raw, (int, str)) and str(old_inact_unsat_raw).isdigit()
            else 0
        )
        new_inact_unsat = (
            int(new_inact_unsat_raw)
            if isinstance(new_inact_unsat_raw, (int, str)) and str(new_inact_unsat_raw).isdigit()
            else 0
        )

        if new_inact_unsat > old_inact_unsat:
            increment = new_inact_unsat - old_inact_unsat
            if deduper.should_send(
                mam_id, "inactive_unsatisfied", old_inact_unsat, new_inact_unsat
            ):
                await self.notifications_service.notify_event(
                    event_type="inactive_unsatisfied",
                    label=label,
                    status="INCREMENT",
                    message=(
                        "Inactive Unsatisfied (Pre-H&R) count increased by "
                        f"{increment} (from {old_inact_unsat} to {new_inact_unsat})"
                    ),
                    details={
                        "old_count": old_inact_unsat,
                        "new_count": new_inact_unsat,
                        "increment": increment,
                        "mam_id": mam_id,
                    },
                )

    async def auto_update_seedbox_if_needed(
        self, cfg: dict[str, Any], label: str, ip_to_use: str | None, asn: str | None, now: datetime
    ) -> tuple[bool, dict[str, Any] | None]:
        """Execute the dynamic seedbox update flow when configuration drift is detected."""
        if not ip_to_use:
            return False, None

        session_type = cfg.get("mam", {}).get("session_type", "").lower()
        last_seedbox_ip: str | None = cfg.get("last_seedbox_ip")
        last_seedbox_asn: str | None = cfg.get("last_seedbox_asn")
        last_seedbox_update = cfg.get("last_seedbox_update")
        mam_id: str = cfg.get("mam", {}).get("mam_id", "")

        proxy_cfg = resolve_proxy_from_session_cfg(cfg)
        proxies = build_proxy_dict(proxy_cfg) if proxy_cfg else None

        if session_type == "asn locked":
            proxied_ip = cfg.get("proxied_public_ip")
            proxy_cfg = resolve_proxy_from_session_cfg(cfg)
            asn_to_check, _ = await get_asn_and_timezone_from_ip(
                proxied_ip or ip_to_use, proxy_cfg if proxied_ip else None
            )

            if asn_to_check is None or asn_to_check == "Unknown ASN":
                self.logger.info(
                    "[AutoUpdate] label=%s ASN lookup failed or unavailable (likely fallback provider).",
                    label,
                )
            else:
                norm_last = extract_asn_number(last_seedbox_asn) if last_seedbox_asn else None
                norm_check = (
                    extract_asn_number(asn_to_check) if "asn_to_check" in locals() else None
                )
                if norm_check is not None:
                    cfg["last_seedbox_asn"] = norm_check
                    save_session(cfg, old_label=label)
                if norm_last != norm_check:
                    asn_reason = f"ASN changed: {norm_last} -> {norm_check}"
                    self.logger.info(
                        "[AutoUpdate] label=%s ASN changed, no seedbox API call. reason=%s",
                        label,
                        asn_reason,
                    )
                    await self.notifications_service.notify_event(
                        event_type="asn_changed",
                        label=label,
                        status="CHANGED",
                        message=asn_reason,
                        details={"old_asn": norm_last, "new_asn": norm_check},
                    )
                    return False, {
                        "success": True,
                        "msg": "ASN changed, no seedbox update performed.",
                        "reason": asn_reason,
                    }
                self.logger.info(
                    "[AutoUpdate] label=%s ASN check: %s -> %s | No change needed",
                    label,
                    norm_last,
                    norm_check,
                )

        proxied_ip = cfg.get("proxied_public_ip")
        if proxied_ip:
            ip_to_check = proxied_ip
        else:
            detected_ip = await get_public_ip()
            ip_to_check = detected_ip

        if ip_to_check is None:
            self.logger.warning(
                "[AutoUpdate] label=%s Could not detect valid public IP. Skipping config update.",
                label,
            )
            return False, {"success": False, "msg": "IP lookup failed. No update performed."}

        update_needed = False
        reason: str | None = None
        if last_seedbox_ip is None or ip_to_check != last_seedbox_ip:
            update_needed = True
            reason = f"IP changed: {last_seedbox_ip} -> {ip_to_check or 'N/A'}"
            self.logger.info(
                "[AutoUpdate] label=%s IP changed: %s -> %s",
                label,
                last_seedbox_ip,
                ip_to_check or "N/A",
            )
        else:
            self.logger.info(
                "[AutoUpdate] label=%s IP check: %s -> %s | No change needed",
                label,
                last_seedbox_ip,
                ip_to_check,
            )

        if not update_needed:
            return False, None

        self.logger.info(
            "[AutoUpdate] label=%s update_needed=True asn=%s reason=%s",
            label,
            asn,
            reason,
        )

        if not mam_id:
            self.logger.warning(
                "[AutoUpdate] label=%s update_needed=True but mam_id is missing. Skipping seedbox API call.",
                label,
            )
            return False, {"success": False, "error": "mam_id missing", "reason": reason}

        try:
            self.logger.debug(
                "[AutoUpdate][TRACE] label=%s About to call seedbox API (using proxy)",
                label,
            )
            cookies = {"mam_id": mam_id}
            proxy_url = None
            if proxies and isinstance(proxies, dict):
                proxy_url = proxies.get("https") or proxies.get("http")

            timeout = aiohttp.ClientTimeout(total=10)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(
                    "https://t.myanonamouse.net/json/dynamicSeedbox.php",
                    cookies=cookies,
                    proxy=proxy_url,
                ) as resp,
            ):
                self.logger.debug(
                    "[AutoUpdate][TRACE] label=%s Seedbox API call complete. Status=%s",
                    label,
                    resp.status,
                )
                try:
                    result = await resp.json()
                    self.logger.debug(
                        "[AutoUpdate][TRACE] label=%s Seedbox API response JSON received",
                        label,
                    )
                except Exception as exc_json:  # pragma: no cover - defensive
                    self.logger.warning(
                        "[AutoUpdate][TRACE] label=%s Non-JSON response from seedbox API (error: %s)",
                        label,
                        exc_json,
                    )
                    text = await resp.text()
                    result = {"Success": False, "msg": f"Non-JSON response: {text}"}

                if resp.status == 200 and result.get("Success"):
                    proxied_ip = cfg.get("proxied_public_ip")
                    if proxied_ip:
                        new_ip = proxied_ip
                    else:
                        new_ip = await get_public_ip()
                    cfg["last_seedbox_ip"] = new_ip
                    cfg["mam_ip"] = new_ip
                    cfg["last_seedbox_update"] = now.isoformat()
                    cfg["last_seedbox_asn"] = asn
                    try:
                        save_session(cfg, old_label=label)
                    except Exception as exc_save:  # pragma: no cover - persistence errors rare
                        self.logger.error(
                            "[AutoUpdate][ERROR] label=%s save_session failed: %s",
                            label,
                            exc_save,
                        )
                    self.logger.info(
                        "[AutoUpdate] label=%s result=success reason=%s",
                        label,
                        reason,
                    )
                    api_msg = result.get("msg", "").strip()
                    if not api_msg or api_msg.lower() == "completed":
                        api_msg = "IP Changed. Seedbox IP updated."
                    await self.notifications_service.notify_event(
                        event_type="seedbox_update_success",
                        label=label,
                        status="SUCCESS",
                        message=api_msg,
                        details={"reason": reason, "ip": new_ip, "asn": asn},
                    )
                    return True, {"success": True, "msg": api_msg, "reason": reason}

                if resp.status == 200 and result.get("msg") == "No change":
                    proxied_ip = cfg.get("proxied_public_ip")
                    if proxied_ip:
                        new_ip = proxied_ip
                    else:
                        new_ip = await get_public_ip()
                    cfg["last_seedbox_ip"] = new_ip
                    cfg["mam_ip"] = new_ip
                    cfg["last_seedbox_update"] = now.isoformat()
                    cfg["last_seedbox_asn"] = asn
                    try:
                        save_session(cfg, old_label=label)
                    except Exception as exc_save:  # pragma: no cover - persistence errors rare
                        self.logger.error(
                            "[AutoUpdate][ERROR] label=%s save_session failed: %s",
                            label,
                            exc_save,
                        )
                    self.logger.info(
                        "[AutoUpdate] label=%s result=no_change reason=%s",
                        label,
                        reason,
                    )
                    return True, {
                        "success": True,
                        "msg": "No change: IP/ASN already set.",
                        "reason": reason,
                    }

                if resp.status == 429 or (
                    isinstance(result.get("msg"), str) and "too recent" in result.get("msg", "")
                ):
                    rate_limit_minutes = 60
                    if last_seedbox_update:
                        last_update_dt = datetime.fromisoformat(last_seedbox_update)
                        elapsed = (now - last_update_dt).total_seconds() / 60
                        if elapsed < 0:
                            rate_limit_minutes = 0
                        elif elapsed < 60:
                            rate_limit_minutes = int(60 - elapsed)
                        else:
                            rate_limit_minutes = 0
                    await self.notifications_service.notify_event(
                        event_type="seedbox_update_rate_limited",
                        label=label,
                        status="RATE_LIMITED",
                        message="Rate limit: last change too recent.",
                        details={"reason": reason, "rate_limit_minutes": rate_limit_minutes},
                    )
                    return True, {
                        "success": False,
                        "error": (
                            "Rate limit: last change too recent. Try again in "
                            f"{rate_limit_minutes} minutes."
                        ),
                        "reason": reason,
                        "rate_limit_minutes": rate_limit_minutes,
                    }

                self.logger.info(
                    "[AutoUpdate] label=%s result=error reason=%s",
                    label,
                    reason,
                )
                await self.notifications_service.notify_event(
                    event_type="seedbox_update_failure",
                    label=label,
                    status="FAILED",
                    message=result.get("msg", "Unknown error"),
                    details={"reason": reason},
                )
                return True, {
                    "success": False,
                    "error": result.get("msg", "Unknown error"),
                    "reason": reason,
                }
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.warning(
                "[AutoUpdate] label=%s result=exception reason=%s error=%s",
                label,
                reason,
                exc,
            )
            await self.notifications_service.notify_event(
                event_type="seedbox_update_exception",
                label=label,
                status="EXCEPTION",
                message=str(exc),
                details={"reason": reason},
            )
            return True, {"success": False, "error": str(exc), "reason": reason}

        return False, None
