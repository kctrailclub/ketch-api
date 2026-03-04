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

settings = _raw
