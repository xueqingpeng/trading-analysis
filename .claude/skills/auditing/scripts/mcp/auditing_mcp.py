"""MCP server that lets the auditing agent interact with XBRL filings.

The environment is a directory tree on disk:
    {data_root}/XBRL/{filing_name}-{ticker}-{issue_time}/      6 XBRL files
    {data_root}/US_GAAP_Taxonomy/gaap_chunks_{year}/           taxonomy chunks

Tools expose that environment to the agent: filing folder lookup, fact
extraction with period/dimension resolution, calculation-linkbase walking,
and balance/period-type metadata. Writing the final audit JSON is handled by
the sibling `write_audit.py` CLI script — this server only does reads.

Run:
    pythonauditing_mcp.py --data-root=/abs/path/to/data/auditing
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from calendar import monthrange
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Optional

from fastmcp import FastMCP
from pydantic import BaseModel, Field


# Set by __main__ before mcp.run(). Tool calls resolve through _data_root() so
# importers can also override it programmatically or via env.
DATA_ROOT: Optional[str] = None


def _data_root() -> Path:
    if DATA_ROOT:
        return Path(DATA_ROOT)
    env = os.environ.get("AUDITING_DATA_ROOT") or os.environ.get("AUDITMCP_DATA_ROOT")
    if env:
        return Path(env)
    raise RuntimeError(
        "auditing_mcp: data root not configured. Pass --data-root=<path> on the "
        "command line or set AUDITING_DATA_ROOT."
    )


# ---------------------------------------------------------------------------
# Concept-name normalization and period parsing
# ---------------------------------------------------------------------------


def _normalize_concept(concept_id: str) -> str:
    """Normalize a concept ID to `prefix:LocalName` form.

    Accepts `us-gaap_AssetsCurrent` (underscore form used in XBRL locator
    hrefs) or `us-gaap:AssetsCurrent` (QName form). Bare local names are
    returned unchanged.
    """
    if ":" in concept_id:
        return concept_id
    idx = concept_id.find("_")
    if idx <= 0:
        return concept_id
    prefix = concept_id[:idx]
    if not all(ch.isalnum() or ch == "-" for ch in prefix):
        return concept_id
    return f"{prefix}:{concept_id[idx + 1 :]}"


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})$")
_FY_RE = re.compile(r"^FY(\d{4})$")
_Q_RE = re.compile(r"^Q([1-4])\s+(\d{4})$")


@dataclass(frozen=True)
class ParsedPeriod:
    kind: Literal["instant", "duration"]
    start: str   # ISO date
    end: str     # ISO date; equal to start for instants

    @property
    def canonical(self) -> str:
        return self.start if self.kind == "instant" else f"{self.start}/{self.end}"


def _parse_period(period: str) -> ParsedPeriod:
    """Parse a user-supplied period string into a canonical ParsedPeriod."""
    period = period.strip()
    if _DATE_RE.match(period):
        return ParsedPeriod(kind="instant", start=period, end=period)
    if m := _RANGE_RE.match(period):
        return ParsedPeriod(kind="duration", start=m.group(1), end=m.group(2))
    if m := _FY_RE.match(period):
        year = m.group(1)
        return ParsedPeriod(kind="duration", start=f"{year}-01-01", end=f"{year}-12-31")
    if m := _Q_RE.match(period):
        q = int(m.group(1))
        year = int(m.group(2))
        start_month = (q - 1) * 3 + 1
        end_month = start_month + 2
        end_day = monthrange(year, end_month)[1]
        return ParsedPeriod(
            kind="duration",
            start=f"{year:04d}-{start_month:02d}-01",
            end=f"{year:04d}-{end_month:02d}-{end_day:02d}",
        )
    raise ValueError(
        f"Unparseable period {period!r}. Accepted formats: "
        "'YYYY-MM-DD', 'YYYY-MM-DD to YYYY-MM-DD', 'FYYYYY', 'QN YYYY'."
    )


# ---------------------------------------------------------------------------
# Pydantic return models
# ---------------------------------------------------------------------------


class FilingLocation(BaseModel):
    filing_path: str = ""
    filing_year: int = 0
    files: dict[str, str] = Field(default_factory=dict)
    found: bool = False
    message: str = ""


class Fact(BaseModel):
    value: str
    context_ref: str
    period_type: Literal["instant", "duration"]
    period: str  # canonical
    dimensions: dict[str, str] = Field(default_factory=dict)
    unit_ref: Optional[str] = None
    decimals: Optional[str] = None


class FactsResult(BaseModel):
    concept_id: str
    requested_period: str
    requested_period_canonical: str
    matched: list[Fact]
    all_periods_found: list[str]


class CalChild(BaseModel):
    concept: str
    weight: float
    order: Optional[float] = None


class ParentRole(BaseModel):
    role: str
    children: list[CalChild]


class ChildRole(BaseModel):
    role: str
    parent: str
    siblings: list[CalChild]


class CalculationNetwork(BaseModel):
    concept_id: str
    as_parent: list[ParentRole]
    as_child: list[ChildRole]
    is_isolated: bool
    roles_scanned: list[str]


class ConceptMetadata(BaseModel):
    concept_id: str
    balance: Literal["debit", "credit", "none", "unknown"]
    period_type: Literal["instant", "duration", "unknown"]
    label: Optional[str] = None
    source: Literal["xsd", "taxonomy", "not_found"]
    is_directional_hint: bool


# ---------------------------------------------------------------------------
# XBRL parsers (cached by absolute path)
# ---------------------------------------------------------------------------

_XBRLI_NS = "http://www.xbrl.org/2003/instance"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_LINK_NS = "http://www.xbrl.org/2003/linkbase"
_XBRLDI_NS = "http://xbrl.org/2006/xbrldi"


def _localname(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


@dataclass(frozen=True)
class _ContextInfo:
    period_kind: Literal["instant", "duration"]
    start: str
    end: str
    dimensions: tuple

    @property
    def canonical_period(self) -> str:
        return self.start if self.period_kind == "instant" else f"{self.start}/{self.end}"


@dataclass(frozen=True)
class _ParsedInstance:
    facts: tuple
    contexts: dict


def _parse_instance(htm_path: str) -> _ParsedInstance:
    return _parse_instance_cached(str(Path(htm_path).resolve()))


@functools.lru_cache(maxsize=32)
def _parse_instance_cached(htm_path: str) -> _ParsedInstance:
    uri_to_prefix: dict[str, str] = {}
    for _event, (prefix, uri) in ET.iterparse(htm_path, events=("start-ns",)):
        if prefix:
            uri_to_prefix[uri] = prefix

    tree = ET.parse(htm_path)
    root = tree.getroot()

    contexts: dict[str, _ContextInfo] = {}
    for ctx in root.findall(f"{{{_XBRLI_NS}}}context"):
        cid = ctx.get("id", "")
        period = ctx.find(f"{{{_XBRLI_NS}}}period")
        if period is None:
            continue
        instant = period.find(f"{{{_XBRLI_NS}}}instant")
        if instant is not None:
            start = end = (instant.text or "").strip()
            kind: Literal["instant", "duration"] = "instant"
        else:
            sd = period.find(f"{{{_XBRLI_NS}}}startDate")
            ed = period.find(f"{{{_XBRLI_NS}}}endDate")
            if sd is None or ed is None:
                continue
            start = (sd.text or "").strip()
            end = (ed.text or "").strip()
            kind = "duration"

        dims: list[tuple[str, str]] = []
        segment = ctx.find(f"{{{_XBRLI_NS}}}entity/{{{_XBRLI_NS}}}segment")
        scenario = ctx.find(f"{{{_XBRLI_NS}}}scenario")
        for container in (segment, scenario):
            if container is None:
                continue
            for member in container.findall(f"{{{_XBRLDI_NS}}}explicitMember"):
                axis = member.get("dimension", "")
                val = (member.text or "").strip()
                if axis and val:
                    dims.append((axis, val))
        contexts[cid] = _ContextInfo(
            period_kind=kind, start=start, end=end,
            dimensions=tuple(sorted(dims)),
        )

    fact_tuples: list[tuple] = []
    for elem in root:
        ns_uri = elem.tag.split("}", 1)[0][1:] if "}" in elem.tag else ""
        if ns_uri in (_XBRLI_NS, _LINK_NS):
            continue
        context_ref = elem.get("contextRef")
        if not context_ref:
            continue
        prefix = uri_to_prefix.get(ns_uri)
        local = _localname(elem.tag)
        qname = f"{prefix}:{local}" if prefix else local
        value = (elem.text or "").strip()
        # Skip facts with empty value: these are XBRL nil facts (xsi:nil="true"
        # or self-closing tags). For numeric audit purposes they carry no
        # information — including them would only break Case A/C recomputation
        # with ValueErrors.
        if not value:
            continue
        fact_tuples.append((qname, value, context_ref, elem.get("unitRef"), elem.get("decimals")))

    return _ParsedInstance(facts=tuple(fact_tuples), contexts=contexts)


@dataclass(frozen=True)
class _CalArc:
    role: str
    parent: str
    child: str
    weight: float
    order: Optional[float]


@functools.lru_cache(maxsize=32)
def _parse_cal_cached(cal_path: str) -> tuple[_CalArc, ...]:
    tree = ET.parse(cal_path)
    root = tree.getroot()
    arcs: list[_CalArc] = []

    for link in root.findall(f"{{{_LINK_NS}}}calculationLink"):
        role = link.get(f"{{{_XLINK_NS}}}role", "")
        label_to_concept: dict[str, str] = {}
        for loc in link.findall(f"{{{_LINK_NS}}}loc"):
            label = loc.get(f"{{{_XLINK_NS}}}label", "")
            href = loc.get(f"{{{_XLINK_NS}}}href", "")
            frag = href.split("#", 1)[-1] if "#" in href else href
            label_to_concept[label] = _normalize_concept(frag)

        for arc in link.findall(f"{{{_LINK_NS}}}calculationArc"):
            from_label = arc.get(f"{{{_XLINK_NS}}}from", "")
            to_label = arc.get(f"{{{_XLINK_NS}}}to", "")
            parent = label_to_concept.get(from_label)
            child = label_to_concept.get(to_label)
            if not parent or not child:
                continue
            weight = float(arc.get("weight", "1.0"))
            order_str = arc.get("order")
            order = float(order_str) if order_str is not None else None
            arcs.append(_CalArc(role=role, parent=parent, child=child, weight=weight, order=order))

    return tuple(arcs)


def _parse_cal(cal_path: str) -> tuple[_CalArc, ...]:
    return _parse_cal_cached(str(Path(cal_path).resolve()))


@functools.lru_cache(maxsize=32)
def _parse_xsd_cached(xsd_path: str) -> dict[str, dict[str, str]]:
    tree = ET.parse(xsd_path)
    root = tree.getroot()
    xbrli_balance = f"{{{_XBRLI_NS}}}balance"
    xbrli_period = f"{{{_XBRLI_NS}}}periodType"
    out: dict[str, dict[str, str]] = {}
    for elem in root.iter():
        if _localname(elem.tag) != "element":
            continue
        name = elem.get("name")
        if not name:
            continue
        info: dict[str, str] = {}
        if bal := elem.get(xbrli_balance):
            info["balance"] = bal
        if pt := elem.get(xbrli_period):
            info["periodType"] = pt
        if info:
            out[name] = info
    return out


def _parse_xsd(xsd_path: str) -> dict[str, dict[str, str]]:
    return _parse_xsd_cached(str(Path(xsd_path).resolve()))


@functools.lru_cache(maxsize=16)
def _load_taxonomy_core_cached(taxonomy_dir: str) -> dict[str, dict]:
    path = Path(taxonomy_dir) / "chunks_core.jsonl"
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            concept = row.get("concept_id") or row.get("concept")
            if concept:
                out[_normalize_concept(concept)] = row
    return out


def _load_taxonomy_core(taxonomy_dir: str) -> dict[str, dict]:
    return _load_taxonomy_core_cached(str(Path(taxonomy_dir).resolve()))


def _pick_file(filing_path: str, glob: str) -> str:
    matches = list(Path(filing_path).glob(glob))
    if glob == "*.xsd":
        matches = [m for m in matches if "_" not in m.stem] or matches
    if len(matches) != 1:
        raise FileNotFoundError(
            f"expected exactly one file matching {glob} in {filing_path}, found {len(matches)}"
        )
    return str(matches[0])


# ---------------------------------------------------------------------------
# Directional-hint heuristic
# ---------------------------------------------------------------------------

_DEBIT_DIRECTIONAL_TERMS = (
    "loss", "losses",
    "expense", "expenses",
    "impairment", "impairments",
    "depreciation", "depletion", "amortization",
    "writedown", "writedowns", "writeoff", "writeoffs",
    "deduction", "deductions",
    "repurchase", "repurchases",
    "decrease",
    "withholding",
)

_CREDIT_DIRECTIONAL_TERMS = (
    "contra",
    "treasury",
)

_DIRECTIONAL_EXCLUSIONS = (
    "increase",
    "reconcile",
)

_WORD_RE_CACHE: dict[str, re.Pattern[str]] = {}
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _word_match(haystack: str, term: str) -> bool:
    pat = _WORD_RE_CACHE.get(term)
    if pat is None:
        pat = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        _WORD_RE_CACHE[term] = pat
    return bool(pat.search(haystack))


def _is_directional_hint(local_name: str, label: Optional[str], balance: str) -> bool:
    split_local = _CAMEL_SPLIT_RE.sub(" ", local_name)
    haystack = f"{label or ''} {split_local}"
    if any(_word_match(haystack, exc) for exc in _DIRECTIONAL_EXCLUSIONS):
        return False
    if balance == "debit":
        return any(_word_match(haystack, t) for t in _DEBIT_DIRECTIONAL_TERMS)
    if balance == "credit":
        return any(_word_match(haystack, t) for t in _CREDIT_DIRECTIONAL_TERMS)
    return False


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("auditing_mcp")


_REQUIRED_FILE_GLOBS = {
    "htm": "*_htm.xml",
    "cal": "*_cal.xml",
    "def": "*_def.xml",
    "lab": "*_lab.xml",
    "pre": "*_pre.xml",
    "xsd": "*.xsd",
}


@mcp.tool(
    description=(
        "Locate the folder for a given XBRL filing under "
        "{data_root}/XBRL/{filing_name}-{ticker}-{issue_time}/. "
        "Returns the absolute path, derived filing year, and a map of the "
        "six XBRL files (htm, cal, xsd, def, lab, pre). Sets found=false "
        "with a diagnostic message if the folder or any required file is "
        "missing."
    )
)
def find_filing(
    ticker: Annotated[str, Field(description="Ticker, lowercase, as it appears in the folder name")],
    filing_name: Annotated[str, Field(description="Filing type, lowercase (e.g. '10k', '10q')")],
    issue_time: Annotated[str, Field(description="Issue date YYYYMMDD, e.g. '20231231'")],
) -> FilingLocation:
    folder_name = f"{filing_name}-{ticker}-{issue_time}"
    filing_dir = _data_root() / "XBRL" / folder_name
    if not filing_dir.is_dir():
        return FilingLocation(found=False, message=f"folder not found: {filing_dir}")

    files: dict[str, str] = {}
    for key, glob in _REQUIRED_FILE_GLOBS.items():
        matches = list(filing_dir.glob(glob))
        if key == "xsd":
            matches = [m for m in matches if "_" not in m.stem] or matches
        if len(matches) != 1:
            return FilingLocation(
                found=False,
                message=f"expected exactly one file matching {glob} in {filing_dir}, found {len(matches)}",
            )
        files[key] = str(matches[0])

    try:
        filing_year = int(issue_time[:4])
    except ValueError:
        return FilingLocation(found=False, message=f"bad issue_time: {issue_time!r}")

    return FilingLocation(
        filing_path=str(filing_dir),
        filing_year=filing_year,
        files=files,
        found=True,
    )


@mcp.tool(
    description=(
        "Extract numeric facts for a concept whose context period exactly "
        "matches the requested period. Period grammar: 'YYYY-MM-DD' (instant), "
        "'YYYY-MM-DD to YYYY-MM-DD' (duration), 'FYYYYY' (calendar-year "
        "duration; non-December fiscal years must use explicit ranges), "
        "'QN YYYY' (calendar quarter). Returns matched facts ranked with "
        "non-dimensional first, plus all distinct periods found for this "
        "concept to help diagnose period misses."
    )
)
def get_facts(
    filing_path: Annotated[str, Field(description="Absolute filing folder path from find_filing")],
    concept_id: Annotated[str, Field(description="Concept QName, e.g. 'us-gaap:AssetsCurrent'")],
    period: Annotated[str, Field(description="Period expression — see grammar in description")],
) -> FactsResult:
    normalized = _normalize_concept(concept_id)
    parsed_period = _parse_period(period)
    htm_path = _pick_file(filing_path, "*_htm.xml")
    instance = _parse_instance(htm_path)

    all_periods: set[str] = set()
    candidates: list[Fact] = []
    seen: set[tuple] = set()
    for qname, value, ctx_ref, unit_ref, decimals in instance.facts:
        if qname != normalized:
            continue
        ctx = instance.contexts.get(ctx_ref)
        if ctx is None:
            continue
        all_periods.add(ctx.canonical_period)
        if ctx.period_kind != parsed_period.kind:
            continue
        if ctx.start != parsed_period.start or ctx.end != parsed_period.end:
            continue
        dedup_key = (value, ctx_ref, ctx.dimensions, unit_ref, decimals)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        candidates.append(Fact(
            value=value,
            context_ref=ctx_ref,
            period_type=ctx.period_kind,
            period=ctx.canonical_period,
            dimensions={a: m for a, m in ctx.dimensions},
            unit_ref=unit_ref,
            decimals=decimals,
        ))

    def _numeric_ok(f: Fact) -> int:
        try:
            float(f.value)
            return 0
        except ValueError:
            return 1

    candidates.sort(key=lambda f: (len(f.dimensions) > 0, _numeric_ok(f)))

    return FactsResult(
        concept_id=normalized,
        requested_period=period,
        requested_period_canonical=parsed_period.canonical,
        matched=candidates,
        all_periods_found=sorted(all_periods),
    )


@mcp.tool(
    description=(
        "Return the calculation-linkbase relationships for a concept: roles "
        "where it is the summation parent (with weighted children) and roles "
        "where it appears as a child (with parent and sibling weights, "
        "including its own weight). is_isolated=true ⇒ no calculation "
        "relationships at all (Case D hint)."
    )
)
def get_calculation_network(
    filing_path: Annotated[str, Field(description="Absolute filing folder path from find_filing")],
    concept_id: Annotated[str, Field(description="Concept QName, e.g. 'us-gaap:AssetsCurrent'")],
) -> CalculationNetwork:
    normalized = _normalize_concept(concept_id)
    cal_path = _pick_file(filing_path, "*_cal.xml")
    arcs = _parse_cal(cal_path)

    roles_scanned = sorted({a.role for a in arcs})

    by_role: dict[str, list[_CalArc]] = {}
    for a in arcs:
        by_role.setdefault(a.role, []).append(a)

    as_parent: list[ParentRole] = []
    as_child: list[ChildRole] = []

    for role, role_arcs in by_role.items():
        parent_arcs = [a for a in role_arcs if a.parent == normalized]
        if parent_arcs:
            children = [CalChild(concept=a.child, weight=a.weight, order=a.order)
                        for a in parent_arcs]
            as_parent.append(ParentRole(role=role, children=children))

        child_arcs = [a for a in role_arcs if a.child == normalized]
        for ca in child_arcs:
            sibling_arcs = [a for a in role_arcs if a.parent == ca.parent]
            siblings = [CalChild(concept=a.child, weight=a.weight, order=a.order)
                        for a in sibling_arcs]
            as_child.append(ChildRole(role=role, parent=ca.parent, siblings=siblings))

    return CalculationNetwork(
        concept_id=normalized,
        as_parent=as_parent,
        as_child=as_child,
        is_isolated=not as_parent and not as_child,
        roles_scanned=roles_scanned,
    )


@mcp.tool(
    description=(
        "Return balance type, period type, label, and a directional hint for "
        "a concept. Looks up the filing's extension *.xsd first, then falls "
        "back to {data_root}/US_GAAP_Taxonomy/gaap_chunks_{taxonomy_year}/"
        "chunks_core.jsonl. is_directional_hint is a heuristic "
        "(expense/loss/contra-style keywords + balance); the agent makes the "
        "final Case B determination."
    )
)
def get_concept_metadata(
    filing_path: Annotated[str, Field(description="Absolute filing folder path from find_filing")],
    concept_id: Annotated[str, Field(description="Concept QName, e.g. 'us-gaap:AssetsCurrent'")],
    taxonomy_year: Annotated[int, Field(description="Taxonomy year, e.g. 2023")],
) -> ConceptMetadata:
    normalized = _normalize_concept(concept_id)
    local = normalized.split(":", 1)[-1]

    try:
        xsd_path = _pick_file(filing_path, "*.xsd")
        xsd_map = _parse_xsd(xsd_path)
    except FileNotFoundError:
        xsd_map = {}
    if local in xsd_map:
        info = xsd_map[local]
        bal = info.get("balance", "unknown")
        pt = info.get("periodType", "unknown")
        label = None
        return ConceptMetadata(
            concept_id=normalized,
            balance=bal if bal in ("debit", "credit", "none") else "unknown",
            period_type=pt if pt in ("instant", "duration") else "unknown",
            label=label,
            source="xsd",
            is_directional_hint=_is_directional_hint(local, label, bal),
        )

    tax_dir = _data_root() / "US_GAAP_Taxonomy" / f"gaap_chunks_{taxonomy_year}"
    tax_map = _load_taxonomy_core(str(tax_dir))
    row = tax_map.get(normalized)
    if row:
        bal_raw = row.get("balance", "")
        pt_raw = row.get("periodType") or row.get("period_type") or "unknown"
        label = row.get("label")
        if bal_raw in ("debit", "credit"):
            balance = bal_raw
        elif bal_raw in ("", "none"):
            balance = "none"
        else:
            balance = "unknown"
        return ConceptMetadata(
            concept_id=normalized,
            balance=balance,
            period_type=pt_raw if pt_raw in ("instant", "duration") else "unknown",
            label=label,
            source="taxonomy",
            is_directional_hint=_is_directional_hint(local, label, balance),
        )

    return ConceptMetadata(
        concept_id=normalized,
        balance="unknown",
        period_type="unknown",
        label=None,
        source="not_found",
        is_directional_hint=False,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="auditing_mcp MCP server")
    parser.add_argument(
        "--data-root",
        default=None,
        help="Path to the auditing data root containing XBRL/ and "
        "US_GAAP_Taxonomy/. Falls back to $AUDITING_DATA_ROOT or "
        "$AUDITMCP_DATA_ROOT.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.data_root:
        DATA_ROOT = str(Path(args.data_root).expanduser().resolve())
    elif os.environ.get("AUDITING_DATA_ROOT"):
        DATA_ROOT = os.environ["AUDITING_DATA_ROOT"]
    elif os.environ.get("AUDITMCP_DATA_ROOT"):
        DATA_ROOT = os.environ["AUDITMCP_DATA_ROOT"]
    else:
        print(
            "auditing_mcp: --data-root is required (or set AUDITING_DATA_ROOT).",
            file=sys.stderr,
        )
        sys.exit(2)
    mcp.run()
