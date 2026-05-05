---
name: resource_reallocation
description: >
  Makes one CEO-level resource reallocation decision for a single company on a
  given target date by querying an offline scenario environment through the
  `resource_reallocation_mcp` server. Each invocation handles exactly one
  `(company_id, target_date)` pair and upserts one result record into
  `results/resource_reallocation/`. The task is a multi-role decision problem:
  CFO, CTO, COO, and CMO each provide role-conditioned recommendations under
  fixed role constraints, and the CEO integrates them into one final capital /
  resource reallocation plan. The same skill can be used for historical replay
  (caller loops over dates) or live-style evaluation (caller passes the current
  scenario date).

  Use this skill whenever the user asks for a resource reallocation decision,
  capital reallocation step, business-unit reallocation decision, or a single
  CEO portfolio-allocation judgment.
---

# Resource Reallocation Skill

You are making a **single-date CEO resource reallocation decision** for one
company. Your job is to:

1. read the company state and role-specific scenario information from MCP tools,
2. generate role-conditioned recommendations from multiple C-suite views,
3. integrate those views as the CEO,
4. produce one final reallocation plan,
5. validate the plan against deterministic environment constraints,
6. upsert exactly one structured result record.

This task is designed to evaluate **CEO judgment under cross-functional
tension**. The CEO does not decide in a vacuum: the same scenario is viewed
through distinct role lenses:

- `CFO` focuses on capital discipline, liquidity, downside risk, and financing constraints
- `CTO` focuses on technical feasibility, platform leverage, and innovation timing
- `COO` focuses on operational continuity, transition burden, and execution capacity
- `CMO` focuses on demand timing, growth capture, and market-window risk

The final evaluation centers on whether the CEO makes a
**scenario-appropriate** reallocation decision, whether the CEO places the
right implicit weight on the right executive view for the scenario, and
whether the CEO is bold **when conditions support boldness** but restrained
when the company state does not.

This skill should follow the same design discipline as the other tasks in this
repository:

- one invocation = one target date
- no look-ahead beyond the decision date
- data access via MCP only
- one incremental upsert into a result JSON file

Everything you know about the company state must come from MCP tools on the
`resource_reallocation_mcp` server.

---

## Environment

This task reads from an offline DuckDB environment through MCP tools.

- The DuckDB environment is built externally from benchmark-authored seed data.
- The decision agent should treat it as read-only.
- The decision agent should never inspect raw DuckDB, seed JSON, or hidden evaluation targets directly.

---

## Inputs

The user invocation specifies:

1. **`COMPANY_ID`** - one supported company scenario id, for example
   `alpha_industrial`, `nova_software`, or another id defined by the benchmark.
2. **`TARGET_DATE`** - the scenario date to decide on, `YYYY-MM-DD`. Optional.
   If omitted, call `get_latest_date(company_id=COMPANY_ID)` and use that date.

Typical user phrasings:

- `run resource reallocation for alpha_industrial on 2025-04-01`
- `make one CEO reallocation decision for nova_software 2025-05-12`
- `decide the capex reallocation for alpha_industrial`

Each invocation handles exactly **one** `(COMPANY_ID, TARGET_DATE)` pair.

---

## Data Access - Scenario Environment via MCP

The `resource_reallocation_mcp` server is assumed to expose a structured
offline environment similar in spirit to the trading MCP servers:
deterministic, read-only, and local to the benchmark.

### Proposed MCP tools

| Tool | Purpose |
|---|---|
| `get_latest_date(company_id)` | Return the latest available scenario date for the company. |
| `get_company_state(company_id, target_date)` | Return the current company-level state at `target_date`: liquidity, leverage, growth pressure, board priorities, and operating constraints. |
| `get_company_state_history(company_id, date_start, date_end)` | Return company-level state history up to the current decision date. Use when historical trajectory helps the decision. |
| `get_business_units(company_id, target_date)` | Return the business-unit breakdown with current allocation shares, performance, absorptive capacity, and transition frictions. |
| `get_business_units_history(company_id, date_start, date_end)` | Return business-unit state history up to the current decision date. Use when allocation drift or prior transitions matter. |
| `get_reallocation_constraints(company_id, target_date)` | Return the feasible action envelope: total reallocatable share, unit floors/caps, locked budgets, and transfer rules. |
| `get_csuite_role_briefs(company_id, target_date)` | Return role-specific briefs for CFO / CTO / COO / CMO, including role objectives, hard constraints, veto conditions, private signals, confidence levels, and bias risks. |
| `get_benchmark_context(company_id, target_date)` | Return task-level benchmark facts, such as the McKinsey reallocation reference, difficulty tier, plus the state conditions under which boldness is or is not desirable. |
| `get_decision_history(company_id, date_start, date_end)` | Return prior reallocation decisions up to `target_date` only; use for historical context without look-ahead. |
| `validate_reallocation_plan(company_id, target_date, allocation_plan)` | Deterministically check whether a proposed plan respects all budget, floor/cap, timing, and dependency constraints. Returns validity plus violation details. |

### Expected shapes

#### `get_company_state(company_id, target_date)`

Returns one object like:

```json
{
  "company_id": "alpha_industrial",
  "date": "2025-04-01",
  "cash_runway_months": 11,
  "net_debt_to_ebitda": 2.8,
  "revenue_growth_pct": 4.2,
  "gross_margin_pct": 38.5,
  "transformation_pressure": "high",
  "capacity_constraint": "medium",
  "board_priority": "shift capital toward higher-growth units"
}
```

#### `get_business_units(company_id, target_date)`

Returns a list like:

```json
[
  {
    "unit_id": "core_legacy",
    "current_capex_share": 0.42,
    "roi_trend": "declining",
    "growth_outlook": "low",
    "execution_risk": "low",
    "absorptive_capacity": "medium"
  },
  {
    "unit_id": "ai_platform",
    "current_capex_share": 0.18,
    "roi_trend": "emerging",
    "growth_outlook": "high",
    "execution_risk": "medium",
    "absorptive_capacity": "high"
  }
]
```

#### `get_reallocation_constraints(company_id, target_date)`

Returns one object describing the feasible plan space:

```json
{
  "reallocatable_share_cap": 0.62,
  "unit_floor_share": {
    "core_legacy": 0.22,
    "ai_platform": 0.10,
    "cloud_services": 0.08,
    "field_automation": 0.05
  },
  "unit_ceiling_share": {
    "core_legacy": 0.50,
    "ai_platform": 0.45,
    "cloud_services": 0.32,
    "field_automation": 0.20
  },
  "locked_share": {
    "core_legacy": 0.12
  },
  "transfer_rules": [
    "Capital removed from regulated maintenance programs cannot be reassigned into speculative R&D in the same quarter.",
    "At least 40% of any reduction from core_legacy must remain within industrial-adjacent units."
  ]
}
```

#### `get_csuite_role_briefs(company_id, target_date)`

Returns something like:

```json
{
  "CFO": {
    "priority": "preserve balance-sheet resilience",
    "watch_for": ["cash risk", "debt pressure", "capital discipline"],
    "must_optimize": ["free_cash_flow_resilience", "downside_protection"],
    "veto_if": ["pro_forma_runway_below_9_months", "net_debt_to_ebitda_above_3_5x"]
  },
  "CTO": {
    "priority": "fund platform capability for future advantage",
    "watch_for": ["technical leverage", "innovation timing", "build feasibility"],
    "must_optimize": ["platform_compounding", "technical_option_value"],
    "veto_if": ["critical_platform_dependency_left_unfunded"]
  },
  "COO": {
    "priority": "avoid destabilizing operations",
    "watch_for": ["execution load", "transition risk", "delivery continuity"],
    "must_optimize": ["service_continuity", "transition_feasibility"],
    "veto_if": ["field_delivery_sla_breach_likely", "execution_load_exceeds_capacity"]
  },
  "CMO": {
    "priority": "capture market window",
    "watch_for": ["growth opportunity", "customer demand", "go-to-market timing"],
    "must_optimize": ["demand_capture", "share_gain_timing"],
    "veto_if": ["window_closes_within_two_quarters_if_underfunded"]
  }
}
```

#### `get_benchmark_context(company_id, target_date)`

Returns benchmark framing like:

```json
{
  "mckinsey_reference": "Companies that reallocated more than 50% of capital expenditure across business units over ten years created about 50% more value.",
  "boldness_threshold": 0.50,
  "scenario_note": "Current board concern is that management has historically under-reallocated capital.",
  "boldness_is_desirable_if": [
    "balance_sheet_headroom_is_adequate",
    "receiving_units_have_positive_growth_conviction",
    "execution_disruption_risk_is_contained"
  ],
  "boldness_is_undesirable_if": [
    "liquidity_is_fragile",
    "reallocated_capital_overloads_operations",
    "destination_units_cannot_absorb_capital_productively"
  ]
}
```

### No-look-ahead discipline

The environment may contain scenario dates after the current decision date.
Your queries must not request information beyond `TARGET_DATE`.

- `get_company_state`, `get_business_units`, and `get_reallocation_constraints`
  must be called only for `TARGET_DATE`
- `get_company_state_history` and `get_business_units_history` must have `date_end <= TARGET_DATE`
- `get_decision_history` must have `date_end <= TARGET_DATE`

This mirrors the no-look-ahead discipline in the trading tasks.

---

## Task framing

The task is:

**Given one company state and one feasible reallocation envelope, decide how
the CEO should reallocate resources across business units on this date.**

The CEO must not merely echo one executive. The CEO must also reason under private role signals and scenario difficulty, then choose a plan that can fit one of several acceptable strategic profiles in post-hoc evaluation. The CEO must:

1. understand the company-wide problem,
2. generate role-conditioned advice from each executive perspective,
3. identify where the executives conflict,
4. decide which roles deserve more weight in this scenario,
5. produce one final allocation plan,
6. validate that plan against environment constraints.

This is a **single final decision** task, not a debate transcript task and not
a multi-day simulation.

The benchmark hypothesis is **not** that “more reallocation is always better.”
Instead:

- **boldness** is a conditional evaluation dimension
- **decision quality** depends on whether capital is moved to the right places
- **role weighting quality** depends on whether the CEO emphasizes the right
  executive concerns for the scenario
- **outcome quality** is judged after the decision through the companion
  evaluation task, not by peeking into hidden future information during the
  decision

---

## Internal reasoning protocol

Use the following internal sequence for every invocation.

### Step 1 - Read the scenario

Call:

- `get_company_state(COMPANY_ID, TARGET_DATE)`
- `get_business_units(COMPANY_ID, TARGET_DATE)`
- `get_reallocation_constraints(COMPANY_ID, TARGET_DATE)`
- `get_csuite_role_briefs(COMPANY_ID, TARGET_DATE)`
- `get_benchmark_context(COMPANY_ID, TARGET_DATE)`

Optionally call:

- `get_decision_history(COMPANY_ID, TARGET_DATE - 365d, TARGET_DATE)`

### Step 2 - Generate role-conditioned recommendations

For each of `CFO`, `CTO`, `COO`, and `CMO`, produce:

- one proposed **allocation delta** from that role's perspective
- one short rationale from that role's perspective
- one key risk the role is worried about
- one condition under which that role would oppose the CEO's plan

These are **internal structured role views**. They should reflect the role
brief. They should not collapse into generic management language. The role
recommendations should conflict when the scenario genuinely contains trade-offs.

### Step 3 - Identify the tension

Before finalizing the CEO decision, explicitly identify:

- where the role recommendations agree
- where they conflict
- whether the key conflict is:
  - boldness vs prudence
  - innovation vs continuity
  - growth vs balance-sheet safety
  - market timing vs execution readiness

### Step 4 - CEO weighting and synthesis

The CEO then chooses one final `allocation_plan`.

The final decision should state:

- which role(s) received the greatest weight and why
- whether the final decision is conservative, moderate, or bold **for this scenario**
- whether the final decision crosses the benchmark boldness threshold
- why crossing or not crossing that threshold is justified by the state

### Step 5 - Final recommendation

Produce exactly one final resource reallocation decision.

The CEO should be bold **when the scenario supports it**, and should be able to
justify either:

- a bold reallocation,
- a moderate reallocation,
- or a hold / minimal-change allocation

using cross-functional logic and company-state logic.

Before finalizing, call:

- `validate_reallocation_plan(COMPANY_ID, TARGET_DATE, allocation_plan)`

If the plan is invalid, revise it until it passes validation. Do not emit an
invalid plan.

---

## Final output fields

Write one upserted record keyed by `date`.

The output path should be provided by the runtime / caller.

Do not infer or invent `agent_name` or `model` yourself.

Typical runtime-managed output path:

```text
results/resource_reallocation/resource_reallocation_{COMPANY_ID}_{run_label}.json
```

The output file should contain one document with a `recommendations` array.
Each invocation upserts exactly one record keyed by `date`.

### Output record schema

Each record should look like:

```json
{
  "date": "2025-04-01",
  "company_id": "alpha_industrial",
  "allocation_plan": {
    "from_units": {
      "core_legacy": -0.24,
      "field_services": -0.08
    },
    "to_units": {
      "ai_platform": 0.18,
      "cloud_services": 0.09,
      "field_automation": 0.05
    }
  },
  "decision_type": "bold",
  "reallocation_share": 0.32,
  "crosses_boldness_threshold": false,
  "scenario_conditioned_boldness": {
    "assessment": "appropriate_boldness",
    "why": "Balance-sheet headroom is adequate, destination units have positive growth conviction, and operations can absorb the shift."
  },
  "ceo_rationale": "The CEO prioritizes long-term value creation and weighs CTO and CMO more heavily than CFO in this scenario because the company has enough balance-sheet capacity and the growth window is time-sensitive.",
  "role_recommendations": {
    "CFO": {
      "allocation_delta": {
        "core_legacy": -0.08,
        "ai_platform": 0.05,
        "cloud_services": 0.03
      },
      "rationale": "...",
      "primary_risk": "...",
      "opposition_condition": "runway falls below 9 months"
    },
    "CTO": {
      "allocation_delta": {
        "core_legacy": -0.20,
        "field_services": -0.06,
        "ai_platform": 0.17,
        "cloud_services": 0.09
      },
      "rationale": "...",
      "primary_risk": "...",
      "opposition_condition": "critical platform dependency remains underfunded"
    },
    "COO": {
      "allocation_delta": {
        "core_legacy": -0.12,
        "field_services": -0.05,
        "ai_platform": 0.08,
        "cloud_services": 0.05,
        "field_automation": 0.04
      },
      "rationale": "...",
      "primary_risk": "...",
      "opposition_condition": "delivery continuity is threatened"
    },
    "CMO": {
      "allocation_delta": {
        "core_legacy": -0.18,
        "field_services": -0.07,
        "ai_platform": 0.12,
        "cloud_services": 0.07,
        "field_automation": 0.06
      },
      "rationale": "...",
      "primary_risk": "...",
      "opposition_condition": "market window closes before demand-facing units are funded"
    }
  },
  "ceo_weighting_note": "CEO placed highest weight on CTO and CMO, moderate weight on COO, and lower weight on CFO.",
  "validation": {
    "is_valid": true,
    "violations": []
  },
  "benchmark_reference": "McKinsey >50% reallocation reference considered"
}
```

### Incremental upsert

Use the same incremental-upsert pattern as the trading tasks:

```python
import json
from pathlib import Path

out_path = Path(OUTPUT_PATH)
out_path.parent.mkdir(parents=True, exist_ok=True)

if out_path.exists():
    doc = json.loads(out_path.read_text())
else:
    doc = {"status": "in_progress", "recommendations": []}

rec_by_date = {r["date"]: r for r in doc.get("recommendations", [])}
rec_by_date[TARGET_DATE] = RECORD

recs = sorted(rec_by_date.values(), key=lambda r: r["date"])

doc = {
    "status": "in_progress",
    "company_id": COMPANY_ID,
    "start_date": recs[0]["date"],
    "end_date": recs[-1]["date"],
    "recommendations": recs,
}

out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
```

Same `TARGET_DATE` called again must overwrite the prior record.

---

## What NOT to do

- Do not read parquet, DuckDB, or scenario JSON files directly if MCP tools are available.
- Do not use data with `date > TARGET_DATE`.
- Do not fabricate an `agent_name`, `model`, or output filename scheme.
- Do not collapse all C-suite views into one generic summary without role distinction.
- Do not treat the McKinsey benchmark as a hard rule that boldness is always correct.
- Do not emit a plan that fails deterministic validation.
- Do not write multiple dates in one invocation.
- Do not rewrite the whole file manually; always upsert.

---

## Implementation approach

1. Resolve `TARGET_DATE`: use the provided date, or call `get_latest_date(COMPANY_ID)`.
2. Fetch company state, business units, constraints, role briefs, and benchmark context.
   If helpful, also fetch historical company or business-unit state ending at `TARGET_DATE`.
3. Build four role-conditioned recommendations: `CFO`, `CTO`, `COO`, `CMO`.
4. Identify the main conflict among those role recommendations.
5. Make one CEO synthesis decision:
   - final `allocation_plan`
   - bold / moderate / conservative label
   - whether it crosses the boldness threshold
   - why those role weights are appropriate in this scenario
6. Call `validate_reallocation_plan(...)`. If invalid, revise and re-validate.
7. Upsert exactly one structured record into the results JSON file.

One invocation in, one decision out.
