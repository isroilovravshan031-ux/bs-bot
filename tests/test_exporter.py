import csv
import dataclasses
import io
from datetime import date
from decimal import Decimal

import pytest

from nakladnoy_bot.exporter import (
    EncodingProblem,
    ExportDoc,
    ExportLine,
    ProfileError,
    build_csv,
    load_profile,
)


def sample_doc(**changes) -> ExportDoc:
    base = dict(
        key="NK-000001",
        doc_date=date(2026, 11, 3),
        inn="300000001",
        contractor="Тошматов Азиз  (снабженец)",
        contract="Договор № 1 от 01.01.2026г.",
        total=Decimal("720000.00"),
        in_number="",
        comment="",
        lines=[
            ExportLine(1, "А4 лист", "блок", Decimal("2.000"), Decimal("250000.00"), Decimal("500000.00")),
            ExportLine(2, "Офис дафтари", "шт", Decimal("4.000"), Decimal("35000.00"), Decimal("140000.00")),
            ExportLine(3, "Офис папка", "шт", Decimal("2.000"), Decimal("40000.00"), Decimal("80000.00")),
        ],
    )
    return ExportDoc(**{**base, **changes})


def parse(content: bytes, encoding="utf-8-sig", delimiter=";"):
    return list(csv.reader(io.StringIO(content.decode(encoding), newline=""), delimiter=delimiter))


def test_columns_follow_1c_screenshot_order(profile):
    header = parse(build_csv([sample_doc()], profile))[0]
    assert header[:8] == [
        "Date", "Number", "Биржевая", "ИНН", "Биржевая", "Контрагент", "Договор", "Сумма документа",
    ]
    assert header[8:12] == ["Валюта", "Сумма в валюте", "Подразделение", "Ставки НДС"]
    assert header[-7:] == ["Ключ документа", "№ строки", "Материал", "Ед.изм.", "Количество", "Цена", "Сумма"]


def test_real_nakladnoy_row_values(profile):
    rows = parse(build_csv([sample_doc()], profile))
    assert len(rows) == 4  # sarlavha + 3 qator
    row = dict(zip(rows[0], rows[1]))
    assert row["Date"] == "03.11.2026 12:00:00"
    assert row["Number"] == ""
    assert row["ИНН"] == "300000001"
    assert row["Контрагент"] == "Тошматов Азиз  (снабженец)"
    assert row["Договор"] == "Договор № 1 от 01.01.2026г."
    assert row["Сумма документа"] == "720000"
    assert row["Валюта"] == "UZS"
    assert row["Ставки НДС"] == "Без НДС"
    assert row["Дата вх.док-та"] == "03.11.2026"
    assert (row["Материал"], row["Ед.изм."], row["Количество"], row["Цена"], row["Сумма"]) == (
        "А4 лист", "блок", "2", "250000", "500000",
    )
    assert sum(int(dict(zip(rows[0], r))["Сумма"]) for r in rows[1:]) == 720000


def test_default_encoding_keeps_uzbek_letters_and_has_bom(profile):
    content = build_csv([sample_doc(contractor="(NANO) ЧП  YANGI DAVR INVEST(Қоғоз қоп)")], profile)
    assert content.startswith(b"\xef\xbb\xbf")
    assert "Қоғоз қоп" in content.decode("utf-8-sig")


def test_cp1251_refuses_instead_of_corrupting(profile):
    cp = dataclasses.replace(profile, encoding="cp1251")
    with pytest.raises(EncodingProblem, match="Қ"):
        build_csv([sample_doc(contractor="Қоғоз қоп")], cp)
    assert "Тошматов" in build_csv([sample_doc()], cp).decode("cp1251")


def test_values_with_delimiter_and_quotes_survive(profile):
    doc = sample_doc(contractor='"AVTOTRAK" MCHJ; filial', comment="birinchi\nikkinchi")
    row = dict(zip(*parse(build_csv([doc], profile))[:2]))
    assert row["Контрагент"] == '"AVTOTRAK" MCHJ; filial'
    assert row["Комментарий"] == "birinchi\nikkinchi"


@pytest.mark.parametrize("text", ["=1+1", "+998", "@SUM(A1)"])
def test_formula_like_text_is_neutralised(profile, text):
    doc = sample_doc(comment=text)
    row = dict(zip(*parse(build_csv([doc], profile))[:2]))
    assert row["Комментарий"] == "'" + text


def test_decimal_separator_applies_only_to_fractions(profile):
    comma = dataclasses.replace(profile, decimal_separator=",")
    line = ExportLine(1, "Қум", "т", Decimal("1.500"), Decimal("100000.00"), Decimal("150000.00"))
    row = dict(zip(*parse(build_csv([sample_doc(lines=[line])], comma))[:2]))
    assert (row["Количество"], row["Цена"]) == ("1,5", "100000")


def test_repeat_off_keeps_key_but_blanks_header_fields(profile):
    single = dataclasses.replace(profile, repeat_document_fields=False)
    rows = parse(build_csv([sample_doc()], single))
    second = dict(zip(rows[0], rows[2]))
    assert second["Контрагент"] == "" and second["Сумма документа"] == ""
    assert second["Ключ документа"] == "NK-000001"
    assert second["Материал"] == "Офис дафтари"


def test_load_profile_rejects_unknown_source(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text('[[columns]]\nheader = "X"\nsource = "doc.nope"\n', encoding="utf-8")
    with pytest.raises(ProfileError, match="doc.nope"):
        load_profile(path)


def test_load_profile_rejects_bad_encoding(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text('[format]\nencoding = "nope"\n[[columns]]\nheader = "X"\n', encoding="utf-8")
    with pytest.raises(ProfileError):
        load_profile(path)
