"""Coercion of portal strings into typed, canonical values.

Every function here is total: it returns ``None`` rather than raising when the
input is unusable. A tender with an unparseable budget is still a perfectly
good tender, and losing it over one malformed field would be absurd.

Locale handling is the subtle part. ``1.234,56`` is one thousand two hundred in
France and Tunisia; ``1,234.56`` is the same number in the US. Guessing wrong
is a factor-of-1000 error on a budget, so the separators are configured per
portal and only inferred when the config is silent.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "detect_currency",
    "normalize_date",
    "normalize_email",
    "normalize_money",
    "normalize_text",
    "parse_bool",
    "strip_patterns",
]

_WHITESPACE = re.compile(r"\s+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

#: Month names for French and English, which between them cover every portal in
#: scope. Unknown locales fall through to dateutil.
_MONTHS = {
    "janvier": 1, "january": 1, "jan": 1, "janv": 1,
    "fevrier": 2, "february": 2, "feb": 2, "fev": 2, "fevr": 2,
    "mars": 3, "march": 3, "mar": 3,
    "avril": 4, "april": 4, "apr": 4, "avr": 4,
    "mai": 5, "may": 5,
    "juin": 6, "june": 6, "jun": 6,
    "juillet": 7, "july": 7, "jul": 7, "juil": 7,
    "aout": 8, "august": 8, "aug": 8,
    "septembre": 9, "september": 9, "sep": 9, "sept": 9,
    "octobre": 10, "october": 10, "oct": 10,
    "novembre": 11, "november": 11, "nov": 11,
    "decembre": 12, "december": 12, "dec": 12,
}

_CURRENCY_SYMBOLS = {
    "€": "EUR", "$": "USD", "£": "GBP", "dt": "TND", "tnd": "TND",
    "dinar": "TND", "dinars": "TND", "eur": "EUR", "euro": "EUR",
    "euros": "EUR", "usd": "USD", "dollar": "USD", "dollars": "USD",
    "mad": "MAD", "dzd": "DZD", "xof": "XOF", "fcfa": "XOF", "cfa": "XOF",
}


def normalize_text(value: Any, *, max_length: int | None = None) -> str | None:
    """Collapse whitespace and strip zero-width junk. ``None`` if nothing remains."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    cleaned = value.replace(" ", " ").replace("​", "").replace("﻿", "")
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if not cleaned:
        return None
    if max_length and len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip()
    return cleaned


def strip_patterns(value: str | None, patterns: list[str] | None) -> str | None:
    """Remove portal boilerplate ("Objet :", "(nouvelle fenêtre)") from a value."""
    if not value or not patterns:
        return value
    result = value
    for pattern in patterns:
        try:
            result = re.sub(pattern, "", result, flags=re.IGNORECASE)
        except re.error:
            logger.warning("normalizer.invalid_strip_pattern", pattern=pattern)
    return normalize_text(result)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def _textual_date(value: str) -> datetime | None:
    """Parse "15 janvier 2026" / "15 Jan 2026 14:30" style dates."""
    lowered = value.lower()
    lowered = (
        lowered.replace("é", "e").replace("è", "e").replace("û", "u").replace("î", "i")
    )
    match = re.search(
        r"(\d{1,2})\s*(?:er)?\s+([a-z]+)\.?\s+(\d{4})(?:\D+(\d{1,2})[:h](\d{2}))?",
        lowered,
    )
    if not match:
        return None
    day, month_name, year, hour, minute = match.groups()
    month = _MONTHS.get(month_name.strip("."))
    if not month:
        return None
    try:
        return datetime(
            int(year), month, int(day), int(hour or 0), int(minute or 0), tzinfo=timezone.utc
        )
    except ValueError:
        return None


def normalize_date(
    value: Any,
    *,
    formats: list[str] | None = None,
    tz: str | None = None,
    dayfirst: bool = True,
) -> datetime | None:
    """Parse a portal date into an aware UTC datetime.

    Tries, in order: the portal's declared formats (fastest and unambiguous),
    ISO-8601, French/English textual dates, then dateutil as a last resort.
    A naive result is interpreted in the portal's timezone — assuming UTC would
    silently shift a submission deadline by up to a day, which is the one error
    in this module that could actually cost a bid.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else _localize(value, tz)
    if isinstance(value, date):
        return _localize(datetime(value.year, value.month, value.day), tz)

    text = normalize_text(value)
    if not text:
        return None

    for fmt in formats or []:
        try:
            parsed = datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
        return parsed if parsed.tzinfo else _localize(parsed, tz)

    iso_candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        return parsed if parsed.tzinfo else _localize(parsed, tz)
    except ValueError:
        pass

    textual = _textual_date(text)
    if textual is not None:
        return textual

    try:
        from dateutil import parser as dateutil_parser

        parsed = dateutil_parser.parse(text, dayfirst=dayfirst, fuzzy=True)
        return parsed if parsed.tzinfo else _localize(parsed, tz)
    except Exception:
        logger.debug("normalizer.date_unparseable", value=text[:80])
        return None


def _localize(naive: datetime, tz: str | None) -> datetime:
    if tz:
        try:
            from zoneinfo import ZoneInfo

            return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
        except Exception:
            pass
    return naive.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def detect_currency(value: str | None, default: str | None = None) -> str | None:
    if not value:
        return default
    lowered = value.lower()
    for token, code in _CURRENCY_SYMBOLS.items():
        if token in lowered:
            return code
    match = re.search(r"\b([A-Z]{3})\b", value)
    if match and match.group(1) not in {"THE", "AND", "FOR"}:
        return match.group(1)
    return default


def normalize_money(
    value: Any,
    *,
    decimal_separator: str = ",",
    thousands_separator: str = " ",
    default_currency: str | None = None,
) -> tuple[Decimal | None, str | None]:
    """Extract ``(amount, currency)`` from a portal's money string."""
    if value is None or value == "":
        return None, default_currency
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value)), default_currency
        except InvalidOperation:
            return None, default_currency

    text = normalize_text(value)
    if not text:
        return None, default_currency

    currency = detect_currency(text, default_currency)

    # Multipliers are common in tender notices and change the value by 10^6.
    multiplier = Decimal(1)
    lowered = text.lower()
    if re.search(r"\bmillions?\b|\bmd\b", lowered):
        multiplier = Decimal(1_000_000)
    elif re.search(r"\bmilliers?\b|\bk\b(?!\w)", lowered):
        multiplier = Decimal(1_000)

    numeric = re.sub(r"[^\d.,\s' -]", "", text).strip()
    if not numeric:
        return None, currency

    numeric = numeric.replace(" ", " ").replace("'", "")
    if thousands_separator:
        numeric = numeric.replace(thousands_separator, "")
    numeric = numeric.replace(" ", "")

    if decimal_separator == "," :
        # Remaining dots are thousands separators in this locale.
        numeric = numeric.replace(".", "").replace(",", ".")
    else:
        numeric = numeric.replace(",", "")

    match = re.search(r"-?\d+(?:\.\d+)?", numeric)
    if not match:
        return None, currency
    try:
        amount = Decimal(match.group(0)) * multiplier
    except InvalidOperation:
        return None, currency
    if amount < 0:
        return None, currency
    return amount.quantize(Decimal("0.01")), currency


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
def normalize_email(value: Any) -> str | None:
    """Pull an address out of raw text or a ``mailto:`` href."""
    if not value:
        return None
    text = str(value)
    if text.lower().startswith("mailto:"):
        text = text[7:].split("?")[0]
    match = _EMAIL.search(text)
    return match.group(0).lower() if match else None


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "oui", "vrai", "on"}:
        return True
    if text in {"false", "0", "no", "n", "non", "faux", "off"}:
        return False
    return default
