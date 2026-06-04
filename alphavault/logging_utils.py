from __future__ import annotations

import logging
import os


def _parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def is_debug_enabled() -> bool:
    return _parse_bool(os.getenv("STACKWEALTH_DEBUG")) or _parse_bool(os.getenv("ALPHAVAULT_DEBUG"))


def _apply_noise_controls(debug_enabled: bool) -> None:
    # Keep third-party libraries quiet unless debugging is explicitly enabled.
    noisy_level = logging.INFO if debug_enabled else logging.WARNING
    for logger_name in ("yfinance", "urllib3", "robin_stocks"):
        logging.getLogger(logger_name).setLevel(noisy_level)


def configure_logging(debug_enabled: bool | None = None) -> bool:
    enabled = is_debug_enabled() if debug_enabled is None else bool(debug_enabled)
    level = logging.DEBUG if enabled else logging.INFO

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
    else:
        root_logger.setLevel(level)

    _apply_noise_controls(enabled)
    return enabled


def set_debug_logging(enabled: bool) -> None:
    configure_logging(debug_enabled=enabled)


def mask_email(email: str | None) -> str:
    value = str(email or "").strip()
    if "@" not in value:
        return "<redacted>"

    local, domain = value.split("@", 1)
    local_mask = (local[:1] + "***") if local else "***"
    return f"{local_mask}@{domain}"


def mask_account(account_number: str | None) -> str:
    value = str(account_number or "").strip()
    if not value:
        return "default"
    return f"***{value[-4:]}"
