# Running the Evals

5-level eval pyramid for the Latitude PA prototype. Tests every stage of the agent against synthetic cases that are **independent of the demo patients** (Smith / Taylor / Welsh).

## Prereqs

```bash
# One-time
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

# For LLM-dependent levels (L2, L3): set your key in .env
cp .env.example .env
# edit .env, set ANTHROPIC_API_KEY=sk-ant-...
```

## Run all evals

```bash
# Fast: L0 + L1 + L4 only (deterministic, no LLM cost, ~2 sec)
.venv/bin/python -m scripts.run_evals

# Full: L0 + L1 + L2 + L3 + L4 (incl. LLM, ~$3-5, ~10 min)
RUN_LLM_EVALS=1 .venv/bin/python -m scripts.run_evals
```

Results land in `tests/evals/results/<run_id>.json`.

## Run individual levels

```bash
# L0 — Citation faithfulness (always-on gate; runs at policy load)
.venv/bin/pytest tests/unit/test_policy_registry.py -v

# L1 — Selector (deterministic, ~1 sec)
.venv/bin/pytest tests/evals/level1_selector/ -v

# L2 — Per-criterion adjudication (LLM, ~$1, ~5 min)
RUN_LLM_EVALS=1 .venv/bin/pytest tests/evals/level2_criteria/ -v -s

# L3 — End-to-end synthetic + Smith (LLM, ~$2-3, ~8 min)
RUN_LLM_EVALS=1 .venv/bin/pytest tests/evals/level3_e2e/ -v -s

# L4 — Cross-policy selector boundary (deterministic, ~1 sec)
.venv/bin/pytest tests/evals/level4_selector_cross_policy/ -v
```

## What each level measures

| Level | What | Cases | Target | LLM cost |
|---|---|---:|---:|---:|
| **L0** | Every policy citation substring-verifies against its source PDF | 78 | 100% | $0 |
| **L1** | Deterministic policy selector picks right policy | 16 | ≥98% | $0 |
| **L2** | Adjudicator agent's per-criterion verdict in isolation | 39 | ≥92% | ~$0.02/case |
| **L3** | End-to-end (intake → selector → adjudicator → reviewer) | 4* | ≥85% | ~$0.50/case |
| **L4** | Cross-policy selector against the real 3-policy registry | 8 | 100% | $0 |

\* Smith keystone + 3 synthetic narratives. Adding more L3 cases: drop a `CASE = L3SyntheticCase(...)` module into [`tests/evals/level3_e2e/synthetic_cases/`](level3_e2e/synthetic_cases/) — the runner auto-discovers them.

## Eval cases by policy domain

| Policy | L2 cases | L3 cases |
|---|---:|---:|
| Molina ESI (`molina-mcp-032`) | 19 | 1 (Smith) + 1 synthetic |
| MassHealth Zepbound (`masshealth-anti-obesity-zepbound`) | 10 | 1 synthetic |
| Oregon Adenomyosis (`oregon-hcr-39`) | 10 | 1 synthetic |
| **Total** | **39** | **4** |

## Independence from demo patients

Every eval case is verified independent from the Smith / Taylor / Welsh demo patients via two audits:

1. **Fingerprint scan** — no demo-patient identifiers (names, MRNs, payer IDs, characteristic medications, exact phrases) appear in any eval case.
2. **5-gram overlap** — no 5-word phrase from L3 synthetic narratives matches the demo PDFs (excluding FDA/CAP-standardized clinical boilerplate).

See [`EVAL_DESIGN.md`](EVAL_DESIGN.md) for the full design spec and case-by-case rationale.

## Adding new cases

**L2 (criterion-level, FHIR fact collection):**
```python
# tests/evals/level2_criteria/cases.py — append to L2_CASES
L2Case(
    id="L2-NEW-01",
    policy_id="molina-mcp-032",  # or another policy_id
    category="threshold",
    criterion_id="indication.initial_injection.severity.nrs_above_4",
    facts=_case({"observations": [ ExtractedObservation(...) ]}),
    expected_verdict="met",
)
```

**L3 (end-to-end narrative):**
```python
# tests/evals/level3_e2e/synthetic_cases/<new_case>.py
from tests.evals.level3_e2e.synthetic_bundle import L3SyntheticCase

CASE = L3SyntheticCase(
    id="L3-XXX-NN",
    domain="esi",  # or "zepbound" / "adenomyosis"
    family="...", given="...", birth_date="YYYY-MM-DD", ...
    payer_id="...", cpt_code="...", icd10_codes=[...],
    narrative="""H&P-style narrative text...""",
    expected_outcome="pend",  # approve / deny / pend
    expected_info_keywords=["pt", "imaging"],
)
```

The runner auto-discovers any module exporting a `CASE: L3SyntheticCase`.

**L4 (selector boundary):** add a `test_l4_XX_*` function to `tests/evals/level4_selector_cross_policy/test_l4_cross_policy.py`.

## Diffing across runs

```bash
ls tests/evals/results/   # all past runs
diff <(jq . tests/evals/results/<old>.json) <(jq . tests/evals/results/<new>.json)
```

## Metric outputs (with `RUN_LLM_EVALS=1`)

```
L0 citation:    100.0%  (target 100%)
L1 selector:    PASS    (target ≥98%)
L4 x-policy:    PASS    (target 100%)
L2 criterion:   XX.X%   (target ≥92%)
  By policy:
    molina-mcp-032                              X/19
    masshealth-anti-obesity-zepbound            X/10
    oregon-hcr-39                               X/10
L3 e2e:         XX.X%   (target ≥85%)
  By domain:
    esi                                         X/2
    zepbound                                    X/1
    adenomyosis                                 X/1
Total LLM cost: $X.XX
```
