"""Service layer helpers for dispatching user notifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
import logging
import os
from pathlib import Path
import smtplib
from typing import TYPE_CHECKING, Any

import aiohttp
import yaml

if TYPE_CHECKING:
    from backend.app_state import BackendState

_DEFAULT_NOTIFY_PATH = os.environ.get("NOTIFY_CONFIG_PATH", "/config/notify.yaml")


@dataclass(slots=True)
class NotificationsService:
    """Coordinate webhook, SMTP, and Apprise notifications."""

    state: BackendState | None = None
    config_path: Path = field(default_factory=lambda: Path(_DEFAULT_NOTIFY_PATH))
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))

    def load_notify_config(self) -> dict[str, Any]:
        """Return the current notification configuration as a dictionary."""

        path = self.config_path
        if not path.exists():
            return {}
        try:
            with path.open(encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError) as error:
            self.logger.error("[Notify] Failed to load config: %s", error)
            return {}
        return data if isinstance(data, dict) else {}

    async def send_webhook_notification(
        self, url: str, payload: dict[str, Any], *, discord: bool = False
    ) -> bool:
        """Send a JSON payload to ``url``; optionally format for Discord."""

        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                request_payload = (
                    {"content": payload.get("message") or str(payload)} if discord else payload
                )
                async with session.post(url, json=request_payload) as response:
                    if response.status >= 400:
                        body = await response.text()
                        message = body[:200]
                        raise aiohttp.ClientResponseError(
                            request_info=response.request_info,
                            history=tuple(response.history),
                            status=response.status,
                            headers=response.headers,
                            message=message,
                        )
        except (aiohttp.ClientError, TimeoutError) as error:
            self.logger.error("[Notify] Webhook failed: %s", error)
            return False
        return True

    def send_smtp_notification(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        to_email: str,
        subject: str,
        body: str,
        *,
        use_tls: bool = True,
    ) -> bool:
        """Send a plain-text email using the provided SMTP credentials."""

        message = EmailMessage()
        message["From"] = username
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(body, subtype="plain")

        try:
            with smtplib.SMTP(host=smtp_host, port=smtp_port, timeout=10) as server:
                if use_tls:
                    server.starttls()
                server.login(username, password)
                server.send_message(message)
        except (smtplib.SMTPException, OSError) as error:
            self.logger.error("[Notify] SMTP failed: %s", error)
            return False
        return True

    async def send_apprise_notification(
        self,
        apprise_url: str,
        notify_url_string: str,
        payload: dict[str, Any],
        *,
        include_prefix: bool = False,
    ) -> bool:
        """Send a notification via an Apprise bridge endpoint."""

        apprise_base = apprise_url.rstrip("/")
        post_url = (
            apprise_base if apprise_base.lower().endswith("/notify") else f"{apprise_base}/notify"
        )
        event_type = payload.get("event_type", "Notification").replace("_", " ").title()
        status = payload.get("status")
        status_suffix = f": {status.title()}" if isinstance(status, str) and status else ""
        title_prefix = "MouseTrap: " if include_prefix else ""
        title = f"{title_prefix}{event_type}{status_suffix}"
        message = payload.get("message", "")
        status_value = payload.get("status")
        notif_type = (
            "success"
            if status_value == "SUCCESS"
            else "failure"
            if status_value == "FAILED"
            else "info"
        )

        timeout = aiohttp.ClientTimeout(total=5)
        data = {
            "urls": notify_url_string,
            "body": message,
            "title": title,
            "type": notif_type,
        }

        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(post_url, data=data) as response,
            ):
                if response.status < 200 or response.status >= 300:
                    text = await response.text()
                    self.logger.error(
                        "[Notify] Apprise failed. Response: %s - %s", response.status, text
                    )
                    return False
                try:
                    payload_json = await response.json()
                except aiohttp.ContentTypeError:
                    payload_json = None
                if isinstance(payload_json, dict) and payload_json.get("success") is False:
                    self.logger.error("[Notify] Apprise reported success=false: %s", payload_json)
                    return False
        except (aiohttp.ClientError, TimeoutError) as error:
            self.logger.error("[Notify] Apprise failed: %s", error)
            return False
        return True

    async def notify_event(
        self,
        *,
        event_type: str,
        label: str | None = None,
        status: str | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Dispatch notifications for ``event_type`` using configured channels."""

        config = self.load_notify_config()
        event_rules = config.get("event_rules", {})
        rule = event_rules.get(event_type, {"email": False, "webhook": False, "apprise": False})
        if not any(rule.get(key) for key in ("email", "webhook", "apprise")):
            return

        mousetrap_prefix = "MouseTrap: "
        session_part = f"Session: {label}, " if label else ""
        details_payload = details or {}
        full_message = (
            f"{mousetrap_prefix}{session_part}{message}"
            if message
            else f"{mousetrap_prefix}{session_part}".rstrip(", ")
        )
        payload = {
            "event_type": event_type,
            "label": label,
            "status": status,
            "message": full_message,
            "details": details_payload,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        if rule.get("webhook"):
            webhook_url = config.get("webhook_url")
            discord_webhook = config.get("discord_webhook", False)
            if webhook_url:
                await self.send_webhook_notification(webhook_url, payload, discord=discord_webhook)

        if rule.get("email"):
            smtp_cfg = config.get("smtp", {})
            required = ["host", "port", "username", "password", "to_email"]
            if all(key in smtp_cfg for key in required):
                subject = f"[MouseTrap] {event_type} - {status or ''}".strip()
                body = (
                    f"Event: {event_type}\n"
                    f"Label: {label}\n"
                    f"Status: {status}\n"
                    f"Message: {full_message}\n"
                    f"Details: {details_payload}"
                )
                self.send_smtp_notification(
                    smtp_cfg["host"],
                    smtp_cfg["port"],
                    smtp_cfg["username"],
                    smtp_cfg["password"],
                    smtp_cfg["to_email"],
                    subject,
                    body,
                    use_tls=smtp_cfg.get("use_tls", True),
                )

        if rule.get("apprise"):
            apprise_cfg = config.get("apprise", {})
            apprise_url = apprise_cfg.get("url")
            notify_url_string = apprise_cfg.get("notify_url_string")
            include_prefix = apprise_cfg.get("include_prefix", False)
            if apprise_url and notify_url_string:
                await self.send_apprise_notification(
                    apprise_url,
                    notify_url_string,
                    payload,
                    include_prefix=include_prefix,
                )


async def notify_event(
    event_type: str,
    *,
    label: str | None = None,
    status: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
    state: BackendState | None = None,
) -> None:
    """Convenience wrapper that proxies to :class:`NotificationsService`."""

    service = NotificationsService(state=state)
    await service.notify_event(
        event_type=event_type,
        label=label,
        status=status,
        message=message,
        details=details,
    )


def load_notify_config(state: BackendState | None = None) -> dict[str, Any]:
    """Load notification configuration via a temporary service instance."""

    service = NotificationsService(state=state)
    return service.load_notify_config()


def send_smtp_notification(
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    to_email: str,
    subject: str,
    body: str,
    *,
    use_tls: bool = True,
) -> bool:
    """Proxy helper for sending SMTP notifications without instantiating the service."""

    service = NotificationsService()
    return service.send_smtp_notification(
        smtp_host,
        smtp_port,
        username,
        password,
        to_email,
        subject,
        body,
        use_tls=use_tls,
    )


async def send_webhook_notification(
    url: str, payload: dict[str, Any], *, discord: bool = False
) -> bool:
    """Proxy helper that defers to :class:`NotificationsService`."""

    service = NotificationsService()
    return await service.send_webhook_notification(url, payload, discord=discord)


async def send_apprise_notification(
    apprise_url: str,
    notify_url_string: str,
    payload: dict[str, Any],
    *,
    include_prefix: bool = False,
) -> bool:
    """Proxy helper that defers to the service implementation."""

    service = NotificationsService()
    return await service.send_apprise_notification(
        apprise_url,
        notify_url_string,
        payload,
        include_prefix=include_prefix,
    )


__all__ = [
    "NotificationsService",
    "notify_event",
    "load_notify_config",
    "send_webhook_notification",
    "send_smtp_notification",
    "send_apprise_notification",
]
