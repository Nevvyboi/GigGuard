"""Settings, read once from backend/.env and cached.

Same variable names the Express build used, so an existing .env keeps working
unchanged. Copy .env.example to backend/.env to get started.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Shared secret between the card code, the dashboards and this backend.
    # Sent as the x-api-key header on every call.
    gigguard_api_key: str = "change-me-to-a-long-random-string"

    # Investec credentials for the seeded demo driver. Everyone else gets
    # theirs through /setup.
    investec_client_id: str = ""
    investec_secret: str = ""
    investec_api_key: str = ""
    investec_account_id: str = ""

    # Investec hosts. The sandbox serves both the token and the data from the
    # same host, and the token path is /identity/v2/oauth2/token. Production
    # mints tokens somewhere else entirely, which is exactly the trap that
    # cost us an afternoon: see the knowledge file, note 2.
    investec_token_url: str = (
        "https://openapisandbox.investec.com/identity/v2/oauth2/token"
    )
    investec_api_base: str = "https://openapisandbox.investec.com/za/pb/v1"

    # Kept at 3000 so the dashboards, the card IDE webhook URL and every curl
    # in the README carry over from the Express build untouched.
    port: int = 3000

    # Loopback by default. This backend holds Investec credentials and has one
    # shared key in front of it, so do not bind it to the world; put a tunnel
    # in front of it instead when the card IDE needs a public URL.
    host: str = "127.0.0.1"

    store_path: Path = BACKEND_DIR / "data" / "store.json"

    # Wide open suits a sandbox demo where the dashboards are opened straight
    # off the filesystem. Narrow this to real origins before any other use.
    cors_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
