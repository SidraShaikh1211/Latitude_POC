from app.determination.decider import decide


def test_clean_approve():
    d = decide(root_verdict="met", exclusion_verdicts={})
    assert d.outcome == "approve"


def test_clean_deny_not_met():
    d = decide(root_verdict="not_met", exclusion_verdicts={})
    assert d.outcome == "deny"


def test_pend_on_unclear():
    d = decide(root_verdict="unclear", exclusion_verdicts={})
    assert d.outcome == "pend"


def test_pend_on_not_documented():
    d = decide(root_verdict="not_documented", exclusion_verdicts={})
    assert d.outcome == "pend"


def test_exclusion_short_circuits_to_deny_even_when_met():
    d = decide(
        root_verdict="met",
        exclusion_verdicts={"X1": "met"},
    )
    assert d.outcome == "deny"
    assert "X1" in d.triggered_exclusions


def test_exclusion_unclear_does_not_fire():
    d = decide(
        root_verdict="met",
        exclusion_verdicts={"X1": "unclear"},
    )
    assert d.outcome == "approve"
    assert d.triggered_exclusions == []


def test_escalation_overrides_everything():
    d = decide(
        root_verdict="met",
        exclusion_verdicts={},
        escalations=["adjudicator requested human review on conservative_therapy"],
    )
    assert d.outcome == "needs_human_review"
    assert d.escalation_reasons


def test_multiple_exclusions_listed():
    d = decide(
        root_verdict="met",
        exclusion_verdicts={"X1": "met", "X2": "met", "X3": "not_met"},
    )
    assert d.outcome == "deny"
    assert set(d.triggered_exclusions) == {"X1", "X2"}
