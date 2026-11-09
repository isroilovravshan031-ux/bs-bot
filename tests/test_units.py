from datetime import date
from decimal import Decimal

import pytest

from nakladnoy_bot.matching import Named, match_contractor, match_material
from nakladnoy_bot.normalize import norm_item, norm_party
from nakladnoy_bot.numbers import ParseError, fmt, parse_date, parse_number, plain, to_decimal
from nakladnoy_bot.validation import arithmetic_errors

from .conftest import invoice

ITEMS = [
    Named(1, "Тошматов Азиз  (снабженец)", "300000001"),
    Named(2, "ООО Ҳамидов Шерзод (материал)", "300000002"),
    Named(3, '"AVTOTRAK" MCHJ', "300000005"),
    Named(4, "(NANO) ЧП  YANGI DAVR INVEST(Қоғоз қоп)", "300000009"),
    Named(5, "(BS) Nurli Invest Production (Пента)", "300000008"),
]


@pytest.mark.parametrize("text,expected", [
    ("250.000", "250000"), ("250 000", "250000"), ("250,000", "250000"), ("1.250.000", "1250000"),
    ("1,5", "1.5"), ("1.5", "1.5"), ("0.750", "0.750"), ("35000", "35000"), ("1,234.50", "1234.50"),
])
def test_parse_number(text, expected):
    assert parse_number(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "abc", "12a", "-5"])
def test_parse_number_rejects(text):
    with pytest.raises(ParseError):
        parse_number(text)


def test_fmt_and_plain():
    assert fmt(Decimal("720000.00")) == "720 000"
    assert fmt(Decimal("1.500")) == "1.5"
    assert fmt(None) == "?"
    assert plain(Decimal("1E+3")) == "1000"


def test_to_decimal_has_no_float_noise():
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(None) is None
    assert to_decimal(float("nan")) is None


@pytest.mark.parametrize("text,expected", [
    ("03.11.2026", date(2026, 11, 3)), ("3.11.26", date(2026, 11, 3)),
    ("3/11", date(2026, 11, 3)), ("2026-11-03", date(2026, 11, 3)), ("3 11 2026", date(2026, 11, 3)),
])
def test_parse_date(text, expected):
    assert parse_date(text, date(2026, 11, 4)) == expected


@pytest.mark.parametrize("text", ["32.01.2026", "kecha", ""])
def test_parse_date_rejects(text):
    with pytest.raises(ParseError):
        parse_date(text, date(2026, 11, 4))


def test_normalize_unifies_scripts_and_tags():
    assert norm_party("Тошматов Азиз  (снабженец)") == norm_party("Toshmatov Aziz")
    assert norm_party("ООО Ҳамидов Шерзод (материал)") == norm_party("Ҳамидов Шерзод")
    assert norm_item("А4 лист") == norm_item("A4 list")  # kirill А va lotin A
    assert norm_item("Қоғоз") == norm_item("Qog'oz")


def test_match_by_inn_wins():
    m = match_contractor("umuman boshqa", "300000001", ITEMS, {})
    assert (m.id, m.method) == (1, "inn")


@pytest.mark.parametrize("raw", ["Тошматов Азиз", "Toshmatov Aziz", "ТОШМАТОВ АЗИЗ"])
def test_match_exact_across_scripts(raw):
    m = match_contractor(raw, None, ITEMS, {})
    assert (m.id, m.method) == (1, "exact")


def test_match_tolerates_misread_letter_but_flags_fuzzy():
    m = match_contractor("Тошмотов Азиз", None, ITEMS, {})
    assert (m.id, m.method) == (1, "fuzzy")


def test_single_common_word_is_not_auto_matched():
    # 'Invest' ikki kontragentda bor: taxmin qilib bo'lmaydi.
    m = match_contractor("Invest", None, ITEMS, {})
    assert m.id is None
    assert m.candidates


def test_unknown_supplier_gets_no_match():
    m = match_contractor("Каримов Жасур", None, ITEMS, {})
    assert m.id is None


def test_learned_alias_resolves_abbreviation():
    aliases = {norm_party("AVTO TRAK"): 3}
    m = match_contractor("AVTO TRAK", None, ITEMS, aliases)
    assert (m.id, m.method) == (3, "alias")


def test_empty_supplier_is_safe():
    assert match_contractor(None, None, ITEMS, {}).id is None
    assert match_contractor("(  )", None, ITEMS, {}).id is None


def test_material_alias_and_exact():
    materials = [Named(1, "Бумага A4"), Named(2, "Папка офисная")]
    units = {1: "пачка", 2: "шт"}
    hit = match_material("А4 лист", materials, {norm_item("А4 лист"): 1}, units)
    assert (hit.name, hit.method, hit.unit) == ("Бумага A4", "alias", "пачка")
    assert match_material("Папка офисная", materials, {}, units).method == "exact"
    assert match_material("Цемент", materials, {}, units) is None
    assert match_material("Цемент", [], {}, {}) is None


def test_arithmetic_errors_clean_on_sample():
    assert arithmetic_errors(invoice()) == []


def test_arithmetic_errors_catch_bad_line_and_total():
    bad = invoice(lines=[
        {"name": "x", "unit": "шт", "quantity": 4, "price": 35000, "amount": 150000, "confidence": "high"},
    ], total=720000)
    errors = arithmetic_errors(bad)
    assert any("line 1" in e for e in errors)
    assert any("written total" in e for e in errors)
