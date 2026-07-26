"""Settings loaded from environment / .env.

Nothing here may carry a real credential, hostname or country: this repository
is public, and the whole point of the VPN requirement is not publishing where
the operator lives. Defaults are placeholders.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_URL = "https://www.erepublik.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    database_url: str = Field(default="postgresql://babel:babel@localhost:5432/babel")

    # Two-letter ISO code of the operator's own country. The crawler refuses to
    # run if the egress IP reports this, which is what makes a VPN failure loud.
    home_country: str = Field(default="XX", min_length=2, max_length=2)
    gluetun_api_url: str = Field(default="http://localhost:8000")

    requests_per_second: float = Field(default=1.0, gt=0, le=20)
    request_timeout_sec: int = Field(default=20, ge=1)
    max_attempts: int = Field(default=3, ge=1)

    # The article page takes comments-per-page in the URL; 1000 returns every
    # comment in the same response as the article. No article has come close.
    comments_per_page: int = Field(default=1000, ge=1)

    image_root: str = Field(default="./data/images")
    min_free_bytes: int = Field(default=20 * 1024**3, ge=0)
    max_image_bytes: int = Field(default=8 * 1024**2, ge=1)

    image_requests_per_second: float = Field(default=5.0, gt=0, le=100)

    # A hard deadline per image. curl's own timeout does not bound a streamed body
    # read, so a host that accepts the connection and then sends nothing hangs the
    # fetch forever — which stalled the whole image archive on the first live run.
    # Generous, because a large image on a slow host is legitimate; bounded,
    # because no single host may cost more than this.
    image_timeout_sec: float = Field(default=60.0, gt=0)

    # How many image fetches may be in flight at once. This is not a politeness
    # setting: the global limiter (image_requests_per_second) and the one-request-
    # per-host lock are what cap load, and both still apply. This exists so a host
    # taking twenty seconds does not spend the whole budget waiting — sequential
    # processing measured 0.13 img/s against the 5.3 img/s the walk produces.
    image_concurrency: int = Field(default=8, ge=1, le=64)

    # How long a just-failed image waits before it may be tried again. Mirrors
    # retry_cooldown_sec on the article side: without it a host having a bad minute
    # burns all five attempts inside that minute, and the ceiling is permanent.
    image_retry_cooldown_sec: float = Field(default=3600.0, ge=0)

    image_batch_size: int = Field(default=50, ge=1)
    image_idle_sleep_sec: float = Field(default=60.0, gt=0)
    image_disk_full_sleep_sec: float = Field(default=300.0, gt=0)

    poll_interval_sec: int = Field(default=900, ge=60)
    rss_pages: int = Field(default=5, ge=1, le=5)
    ip_check_interval_sec: int = Field(default=900, ge=60)

    retry_cooldown_sec: int = Field(default=3600, ge=0)
    backfill_idle_sleep_sec: float = Field(default=300.0, gt=0)

    bot_token: str | None = Field(default=None, description="Telegram bot token. Never committed.")
    chat_id: str | None = Field(default=None, description="Telegram chat id. Never committed.")
    alert_repeat_sec: float = Field(default=3600.0, gt=0)

    def article_url(self, article_id: int) -> str:
        return f"{BASE_URL}/en/article/{article_id}/1/{self.comments_per_page}"

    def rss_url(self, page: int) -> str:
        return f"{BASE_URL}/en/main/news/latest/all/all/{page}/rss"
