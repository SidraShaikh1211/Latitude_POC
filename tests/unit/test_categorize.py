"""Tests for app/pas/categorize.py — the shared CPT→request_category
inference used by both the bundle constructor (write side) and the bundle
parser (read-side fallback)."""

import pytest

from app.pas.categorize import (
    ALLOWED_CATEGORIES,
    SNOMED_TO_CATEGORY,
    infer_request_category,
)


@pytest.mark.parametrize("cpt,expected", [
    ("58150", "surgical"),     # hysterectomy — gyn surgery range
    ("58999", "surgical"),     # upper bound of gyn range
    ("58000", "surgical"),     # lower bound of gyn range
    ("22612", "surgical"),     # spine fusion
    ("27447", "surgical"),     # total knee
    ("47562", "surgical"),     # laparoscopic chole — open abdominal range
])
def test_surgical_cpt_ranges_map_to_surgical(cpt, expected):
    assert infer_request_category(cpt) == expected


@pytest.mark.parametrize("cpt", [
    "62323",       # lumbar ESI — procedural injection
    "99213",       # E&M
    "70551",       # MRI brain
    "21999",       # one short of 22000 surgical range
    "59000",       # one above 58999 gyn range
])
def test_non_surgical_numeric_cpt_defaults_to_procedural(cpt):
    assert infer_request_category(cpt) == "procedural"


@pytest.mark.parametrize("code", [
    "J3490",           # generic J-code unclassified drug
    "J2350",           # ocrelizumab
    "tirzepatide",     # drug-name code
    "Zepbound",        # brand-name code
])
def test_pharmacy_detection_via_jcode_or_drug_name(code):
    assert infer_request_category(code) == "pharmacy"


def test_empty_or_blank_cpt_defaults_to_procedural():
    assert infer_request_category("") == "procedural"
    assert infer_request_category("   ") == "procedural"


def test_allowed_categories_covers_inferrer_outputs():
    # Any value the inferrer can return must be a legal selector enum.
    for cpt in ("58150", "62323", "J3490", "Zepbound", "", "garbage-text"):
        assert infer_request_category(cpt) in ALLOWED_CATEGORIES


def test_snomed_table_only_maps_to_allowed_categories():
    for code, cat in SNOMED_TO_CATEGORY.items():
        assert cat in ALLOWED_CATEGORIES, f"{code} maps to unknown {cat}"
