"""Testlar haqiqiy PostgreSQL'da ishlaydi (nakladnoy_test bazasi), har test toza sxemadan boshlanadi."""
from __future__ import annotations

import io
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nakladnoy_bot.config import ROOT, Settings  # noqa: E402
from nakladnoy_bot.db import create_pool, migrate  # noqa: E402
from nakladnoy_bot.exporter import load_profile  # noqa: E402
from nakladnoy_bot.gemini import ExtractionResult  # noqa: E402
from nakladnoy_bot.models import ExtractedInvoice  # noqa: E402
from nakladnoy_bot.service import Service  # noqa: E402

TODAY = date(2026, 11, 4)

# O'ylab topilgan kontragentlar (repo ochiq, haqiqiylari unda yo'q). Ikkitasining shartnomasi tasdiqlanmagan.
TEST_CONTRACTORS = [
    ("300000001", "Тошматов Азиз  (снабженец)", "Договор № 1 от 01.01.2026г.", True),
    ("300000002", "ООО Ҳамидов Шерзод (материал)", "Договор № 2 от 01.01.2026г.", True),
    ("300000003", "TEMIRSAVDO MCHJ (материал)", "Договор № 3 от 01.01.2026г.", True),
    ("300000004", "(BS) OQTOSH INDUSTRIES  MCHJ QK  (Охак)", "Договор № 4 от 01.01.2026г.", False),
    ("300000005", '"AVTOTRAK" MCHJ', "Договор № 5 от 01.01.2026г.", False),
    ("300000006", "(NANO) BOROLIT (Мел)", "Договор № 6 от 01.01.2026г.", True),
    ("300000007", "(BS) QUM TASHUVCHI MCHJ (Кум)", "Договор № 7 от 01.01.2026г.", True),
    ("300000008", "(BS) Nurli Invest Production (Пента)", "Договор № 8 от 01.01.2026г.", True),
    ("300000009", "(NANO) ЧП  YANGI DAVR INVEST(Қоғоз қоп)", "Договор № 9 от 01.01.2026г.", True),
    ("300000010", '(NANO) ЧП "KIMYO SAVDO" (Химикат)', "Договор № 10 от 01.01.2026г.", True),
]

# Nakladnoy namunasi: qog'ozdagi qiymatlar, kontragent nomi o'zgartirilgan.
SAMPLE = {
    "is_invoice": True,
    "supplier_name": "Тошматов Азиз",
    "supplier_inn": None,
    "receiver_name": "Марказий омбор",
    "doc_number": None,
    "doc_date": "2026-11-03",
    "lines": [
        {"name": "А4 лист", "unit": "блок", "quantity": 2, "price": 250000, "amount": 500000, "confidence": "high"},
        {"name": "Офис дафтари", "unit": "шт", "quantity": 4, "price": 35000, "amount": 140000, "confidence": "high"},
        {"name": "Офис папка", "unit": "шт", "quantity": 2, "price": 40000, "amount": 80000, "confidence": "high"},
    ],
    "total": 720000,
    "uncertainties": [],
}


def invoice(**changes) -> ExtractedInvoice:
    return ExtractedInvoice.model_validate({**SAMPLE, **changes})


class FakeExtractor:
    """Navbat bilan javob qaytaradi; Exception bo'lsa, ko'taradi."""

    def __init__(self, *responses):
        self.responses = list(responses) or [invoice()]
        self.calls: list[list[str] | None] = []

    async def extract(self, data, mime, today, recheck=None):
        self.calls.append(recheck)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return ExtractionResult(item, "fake-model", 100, 50, 5)


def make_image(label: str = "nakladnoy") -> bytes:
    img = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(img).text((10, 10), label, fill="black")
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


def _test_dsn() -> str:
    parts = urlsplit(Settings().database_url)
    return urlunsplit(parts._replace(path="/nakladnoy_test"))


@pytest_asyncio.fixture
async def pool():
    pool = await create_pool(_test_dsn())
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(pool)
    await pool.executemany(
        "INSERT INTO contractors (inn, name, contract, contract_verified) VALUES ($1, $2, $3, $4)",
        TEST_CONTRACTORS,
    )
    yield pool
    await pool.close()


@pytest.fixture
def profile():
    return load_profile(ROOT / "config" / "export_profile.toml")


@pytest_asyncio.fixture
async def make_service(pool, profile, tmp_path):
    def build(*responses) -> Service:
        return Service(pool, FakeExtractor(*responses), profile, tmp_path, today_fn=lambda: TODAY)

    return build
