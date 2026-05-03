from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SEED_PATH = ROOT / "data" / "seed"
DEFAULT_DB_PATH = ROOT / "env" / "resource_reallocation_env.duckdb"


def _connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def _exec_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(SCHEMA_PATH.read_text())


def _clear_tables(conn: duckdb.DuckDBPyConnection) -> None:
    for table in [
        "company_states",
        "business_units",
        "reallocation_constraints",
        "unit_constraints",
        "role_briefs",
        "benchmark_context",
        "decision_history",
        "hidden_outcome_targets",
    ]:
        conn.execute(f"DELETE FROM {table}")


def _merge_seed_docs(docs: list[dict]) -> dict:
    merged: dict[str, list[dict]] = {}
    for doc in docs:
        for company in doc.get("companies", []):
            merged.setdefault(company["company_id"], [])
            merged[company["company_id"]].extend(company.get("dates", []))

    companies = []
    for company_id, dates in sorted(merged.items()):
        dedup = {snapshot["date"]: snapshot for snapshot in dates}
        companies.append(
            {
                "company_id": company_id,
                "dates": [dedup[date] for date in sorted(dedup.keys())],
            }
        )
    return {"companies": companies}


def load_seed(seed_path: Path) -> dict:
    if seed_path.is_dir():
        docs = [json.loads(path.read_text()) for path in sorted(seed_path.glob("*.json"))]
        if not docs:
            raise FileNotFoundError(f"No seed json files found in {seed_path}")
        return _merge_seed_docs(docs)
    return json.loads(seed_path.read_text())


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def populate(conn: duckdb.DuckDBPyConnection, seed: dict) -> None:
    for company in seed["companies"]:
        company_id = company["company_id"]
        for snapshot in company["dates"]:
            dt = snapshot["date"]
            state = snapshot["company_state"]
            conn.execute(
                """
                INSERT INTO company_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    company_id,
                    dt,
                    state["cash_runway_months"],
                    state["net_debt_to_ebitda"],
                    state["revenue_growth_pct"],
                    state["gross_margin_pct"],
                    state["transformation_pressure"],
                    state["capacity_constraint"],
                    state["board_priority"],
                ],
            )

            for unit in snapshot["business_units"]:
                conn.execute(
                    """
                    INSERT INTO business_units VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        company_id,
                        dt,
                        unit["unit_id"],
                        unit["current_capex_share"],
                        unit["roi_trend"],
                        unit["growth_outlook"],
                        unit["execution_risk"],
                        unit["absorptive_capacity"],
                        unit["strategic_role"],
                    ],
                )

            constraints = snapshot["reallocation_constraints"]
            conn.execute(
                """
                INSERT INTO reallocation_constraints VALUES (?, ?, ?, ?)
                """,
                [
                    company_id,
                    dt,
                    constraints["reallocatable_share_cap"],
                    _dumps(constraints["transfer_rules"]),
                ],
            )
            for unit_id, unit_limits in constraints["unit_constraints"].items():
                conn.execute(
                    """
                    INSERT INTO unit_constraints VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        company_id,
                        dt,
                        unit_id,
                        unit_limits["floor_share"],
                        unit_limits["ceiling_share"],
                        unit_limits["locked_share"],
                    ],
                )

            for role, brief in snapshot["role_briefs"].items():
                conn.execute(
                    """
                    INSERT INTO role_briefs VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        company_id,
                        dt,
                        role,
                        brief["priority"],
                        _dumps(brief["watch_for"]),
                        _dumps(brief["must_optimize"]),
                        _dumps(brief["veto_if"]),
                    ],
                )

            benchmark = snapshot["benchmark_context"]
            conn.execute(
                """
                INSERT INTO benchmark_context VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    company_id,
                    dt,
                    benchmark["mckinsey_reference"],
                    benchmark["boldness_threshold"],
                    benchmark["scenario_note"],
                    _dumps(benchmark["boldness_is_desirable_if"]),
                    _dumps(benchmark["boldness_is_undesirable_if"]),
                ],
            )

            for decision in snapshot.get("decision_history", []):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO decision_history VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        company_id,
                        decision["date"],
                        _dumps(decision["allocation_plan"]),
                        decision["decision_type"],
                        decision["reallocation_share"],
                        decision["ceo_rationale"],
                    ],
                )

            hidden = snapshot["hidden_outcome_targets"]
            conn.execute(
                """
                INSERT INTO hidden_outcome_targets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    company_id,
                    dt,
                    hidden["preferred_min_share"],
                    hidden["preferred_max_share"],
                    _dumps(hidden["preferred_receiving_units"]),
                    _dumps(hidden["protected_units"]),
                    _dumps(hidden["overloaded_if_funded"]),
                    hidden["downside_if_too_conservative"],
                    hidden["downside_if_too_aggressive"],
                ],
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the resource reallocation DuckDB environment.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--seed-path", default=str(SEED_PATH))
    args = parser.parse_args()

    db_path = Path(args.db_path)
    seed_path = Path(args.seed_path)

    seed = load_seed(seed_path)
    with _connect(db_path) as conn:
        _exec_schema(conn)
        _clear_tables(conn)
        populate(conn, seed)

    print(f"Built resource reallocation env at {db_path}")


if __name__ == "__main__":
    main()
