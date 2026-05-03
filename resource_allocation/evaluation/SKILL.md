---
name: resource_reallocation_evaluation
description: >
  Evaluates one CEO resource reallocation decision for a single company on a
  given target date by querying the offline `resource_reallocation_mcp`
  environment. This is a post-hoc evaluation task: it reads the already-made
  allocation plan, checks hard validity constraints, reads hidden outcome
  targets, and computes a deterministic benchmark score. Writes one structured
  JSON result under `results/resource_reallocation_evaluation/`.

  Use this skill whenever the user asks to score a resource reallocation
  decision, evaluate a CEO reallocation plan, audit a resource reallocation
  output, or compute post-hoc benchmark quality for one decision.
---

# Resource Reallocation Evaluation Skill

You are evaluating one **already-made** resource reallocation decision.

This skill is separate from `resource_reallocation/SKILL.md` on purpose:

- the decision skill should not see hidden outcome targets
- the evaluation skill may use hidden outcome targets post hoc
- this preserves the benchmark's no-look-ahead discipline

---

## Inputs

The user invocation specifies:

1. **`COMPANY_ID`**
2. **`TARGET_DATE`**
3. **`ALLOCATION_PLAN`** or an output file containing the already-made plan

If the user provides an output file, extract the decision record for
`TARGET_DATE` and evaluate that plan.

---

## Data access

Use the `resource_reallocation_mcp` server.

For evaluation, the relevant MCP tools are:

- `validate_reallocation_plan(company_id, target_date, allocation_plan)`
- `evaluate_reallocation_outcome(company_id, target_date, allocation_plan)`
- `score_reallocation_decision(company_id, target_date, allocation_plan)`

Do not modify the original decision file.

---

## Output

Write one JSON result under:

```text
results/resource_reallocation_evaluation/
```

The runtime / caller should provide the exact output path.

Each result should contain:

```json
{
  "company_id": "alpha_industrial",
  "date": "2025-04-01",
  "total_score": 65.12,
  "grade": "C",
  "component_scores": {
    "validation": 30.0,
    "range_fit": 20.45,
    "receiving_alignment": 20.0,
    "protected_units": 10.0,
    "overload_avoidance": 10.0
  },
  "outcome": {
    "range_status": "under_reallocated",
    "conditional_boldness": "not_bold_enough_for_state",
    "overall_assessment": "misallocated",
    "validation": {
      "is_valid": true,
      "violations": []
    }
  }
}
```

---

## Implementation approach

1. Resolve the already-made allocation plan.
2. Call `validate_reallocation_plan(...)`.
3. Call `evaluate_reallocation_outcome(...)`.
4. Call `score_reallocation_decision(...)`.
5. Write one structured evaluation record.

One evaluation in, one score out.
