from __future__ import annotations

import asyncio
import imaplib
import smtplib
from contextlib import contextmanager
from email.message import EmailMessage
from typing import Iterator

from app.core.config import settings


def email_configuration() -> dict:
    smtp_missing = [
        name
        for name, value in {
            "SMTP_HOST": settings.SMTP_HOST,
            "SMTP_USERNAME": settings.SMTP_USERNAME,
            "SMTP_PASSWORD": settings.SMTP_PASSWORD,
            "SMTP_TO": settings.SMTP_TO,
        }.items()
        if not value
    ]
    imap_missing = [
        name
        for name, value in {
            "IMAP_HOST": settings.IMAP_HOST,
            "IMAP_USERNAME": settings.IMAP_USERNAME,
            "IMAP_PASSWORD": settings.IMAP_PASSWORD,
        }.items()
        if not value
    ]
    sender = settings.SMTP_FROM or settings.SMTP_USERNAME
    warnings = []
    if sender and settings.SMTP_TO and sender.casefold() == settings.SMTP_TO.casefold():
        warnings.append("发件邮箱与收件邮箱相同，建议用独立 Agent 邮箱向你的日常邮箱发送")
    if settings.SMTP_USE_SSL and settings.SMTP_USE_TLS:
        warnings.append("SMTP_USE_SSL 与 SMTP_USE_TLS 不应同时开启")
    if sender and settings.IMAP_USERNAME and sender.casefold() != settings.IMAP_USERNAME.casefold():
        warnings.append("回复地址与 IMAP 账号不同，请确认回复邮件会转投到该 IMAP 邮箱")
    return {
        "smtp_configured": not smtp_missing,
        "imap_configured": settings.ENABLE_EMAIL_REPLY_POLLING and not imap_missing,
        "smtp_missing": smtp_missing,
        "imap_missing": (["ENABLE_EMAIL_REPLY_POLLING"] if not settings.ENABLE_EMAIL_REPLY_POLLING else []) + imap_missing,
        "smtp_host": settings.SMTP_HOST,
        "smtp_port": settings.SMTP_PORT,
        "smtp_use_ssl": settings.SMTP_USE_SSL,
        "smtp_use_tls": settings.SMTP_USE_TLS,
        "smtp_username": _mask(settings.SMTP_USERNAME),
        "smtp_to": _mask(settings.SMTP_TO),
        "imap_host": settings.IMAP_HOST,
        "imap_port": settings.IMAP_PORT,
        "imap_username": _mask(settings.IMAP_USERNAME),
        "imap_folder": settings.IMAP_FOLDER,
        "reply_polling_enabled": settings.ENABLE_EMAIL_REPLY_POLLING,
        "warnings": warnings,
    }


async def test_smtp(*, send_message: bool = False) -> dict:
    configuration = email_configuration()
    if not configuration["smtp_configured"]:
        raise ValueError(f"Missing SMTP settings: {', '.join(configuration['smtp_missing'])}")
    await asyncio.to_thread(_test_smtp_sync, send_message)
    return {"ok": True, "channel": "smtp", "message_sent": send_message, "recipient": configuration["smtp_to"]}


async def test_imap() -> dict:
    configuration = email_configuration()
    if not configuration["imap_configured"]:
        raise ValueError(f"Missing IMAP settings: {', '.join(configuration['imap_missing'])}")
    mailbox_count = await asyncio.to_thread(_test_imap_sync)
    return {"ok": True, "channel": "imap", "folder": settings.IMAP_FOLDER, "message_count": mailbox_count}


def _test_smtp_sync(send_message: bool) -> None:
    with smtp_connection() as server:
        server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        if send_message:
            message = EmailMessage()
            message["Subject"] = "[Learning Agent] SMTP 配置测试"
            message["From"] = settings.SMTP_FROM or settings.SMTP_USERNAME
            message["To"] = settings.SMTP_TO
            message.set_content("Learning Agent 已成功连接 SMTP 并发送这封测试邮件。")
            server.send_message(message)


@contextmanager
def smtp_connection() -> Iterator[smtplib.SMTP]:
    client_class = smtplib.SMTP_SSL if settings.SMTP_USE_SSL else smtplib.SMTP
    with client_class(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
        if settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL:
            server.starttls()
        yield server


def _test_imap_sync() -> int:
    client = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT, timeout=20)
    try:
        client.login(settings.IMAP_USERNAME, settings.IMAP_PASSWORD)
        status, data = client.select(settings.IMAP_FOLDER, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Unable to select IMAP folder: {settings.IMAP_FOLDER}")
        return int(data[0]) if data and data[0] else 0
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _mask(value: str) -> str:
    if not value:
        return ""
    if "@" in value:
        name, domain = value.split("@", 1)
        return f"{name[:2]}***@{domain}"
    return f"{value[:2]}***"
