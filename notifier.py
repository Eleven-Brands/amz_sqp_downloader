import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_CU_WORKSPACE   = "31030239"
_CU_CHANNEL_SQP = "xjyyz-50913"  # #teste-automate


def send_clickup(message: str) -> None:
    api_key = os.getenv("CLICKUP_API_KEY")
    if not api_key:
        print("[notifier] CLICKUP_API_KEY not set — skipping ClickUp notification")
        return
    url = f"https://api.clickup.com/api/v3/workspaces/{_CU_WORKSPACE}/chat/channels/{_CU_CHANNEL_SQP}/messages"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"content": message, "content_format": "text/md"},
            timeout=10,
        )
        if resp.ok:
            print(f"[notifier] ClickUp message sent to #teste-automate")
        else:
            print(f"[notifier] ClickUp error {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        print(f"[notifier] ClickUp failed: {exc}")


def send(subject: str, body: str) -> None:
    user = os.getenv("GMAIL_FROM")
    pwd  = os.getenv("GMAIL_APP_PASSWORD")
    to   = os.getenv("NOTIFY_EMAIL", user)

    if not user or not pwd:
        print(f"[notifier] Email not configured — skipping: {subject}")
        return

    msg = MIMEMultipart()
    msg["From"]    = user
    msg["To"]      = to
    msg["Subject"] = f"[SQP Downloader] {subject}"
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(user, pwd)
            smtp.send_message(msg)
        print(f"[notifier] Sent: {subject}")
    except Exception as exc:
        print(f"[notifier] Failed to send email: {exc}")
