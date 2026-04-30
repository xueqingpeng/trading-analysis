#!/usr/bin/env python3
#Write one report_evaluation JSON artifact and companion Markdown summary.

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize(value: str) -> str:
    return _FILENAME_SAFE_RE.sub("_", value)


def _fmt(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _mean_score(scores: dict[str, Any]) -> str:
    vals = [v for v in scores.values() if isinstance(v, (int, float))]
    if not vals:
        return "N/A"
    return f"{sum(vals) / len(vals):.2f}"


def _clean_for_utf8(value: Any) -> Any:

    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_clean_for_utf8(v) for v in value]
    if isinstance(value, dict):
        return {
            _clean_for_utf8(k) if isinstance(k, str) else k: _clean_for_utf8(v)
            for k, v in value.items()
        }
    return value


def _make_markdown(payload: dict[str, Any]) -> str:
    agent = payload.get("agent", "")
    symbol = payload.get("symbol") or payload.get("ticker") or ""
    model = payload.get("model", "")
    evaluation_date = payload.get("evaluation_date") or date.today().isoformat()
    rubric_version = payload.get("rubric_version", "")
    reports = payload.get("per_report") or []
    run = payload.get("run_metrics") or {}

    lines: list[str] = []
    lines.append(f"# Report Evaluation: {symbol}")
    lines.append("")
    lines.append(f"**Agent:** {agent} | **Model:** {model} | **Evaluation Date:** {evaluation_date}")
    lines.append(f"**Rubric:** {rubric_version} | **Reports Evaluated:** {payload.get('reports_evaluated', len(reports))}")
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## 1. Overall Assessment")
    lines.append("")
    lines.append(str(payload.get("overall_assessment") or "No overall assessment provided."))
    lines.append("")

    lines.append("## 2. Run Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Rating Distribution | {_fmt(run.get('rating_distribution'))} |")
    lines.append(f"| Mean 5D Forward Return by Rating | {_fmt(run.get('mean_forward_return_per_rating_5d'))} |")
    lines.append(f"| Mean 20D Forward Return by Rating | {_fmt(run.get('mean_forward_return_per_rating_20d'))} |")
    lines.append(f"| Reports with Full Horizons | {_fmt(run.get('n_with_full_horizons'))} |")
    lines.append(f"| Hit Rate 5D | {_fmt(run.get('hit_rate_5d'))} |")
    lines.append(f"| Hit Rate 20D | {_fmt(run.get('hit_rate_20d'))} |")
    lines.append(f"| Mean Dimension Scores | {_fmt(run.get('mean_dimension_scores'))} |")
    lines.append("")

    lines.append("## 3. Strengths and Weaknesses")
    lines.append("")
    lines.append("### Consistent Strengths")
    strengths = payload.get("consistent_strengths") or []
    lines.extend([f"- {item}" for item in strengths] if strengths else ["- None recorded."])
    lines.append("")
    lines.append("### Consistent Weaknesses")
    weaknesses = payload.get("consistent_weaknesses") or []
    lines.extend([f"- {item}" for item in weaknesses] if weaknesses else ["- None recorded."])
    lines.append("")

    lines.append("## 4. Per-Report Scorecard")
    lines.append("")
    lines.append("| Date | Rating | Avg Score | Quant | Structure | Metadata | Evidence | Reasoning | 5D Outcome | 20D Outcome |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|---|")
    for item in reports:
        extracted = item.get("extracted") or {}
        scores = item.get("scores") or {}
        forward = item.get("forward_performance") or {}
        lines.append(
            "| {date} | {rating} | {avg} | {q} | {s} | {m} | {e} | {r} | {o5} | {o20} |".format(
                date=_fmt(item.get("report_date")),
                rating=_fmt(extracted.get("rating")),
                avg=_mean_score(scores),
                q=_fmt(scores.get("quantitative_alignment")),
                s=_fmt(scores.get("structure_and_format")),
                m=_fmt(scores.get("metadata_accuracy")),
                e=_fmt(scores.get("evidence_fidelity")),
                r=_fmt(scores.get("reasoning_quality")),
                o5=_fmt(forward.get("rating_outcome_5d")),
                o20=_fmt(forward.get("rating_outcome_20d")),
            )
        )
    lines.append("")

    lines.append("## 5. Per-Report Notes")
    lines.append("")
    for item in reports:
        filename = item.get("filename") or "unknown file"
        report_date = item.get("report_date") or "unknown date"
        extracted = item.get("extracted") or {}
        forward = item.get("forward_performance") or {}
        horizons = forward.get("horizons") or {}
        lines.append(f"### {report_date} — {filename}")
        lines.append("")
        lines.append(f"- **Rating:** {_fmt(extracted.get('rating'))}")
        lines.append(f"- **Forward Returns:** {_fmt(horizons)}")
        lines.append(f"- **Notes:** {item.get('notes') or 'No notes.'}")
        warnings = extracted.get("parse_warnings") or []
        if warnings:
            lines.append(f"- **Parse Warnings:** {_fmt(warnings)}")
        lines.append("")

    lines.append("## 6. Artifact Details")
    lines.append("")
    lines.append("This Markdown file is generated mechanically by `upsert_evaluation.py` from the JSON evaluation payload.")
    lines.append("The JSON file remains the source of truth for detailed metric diffs and evidence checks.")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="upsert_evaluation",
        description="Write one report_evaluation JSON artifact plus Markdown summary.",
    )
    symbol_group = parser.add_mutually_exclusive_group(required=True)
    symbol_group.add_argument("--symbol", help="Stock symbol, e.g. NVDA")
    symbol_group.add_argument("--ticker", help="Alias for --symbol, kept for compatibility")
    parser.add_argument("--agent", required=True, help="Target report-generation agent, e.g. claude-code")
    parser.add_argument("--model", required=True, help="Target report-generation model, e.g. claude-sonnet-4-6")
    parser.add_argument(
        "--output-root",
        default="results/report_evaluation",
        help="Output directory (default: results/report_evaluation, relative to cwd)",
    )
    args = parser.parse_args()
    symbol = args.symbol or args.ticker

    raw = sys.stdin.read()
    if not raw.strip():
        parser.error("empty evaluation JSON on stdin; nothing to write")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        parser.error(f"stdin must be valid JSON: {exc}")
    if not isinstance(payload, dict):
        parser.error("stdin JSON must be an object")

    payload = _clean_for_utf8(payload)

    payload.setdefault("status", "completed")
    payload.setdefault("agent", args.agent)
    payload.setdefault("symbol", symbol)
    payload.setdefault("ticker", symbol)
    payload.setdefault("model", args.model)
    payload.setdefault("evaluation_date", date.today().isoformat())
    payload.setdefault("reports_evaluated", len(payload.get("per_report") or []))

    safe_agent = _sanitize(args.agent).lower()
    safe_symbol = _sanitize(symbol)
    safe_model = _sanitize(args.model).lower()
    stem = f"{safe_agent}_report_evaluation_{safe_symbol}_{safe_model}"

    out_dir = Path(args.output_root)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        parser.error(
            f"Cannot create output directory {out_dir}: {exc}. "
            "Pass --output-root=<writable path> — use whatever directory the caller specified."
        )

    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_make_markdown(payload), encoding="utf-8")

    summary = {
        "path": str(json_path),
        "markdown_path": str(md_path),
        "symbol": symbol,
        "agent": args.agent,
        "model": args.model,
        "status": payload.get("status"),
        "reports_evaluated": payload.get("reports_evaluated"),
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
