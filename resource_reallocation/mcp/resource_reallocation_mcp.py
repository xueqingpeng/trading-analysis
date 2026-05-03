"""Local MCP tools for the resource reallocation task.

The environment is a DuckDB file populated from a local scenario seed. Tools
expose only deterministic local state: no external APIs and no hidden outcome
leakage during the CEO decision phase.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Optional

import duckdb
from fastmcp import FastMCP
from pydantic import Field


DB_PATH = os.environ.get(
    "RESOURCE_REALLOCATION_DB_PATH",
    str(Path(__file__).resolve().parents[1] / "env" / "resource_reallocation_env.duckdb"),
)

mcp = FastMCP("resource_reallocation_mcp")


def _connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(DB_PATH, read_only=True)


def _loads(text: Optional[str]):
    if not text:
        return []
    return json.loads(text)


@mcp.tool(description="Return the latest available scenario date for a company.")
def get_latest_date(
    company_id: Annotated[str, Field(description="Scenario company id, e.g. 'alpha_industrial'")],
) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT CAST(MAX(date) AS VARCHAR) FROM company_states WHERE company_id = ?",
            [company_id],
        ).fetchone()
    return row[0] if row and row[0] else None


@mcp.tool(description="Return the current company-level state for one company on one target date.")
def get_company_state(
    company_id: Annotated[str, Field(description="Scenario company id")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
) -> dict:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT company_id, CAST(date AS VARCHAR), cash_runway_months, net_debt_to_ebitda,
                   revenue_growth_pct, gross_margin_pct, transformation_pressure,
                   capacity_constraint, board_priority
            FROM company_states
            WHERE company_id = ? AND date = ?
            """,
            [company_id, target_date],
        ).fetchone()
    if not row:
        raise ValueError(f"No company state for {company_id} on {target_date}")
    return {
        "company_id": row[0],
        "date": row[1],
        "cash_runway_months": row[2],
        "net_debt_to_ebitda": row[3],
        "revenue_growth_pct": row[4],
        "gross_margin_pct": row[5],
        "transformation_pressure": row[6],
        "capacity_constraint": row[7],
        "board_priority": row[8],
    }


@mcp.tool(description="Return business-unit state for one company on one target date.")
def get_business_units(
    company_id: Annotated[str, Field(description="Scenario company id")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT unit_id, current_capex_share, roi_trend, growth_outlook,
                   execution_risk, absorptive_capacity, strategic_role
            FROM business_units
            WHERE company_id = ? AND date = ?
            ORDER BY unit_id ASC
            """,
            [company_id, target_date],
        ).fetchall()
    return [
        {
            "unit_id": r[0],
            "current_capex_share": r[1],
            "roi_trend": r[2],
            "growth_outlook": r[3],
            "execution_risk": r[4],
            "absorptive_capacity": r[5],
            "strategic_role": r[6],
        }
        for r in rows
    ]


@mcp.tool(description="Return the feasible reallocation envelope for one company on one target date.")
def get_reallocation_constraints(
    company_id: Annotated[str, Field(description="Scenario company id")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
) -> dict:
    with _connect() as conn:
        head = conn.execute(
            """
            SELECT reallocatable_share_cap, transfer_rules_json
            FROM reallocation_constraints
            WHERE company_id = ? AND date = ?
            """,
            [company_id, target_date],
        ).fetchone()
        unit_rows = conn.execute(
            """
            SELECT unit_id, floor_share, ceiling_share, locked_share
            FROM unit_constraints
            WHERE company_id = ? AND date = ?
            ORDER BY unit_id ASC
            """,
            [company_id, target_date],
        ).fetchall()
    if not head:
        raise ValueError(f"No constraints for {company_id} on {target_date}")
    return {
        "reallocatable_share_cap": head[0],
        "transfer_rules": _loads(head[1]),
        "unit_floor_share": {r[0]: r[1] for r in unit_rows},
        "unit_ceiling_share": {r[0]: r[2] for r in unit_rows},
        "locked_share": {r[0]: r[3] for r in unit_rows},
    }


@mcp.tool(description="Return role-specific briefs for CFO, CTO, COO, and CMO on the target date.")
def get_csuite_role_briefs(
    company_id: Annotated[str, Field(description="Scenario company id")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
) -> dict:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT role, priority, watch_for_json, must_optimize_json, veto_if_json
            FROM role_briefs
            WHERE company_id = ? AND date = ?
            ORDER BY role ASC
            """,
            [company_id, target_date],
        ).fetchall()
    return {
        r[0]: {
            "priority": r[1],
            "watch_for": _loads(r[2]),
            "must_optimize": _loads(r[3]),
            "veto_if": _loads(r[4]),
        }
        for r in rows
    }


@mcp.tool(description="Return benchmark framing for one company on one target date.")
def get_benchmark_context(
    company_id: Annotated[str, Field(description="Scenario company id")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
) -> dict:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT mckinsey_reference, boldness_threshold, scenario_note,
                   boldness_desirable_if_json, boldness_undesirable_if_json
            FROM benchmark_context
            WHERE company_id = ? AND date = ?
            """,
            [company_id, target_date],
        ).fetchone()
    if not row:
        raise ValueError(f"No benchmark context for {company_id} on {target_date}")
    return {
        "mckinsey_reference": row[0],
        "boldness_threshold": row[1],
        "scenario_note": row[2],
        "boldness_is_desirable_if": _loads(row[3]),
        "boldness_is_undesirable_if": _loads(row[4]),
    }


@mcp.tool(description="Return prior resource reallocation decisions up to date_end inclusive.")
def get_decision_history(
    company_id: Annotated[str, Field(description="Scenario company id")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT CAST(date AS VARCHAR), allocation_plan_json, decision_type,
                   reallocation_share, ceo_rationale
            FROM decision_history
            WHERE company_id = ? AND date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            [company_id, date_start, date_end],
        ).fetchall()
    return [
        {
            "date": r[0],
            "allocation_plan": json.loads(r[1]),
            "decision_type": r[2],
            "reallocation_share": r[3],
            "ceo_rationale": r[4],
        }
        for r in rows
    ]


def _current_unit_shares(company_id: str, target_date: str) -> dict[str, float]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT unit_id, current_capex_share
            FROM business_units
            WHERE company_id = ? AND date = ?
            """,
            [company_id, target_date],
        ).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def _hidden_outcome_targets(company_id: str, target_date: str) -> dict:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT preferred_min_share, preferred_max_share, preferred_receiving_units_json,
                   protected_units_json, overloaded_if_funded_json,
                   downside_if_too_conservative, downside_if_too_aggressive
            FROM hidden_outcome_targets
            WHERE company_id = ? AND date = ?
            """,
            [company_id, target_date],
        ).fetchone()
    if not row:
        raise ValueError(f"No hidden outcome targets for {company_id} on {target_date}")
    return {
        "preferred_min_share": float(row[0]),
        "preferred_max_share": float(row[1]),
        "preferred_receiving_units": _loads(row[2]),
        "protected_units": _loads(row[3]),
        "overloaded_if_funded": _loads(row[4]),
        "downside_if_too_conservative": row[5],
        "downside_if_too_aggressive": row[6],
    }


def _plan_stats(allocation_plan: dict) -> tuple[dict[str, float], dict[str, float], float]:
    from_units = {k: float(v) for k, v in allocation_plan.get("from_units", {}).items()}
    to_units = {k: float(v) for k, v in allocation_plan.get("to_units", {}).items()}
    removed = round(sum(abs(v) for v in from_units.values()), 6)
    return from_units, to_units, removed


@mcp.tool(description="Deterministically validate whether a proposed allocation plan is feasible on the target date.")
def validate_reallocation_plan(
    company_id: Annotated[str, Field(description="Scenario company id")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
    allocation_plan: Annotated[dict, Field(description="Allocation plan with from_units and to_units dictionaries")],
) -> dict:
    constraints = get_reallocation_constraints(company_id, target_date)
    current = _current_unit_shares(company_id, target_date)
    from_units, to_units, removed = _plan_stats(allocation_plan)
    violations: list[str] = []

    added = round(sum(float(v) for v in to_units.values()), 6)
    if abs(removed - added) > 1e-6:
        violations.append("Total capital removed must equal total capital added.")

    if removed > float(constraints["reallocatable_share_cap"]) + 1e-6:
        violations.append("Plan exceeds reallocatable_share_cap.")

    for unit, share in current.items():
        delta = float(from_units.get(unit, 0.0)) + float(to_units.get(unit, 0.0))
        new_share = share + delta
        if new_share < float(constraints["unit_floor_share"].get(unit, 0.0)) - 1e-6:
            violations.append(f"{unit} falls below floor_share.")
        if new_share > float(constraints["unit_ceiling_share"].get(unit, 1.0)) + 1e-6:
            violations.append(f"{unit} exceeds ceiling_share.")
        locked = float(constraints["locked_share"].get(unit, 0.0))
        if unit in from_units and abs(float(from_units[unit])) > max(0.0, share - locked) + 1e-6:
            violations.append(f"{unit} removes locked capital.")

    return {"is_valid": len(violations) == 0, "violations": violations}


@mcp.tool(
    description=(
        "Evaluate the hidden benchmark consequences of a proposed allocation plan. "
        "Use this only after the CEO decision has been made, not during the decision."
    )
)
def evaluate_reallocation_outcome(
    company_id: Annotated[str, Field(description="Scenario company id")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
    allocation_plan: Annotated[dict, Field(description="Allocation plan with from_units and to_units dictionaries")],
) -> dict:
    validation = validate_reallocation_plan(company_id, target_date, allocation_plan)
    benchmark = get_benchmark_context(company_id, target_date)
    hidden = _hidden_outcome_targets(company_id, target_date)
    current = _current_unit_shares(company_id, target_date)
    from_units, to_units, removed = _plan_stats(allocation_plan)

    preferred_min = hidden["preferred_min_share"]
    preferred_max = hidden["preferred_max_share"]

    if removed < preferred_min:
        range_status = "under_reallocated"
        range_fit = max(0.0, 1.0 - (preferred_min - removed) / max(preferred_min, 1e-6))
        downside = hidden["downside_if_too_conservative"]
    elif removed > preferred_max:
        range_status = "over_reallocated"
        range_fit = max(0.0, 1.0 - (removed - preferred_max) / max(preferred_max, 1e-6))
        downside = hidden["downside_if_too_aggressive"]
    else:
        range_status = "within_preferred_range"
        range_fit = 1.0
        downside = "No primary range-based downside triggered."

    total_added = sum(to_units.values())
    preferred_added = sum(v for k, v in to_units.items() if k in hidden["preferred_receiving_units"])
    receiving_alignment = preferred_added / total_added if total_added > 0 else 0.0

    protected_violations = []
    for unit in hidden["protected_units"]:
        delta = from_units.get(unit, 0.0) + to_units.get(unit, 0.0)
        if delta < 0:
            protected_violations.append(unit)

    overload_units = [unit for unit in hidden["overloaded_if_funded"] if to_units.get(unit, 0.0) > 0]
    overloaded_share = sum(to_units.get(unit, 0.0) for unit in overload_units)
    overload_penalty = min(1.0, overloaded_share / max(preferred_max, 1e-6)) if overload_units else 0.0

    post_shares = {
        unit: round(current.get(unit, 0.0) + from_units.get(unit, 0.0) + to_units.get(unit, 0.0), 6)
        for unit in current
    }

    boldness_threshold = float(benchmark["boldness_threshold"])
    if removed >= boldness_threshold and range_status == "over_reallocated":
        conditional_boldness = "too_bold_for_state"
    elif removed < boldness_threshold and range_status == "under_reallocated":
        conditional_boldness = "not_bold_enough_for_state"
    else:
        conditional_boldness = "state_appropriate"

    if not validation["is_valid"]:
        overall_assessment = "invalid_plan"
    elif range_status == "within_preferred_range" and receiving_alignment >= 0.75 and not protected_violations and not overload_units:
        overall_assessment = "high_quality"
    elif range_status == "within_preferred_range" and receiving_alignment >= 0.5:
        overall_assessment = "acceptable_with_tradeoffs"
    else:
        overall_assessment = "misallocated"

    return {
        "company_id": company_id,
        "date": target_date,
        "reallocation_share": removed,
        "range_status": range_status,
        "range_fit": round(range_fit, 4),
        "receiving_alignment": round(receiving_alignment, 4),
        "protected_unit_violations": protected_violations,
        "overloaded_destination_units": overload_units,
        "overload_penalty": round(overload_penalty, 4),
        "conditional_boldness": conditional_boldness,
        "overall_assessment": overall_assessment,
        "validation": validation,
        "post_allocation_shares": post_shares,
        "primary_downside": downside,
    }


@mcp.tool(
    description=(
        "Score a proposed allocation plan after the decision using a deterministic, "
        "scenario-conditioned rubric. Use only post hoc, not during the CEO decision."
    )
)
def score_reallocation_decision(
    company_id: Annotated[str, Field(description="Scenario company id")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
    allocation_plan: Annotated[dict, Field(description="Allocation plan with from_units and to_units dictionaries")],
) -> dict:
    outcome = evaluate_reallocation_outcome(company_id, target_date, allocation_plan)
    validation_ok = 1.0 if outcome["validation"]["is_valid"] else 0.0
    protected_ok = 1.0 if not outcome["protected_unit_violations"] else 0.0
    overload_ok = 1.0 - outcome["overload_penalty"]

    component_scores = {
        "validation": round(30.0 * validation_ok, 2),
        "range_fit": round(30.0 * outcome["range_fit"], 2),
        "receiving_alignment": round(20.0 * outcome["receiving_alignment"], 2),
        "protected_units": round(10.0 * protected_ok, 2),
        "overload_avoidance": round(10.0 * overload_ok, 2),
    }
    base_score = sum(component_scores.values())

    adjustment_multiplier = 1.0
    if not outcome["validation"]["is_valid"]:
        adjustment_multiplier *= 0.45
    elif outcome["overall_assessment"] == "misallocated":
        adjustment_multiplier *= 0.72
    elif outcome["overall_assessment"] == "acceptable_with_tradeoffs":
        adjustment_multiplier *= 0.9

    total_score = round(base_score * adjustment_multiplier, 2)

    if total_score >= 85:
        grade = "A"
    elif total_score >= 70:
        grade = "B"
    elif total_score >= 55:
        grade = "C"
    elif total_score >= 40:
        grade = "D"
    else:
        grade = "F"

    return {
        "company_id": company_id,
        "date": target_date,
        "total_score": total_score,
        "grade": grade,
        "component_scores": component_scores,
        "outcome": outcome,
    }


if __name__ == "__main__":
    mcp.run()
