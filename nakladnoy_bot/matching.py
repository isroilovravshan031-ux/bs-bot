"""Qog'ozdagi nomni 1C ma'lumotnomasiga bog'lash. Sof funksiyalar, bazaga bog'liq emas."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from rapidfuzz import fuzz

from .normalize import norm_item, norm_party

AUTO_SCORE = 85
AUTO_MARGIN = 10
MATERIAL_AUTO_SCORE = 90


@dataclass(frozen=True)
class Named:
    id: int
    name: str
    inn: str = ""


@dataclass
class Match:
    id: int | None = None
    method: str = "none"  # inn | alias | exact | fuzzy | none
    score: int = 0
    candidates: list[dict] = field(default_factory=list)


def party_score(query: str, target: str) -> int:
    if not query or not target:
        return 0
    # Faqat token_set bo'lsa, 'INVEST' so'zi 'YANGI DAVR INVEST' bilan 100 berib qo'yardi.
    return round(0.6 * fuzz.token_sort_ratio(query, target) + 0.4 * fuzz.token_set_ratio(query, target))


def rank_parties(query: str, items: Iterable[Named], limit: int = 5) -> list[dict]:
    q = norm_party(query)
    scored = [{"id": it.id, "name": it.name, "score": party_score(q, norm_party(it.name))} for it in items]
    scored.sort(key=lambda c: (-c["score"], c["name"]))
    return scored[:limit]


def match_contractor(
    raw_name: str | None,
    raw_inn: str | None,
    items: list[Named],
    aliases: Mapping[str, int],
) -> Match:
    inn = "".join(ch for ch in (raw_inn or "") if ch.isdigit())
    if inn:
        for it in items:
            if it.inn == inn:
                return Match(it.id, "inn", 100)
    name = raw_name or ""
    norm = norm_party(name)
    candidates = rank_parties(name, items) if norm else []
    if norm and norm in aliases:
        return Match(aliases[norm], "alias", 100, candidates)
    if not candidates:
        return Match(candidates=[])
    best = candidates[0]
    second = candidates[1]["score"] if len(candidates) > 1 else 0
    if best["score"] == 100 and second < 100:
        return Match(best["id"], "exact", 100, candidates)
    if best["score"] >= AUTO_SCORE and best["score"] - second >= AUTO_MARGIN:
        return Match(best["id"], "fuzzy", best["score"], candidates)
    return Match(None, "none", best["score"], candidates)


@dataclass(frozen=True)
class MaterialHit:
    id: int
    name: str
    unit: str
    method: str  # alias | exact | fuzzy
    score: int


def match_material(
    raw_name: str,
    materials: list[Named],
    aliases: Mapping[str, int],
    units: Mapping[int, str],
) -> MaterialHit | None:
    norm = norm_item(raw_name)
    if not norm:
        return None
    by_id = {m.id: m for m in materials}
    alias_id = aliases.get(norm)
    if alias_id in by_id:
        m = by_id[alias_id]
        return MaterialHit(m.id, m.name, units.get(m.id, ""), "alias", 100)
    scored = sorted(
        ((round(fuzz.ratio(norm, norm_item(m.name))), m) for m in materials),
        key=lambda pair: -pair[0],
    )
    if not scored:
        return None
    best_score, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0
    if best_score == 100:
        return MaterialHit(best.id, best.name, units.get(best.id, ""), "exact", 100)
    if best_score >= MATERIAL_AUTO_SCORE and best_score - second >= 5:
        return MaterialHit(best.id, best.name, units.get(best.id, ""), "fuzzy", best_score)
    return None
