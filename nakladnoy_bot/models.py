"""AI javob sxemasi va bot ichidagi hujjat ko'rinishi."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class ExtractedLine(BaseModel):
    name: str
    unit: Optional[str]
    quantity: Optional[float]
    price: Optional[float]
    amount: Optional[float]
    confidence: Confidence


class ExtractedInvoice(BaseModel):
    is_invoice: bool
    supplier_name: Optional[str]
    supplier_inn: Optional[str]
    receiver_name: Optional[str]
    doc_number: Optional[str]
    doc_date: Optional[str]
    lines: list[ExtractedLine]
    total: Optional[float]
    uncertainties: list[str]


@dataclass
class Line:
    position: int
    name: str
    unit: str
    quantity: Decimal | None
    price: Decimal | None
    amount: Decimal | None
    raw_name: str = ""
    confidence: str = "high"
    match_note: str = ""


@dataclass
class Issue:
    level: str  # block: tasdiqlab bo'lmaydi; warn: ko'rsatiladi, lekin to'smaydi
    text: str


@dataclass
class DocView:
    id: int
    status: str
    doc_date: date | None
    doc_number: str
    comment: str
    supplier_raw: str
    receiver_raw: str
    contractor_id: int | None
    contractor_name: str
    contractor_inn: str
    contract: str
    contract_verified: bool
    match_method: str
    match_score: int
    candidates: list[dict]
    paper_total: Decimal | None
    ai_notes: list[str]
    error: str
    lines: list[Line]
    duplicate_of: int | None = None
    batch_id: int | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((ln.amount or Decimal(0) for ln in self.lines), Decimal(0))

    @property
    def key(self) -> str:
        return document_key(self.id)

    @property
    def blocks(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "block"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warn"]


def document_key(doc_id: int) -> str:
    return f"NK-{doc_id:06d}"
