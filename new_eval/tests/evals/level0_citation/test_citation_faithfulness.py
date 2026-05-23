"""Level 0 — Citation faithfulness.

Hard contract: every policy_citation in every loaded policy must
substring-verify against its source PDF after whitespace normalization.

Target: 100%. Any non-100% is a P0 build break.

This duplicates the verification the registry does at load time, but
promotes it to an explicit CI gate so a regression is visible in test
output (not buried in a startup exception).
"""

import pymupdf
import pytest

from app.llm.citation_verify import normalize, verify_substring
from app.policy.registry import PolicyRegistry, iter_leaves, reset_registry


@pytest.fixture(scope="module")
def registry() -> PolicyRegistry:
    reset_registry()
    r = PolicyRegistry()
    r.load_dir()
    return r


def _collect_citations(reg: PolicyRegistry):
    out = []
    for p in reg.all_policies():
        # Tree citations
        def visit(node, path):
            cit = getattr(node, "policy_citation", None)
            if cit is not None:
                out.append((p.policy_id, "tree", path, cit))
            if hasattr(node, "children"):
                for c in node.children:
                    visit(c, f"{path}>{c.id}")
        visit(p.criteria, p.criteria.id)
        # Exclusions
        for ex in p.exclusions:
            out.append((p.policy_id, "exclusion", ex.id, ex.policy_citation))
    return out


def test_level0_every_policy_citation_substring_verifies(registry):
    cits = _collect_citations(registry)
    assert cits, "no citations collected — policy loader broken?"

    failures = []
    for policy_id, kind, path, cit in cits:
        policy = registry.get(policy_id)
        page_text = policy.page_text(cit.page)
        check = verify_substring(cit.quote, page_text)
        if not check.found:
            failures.append({
                "policy_id": policy_id,
                "kind": kind,
                "path": path,
                "page": cit.page,
                "quote": cit.quote[:80],
                "diagnostic": check.diagnostic,
            })

    pass_count = len(cits) - len(failures)
    pass_rate = pass_count / len(cits) if cits else 0.0

    # Print the metric so it shows up in pytest -v output
    print(f"\n[L0 citation faithfulness] {pass_count}/{len(cits)} = {pass_rate:.1%}")
    assert pass_rate == 1.0, (
        f"Citation faithfulness {pass_rate:.1%} < 100% (target). "
        f"{len(failures)} failure(s):\n"
        + "\n".join(f"  {f}" for f in failures[:10])
    )


def test_level0_every_citation_has_offsets_set_after_load(registry):
    """Confirm the registry populated start/end offsets so the UI can highlight
    the source span without recomputing."""
    misses = []
    for p in registry.all_policies():
        for leaf in iter_leaves(p.criteria):
            cit = leaf.policy_citation
            if cit.start_offset is None or cit.end_offset is None:
                misses.append(f"{p.policy_id}>{leaf.id} p.{cit.page}")
        for ex in p.exclusions:
            cit = ex.policy_citation
            if cit.start_offset is None or cit.end_offset is None:
                misses.append(f"{p.policy_id}>{ex.id} p.{cit.page}")
    assert not misses, f"{len(misses)} citations missing offsets after load: {misses[:5]}"


def test_level0_normalize_idempotent():
    """normalize(normalize(x)) == normalize(x) — a property the registry relies on."""
    samples = [
        "hello\nworld",
        "  spaces  ",
        "with​zero-width",
        "abc   def\n\nghi",
    ]
    for s in samples:
        n1 = normalize(s)
        n2 = normalize(n1)
        assert n1 == n2


def test_level0_normalize_does_not_lose_essential_chars():
    """normalize must preserve punctuation, parentheses, and digits."""
    raw = "(Greater than4 on the Numerical Rating Scale Pain Rating Scale*)"
    n = normalize(raw)
    assert "(" in n and ")" in n and "4" in n and "*" in n
