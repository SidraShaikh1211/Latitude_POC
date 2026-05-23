"""Term class / synonym lookup.

Hand-authored dictionary first (deterministic, fast, reviewable), with an
optional LLM fallback (Claude structured-output) for unknown terms.

Covers:
  - Medication class membership (NSAID, muscle relaxant, antidepressant,
    anticonvulsant, opiate, corticosteroid_oral, anticoagulant)
  - ICD-10 conceptual groupings (lumbar_radiculopathy, non_radicular,
    myofascial, herpes_zoster, post_laminectomy, etc.)
  - PT/therapy procedure codes

The Smith case relies on NSAID + radiculopathy + myofascial + (negative)
acetaminophen-not-NSAID, all of which are in the dictionary.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Medication classes (lowercase generic names + common brand names)
# ---------------------------------------------------------------------------

MEDICATION_CLASSES: dict[str, set[str]] = {
    "NSAID": {
        "ibuprofen", "advil", "motrin", "nurofen",
        "naproxen", "aleve", "naprosyn", "anaprox",
        "celecoxib", "celebrex",
        "meloxicam", "mobic",
        "diclofenac", "voltaren", "cataflam", "cambia",
        "indomethacin", "indocin",
        "etodolac", "lodine",
        "piroxicam", "feldene",
        "ketorolac", "toradol",
        "mefenamic acid", "ponstel",
        "nabumetone", "relafen",
        "sulindac", "clinoril",
        "tolmetin", "tolectin",
        "ketoprofen", "orudis", "oruvail",
        "fenoprofen", "nalfon",
        "oxaprozin", "daypro",
        "aspirin", "asa", "acetylsalicylic acid", "bayer", "ecotrin", "bufferin",
        "salsalate", "disalcid",
        "diflunisal", "dolobid",
    },
    "muscle_relaxant": {
        "cyclobenzaprine", "flexeril", "amrix",
        "methocarbamol", "robaxin",
        "tizanidine", "zanaflex",
        "baclofen", "lioresal", "gablofen",
        "carisoprodol", "soma",
        "metaxalone", "skelaxin",
        "orphenadrine", "norflex",
        "chlorzoxazone", "parafon forte",
    },
    "antidepressant": {
        "amitriptyline", "elavil",
        "nortriptyline", "pamelor",
        "duloxetine", "cymbalta",
        "venlafaxine", "effexor",
        "desvenlafaxine", "pristiq",
        "imipramine", "tofranil",
        "doxepin", "sinequan", "silenor",
        "milnacipran", "savella",
    },
    "anticonvulsant": {
        "gabapentin", "neurontin", "gralise",
        "pregabalin", "lyrica",
        "carbamazepine", "tegretol",
        "oxcarbazepine", "trileptal",
        "topiramate", "topamax",
        "lamotrigine", "lamictal",
        "valproate", "depakote", "depakene",
    },
    "opiate": {
        "oxycodone", "oxycontin", "roxicodone", "percocet",
        "hydrocodone", "vicodin", "norco", "lortab",
        "morphine", "ms contin", "kadian",
        "tramadol", "ultram", "conzip",
        "codeine",
        "fentanyl", "duragesic", "actiq", "sublimaze",
        "hydromorphone", "dilaudid", "exalgo",
        "oxymorphone", "opana",
        "tapentadol", "nucynta",
        "methadone", "dolophine",
        "buprenorphine", "subutex", "suboxone", "belbuca", "butrans",
    },
    "corticosteroid_oral": {
        "prednisone", "deltasone",
        "prednisolone", "orapred", "millipred",
        "methylprednisolone", "medrol", "solu-medrol",
        "dexamethasone", "decadron",
        "hydrocortisone", "cortef",
        "triamcinolone",
    },
    "anticoagulant": {
        "warfarin", "coumadin", "jantoven",
        "apixaban", "eliquis",
        "rivaroxaban", "xarelto",
        "dabigatran", "pradaxa",
        "edoxaban", "savaysa",
        "heparin",
        "enoxaparin", "lovenox",
        "dalteparin", "fragmin",
        "fondaparinux", "arixtra",
        "clopidogrel", "plavix",
        "ticagrelor", "brilinta",
        "prasugrel", "effient",
        "dipyridamole", "persantine", "aggrenox",
        "argatroban",
        "bivalirudin", "angiomax",
    },
}

# Explicit non-members (key negatives the Smith case depends on)
MEDICATION_NON_MEMBERS: dict[str, set[str]] = {
    "NSAID": {"acetaminophen", "tylenol", "paracetamol", "panadol"},
}


# ---------------------------------------------------------------------------
# ICD-10 conceptual groupings (glob-style patterns)
# ---------------------------------------------------------------------------

ICD10_CLASSES: dict[str, list[str]] = {
    "lumbar_radiculopathy": [
        "M54.1*", "M54.16", "M54.17",      # radiculopathy
        "M54.3*", "M54.4*",                # sciatica / lumbago with sciatica
        "M51.1*",                          # lumbar disc disorder with radiculopathy
        "G54.4", "G54.5",                  # lumbosacral root disorders
    ],
    "cervical_radiculopathy": [
        "M54.12", "M54.13",
        "M50.1*",                          # cervical disc with radiculopathy
        "G54.2", "G54.3",
    ],
    "neurogenic_claudication": [
        "M48.06*",                         # lumbar/lumbosacral spinal stenosis
    ],
    "non_radicular_back_pain": [
        "M54.5",   "M54.50", "M54.59",     # low back pain (axial)
        "M54.9",                           # dorsalgia, unspecified
        "M54.89",                          # other dorsalgia
    ],
    "myofascial_pain": [
        "M79.1*",                          # myalgia (myofascial)
        "M79.7",                           # fibromyalgia
    ],
    "herpes_zoster": [
        "B02.*",                           # herpes zoster + complications
    ],
    "post_laminectomy_syndrome": [
        "M96.1*",
    ],
    "spinal_tumor": [
        "C41.*", "D32.*", "C72.0",
    ],
    "cauda_equina_syndrome": [
        "G83.4",
    ],
    "spinal_cord_compression": [
        "G95.2*", "G95.20",
    ],
    "pregnancy": [
        "Z34.*", "Z33.*", "O09.*", "O0*",
    ],
}


# ---------------------------------------------------------------------------
# PT / therapy CPT codes (for conservative-therapy evaluation)
# ---------------------------------------------------------------------------

THERAPY_CPT_CODES: dict[str, str] = {
    "97110": "Therapeutic exercise",
    "97112": "Neuromuscular reeducation",
    "97140": "Manual therapy",
    "97530": "Therapeutic activities",
    "97161": "PT evaluation, low complexity",
    "97162": "PT evaluation, moderate complexity",
    "97163": "PT evaluation, high complexity",
    "97164": "PT re-evaluation",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class TermClassResult:
    term: str
    matched_class: str | None
    in_class: bool
    confidence: float           # 1.0 for dict hits, lower for LLM fallback
    source: str                 # "dictionary" or "llm_fallback" or "unknown"
    explanation: str = ""


def _norm_med(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip().lower())


def _icd_matches_pattern(code: str, pattern: str) -> bool:
    """Match an ICD-10 code against a glob pattern (M54.* etc.)."""
    return fnmatch.fnmatchcase(code.upper(), pattern.upper())


def lookup_medication_class(med_name: str, target_class: str) -> TermClassResult:
    """Is `med_name` in the `target_class` medication class?"""
    name = _norm_med(med_name)
    target = target_class.strip()

    if target in MEDICATION_NON_MEMBERS and name in MEDICATION_NON_MEMBERS[target]:
        return TermClassResult(
            term=med_name, matched_class=target, in_class=False,
            confidence=1.0, source="dictionary",
            explanation=f"{med_name} is explicitly NOT in the {target} class (e.g., acetaminophen ≠ NSAID).",
        )

    members = MEDICATION_CLASSES.get(target, set())
    if name in members:
        return TermClassResult(
            term=med_name, matched_class=target, in_class=True,
            confidence=1.0, source="dictionary",
            explanation=f"{med_name} is in the dictionary for {target}.",
        )
    return TermClassResult(
        term=med_name, matched_class=None, in_class=False,
        confidence=0.0, source="unknown",
        explanation=f"{med_name} not found in {target} dictionary.",
    )


def classify_medication(med_name: str) -> list[str]:
    """Return all medication classes the name belongs to (case where the
    same drug fits multiple classes — rare but possible)."""
    name = _norm_med(med_name)
    hits: list[str] = []
    for cls, members in MEDICATION_CLASSES.items():
        if name in members:
            hits.append(cls)
    return hits


def lookup_icd10_class(code: str, target_class: str) -> TermClassResult:
    """Is `code` in the `target_class` ICD-10 grouping?"""
    code_u = code.strip().upper()
    patterns = ICD10_CLASSES.get(target_class, [])
    for pat in patterns:
        if _icd_matches_pattern(code_u, pat):
            return TermClassResult(
                term=code, matched_class=target_class, in_class=True,
                confidence=1.0, source="dictionary",
                explanation=f"{code} matches pattern {pat} in {target_class}.",
            )
    return TermClassResult(
        term=code, matched_class=None, in_class=False,
        confidence=1.0, source="dictionary",
        explanation=f"{code} does not match any pattern in {target_class}.",
    )


def classify_icd10(code: str) -> list[str]:
    """Return all conceptual groupings the ICD-10 code belongs to."""
    code_u = code.strip().upper()
    hits: list[str] = []
    for cls, patterns in ICD10_CLASSES.items():
        for pat in patterns:
            if _icd_matches_pattern(code_u, pat):
                hits.append(cls)
                break
    return hits


# ---------------------------------------------------------------------------
# LLM fallback (used when dictionary returns "unknown")
# ---------------------------------------------------------------------------

class _TermClassLLMResponse(BaseModel):
    in_class: bool = Field(description="True if the term is in the target class.")
    rationale: str = Field(description="One-sentence explanation.")
    canonical_class_if_different: str | None = Field(
        default=None,
        description="If the term belongs to a different recognized class, name it; otherwise null.",
    )


async def lookup_with_llm_fallback(
    term: str,
    target_class: str,
    domain: str = "medication",
) -> TermClassResult:
    """Dictionary first; if unknown, ask Claude.

    `domain`: "medication" or "icd10" (controls system prompt).
    """
    # Dictionary attempt
    if domain == "medication":
        dict_result = lookup_medication_class(term, target_class)
    else:
        dict_result = lookup_icd10_class(term, target_class)
    if dict_result.source == "dictionary":
        return dict_result

    # LLM fallback (only for medication unknowns — ICD-10 dictionary is authoritative)
    if domain != "medication":
        return dict_result

    from app.llm.client import get_client
    client = get_client()
    system = (
        "You are a clinical pharmacology classifier. Given a medication name "
        "(generic or brand) and a target drug class, return whether the medication "
        "is a member of that class. Be strict: e.g., acetaminophen is NOT an NSAID. "
        "Recognized classes include: NSAID, muscle_relaxant, antidepressant, "
        "anticonvulsant, opiate, corticosteroid_oral, anticoagulant."
    )
    user = f"Term: {term}\nTarget class: {target_class}\nIs this term in the target class?"
    try:
        result = await client.structured_output(
            system=system, user=user, schema=_TermClassLLMResponse, max_tokens=256
        )
        parsed: _TermClassLLMResponse = result.parsed  # type: ignore[assignment]
        return TermClassResult(
            term=term,
            matched_class=target_class if parsed.in_class else parsed.canonical_class_if_different,
            in_class=parsed.in_class,
            confidence=0.7,
            source="llm_fallback",
            explanation=parsed.rationale,
        )
    except Exception as e:
        return TermClassResult(
            term=term, matched_class=None, in_class=False,
            confidence=0.0, source="llm_fallback_error",
            explanation=f"LLM fallback failed: {e}",
        )


def is_therapy_cpt(code: str) -> bool:
    return code.strip() in THERAPY_CPT_CODES
