import warnings

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str

    # JWT
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30

    # Invite tokens
    invite_token_expire_hours: int = 72

    # Email — Resend (preferred) or SMTP fallback
    resend_api_key: str | None = None
    email_from: str = "onboarding@resend.dev"
    email_from_name: str = "KCTC Volunteer Hours"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None

    # App
    app_name: str = "KCTC Volunteer Hours"
    frontend_url: str

    # Anthropic (for natural language query)
    anthropic_api_key: str | None = None

    class Config:
        env_file = ".env"


_raw = Settings()

# Strip stray whitespace / newlines from the API key (common copy-paste issue)
if _raw.anthropic_api_key:
    _raw.anthropic_api_key = _raw.anthropic_api_key.strip()

# Warn if SECRET_KEY looks weak (common dev defaults)
_WEAK_MARKERS = {"secret", "changeme", "local", "dev", "test", "example", "placeholder"}
if len(_raw.secret_key) < 32 or any(m in _raw.secret_key.lower() for m in _WEAK_MARKERS):
    warnings.warn(
        "SECRET_KEY appears weak — use a random string of 32+ characters in production "
        "(e.g. python -c \"import secrets; print(secrets.token_urlsafe(64))\")",
        stacklevel=1,
    )

settings = _raw
