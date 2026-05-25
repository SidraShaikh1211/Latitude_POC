"""Thin deterministic wrapper around `simple-icd-10-cm`.

Used by the metadata-extraction LLM (`app/extraction/metadata.py`) as a
mandatory tool: before emitting any ICD-10 code, Claude must call `lookup`
to confirm the code is real and copy the canonical description into the
bundle's `display` field. `search_by_term` lets Claude discover candidate
codes for a clinical term ("endometriosis", "type 2 diabetes") rather
than guessing.

The library bundles the official ICD-10-CM tabular list — ~98k codes
with descriptions, hierarchy, and validation. No network, no LLM.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TypedDict

import simple_icd_10_cm as _icd


class LookupResult(TypedDict):
    valid: bool
    code: str
    official_description: str | None


class SearchHit(TypedDict):
    code: str
    description: str


def lookup(code: str) -> LookupResult:
    """Confirm an ICD-10-CM code and return its canonical description."""
    code = (code or "").strip()
    if not code:
        return {"valid": False, "code": code, "official_description": None}
    try:
        if _icd.is_valid_item(code):
            return {
                "valid": True,
                "code": code,
                "official_description": _icd.get_description(code),
            }
    except Exception:
        pass
    return {"valid": False, "code": code, "official_description": None}


def search_by_term(term: str, *, limit: int = 25) -> list[SearchHit]:
    """Return codes whose official description contains `term` (case-insensitive).

    Results are ordered shortest-description-first as a cheap proxy for
    "least specific / most general first" — the LLM should normally pick a
    more specific match if the chart documents body site or laterality.
    """
    term = (term or "").strip().lower()
    if not term:
        return []
    hits: list[tuple[int, str, str]] = []
    for code, desc in _all_code_descriptions():
        if term in desc.lower():
            hits.append((len(desc), code, desc))
            if len(hits) > limit * 4:  # collect a few extra before sorting
                break
    hits.sort(key=lambda x: (x[0], x[1]))
    return [{"code": c, "description": d} for _, c, d in hits[:limit]]


@lru_cache(maxsize=1)
def _all_code_descriptions() -> tuple[tuple[str, str], ...]:
    """One-time materialization of (code, description) for every leaf code.

    `get_all_codes` returns chapters/blocks too; we keep them because
    chapter-level descriptions are useful for broad search terms, but
    skip empty descriptions defensively.
    """
    out: list[tuple[str, str]] = []
    for code in _icd.get_all_codes():
        try:
            desc = _icd.get_description(code)
        except Exception:
            continue
        if desc:
            out.append((code, desc))
    return tuple(out)
