"""定时汇总通知投递。"""
import asyncio
import base64
import hashlib
import hmac
import html
import smtplib
import time
from email.message import EmailMessage

import httpx

from . import egress, store
from .security import decrypt, scrub


async def deliver_summary(
    schedule: dict, run: dict, phase: str, summary: str,
) -> None:
    jobs = []
    for destination_id in store.loads(schedule["feishu_webhook_ids"], []):
        destination = store.get("feishu_webhooks", int(destination_id))
        if destination:
            jobs.append(_deliver(run, phase, "feishu", destination, summary))
    for destination_id in store.loads(schedule["email_recipient_ids"], []):
        destination = store.get("email_recipients", int(destination_id))
        if destination:
            jobs.append(_deliver(run, phase, "email", destination, summary))
    if jobs:
        await asyncio.gather(*jobs)


async def send_feishu_test(destination: dict) -> None:
    await _send_feishu(destination, "模型渠道测试平台：飞书通知接口测试成功。")


async def send_email_test(destination: dict) -> None:
    await asyncio.to_thread(
        _send_email, destination, "通知接口测试\n模型渠道测试平台的 SMTP 发件配置可用。")


async def _deliver(run: dict, phase: str, channel_type: str,
                   destination: dict, summary: str) -> None:
    existing = store.query(
        "SELECT * FROM notification_deliveries WHERE scheduled_run_id=? "
        "AND phase=? AND channel_type=? AND destination_id=?",
        (run["id"], phase, channel_type, destination["id"]),
    )
    if existing and existing[0]["status"] == "sent":
        return
    delivery_id = existing[0]["id"] if existing else store.insert(
        "notification_deliveries", {
            "scheduled_run_id": run["id"], "phase": phase,
            "channel_type": channel_type, "destination_id": destination["id"],
            "status": "pending", "attempts": 0,
        })
    error = ""
    for attempt in range(1, 4):
        try:
            if channel_type == "feishu":
                await _send_feishu(destination, summary)
            else:
                await asyncio.to_thread(_send_email, destination, summary)
            store.update("notification_deliveries", delivery_id, {
                "status": "sent", "attempts": attempt, "error": "", "sent_at": time.time()})
            return
        except Exception as exc:
            exposed_secret = decrypt(destination["webhook_enc"]) \
                if channel_type == "feishu" else ""
            error = scrub(str(exc), exposed_secret)[:300]
            if attempt < 3:
                await asyncio.sleep(attempt * 2)
    store.update("notification_deliveries", delivery_id, {
        "status": "failed", "attempts": 3, "error": error})


async def _send_feishu(destination: dict, summary: str) -> None:
    webhook = decrypt(destination["webhook_enc"])
    payload: dict = {"msg_type": "text", "content": {"text": summary}}
    if destination.get("secret_enc"):
        secret = decrypt(destination["secret_enc"])
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        payload["timestamp"] = timestamp
        payload["sign"] = base64.b64encode(hmac.new(
            string_to_sign.encode(), digestmod=hashlib.sha256).digest()).decode()
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=False,
        max_redirects=0, event_hooks=egress.event_hooks(),
    ) as client:
        response = await client.post(webhook, json=payload)
        response.raise_for_status()
        egress.ensure_response_size(response)
        data = response.json()
        if data.get("code", data.get("StatusCode", 0)) != 0:
            raise RuntimeError(data.get("msg") or data.get("StatusMessage") or "飞书发送失败")


def _send_email(destination: dict, summary: str) -> None:
    settings = store.get("smtp_settings", 1)
    if not settings:
        raise RuntimeError("尚未配置 SMTP 发件接口")
    message = EmailMessage()
    message["Subject"] = summary.splitlines()[0]
    message["From"] = f"{settings['from_name']} <{settings['from_address']}>" \
        if settings["from_name"] else settings["from_address"]
    message["To"] = destination["address"]
    message.set_content(summary)
    message.add_alternative(
        f"<html><body><pre style='font:14px/1.7 sans-serif;white-space:pre-wrap'>"
        f"{html.escape(summary)}</pre></body></html>", subtype="html")
    smtp_type = smtplib.SMTP_SSL if settings["tls_mode"] == "ssl" else smtplib.SMTP
    egress.validate_host(settings["host"], settings["port"])
    with smtp_type(settings["host"], settings["port"], timeout=20) as client:
        if settings["tls_mode"] == "starttls":
            client.starttls()
        client.login(settings["username"], decrypt(settings["password_enc"]))
        client.send_message(message)
