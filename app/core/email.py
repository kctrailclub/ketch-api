import html as html_mod
import json
import logging
import smtplib
import urllib.request
import urllib.error
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

log = logging.getLogger(__name__)


def _send(to: str, subject: str, html: str) -> None:
    if settings.zeptomail_token:
        provider = "ZeptoMail"
    elif settings.resend_api_key:
        provider = "Resend"
    elif settings.smtp_host:
        provider = "SMTP"
    else:
        raise RuntimeError("No email provider configured (set ZEPTOMAIL_TOKEN, RESEND_API_KEY, or SMTP_HOST)")

    log.info("Sending email via %s to=%s subject=%r", provider, to, subject)
    try:
        if provider == "ZeptoMail":
            _send_zeptomail(to, subject, html)
        elif provider == "Resend":
            _send_resend(to, subject, html)
        else:
            _send_smtp(to, subject, html)
        log.info("Email sent successfully via %s to=%s", provider, to)
    except Exception as exc:
        log.error("Email failed via %s to=%s: %s", provider, to, exc)
        raise


def _send_zeptomail(to: str, subject: str, html: str) -> None:
    payload = json.dumps({
        "from": {"address": settings.email_from, "name": settings.email_from_name},
        "to": [{"email_address": {"address": to}}],
        "subject": subject,
        "htmlbody": html,
    }).encode()

    req = urllib.request.Request(
        "https://api.zeptomail.com/v1.1/email",
        data=payload,
        headers={
            "Authorization": settings.zeptomail_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        raise RuntimeError(f"ZeptoMail API error {exc.code}: {body}") from exc


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

    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        raise RuntimeError(f"Resend API error {exc.code}: {body}") from exc


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


def send_registration_confirmation(to: str, firstname: str) -> None:
    safe_name = html_mod.escape(firstname)
    html = f"""
    <p>Hi {safe_name},</p>
    <p>Thank you for requesting an account on <strong>{settings.app_name}</strong>. Your request has been submitted and will be reviewed by an admin.</p>
    <p><strong>Before you can volunteer, please complete the KCTC Volunteer Waiver on the
    <a href="https://ken-carylranch.org">Ken-Caryl Ranch website</a>.</strong>
    Your registration will be reviewed once your waiver is on file.</p>
    <p>You'll receive an invite email once your account is approved.</p>
    """
    _send(to, f"Registration received — {settings.app_name}", html)


def send_hours_logged_email(to: str, firstname: str, hours: float, project_name: str, service_date: str) -> None:
    safe_name = html_mod.escape(firstname)
    safe_project = html_mod.escape(project_name)
    link = f"{settings.frontend_url}/hours"
    html = f"""
    <p>Hi {safe_name},</p>
    <p>An admin has logged <strong>{hours}h</strong> for you on <strong>{safe_project}</strong> (service date: {service_date}).</p>
    <p>These hours have been auto-approved and are reflected in your account.</p>
    <p><a href="{link}">View your hours</a></p>
    """
    _send(to, f"Hours logged for you — {settings.app_name}", html)


def send_hours_approved_email(to: str, firstname: str, hours: float, project_name: str, service_date: str) -> None:
    safe_name = html_mod.escape(firstname)
    safe_project = html_mod.escape(project_name)
    link = f"{settings.frontend_url}/hours"
    html = f"""
    <p>Hi {safe_name},</p>
    <p>Your <strong>{hours}h</strong> submitted for <strong>{safe_project}</strong> (service date: {service_date}) has been <strong>approved</strong>.</p>
    <p><a href="{link}">View your hours</a></p>
    """
    _send(to, f"Hours approved — {settings.app_name}", html)


def send_hours_removed_email(to: str, firstname: str, hours: float, project_name: str, service_date: str, reason: str | None = None) -> None:
    safe_name = html_mod.escape(firstname)
    safe_project = html_mod.escape(project_name)
    reason_line = ""
    if reason:
        safe_reason = html_mod.escape(reason)
        reason_line = f"<p><strong>Reason:</strong> {safe_reason}</p>"
    html = f"""
    <p>Hi {safe_name},</p>
    <p>Your <strong>{hours}h</strong> for <strong>{safe_project}</strong> (service date: {service_date}) has been <strong>removed</strong> by an administrator.</p>
    {reason_line}
    <p>If you have questions, please contact <a href="mailto:membership@kctrailclub.org">membership@kctrailclub.org</a>.</p>
    """
    _send(to, f"Hours removed — {settings.app_name}", html)


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
