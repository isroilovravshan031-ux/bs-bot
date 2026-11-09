"""Haqiqiy PostgreSQL bilan to'liq oqim."""
import asyncio
import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from nakladnoy_bot.gemini import ExtractionError
from nakladnoy_bot.imaging import BadImage, prepare
from nakladnoy_bot.service import (
    BadInput,
    Blocked,
    NotEditable,
    parse_contractor_input,
    parse_line_input,
    read_table,
)

from .conftest import invoice, make_image

USER, CHAT = 111, 222


def rows_of(content: bytes):
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""), delimiter=";"))


async def test_seed_has_screenshot_contractors(pool):
    assert await pool.fetchval("SELECT count(*) FROM contractors") == 10
    row = await pool.fetchrow("SELECT name, contract FROM contractors WHERE inn = '300000001'")
    assert row["contract"] == "Договор № 1 от 01.01.2026г."
    assert await pool.fetchval("SELECT count(*) FROM contractors WHERE NOT contract_verified") == 2


async def test_real_nakladnoy_full_flow_to_csv(make_service):
    svc = make_service()
    res = await svc.ingest(make_image(), USER, CHAT)
    view = await svc.view(res.doc_id)
    assert view.status == "draft"
    assert view.contractor_inn == "300000001"
    assert view.match_method == "exact"
    assert view.doc_date == date(2026, 11, 3)
    assert view.total == Decimal("720000")
    assert view.issues == []

    await svc.confirm(res.doc_id, USER)
    items, drafts = await svc.queue()
    assert [i.id for i in items] == [res.doc_id] and drafts == 0

    out = await svc.export(USER)
    assert out.doc_count == 1 and out.total == Decimal("720000")
    rows = rows_of(out.content)
    assert [r["Материал"] for r in rows] == ["А4 лист", "Офис дафтари", "Офис папка"]
    assert rows[0]["Контрагент"] == "Тошматов Азиз  (снабженец)"
    assert rows[0]["Сумма документа"] == "720000"
    assert (await svc.view(res.doc_id)).status == "exported"


async def test_export_never_repeats_documents(make_service):
    svc = make_service()
    res = await svc.ingest(make_image(), USER, CHAT)
    await svc.confirm(res.doc_id, USER)
    assert await svc.export(USER) is not None
    assert await svc.export(USER) is None  # ikkinchi bosish bo'sh


async def test_concurrent_exports_produce_one_file(make_service):
    svc = make_service()
    res = await svc.ingest(make_image(), USER, CHAT)
    await svc.confirm(res.doc_id, USER)
    results = await asyncio.gather(svc.export(USER), svc.export(USER))
    assert sum(r is not None for r in results) == 1


async def test_batch_stays_downloadable(make_service):
    svc = make_service()
    res = await svc.ingest(make_image(), USER, CHAT)
    await svc.confirm(res.doc_id, USER)
    out = await svc.export(USER)
    stored = await svc.batch(out.batch_id)
    assert bytes(stored["content"]) == out.content
    assert stored["filename"] == out.filename and out.filename.endswith(f"_{out.batch_id}.csv")
    assert [b["id"] for b in await svc.batches()] == [out.batch_id]


async def test_same_photo_is_not_read_twice(make_service):
    svc = make_service()
    raw = make_image()
    first = await svc.ingest(raw, USER, CHAT)
    second = await svc.ingest(raw, USER, CHAT)
    assert second.duplicate and second.doc_id == first.doc_id
    assert len(svc.extractor.calls) == 1


async def test_cancelled_photo_can_be_sent_again(make_service):
    svc = make_service()
    raw = make_image()
    first = await svc.ingest(raw, USER, CHAT)
    await svc.cancel(first.doc_id)
    second = await svc.ingest(raw, USER, CHAT)
    assert not second.duplicate and second.doc_id != first.doc_id
    with pytest.raises(NotEditable):  # ikkinchisi faol: birinchini tiklab bo'lmaydi
        await svc.restore(first.doc_id)


async def test_different_photo_same_content_warns_duplicate(make_service):
    svc = make_service()
    a = await svc.ingest(make_image("a"), USER, CHAT)
    b = await svc.ingest(make_image("b"), USER, CHAT)
    view = await svc.view(b.doc_id)
    assert view.duplicate_of == a.doc_id
    assert any("O'xshash" in i.text for i in view.warnings)
    await svc.confirm(b.doc_id, USER)  # ogohlantirish to'smaydi: bir kunda ikki xarid bo'lishi mumkin


async def test_arithmetic_mismatch_triggers_one_reread_and_second_wins(make_service):
    bad = invoice(lines=[
        {"name": "А4 лист", "unit": "блок", "quantity": 2, "price": 250000, "amount": 560000, "confidence": "medium"},
        {"name": "Офис дафтари", "unit": "шт", "quantity": 4, "price": 35000, "amount": 140000, "confidence": "high"},
        {"name": "Офис папка", "unit": "шт", "quantity": 2, "price": 40000, "amount": 80000, "confidence": "high"},
    ])
    svc = make_service(bad, invoice())
    res = await svc.ingest(make_image(), USER, CHAT)
    assert svc.extractor.calls[0] is None and "line 1" in svc.extractor.calls[1][0]
    view = await svc.view(res.doc_id)
    assert view.total == Decimal("720000") and view.issues == []


async def test_real_paper_mistake_stays_visible_after_reread(make_service):
    bad = invoice(lines=[
        {"name": "Офис папка", "unit": "шт", "quantity": 2, "price": 40000, "amount": 90000, "confidence": "high"},
    ], total=90000)
    svc = make_service(bad)
    res = await svc.ingest(make_image(), USER, CHAT)
    assert len(svc.extractor.calls) == 2
    view = await svc.view(res.doc_id)
    assert view.total == Decimal("90000")  # qog'ozdagi qiymat jim almashtirilmaydi
    assert any("2 × 40 000 = 80 000" in i.text for i in view.warnings)
    await svc.update_line(res.doc_id, 1, "Офис папка", "шт", Decimal(2), Decimal(40000))
    view = await svc.view(res.doc_id)
    assert view.total == Decimal("80000")


async def test_reread_failure_keeps_first_result(make_service):
    bad = invoice(total=999999)
    svc = make_service(bad, ExtractionError("quota", "limit"))
    res = await svc.ingest(make_image(), USER, CHAT)
    view = await svc.view(res.doc_id)
    assert view.status == "draft"
    assert any("qog'ozdagi jami" in i.text for i in view.warnings)


async def test_missing_amount_is_computed_and_flagged(make_service):
    svc = make_service(invoice(lines=[
        {"name": "Офис папка", "unit": "шт", "quantity": 2, "price": 40000, "amount": None, "confidence": "high"},
    ], total=None))
    res = await svc.ingest(make_image(), USER, CHAT)
    view = await svc.view(res.doc_id)
    assert view.lines[0].amount == Decimal("80000")
    assert any("rasm bilan solishtiring" in i.text for i in view.warnings)


async def test_ai_failure_keeps_photo_for_retry(make_service):
    svc = make_service(ExtractionError("quota", "limit"), invoice())
    res = await svc.ingest(make_image(), USER, CHAT)
    view = await svc.view(res.doc_id)
    assert view.status == "failed" and view.error.startswith("quota")
    await svc.retry(res.doc_id)
    assert (await svc.view(res.doc_id)).status == "draft"
    with pytest.raises(NotEditable):
        await svc.retry(res.doc_id)


async def test_failed_photo_may_be_resent(make_service):
    svc = make_service(ExtractionError("network", "x"), invoice())
    raw = make_image()
    first = await svc.ingest(raw, USER, CHAT)
    second = await svc.ingest(raw, USER, CHAT)
    assert not second.duplicate and second.doc_id != first.doc_id


async def test_non_invoice_photo_is_marked(make_service):
    svc = make_service(invoice(is_invoice=False, lines=[]))
    res = await svc.ingest(make_image(), USER, CHAT)
    view = await svc.view(res.doc_id)
    assert view.status == "failed" and view.error.startswith("not_invoice")


async def test_bad_file_is_rejected_before_ai(make_service):
    svc = make_service()
    with pytest.raises(BadImage):
        await svc.ingest(b"bu rasm emas", USER, CHAT)
    assert svc.extractor.calls == []


async def test_pdf_passes_through(make_service):
    svc = make_service()
    res = await svc.ingest(b"%PDF-1.7 test", USER, CHAT)
    assert (await svc.view(res.doc_id)).status == "draft"


def test_prepare_shrinks_and_rotates_large_photo():
    from PIL import Image

    img = Image.new("RGB", (6000, 3000), "white")
    exif = img.getexif()
    exif[0x0112] = 6  # 90 gradusga burilgan telefon rasmi
    out = io.BytesIO()
    img.save(out, format="JPEG", exif=exif)
    prepared = prepare(out.getvalue())
    result = Image.open(io.BytesIO(prepared.data))
    assert result.size == (1300, 2600)


async def test_unknown_supplier_blocks_until_chosen_then_learns(make_service, pool):
    svc = make_service(invoice(supplier_name="AVTO"))
    res = await svc.ingest(make_image("a"), USER, CHAT)
    view = await svc.view(res.doc_id)
    assert view.contractor_id is None
    assert view.candidates and "AVTOTRAK" in view.candidates[0]["name"]
    with pytest.raises(Blocked) as err:
        await svc.confirm(res.doc_id, USER)
    assert "Kontragent tanlanmagan" in err.value.reasons

    await svc.set_contractor(res.doc_id, view.candidates[0]["id"])
    await svc.confirm(res.doc_id, USER)

    # Ikkinchi marta shu yozuv o'zi topiladi.
    other = await svc.ingest(make_image("b"), USER, CHAT)
    assert (await svc.view(other.doc_id)).match_method == "alias"


async def test_misspelled_supplier_is_matched_with_warning(make_service):
    svc = make_service(invoice(supplier_name="AVTO TRAK"))
    res = await svc.ingest(make_image(), USER, CHAT)
    view = await svc.view(res.doc_id)
    assert view.contractor_inn == "300000005" and view.match_method == "fuzzy"
    assert any("taxminan topildi" in i.text for i in view.warnings)


async def test_unverified_contract_warns(make_service):
    svc = make_service(invoice(supplier_name="OQTOSH INDUSTRIES"))
    res = await svc.ingest(make_image(), USER, CHAT)
    view = await svc.view(res.doc_id)
    assert view.contractor_inn == "300000004"
    assert any("Shartnoma nomi" in i.text for i in view.warnings)


async def test_correcting_contract_clears_warning(make_service):
    svc = make_service(invoice(supplier_name="OQTOSH INDUSTRIES"))
    res = await svc.ingest(make_image(), USER, CHAT)
    _, added = await svc.upsert_contractor("300000004", "(BS) OQTOSH INDUSTRIES  MCHJ QK  (Охак)", "Договор № 4 от 01.01.2026г.")
    assert not added
    view = await svc.view(res.doc_id)
    assert not any("Shartnoma nomi" in i.text for i in view.warnings)


async def test_material_correction_is_learned(make_service):
    svc = make_service()
    res = await svc.ingest(make_image("a"), USER, CHAT)
    await svc.update_line(res.doc_id, 1, "Бумага A4", "пачка", Decimal(2), Decimal(250000))
    await svc.confirm(res.doc_id, USER)
    other = await svc.ingest(make_image("b"), USER, CHAT)
    view = await svc.view(other.doc_id)
    assert view.lines[0].name == "Бумага A4"
    assert view.lines[0].raw_name == "А4 лист"
    assert view.lines[0].unit == "блок"  # qog'ozdagi birlik saqlanadi


async def test_edit_operations_and_totals(make_service):
    svc = make_service()
    res = await svc.ingest(make_image(), USER, CHAT)
    doc = res.doc_id
    await svc.set_date(doc, date(2026, 11, 2))
    await svc.set_number(doc, " 15 ")
    await svc.set_comment(doc, "AVTO TRAK")
    await svc.add_line(doc, "Ручка", "шт", Decimal(10), Decimal(2000))
    await svc.delete_line(doc, 1)
    view = await svc.view(doc)
    assert view.doc_date == date(2026, 11, 2) and view.doc_number == "15" and view.comment == "AVTO TRAK"
    assert [(ln.position, ln.name) for ln in view.lines] == [(1, "Офис дафтари"), (2, "Офис папка"), (3, "Ручка")]
    assert view.total == Decimal("240000")
    await svc.confirm(doc, USER)
    rows = rows_of((await svc.export(USER)).content)
    assert rows[0]["№ вх.док-та"] == "15" and rows[0]["Комментарий"] == "AVTO TRAK"
    assert rows[0]["Дата вх.док-та"] == "02.11.2026"


async def test_exported_document_is_locked(make_service):
    svc = make_service()
    res = await svc.ingest(make_image(), USER, CHAT)
    await svc.confirm(res.doc_id, USER)
    await svc.export(USER)
    for action in (
        svc.set_date(res.doc_id, date(2026, 1, 1)),
        svc.delete_line(res.doc_id, 1),
        svc.cancel(res.doc_id),
        svc.unconfirm(res.doc_id),
        svc.confirm(res.doc_id, USER),
    ):
        with pytest.raises(NotEditable):
            await action


async def test_confirm_twice_is_rejected(make_service):
    svc = make_service()
    res = await svc.ingest(make_image(), USER, CHAT)
    await svc.confirm(res.doc_id, USER)
    with pytest.raises(NotEditable):
        await svc.confirm(res.doc_id, USER)
    await svc.unconfirm(res.doc_id)
    await svc.confirm(res.doc_id, USER)


async def test_no_date_or_lines_blocks(make_service):
    svc = make_service(invoice(doc_date=None, lines=[], total=None))
    res = await svc.ingest(make_image(), USER, CHAT)
    with pytest.raises(Blocked) as err:
        await svc.confirm(res.doc_id, USER)
    assert set(err.value.reasons) == {"Sana yo'q", "Birorta ham qator yo'q"}


async def test_unreadable_quantity_blocks(make_service):
    svc = make_service(invoice(lines=[
        {"name": "Папка", "unit": "шт", "quantity": None, "price": 40000, "amount": 80000, "confidence": "low"},
    ], total=80000))
    res = await svc.ingest(make_image(), USER, CHAT)
    with pytest.raises(Blocked) as err:
        await svc.confirm(res.doc_id, USER)
    assert any("soni" in r for r in err.value.reasons)


async def test_old_and_future_dates_warn(make_service):
    for value, text in (("2026-08-01", "eski"), ("2027-11-03", "kelajakda")):
        svc = make_service(invoice(doc_date=value))
        res = await svc.ingest(make_image(value), USER, CHAT)
        assert any(text in i.text for i in (await svc.view(res.doc_id)).warnings)


async def test_export_rolls_back_on_encoding_problem(make_service, profile):
    import dataclasses

    from nakladnoy_bot.exporter import EncodingProblem

    svc = make_service(invoice(lines=[
        {"name": "Қоғоз қоп", "unit": "шт", "quantity": 1, "price": 1000, "amount": 1000, "confidence": "high"},
    ], total=1000))
    svc.profile = dataclasses.replace(profile, encoding="cp1251")
    res = await svc.ingest(make_image(), USER, CHAT)
    await svc.confirm(res.doc_id, USER)
    with pytest.raises(EncodingProblem):
        await svc.export(USER)
    assert (await svc.view(res.doc_id)).status == "confirmed"
    assert await svc.batches() == []


async def test_draft_is_not_exported(make_service):
    svc = make_service()
    await svc.ingest(make_image("a"), USER, CHAT)
    assert await svc.export(USER) is None
    confirmed = await svc.ingest(make_image("b"), USER, CHAT)
    await svc.confirm(confirmed.doc_id, USER)
    out = await svc.export(USER)
    assert out.doc_count == 1 and out.drafts_left == 1


async def test_export_order_and_grouping_by_key(make_service):
    svc = make_service(invoice(doc_date="2026-11-03"), invoice(doc_date="2026-11-01", supplier_name="TEMIRSAVDO"))
    a = await svc.ingest(make_image("a"), USER, CHAT)
    b = await svc.ingest(make_image("b"), USER, CHAT)
    await svc.confirm(a.doc_id, USER)
    await svc.confirm(b.doc_id, USER)
    rows = rows_of((await svc.export(USER)).content)
    assert len(rows) == 6
    assert rows[0]["Date"].startswith("01.11.2026") and rows[3]["Date"].startswith("03.11.2026")
    assert len({r["Ключ документа"] for r in rows}) == 2


def test_parse_line_input():
    assert parse_line_input("Офис папка; шт; 2; 40.000") == ("Офис папка", "шт", Decimal(2), Decimal(40000))
    for bad in ("Офис папка", "; шт; 2; 3", "x; шт; abc; 3", "x; шт; 0; 3"):
        with pytest.raises(BadInput):
            parse_line_input(bad)


def test_parse_contractor_input():
    assert parse_contractor_input("300000001; Nomi; Договор") == ("300000001", "Nomi", "Договор")
    assert parse_contractor_input("; Nomi") == ("", "Nomi", "")
    for bad in ("123; Nomi; X", "300000001", "300000001;  ; X"):
        with pytest.raises(BadInput):
            parse_contractor_input(bad)


@pytest.mark.parametrize("encoding,delimiter", [("utf-8-sig", ";"), ("cp1251", ";"), ("utf-8", ","), ("cp1251", "\t")])
def test_read_table_detects_format(encoding, delimiter):
    raw = (delimiter.join(["ИНН", "Контрагент"]) + "\r\n" + delimiter.join(["1", "Ким"])).encode(encoding)
    assert read_table(raw) == [["ИНН", "Контрагент"], ["1", "Ким"]]


async def test_stuck_processing_returns_to_retry(make_service, pool):
    svc = make_service()
    doc_id = await pool.fetchval(
        "INSERT INTO documents (created_by, chat_id, image_path, image_hash) VALUES (1, 1, 'x', 'h') RETURNING id"
    )
    assert await svc.recover_stuck() == 1
    view = await svc.view(doc_id)
    assert view.status == "failed" and view.error.startswith("network")
    assert await svc.recover_stuck() == 0


async def test_absurd_ai_number_fails_cleanly_not_stuck(make_service):
    svc = make_service(invoice(lines=[
        {"name": "x", "unit": "шт", "quantity": 1e30, "price": 1e30, "amount": 1e30, "confidence": "low"},
    ], total=None))
    res = await svc.ingest(make_image(), USER, CHAT)
    view = await svc.view(res.doc_id)
    assert view.status == "failed" and view.error.startswith("bad_output")


async def test_newlines_from_ai_and_user_are_flattened(make_service):
    svc = make_service(invoice(lines=[
        {"name": "Офис\nпапка", "unit": "шт\n", "quantity": 2, "price": 40000, "amount": 80000, "confidence": "high"},
    ], total=80000))
    res = await svc.ingest(make_image(), USER, CHAT)
    await svc.set_comment(res.doc_id, "bir\nikki")
    view = await svc.view(res.doc_id)
    assert (view.lines[0].name, view.lines[0].unit, view.comment) == ("Офис папка", "шт", "bir ikki")


async def test_import_contractors_with_header(make_service, pool):
    svc = make_service()
    raw = (
        "ИНН;Контрагент;Договор\r\n"
        "300000004;(BS) OQTOSH INDUSTRIES  MCHJ QK  (Охак);Договор № 4 от 01.01.2026г.\r\n"
        "399999999;Yangi Firma MCHJ;Договор № 5\r\n"
        "noto'g'ri;Xato Firma;X\r\n"
    ).encode("cp1251", errors="replace")
    stats = await svc.import_contractors(raw)
    assert (stats.added, stats.updated, stats.skipped) == (1, 1, 1)
    assert await pool.fetchval(
        "SELECT contract_verified FROM contractors WHERE inn = '300000004'"
    ) is True


async def test_import_materials_then_matches_new_photos(make_service):
    svc = make_service()
    stats = await svc.import_materials("Наименование;Ед.изм.\r\nОфис папка;шт\r\n;\r\n".encode("utf-8-sig"))
    assert stats.added == 1
    rows, count = await svc.materials()
    assert count == 1 and rows[0]["unit"] == "шт"
    assert (await svc.import_materials("Офис папка;шт\r\n".encode())).updated == 1


async def test_contractor_search(make_service):
    svc = make_service()
    found = await svc.search_contractors("300000001")
    assert "Тошматов" in found[0]["name"]
    found = await svc.search_contractors("hamidov")
    assert "Ҳамидов" in found[0]["name"]
    assert await svc.search_contractors("zzzzzzzz") == []


async def test_photo_is_kept_in_database_not_on_disk(make_service, pool, tmp_path):
    svc = make_service(ExtractionError("quota", "limit"), invoice())
    res = await svc.ingest(make_image(), USER, CHAT)
    assert list(tmp_path.rglob("*.jpg")) == []  # server diski har deploy'da tozalanadi
    stored = await pool.fetchval("SELECT data FROM document_images WHERE document_id = $1", res.doc_id)
    assert bytes(stored)[:2] == b"\xff\xd8"
    await svc.retry(res.doc_id)
    assert (await svc.view(res.doc_id)).status == "draft"


async def test_retry_without_any_image_asks_to_resend(make_service, pool):
    svc = make_service(ExtractionError("quota", "limit"))
    res = await svc.ingest(make_image(), USER, CHAT)
    await pool.execute("DELETE FROM document_images WHERE document_id = $1", res.doc_id)
    await svc.retry(res.doc_id)
    view = await svc.view(res.doc_id)
    assert view.status == "failed" and view.error.startswith("no_image")


async def test_legacy_document_retries_from_disk(make_service, pool, tmp_path):
    svc = make_service(ExtractionError("quota", "limit"), invoice())
    res = await svc.ingest(make_image(), USER, CHAT)
    data = await pool.fetchval("SELECT data FROM document_images WHERE document_id = $1", res.doc_id)
    legacy = tmp_path / "old.jpg"
    legacy.write_bytes(bytes(data))
    await pool.execute("DELETE FROM document_images WHERE document_id = $1", res.doc_id)
    await pool.execute("UPDATE documents SET image_path = $2 WHERE id = $1", res.doc_id, str(legacy))
    await svc.retry(res.doc_id)
    assert (await svc.view(res.doc_id)).status == "draft"
