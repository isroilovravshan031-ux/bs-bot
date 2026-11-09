"""Groq zaxira provayderi va provayderlar zanjiri.

Groq OpenAI'ga mos API beradi. Rasm o'qiydigan ochiq model: qwen/qwen3.8-27b.
Sinovda raqamlarni to'g'ri, lekin qo'lyozma kirill nomlarini Gemini'dan yomonroq o'qidi,
shuning uchun u bilan o'qilgan har bir kartada ogohlantirish chiqadi.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import date

import httpx

from .gemini import SYSTEM_PROMPT, ExtractionError, ExtractionResult, Extractor, parse_response_text, user_prompt
from .imaging import PDF_MIME

log = logging.getLogger(__name__)

API_URL = "https://api.groq.com/openai/v1/chat/completions"
FALLBACK_NOTE = "Zaxira model (Groq) o'qidi: raqamlar odatda to'g'ri, nomlarni rasm bilan solishtiring"
MAX_RETRY_WAIT = 30.0

_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_NUMBER = {"type": ["number", "null"]}

# Strict rejim hamma maydonni required va additionalProperties=false talab qiladi.
# ExtractedInvoice bilan bir xil bo'lishi test_groq.py da tekshiriladi.
RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_invoice", "supplier_name", "supplier_inn", "receiver_name",
        "doc_number", "doc_date", "lines", "total", "uncertainties",
    ],
    "properties": {
        "is_invoice": {"type": "boolean"},
        "supplier_name": _NULLABLE_STRING,
        "supplier_inn": _NULLABLE_STRING,
        "receiver_name": _NULLABLE_STRING,
        "doc_number": _NULLABLE_STRING,
        "doc_date": _NULLABLE_STRING,
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "unit", "quantity", "price", "amount", "confidence"],
                "properties": {
                    "name": {"type": "string"},
                    "unit": _NULLABLE_STRING,
                    "quantity": _NULLABLE_NUMBER,
                    "price": _NULLABLE_NUMBER,
                    "amount": _NULLABLE_NUMBER,
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
        "total": _NULLABLE_NUMBER,
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
}


class _RateLimited(ExtractionError):
    def __init__(self, wait: float):
        super().__init__("quota", "Groq limiti tugadi (429)")
        self.wait = wait


def _retry_after(response: httpx.Response) -> float:
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return 0.0


def _raise_for_response(response: httpx.Response) -> None:
    code = response.status_code
    if code == 200:
        return
    if code in (401, 403):
        raise ExtractionError("auth", f"Groq kaliti rad etildi ({code})")
    if code == 429:
        raise _RateLimited(_retry_after(response))
    if code == 404:
        raise ExtractionError("model", "Groq modeli topilmadi (404)")
    if code >= 500 or code == 498:  # 498: Groq sig'imi vaqtincha yetmayapti
        raise ExtractionError("network", f"Groq serveri xatosi ({code})")
    if code == 400 and "json_validate_failed" in response.text:
        raise ExtractionError("bad_output", "Groq JSON tuza olmadi")
    raise ExtractionError("model", f"Groq so'rovni rad etdi ({code})")


class GroqExtractor:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: int = 60,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delays: tuple[float, ...] = (2.0, 6.0),
    ):
        self._key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._transport = transport
        self._delays = retry_delays

    def _payload(self, data: bytes, mime: str, prompt: str) -> dict:
        encoded = base64.b64encode(data).decode("ascii")
        return {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                    ],
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "invoice", "strict": True, "schema": RESPONSE_SCHEMA},
            },
        }

    async def _call(self, client: httpx.AsyncClient, payload: dict) -> ExtractionResult:
        started = time.monotonic()
        try:
            response = await client.post(
                API_URL, json=payload, headers={"Authorization": f"Bearer {self._key}"}
            )
        except httpx.HTTPError as exc:
            raise ExtractionError("network", f"Groq bilan ulanish xatosi: {type(exc).__name__}") from exc
        _raise_for_response(response)
        try:
            body = response.json()
            text = body["choices"][0]["message"].get("content")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ExtractionError("bad_output", "Groq javobi tushunarsiz") from exc
        usage = body.get("usage") or {}
        return ExtractionResult(
            invoice=parse_response_text(text),
            model=self._model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            duration_ms=int((time.monotonic() - started) * 1000),
            note=FALLBACK_NOTE,
        )

    async def extract(
        self, data: bytes, mime: str, today: date, recheck: list[str] | None = None
    ) -> ExtractionResult:
        if mime == PDF_MIME:
            raise ExtractionError("model", "Groq PDF o'qimaydi, faqat rasm")
        payload = self._payload(data, mime, user_prompt(today, recheck))
        last: ExtractionError | None = None
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            for attempt in range(len(self._delays) + 1):
                try:
                    return await self._call(client, payload)
                except ExtractionError as exc:
                    last = exc
                    log.warning("Groq, urinish %d: %s", attempt + 1, exc)
                    if exc.kind in ("auth", "model") or attempt >= len(self._delays):
                        break
                    # Bepul tarifda daqiqalik limit tez-tez to'ladi; server aytgan vaqtni kutamiz.
                    wait = getattr(exc, "wait", 0.0) or self._delays[attempt]
                    if wait > MAX_RETRY_WAIT:
                        break
                    await asyncio.sleep(wait)
        assert last is not None
        raise last


class ChainExtractor:
    """Provayderlarni navbat bilan sinaydi. Hammasi yiqilsa, birinchisining xatosi ko'rsatiladi:
    u asosiy provayder va foydalanuvchiga eng foydali sabab shu."""

    def __init__(self, extractors: list[Extractor], budget_seconds: float = 150):
        self._extractors = extractors
        self._budget = budget_seconds

    async def extract(
        self, data: bytes, mime: str, today: date, recheck: list[str] | None = None
    ) -> ExtractionResult:
        first: ExtractionError | None = None
        for extractor in self._extractors:
            try:
                # Asosiy provayder uzoq osilib qolsa ham zaxiraga navbat kelsin.
                return await asyncio.wait_for(extractor.extract(data, mime, today, recheck), self._budget)
            except ExtractionError as exc:
                log.warning("%s ishlamadi: %s", type(extractor).__name__, exc)
                first = first or exc
            except TimeoutError:
                log.warning("%s vaqtga sig'madi", type(extractor).__name__)
                first = first or ExtractionError("network", "AI vaqtida javob bermadi")
        assert first is not None
        raise first
