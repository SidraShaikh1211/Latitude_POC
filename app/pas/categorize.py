"""Shared request_category vocabulary for the PAS Bundle constructor and
parser. The constructor writes this enum into the wire format; the parser
reads it back and uses CPT-derived inference as a fallback when the sender
omits or uses a vocabulary we don't recognize.

Keeping both sides on the same inference function means a round-trip
through wire format is identity-preserving for any CPT the constructor
understands.
"""

from __future__ import annotations


# The five values the policy selector knows how to filter on.
ALLOWED_CATEGORIES: frozenset[str] = frozenset({
    "pharmacy", "dme", "service", "procedural", "surgical",
})


# CPT ranges that imply "surgical" rather than "procedural". The CPT
# surgery section is 10000-69999, but the system's PA scope only
# intersects with a narrow slice — keep this list explicit so a new
# surgical PA doesn't silently slip through as procedural.
_SURGICAL_CPT_RANGES: tuple[tuple[int, int], ...] = (
    (22000, 22999),   # spine surgery (laminectomy, fusion)
    (27000, 27999),   # orthopedic — hip/knee
    (47000, 47999),   # open abdominal
    (58000, 58999),   # gynecologic surgery (hysterectomy etc.)
)


# SNOMED codes the constructor emits on ServiceRequest.category[].coding.
# Real EHRs that follow SNOMED conventions but don't populate `.text` with
# our enum string still get mapped correctly through this table.
SNOMED_TO_CATEGORY: dict[str, str] = {
    "387713003": "surgical",       # Surgical procedure
    "103693007": "procedural",     # Diagnostic procedure
    "440655000": "pharmacy",       # Outpatient pharmacy service
}


def infer_request_category(cpt_code: str) -> str:
    """Map a CPT/HCPCS code to a request_category the selector understands.

    - "pharmacy"  — HCPCS J-codes or known drug-name codes (e.g. tirzepatide)
    - "surgical"  — CPT in one of the _SURGICAL_CPT_RANGES
    - "procedural" — default for everything else (injections, E&M, imaging)
    """
    code = (cpt_code or "").strip()
    if not code:
        return "procedural"
    # Pharmacy: HCPCS J-code or any non-numeric drug-name code
    if code.startswith("J") or not code[0].isdigit():
        return "pharmacy"
    try:
        num = int(code[:5])
    except ValueError:
        return "procedural"
    for low, high in _SURGICAL_CPT_RANGES:
        if low <= num <= high:
            return "surgical"
    return "procedural"
