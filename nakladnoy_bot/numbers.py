"""Son va sanalarni o'qish/ko'rsatish. Pul doim Decimal, float emas."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


class ParseError(ValueError):
    pass


def to_decimal(value: float | int | str | Decimal | None) -> Decimal | None:
    """AI qaytargan sonni Decimal'ga o'tkazadi (float xatosini str orqali chetlab)."""
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def parse_number(text: str) -> Decimal:
    """Foydalanuvchi yozgan sonni o'qiydi: 250.000, 250 000, 250,000 -> 250000; 1,5 va 1.5 -> 1.5."""
    s = re.sub(r"[\s '’]", "", text or "")
    if not s or not re.fullmatch(r"\d[\d.,]*", s):
        raise ParseError(f"Son emas: {text!r}")
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        # Oxirgi ajratkich kasr, qolgani minglik.
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        thousands = "," if dec == "." else "."
        s = s.replace(thousands, "").replace(dec, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        parts = s.split(sep)
        is_grouped = len(parts[0]) <= 3 and parts[0] != "0" and all(len(p) == 3 for p in parts[1:])
        if is_grouped or len(parts) > 2:
            s = s.replace(sep, "")
        else:
            s = s.replace(sep, ".")
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ParseError(f"Son emas: {text!r}") from exc


def fmt(value: Decimal | None) -> str:
    """Ko'rsatish uchun: 720000 -> '720 000', 1.5 -> '1.5'."""
    if value is None:
        return "?"
    text = plain(value)
    whole, _, frac = text.partition(".")
    grouped = f"{int(whole):,}".replace(",", " ")
    return f"{grouped}.{frac}" if frac else grouped


def plain(value: Decimal) -> str:
    """Eksponentasiz, ortiqcha nolsiz matn: Decimal('2.000') -> '2'."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_date(text: str, today: date) -> date:
    """03.11.2026, 3.11.26, 3/11, 2026-11-03 shakllarini o'qiydi."""
    s = (text or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if match:
        year, month, day = (int(g) for g in match.groups())
    else:
        match = re.fullmatch(r"(\d{1,2})[./\-\s](\d{1,2})(?:[./\-\s](\d{2}|\d{4}))?", s)
        if not match:
            raise ParseError(f"Sana emas: {text!r}")
        day, month = int(match[1]), int(match[2])
        year = int(match[3]) if match[3] else today.year
        if year < 100:
            year += 2000
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ParseError(f"Sana emas: {text!r}") from exc


def parse_iso_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return datetime.strptime(text.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
