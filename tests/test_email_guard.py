"""BL-0038 / RSK-0007: non-production email redirect and fail-closed guard.

Every provider function is replaced with a recorder, so no network or SMTP call is made.
"""
import pathlib
import re

import pytest

from app.core import email as email_mod
from app.core.config import settings

REDIRECT = "redirect@example.org"
REAL = "member@example.com"
BODY = '<p><a href="http://x/set-password?token=SECRET-TOKEN">link</a></p>'


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def make(name):
        def fn(to, subject, html):
            calls.append((name, to, subject, html))
        return fn

    monkeypatch.setattr(email_mod, "_send_zeptomail", make("ZeptoMail"))
    monkeypatch.setattr(email_mod, "_send_resend", make("Resend"))
    monkeypatch.setattr(email_mod, "_send_smtp", make("SMTP"))
    monkeypatch.setattr(settings, "zeptomail_token", "z")
    monkeypatch.setattr(settings, "resend_api_key", "r")
    monkeypatch.setattr(settings, "smtp_host", "smtp.invalid")
    return calls


def _env(monkeypatch, env, redirect=REDIRECT):
    monkeypatch.setattr(settings, "environment", env)
    monkeypatch.setattr(settings, "email_redirect_to", redirect)


def test_non_production_redirects_to_single_address(monkeypatch, sent):
    _env(monkeypatch, "staging")
    email_mod._send(REAL, "Hours approved", BODY)
    assert len(sent) == 1
    name, to, subject, html = sent[0]
    assert to == REDIRECT
    assert REAL not in to
    assert html == BODY


def test_subject_shows_intended_recipient(monkeypatch, sent):
    _env(monkeypatch, "staging")
    email_mod._send(REAL, "Hours approved", BODY)
    assert sent[0][2] == f"[STAGING → {REAL}] Hours approved"


def test_public_helper_is_redirected(monkeypatch, sent):
    _env(monkeypatch, "staging")
    email_mod.send_password_reset_email(REAL, "Pat", "SECRET-TOKEN")
    assert [c[1] for c in sent] == [REDIRECT]
    assert REAL in sent[0][2]


@pytest.mark.parametrize("redirect", [None, "", "   ", "not-an-address", "a@b.com,c@d.com",
                                      "a@b.com\nBcc: x@y.com", "a@b", "@b.com", "a b@c.com"])
def test_fail_closed_without_valid_redirect(monkeypatch, sent, caplog, redirect):
    _env(monkeypatch, "staging", redirect)
    with pytest.raises(email_mod.EmailBlockedError):
        email_mod._send(REAL, "Reset", BODY)
    assert sent == []  # no provider was called, not even as a fallback
    assert "EMAIL BLOCKED" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text  # body is never logged


@pytest.mark.parametrize("env", ["staging", "dev", "Staging ", "prod", "", "productio"])
def test_anything_but_production_is_guarded(monkeypatch, sent, env):
    _env(monkeypatch, env, None)
    with pytest.raises(email_mod.EmailBlockedError):
        email_mod._send(REAL, "x", BODY)
    assert sent == []


def test_body_never_logged_on_redirect(monkeypatch, sent, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    _env(monkeypatch, "staging")
    email_mod._send(REAL, "Reset", BODY)
    assert "SECRET-TOKEN" not in caplog.text


@pytest.mark.parametrize("env", ["production", "Production", " production "])
def test_production_unchanged(monkeypatch, sent, env):
    _env(monkeypatch, env, REDIRECT)  # redirect value must be ignored in production
    email_mod._send(REAL, "Hours approved", BODY)
    assert sent == [("ZeptoMail", REAL, "Hours approved", BODY)]


def test_production_without_redirect_setting_unchanged(monkeypatch, sent):
    _env(monkeypatch, "production", None)
    email_mod._send(REAL, "Subject", BODY)
    assert sent == [("ZeptoMail", REAL, "Subject", BODY)]


def test_production_fallback_order_preserved(monkeypatch, sent):
    _env(monkeypatch, "production", None)
    order = []

    def boom(name):
        def fn(to, subject, html):
            order.append((name, to, subject))
            if name != "SMTP":
                raise RuntimeError("down")
        return fn

    monkeypatch.setattr(email_mod, "_send_zeptomail", boom("ZeptoMail"))
    monkeypatch.setattr(email_mod, "_send_resend", boom("Resend"))
    monkeypatch.setattr(email_mod, "_send_smtp", boom("SMTP"))
    email_mod._send(REAL, "S", BODY)
    assert order == [("ZeptoMail", REAL, "S"), ("Resend", REAL, "S"), ("SMTP", REAL, "S")]


def test_redirect_uses_same_fallback_with_redirected_target(monkeypatch, sent):
    _env(monkeypatch, "staging")
    order = []

    def boom(name):
        def fn(to, subject, html):
            order.append((name, to))
            if name == "ZeptoMail":
                raise RuntimeError("down")
        return fn

    monkeypatch.setattr(email_mod, "_send_zeptomail", boom("ZeptoMail"))
    monkeypatch.setattr(email_mod, "_send_resend", boom("Resend"))
    email_mod._send(REAL, "S", BODY)
    assert order == [("ZeptoMail", REDIRECT), ("Resend", REDIRECT)]


def test_production_no_provider_still_raises(monkeypatch, sent):
    _env(monkeypatch, "production", None)
    for k in ("zeptomail_token", "resend_api_key", "smtp_host"):
        monkeypatch.setattr(settings, k, None)
    with pytest.raises(RuntimeError, match="No email provider"):
        email_mod._send(REAL, "S", BODY)


def test_provider_payloads_carry_only_one_recipient_and_no_cc_bcc():
    """Static check: the provider functions build a single To and no Cc/Bcc."""
    src = pathlib.Path(email_mod.__file__).read_text()
    assert not re.search(r"\b(bcc|cc)\b", src, re.I)


def test_no_provider_called_outside_email_module():
    """Static check: nothing in app/ reaches a provider or a mail transport except via _send."""
    app_dir = pathlib.Path(email_mod.__file__).resolve().parents[1]
    pattern = re.compile(r"_send_(zeptomail|resend|smtp)\b|\bsmtplib\b|api\.zeptomail\.com|api\.resend\.com")
    offenders = [
        str(p.relative_to(app_dir))
        for p in app_dir.rglob("*.py")
        if p.name != "email.py" and pattern.search(p.read_text())
    ]
    assert offenders == []
