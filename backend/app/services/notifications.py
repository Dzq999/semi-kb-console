from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EncryptedCredential, NotificationRecord, ReportSetting
from ..security import decrypt_secret


class NotificationError(RuntimeError):
    pass


WECOM_MARKDOWN_V2_LIMIT = 4096


def split_markdown_v2(content: str, max_bytes: int = WECOM_MARKDOWN_V2_LIMIT) -> list[str]:
    """Split UTF-8 Markdown into API-safe chunks without cutting a codepoint."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if not content:
        return [""]
    chunks: list[str] = []
    current = ""

    def append_piece(piece: str) -> None:
        nonlocal current
        if not piece:
            return
        if len((current + piece).encode("utf-8")) <= max_bytes:
            current += piece
            return
        if current:
            chunks.append(current)
            current = ""
        remainder = piece
        while len(remainder.encode("utf-8")) > max_bytes:
            part = remainder.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            if not part:
                raise ValueError("max_bytes is smaller than one UTF-8 codepoint")
            chunks.append(part)
            remainder = remainder[len(part):]
        current = remainder

    for line in content.splitlines(keepends=True):
        append_piece(line)
    if current:
        chunks.append(current)
    return chunks or [content]


def _secret(db: Session, user_id: int, kind: str) -> str:
    row = db.scalar(select(EncryptedCredential).where(EncryptedCredential.user_id == user_id, EncryptedCredential.kind == kind))
    if not row:
        raise NotificationError(f"未配置 {kind}")
    return decrypt_secret(row.ciphertext)


async def send_wecom(db: Session, user_id: int, markdown: str, report_id: int | None = None) -> dict:
    key = _secret(db, user_id, "wecom_webhook_key")
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    chunks = split_markdown_v2(markdown)
    responses = []
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in chunks:
            response = await client.post(url, params={"key": key}, json={"msgtype": "markdown_v2", "markdown_v2": {"content": chunk}})
            data = response.json()
            if response.status_code >= 400 or data.get("errcode") != 0:
                db.add(NotificationRecord(user_id=user_id, report_id=report_id, channel="wecom", status="failed", detail=f"HTTP {response.status_code}; errcode={data.get('errcode')}"))
                db.commit()
                raise NotificationError(f"企业微信发送失败：{data.get('errmsg', response.status_code)}")
            responses.append({"errcode": data.get("errcode"), "errmsg": data.get("errmsg")})
    db.add(NotificationRecord(user_id=user_id, report_id=report_id, channel="wecom", status="sent", detail=f"chunks={len(chunks)}"))
    db.commit()
    return {"chunks": len(chunks), "responses": responses}


async def send_email_reminder(db: Session, user_id: int, subject: str, body: str, report_id: int | None = None) -> None:
    auth_code = _secret(db, user_id, "qq_smtp_auth_code")
    settings_row = db.get(ReportSetting, user_id)
    if not settings_row or not settings_row.email_sender or not settings_row.email_recipient:
        raise NotificationError("未配置发件人或收件人")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings_row.email_sender
    message["To"] = settings_row.email_recipient
    message.set_content(body)

    def _send() -> None:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.qq.com", 465, context=context, timeout=20) as smtp:
            smtp.login(settings_row.email_sender, auth_code)
            smtp.send_message(message)

    try:
        await asyncio.to_thread(_send)
    except (OSError, smtplib.SMTPException) as exc:
        db.add(NotificationRecord(user_id=user_id, report_id=report_id, channel="email", status="failed", detail=type(exc).__name__))
        db.commit()
        raise NotificationError(f"邮件发送失败：{type(exc).__name__}") from exc
    db.add(NotificationRecord(user_id=user_id, report_id=report_id, channel="email", status="sent", detail="QQ SMTP"))
    db.commit()
