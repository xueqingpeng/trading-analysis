---
name: auditing
description: >
  Audits XBRL numeric facts in SEC-style filings by comparing the reported (book)
  value from the instance document against the correct (true) expected value derived
  from the filing's calculation linkbase, US-GAAP taxonomy, and XBRL sign conventions.
  The correct value may be a summation recomputation, a sign correction for directional
  concepts (expenditures, losses must be positive), or an algebraic derivation.
  Handles fact extraction, context resolution, period matching, balance-type checking,
  and writing the final JSON result to results/auditing/.

  Use this skill whenever the user asks to audit a filing, verify a reported XBRL
  value, compute a calculated value from linkbases, or check numeric consistency in
  a 10-K or 10-Q — even if they phrase it as "what is the reported value of X",
  "audit this concept", "check the filing math", or "verify AssetsCurrent for FY2021".
---

# Auditing Skill

You are auditing a **single** XBRL numeric fact in an SEC-style filing. You
call MCP tools on the `auditing_mcp` server to read the filing's XBRL files
and the US-GAAP taxonomy, reason over what you see, then run the
`write_audit.py` CLI script to record the result.

Everything you know about the filing comes from the MCP tools described
below. Do not parse the XML files yourself.

Your job is to:
1. Extract the **reported (book) value** — the number literally stored in the filing's instance document.
2. Determine the **correct (true) value** — what the number *should be* in a valid XBRL filing.
3. Run `write_audit.py` to write one line of JSON to the output file.

These two values may differ. The correct value is **not always a mathematical recomputation** — it depends on the nature of the concept:
- For **summation concepts** (a concept that is the parent of children in `*_cal.xml`): recompute by summing weighted children.
- For **directional concepts** (expenditures, losses, deductions, contra-assets): the correct value is always a **positive absolute value** — the sign is encoded in the concept semantics, not in a negative number.
- For **child concepts** (a component within a parent sum): derive algebraically from the parent and siblings.
- For **other concepts** with no calculation relationships: report the value as found if it is consistent with its balance type.

Integrity of the audit depends on never substituting taxonomy-inferred
relationships for filing-specific ones, and never silently mismatching periods
or contexts.

---

## Inputs

The user invocation specifies:

| Parameter     | Example                    | Notes |
|---------------|----------------------------|-------|
| `ticker`      | `rrr`, `zions`             | **lowercase** as it appears in folder names |
| `issue_time`  | `20231231`                 | `YYYYMMDD` |
| `filing_name` | `10k`, `10q`               | lowercase |
| `concept_id`  | `us-gaap:AssetsCurrent`    | exact concept name including namespace prefix |
| `period`      | `FY2021`, `Q3 2022`, `2021-12-31`, `2021-01-01 to 2021-12-31` | user's expression |

A typical user request looks like:

```
Please audit the value of us-gaap:AdjustmentsRelatedToTaxWithholdingForShareBasedCompensation
for 2023-01-01 to 2023-12-31 in the 10k filing released by rrr on 2023-12-31.
What's the reported value? What's the actual value calculated from the relevant
linkbases and US-GAAP taxonomy?
```

---

## Data access — XBRL files via MCP

Tools on the `auditing_mcp` server. Always start with `find_filing` to
resolve the filing folder, then layer on the other tools as the audit
unfolds.

| Tool | Purpose |
|---|---|
| `find_filing(ticker, filing_name, issue_time)` | Resolve the filing folder under `{data_root}/XBRL/`. Returns `{filing_path, filing_year, files: {htm, cal, xsd, def, lab, pre}, found, message}`. **Always call first** — `filing_path` and `filing_year` feed into the other tools. |
| `get_facts(filing_path, concept_id, period)` | Extract numeric facts whose context period **exactly** matches `period`. Returns `{matched, all_periods_found}`. Use `matched[0]` (non-dimensional first) as the reported value. Use `all_periods_found` to diagnose period misses. |
| `get_calculation_network(filing_path, concept_id)` | Return the calculation-linkbase relationships: `as_parent` (Case A children with weights), `as_child` (Case C parent + siblings), `is_isolated` (Case D). |
| `get_concept_metadata(filing_path, concept_id, taxonomy_year)` | Look up `balance` (`debit`/`credit`/`none`/`unknown`), `period_type` (`instant`/`duration`), `label`, `is_directional_hint` (Case B heuristic). Resolves via the filing's `*.xsd` first, then taxonomy `chunks_core.jsonl`. Pass `taxonomy_year = filing_year` from `find_filing`. |

### Period grammar (for `get_facts`)

| Form | Example | Resolves to |
|---|---|---|
| Instant | `2023-12-31` | instant context on that date |
| Explicit duration | `2023-01-01 to 2023-12-31` | duration context with these bounds |
| Calendar fiscal year | `FY2023` | `2023-01-01` to `2023-12-31` (non-Dec fiscal years must use the explicit form) |
| Calendar quarter | `Q3 2023` | `2023-07-01` to `2023-09-30` |

If `matched` is empty, look at `all_periods_found` and reconsider: the user's
period may be expressed as a fiscal year that doesn't actually align to a
calendar year, or the concept may only appear under a dimensional context.

### Concept name normalization

`concept_id` accepts either the colon (`us-gaap:Liabilities`) or underscore
(`us-gaap_Liabilities`) form — both normalize identically inside the server.
Locator hrefs in `*_cal.xml` (underscore) and instance QNames (colon) are
already reconciled by `get_calculation_network` and `get_facts`.

### Writing the result — `write_audit.py` (CLI, not MCP)

Use the standalone script `.claude/skills/auditing/scripts/write_audit.py`
via the Bash tool to write the audit result. It owns all the file-I/O logic
(sanitize filename, ensure output dir, write the single-line JSON) so you
don't have to write inline Python. See "Output" below for the full call.

---

## The audit workflow

Work through this checklist in order. Never skip steps or reorder them.

### Step 1 — Locate the filing

```
find_filing(ticker, filing_name, issue_time)
```

If `found=false`, stop and report `message` to the user — do not run the
write step. On success, keep `filing_path` and `filing_year` for later
calls.

### Step 2 — Concept metadata first (it tells you which Case applies)

```
get_concept_metadata(filing_path, concept_id, taxonomy_year=filing_year)
```

Use the result to plan:

- `balance` — `"debit"` / `"credit"` / `"none"` / `"unknown"`. Governs Case B.
- `period_type` — sanity-check this against the period the user is asking
  about. If you're querying an `instant` period for a `duration` concept (or
  vice versa), `get_facts` will return no matches.
- `is_directional_hint` — **weak** heuristic: `True` when label + balance
  match a small keyword list (loss/expense/treasury/contra/withholding…).
  The list is not exhaustive — many directional concepts return `False`
  here. **Do not use `is_directional_hint == false` as evidence that Case
  B does not apply.** The real Case B trigger is `balance` +
  extracted-value sign — see Step 5.
- `source` — `"xsd"` (filing-specific concept), `"taxonomy"` (standard
  US-GAAP), or `"not_found"` (sanity-check `concept_id` for typos).

### Step 3 — Extract reported facts

```
get_facts(filing_path, concept_id, period)
```

**Default selection.** Take `matched[0]` as `extracted_value`. The list
is ranked non-dimensional first, then numeric-parseable first — this is
the right pick for a typical balance-sheet / income-statement read.

**Audit-mode awareness — what the candidates list is telling you.**
When `matched` has multiple entries, scan all of them before committing.
Two patterns matter:

- **Mixed signs.** If some facts are positive and some are negative,
  this is a strong signal — but not a deterministic rule. Read the
  prompt: is the user asking for the *total* (in which case the
  non-dimensional positive total is usually right) or for the *sign
  error this audit is meant to catch* (in which case the negative
  dimensional outlier is the audit target)? When the prompt names a
  specific segment / member / instrument, use that. Otherwise, the
  default `matched[0]` is correct.
- **All same sign with similar magnitude.** Pure totals; default pick.

If you do pick a non-default candidate (a specific dimensional fact),
say which dimension you picked and why — this is auditable reasoning,
not a hidden choice.

If `matched` is empty:
- **Do not silently retry with a different period.** Report
  `requested_period_canonical` and `all_periods_found` in your
  reasoning. Your job is to surface the mismatch, not to guess whether
  the user meant a fiscal-year period that differs from what they
  literally wrote. A common silent-wrong failure: user writes `FY2023`
  meaning calendar 2023, but the filing uses a non-Dec fiscal year
  (e.g. ends 09-30); retrying with the filing's fiscal boundaries
  returns the wrong number under a misleading guise of success.
- **Default:** `extracted_value = "0"`, treat the audit as
  unable-to-locate, `calculated_value = "0"`. Reasoning must cite
  `all_periods_found` explicitly so the prompt can be corrected.
- **The only allowed retries** are:
  - The user's prompt explicitly authorizes it (e.g. "use the fiscal
    year, which ends 09-30").
  - The user's literal period is **mechanically equivalent** to a
    period in `all_periods_found` after a unit-only conversion (e.g.
    user wrote `2023-12-31` and the filing uses `2023-12-31T00:00:00`,
    which the MCP normalizes — these will already match in practice).
- If the user explicitly requested a dimensional fact, take the
  dimensional candidate that matches the requested axis/member.

Set `extracted_value` = the chosen fact's `value` (verbatim string —
do **not** reformat or round). If nothing was found and you cannot
derive a value, record `"0"`.

### Step 4 — Build the calculation network

```
get_calculation_network(filing_path, concept_id)
```

Branch on the result:

- `as_parent` non-empty → **Case A** is in play. The summation children
  with their `weight` are listed under each role. If multiple roles return
  the concept as a parent, prefer the role whose child coverage best
  matches the available facts (i.e. you can `get_facts` for most children
  in the chosen parent's period).
- `as_child` non-empty → **Case C** is in play. Each entry has a `parent`
  and the full sibling list, **including** the target concept (so you can
  read its own `weight` directly).
- `is_isolated == true` → **Case D**.

### Step 5 — Determine the correct (calculated) value

Cases are **not mutually exclusive** — a concept can match more than one.
Apply every case that matches and combine the results: Case A gives the
numeric value, Case B enforces the sign. If both A and B apply,
`calculated_value = abs(sum of weighted children)`.

---

**Case A — Summation parent** (`as_parent` non-empty)

For each child concept in the chosen role:
1. `get_facts(filing_path, child_concept, period)` using the **same** period
   you used for the parent (carry it through verbatim — do not re-derive).
2. Pick the candidate that **strictly matches the parent fact's dimension
   signature**. If the parent is non-dimensional, the child must be
   non-dimensional. **Do not substitute a dimensional fact for a missing
   non-dimensional child** — segment-level (dimensional) facts are
   disaggregations, not children of the non-dim total. If no matching
   candidate exists, that child counts as missing (see below).
3. Multiply `child.value × arc.weight` and sum across all children.

→ `calculated_value = Σ (weight × child_value)`

**Missing-fact handling.** If a child concept has no matching numeric
fact in the period (after the strict dimension-signature match in
step 2), treat that child as contributing **0** to the sum and continue
with the remaining children. The audit answer is then the partial sum.

In your reasoning, **explicitly list every child that was treated as
missing** and explain why (e.g. "child concept X has no non-dimensional
fact for period Y; KapowEventsMember dimensional fact not used because
parent is non-dimensional"). This converts a silent partial-sum into
auditable reasoning — the evaluator sees both the sum and what went
into it.

Why this differs from Case C: in Case A the parent is being recomputed
from a known structure (the calc role's children); a missing child
unambiguously contributes 0 to that sum. In Case C the unknown is
absorbed into the algebraic residual, which silently corrupts the
answer — that's why Case C uses a stricter rule (below).

---

**Case B — Sign correction for monetary concepts**

In XBRL, **debit-balance and credit-balance monetary concepts are filed as
positive absolute values**. Sign is encoded in the concept semantics
(via the `balance` attribute), not in the value. A negative number in the
instance document for a `balance ∈ {"debit", "credit"}` concept is almost
always a filing error — and that's exactly what this audit is here to
detect.

**Default trigger — apply Case B when ALL of these hold:**

1. `balance` from `get_concept_metadata` is **not** `"unknown"`. That is:
   - `"debit"` or `"credit"` — monetary concepts, sign encoded in
     balance attribute.
   - `"none"` — non-monetary numeric concepts (notional amounts, share
     counts, rates, percentages, durations). XBRL doesn't give these a
     balance attribute, but they are still single-direction values —
     a notional amount, a share count, a rate are not negative.
2. `extracted_value < 0`.
3. The concept is **not** a net-change / two-direction item, and **not**
   a cumulative balance-sheet equity item. Inspect the concept's local
   name (the part after `:`) and skip Case B when it contains any of:
   - `IncreaseDecreaseIn…` — cash-flow change items, allowed to be ±
   - `Net…Change…`, `…NetOfTax…`, `…NetOf…` — net items, sign carries info
   - `AdjustmentsToReconcile…` — reconciliation summation parents
   - `NetIncome…`, `NetLoss…`, `NetEarning…`, `ProfitLoss…` —
     income-statement bottom-line items where the sign records
     profit-vs-loss outcome
   - `Gain…OrLoss…`, `…GainLoss…`, `GainsLosses…` — net gain-or-loss items
   - `NetCashProvidedByUsedIn…` — cash-flow statement net items where the
     sign records inflow-vs-outflow
   - `StockholdersEquity`, `Equity*`, `EquityAttributableTo…` —
     total-equity concepts legitimately allow negative values
     (deficit equity from accumulated losses)
   - `RetainedEarnings…`, `AccumulatedDeficit`, `AccumulatedOther…` —
     cumulative balance-sheet items where the sign records cumulative
     profit-vs-loss or comprehensive-income direction

That's the rule. **Do not invent a domain-specific justification for a
negative value.** The whole point of the audit is to catch sign-encoding
errors that look mathematically consistent in the filer's own linkbase.
If you find yourself reaching for an accounting-standards explanation of
why a negative value is fine on a non-excluded concept, that is the
moment to apply Case B instead.

`is_directional_hint == true` is a **bonus** signal (catches some
positive-but-mis-classified facts), but `is_directional_hint == false`
is **not** evidence against Case B. The keyword list inside the MCP is
small and misses many directional concepts — trust `balance` + sign over
the heuristic.

**Apply:**
- If the trigger holds → `calculated_value = abs(extracted_value)`.
- If `extracted_value ≥ 0` → `calculated_value = extracted_value`
  (already correct; no change needed even for directional concepts).

**When `extracted_value` is a dimensional fact** (a specific segment /
member / instrument), skip the Case A/C recomputation. Case A operates
on non-dimensional totals; Case C uses a parent and siblings — neither
maps to a single dimensional fact's audit. The audit answer for a
dimensional sign error is `abs(extracted_value)` (when Case B triggers)
or `extracted_value` itself otherwise.

---

**Case C — Calculation child only** (`as_child` non-empty, `as_parent` empty)

Pick one entry from `as_child` (typically there is only one). Let `P` be the
`parent` and the siblings list contain rows like
`{concept, weight, order}` — **including the target concept itself**. Then:

1. `get_facts(filing_path, P, period)` → `parent_value`.
2. For every sibling `S` other than the target, `get_facts(filing_path,
   S.concept, period)` and compute `Σ S.weight × S.value`.
3. Find the target's own row in the siblings list to get `own_weight`.

→ `calculated_value = (parent_value − Σ_other (sibling_weight × sibling_value)) / own_weight`

Use exact weights and matching contexts/dimensions for all sibling and
parent facts.

**Missing-fact handling.** If `parent_value` has no matching fact in
the period, OR if any non-target sibling has no matching numeric fact,
the algebraic derivation is incomplete. Do **not** complete the formula
with the available facts and report the residual — the residual will
absorb whatever is missing and produce a misleading `calculated_value`.
Instead:

1. Note in your reasoning which parent / sibling fact(s) are missing.
2. Set `calculated_value = "0"` to mark the case as unable-to-verify.
3. If Case B also applies, report `calculated_value = abs(extracted_value)`
   instead of `"0"` — sign correction is verifiable without the
   algebraic derivation.

---

**Case D — No calculation relationships and neutral balance type**

`is_isolated == true` and Case B does not apply. No recomputation is
possible and no sign correction is required.

→ `calculated_value = extracted_value` (report as found).

State explicitly that no calculation network was found and no sign
correction applies.

---

### Step 6 — Write the result

**Run `write_audit.py` via the Bash tool.** Do NOT write inline Python for
the write step, and do NOT generate the JSON yourself.

```bash
python .claude/skills/auditing/scripts/write_audit.py \
    --filing-name 10k --ticker rrr --issue-time 20231231 \
    --concept-id us-gaap:AssetsCurrent \
    --period "FY2023" \
    --model claude-sonnet-4-6 \
    --extracted-value -1234567000 \
    --calculated-value 1234567000
```

| Flag | Value |
|---|---|
| `--filing-name` | `filing_name` from the request (lowercase) |
| `--ticker` | `ticker` from the request (lowercase) |
| `--issue-time` | `issue_time` from the request (`YYYYMMDD`) |
| `--concept-id` | `concept_id` from the request (verbatim, including the namespace prefix) |
| `--period` | `period` from the request (verbatim, the same string you passed to `get_facts`) |
| `--model` | Your actual model identifier, e.g. `claude-sonnet-4-6` |
| `--extracted-value` | Numeric string **verbatim** as it appears in the instance document (may be negative); `"0"` if not found |
| `--calculated-value` | Numeric string of the correct expected value per Case A/B/C/D; `"0"` if not determinable |
| `--output-root` | **Pass the value the caller specified in the invocation** (e.g. `/io/slot1`). Falls back to `results/auditing` (relative to cwd) only if no value was given — that default is rarely writable inside a sandbox, so omitting it usually causes a `PermissionError`. |

The script writes:

```
{output_root}/auditing_{filing_name}-{ticker}-{issue_time}_{concept}_{period}_{model}.json
```

Example: `results/auditing/auditing_10k-rrr-20231231_us-gaap-AssetsCurrent_FY2023_claude-sonnet-4-6.json`

The `concept`, `period`, and `model` components are sanitized — any
character outside `[A-Za-z0-9._-]` becomes `-`.

The file body is exactly one line:

```json
{"extracted_value": "-1234567000", "calculated_value": "1234567000"}
```

Calling the script again with the same arguments **overwrites** the file.

---

## Ambiguity handling

Pay extra attention when:

- `find_filing` succeeds but the concept appears as a parent in several
  calculation roles (`as_parent` has multiple entries).
- The filing uses extension concepts (custom hrefs) that change the expected
  subtotal — `get_concept_metadata.source == "xsd"` is the signal.
- The selected calculation role has many missing children when you call
  `get_facts` on each.
- Multiple candidate facts survive period filtering with non-dimensional vs.
  dimensional rows.

Surface the ambiguity in a brief note before writing the output file — but
the output file must still contain exactly one JSON line.

---

## What NOT to do

- Do **not** parse the XBRL XML yourself — call MCP tools.
- Do **not** read the human-readable `.htm` file. The six XBRL files
  (`*_htm.xml`, `*_cal.xml`, `*_def.xml`, `*_lab.xml`, `*_pre.xml`, `*.xsd`)
  are exposed via `find_filing.files`; the rest of the tools handle them.
- Do **not** silently switch period types (instant ↔ duration, quarter ↔
  YTD) or silently substitute a fiscal-year period for the literal one
  the user wrote when the user's period yields no matches. Surface the
  mismatch and report `"0"` instead — see Step 3 for the full rule.
- Do **not** substitute a dimensional fact for a missing non-dimensional
  child in Case A summation. Treat the child as missing (contributes 0
  to the partial sum) and list it in your reasoning — see Step 5 Case A.
- Do **not** report a partial Case C residual as `calculated_value`
  when any sibling or the parent fact is missing. The residual silently
  absorbs the missing value. Set `calculated_value = "0"` (or
  `abs(extracted_value)` if Case B also triggers) — see Step 5 Case C.
- Do **not** confuse arc direction in Case A vs Case C: parents are
  `as_parent`, children are `as_child`. The MCP already resolves
  `xlink:from`/`xlink:to` for you.
- Do **not** report a negative `calculated_value` when the Case B
  trigger holds (see Step 5: `balance != "unknown"` + extracted value
  negative + concept not in the net-change / cumulative-equity exclusion
  list). Those must be `abs(...)` regardless of what the linkbase
  residual works out to.
- Do **not** replace filing-specific calculation networks with taxonomy-only
  relationships. The MCP already prefers filing networks; do not invent
  parent/child links that aren't in `get_calculation_network`.
- Do **not** create temporary scripts, debug logs, or intermediate files.
- Do **not** write inline Python via Bash heredoc (`python - <<'PY' … PY`)
  to produce the result JSON — use `write_audit.py`.
- Do **not** add any text outside the JSON on the output line — the script
  handles that for you.

---

## Implementation approach

1. Parse the user's request into the 5 inputs (`ticker`, `issue_time`,
   `filing_name`, `concept_id`, `period`).
2. `find_filing(ticker, filing_name, issue_time)` → `filing_path`, `filing_year`.
   If `found=false`, stop and report.
3. `get_concept_metadata(filing_path, concept_id, filing_year)` → `balance`,
   `period_type`, `is_directional_hint`, `label`. **Case B is on the
   table whenever `balance != "unknown"`** (i.e. `debit` / `credit` /
   `none` — and `none` covers non-monetary numerics like notional
   amounts, share counts, rates). `is_directional_hint` only adds
   confidence; it does not gate Case B.
4. `get_facts(filing_path, concept_id, period)` → `extracted_value =
   matched[0].value`. If `matched` is empty, look at `all_periods_found`
   and decide whether to retry with a different period form or record
   `"0"`.
5. `get_calculation_network(filing_path, concept_id)` → branch:
   - `as_parent` non-empty → **Case A** (sum weighted children — call
     `get_facts` for each child in the same period).
   - `as_child` non-empty → **Case C** (parent − Σ siblings, then ÷ own_weight).
   - `is_isolated` → **Case D** (`calculated_value = extracted_value`).
6. **Case B sign check** — if `balance != "unknown"` AND
   `extracted_value < 0` AND the concept's local name is **not** in any
   of the net-change / cumulative-equity exclusion patterns (full list
   in Step 5 Case B), then `calculated_value = abs(...)`. Do not look
   for reasons why a negative value is "fine" — that is the filing error
   this audit is designed to catch.
7. Run `python .claude/skills/auditing/scripts/write_audit.py` via Bash
   with the eight required flags (`--filing-name`, `--ticker`,
   `--issue-time`, `--concept-id`, `--period`, `--model`,
   `--extracted-value`, `--calculated-value`). Do not write inline Python.

One audit in, one record out.
