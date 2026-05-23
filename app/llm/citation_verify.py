"""Citation verification: substring-match a quote against source text with
normalized whitespace and stripped zero-width characters.

This module is the trust statement of the whole system: every policy citation
(policy-side) and every patient-evidence citation (patient-side) must pass this
check, or the verdict is dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_ZERO_WIDTH = re.compile(r"[​‌‍﻿]")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Strip zero-width chars and collapse all whitespace runs to single spaces."""
    text = _ZERO_WIDTH.sub("", text)
    return _WS.sub(" ", text).strip()


@dataclass
class CitationCheck:
    found: bool
    normalized_quote: str
    start_offset: int | None
    end_offset: int | None
    diagnostic: str | None = None


def verify_substring(quote: str, source_text: str) -> CitationCheck:
    """Verify that `quote` appears (after normalization) inside `source_text`.

    Returns a CitationCheck with start/end offsets computed against the
    *normalized* source text. If the substring is not found, returns
    `found=False` with a short diagnostic explaining the nearest partial match.
    """
    n_quote = normalize(quote)
    n_source = normalize(source_text)

    if not n_quote:
        return CitationCheck(False, n_quote, None, None, "empty quote")

    idx = n_source.find(n_quote)
    if idx >= 0:
        return CitationCheck(True, n_quote, idx, idx + len(n_quote))

    # Compute a useful diagnostic: how far we got with prefix matching.
    for size in (80, 60, 40, 20):
        head = n_quote[:size]
        if head and head in n_source:
            return CitationCheck(
                False, n_quote, None, None,
                f"first {size} chars matched but full quote did not; check after char {size}",
            )
    return CitationCheck(False, n_quote, None, None, "no substring overlap found")
