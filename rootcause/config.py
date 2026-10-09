"""Runtime settings, read from the environment."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("ROOTCAUSE_DB", "sqlite:///rootcause.db")
    telemetry_dir: str = os.getenv("TELEMETRY_DIR", "data/telemetry")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    bucket_seconds: float = float(os.getenv("BUCKET_SECONDS", "5"))
    baseline_seconds: float = float(os.getenv("BASELINE_SECONDS", "120"))


settings = Settings()
