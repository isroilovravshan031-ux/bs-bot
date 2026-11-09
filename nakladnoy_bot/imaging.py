"""Rasmni AI uchun tayyorlash: to'g'ri aylantirish, o'lchamni cheklash, JPEG."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SIDE = 2600
JPEG_QUALITY = 90
PDF_MIME = "application/pdf"


class BadImage(ValueError):
    pass


@dataclass
class Prepared:
    data: bytes
    mime: str
    sha256: str


def _register_heif() -> None:
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:  # iPhone rasmlaridan boshqasi uchun shart emas
        pass


def prepare(raw: bytes) -> Prepared:
    # Xesh asl fayldan: bitta faylni qayta yuborish aniq ushlanadi.
    digest = hashlib.sha256(raw).hexdigest()
    if raw[:5] == b"%PDF-":
        return Prepared(raw, PDF_MIME, digest)
    _register_heif()
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=JPEG_QUALITY)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise BadImage("Fayl rasm sifatida ochilmadi") from exc
    return Prepared(out.getvalue(), "image/jpeg", digest)
