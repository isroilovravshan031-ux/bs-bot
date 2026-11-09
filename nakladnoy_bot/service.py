"""Biznes mantiq: rasmni o'qish, tahrir, tasdiqlash, eksport. Telegram'ni bilmaydi."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable

import asyncpg

from .config import now_local, today_local
from .exporter import ExportDoc, ExportLine, Profile, build_csv
from .gemini import ExtractionError, ExtractionResult, Extractor
from .imaging import Prepared, prepare
from .matching import Match, Named, match_contractor, match_material, rank_parties
from .models import DocView, ExtractedInvoice, Line, document_key
from .normalize import norm_item, norm_party
from .numbers import ParseError, parse_iso_date, parse_number, to_decimal
from .validation import arithmetic_errors, check_document

log = logging.getLogger(__name__)


class NotEditable(Exception):
    """Hujjat bu holatda o'zgartirilmaydi."""


class Blocked(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class BadInput(ValueError):
    """Foydalanuvchi matni noto'g'ri; xabar to'g'ridan-to'g'ri ko'rsatiladi."""


@dataclass
class IngestResult:
    doc_id: int
    duplicate: bool = False


@dataclass
class ExportResult:
    batch_id: int
    filename: str
    content: bytes
    doc_count: int
    total: Decimal
    drafts_left: int


@dataclass
class QueueItem:
    id: int
    doc_date: date | None
    contractor: str
    total: Decimal


@dataclass
class ImportStats:
    added: int = 0
    updated: int = 0
    skipped: int = 0


def parse_line_input(text: str) -> tuple[str, str, Decimal, Decimal]:
    """'Офис папка; шт; 2; 40000' ni o'qiydi."""
    parts = [p.strip() for p in (text or "").split(";")]
    if len(parts) != 4 or not parts[0]:
        raise BadInput("Shakl: <code>Nomi; birlik; soni; narxi</code>")
    try:
        qty, price = parse_number(parts[2]), parse_number(parts[3])
    except ParseError as exc:
        raise BadInput("Soni va narxi son bo'lishi kerak") from exc
    if qty <= 0:
        raise BadInput("Soni noldan katta bo'lishi kerak")
    return parts[0], parts[1], qty, price


def parse_contractor_input(text: str) -> tuple[str, str, str]:
    """'300000001; Nomi; Договор ...' ni o'qiydi."""
    parts = [p.strip() for p in (text or "").split(";")]
    if len(parts) < 2 or len(parts) > 3 or not parts[1]:
        raise BadInput("Shakl: <code>INN; 1C dagi nomi; Shartnoma</code>")
    inn = parts[0]
    if inn and not re.fullmatch(r"\d{9}|\d{14}", inn):
        raise BadInput("INN 9 xonali (PINFL 14 xonali) bo'lishi kerak")
    return inn, parts[1], parts[2] if len(parts) == 3 else ""


def read_table(raw: bytes) -> list[list[str]]:
    """1C yoki Excel'dan chiqqan CSV: kodirovka va ajratkichni o'zi aniqlaydi."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    first = text.split("\n", 1)[0]
    delimiter = max(";,\t", key=first.count)
    rows = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    return [[c.strip() for c in row] for row in rows if any(c.strip() for c in row)]


def _column_index(header: list[str], names: tuple[str, ...]) -> int | None:
    lowered = [h.lower() for h in header]
    for i, h in enumerate(lowered):
        if any(n in h for n in names):
            return i
    return None


class Service:
    def __init__(
        self,
        pool: asyncpg.Pool,
        extractor: Extractor,
        profile: Profile,
        data_dir: Path,
        max_concurrent: int = 2,
        today_fn: Callable[[], date] = today_local,
    ):
        self.pool = pool
        self.extractor = extractor
        self.profile = profile
        self._limit = asyncio.Semaphore(max_concurrent)
        self._today = today_fn

    # ---------- Rasmni qabul qilish ----------

    async def ingest(self, raw: bytes, user_id: int, chat_id: int) -> IngestResult:
        prepared: Prepared = await asyncio.to_thread(prepare, raw)
        existing = await self._find_by_hash(prepared.sha256)
        if existing:
            return IngestResult(existing, duplicate=True)
        try:
            async with self.pool.acquire() as conn, conn.transaction():
                doc_id = await conn.fetchval(
                    "INSERT INTO documents (created_by, chat_id, image_path, image_hash, image_mime)"
                    " VALUES ($1, $2, '', $3, $4) RETURNING id",
                    user_id, chat_id, prepared.sha256, prepared.mime,
                )
                await conn.execute(
                    "INSERT INTO document_images (document_id, data) VALUES ($1, $2)", doc_id, prepared.data
                )
        except asyncpg.UniqueViolationError:  # bir vaqtda ikki marta yuborilgan
            existing = await self._find_by_hash(prepared.sha256)
            return IngestResult(existing, duplicate=True)
        await self._read(doc_id, prepared.data, prepared.mime)
        return IngestResult(doc_id)

    async def _find_by_hash(self, sha256: str) -> int | None:
        return await self.pool.fetchval(
            "SELECT id FROM documents WHERE image_hash = $1 AND status NOT IN ('cancelled', 'failed')",
            sha256,
        )

    async def retry(self, doc_id: int) -> None:
        row = await self.pool.fetchrow(
            "UPDATE documents SET status = 'processing', error = NULL, updated_at = now()"
            " WHERE id = $1 AND status = 'failed' RETURNING image_path, image_mime",
            doc_id,
        )
        if not row:
            raise NotEditable
        data = await self.pool.fetchval("SELECT data FROM document_images WHERE document_id = $1", doc_id)
        if data is None and row["image_path"]:
            # 003-migratsiyadan oldingi hujjatlar: rasm diskda.
            try:
                data = await asyncio.to_thread(Path(row["image_path"]).read_bytes)
            except OSError:
                data = None
        if data is None:
            await self._fail(doc_id, "no_image: Asl rasm topilmadi, uni qayta yuboring")
            return
        await self._read(doc_id, bytes(data), row["image_mime"])

    async def _read(self, doc_id: int, data: bytes, mime: str) -> None:
        try:
            async with self._limit:
                result = await self._extract_checked(doc_id, data, mime)
        except ExtractionError as exc:
            await self._fail(doc_id, f"{exc.kind}: {exc}")
            return
        if not result.invoice.is_invoice:
            await self._fail(doc_id, "not_invoice: Rasm nakladnoyga o'xshamaydi")
            return
        try:
            await self._apply(doc_id, result)
        except (asyncpg.PostgresError, ValueError) as exc:
            # Masalan, AI juda katta son qaytardi. Hujjat 'processing' da osilib qolmasin.
            log.exception("AI natijasini saqlab bo'lmadi, hujjat %s", doc_id)
            await self._fail(doc_id, f"bad_output: {type(exc).__name__}")

    async def recover_stuck(self) -> int:
        """Bot o'qish paytida to'xtagan bo'lsa, hujjatlar 'Qayta o'qish' tugmasiga qaytadi."""
        rows = await self.pool.fetch(
            "UPDATE documents SET status = 'failed', error = 'network: Bot o''qish paytida to''xtagan',"
            " updated_at = now() WHERE status = 'processing' RETURNING id"
        )
        return len(rows)

    async def _extract_checked(self, doc_id: int, data: bytes, mime: str) -> ExtractionResult:
        """Hisob-kitob mos kelmasa, AI'dan aynan shu kataklarni qayta o'qishni bir marta so'raydi."""
        first = await self._logged_extract(doc_id, data, mime, None)
        errors = arithmetic_errors(first.invoice) if first.invoice.is_invoice else []
        if not errors:
            return first
        try:
            second = await self._logged_extract(doc_id, data, mime, errors)
        except ExtractionError:
            return first  # birinchi natija bor; ogohlantirish kartada ko'rinadi
        return second if len(arithmetic_errors(second.invoice)) < len(errors) else first

    async def _logged_extract(
        self, doc_id: int, data: bytes, mime: str, recheck: list[str] | None
    ) -> ExtractionResult:
        try:
            result = await self.extractor.extract(data, mime, self._today(), recheck)
        except ExtractionError as exc:
            await self.pool.execute(
                "INSERT INTO ai_calls (document_id, model, duration_ms, ok, error)"
                " VALUES ($1, '', 0, false, $2)",
                doc_id, f"{exc.kind}: {exc}",
            )
            raise
        await self.pool.execute(
            "INSERT INTO ai_calls (document_id, model, input_tokens, output_tokens, duration_ms, ok)"
            " VALUES ($1, $2, $3, $4, $5, true)",
            doc_id, result.model, result.input_tokens, result.output_tokens, result.duration_ms,
        )
        return result

    async def _fail(self, doc_id: int, error: str) -> None:
        await self.pool.execute(
            "UPDATE documents SET status = 'failed', error = $2, updated_at = now() WHERE id = $1",
            doc_id, error,
        )

    async def _apply(self, doc_id: int, result: ExtractionResult) -> None:
        inv: ExtractedInvoice = result.invoice
        async with self.pool.acquire() as conn, conn.transaction():
            match = await self._match_contractor(conn, inv.supplier_name, inv.supplier_inn)
            materials, m_aliases, units = await self._material_directory(conn)
            await conn.execute("DELETE FROM document_lines WHERE document_id = $1", doc_id)
            for pos, ext in enumerate(inv.lines, 1):
                qty, price = to_decimal(ext.quantity), to_decimal(ext.price)
                amount = to_decimal(ext.amount)
                confidence = ext.confidence.value
                if amount is None and qty is not None and price is not None:
                    amount, confidence = qty * price, "low"  # qog'ozda o'qilmadi, hisoblandi
                raw_name = " ".join(ext.name.split())  # CSV'da qator ko'chishi bo'lmasin
                name, unit, note = raw_name, " ".join((ext.unit or "").split()), ""
                hit = match_material(raw_name, materials, m_aliases, units)
                if hit:
                    name, unit = hit.name, unit or hit.unit
                    if hit.method == "fuzzy":
                        note = f"«{raw_name}» katalogdagi «{hit.name}» deb olindi"
                await conn.execute(
                    "INSERT INTO document_lines (document_id, position, raw_name, name, unit,"
                    " quantity, price, amount, confidence, match_note)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                    doc_id, pos, raw_name, name, unit, qty, price, amount, confidence, note,
                )
            await conn.execute(
                "UPDATE documents SET status = 'draft', error = NULL, ai_model = $2, raw_ai = $3,"
                " ai_notes = $4, supplier_raw = $5, supplier_inn = $6, receiver_raw = $7, doc_number = $8,"
                " doc_date = $9, contractor_id = $10, match_method = $11, match_score = $12,"
                " candidates = $13, paper_total = $14, updated_at = now() WHERE id = $1",
                doc_id, result.model, inv.model_dump(mode="json"),
                ([result.note] if result.note else []) + inv.uncertainties,
                inv.supplier_name, inv.supplier_inn, inv.receiver_name, (inv.doc_number or "").strip(),
                parse_iso_date(inv.doc_date), match.id, match.method, match.score,
                match.candidates, to_decimal(inv.total),
            )

    @staticmethod
    async def _contractor_directory(conn) -> tuple[list[Named], dict[str, int]]:
        rows = await conn.fetch("SELECT id, name, coalesce(inn, '') AS inn FROM contractors WHERE active")
        aliases = await conn.fetch("SELECT alias_norm, contractor_id FROM contractor_aliases")
        return (
            [Named(r["id"], r["name"], r["inn"]) for r in rows],
            {a["alias_norm"]: a["contractor_id"] for a in aliases},
        )

    async def _match_contractor(self, conn, name: str | None, inn: str | None) -> Match:
        items, aliases = await self._contractor_directory(conn)
        return match_contractor(name, inn, items, aliases)

    @staticmethod
    async def _material_directory(conn):
        rows = await conn.fetch("SELECT id, name, unit FROM materials WHERE active")
        aliases = await conn.fetch("SELECT alias_norm, material_id FROM material_aliases")
        return (
            [Named(r["id"], r["name"]) for r in rows],
            {a["alias_norm"]: a["material_id"] for a in aliases},
            {r["id"]: r["unit"] for r in rows},
        )

    # ---------- O'qish ----------

    async def view(self, doc_id: int) -> DocView | None:
        row = await self.pool.fetchrow(
            "SELECT d.*, c.name AS c_name, coalesce(c.inn, '') AS c_inn, c.contract AS c_contract,"
            " c.contract_verified AS c_verified"
            " FROM documents d LEFT JOIN contractors c ON c.id = d.contractor_id WHERE d.id = $1",
            doc_id,
        )
        if not row:
            return None
        lines = await self.pool.fetch(
            "SELECT * FROM document_lines WHERE document_id = $1 ORDER BY position", doc_id
        )
        view = DocView(
            id=row["id"],
            status=row["status"],
            doc_date=row["doc_date"],
            doc_number=row["doc_number"],
            comment=row["comment"],
            supplier_raw=row["supplier_raw"] or "",
            receiver_raw=row["receiver_raw"] or "",
            contractor_id=row["contractor_id"],
            contractor_name=row["c_name"] or "",
            contractor_inn=row["c_inn"] or "",
            contract=row["c_contract"] or "",
            contract_verified=bool(row["c_verified"]) if row["contractor_id"] else True,
            match_method=row["match_method"] or "",
            match_score=row["match_score"] or 0,
            candidates=row["candidates"] or [],
            paper_total=row["paper_total"],
            ai_notes=row["ai_notes"] or [],
            error=row["error"] or "",
            batch_id=row["batch_id"],
            lines=[
                Line(
                    position=ln["position"], name=ln["name"], unit=ln["unit"],
                    quantity=ln["quantity"], price=ln["price"], amount=ln["amount"],
                    raw_name=ln["raw_name"], confidence=ln["confidence"], match_note=ln["match_note"],
                )
                for ln in lines
            ],
        )
        if view.contractor_id and view.doc_date and view.lines:
            view.duplicate_of = await self.pool.fetchval(
                "SELECT d.id FROM documents d WHERE d.id <> $1 AND d.contractor_id = $2 AND d.doc_date = $3"
                " AND d.status NOT IN ('cancelled', 'failed')"
                " AND (SELECT coalesce(sum(amount), 0) FROM document_lines l WHERE l.document_id = d.id) = $4"
                " ORDER BY d.id LIMIT 1",
                view.id, view.contractor_id, view.doc_date, view.total,
            )
        if view.status in ("draft", "confirmed"):
            view.issues = check_document(view, self._today())
        return view

    # ---------- Tahrir (faqat qoralama) ----------

    @staticmethod
    async def _lock_draft(conn, doc_id: int) -> None:
        status = await conn.fetchval("SELECT status FROM documents WHERE id = $1 FOR UPDATE", doc_id)
        if status != "draft":
            raise NotEditable

    async def _set_field(self, doc_id: int, column: str, value) -> None:
        # column faqat shu fayldagi doimiy nomlardan keladi, foydalanuvchidan emas.
        row = await self.pool.fetchval(
            f"UPDATE documents SET {column} = $2, updated_at = now()"
            " WHERE id = $1 AND status = 'draft' RETURNING id",
            doc_id, value,
        )
        if not row:
            raise NotEditable

    async def set_date(self, doc_id: int, value: date) -> None:
        await self._set_field(doc_id, "doc_date", value)

    async def set_number(self, doc_id: int, value: str) -> None:
        await self._set_field(doc_id, "doc_number", value.strip())

    async def set_comment(self, doc_id: int, value: str) -> None:
        await self._set_field(doc_id, "comment", " ".join(value.split()))

    async def set_contractor(self, doc_id: int, contractor_id: int) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await self._lock_draft(conn, doc_id)
            await conn.execute(
                "UPDATE documents SET contractor_id = $2, match_method = 'manual', match_score = 100,"
                " updated_at = now() WHERE id = $1",
                doc_id, contractor_id,
            )

    async def update_line(self, doc_id: int, position: int, name: str, unit: str, qty: Decimal, price: Decimal) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await self._lock_draft(conn, doc_id)
            done = await conn.fetchval(
                "UPDATE document_lines SET name = $3, unit = $4, quantity = $5, price = $6, amount = $7,"
                " confidence = 'high', match_note = '' WHERE document_id = $1 AND position = $2 RETURNING id",
                doc_id, position, name, unit, qty, price, qty * price,
            )
            if not done:
                raise NotEditable

    async def add_line(self, doc_id: int, name: str, unit: str, qty: Decimal, price: Decimal) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await self._lock_draft(conn, doc_id)
            pos = await conn.fetchval(
                "SELECT coalesce(max(position), 0) + 1 FROM document_lines WHERE document_id = $1", doc_id
            )
            await conn.execute(
                "INSERT INTO document_lines (document_id, position, raw_name, name, unit, quantity, price, amount)"
                " VALUES ($1, $2, $3, $3, $4, $5, $6, $7)",
                doc_id, pos, name, unit, qty, price, qty * price,
            )

    async def delete_line(self, doc_id: int, position: int) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await self._lock_draft(conn, doc_id)
            await conn.execute(
                "DELETE FROM document_lines WHERE document_id = $1 AND position = $2", doc_id, position
            )
            rows = await conn.fetch(
                "SELECT id FROM document_lines WHERE document_id = $1 ORDER BY position", doc_id
            )
            # Unique (document_id, position) buzilmasligi uchun avval manfiyga o'tkaziladi.
            await conn.execute(
                "UPDATE document_lines SET position = -position WHERE document_id = $1", doc_id
            )
            for pos, row in enumerate(rows, 1):
                await conn.execute("UPDATE document_lines SET position = $2 WHERE id = $1", row["id"], pos)

    # ---------- Holat o'tishlari ----------

    async def confirm(self, doc_id: int, user_id: int) -> None:
        view = await self.view(doc_id)
        if view is None or view.status != "draft":
            raise NotEditable
        if view.blocks:
            raise Blocked([b.text for b in view.blocks])
        async with self.pool.acquire() as conn, conn.transaction():
            done = await conn.fetchval(
                "UPDATE documents SET status = 'confirmed', confirmed_by = $2, confirmed_at = now(),"
                " updated_at = now() WHERE id = $1 AND status = 'draft' RETURNING id",
                doc_id, user_id,
            )
            if not done:
                raise NotEditable
            await self._learn(conn, view)

    @staticmethod
    async def _learn(conn, view: DocView) -> None:
        """Tasdiqlangan moslik keyingi safar o'zi topilishi uchun yodda qoladi."""
        alias = norm_party(view.supplier_raw)
        if alias and view.contractor_id:
            await conn.execute(
                "INSERT INTO contractor_aliases (alias_norm, contractor_id) VALUES ($1, $2)"
                " ON CONFLICT (alias_norm) DO UPDATE SET contractor_id = EXCLUDED.contractor_id",
                alias, view.contractor_id,
            )
        for ln in view.lines:
            material_id = await conn.fetchval(
                "INSERT INTO materials (name, unit) VALUES ($1, $2) ON CONFLICT (name) DO UPDATE"
                " SET unit = CASE WHEN materials.unit = '' THEN EXCLUDED.unit ELSE materials.unit END"
                " RETURNING id",
                ln.name, ln.unit,
            )
            for text in {norm_item(ln.name), norm_item(ln.raw_name)}:
                if text:
                    await conn.execute(
                        "INSERT INTO material_aliases (alias_norm, material_id) VALUES ($1, $2)"
                        " ON CONFLICT (alias_norm) DO UPDATE SET material_id = EXCLUDED.material_id",
                        text, material_id,
                    )

    async def _transition(self, doc_id: int, source: str, target: str) -> None:
        done = await self.pool.fetchval(
            "UPDATE documents SET status = $3, updated_at = now() WHERE id = $1 AND status = $2 RETURNING id",
            doc_id, source, target,
        )
        if not done:
            raise NotEditable

    async def unconfirm(self, doc_id: int) -> None:
        await self._transition(doc_id, "confirmed", "draft")

    async def cancel(self, doc_id: int) -> None:
        await self._transition(doc_id, "draft", "cancelled")

    async def restore(self, doc_id: int) -> None:
        try:
            await self._transition(doc_id, "cancelled", "draft")
        except asyncpg.UniqueViolationError as exc:  # shu rasm qayta yuborilgan bo'lsa
            raise NotEditable from exc

    # ---------- Navbat va eksport ----------

    async def queue(self) -> tuple[list[QueueItem], int]:
        rows = await self.pool.fetch(
            "SELECT d.id, d.doc_date, c.name,"
            " (SELECT coalesce(sum(amount), 0) FROM document_lines l WHERE l.document_id = d.id) AS total"
            " FROM documents d JOIN contractors c ON c.id = d.contractor_id"
            " WHERE d.status = 'confirmed' ORDER BY d.doc_date, d.id"
        )
        drafts = await self.pool.fetchval("SELECT count(*) FROM documents WHERE status = 'draft'")
        return [QueueItem(r["id"], r["doc_date"], r["name"], r["total"]) for r in rows], drafts

    async def export(self, user_id: int) -> ExportResult | None:
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT d.id, d.doc_date, d.doc_number, d.comment, coalesce(c.inn, '') AS inn,"
                " c.name, c.contract FROM documents d JOIN contractors c ON c.id = d.contractor_id"
                " WHERE d.status = 'confirmed' ORDER BY d.doc_date, d.id FOR UPDATE OF d"
            )
            if not rows:
                return None
            docs: list[ExportDoc] = []
            for row in rows:
                lines = await conn.fetch(
                    "SELECT * FROM document_lines WHERE document_id = $1 ORDER BY position", row["id"]
                )
                export_lines = [
                    ExportLine(ln["position"], ln["name"], ln["unit"], ln["quantity"], ln["price"], ln["amount"])
                    for ln in lines
                ]
                docs.append(ExportDoc(
                    key=document_key(row["id"]), doc_date=row["doc_date"], inn=row["inn"],
                    contractor=row["name"], contract=row["contract"],
                    total=sum((ln.amount for ln in export_lines), Decimal(0)),
                    in_number=row["doc_number"], comment=row["comment"], lines=export_lines,
                ))
            content = build_csv(docs, self.profile)  # kodirovka xatosida tranzaksiya qaytadi
            total = sum((d.total for d in docs), Decimal(0))
            batch_id = await conn.fetchval(
                "INSERT INTO export_batches (filename, doc_count, total_amount, content, created_by)"
                " VALUES ('', $1, $2, $3, $4) RETURNING id",
                len(docs), total, content, user_id,
            )
            filename = f"postuplenie_{now_local():%Y-%m-%d_%H%M}_{batch_id}.csv"
            await conn.execute("UPDATE export_batches SET filename = $2 WHERE id = $1", batch_id, filename)
            await conn.execute(
                "UPDATE documents SET status = 'exported', batch_id = $1, exported_at = now(),"
                " updated_at = now() WHERE id = ANY($2::bigint[])",
                batch_id, [r["id"] for r in rows],
            )
            drafts = await conn.fetchval("SELECT count(*) FROM documents WHERE status = 'draft'")
        return ExportResult(batch_id, filename, content, len(docs), total, drafts)

    async def batches(self, limit: int = 10) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT id, filename, doc_count, total_amount, created_at FROM export_batches"
            " ORDER BY id DESC LIMIT $1",
            limit,
        )

    async def batch(self, batch_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM export_batches WHERE id = $1", batch_id)

    # ---------- Ma'lumotnomalar ----------

    async def contractors(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT id, inn, name, contract, contract_verified FROM contractors WHERE active ORDER BY name"
        )

    async def search_contractors(self, query: str, limit: int = 8) -> list[dict]:
        async with self.pool.acquire() as conn:
            items, _ = await self._contractor_directory(conn)
        if query.strip().isdigit():
            found = [it for it in items if it.inn.startswith(query.strip())]
            return [{"id": it.id, "name": it.name, "score": 100} for it in found[:limit]]
        return [c for c in rank_parties(query, items, limit) if c["score"] >= 40]

    async def all_contractor_choices(self, limit: int = 8) -> list[dict]:
        async with self.pool.acquire() as conn:
            items, _ = await self._contractor_directory(conn)
        return [{"id": it.id, "name": it.name, "score": 0} for it in items[:limit]]

    async def upsert_contractor(self, inn: str, name: str, contract: str) -> tuple[int, bool]:
        """(id, yangimi). INN bo'yicha, bo'lmasa nom bo'yicha yangilaydi."""
        async with self.pool.acquire() as conn, conn.transaction():
            existing = None
            if inn:
                existing = await conn.fetchval("SELECT id FROM contractors WHERE inn = $1", inn)
            if existing is None:
                existing = await conn.fetchval("SELECT id FROM contractors WHERE name = $1", name)
            if existing is None:
                new_id = await conn.fetchval(
                    "INSERT INTO contractors (inn, name, contract) VALUES ($1, $2, $3) RETURNING id",
                    inn or None, name, contract,
                )
                return new_id, True
            await conn.execute(
                "UPDATE contractors SET inn = coalesce(nullif($2, ''), inn), name = $3,"
                " contract = CASE WHEN $4 = '' THEN contract ELSE $4 END,"
                " contract_verified = CASE WHEN $4 = '' THEN contract_verified ELSE true END, active = true"
                " WHERE id = $1",
                existing, inn, name, contract,
            )
            return existing, False

    async def materials(self, limit: int = 60) -> tuple[list[asyncpg.Record], int]:
        rows = await self.pool.fetch(
            "SELECT name, unit, source FROM materials WHERE active ORDER BY name LIMIT $1", limit
        )
        count = await self.pool.fetchval("SELECT count(*) FROM materials WHERE active")
        return rows, count

    async def upsert_material(self, name: str, unit: str, source: str = "manual") -> bool:
        """Yangi bo'lsa True."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO materials (name, unit, source) VALUES ($1, $2, $3)"
                " ON CONFLICT (name) DO UPDATE SET"
                " unit = CASE WHEN EXCLUDED.unit = '' THEN materials.unit ELSE EXCLUDED.unit END,"
                " source = EXCLUDED.source, active = true"
                " RETURNING id, (xmax = 0) AS inserted",
                name, unit, source,
            )
            await conn.execute(
                "INSERT INTO material_aliases (alias_norm, material_id) VALUES ($1, $2)"
                " ON CONFLICT (alias_norm) DO UPDATE SET material_id = EXCLUDED.material_id",
                norm_item(name), row["id"],
            )
            return row["inserted"]

    async def import_contractors(self, raw: bytes) -> ImportStats:
        rows = read_table(raw)
        stats = ImportStats()
        if not rows:
            return stats
        i_inn = _column_index(rows[0], ("инн", "inn", "stir"))
        i_name = _column_index(rows[0], ("контрагент", "наименование", "nomi", "name"))
        i_contract = _column_index(rows[0], ("договор", "shartnoma", "contract"))
        has_header = i_name is not None
        if not has_header:
            i_inn, i_name, i_contract = 0, 1, 2
        for row in rows[1 if has_header else 0:]:
            get = lambda i: row[i] if i is not None and i < len(row) else ""  # noqa: E731, B023
            name, inn = get(i_name), get(i_inn)
            if not name or (inn and not re.fullmatch(r"\d{9}|\d{14}", inn)):
                stats.skipped += 1
                continue
            _, added = await self.upsert_contractor(inn, name, get(i_contract))
            stats.added += int(added)
            stats.updated += int(not added)
        return stats

    async def import_materials(self, raw: bytes) -> ImportStats:
        rows = read_table(raw)
        stats = ImportStats()
        if not rows:
            return stats
        i_name = _column_index(rows[0], ("материал", "номенклатура", "наименование", "nomi", "name"))
        i_unit = _column_index(rows[0], ("ед", "unit", "birlik", "o'lchov"))
        has_header = i_name is not None
        if not has_header:
            i_name, i_unit = 0, 1
        for row in rows[1 if has_header else 0:]:
            name = row[i_name] if i_name < len(row) else ""
            unit = row[i_unit] if i_unit is not None and i_unit < len(row) else ""
            if not name:
                stats.skipped += 1
                continue
            added = await self.upsert_material(name, unit, "import")
            stats.added += int(added)
            stats.updated += int(not added)
        return stats
