from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/uxie"
    jwt_private_key: str = ""   # RS256 PEM private key
    jwt_public_key: str = ""    # RS256 PEM public key
    jwt_algorithm: str = "RS256"
    jwt_expiry_days: int = 30

    resend_api_key: str = ""
    resend_from: str = "Uxie <noreply@uxie.ai>"
    otp_expiry_minutes: int = 10

    groq_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""   # computer use (model_gateway role "computer_use")
    e2b_api_key: str = ""         # cloud desktops for computer use
    deepgram_api_key: str = ""

    # OAuth client credentials per provider. Set via Railway env.
    google_client_id: str = ""
    google_client_secret: str = ""
    deepgram_project_id: str = ""   # if set, /stt/session mints per-session scoped keys
    deepgram_session_ttl_seconds: int = 300

    free_dictation_limit: int = 100   # per month
    free_command_limit: int = 50      # per month
    pro_dictation_limit: int = 999999
    pro_command_limit: int = 500
    trial_days: int = 30              # new users get 30 free days (full Pro)

    # Per-user burst limits (in-memory token bucket; resets on process restart).
    # Belt-and-braces on top of monthly counters — defends against a stolen JWT
    # being used to spam expensive endpoints in a single night.
    burst_structure_meeting_per_hour: int = 20
    burst_structure_meeting_per_day: int = 50

    # Comma-separated list of emails allowed to view /admin/* pages.
    admin_emails: str = ""

    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    # Cloudflare R2 — stores per-session audio for admin debugging. Optional;
    # when unset, /debug/upload-audio 503s cleanly and text-only logging keeps
    # working.
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
