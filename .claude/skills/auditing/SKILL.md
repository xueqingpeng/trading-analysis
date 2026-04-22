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

You are auditing a single XBRL numeric fact in an SEC-style filing. Your job is to:
1. Extract the **reported (book) value** — the number literally stored in the filing's instance document.
2. Determine the **correct (true) value** — what the number *should be* in a valid XBRL filing.
3. Write one line of JSON to the output file.

These two values may differ. The correct value is **not always a mathematical recomputation** — it depends on the nature of the concept:
- For **summation concepts** (a concept that is the parent of children in `*_cal.xml`): recompute by summing weighted children.
- For **directional concepts** (expenditures, losses, deductions, contra-assets): the correct value is always a **positive absolute value** — the sign is encoded in the concept semantics, not in a negative number.
- For **child concepts** (a component within a parent sum): derive algebraically from the parent and siblings.
- For **other concepts** with no calculation relationships: report the value as found if it is consistent with its balance type.

Integrity of the audit depends on never substituting taxonomy-inferred relationships
for filing-specific ones, and never silently mismatching periods or contexts.
Read this skill carefully before starting.

For input and output paths, the user will provide them directly. For example:
"The data is at `/data/auditing`", "Please save results to `/results/auditing`".

A typical user request looks like:

```
Please audit the value of us-gaap:AdjustmentsRelatedToTaxWithholdingForShareBasedCompensation
for 2023-01-01 to 2023-12-31 in the 10k filing released by rrr on 2023-12-31.
What's the reported value? What's the actual value calculated from the relevant
linkbases and US-GAAP taxonomy? (id: mr_1)
The input data is at /data/auditing, please save the output to /results/auditing.
```

---

## Setup

### Parse the request into these parameters

| Parameter     | Example                    | Notes |
|---------------|----------------------------|-------|
| `agent_name`  | `claude-code`, `codex`     | your agent name, e.g. "claude-code"; used in output filename |
| `ticker`      | `rrr`, `zions`             | **lowercase** as it appears in folder names |
| `issue_time`  | `20231231`                 | format `YYYYMMDD` |
| `filing_name` | `10k`, `10q`               | lowercase |
| `concept_id`  | `us-gaap:AssetsCurrent`    | exact concept name including namespace prefix |
| `period`      | `FY2021`, `Q3 2022`, `2021-12-31`, `2021-01-01 to 2021-12-31` | user's expression |
| `id`          | `mr_1`                     | the value from `(id: ...)` in the user's request; used verbatim in the output filename |
| `model`       | `claude-sonnet-4-6`        | your model identifier from system context; sanitize for filename use |

### Locate the filing folder

```
data/auditing/XBRL/{filing_name}-{ticker}-{issue_time}/
```

Example: `data/auditing/XBRL/10k-zions-20231231/`

Within that folder, you need these files:

| File pattern  | Purpose |
|---------------|---------|
| `*_htm.xml`   | Instance document — all reported facts and contexts **(primary)** |
| `*_cal.xml`   | Calculation linkbase — summation-item relationships + locators **(primary)** |
| `*.xsd`       | Extension schema — concept definitions, use if extension names are unclear |
| `*_def.xml`   | Definition linkbase — dimensional relationships, use if needed |
| `*_lab.xml`   | Label linkbase — human-readable concept labels, use if needed |
| `*_pre.xml`   | Presentation linkbase — statement structure, use if needed |
| `*.htm`       | **Ignore** — human-readable HTML, never used |

### Locate the taxonomy folder

```
data/auditing/US_GAAP_Taxonomy/gaap_chunks_{year}/
```

Where `{year}` matches the filing year derived from `issue_time` (e.g., `20231231` → `2023`).
Each taxonomy folder contains:
- `chunks_core.jsonl` — concept labels and types (one JSON object per line)
- `chunks_relations.jsonl` — taxonomy-level relationships
- `meta.json` — taxonomy metadata

Use taxonomy files only to clarify concept semantics and sanity-check results —
never to replace filing-specific calculation networks.

### Ensure the output directory exists

```
results/auditing/
```

Create it if it doesn't exist yet.

---

## The audit workflow

Work through this checklist in order. Never skip steps or reorder them.

### Step 1 — Extract reported facts for the target concept

Open `*_htm.xml`. Find all elements whose local name matches the concept from
`concept_id` (strip the `us-gaap:` prefix — e.g., `us-gaap:AssetsCurrent` →
look for elements named `AssetsCurrent`). For each matching element:

- Note its numeric value
- Note its `contextRef` attribute
- Follow `contextRef` to the matching `<context id="...">` element in the same file
- Read the period from that context:
  - `<instant>` → instant date
  - `<startDate>` + `<endDate>` → duration range
- Keep only facts whose resolved context period matches the user's requested period exactly

**Do not determine the period type first and then search.** Always resolve
`fact → contextRef → context id → period` before filtering.

Do not silently switch between:
- instant and duration
- quarter-only and year-to-date
- current period and prior period
- consolidated and dimensional contexts

### Step 2 — Select the best candidate fact

Rank candidates by these preferences (highest first):

1. Exact concept match
2. Exact period match (after resolving contextRef)
3. No dimensions before dimensional facts
4. Numeric facts before non-numeric facts

Use the top-ranked fact as `extracted_value` unless the user explicitly asks for a
segmented or dimensional fact. If multiple candidates remain equally plausible, report
the ambiguity rather than forcing a single answer.

### Step 3 — Build the calculation network from `*_cal.xml`

The calculation linkbase has two parts you must read together:

**Part A — Locators** (`<link:loc>` elements):
Each locator maps a label string to a concept name via its `xlink:href`:
```xml
<link:loc xlink:label="loc_us-gaap_Liabilities_UUID"
          xlink:href="https://...#us-gaap_Liabilities"/>
```
The concept name is the fragment after `#` (e.g., `us-gaap_Liabilities`).
Build a lookup table: `label → concept_name`.

> **Normalization:** href fragments use underscores (`us-gaap_Liabilities`) while
> `concept_id` in the request uses a colon (`us-gaap:Liabilities`). Treat them as
> equivalent — normalize by replacing `:` with `_` (or vice versa) before matching.

**Part B — Arcs** (`<link:calculationArc>` elements):
```xml
<link:calculationArc xlink:from="loc_us-gaap_Liabilities_UUID"
                     xlink:to="loc_us-gaap_Deposits_UUID"
                     weight="1.0" .../>
```
**Arc direction: `xlink:from` = PARENT (the sum), `xlink:to` = CHILD (a component).**

Using your locator lookup table, resolve `xlink:from` and `xlink:to` labels to
actual concept names. Then determine the target concept's role in the network:

- **As a parent (Case A):** find arcs where the resolved `from` concept matches the target `concept_id`. Those arcs' `to` concepts (with their `weight`) are the calculation children. Note the calculation role (`xlink:role` on the enclosing `<link:calculationLink>`). If multiple roles contain the concept as a parent, prefer the role whose child coverage best matches the available facts.

- **As a child (Case C):** find arcs where the resolved `to` concept matches the target `concept_id`. Note the parent concept (`from`) and all sibling `to` concepts with their weights in the same role — these are needed for the algebraic derivation in Step 4.

- **Neither:** the concept has no calculation relationships; Case D applies.

### Step 4 — Determine the correct (calculated) value

First, check the concept's **balance type** from `*.xsd` (attribute `balance="debit"` or
`balance="credit"`) or from `chunks_core.jsonl` in the taxonomy. This tells you the
concept's directional nature and governs which case applies below.

Cases are **not mutually exclusive** — a concept can match more than one. Apply every case that matches and combine the results: Case A gives the numeric value, Case B enforces the sign. If both A and B apply, `calculated_value` = `abs(sum of weighted children)`.

---

**Case A — Summation parent** (the target concept appears as `xlink:from` in `*_cal.xml`)

Recompute by summing weighted children:
1. Find each child fact in `*_htm.xml` (strip `us-gaap:` prefix to match element names)
2. Resolve its `contextRef` to a context
3. Keep it only if the context period matches the chosen parent fact's period exactly
4. Prefer the same dimension signature as the chosen parent fact
5. Multiply the child fact value by its arc `weight` and sum all contributions

→ `calculated_value` = sum of `weight × child_value` for all matched children.

If some children have no matching fact, the recomputation is **partial** — still
report the sum of available children but note it is partial.

---

**Case B — Directional concept** (expenditure, loss, deduction, contra-asset, or any
concept that represents an outflow or reduction — typically `balance="debit"` for
expense/loss items, or `balance="credit"` for contra-asset/contra-equity items)

In XBRL, directional concepts must always be filed as **positive absolute values**.
The sign is implied by the concept's semantics; a negative sign in the instance
document is a filing error.

- If `extracted_value` is negative → `calculated_value` = `abs(extracted_value)`
- If `extracted_value` is already positive → `calculated_value` = same value (correctly reported)

When the concept is also a summation parent (Cases A and B both apply), first recompute the sum from children (Case A), then apply the sign rule: `calculated_value` = `abs(recomputed sum)`.

---

**Case C — Calculation child only** (the target concept appears as `xlink:to` but
never as `xlink:from` in `*_cal.xml`)

Derive algebraically from the parent relationship:
`calculated_value` = `(parent_value - sum(sibling_weight × sibling_value)) / own_weight`

Use exact weights and matching contexts for all sibling and parent facts.

---

**Case D — No calculation relationships and neutral balance type**

No recomputation is possible and no sign correction is required.
→ `calculated_value` = `extracted_value` (report as found).
State explicitly that no calculation network was found and no sign correction applies.

---

## Ambiguity handling

Pay extra attention when:

- Multiple filing folders match the same ticker and issue date
- The concept appears as a parent in several calculation roles
- The filing uses extension concepts (custom `xlink:href` fragments) that change the expected subtotal
- The selected calculation role has many missing children
- Multiple candidate facts survive period filtering (dimensional vs. non-dimensional)

In these cases, surface the ambiguity in a brief note before writing the output file —
but the output file must still contain exactly one JSON line.

---

## Output format

Write a single `.json` file to:

```
results/auditing/{agent_name}_auditing_{filing_name}_{ticker}_{issue_time}_{id}_{model}.json
```

Example: `results/auditing/claude-code_auditing_10k_zions_20231231_mr_1_claude-sonnet-4-6.json`

The file must contain **exactly one line**: the final JSON object.

```json
{"extracted_value": "-1234567000", "calculated_value": "1234567000"}
```

*(Example: a loss concept was filed as negative — the correct value is the positive absolute.)*

**Field rules:**

| Field | Rule |
|-------|------|
| `extracted_value` | Numeric string **exactly as it appears** in the instance document (may be negative); `"0"` if not found |
| `calculated_value` | Numeric string of the **correct expected value** per Step 4 (Case A/B/C/D); `"0"` if not determinable |

- Output JSON only on that line. No explanation, no Markdown fences, no extra keys.
- Preserve numeric values exactly as strings (do not reformat or round).
- Write the file once, after completing both steps. Do not append or overwrite.

---

## What NOT to do

- Do not replace filing-specific calculation networks with taxonomy-only relationships
  unless the filing network is absent (and state that fallback explicitly)
- Do not silently switch period types (instant vs. duration, quarter vs. YTD)
- Do not use `.htm` files — each filing folder contains six XBRL files (`*_htm.xml`, `*_cal.xml`, `*_def.xml`, `*_lab.xml`, `*_pre.xml`, `*.xsd`); the primary ones are `*_htm.xml` and `*_cal.xml`, but the others may be consulted if needed
- Do not confuse arc direction: `xlink:from` = parent (sum), `xlink:to` = child (component)
- Do not report a negative `calculated_value` for directional concepts (expenditures, losses, deductions) — these must always be positive absolute values in valid XBRL
- Do not use locator label strings as concept names — always resolve via `<link:loc xlink:href>`
- Do not create temporary scripts, debug logs, or intermediate files
- Do not write multiple output files for the same audit run
- Do not add any text outside the JSON on the output line

---

## Implementation approach

The cleanest approach: write a short inline Python script via the Bash tool that
parses the XML files, resolves all locators and contexts, applies the correct Case
(A/B/C/D), and writes the final JSON. Keep all computation in memory. Do not save
the script to disk.

Recommended libraries: `xml.etree.ElementTree` for XML parsing, `json` for output,
`re` or string operations for concept name normalization. No third-party packages
needed.

Suggested script structure:
1. Parse `*_htm.xml` → build `{concept_name: [(value, period, dimensions)]}` lookup
2. Parse `*.xsd` or search `chunks_core.jsonl` → get balance type of target concept
3. Parse `*_cal.xml` → build locator table, then identify the concept as parent / child / neither
4. Apply Case A/B/C/D to compute `calculated_value`
5. Print `{"extracted_value": "...", "calculated_value": "..."}` and write to output file
