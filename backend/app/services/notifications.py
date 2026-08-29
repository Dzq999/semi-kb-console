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


def _secret(db: Session, user_id: int, kind: str) -> str:
    row = db.scalar(select(EncryptedCredential).where(EncryptedCredential.user_id == user_id, EncryptedCredential.kind == kind))
    if not row:
        raise NotificationError(f"未配置 {kind}")
    return decrypt_secret(row.ciphertext)


async def send_wecom(db: Session, user_id: int, markdown: str, report_id: int | None = None) -> dict:
    key = _secret(db, user_id, "wecom_webhook_key")
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    chunks = [markdown[i:i + 3800] for i in range(0, len(markdown), 3800)] or [markdown]
    responses = []
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in chunks:
            response = await client.post(url, params={"key": key}, json={"msgtype": "markdown", "markdown": {"content": chunk}})
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

