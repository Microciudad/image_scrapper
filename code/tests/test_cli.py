"""Tests for cover_fetcher.cli helper field extraction."""

from __future__ import annotations

from cover_fetcher.cli import _extract_query_fields


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
