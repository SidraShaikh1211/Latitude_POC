"""Policy registry: loads JSON policy files at startup, validates them
against a Pydantic schema, and substring-verifies every policy_citation
against the source PDF text.

Failure mode is loud: any malformed policy raises PolicyLoadError at load
time, so a broken policy is a startup failure (caught in CI), not a silent
runtime degradation.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from app.llm.citation_verify import normalize, verify_substring
from app.settings import settings


class PolicyLoadError(Exception):
    pass


VALID_OPERATORS = {"ALL", "ONE_OF", "NOT", "AT_LEAST_K"}
VALID_VERDICTS = {"met", "not_met", "unclear", "not_documented"}


@dataclass
class PolicyCitation:
    page: int
    section: str | None
    quote: str
    start_offset: int | None = None  # filled in by registry after verification
    end_offset: int | None = None


@dataclass
class CriterionLeaf:
    id: str
    type: str  # "leaf"
    description: str
    policy_citation: PolicyCitation
    evaluation: dict[str, Any]
    verdict_rubric: dict[str, str]


@dataclass
class CriterionNode:
    id: str
    type: str  # "internal"
    operator: str
    description: str
    children: list["CriterionNode | CriterionLeaf"]
    policy_citation: PolicyCitation | None = None
    k: int | None = None  # for AT_LEAST_K


@dataclass
class Exclusion:
    id: str
    description: str
    policy_citation: PolicyCitation
    evaluation: dict[str, Any]
    verdict_rubric: dict[str, str]


@dataclass
class AppliesTo:
    cpt_codes: list[str]
    hcpcs_codes: list[str]
    icd10_patterns: list[str]
    lines_of_business: list[str]
    states: list[str]
    age_min: int | None
    age_max: int | None
    settings_of_care: list[str]
    request_categories: list[str]
    branches: dict[str, str]
    payer_id: str = ""


@dataclass
class Policy:
    policy_id: str
    name: str
    payer_id: str
    version: str
    effective_from: str
    effective_until: str | None
    source_pdf_path: Path
    source_total_pages: int
    applies_to: AppliesTo
    criteria: CriterionNode
    exclusions: list[Exclusion]
    metadata: dict[str, Any] = field(default_factory=dict)
    _page_texts: dict[int, str] = field(default_factory=dict, repr=False)

    def page_text(self, page: int) -> str:
        return self._page_texts.get(page, "")


class PolicyRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, Policy] = {}
        self._by_cpt: dict[str, list[Policy]] = defaultdict(list)
        self._by_payer: dict[str, list[Policy]] = defaultdict(list)

    def load_dir(self, directory: Path | None = None) -> None:
        directory = directory or settings.policies_dir
        json_files = sorted(p for p in directory.glob("*.json") if p.is_file())
        if not json_files:
            raise PolicyLoadError(f"No policy JSON files found in {directory}")
        for path in json_files:
            policy = self._load_one(path)
            self._index(policy)

    def _load_one(self, path: Path) -> Policy:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise PolicyLoadError(f"{path.name}: invalid JSON: {e}") from e

        # Resolve source PDF path (relative to project root if not absolute)
        src = data.get("source") or {}
        pdf_path = Path(src.get("pdf_path", ""))
        if not pdf_path.is_absolute():
            from app.settings import PROJECT_ROOT
            pdf_path = (PROJECT_ROOT / pdf_path).resolve()
        if not pdf_path.exists():
            raise PolicyLoadError(
                f"{path.name}: source PDF not found at {pdf_path}"
            )

        # Read all pages once and keep normalized text for verification
        page_texts: dict[int, str] = {}
        with pymupdf.open(pdf_path) as doc:
            for i in range(len(doc)):
                page_texts[i + 1] = normalize(doc[i].get_text())

        applies_to = _build_applies_to(data.get("applies_to", {}), data.get("payer_id", ""))

        criteria = _build_node(data["criteria"], path.name)
        exclusions = [_build_exclusion(e, path.name) for e in data.get("exclusions", [])]

        policy = Policy(
            policy_id=data["policy_id"],
            name=data["name"],
            payer_id=data["payer_id"],
            version=data["version"],
            effective_from=data["effective_from"],
            effective_until=data.get("effective_until"),
            source_pdf_path=pdf_path,
            source_total_pages=src.get("total_pages", len(page_texts)),
            applies_to=applies_to,
            criteria=criteria,
            exclusions=exclusions,
            metadata=data.get("metadata", {}),
            _page_texts=page_texts,
        )

        # Verify every citation in the tree and exclusions
        self._verify_citations(policy, path.name)
        return policy

    def _verify_citations(self, policy: Policy, filename: str) -> None:
        misses: list[str] = []

        def visit(node: CriterionNode | CriterionLeaf, path: str) -> None:
            cit = getattr(node, "policy_citation", None)
            if cit is not None:
                self._verify_one(policy, cit, f"{path}", misses)
            if isinstance(node, CriterionNode):
                for child in node.children:
                    visit(child, f"{path}>{child.id}")

        visit(policy.criteria, policy.criteria.id)
        for ex in policy.exclusions:
            self._verify_one(policy, ex.policy_citation, f"exclusion:{ex.id}", misses)

        if misses:
            raise PolicyLoadError(
                f"{filename}: {len(misses)} citation(s) failed substring verification:\n  "
                + "\n  ".join(misses)
            )

    def _verify_one(
        self, policy: Policy, cit: PolicyCitation, path: str, misses: list[str]
    ) -> None:
        page_text = policy.page_text(cit.page)
        check = verify_substring(cit.quote, page_text)
        if not check.found:
            misses.append(
                f"{path} p.{cit.page}: {check.diagnostic}; quote={cit.quote!r}"
            )
            return
        # Cache offsets in the citation for downstream UI / highlighting
        cit.start_offset = check.start_offset
        cit.end_offset = check.end_offset

    def _index(self, policy: Policy) -> None:
        self._by_id[policy.policy_id] = policy
        for cpt in policy.applies_to.cpt_codes:
            self._by_cpt[cpt].append(policy)
        for hcpcs in policy.applies_to.hcpcs_codes:
            self._by_cpt[hcpcs].append(policy)
        self._by_payer[policy.payer_id].append(policy)

    def get(self, policy_id: str) -> Policy | None:
        return self._by_id.get(policy_id)

    def all_policies(self) -> list[Policy]:
        return list(self._by_id.values())

    def policies_covering_cpt(self, cpt: str) -> list[Policy]:
        return list(self._by_cpt.get(cpt, []))

    def policies_for_payer(self, payer_id: str) -> list[Policy]:
        return list(self._by_payer.get(payer_id, []))


def _build_applies_to(data: dict, payer_id: str) -> AppliesTo:
    return AppliesTo(
        cpt_codes=list(data.get("cpt_codes", [])),
        hcpcs_codes=list(data.get("hcpcs_codes", [])),
        icd10_patterns=list(data.get("icd10_patterns", [])),
        lines_of_business=list(data.get("lines_of_business", [])),
        states=list(data.get("states", [])),
        age_min=data.get("age_min"),
        age_max=data.get("age_max"),
        settings_of_care=list(data.get("settings_of_care", [])),
        request_categories=list(data.get("request_categories", [])),
        branches=dict(data.get("branches", {})),
        payer_id=payer_id,
    )


def _build_citation(data: dict | None) -> PolicyCitation | None:
    if data is None:
        return None
    return PolicyCitation(
        page=int(data["page"]),
        section=data.get("section"),
        quote=str(data["quote"]),
    )


def _build_node(data: dict, filename: str, path: str = "root") -> CriterionNode | CriterionLeaf:
    node_type = data.get("type")
    node_id = data.get("id", path)
    if node_type == "leaf":
        cit = _build_citation(data.get("policy_citation"))
        if cit is None:
            raise PolicyLoadError(f"{filename}: leaf {node_id} missing policy_citation")
        rubric = data.get("verdict_rubric", {})
        if set(rubric.keys()) - VALID_VERDICTS:
            raise PolicyLoadError(
                f"{filename}: leaf {node_id} verdict_rubric has unknown verdict keys: "
                f"{set(rubric.keys()) - VALID_VERDICTS}"
            )
        return CriterionLeaf(
            id=node_id,
            type="leaf",
            description=data.get("description", ""),
            policy_citation=cit,
            evaluation=data.get("evaluation", {}),
            verdict_rubric=rubric,
        )
    if node_type == "internal":
        operator = data.get("operator")
        if operator not in VALID_OPERATORS:
            raise PolicyLoadError(
                f"{filename}: node {node_id} invalid operator: {operator!r}"
            )
        children = [
            _build_node(c, filename, f"{path}>{c.get('id', '?')}")
            for c in data.get("children", [])
        ]
        if not children:
            raise PolicyLoadError(f"{filename}: internal node {node_id} has no children")
        node = CriterionNode(
            id=node_id,
            type="internal",
            operator=operator,
            description=data.get("description", ""),
            children=children,
            policy_citation=_build_citation(data.get("policy_citation")),
            k=data.get("k"),
        )
        if operator == "AT_LEAST_K" and not isinstance(node.k, int):
            raise PolicyLoadError(f"{filename}: node {node_id} AT_LEAST_K requires integer 'k'")
        return node
    raise PolicyLoadError(f"{filename}: unknown node type {node_type!r} at {path}")


def _build_exclusion(data: dict, filename: str) -> Exclusion:
    cit = _build_citation(data.get("policy_citation"))
    if cit is None:
        raise PolicyLoadError(f"{filename}: exclusion {data.get('id')} missing policy_citation")
    rubric = data.get("verdict_rubric", {})
    if set(rubric.keys()) - VALID_VERDICTS:
        raise PolicyLoadError(
            f"{filename}: exclusion {data.get('id')} verdict_rubric has unknown verdict keys: "
            f"{set(rubric.keys()) - VALID_VERDICTS}"
        )
    return Exclusion(
        id=data["id"],
        description=data.get("description", ""),
        policy_citation=cit,
        evaluation=data.get("evaluation", {}),
        verdict_rubric=rubric,
    )


def iter_leaves(node: CriterionNode | CriterionLeaf) -> Iterable[CriterionLeaf]:
    if isinstance(node, CriterionLeaf):
        yield node
        return
    for child in node.children:
        yield from iter_leaves(child)


# Module-level singleton populated by FastAPI lifespan or test fixtures
_registry: PolicyRegistry | None = None


def get_registry() -> PolicyRegistry:
    global _registry
    if _registry is None:
        _registry = PolicyRegistry()
        _registry.load_dir()
    return _registry


def reset_registry() -> None:
    """For tests: force the next get_registry() call to reload."""
    global _registry
    _registry = None
