import html as html_mod
import json
import smtplib
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings


def _send(to: str, subject: str, html: str) -> None:
    if settings.resend_api_key:
        _send_resend(to, subject, html)
    elif settings.smtp_host:
        _send_smtp(to, subject, html)
    else:
        raise RuntimeError("No email provider configured (set RESEND_API_KEY or SMTP_HOST)")


def _send_resend(to: str, subject: str, html: str) -> None:
    payload = json.dumps({
        "from": f"{settings.email_from_name} <{settings.email_from}>",
        "to": [to],
        "subject": subject,
        "html": html,
    }).encode()

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req) as resp:
        if resp.status not in (200, 201):
            body = resp.read().decode()
            raise RuntimeError(f"Resend API error {resp.status}: {body}")


def _send_smtp(to: str, subject: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.email_from_name} <{settings.email_from}>"
    msg["To"] = to
    msg.attach(MIMEText(html, "html"))

    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.email_from, to, msg.as_string())
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.email_from, to, msg.as_string())


def send_invite_email(to: str, firstname: str, token: str) -> None:
    safe_name = html_mod.escape(firstname)
    link = f"{settings.frontend_url}/set-password?token={token}"
    html = f"""
    <p>Hi {safe_name},</p>
    <p>An account has been created for you on <strong>{settings.app_name}</strong>.</p>
    <p>Click the link below to set your password and get started.
       This link expires in {settings.invite_token_expire_hours} hours.</p>
    <p><a href="{link}">{link}</a></p>
    <p>If you weren't expecting this email, you can ignore it.</p>
    """
    _send(to, f"You've been invited to {settings.app_name}", html)


def send_raw_email(to: str, subject: str, body: str) -> None:
    """Send a plain-text email with a custom subject and body."""
    html = body.replace("\n", "<br>")
    _send(to, subject, html)


def send_password_reset_email(to: str, firstname: str, token: str) -> None:
    safe_name = html_mod.escape(firstname)
    link = f"{settings.frontend_url}/set-password?token={token}"
    html = f"""
    <p>Hi {safe_name},</p>
    <p>We received a request to reset your password for <strong>{settings.app_name}</strong>.</p>
    <p>Click the link below to set a new password.
       This link expires in {settings.invite_token_expire_hours} hours.</p>
    <p><a href="{link}">{link}</a></p>
    <p>If you didn't request a password reset, you can ignore this email.</p>
    """
    _send(to, f"Reset your {settings.app_name} password", html)
