from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

# O'zbekistonda yozgi vaqt yo'q; tzdata paketisiz ishlash uchun doimiy siljish.
TASHKENT = timezone(timedelta(hours=5))


def now_local() -> datetime:
    return datetime.now(TASHKENT)


def today_local() -> date:
    return now_local().date()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    telegram_bot_token: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_fallback_model: str = "gemini-3.6-flash,gemini-3.5-flash-lite"
    gemini_thinking_level: str = "medium"
    gemini_timeout_seconds: int = 75
    # Bo'sh bo'lsa, zaxira o'chiq. Gemini limiti tugasa yoki ishlamasa ishlatiladi.
    groq_api_key: str = ""
    groq_model: str = "qwen/qwen3.8-27b"
    database_url: str = ""
    allowed_user_ids: str = ""
    data_dir: Path = ROOT / "data"
    export_profile_path: Path = ROOT / "config" / "export_profile.toml"
    # Bir vaqtda nechta rasm o'qilsin (bepul tarifda 429 bo'lmasligi uchun kichik).
    max_concurrent_reads: int = 2

    @property
    def allowed_ids(self) -> set[int]:
        out: set[int] = set()
        for part in self.allowed_user_ids.replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                out.add(int(part))
        return out
