"""Qog'ozdagi yozuvni 1C nomi bilan solishtirish uchun normallashtirish.

Kirill/lotin, o'zbek harflari va yuridik shakllar farqini yo'qotadi:
'Тошматов Азиз' va 'Toshmatov Aziz (snabjenec)' bir xil ko'rinishga keladi.
"""
from __future__ import annotations

import re

_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "s",
    "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "қ": "k", "ғ": "g", "ў": "o", "ҳ": "h", "і": "i",
}
_LEGAL_FORMS = {"ooo", "chp", "mchj", "qk", "kk", "ok", "ao", "oao", "yatt", "xk", "hk", "mas", "ma"}


def translit(text: str) -> str:
    lowered = (text or "").lower()
    out = "".join(_CYR.get(ch, ch) for ch in lowered)
    out = re.sub(r"['`ʻʼ’‘]", "", out)
    out = out.replace("x", "h").replace("q", "k").replace("ck", "k")
    return re.sub(r"c(?!h)", "k", out)


def _tokens(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text))


def norm_party(name: str) -> str:
    """Kontragent nomi: qavsdagi teglar va yuridik shakl olib tashlanadi."""
    stripped = re.sub(r"\([^)]*\)", " ", name or "")
    tokens = [t for t in _tokens(translit(stripped)).split() if t not in _LEGAL_FORMS]
    if tokens:
        return " ".join(tokens)
    # Nom butunlay qavs ichida bo'lsa ham bo'sh qolmasin.
    return _tokens(translit(name))


def norm_item(name: str) -> str:
    """Material nomi: qavslar saqlanadi, chunki ular navni bildirishi mumkin."""
    return _tokens(translit(name))
