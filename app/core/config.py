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

    # Email
    smtp_host: str
    smtp_port: int = 587
    smtp_user: str
    smtp_password: str
    email_from: str
    email_from_name: str = "KCTC Volunteer Hours"

    # App
    app_name: str = "KCTC Volunteer Hours"
    frontend_url: str

    # Anthropic (for natural language query)
    anthropic_api_key: str | None = None

    class Config:
        env_file = ".env"


settings = Settings()
