"""Ishga tushirish: python -m nakladnoy_bot run | doctor | read <rasm>"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import ROOT, Settings, today_local


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(ROOT / "logs" / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # so'rov URL'lari logga tushmasin


def _extractor(settings: Settings, provider: str = "auto"):
    from .gemini import GeminiExtractor
    from .groq_vision import ChainExtractor, GroqExtractor

    chain = []
    if provider in ("auto", "gemini") and settings.gemini_api_key:
        chain.append(GeminiExtractor(
            settings.gemini_api_key,
            settings.gemini_model,
            settings.gemini_fallback_model,
            settings.gemini_thinking_level,
            settings.gemini_timeout_seconds,
        ))
    if provider in ("auto", "groq") and settings.groq_api_key:
        chain.append(GroqExtractor(settings.groq_api_key, settings.groq_model))
    if not chain:
        raise SystemExit(f".env da {provider} uchun API kalit yo'q")
    return chain[0] if len(chain) == 1 else ChainExtractor(chain)


async def run_bot(settings: Settings) -> int:
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.exceptions import TelegramUnauthorizedError
    from aiogram.types import BotCommand

    from .db import INSTANCE_LOCK, create_pool, migrate
    from .exporter import load_profile
    from .handlers import build_dispatcher
    from .service import Service

    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token),
                              ("GEMINI_API_KEY yoki GROQ_API_KEY", settings.gemini_api_key or settings.groq_api_key),
                              ("DATABASE_URL", settings.database_url)) if not v]
    if missing:
        logging.error(".env da yetishmayapti: %s", ", ".join(missing))
        return 2
    if not settings.allowed_ids:
        logging.warning("ALLOWED_USER_IDS bo'sh: bot hammani rad etadi va ularga ID sini ko'rsatadi.")

    profile = load_profile(settings.export_profile_path)
    pool = await create_pool(settings.database_url)
    lock_conn = await pool.acquire()
    try:
        # Ikki nusxa bir token bilan polling qilsa, Telegram Conflict xatosi beradi.
        # Railway yangi versiyani eskisi to'xtamasdan ishga tushiradi: yiqilish o'rniga navbat kutiladi.
        waited = False
        while not await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", INSTANCE_LOCK):
            if not waited:
                logging.warning("Botning boshqa nusxasi shu bazada ishlayapti, u to'xtashi kutilmoqda...")
                waited = True
            await asyncio.sleep(5)
        await migrate(pool)
        service = Service(pool, _extractor(settings), profile, settings.data_dir, settings.max_concurrent_reads)
        stuck = await service.recover_stuck()
        if stuck:
            logging.warning("O'qish paytida to'xtagan hujjatlar qayta o'qishga qaytarildi: %d", stuck)
        bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await bot.get_me()
        except TelegramUnauthorizedError:
            logging.error("TELEGRAM_BOT_TOKEN noto'g'ri: Telegram rad etdi.")
            await bot.session.close()
            return 2
        await bot.set_my_commands([
            BotCommand(command="csv", description="Navbatdagini CSV qilish"),
            BotCommand(command="navbat", description="Tasdiqlangan hujjatlar"),
            BotCommand(command="tarix", description="Oldingi CSV fayllar"),
            BotCommand(command="kontragentlar", description="Kontragentlar ro'yxati"),
            BotCommand(command="kontragent_qosh", description="Kontragent qo'shish yoki yangilash"),
            BotCommand(command="kontragent_import", description="Kontragentlarni CSV'dan yuklash"),
            BotCommand(command="materiallar", description="Material katalogi"),
            BotCommand(command="material_import", description="Nomenklaturani CSV'dan yuklash"),
            BotCommand(command="yordam", description="Yo'riqnoma"),
            BotCommand(command="bekor", description="Joriy kiritishni to'xtatish"),
        ])
        dp = build_dispatcher(settings, service)
        logging.info(
            "Bot ishga tushdi. Model: %s, Groq zaxirasi: %s",
            settings.gemini_model, settings.groq_model if settings.groq_api_key else "o'chiq",
        )
        try:
            await dp.start_polling(bot)
        finally:
            await bot.session.close()
    finally:
        await pool.release(lock_conn)
        await pool.close()
    return 0


async def doctor(settings: Settings) -> int:
    """Har bir qismni tekshiradi va nima yetishmayotganini aniq aytadi."""
    from .db import create_pool, migrate
    from .exporter import ProfileError, load_profile

    failed = 0

    def report(ok: bool, name: str, detail: str = "") -> None:
        nonlocal failed
        failed += 0 if ok else 1
        print(f"[{'OK' if ok else 'XATO'}] {name}" + (f": {detail}" if detail else ""))

    try:
        pool = await create_pool(settings.database_url)
        applied = await migrate(pool)
        count = await pool.fetchval("SELECT count(*) FROM contractors")
        report(True, "PostgreSQL", f"{count} ta kontragent, yangi migratsiya: {len(applied)}")
        await pool.close()
    except Exception as exc:  # noqa: BLE001
        report(False, "PostgreSQL", f"{type(exc).__name__}. scripts\\pg-start.ps1 ni ishga tushiring")

    try:
        profile = load_profile(settings.export_profile_path)
        report(True, "Eksport profili", f"{len(profile.columns)} ustun, {profile.encoding}")
    except ProfileError as exc:
        report(False, "Eksport profili", str(exc))

    report(bool(settings.allowed_ids), "ALLOWED_USER_IDS", str(len(settings.allowed_ids)) + " ta foydalanuvchi")

    if settings.telegram_bot_token:
        from aiogram import Bot

        bot = Bot(settings.telegram_bot_token)
        try:
            me = await bot.get_me()
            report(True, "Telegram", f"@{me.username}")
        except Exception as exc:  # noqa: BLE001
            report(False, "Telegram", type(exc).__name__)
        finally:
            await bot.session.close()
    else:
        report(False, "Telegram", ".env da TELEGRAM_BOT_TOKEN yo'q")

    if settings.gemini_api_key:
        from google import genai

        try:
            client = genai.Client(api_key=settings.gemini_api_key)
            info = await client.aio.models.get(model=settings.gemini_model)
            report(True, "Gemini", info.name or settings.gemini_model)
        except Exception as exc:  # noqa: BLE001
            report(False, "Gemini", f"{type(exc).__name__}: {str(exc)[:150]}")
    else:
        report(False, "Gemini", ".env da GEMINI_API_KEY yo'q")

    if settings.groq_api_key:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    f"https://api.groq.com/openai/v1/models/{settings.groq_model}",
                    headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                )
            report(response.status_code == 200, "Groq zaxirasi", f"{settings.groq_model} ({response.status_code})")
        except httpx.HTTPError as exc:
            report(False, "Groq zaxirasi", type(exc).__name__)
    else:
        print("[--] Groq zaxirasi: o'chiq")
    return 1 if failed else 0


async def read_image(settings: Settings, path: Path, provider: str) -> int:
    """Telegram'siz sinov: rasmni haqiqiy AI orqali o'qiydi va natijani chiqaradi."""
    from .imaging import prepare
    from .validation import arithmetic_errors

    prepared = prepare(path.read_bytes())
    result = await _extractor(settings, provider).extract(prepared.data, prepared.mime, today_local())
    print(json.dumps(result.invoice.model_dump(mode="json"), ensure_ascii=False, indent=2))
    print(f"model={result.model} kirish={result.input_tokens} chiqish={result.output_tokens} {result.duration_ms} ms")
    errors = arithmetic_errors(result.invoice)
    print("Hisob-kitob: to'g'ri" if not errors else f"Hisob-kitob xatolari: {errors}")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="nakladnoy_bot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Botni ishga tushirish")
    sub.add_parser("doctor", help="Sozlamalarni tekshirish")
    read = sub.add_parser("read", help="Rasmni AI bilan sinab o'qish")
    read.add_argument("image", type=Path)
    read.add_argument("--provider", choices=["auto", "gemini", "groq"], default="auto")
    args = parser.parse_args()

    settings = Settings()
    if args.command == "run":
        setup_logging()
        return asyncio.run(run_bot(settings))
    if args.command == "doctor":
        return asyncio.run(doctor(settings))
    return asyncio.run(read_image(settings, args.image, args.provider))


if __name__ == "__main__":
    raise SystemExit(main())
