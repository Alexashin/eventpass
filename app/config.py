from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: str = "development"
    app_secret: str = "dev-secret-change-me"
    public_base_url: str = "http://localhost:8080"
    allowed_hosts: str = "localhost,127.0.0.1"

    event_name: str = "RANEPARTY"
    event_date: str = "2026-10-31"
    event_start: str = "22:00"
    event_age: str = "17+"
    ticket_prefix: str = "RANE"
    expected_amount: int = 500

    payment_url: str = "https://example.com/replace-me"
    payment_note: str = "После оплаты вернись в бот и пришли чек"

    owner_username: str = "owner"
    owner_password: str = "change-me-now"

    database_url: str = "sqlite:///./eventpass.db"
    redis_url: str = "redis://redis:6379/0"

    telegram_bot_token: str = ""
    telegram_admin_ids: str = ""

    receipt_dir: str = "./data/receipts"
    receipt_max_bytes: int = 10 * 1024 * 1024
    receipt_max_image_pixels: int = 25_000_000
    receipt_max_pdf_pages: int = 5
    receipt_ocr_pdf_pages: int = 2

    session_https_only: bool = False
    session_max_age_seconds: int = 24 * 60 * 60
    login_max_failures: int = 6
    login_window_seconds: int = 5 * 60
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def admin_telegram_ids(self) -> set[int]:
        result: set[int] = set()
        for raw in self.telegram_admin_ids.split(","):
            raw = raw.strip()
            if raw:
                try:
                    result.add(int(raw))
                except ValueError:
                    pass
        return result

    @property
    def receipt_path(self) -> Path:
        path = Path(self.receipt_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def allowed_host_list(self) -> list[str]:
        hosts = [x.strip() for x in self.allowed_hosts.split(",") if x.strip()]
        public_host = urlparse(self.public_base_url).hostname
        if public_host and public_host not in hosts:
            hosts.append(public_host)
        for internal_host in ("localhost", "127.0.0.1"):
            if internal_host not in hosts:
                hosts.append(internal_host)
        return hosts

    @property
    def event_date_display(self) -> str:
        try:
            yyyy, mm, dd = self.event_date.split("-")
            return f"{dd}.{mm}.{yyyy}"
        except ValueError:
            return self.event_date

    def validate_runtime(self) -> None:
        if not self.is_production:
            return
        errors: list[str] = []
        if len(self.app_secret) < 32 or "change" in self.app_secret.lower() or self.app_secret == "dev-secret-change-me":
            errors.append("APP_SECRET должен быть случайной строкой длиной минимум 32 символа")
        if not self.public_base_url.startswith("https://"):
            errors.append("PUBLIC_BASE_URL в production должен начинаться с https://")
        if not self.session_https_only:
            errors.append("SESSION_HTTPS_ONLY должен быть true в production")
        if (
            self.owner_password in {"change-me-now", "password", "admin"}
            or len(self.owner_password) < 10
            or "change" in self.owner_password.lower()
        ):
            errors.append("OWNER_PASSWORD должен быть сложным и длиной минимум 10 символов")
        if not self.database_url.startswith("postgresql"):
            errors.append("В production должна использоваться PostgreSQL")
        if not self.payment_url.startswith("https://") or "example.com" in self.payment_url:
            errors.append("PAYMENT_URL должен содержать реальную HTTPS-ссылку на оплату")
        if not self.telegram_bot_token:
            errors.append("TELEGRAM_BOT_TOKEN обязателен для production")
        if not self.admin_telegram_ids:
            errors.append("TELEGRAM_ADMIN_IDS обязателен для production")
        if not self.redis_url:
            errors.append("REDIS_URL обязателен для production")
        if "*" in self.allowed_host_list:
            errors.append("ALLOWED_HOSTS не должен содержать * в production")
        if errors:
            raise RuntimeError("Некорректная production-конфигурация:\n- " + "\n- ".join(errors))


@lru_cache
def get_settings() -> Settings:
    return Settings()
