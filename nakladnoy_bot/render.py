"""Telegram matnlari. Barcha tashqi matn html.escape dan o'tadi: qo'lyozmada < yoki & bo'lishi mumkin."""
from __future__ import annotations

from html import escape

from .models import DocView
from .numbers import fmt

MAX_CARD_LINES = 25
MAX_CARD_WARNINGS = 8

STATUS_TITLES = {
    "processing": "⏳ o'qilmoqda",
    "failed": "⚠️ o'qilmadi",
    "draft": "📝 qoralama",
    "confirmed": "✅ tasdiqlangan, navbatda",
    "exported": "📤 CSV'ga chiqarilgan",
    "cancelled": "❌ bekor qilingan",
}

FAILURE_TEXTS = {
    "auth": "Gemini kaliti ishlamayapti. .env dagi GEMINI_API_KEY ni tekshiring.",
    "quota": "Gemini limiti tugadi. Birozdan keyin «Qayta o'qish»ni bosing.",
    "network": "Gemini bilan aloqa bo'lmadi. Internetni tekshirib, «Qayta o'qish»ni bosing.",
    "model": "Gemini modeli so'rovni qabul qilmadi. .env dagi GEMINI_MODEL ni tekshiring.",
    "bad_output": "AI tushunarsiz javob qaytardi. «Qayta o'qish»ni bosing.",
    "not_invoice": "Rasm nakladnoyga o'xshamaydi. Xato bo'lsa, «Qayta o'qish»ni bosing.",
    "no_image": "Asl rasm saqlanmagan. Rasmni qaytadan yuboring.",
}


def failure_text(error: str) -> str:
    kind = error.split(":", 1)[0]
    return FAILURE_TEXTS.get(kind, "Rasmni o'qib bo'lmadi.")


def render_card(view: DocView) -> str:
    head = f"<b>Nakladnoy #{view.id}</b> · {STATUS_TITLES.get(view.status, view.status)}"
    if view.status == "failed":
        return f"{head}\n\n{escape(failure_text(view.error))}"

    parts = [head, ""]
    parts.append(f"📅 Sana: <b>{view.doc_date:%d.%m.%Y}</b>" if view.doc_date else "📅 Sana: <b>yo'q</b>")
    if view.contractor_id:
        parts.append(f"🏢 Kontragent: <b>{escape(view.contractor_name)}</b>")
        parts.append(f"     ИНН {escape(view.contractor_inn) or '—'}")
        parts.append(f"📑 {escape(view.contract) or 'Shartnoma yozilmagan'}")
    else:
        parts.append("🏢 Kontragent: <b>tanlanmagan</b>")
    if view.supplier_raw:
        parts.append(f"✍️ Qog'ozda: {escape(view.supplier_raw)}")
    if view.receiver_raw:
        parts.append(f"📦 Kimga: {escape(view.receiver_raw)}")
    if view.doc_number:
        parts.append(f"🧾 Hujjat №: {escape(view.doc_number)}")
    if view.comment:
        parts.append(f"💬 Izoh: {escape(view.comment)}")

    parts.append("")
    # Telegram xabari 4096 belgidan oshmasin (ko'p betli PDF'da qatorlar ko'p bo'lishi mumkin).
    for ln in view.lines[:MAX_CARD_LINES]:
        unit = f" {escape(ln.unit[:20])}" if ln.unit else ""
        parts.append(
            f"{ln.position}. {escape(ln.name[:70])} — {fmt(ln.quantity)}{unit} × {fmt(ln.price)} = <b>{fmt(ln.amount)}</b>"
        )
    if len(view.lines) > MAX_CARD_LINES:
        parts.append(f"… yana {len(view.lines) - MAX_CARD_LINES} ta qator")
    parts.append("")
    total_line = f"💰 Jami: <b>{fmt(view.total)} UZS</b>"
    if view.paper_total is not None and abs(view.paper_total - view.total) <= 1:
        total_line += " (qog'oz bilan mos)"
    parts.append(total_line)

    if view.blocks:
        parts.append("")
        parts.extend(f"⛔ {escape(i.text)}" for i in view.blocks)
    if view.warnings:
        parts.append("")
        parts.extend(f"⚠️ {escape(i.text[:200])}" for i in view.warnings[:MAX_CARD_WARNINGS])
        if len(view.warnings) > MAX_CARD_WARNINGS:
            parts.append(f"⚠️ … yana {len(view.warnings) - MAX_CARD_WARNINGS} ta ogohlantirish")
    return "\n".join(parts)


HELP_TEXT = """<b>Nakladnoy → 1C CSV bot</b>

1. Nakladnoy rasmini yuboring (bir nechtasini birdan ham bo'ladi).
2. Kartani rasm bilan solishtirib, <b>✅ Tasdiqlash</b>ni bosing.
3. Kun oxirida <b>📄 CSV olish</b>: tasdiqlanganlarning hammasi bitta faylga tushadi.

Yozuv xira bo'lsa, rasmni <b>fayl sifatida</b> yuboring: Telegram siqmaydi.

<b>Buyruqlar</b>
/csv — navbatdagini CSV qilish
/navbat — tasdiqlangan hujjatlar
/tarix — oldingi CSV fayllar
/kontragentlar — ro'yxat
/kontragent_qosh — qo'shish yoki yangilash
/kontragent_import — 1C ro'yxatini CSV'dan yuklash
/materiallar — katalog
/material_import — 1C nomenklaturasini CSV'dan yuklash
/bekor — joriy kiritishni to'xtatish"""
