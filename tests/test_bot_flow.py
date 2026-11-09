"""Telegram oqimi: haqiqiy aiogram Dispatcher, faqat tarmoq qatlami soxta."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from itertools import count

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, GetFile, SendDocument, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    File,
    Message,
    PhotoSize,
    Update,
    User,
)

from nakladnoy_bot.config import Settings
from nakladnoy_bot.gemini import ExtractionError
from nakladnoy_bot.handlers import build_dispatcher

from .conftest import invoice, make_image

OWNER = 7001
STRANGER = 7002
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
CHAT = Chat(id=OWNER, type="private")
USER = User(id=OWNER, is_bot=False, first_name="Egasi")


class FakeSession(BaseSession):
    """So'rovlarni yozib oladi; yuklab olish uchun berilgan baytlarni qaytaradi."""

    def __init__(self):
        super().__init__()
        self.sent: list = []
        self.file_bytes = b""
        self._ids = count(1000)

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path="photos/x.jpg")
        if isinstance(method, AnswerCallbackQuery):
            return True
        chat_id = getattr(method, "chat_id", OWNER)
        msg = Message(
            message_id=getattr(method, "message_id", None) or next(self._ids),
            date=NOW,
            chat=Chat(id=chat_id, type="private"),
            text=getattr(method, "text", None),
        )
        return msg.as_(bot)

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield self.file_bytes

    # --- yordamchilar ---
    @property
    def texts(self) -> list[str]:
        return [
            m.text for m in self.sent
            if hasattr(m, "text") and m.text and not isinstance(m, AnswerCallbackQuery)
        ]

    @property
    def last_markup(self):
        for method in reversed(self.sent):
            markup = getattr(method, "reply_markup", None)
            if markup is not None and hasattr(markup, "inline_keyboard"):
                return markup
        return None

    def button(self, label: str) -> str:
        for row in self.last_markup.inline_keyboard:
            for btn in row:
                if label in btn.text:
                    return btn.callback_data
        raise AssertionError(f"Tugma yo'q: {label}; bor: {self.last_markup}")

    @property
    def documents(self) -> list[SendDocument]:
        return [m for m in self.sent if isinstance(m, SendDocument)]

    @property
    def alerts(self) -> list[AnswerCallbackQuery]:
        return [m for m in self.sent if isinstance(m, AnswerCallbackQuery) and m.show_alert]


class Harness:
    def __init__(self, bot, session, dp, service):
        self.bot, self.session, self.dp, self.service = bot, session, dp, service
        self._ids = count(1)

    def _update(self, **kwargs) -> Update:
        return Update(update_id=next(self._ids), **kwargs)

    async def text(self, text: str, user_id: int = OWNER):
        user = User(id=user_id, is_bot=False, first_name="U")
        msg = Message(message_id=next(self._ids), date=NOW, chat=Chat(id=user_id, type="private"), from_user=user, text=text)
        await self.dp.feed_update(self.bot, self._update(message=msg))

    async def photo(self, raw: bytes, user_id: int = OWNER):
        self.session.file_bytes = raw
        user = User(id=user_id, is_bot=False, first_name="U")
        sizes = [
            PhotoSize(file_id="small", file_unique_id="s", width=90, height=90),
            PhotoSize(file_id="large", file_unique_id="l", width=1280, height=960, file_size=len(raw)),
        ]
        msg = Message(message_id=next(self._ids), date=NOW, chat=Chat(id=user_id, type="private"), from_user=user, photo=sizes)
        await self.dp.feed_update(self.bot, self._update(message=msg))

    async def document(self, raw: bytes, name: str, mime: str, size: int | None = None):
        self.session.file_bytes = raw
        doc = Document(file_id="doc", file_unique_id="d", file_name=name, mime_type=mime, file_size=size or len(raw))
        msg = Message(message_id=next(self._ids), date=NOW, chat=CHAT, from_user=USER, document=doc)
        await self.dp.feed_update(self.bot, self._update(message=msg))

    async def press(self, data: str, user_id: int = OWNER):
        user = User(id=user_id, is_bot=False, first_name="U")
        host = Message(message_id=500, date=NOW, chat=Chat(id=user_id, type="private"), text="karta")
        cb = CallbackQuery(id=str(next(self._ids)), from_user=user, chat_instance="c", message=host, data=data)
        await self.dp.feed_update(self.bot, self._update(callback_query=cb))


@pytest_asyncio.fixture
async def harness(make_service):
    def build(*responses) -> Harness:
        service = make_service(*responses)
        session = FakeSession()
        bot = Bot("123:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        settings = Settings(allowed_user_ids=str(OWNER), telegram_bot_token="x", database_url="x")
        return Harness(bot, session, build_dispatcher(settings, service), service)

    yield build
    # Router modul darajasida: keyingi test uchun Dispatcher'dan ajratiladi.
    from nakladnoy_bot.handlers import router

    router._parent_router = None


def csv_rows(document) -> list[dict]:
    return list(csv.DictReader(io.StringIO(document.data.decode("utf-8-sig"), newline=""), delimiter=";"))


async def test_stranger_is_refused_and_told_their_id(harness):
    h = harness()
    await h.text("/start", user_id=STRANGER)
    assert "Ruxsat yo'q" in h.session.texts[-1] and str(STRANGER) in h.session.texts[-1]
    await h.photo(make_image(), user_id=STRANGER)
    assert h.service.extractor.calls == []


async def test_stranger_cannot_press_buttons(harness):
    h = harness()
    await h.photo(make_image())
    data = h.session.button("Tasdiqlash")
    await h.press(data, user_id=STRANGER)
    assert h.session.alerts
    items, _ = await h.service.queue()
    assert items == []


async def test_start_shows_help(harness):
    h = harness()
    await h.text("/start")
    assert "Nakladnoy" in h.session.texts[-1]


async def test_photo_to_csv_end_to_end(harness):
    h = harness()
    await h.photo(make_image())
    card = h.session.texts[-1]
    assert "Тошматов Азиз" in card
    assert "03.11.2026" in card
    assert "720 000" in card and "qog'oz bilan mos" in card
    assert "1. А4 лист — 2 блок × 250 000 = <b>500 000</b>" in card

    await h.press(h.session.button("Tasdiqlash"))
    assert "tasdiqlangan" in h.session.texts[-1]

    await h.text("/csv")
    assert len(h.session.documents) == 1
    document = h.session.documents[0].document
    assert document.filename.startswith("postuplenie_")
    rows = csv_rows(document)
    assert [r["Сумма"] for r in rows] == ["500000", "140000", "80000"]
    assert rows[0]["ИНН"] == "300000001"
    assert "720 000" in h.session.documents[0].caption

    await h.text("/csv")
    assert "Navbat bo'sh" in h.session.texts[-1]
    assert len(h.session.documents) == 1


async def test_menu_button_exports_and_history_redownloads(harness):
    h = harness()
    await h.photo(make_image())
    await h.press(h.session.button("Tasdiqlash"))
    await h.press(h.session.button("CSV olish"))
    assert len(h.session.documents) == 1
    await h.text("🕘 Tarix")
    await h.press(h.session.last_markup.inline_keyboard[0][0].callback_data)
    assert len(h.session.documents) == 2
    assert h.session.documents[0].document.data == h.session.documents[1].document.data
    assert "ikki marta" in h.session.documents[1].caption


async def test_duplicate_photo_message(harness):
    h = harness()
    raw = make_image()
    await h.photo(raw)
    await h.photo(raw)
    assert "oldin yuborilgan" in h.session.texts[-1]
    assert len(h.service.extractor.calls) == 1


async def test_unknown_supplier_pick_from_buttons(harness):
    h = harness(invoice(supplier_name="AVTO"))
    await h.photo(make_image())
    assert "tanlanmagan" in h.session.texts[-1]
    await h.press(h.session.button("Tasdiqlash"))
    assert "Kontragent tanlanmagan" in h.session.alerts[-1].text

    await h.press(h.session.button("Kontragent tanlash"))
    await h.press(h.session.button("AVTOTRAK"))
    assert "AVTOTRAK" in h.session.texts[-1] and "300000005" in h.session.texts[-1]
    await h.press(h.session.button("Tasdiqlash"))
    assert "tasdiqlangan" in h.session.texts[-1]


async def test_new_contractor_via_text(harness):
    h = harness(invoice(supplier_name="Каримов Жасур"))
    await h.photo(make_image())
    await h.press(h.session.button("Kontragent tanlash"))
    await h.press(h.session.button("Yangi"))
    await h.text("bu noto'g'ri")
    assert "Shakl" in h.session.texts[-1]
    await h.text("399999999; Каримов Жасур (снабженец); Договор № 9 от 01.01.2026г.")
    assert "Каримов Жасур (снабженец)" in h.session.texts[-1]
    assert "399999999" in h.session.texts[-1]


async def test_contractor_search_flow(harness):
    h = harness(invoice(supplier_name="Номаълум"))
    await h.photo(make_image())
    await h.press(h.session.button("Kontragent tanlash"))
    await h.press(h.session.button("Qidirish"))
    await h.text("temirsavdo")
    await h.press(h.session.button("TEMIRSAVDO"))
    assert "300000003" in h.session.texts[-1]


async def test_edit_date_with_validation(harness):
    h = harness()
    await h.photo(make_image())
    await h.press(h.session.button("Tahrirlash"))
    await h.press(h.session.button("Sana"))
    await h.text("kecha")
    assert "Sana shakli" in h.session.texts[-1]
    await h.text("01.11.2026")
    assert "01.11.2026" in h.session.texts[-1]


async def test_edit_line_recalculates_total(harness):
    h = harness()
    await h.photo(make_image())
    await h.press(h.session.button("Tahrirlash"))
    await h.press(h.session.button("3. Офис папка"))
    assert "Офис папка; шт; 2; 40000" in h.session.texts[-1]
    await h.text("Офис папка; шт; 3; 40000")
    assert "120 000" in h.session.texts[-1] and "760 000" in h.session.texts[-1]
    assert "ozdagi jami 720 000" in h.session.texts[-1]


async def test_delete_and_add_line(harness):
    h = harness()
    await h.photo(make_image())
    await h.press(h.session.button("Tahrirlash"))
    await h.press(h.session.button("1. А4 лист"))
    await h.press(h.session.button("Qatorni o'chirish"))
    assert "А4 лист" not in h.session.texts[-1]
    await h.press(h.session.button("Tahrirlash"))
    await h.press(h.session.button("Qator qo'shish"))
    await h.text("Ручка; шт; 10; 2 000")
    assert "Ручка — 10 шт × 2 000 = <b>20 000</b>" in h.session.texts[-1]


async def test_cancel_and_restore(harness):
    h = harness()
    await h.photo(make_image())
    await h.press(h.session.button("Bekor qilish"))
    assert "bekor qilingan" in h.session.texts[-1]
    await h.press(h.session.button("Tiklash"))
    assert "qoralama" in h.session.texts[-1]


async def test_ai_failure_shows_retry_button(harness):
    h = harness(ExtractionError("quota", "limit"), invoice())
    await h.photo(make_image())
    assert "limiti tugadi" in h.session.texts[-1]
    await h.press(h.session.button("Qayta o'qish"))
    assert "Тошматов" in h.session.texts[-1]


async def test_bad_image_message(harness):
    h = harness()
    await h.document(b"rasm emas", "x.jpg", "image/jpeg")
    assert "ochilmadi" in h.session.texts[-1]


async def test_image_sent_as_file_is_read(harness):
    h = harness()
    await h.document(make_image(), "skan.jpg", "image/jpeg")
    assert "Тошматов" in h.session.texts[-1]


async def test_oversized_file_is_refused(harness):
    h = harness()
    await h.document(make_image(), "katta.jpg", "image/jpeg", size=25 * 1024 * 1024)
    assert "20 MB" in h.session.texts[-1]
    assert h.service.extractor.calls == []


async def test_other_files_explained(harness):
    h = harness()
    await h.document(b"a;b", "ro'yxat.csv", "text/csv")
    assert "kontragent_import" in h.session.texts[-1]


async def test_import_contractors_flow(harness):
    h = harness()
    await h.text("/kontragent_import")
    await h.document("ИНН;Контрагент;Договор\r\n399999999;Yangi MCHJ;D-1\r\n".encode("utf-8-sig"), "k.csv", "text/csv")
    assert "1 ta qo'shildi" in h.session.texts[-1]
    await h.text("/kontragentlar")
    assert "Yangi MCHJ" in "".join(h.session.texts[-2:])


async def test_contractor_upsert_fixes_truncated_contract(harness):
    h = harness()
    await h.text("/kontragentlar")
    assert "⚠️" in h.session.texts[-1]
    await h.text("/kontragent_qosh")
    await h.text('300000005; "AVTOTRAK" MCHJ; Договор № 5 от 01.01.2026г.')
    assert "Yangilandi" in h.session.texts[-1]


async def test_command_escapes_input_state(harness):
    h = harness()
    await h.photo(make_image())
    await h.press(h.session.button("Tahrirlash"))
    await h.press(h.session.button("Sana"))
    await h.text("/bekor")
    await h.text("03.11.2026")  # endi sana sifatida qabul qilinmaydi
    assert "rasmini yuboring" in h.session.texts[-1]


async def test_html_in_handwriting_is_escaped(harness):
    h = harness(invoice(supplier_name="<b>Firma</b> & Co"))
    await h.photo(make_image())
    assert "&lt;b&gt;Firma&lt;/b&gt; &amp; Co" in h.session.texts[-1]


async def test_queue_lists_confirmed_and_counts_drafts(harness):
    h = harness()
    await h.photo(make_image("a"))
    await h.press(h.session.button("Tasdiqlash"))
    await h.photo(make_image("b"))
    await h.text("📋 Navbat")
    text = h.session.texts[-1]
    assert "Navbatda 1 ta hujjat" in text and "qoralama: 1 ta" in text


async def test_card_with_many_lines_fits_telegram_limit(harness):
    lines = [
        {"name": "Ж" * 300, "unit": "шт", "quantity": 1, "price": 1000, "amount": 1000, "confidence": "low"}
        for _ in range(120)
    ]
    h = harness(invoice(lines=lines, total=120000))
    await h.photo(make_image())
    assert len(h.session.texts[-1]) < 4096
    assert "yana 95 ta qator" in h.session.texts[-1]
