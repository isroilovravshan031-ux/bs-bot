"""Telegram qatlami: tugmalar va xabarlar. Mantiq service.py da."""
from __future__ import annotations

import io
import logging
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)

from .config import Settings, today_local
from .exporter import EncodingProblem
from .imaging import BadImage
from .models import DocView
from .numbers import ParseError, fmt, parse_date
from .render import HELP_TEXT, render_card
from .service import (
    BadInput,
    Blocked,
    NotEditable,
    Service,
    parse_contractor_input,
    parse_line_input,
)

log = logging.getLogger(__name__)
router = Router()

MAX_FILE_BYTES = 20 * 1024 * 1024  # Telegram bot API yuklab olish chegarasi
BTN_CSV = "📄 CSV olish"
BTN_QUEUE = "📋 Navbat"
BTN_HISTORY = "🕘 Tarix"
BTN_CONTRACTORS = "🏢 Kontragentlar"
STALE_TEXT = "Bu hujjat holati o'zgargan, karta yangilandi."


class DocCb(CallbackData, prefix="d"):
    action: str
    id: int
    arg: int = 0


class BatchCb(CallbackData, prefix="b"):
    id: int


class Input(StatesGroup):
    date = State()
    number = State()
    comment = State()
    line = State()
    new_line = State()
    contractor_search = State()
    new_contractor = State()
    contractor_upsert = State()
    import_contractors = State()
    import_materials = State()


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CSV), KeyboardButton(text=BTN_QUEUE)],
            [KeyboardButton(text=BTN_HISTORY), KeyboardButton(text=BTN_CONTRACTORS)],
        ],
        resize_keyboard=True,
    )


def _btn(text: str, action: str, doc_id: int, arg: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=DocCb(action=action, id=doc_id, arg=arg).pack())


def card_keyboard(view: DocView) -> InlineKeyboardMarkup | None:
    if view.status == "draft":
        rows = []
        if view.contractor_id is None:
            rows.append([_btn("🏢 Kontragent tanlash", "contractor", view.id)])
        rows.append([_btn("✅ Tasdiqlash", "confirm", view.id), _btn("✏️ Tahrirlash", "edit", view.id)])
        rows.append([_btn("❌ Bekor qilish", "cancel", view.id)])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    if view.status == "confirmed":
        return InlineKeyboardMarkup(inline_keyboard=[
            [_btn("📄 CSV olish", "export", view.id), _btn("↩️ Qoralamaga", "unconfirm", view.id)],
        ])
    if view.status == "cancelled":
        return InlineKeyboardMarkup(inline_keyboard=[[_btn("↩️ Tiklash", "restore", view.id)]])
    if view.status == "failed":
        return InlineKeyboardMarkup(inline_keyboard=[[_btn("🔄 Qayta o'qish", "retry", view.id)]])
    return None


def edit_keyboard(view: DocView) -> InlineKeyboardMarkup:
    rows = [
        [_btn("📅 Sana", "f_date", view.id), _btn("🏢 Kontragent", "contractor", view.id)],
        [_btn("🧾 Hujjat №", "f_number", view.id), _btn("💬 Izoh", "f_comment", view.id)],
    ]
    for ln in view.lines:
        title = f"{ln.position}. {ln.name}"
        rows.append([_btn(title[:40], "line", view.id, ln.position)])
    rows.append([_btn("➕ Qator qo'shish", "add_line", view.id), _btn("⬅️ Orqaga", "back", view.id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def contractor_keyboard(doc_id: int, choices: list[dict]) -> InlineKeyboardMarkup:
    rows = [[_btn(c["name"][:45], "pick", doc_id, c["id"])] for c in choices]
    rows.append([_btn("🔎 Qidirish", "c_search", doc_id), _btn("➕ Yangi", "c_new", doc_id)])
    rows.append([_btn("⬅️ Orqaga", "back", doc_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target: Message, view: DocView, *, edit: bool = False) -> None:
    text, markup = render_card(view), card_keyboard(view)
    if not edit:
        await target.answer(text, reply_markup=markup)
        return
    try:
        await target.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


async def _refresh(callback: CallbackQuery, service: Service, doc_id: int, note: str = "") -> None:
    view = await service.view(doc_id)
    if view is None:
        await callback.answer("Hujjat topilmadi", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await _show(callback.message, view, edit=True)
    await callback.answer(note)


# ---------- Ruxsat ----------


class AccessMiddleware(BaseMiddleware):
    """Faqat ALLOWED_USER_IDS. Hujjatlarda INN va narxlar bor, bot ochiq bo'lmasligi kerak."""

    def __init__(self, allowed: set[int]):
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id in self.allowed:
            return await handler(event, data)
        log.warning("Ruxsatsiz urinish: %s", user.id)
        if isinstance(event, Message):
            await event.answer(
                f"Ruxsat yo'q. Sizning Telegram ID: <code>{user.id}</code>\n"
                "Egasi shu raqamni .env dagi ALLOWED_USER_IDS ga qo'shib, botni qayta ishga tushirsin."
            )
        elif isinstance(event, CallbackQuery):
            await event.answer("Ruxsat yo'q", show_alert=True)
        return None


# ---------- Buyruqlar va asosiy tugmalar (har qanday holatda ishlaydi) ----------


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=main_keyboard())


@router.message(Command("yordam", "help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=main_keyboard())


@router.message(Command("bekor"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("To'xtatildi.", reply_markup=main_keyboard())


async def _send_batch(bot: Bot, chat_id: int, filename: str, content: bytes, caption: str) -> None:
    await bot.send_document(chat_id, BufferedInputFile(content, filename=filename), caption=caption)


async def _do_export(bot: Bot, chat_id: int, user_id: int, service: Service) -> None:
    try:
        result = await service.export(user_id)
    except EncodingProblem as exc:
        await bot.send_message(chat_id, f"⛔ {escape(str(exc))}\nHujjatlar navbatda qoldi.")
        return
    if result is None:
        await bot.send_message(chat_id, "Navbat bo'sh. Avval hujjatni tasdiqlang.")
        return
    caption = (
        f"📄 Hujjat: <b>{result.doc_count}</b>\n💰 Jami: <b>{fmt(result.total)} UZS</b>\n"
        "Fayl /tarix da saqlanadi."
    )
    if result.drafts_left:
        caption += f"\n⚠️ Tasdiqlanmagan qoralama kirmadi: {result.drafts_left} ta"
    await _send_batch(bot, chat_id, result.filename, result.content, caption)


@router.message(Command("csv"))
@router.message(F.text == BTN_CSV)
async def cmd_csv(message: Message, bot: Bot, state: FSMContext, service: Service) -> None:
    await state.clear()
    await _do_export(bot, message.chat.id, message.from_user.id, service)


@router.message(Command("navbat"))
@router.message(F.text == BTN_QUEUE)
async def cmd_queue(message: Message, state: FSMContext, service: Service) -> None:
    await state.clear()
    items, drafts = await service.queue()
    if not items:
        note = f" Tasdiqlanmagan qoralama: {drafts} ta." if drafts else ""
        await message.answer("Navbat bo'sh." + note)
        return
    total = sum(i.total for i in items)
    lines = [f"<b>Navbatda {len(items)} ta hujjat, jami {fmt(total)} UZS</b>", ""]
    for item in items:
        lines.append(f"#{item.id} · {item.doc_date:%d.%m.%Y} · {escape(item.contractor)} · {fmt(item.total)}")
    if drafts:
        lines += ["", f"⚠️ Tasdiqlanmagan qoralama: {drafts} ta"]
    rows = [[_btn(f"#{i.id} ni ochish", "open", i.id)] for i in items[:15]]
    rows.append([InlineKeyboardButton(text=BTN_CSV, callback_data=DocCb(action="export", id=0).pack())])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("tarix"))
@router.message(F.text == BTN_HISTORY)
async def cmd_history(message: Message, state: FSMContext, service: Service) -> None:
    await state.clear()
    batches = await service.batches()
    if not batches:
        await message.answer("Hali CSV chiqarilmagan.")
        return
    rows = [
        [InlineKeyboardButton(
            text=f"⬇️ {b['created_at'].astimezone():%d.%m %H:%M} · {b['doc_count']} ta · {fmt(b['total_amount'])}",
            callback_data=BatchCb(id=b["id"]).pack(),
        )]
        for b in batches
    ]
    await message.answer("Oldingi CSV fayllar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(BatchCb.filter())
async def cb_batch(callback: CallbackQuery, callback_data: BatchCb, bot: Bot, service: Service) -> None:
    batch = await service.batch(callback_data.id)
    if not batch:
        await callback.answer("Fayl topilmadi", show_alert=True)
        return
    await _send_batch(
        bot, callback.from_user.id, batch["filename"], batch["content"],
        f"Qayta yuklab olindi: {batch['doc_count']} ta hujjat. 1C ga ikki marta kiritmang.",
    )
    await callback.answer()


@router.message(Command("kontragentlar"))
@router.message(F.text == BTN_CONTRACTORS)
async def cmd_contractors(message: Message, state: FSMContext, service: Service) -> None:
    await state.clear()
    rows = await service.contractors()
    lines = [f"<b>Kontragentlar: {len(rows)} ta</b>", ""]
    for row in rows:
        mark = " ⚠️" if not row["contract_verified"] else ""
        lines.append(f"• {escape(row['name'])}\n  {row['inn'] or '—'} · {escape(row['contract']) or '—'}{mark}")
    lines += ["", "⚠️ = shartnoma nomi tasdiqlanmagan. Tuzatish: /kontragent_qosh"]
    text = "\n".join(lines)
    for start in range(0, len(text), 4000):
        await message.answer(text[start : start + 4000])


@router.message(Command("kontragent_qosh"))
async def cmd_contractor_upsert(message: Message, state: FSMContext) -> None:
    await state.set_state(Input.contractor_upsert)
    await message.answer(
        "Yozing: <code>INN; 1C dagi nomi; Shartnoma</code>\n"
        "INN bor bo'lsa yangilanadi, yo'q bo'lsa qo'shiladi. To'xtatish: /bekor"
    )


@router.message(Command("kontragent_import"))
async def cmd_import_contractors(message: Message, state: FSMContext) -> None:
    await state.set_state(Input.import_contractors)
    await message.answer(
        "1C'dan chiqarilgan CSV yuboring. Ustunlar: <code>ИНН; Контрагент; Договор</code>. To'xtatish: /bekor"
    )


@router.message(Command("material_import"))
async def cmd_import_materials(message: Message, state: FSMContext) -> None:
    await state.set_state(Input.import_materials)
    await message.answer(
        "1C nomenklaturasini CSV qilib yuboring. Ustunlar: <code>Наименование; Ед.изм.</code>. To'xtatish: /bekor"
    )


@router.message(Command("materiallar"))
async def cmd_materials(message: Message, state: FSMContext, service: Service) -> None:
    await state.clear()
    rows, count = await service.materials()
    if not rows:
        await message.answer(
            "Katalog bo'sh. Tasdiqlangan har bir nom o'zi shu yerga tushadi. "
            "Aniq 1C nomlari uchun: /material_import"
        )
        return
    lines = [f"<b>Materiallar: {count} ta</b>", ""]
    lines += [f"• {escape(r['name'])}" + (f" ({escape(r['unit'])})" if r["unit"] else "") for r in rows]
    if count > len(rows):
        lines.append(f"… yana {count - len(rows)} ta")
    await message.answer("\n".join(lines)[:4000])


# ---------- Matn kiritish holatlari ----------


async def _current_doc(state: FSMContext) -> int:
    return (await state.get_data())["doc_id"]


async def _after_edit(message: Message, state: FSMContext, service: Service, doc_id: int) -> None:
    await state.clear()
    view = await service.view(doc_id)
    if view:
        await _show(message, view)


async def _guarded(message: Message, state: FSMContext, service: Service, doc_id: int, action) -> None:
    """Xato matnda holat saqlanadi (qayta yozish mumkin); hujjat o'zgargan bo'lsa, chiqiladi."""
    try:
        await action()
    except BadInput as exc:
        await message.answer(f"{exc}\nQayta yozing yoki /bekor")
        return
    except NotEditable:
        await state.clear()
        await message.answer("Bu hujjat endi qoralama emas, o'zgartirilmadi.")
        return
    await _after_edit(message, state, service, doc_id)


@router.message(StateFilter(Input.date), F.text)
async def input_date(message: Message, state: FSMContext, service: Service) -> None:
    doc_id = await _current_doc(state)

    async def action() -> None:
        try:
            value = parse_date(message.text, today_local())
        except ParseError as exc:
            raise BadInput("Sana shakli: <code>03.11.2026</code>") from exc
        await service.set_date(doc_id, value)

    await _guarded(message, state, service, doc_id, action)


@router.message(StateFilter(Input.number), F.text)
async def input_number(message: Message, state: FSMContext, service: Service) -> None:
    doc_id = await _current_doc(state)
    value = "" if message.text.strip() == "-" else message.text
    await _guarded(message, state, service, doc_id, lambda: service.set_number(doc_id, value))


@router.message(StateFilter(Input.comment), F.text)
async def input_comment(message: Message, state: FSMContext, service: Service) -> None:
    doc_id = await _current_doc(state)
    value = "" if message.text.strip() == "-" else message.text
    await _guarded(message, state, service, doc_id, lambda: service.set_comment(doc_id, value))


@router.message(StateFilter(Input.line), F.text)
async def input_line(message: Message, state: FSMContext, service: Service) -> None:
    data = await state.get_data()
    doc_id, position = data["doc_id"], data["position"]

    async def action() -> None:
        name, unit, qty, price = parse_line_input(message.text)
        await service.update_line(doc_id, position, name, unit, qty, price)

    await _guarded(message, state, service, doc_id, action)


@router.message(StateFilter(Input.new_line), F.text)
async def input_new_line(message: Message, state: FSMContext, service: Service) -> None:
    doc_id = await _current_doc(state)

    async def action() -> None:
        name, unit, qty, price = parse_line_input(message.text)
        await service.add_line(doc_id, name, unit, qty, price)

    await _guarded(message, state, service, doc_id, action)


@router.message(StateFilter(Input.contractor_search), F.text)
async def input_contractor_search(message: Message, state: FSMContext, service: Service) -> None:
    doc_id = await _current_doc(state)
    found = await service.search_contractors(message.text)
    await state.clear()
    text = "Topilganlar:" if found else "Topilmadi. Boshqacha yozing yoki yangisini qo'shing."
    await message.answer(text, reply_markup=contractor_keyboard(doc_id, found))


@router.message(StateFilter(Input.new_contractor), F.text)
async def input_new_contractor(message: Message, state: FSMContext, service: Service) -> None:
    doc_id = await _current_doc(state)

    async def action() -> None:
        inn, name, contract = parse_contractor_input(message.text)
        contractor_id, _ = await service.upsert_contractor(inn, name, contract)
        await service.set_contractor(doc_id, contractor_id)

    await _guarded(message, state, service, doc_id, action)


@router.message(StateFilter(Input.contractor_upsert), F.text)
async def input_contractor_upsert(message: Message, state: FSMContext, service: Service) -> None:
    try:
        inn, name, contract = parse_contractor_input(message.text)
    except BadInput as exc:
        await message.answer(f"{exc}\nQayta yozing yoki /bekor")
        return
    _, added = await service.upsert_contractor(inn, name, contract)
    await state.clear()
    await message.answer(f"{'Qo‘shildi' if added else 'Yangilandi'}: {escape(name)}")


async def _download(bot: Bot, file_id: str) -> bytes:
    buffer = await bot.download(file_id, destination=io.BytesIO())
    return buffer.getvalue()


@router.message(StateFilter(Input.import_contractors), F.document)
async def input_import_contractors(message: Message, bot: Bot, state: FSMContext, service: Service) -> None:
    stats = await service.import_contractors(await _download(bot, message.document.file_id))
    await state.clear()
    await message.answer(
        f"Kontragentlar: {stats.added} ta qo'shildi, {stats.updated} ta yangilandi, {stats.skipped} ta o'tkazildi."
    )


@router.message(StateFilter(Input.import_materials), F.document)
async def input_import_materials(message: Message, bot: Bot, state: FSMContext, service: Service) -> None:
    stats = await service.import_materials(await _download(bot, message.document.file_id))
    await state.clear()
    await message.answer(
        f"Materiallar: {stats.added} ta qo'shildi, {stats.updated} ta yangilandi, {stats.skipped} ta o'tkazildi."
    )


# ---------- Rasm qabul qilish ----------


async def _process_file(message: Message, bot: Bot, service: Service, file_id: str, size: int | None) -> None:
    status = await message.reply("⏳ Nakladnoy o'qilmoqda...")
    if size and size > MAX_FILE_BYTES:
        await status.edit_text("Fayl 20 MB dan katta, Telegram botga bermaydi. Kichikroq rasm yuboring.")
        return
    try:
        raw = await _download(bot, file_id)
        result = await service.ingest(raw, message.from_user.id, message.chat.id)
    except BadImage:
        await status.edit_text("Bu fayl rasm sifatida ochilmadi. JPG, PNG, HEIC yoki PDF yuboring.")
        return
    except Exception:
        log.exception("Rasmni qayta ishlash xatosi")
        await status.edit_text("Kutilmagan xato yuz berdi. Rasmni qayta yuboring; takrorlansa, logs\\bot.log ni qarang.")
        return
    view = await service.view(result.doc_id)
    text = render_card(view)
    if result.duplicate:
        text = f"♻️ <b>Bu rasm oldin yuborilgan.</b> Qayta o'qilmadi.\n\n{text}"
    await status.edit_text(text, reply_markup=card_keyboard(view))


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot, service: Service) -> None:
    photo = message.photo[-1]
    await _process_file(message, bot, service, photo.file_id, photo.file_size)


@router.message(F.document)
async def on_document(message: Message, bot: Bot, service: Service) -> None:
    doc = message.document
    mime = doc.mime_type or ""
    name = (doc.file_name or "").lower()
    is_image = mime.startswith("image/") or mime == "application/pdf" or name.endswith((".heic", ".heif", ".pdf"))
    if not is_image:
        await message.answer(
            "Bu fayl turini o'qiy olmayman. Nakladnoy rasmi yoki PDF yuboring.\n"
            "CSV ma'lumotnoma uchun avval /kontragent_import yoki /material_import."
        )
        return
    await _process_file(message, bot, service, doc.file_id, doc.file_size)


# ---------- Karta tugmalari ----------


@router.callback_query(DocCb.filter(F.action == "open"))
async def cb_open(callback: CallbackQuery, callback_data: DocCb, service: Service) -> None:
    view = await service.view(callback_data.id)
    if view and isinstance(callback.message, Message):
        await _show(callback.message, view)
    await callback.answer()


@router.callback_query(DocCb.filter(F.action == "confirm"))
async def cb_confirm(callback: CallbackQuery, callback_data: DocCb, service: Service) -> None:
    try:
        await service.confirm(callback_data.id, callback.from_user.id)
    except Blocked as exc:
        await callback.answer("Tasdiqlab bo'lmaydi:\n" + "\n".join(exc.reasons)[:180], show_alert=True)
        return
    except NotEditable:
        await _refresh(callback, service, callback_data.id, STALE_TEXT)
        return
    items, _ = await service.queue()
    await _refresh(callback, service, callback_data.id, f"Navbatga qo'shildi. Navbatda: {len(items)} ta")


@router.callback_query(DocCb.filter(F.action == "export"))
async def cb_export(callback: CallbackQuery, bot: Bot, service: Service, callback_data: DocCb) -> None:
    await _do_export(bot, callback.from_user.id, callback.from_user.id, service)
    if callback_data.id:
        await _refresh(callback, service, callback_data.id)
    else:
        await callback.answer()


async def _transition(callback: CallbackQuery, service: Service, doc_id: int, action, note: str) -> None:
    try:
        await action(doc_id)
    except NotEditable:
        note = STALE_TEXT
    await _refresh(callback, service, doc_id, note)


@router.callback_query(DocCb.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, callback_data: DocCb, service: Service) -> None:
    await _transition(callback, service, callback_data.id, service.cancel, "Bekor qilindi")


@router.callback_query(DocCb.filter(F.action == "restore"))
async def cb_restore(callback: CallbackQuery, callback_data: DocCb, service: Service) -> None:
    await _transition(callback, service, callback_data.id, service.restore, "Tiklandi")


@router.callback_query(DocCb.filter(F.action == "unconfirm"))
async def cb_unconfirm(callback: CallbackQuery, callback_data: DocCb, service: Service) -> None:
    await _transition(callback, service, callback_data.id, service.unconfirm, "Qoralamaga qaytdi")


@router.callback_query(DocCb.filter(F.action == "retry"))
async def cb_retry(callback: CallbackQuery, callback_data: DocCb, service: Service) -> None:
    await callback.answer("Qayta o'qilmoqda...")
    try:
        await service.retry(callback_data.id)
    except NotEditable:
        pass
    view = await service.view(callback_data.id)
    if view and isinstance(callback.message, Message):
        await _show(callback.message, view, edit=True)


@router.callback_query(DocCb.filter(F.action == "back"))
async def cb_back(callback: CallbackQuery, callback_data: DocCb, state: FSMContext, service: Service) -> None:
    await state.clear()
    await _refresh(callback, service, callback_data.id)


@router.callback_query(DocCb.filter(F.action == "edit"))
async def cb_edit(callback: CallbackQuery, callback_data: DocCb, service: Service) -> None:
    view = await service.view(callback_data.id)
    if view is None or view.status != "draft":
        await _refresh(callback, service, callback_data.id, STALE_TEXT)
        return
    await callback.message.edit_reply_markup(reply_markup=edit_keyboard(view))
    await callback.answer()


async def _ask(callback: CallbackQuery, state: FSMContext, target: State, doc_id: int, text: str, **extra) -> None:
    await state.set_state(target)
    await state.update_data(doc_id=doc_id, **extra)
    await callback.message.answer(f"{text}\nTo'xtatish: /bekor")
    await callback.answer()


@router.callback_query(DocCb.filter(F.action == "f_date"))
async def cb_field_date(callback: CallbackQuery, callback_data: DocCb, state: FSMContext) -> None:
    await _ask(callback, state, Input.date, callback_data.id, "Sanani yozing: <code>03.11.2026</code>")


@router.callback_query(DocCb.filter(F.action == "f_number"))
async def cb_field_number(callback: CallbackQuery, callback_data: DocCb, state: FSMContext) -> None:
    await _ask(callback, state, Input.number, callback_data.id, "Hujjat raqamini yozing (o'chirish uchun <code>-</code>):")


@router.callback_query(DocCb.filter(F.action == "f_comment"))
async def cb_field_comment(callback: CallbackQuery, callback_data: DocCb, state: FSMContext) -> None:
    await _ask(callback, state, Input.comment, callback_data.id, "Izohni yozing (o'chirish uchun <code>-</code>):")


@router.callback_query(DocCb.filter(F.action == "line"))
async def cb_line(callback: CallbackQuery, callback_data: DocCb, state: FSMContext, service: Service) -> None:
    view = await service.view(callback_data.id)
    line = next((ln for ln in (view.lines if view else []) if ln.position == callback_data.arg), None)
    if line is None or view.status != "draft":
        await _refresh(callback, service, callback_data.id, STALE_TEXT)
        return
    current = f"{line.name}; {line.unit}; {fmt(line.quantity).replace(' ', '')}; {fmt(line.price).replace(' ', '')}"
    await state.set_state(Input.line)
    await state.update_data(doc_id=view.id, position=line.position)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🗑 Qatorni o'chirish", "del_line", view.id, line.position), _btn("⬅️ Orqaga", "back", view.id)],
    ])
    await callback.message.answer(
        f"{line.position}-qatorni yangi ko'rinishda yuboring:\n<code>{escape(current)}</code>\n"
        "Shakl: Nomi; birlik; soni; narxi. Summa o'zi hisoblanadi.",
        reply_markup=markup,
    )
    await callback.answer()


@router.callback_query(DocCb.filter(F.action == "del_line"))
async def cb_delete_line(callback: CallbackQuery, callback_data: DocCb, state: FSMContext, service: Service) -> None:
    await state.clear()
    try:
        await service.delete_line(callback_data.id, callback_data.arg)
    except NotEditable:
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    await _refresh(callback, service, callback_data.id, "Qator o'chirildi")


@router.callback_query(DocCb.filter(F.action == "add_line"))
async def cb_add_line(callback: CallbackQuery, callback_data: DocCb, state: FSMContext) -> None:
    await _ask(
        callback, state, Input.new_line, callback_data.id,
        "Yangi qator: <code>Nomi; birlik; soni; narxi</code>",
    )


@router.callback_query(DocCb.filter(F.action == "contractor"))
async def cb_contractor(callback: CallbackQuery, callback_data: DocCb, service: Service) -> None:
    view = await service.view(callback_data.id)
    if view is None or view.status != "draft":
        await _refresh(callback, service, callback_data.id, STALE_TEXT)
        return
    choices = view.candidates or await service.all_contractor_choices()
    await callback.message.edit_reply_markup(reply_markup=contractor_keyboard(view.id, choices))
    await callback.answer("Kontragentni tanlang")


@router.callback_query(DocCb.filter(F.action == "pick"))
async def cb_pick(callback: CallbackQuery, callback_data: DocCb, service: Service) -> None:
    try:
        await service.set_contractor(callback_data.id, callback_data.arg)
    except NotEditable:
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    await _refresh(callback, service, callback_data.id, "Kontragent tanlandi")


@router.callback_query(DocCb.filter(F.action == "c_search"))
async def cb_contractor_search(callback: CallbackQuery, callback_data: DocCb, state: FSMContext) -> None:
    await _ask(callback, state, Input.contractor_search, callback_data.id, "Kontragent nomi yoki INN ni yozing:")


@router.callback_query(DocCb.filter(F.action == "c_new"))
async def cb_contractor_new(callback: CallbackQuery, callback_data: DocCb, state: FSMContext) -> None:
    await _ask(
        callback, state, Input.new_contractor, callback_data.id,
        "Yozing: <code>INN; 1C dagi aniq nomi; Shartnoma</code>",
    )


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Nakladnoy rasmini yuboring yoki /yordam ni bosing.", reply_markup=main_keyboard())


def build_dispatcher(settings: Settings, service: Service) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["service"] = service
    access = AccessMiddleware(settings.allowed_ids)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(router)
    return dp
