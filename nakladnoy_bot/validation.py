"""Hujjatni tekshirish. Har safar joriy holatdan hisoblanadi, tahrirdan keyin o'zi yangilanadi."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from .models import DocView, ExtractedInvoice, Issue
from .numbers import fmt, to_decimal

MAX_AGE_DAYS = 45
TOLERANCE = Decimal("1")  # 1 so'mgacha yaxlitlash farqi xato emas


def _differs(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) > TOLERANCE


def check_document(doc: DocView, today: date) -> list[Issue]:
    issues: list[Issue] = []

    if doc.doc_date is None:
        issues.append(Issue("block", "Sana yo'q"))
    elif doc.doc_date > today + timedelta(days=1):
        issues.append(Issue("warn", f"Sana kelajakda: {doc.doc_date:%d.%m.%Y}"))
    elif doc.doc_date < today - timedelta(days=MAX_AGE_DAYS):
        issues.append(Issue("warn", f"Sana {MAX_AGE_DAYS} kundan eski: {doc.doc_date:%d.%m.%Y}"))

    if doc.contractor_id is None:
        issues.append(Issue("block", "Kontragent tanlanmagan"))
    else:
        if doc.match_method == "fuzzy":
            issues.append(Issue("warn", f"Kontragent taxminan topildi ({doc.match_score}%), tekshiring"))
        if not doc.contract:
            issues.append(Issue("warn", "Kontragentda shartnoma yozilmagan"))
        elif not doc.contract_verified:
            issues.append(Issue("warn", "Shartnoma nomi skrinshotda kesilgan edi, 1C bilan solishtiring"))

    if not doc.lines:
        issues.append(Issue("block", "Birorta ham qator yo'q"))

    for ln in doc.lines:
        label = f"{ln.position}-qator"
        if not ln.name.strip():
            issues.append(Issue("block", f"{label}: nomi yo'q"))
        missing = [t for t, v in (("soni", ln.quantity), ("narxi", ln.price), ("summasi", ln.amount)) if v is None]
        if missing:
            issues.append(Issue("block", f"{label}: {', '.join(missing)} o'qilmadi"))
            continue
        if ln.quantity <= 0 or ln.price < 0 or ln.amount < 0:
            issues.append(Issue("block", f"{label}: son yoki summa noto'g'ri"))
            continue
        computed = ln.quantity * ln.price
        if _differs(computed, ln.amount):
            issues.append(Issue(
                "warn",
                f"{label}: {fmt(ln.quantity)} × {fmt(ln.price)} = {fmt(computed)}, qog'ozda {fmt(ln.amount)}",
            ))
        if ln.confidence == "low":
            issues.append(Issue("warn", f"{label}: yozuv yaxshi o'qilmadi, rasm bilan solishtiring"))
        if ln.match_note:
            issues.append(Issue("warn", f"{label}: {ln.match_note}"))

    if doc.paper_total is not None and doc.lines and _differs(doc.total, doc.paper_total):
        issues.append(Issue(
            "warn", f"Qatorlar yig'indisi {fmt(doc.total)}, qog'ozdagi jami {fmt(doc.paper_total)}"
        ))
    if doc.duplicate_of:
        issues.append(Issue(
            "warn", f"O'xshash hujjat bor (#{doc.duplicate_of}): shu kontragent, sana va summa"
        ))
    for note in doc.ai_notes:
        issues.append(Issue("warn", f"AI izohi: {note}"))
    return issues


def arithmetic_errors(extracted: ExtractedInvoice) -> list[str]:
    """Qayta o'qish kerakmi-yo'qligini aniqlaydi. Natija inglizcha: AI promptiga boradi."""
    errors: list[str] = []
    total = Decimal(0)
    complete = True
    for i, ln in enumerate(extracted.lines, 1):
        qty, price, amount = to_decimal(ln.quantity), to_decimal(ln.price), to_decimal(ln.amount)
        if qty is None or price is None or amount is None:
            errors.append(f"line {i}: quantity, price or amount is missing")
            complete = False
            continue
        total += amount
        if _differs(qty * price, amount):
            errors.append(f"line {i}: {qty} x {price} != {amount}")
    paper_total = to_decimal(extracted.total)
    if complete and paper_total is not None and extracted.lines and _differs(total, paper_total):
        errors.append(f"sum of amounts {total} != written total {paper_total}")
    return errors
