"""Read environment settings, with an editable local .env taking precedence."""

import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    months: int = 6
    threshold: float = 20
    poll_seconds: float = 3


def read_settings(path=None):
    # Compose supplies env_file values in the process environment. Direct
    # Python runs retain live reloading from their editable local .env.
    values = {**os.environ, **dotenv_values(path or ROOT / ".env")}
    try:
        settings = Settings(int(values.get("HISTORY_MONTHS") or 6),
                            float(values.get("DISCOUNT_THRESHOLD_PERCENT") or 20),
                            float(values.get("TELEGRAM_POLL_SECONDS") or 3))
    except (TypeError, ValueError):
        raise ValueError("Invalid numeric setting in .env or environment variables.") from None
    if settings.months <= 0 or not math.isfinite(settings.threshold) or not 0 <= settings.threshold <= 100:
        raise ValueError("HISTORY_MONTHS must be positive; DISCOUNT_THRESHOLD_PERCENT must be 0–100.")
    if not math.isfinite(settings.poll_seconds) or settings.poll_seconds < 1:
        raise ValueError("TELEGRAM_POLL_SECONDS must be at least 1.")
    if (values.get("TELEGRAM_DETECTION_MODE") or "poll").lower() != "poll":
        raise ValueError("This reader supports group-only polling; push mode is disabled.")
    if (values.get("TELEGRAM_ALLOW_ACCOUNT_UPDATES") or "false").lower() not in {"false", "0", "no"}:
        raise ValueError("Account-wide updates are not permitted for this reader.")
    return settings
