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


def _loads(text: Optional[str], default=None):
    if not text:
        return [] if default is None else default
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


@mcp.tool(description=("Return company-level state history for one company in [date_start, date_end] inclusive. "
                       "To respect no-look-ahead, pass date_end <= the current decision date."))
def get_company_state_history(
    company_id: Annotated[str, Field(description="Scenario company id")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT company_id, CAST(date AS VARCHAR), cash_runway_months, net_debt_to_ebitda,
                   revenue_growth_pct, gross_margin_pct, transformation_pressure,
                   capacity_constraint, board_priority
            FROM company_states
            WHERE company_id = ? AND date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            [company_id, date_start, date_end],
        ).fetchall()
    return [
        {
            "company_id": r[0],
            "date": r[1],
            "cash_runway_months": r[2],
            "net_debt_to_ebitda": r[3],
            "revenue_growth_pct": r[4],
            "gross_margin_pct": r[5],
            "transformation_pressure": r[6],
            "capacity_constraint": r[7],
            "board_priority": r[8],
        }
        for r in rows
    ]


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


@mcp.tool(description=("Return business-unit state history for one company in [date_start, date_end] inclusive. "
                       "To respect no-look-ahead, pass date_end <= the current decision date."))
def get_business_units_history(
    company_id: Annotated[str, Field(description="Scenario company id")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT CAST(date AS VARCHAR), unit_id, current_capex_share, roi_trend, growth_outlook,
                   execution_risk, absorptive_capacity, strategic_role
            FROM business_units
            WHERE company_id = ? AND date >= ? AND date <= ?
            ORDER BY date ASC, unit_id ASC
            """,
            [company_id, date_start, date_end],
        ).fetchall()
    return [
        {
            "date": r[0],
            "unit_id": r[1],
            "current_capex_share": r[2],
            "roi_trend": r[3],
            "growth_outlook": r[4],
            "execution_risk": r[5],
            "absorptive_capacity": r[6],
            "strategic_role": r[7],
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
            SELECT role, priority, watch_for_json, must_optimize_json, veto_if_json,
                   private_signal, confidence_level, bias_risk
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
            "private_signal": r[5],
            "confidence_level": r[6],
            "bias_risk": r[7],
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
                   boldness_desirable_if_json, boldness_undesirable_if_json, difficulty_tier
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
        "difficulty_tier": row[5],
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
                   downside_if_too_conservative, downside_if_too_aggressive,
                   acceptable_profiles_json, history_guardrails_json
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
        "acceptable_profiles": _loads(row[7], default=[]),
        "history_guardrails": _loads(row[8], default={}),
    }


def _plan_stats(allocation_plan: dict) -> tuple[dict[str, float], dict[str, float], float]:
    from_units = {k: float(v) for k, v in allocation_plan.get("from_units", {}).items()}
    to_units = {k: float(v) for k, v in allocation_plan.get("to_units", {}).items()}
    removed = round(sum(abs(v) for v in from_units.values()), 6)
    return from_units, to_units, removed


def _history_penalties(history: list[dict], from_units: dict[str, float], to_units: dict[str, float], guardrails: dict) -> tuple[float, list[str]]:
    notes: list[str] = []
    if not history:
        return 0.0, notes
    recent = history[-int(guardrails.get("lookback", 2)):]
    repeat_funding_units = set(guardrails.get("repeat_funding_penalty_units", []))
    repeat_cut_units = set(guardrails.get("repeat_cut_penalty_units", []))

    penalty = 0.0
    for unit in repeat_funding_units:
        if to_units.get(unit, 0.0) > 0:
            repeat_count = sum(1 for item in recent if item["allocation_plan"].get("to_units", {}).get(unit, 0.0) > 0)
            if repeat_count:
                penalty += min(0.4, 0.15 * repeat_count)
                notes.append(f"repeat funding concentration on {unit}")

    for unit in repeat_cut_units:
        if from_units.get(unit, 0.0) < 0:
            repeat_count = sum(1 for item in recent if item["allocation_plan"].get("from_units", {}).get(unit, 0.0) < 0)
            if repeat_count:
                penalty += min(0.4, 0.15 * repeat_count)
                notes.append(f"repeat underfunding pressure on {unit}")

    return min(1.0, penalty), notes


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


def _default_profiles(hidden: dict) -> list[dict]:
    return [
        {
            "profile_id": "legacy_single_profile",
            "label": "legacy single target",
            "preferred_min_share": hidden["preferred_min_share"],
            "preferred_max_share": hidden["preferred_max_share"],
            "preferred_receiving_units": hidden["preferred_receiving_units"],
            "protected_units": hidden["protected_units"],
            "overloaded_if_funded": hidden["overloaded_if_funded"],
        }
    ]


def _profile_outcome(profile: dict, removed: float, to_units: dict[str, float], from_units: dict[str, float], current: dict[str, float], validation: dict, benchmark: dict, history_penalty: float, history_notes: list[str]) -> dict:
    preferred_min = float(profile["preferred_min_share"])
    preferred_max = float(profile["preferred_max_share"])
    if removed < preferred_min:
        range_status = "under_reallocated"
        range_fit = max(0.0, 1.0 - (preferred_min - removed) / max(preferred_min, 1e-6))
    elif removed > preferred_max:
        range_status = "over_reallocated"
        range_fit = max(0.0, 1.0 - (removed - preferred_max) / max(preferred_max, 1e-6))
    else:
        range_status = "within_preferred_range"
        range_fit = 1.0

    total_added = sum(to_units.values())
    preferred_added = sum(v for k, v in to_units.items() if k in profile.get("preferred_receiving_units", []))
    receiving_alignment = preferred_added / total_added if total_added > 0 else 0.0

    protected_violations = []
    for unit in profile.get("protected_units", []):
        delta = from_units.get(unit, 0.0) + to_units.get(unit, 0.0)
        if delta < 0:
            protected_violations.append(unit)

    overload_units = [unit for unit in profile.get("overloaded_if_funded", []) if to_units.get(unit, 0.0) > 0]
    overloaded_share = sum(to_units.get(unit, 0.0) for unit in overload_units)
    overload_penalty = min(1.0, overloaded_share / max(preferred_max, 1e-6)) if overload_units else 0.0
    history_coherence = max(0.0, 1.0 - history_penalty)

    boldness_threshold = float(benchmark["boldness_threshold"])
    if removed >= boldness_threshold and range_status == "over_reallocated":
        conditional_boldness = "too_bold_for_state"
    elif removed < boldness_threshold and range_status == "under_reallocated":
        conditional_boldness = "not_bold_enough_for_state"
    else:
        conditional_boldness = "state_appropriate"

    if not validation["is_valid"]:
        overall_assessment = "invalid_plan"
    elif range_status == "within_preferred_range" and receiving_alignment >= 0.75 and not protected_violations and not overload_units and history_coherence >= 0.75:
        overall_assessment = "high_quality"
    elif range_status == "within_preferred_range" and receiving_alignment >= 0.5 and history_coherence >= 0.55:
        overall_assessment = "acceptable_with_tradeoffs"
    else:
        overall_assessment = "misallocated"

    post_shares = {
        unit: round(current.get(unit, 0.0) + from_units.get(unit, 0.0) + to_units.get(unit, 0.0), 6)
        for unit in current
    }

    fit_score = (
        0.30 * range_fit
        + 0.30 * receiving_alignment
        + 0.15 * (1.0 if not protected_violations else 0.0)
        + 0.10 * (1.0 - overload_penalty)
        + 0.15 * history_coherence
    )

    return {
        "profile_id": profile.get("profile_id", "unknown"),
        "profile_label": profile.get("label", ""),
        "reallocation_share": removed,
        "range_status": range_status,
        "range_fit": round(range_fit, 4),
        "receiving_alignment": round(receiving_alignment, 4),
        "protected_unit_violations": protected_violations,
        "overloaded_destination_units": overload_units,
        "overload_penalty": round(overload_penalty, 4),
        "history_coherence": round(history_coherence, 4),
        "history_notes": history_notes,
        "conditional_boldness": conditional_boldness,
        "overall_assessment": overall_assessment,
        "validation": validation,
        "post_allocation_shares": post_shares,
        "fit_score": round(fit_score, 4),
    }


@mcp.tool(description=("Evaluate the hidden benchmark consequences of a proposed allocation plan. "
                       "Use this only after the CEO decision has been made, not during the decision."))
def evaluate_reallocation_outcome(
    company_id: Annotated[str, Field(description="Scenario company id")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
    allocation_plan: Annotated[dict, Field(description="Allocation plan with from_units and to_units dictionaries")],
) -> dict:
    validation = validate_reallocation_plan(company_id, target_date, allocation_plan)
    benchmark = get_benchmark_context(company_id, target_date)
    hidden = _hidden_outcome_targets(company_id, target_date)
    current = _current_unit_shares(company_id, target_date)
    history = get_decision_history(company_id, '2020-01-01', target_date)
    history = [h for h in history if h['date'] < target_date]
    from_units, to_units, removed = _plan_stats(allocation_plan)

    profiles = hidden.get("acceptable_profiles") or _default_profiles(hidden)
    guardrails = hidden.get("history_guardrails") or {
        "lookback": 2,
        "repeat_funding_penalty_units": hidden.get("overloaded_if_funded", []) or hidden.get("preferred_receiving_units", [])[:1],
        "repeat_cut_penalty_units": hidden.get("protected_units", []),
    }
    history_penalty, history_notes = _history_penalties(history, from_units, to_units, guardrails)

    profile_outcomes = [
        _profile_outcome(profile, removed, to_units, from_units, current, validation, benchmark, history_penalty, history_notes)
        for profile in profiles
    ]
    best = max(profile_outcomes, key=lambda x: x["fit_score"])

    if best["range_status"] == "under_reallocated":
        downside = hidden["downside_if_too_conservative"]
    elif best["range_status"] == "over_reallocated":
        downside = hidden["downside_if_too_aggressive"]
    else:
        downside = "No primary range-based downside triggered."

    return {
        "company_id": company_id,
        "date": target_date,
        "difficulty_tier": benchmark.get("difficulty_tier", "tension"),
        "selected_profile_id": best["profile_id"],
        "selected_profile_label": best["profile_label"],
        "profile_candidates": profile_outcomes,
        "reallocation_share": removed,
        "range_status": best["range_status"],
        "range_fit": best["range_fit"],
        "receiving_alignment": best["receiving_alignment"],
        "protected_unit_violations": best["protected_unit_violations"],
        "overloaded_destination_units": best["overloaded_destination_units"],
        "overload_penalty": best["overload_penalty"],
        "history_coherence": best["history_coherence"],
        "history_notes": best["history_notes"],
        "conditional_boldness": best["conditional_boldness"],
        "overall_assessment": best["overall_assessment"],
        "validation": validation,
        "post_allocation_shares": best["post_allocation_shares"],
        "primary_downside": downside,
    }


@mcp.tool(description=("Score a proposed allocation plan after the decision using a deterministic, "
                       "scenario-conditioned rubric. Use only post hoc, not during the CEO decision."))
def score_reallocation_decision(
    company_id: Annotated[str, Field(description="Scenario company id")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
    allocation_plan: Annotated[dict, Field(description="Allocation plan with from_units and to_units dictionaries")],
) -> dict:
    outcome = evaluate_reallocation_outcome(company_id, target_date, allocation_plan)
    validation_ok = 1.0 if outcome["validation"]["is_valid"] else 0.0
    protected_ok = 1.0 if not outcome["protected_unit_violations"] else 0.0
    overload_ok = 1.0 - outcome["overload_penalty"]
    history_ok = outcome["history_coherence"]

    component_scores = {
        "validation": round(25.0 * validation_ok, 2),
        "range_fit": round(20.0 * outcome["range_fit"], 2),
        "receiving_alignment": round(20.0 * outcome["receiving_alignment"], 2),
        "protected_units": round(10.0 * protected_ok, 2),
        "overload_avoidance": round(10.0 * overload_ok, 2),
        "history_coherence": round(15.0 * history_ok, 2),
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
        "difficulty_tier": outcome["difficulty_tier"],
        "total_score": total_score,
        "grade": grade,
        "component_scores": component_scores,
        "outcome": outcome,
    }


if __name__ == "__main__":
    mcp.run()
