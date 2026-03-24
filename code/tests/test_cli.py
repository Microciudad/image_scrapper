"""Tests for cover_fetcher.cli helper field extraction."""

from __future__ import annotations

import logging
import pytest
import typer

from cover_fetcher.cli import (
    _extract_country_value,
    _extract_query_fields,
    _find_existing_image_filename,
    _get_row_image_value,
    _resolve_captcha_cooldown_bounds,
    _translate_country,
)
from cover_fetcher import csv_handler


def test_extract_query_fields_uses_only_expected_headers():
    row = {
        "Band": "2 Lux 25",
        "Title": "Go !!! / Give Yourself",
        "year": "91",
        "FORMAT": "12",
        "Label": "Extreme",
        "RC": "EX",
        "PC C": "EX",
        "ED": "UK",
        "Precio": "4",
    }

    result = _extract_query_fields(row)
    assert result == ("2 Lux 25", "Go !!! / Give Yourself", "91", "12", "Extreme")


def test_extract_query_fields_parses_malformed_packed_row_without_extra_columns():
    row = {
        "Band;Title;;year;FORMAT;Label;RC;PC C;ED;Precio;Estilo;Sub Stylo;image,image_file;image_file": (
            "2 Lux 25;Go !!! / Give Yourself;;91;12;Extreme;EX;EX;UK;4;Electronic;Dance;,"
        ),
        "image_file": ";;;;;;;;;;;;;",
    }

    result = _extract_query_fields(row)
    assert result == ("2 Lux 25", "Go !!! / Give Yourself", "91", "12", "Extreme")


def test_extract_query_fields_clamps_leaky_columns():
    row = {
        "Band": "2 Lux 25",
        "Title": "Go !!! / Give Yourself",
        "year": "91",
        "FORMAT": '"12"',
        "Label": "Extreme;EX;EX;UK;4;Electronic;Dance",
    }

    result = _extract_query_fields(row)
    assert result == ("2 Lux 25", "Go !!! / Give Yourself", "91", '"12"', "Extreme")


def test_get_row_image_value_prefers_csv_column():
    row = {"image_file": "cover.jpg", "Image": "other.jpg"}
    assert _get_row_image_value(row) == "cover.jpg"


def test_get_row_image_value_reads_excel_column():
    row = {"Image": "cover_xlsx.jpg"}
    assert _get_row_image_value(row) == "cover_xlsx.jpg"


def test_build_image_filename_not_meaningful_when_all_parts_empty():
    assert csv_handler.build_image_filename("", "", "") == ".jpg"


def test_resolve_captcha_cooldown_bounds_from_range():
    assert _resolve_captcha_cooldown_bounds("1-50", 5, 25) == (1, 50)


def test_resolve_captcha_cooldown_bounds_rejects_bad_range():
    with pytest.raises(typer.BadParameter):
        _resolve_captcha_cooldown_bounds("50-1", 5, 25)


def test_find_existing_image_filename_prefers_no_format_name():
    existing = {
        "Martika_Toy_Soldiers.jpg",
        "Martika_Toy_Soldiers_12.jpg",
    }
    matched = _find_existing_image_filename(existing, "Martika", "Toy Soldiers", "12")
    assert matched == "Martika_Toy_Soldiers.jpg"


def test_find_existing_image_filename_falls_back_to_format_name():
    existing = {
        "Martika_Toy_Soldiers_12.jpg",
    }
    matched = _find_existing_image_filename(existing, "Martika", "Toy Soldiers", "12")
    assert matched == "Martika_Toy_Soldiers_12.jpg"


def test_find_existing_image_filename_still_prefers_no_format_when_both_exist():
    existing = {
        "Martika_Toy_Soldiers.jpg",
        "Martika_Toy_Soldiers_12.jpg",
        "Martika_Toy_Soldiers_12_2.jpg",
    }
    matched = _find_existing_image_filename(existing, "Martika", "Toy Soldiers", "12")
    assert matched == "Martika_Toy_Soldiers.jpg"


def test_translate_country_returns_empty_for_unmapped_values():
    assert _translate_country("ZZ", {"SP": "Spain"}) == ""


def test_extract_country_value_reads_from_packed_semicolon_row():
    row = {
        "Band;Title;;year;FORMAT;Label;RC;PC C;ED;Precio": "A;B;;91;12;L;EX;EX;SP;4",
    }
    assert _extract_country_value(row, "ED") == "SP"


def test_country_equivalences_settings_available():
    from cover_fetcher.settings import COUNTRY_EQUIVALENCES
    assert COUNTRY_EQUIVALENCES["SP"] == "Spain"
    assert COUNTRY_EQUIVALENCES["DE"] == "Germany"
    assert COUNTRY_EQUIVALENCES["UK"] == "England"

