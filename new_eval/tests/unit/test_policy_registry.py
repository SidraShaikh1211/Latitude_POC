import pytest

from app.policy.registry import (
    PolicyLoadError,
    PolicyRegistry,
    iter_leaves,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_registry()
    yield
    reset_registry()


def test_loads_molina_policy_cleanly():
    reg = PolicyRegistry()
    reg.load_dir()
    policies = reg.all_policies()
    assert len(policies) == 1
    p = policies[0]
    assert p.policy_id == "molina-mcp-032"
    assert p.payer_id == "molina"
    assert "62323" in p.applies_to.cpt_codes
    assert "medicaid" in p.applies_to.lines_of_business
    assert "NY" in p.applies_to.states


def test_indexes_by_cpt():
    reg = PolicyRegistry()
    reg.load_dir()
    matches = reg.policies_covering_cpt("62323")
    assert len(matches) == 1
    assert matches[0].policy_id == "molina-mcp-032"
    assert reg.policies_covering_cpt("99999") == []


def test_indexes_by_payer():
    reg = PolicyRegistry()
    reg.load_dir()
    assert len(reg.policies_for_payer("molina")) == 1
    assert reg.policies_for_payer("aetna") == []


def test_leaf_count_and_exclusion_count():
    reg = PolicyRegistry()
    reg.load_dir()
    policy = reg.get("molina-mcp-032")
    assert policy is not None
    leaves = list(iter_leaves(policy.criteria))
    assert len(leaves) >= 22, f"expected ≥22 tree leaves, got {len(leaves)}"
    assert len(policy.exclusions) == 10


def test_offsets_populated_after_verification():
    reg = PolicyRegistry()
    reg.load_dir()
    policy = reg.get("molina-mcp-032")
    leaves = list(iter_leaves(policy.criteria))
    for leaf in leaves:
        cit = leaf.policy_citation
        assert cit.start_offset is not None, f"leaf {leaf.id} missing start_offset"
        assert cit.end_offset is not None
        assert cit.end_offset > cit.start_offset


def test_malformed_policy_raises(tmp_path, monkeypatch):
    from app.policy import registry as reg_module
    bad = tmp_path / "bad-policy.json"
    bad.write_text('{"policy_id": "bad", "name": "x", "payer_id": "x", "version": "1", '
                   '"effective_from": "2024-01-01", "effective_until": null, '
                   '"source": {"pdf_path": "nonexistent.pdf"}, '
                   '"applies_to": {}, "criteria": {"id": "root", "type": "leaf", '
                   '"description": "", "policy_citation": {"page": 1, "quote": "xyz"}, '
                   '"evaluation": {}, "verdict_rubric": {}}, "exclusions": []}')
    reg = reg_module.PolicyRegistry()
    monkeypatch.setattr(reg_module.settings, "policies_dir", tmp_path)
    with pytest.raises(PolicyLoadError):
        reg.load_dir()


def test_citation_failure_raises(tmp_path, monkeypatch):
    """If a quote does not substring-match its cited page, registry rejects the policy."""
    from app.policy import registry as reg_module
    import shutil
    # Build a policy that points to the real PDF but with a bogus quote
    pdf_src = reg_module.settings.policies_dir / "sources" / "molina-mcp-032.pdf"
    pdf_dst = tmp_path / "src.pdf"
    shutil.copy(pdf_src, pdf_dst)
    bad = tmp_path / "bad-policy.json"
    bad.write_text(
        '{"policy_id": "bad-cit", "name": "x", "payer_id": "x", "version": "1", '
        '"effective_from": "2024-01-01", "effective_until": null, '
        f'"source": {{"pdf_path": "{pdf_dst}", "total_pages": 8}}, '
        '"applies_to": {}, "criteria": {"id": "root", "type": "leaf", '
        '"description": "", "policy_citation": {"page": 1, "quote": "this quote does not appear anywhere in the source pdf at all"}, '
        '"evaluation": {}, "verdict_rubric": {}}, "exclusions": []}'
    )
    reg = reg_module.PolicyRegistry()
    monkeypatch.setattr(reg_module.settings, "policies_dir", tmp_path)
    with pytest.raises(PolicyLoadError, match="citation"):
        reg.load_dir()
