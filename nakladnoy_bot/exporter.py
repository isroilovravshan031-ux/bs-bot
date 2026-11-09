"""CSV yasash. Shakl to'liq config/export_profile.toml dan olinadi."""
from __future__ import annotations

import csv
import io
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from .numbers import plain

DOC_SOURCES = {
    "doc.datetime", "doc.date", "doc.inn", "doc.contractor", "doc.contract", "doc.total",
    "doc.currency", "doc.department", "doc.vat", "doc.in_number", "doc.in_date",
    "doc.responsible", "doc.comment", "doc.key",
}
LINE_SOURCES = {"line.no", "line.name", "line.unit", "line.qty", "line.price", "line.amount"}
# Son maydonlari: formula himoyasi kerak emas, kasr ajratkichi qo'llanadi.
NUMERIC_SOURCES = {"doc.total", "line.qty", "line.price", "line.amount"}


class ProfileError(ValueError):
    pass


class EncodingProblem(ValueError):
    """Tanlangan kodirovka matnni yoza olmaydi. Jim almashtirish o'rniga eksport to'xtaydi."""


@dataclass(frozen=True)
class Column:
    header: str
    source: str = ""
    value: str = ""


@dataclass(frozen=True)
class Profile:
    delimiter: str = ";"
    encoding: str = "utf-8-sig"
    decimal_separator: str = "."
    date_format: str = "%d.%m.%Y"
    datetime_format: str = "%d.%m.%Y %H:%M:%S"
    document_time: time = time(12, 0, 0)
    repeat_document_fields: bool = True
    currency: str = "UZS"
    vat: str = "Без НДС"
    department: str = ""
    responsible: str = ""
    columns: tuple[Column, ...] = field(default_factory=tuple)


def load_profile(path: Path) -> Profile:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError(f"{path} o'qilmadi: {exc}") from exc
    fmt_, defaults = raw.get("format", {}), raw.get("defaults", {})
    columns = []
    for item in raw.get("columns", []):
        col = Column(item.get("header", ""), item.get("source", ""), item.get("value", ""))
        if not col.header:
            raise ProfileError("Ustunda header yo'q")
        if col.source and col.source not in DOC_SOURCES | LINE_SOURCES:
            raise ProfileError(f"Noma'lum source: {col.source}")
        columns.append(col)
    if not columns:
        raise ProfileError("Profilda ustunlar yo'q")
    if len(fmt_.get("delimiter", ";")) != 1:
        raise ProfileError("delimiter bitta belgi bo'lishi kerak")
    try:
        doc_time = time.fromisoformat(fmt_.get("document_time", "12:00:00"))
        "".encode(fmt_.get("encoding", "utf-8-sig"))
    except (ValueError, LookupError) as exc:
        raise ProfileError(f"Profil qiymati noto'g'ri: {exc}") from exc
    return Profile(
        delimiter=fmt_.get("delimiter", ";"),
        encoding=fmt_.get("encoding", "utf-8-sig"),
        decimal_separator=fmt_.get("decimal_separator", "."),
        date_format=fmt_.get("date_format", "%d.%m.%Y"),
        datetime_format=fmt_.get("datetime_format", "%d.%m.%Y %H:%M:%S"),
        document_time=doc_time,
        repeat_document_fields=fmt_.get("repeat_document_fields", True),
        currency=defaults.get("currency", "UZS"),
        vat=defaults.get("vat", ""),
        department=defaults.get("department", ""),
        responsible=defaults.get("responsible", ""),
        columns=tuple(columns),
    )


@dataclass
class ExportLine:
    no: int
    name: str
    unit: str
    quantity: Decimal
    price: Decimal
    amount: Decimal


@dataclass
class ExportDoc:
    key: str
    doc_date: date
    inn: str
    contractor: str
    contract: str
    total: Decimal
    in_number: str
    comment: str
    lines: list[ExportLine]


def _safe_text(text: str) -> str:
    """Excel'da ochilganda matn formula bo'lib ishlamasin (qo'lyozmadan kelgan matn ishonchsiz)."""
    return "'" + text if text[:1] in ("=", "+", "@", "\t", "\r") else text


def _number(value: Decimal, profile: Profile) -> str:
    return plain(value).replace(".", profile.decimal_separator)


def _doc_value(source: str, doc: ExportDoc, profile: Profile) -> str:
    stamp = datetime.combine(doc.doc_date, profile.document_time)
    return {
        "doc.datetime": stamp.strftime(profile.datetime_format),
        "doc.date": doc.doc_date.strftime(profile.date_format),
        "doc.inn": doc.inn,
        "doc.contractor": doc.contractor,
        "doc.contract": doc.contract,
        "doc.total": _number(doc.total, profile),
        "doc.currency": profile.currency,
        "doc.department": profile.department,
        "doc.vat": profile.vat,
        "doc.in_number": doc.in_number,
        "doc.in_date": doc.doc_date.strftime(profile.date_format),
        "doc.responsible": profile.responsible,
        "doc.comment": doc.comment,
        "doc.key": doc.key,
    }[source]


def _line_value(source: str, line: ExportLine, profile: Profile) -> str:
    return {
        "line.no": str(line.no),
        "line.name": line.name,
        "line.unit": line.unit,
        "line.qty": _number(line.quantity, profile),
        "line.price": _number(line.price, profile),
        "line.amount": _number(line.amount, profile),
    }[source]


def build_csv(docs: list[ExportDoc], profile: Profile) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=profile.delimiter, lineterminator="\r\n")
    writer.writerow([c.header for c in profile.columns])
    for doc in docs:
        for index, line in enumerate(doc.lines):
            row = []
            for col in profile.columns:
                if col.source in LINE_SOURCES:
                    text = _line_value(col.source, line, profile)
                elif col.source in DOC_SOURCES:
                    # doc.key doim yoziladi: yuklovchi qatorlarni shu bo'yicha guruhlaydi.
                    keep = index == 0 or profile.repeat_document_fields or col.source == "doc.key"
                    text = _doc_value(col.source, doc, profile) if keep else ""
                else:
                    text = col.value
                row.append(text if col.source in NUMERIC_SOURCES else _safe_text(text))
            writer.writerow(row)
    text = buffer.getvalue()
    try:
        return text.encode(profile.encoding)
    except UnicodeEncodeError as exc:
        bad = text[max(0, exc.start - 15) : exc.end + 15].replace("\r\n", " ")
        raise EncodingProblem(
            f"{profile.encoding} kodirovkasi bu matnni yoza olmaydi: «{bad}». "
            "export_profile.toml da encoding = \"utf-8-sig\" qiling."
        ) from exc
