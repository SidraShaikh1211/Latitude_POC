from fastapi import APIRouter, HTTPException

from app.policy.registry import get_registry, iter_leaves


router = APIRouter(prefix="/v1/policies", tags=["policies"])


@router.get("")
def list_policies() -> list[dict]:
    out = []
    for p in get_registry().all_policies():
        out.append({
            "policy_id": p.policy_id,
            "name": p.name,
            "payer_id": p.payer_id,
            "version": p.version,
            "effective_from": p.effective_from,
            "effective_until": p.effective_until,
            "cpt_codes": p.applies_to.cpt_codes,
            "lines_of_business": p.applies_to.lines_of_business,
            "states": p.applies_to.states,
            "leaf_count": sum(1 for _ in iter_leaves(p.criteria)),
            "exclusion_count": len(p.exclusions),
        })
    return out


@router.get("/{policy_id}")
def get_policy(policy_id: str) -> dict:
    p = get_registry().get(policy_id)
    if not p:
        raise HTTPException(status_code=404, detail="Policy not found")
    return {
        "policy_id": p.policy_id,
        "name": p.name,
        "payer_id": p.payer_id,
        "version": p.version,
        "effective_from": p.effective_from,
        "effective_until": p.effective_until,
        "applies_to": {
            "cpt_codes": p.applies_to.cpt_codes,
            "icd10_patterns": p.applies_to.icd10_patterns,
            "lines_of_business": p.applies_to.lines_of_business,
            "states": p.applies_to.states,
            "age_min": p.applies_to.age_min,
            "settings_of_care": p.applies_to.settings_of_care,
        },
        "criteria": _serialize_node(p.criteria),
        "exclusions": [
            {"id": e.id, "description": e.description,
             "policy_citation": {"page": e.policy_citation.page, "section": e.policy_citation.section,
                                 "quote": e.policy_citation.quote}}
            for e in p.exclusions
        ],
        "metadata": p.metadata,
    }


def _serialize_node(node) -> dict:
    from app.policy.registry import CriterionLeaf
    if isinstance(node, CriterionLeaf):
        return {
            "id": node.id, "type": "leaf",
            "description": node.description,
            "policy_citation": {
                "page": node.policy_citation.page,
                "section": node.policy_citation.section,
                "quote": node.policy_citation.quote,
            },
            "evaluation": node.evaluation,
            "verdict_rubric": node.verdict_rubric,
        }
    return {
        "id": node.id, "type": "internal",
        "operator": node.operator,
        "description": node.description,
        "policy_citation": (
            {
                "page": node.policy_citation.page,
                "section": node.policy_citation.section,
                "quote": node.policy_citation.quote,
            }
            if node.policy_citation else None
        ),
        "children": [_serialize_node(c) for c in node.children],
    }
