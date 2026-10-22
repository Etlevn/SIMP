# SIMP Dataset Layout

This document describes the **current** local data layout for the SIMP root project: a modular Python pipeline (paths relative to the repository root).

`permno` (permanent security ID from the research database) is the primary join key across tables. Prefer it over tickers, which change over time.

**Publishing note:** `data/cache/`, `data/raw/` (except placeholders), `data/review/` (except placeholders), and `output/data/` are **gitignored**. Do not commit licensed extracts or generated ledgers.

---

## Directory hierarchy

```text
SIMP/
├── data/
│   ├── raw/                 # Authorized corporate-action CSV (local; not in Git)
│   ├── cache/               # Fetched / normalized working tables (reusable; not in Git)
│   └── review/              # Decision CSVs + generated artifacts/ (not in Git)
├── output/data/             # Simulation artifacts (not in Git)
├── src/                     # Runtime modules (e.g. `from pipeline import …` with PYTHONPATH=src)
├── scripts/                 # Optional helpers (e.g. review_auto_pending.py)
├── corporate_actions_template.csv  # Empty schema for the raw CA CSV
├── config.py                # User-editable run parameters (edit this)
├── data_loader.py           # Entrypoint (reads config.py; no hard-coded knobs)
└── requirements.txt         # Python dependencies
```

Runtime entrypoint (from repo root; `data_loader.py` adds `src/` to `sys.path`):

```bash
python data_loader.py
```

Optional assisted review (only updates PENDING rows in the two decision CSVs):

```bash
PYTHONPATH=src python scripts/review_auto_pending.py
```

Edit all experiment / mode settings in [`config.py`](config.py). [`data_loader.py`](data_loader.py) only orchestrates `src/*` modules.

| `config.py` setting       | Meaning                                                                                                                                                                                                     |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `INPUT_MODE`              | `"cache"` = reuse local `data/`; `"fetch_wrds"` = live institutional DB pull into `data/cache` (config value name is historical).                                                                           |
| `RESET_REVIEW_TO_PENDING` | `False` (default): one run does data → keep review → `output/data/`. `True`: reset decisions to PENDING, **clear** stale `output/data/` (keeps `.gitkeep`), and **stop** (edit CSVs, set False, run again). |
| `START_DATE` / …          | Experiment window, schemas, AUM, ledger policy.                                                                                                                                                             |

The authorized corporate-action CSV is a **local file** under `data/raw/` (no vendor API). Empty column template: [`corporate_actions_template.csv`](corporate_actions_template.csv).

| Path           | Role                                                                |
| -------------- | ------------------------------------------------------------------- |
| `data/raw/`    | True external sources that are not produced by our pipeline logic   |
| `data/cache/`  | Research-DB pulls and normalized CA tables used as the single cache |
| `data/review/` | Reconciliation and approve/exclude gate before ledger replay        |
| `output/data/` | End products: orders, fills, holdings, NAV, action journal          |

Optional later inputs (not required for the v1 ledger) may live under `data/raw/` or `data/external/` when introduced: benchmark returns, execution-vendor ID mapping, execution observations, investor flows.

Typical three-step refresh when rebuilding review:

1. `RESET_REVIEW_TO_PENDING=True` → `python data_loader.py` (stop after PENDING reset)
2. `PYTHONPATH=src python scripts/review_auto_pending.py` (optional; remaining PENDING default to EXCLUDE via `EXCLUDE_REMAINING_PENDING`)
3. `RESET_REVIEW_TO_PENDING=False` → `python data_loader.py` (ledger → `output/data/`)

---

## `data/raw/`

### `bloomberg_corporate_actions.csv`

Authorized third-party corporate-action export (manual import). Filename is historical; the pipeline does **not** call a vendor API. Copy columns from `corporate_actions_template.csv`.

| Field                          | Meaning                                              |
| ------------------------------ | ---------------------------------------------------- |
| `event_id`                     | Stable vendor-side event identifier                  |
| `permno`                       | Often empty; resolved later via identifier history   |
| `ticker`, `cusip`              | Security identifiers for matching                    |
| `event_type`                   | e.g. `CASH_DIVIDEND`, `STOCK_SPLIT`, spin-offs, etc. |
| `ex_date`                      | Ex-date / effective date (primary event date)        |
| `pay_date`                     | Cash payment date for dividends                      |
| `announce_date`, `record_date` | Announcement and record dates                        |
| `cash_per_share`               | Dividend amount per share                            |
| `split_ratio`                  | Split multiplier (e.g. 2 = 2-for-1)                  |
| `currency`                     | Currency code; USD required for simple USD ledger    |
| `notes`                        | Free-text vendor metadata                            |

Optional: raw research-DB extract dumps may also be stored here for audit; otherwise fetch results may be written straight into `data/cache/`.

---

## `data/cache/`

Single working cache for research-DB-derived tables and the normalized corporate-action CSV. Table filenames with `wrds_` / `bloomberg_` prefixes are **historical module names** used by the code.

### `weights.csv`

Point-in-time index membership and **capitalization weight proxies** (not official float-adjusted index weights).

| Field                         | Meaning                                                    |
| ----------------------------- | ---------------------------------------------------------- |
| `nominal_date`                | Nominal calendar / announcement reference                  |
| `reference_date`              | Date used for market-cap / membership observation          |
| `date`                        | Decision / snapshot date when these target weights apply   |
| `permno`, `permco`            | Security and company IDs                                   |
| `ticker`, `cusip`, `hdrcusip` | Display / secondary identifiers                            |
| `siccd`, `nasdissuno`         | Industry / listing metadata (not required for core ledger) |
| `market_cap`                  | Market capitalization used for the proxy weight            |
| `cap_weight_proxy`            | Weight in \[0, 1\]; should sum to 1 per `date`             |
| `cap_weight_proxy_pct`        | Same weight as a percentage                                |

### `daily_prices.csv`

Raw (unadjusted) closes for valuation and close-proxy execution.

| Field         | Meaning                                                           |
| ------------- | ----------------------------------------------------------------- |
| `permno`      | Security ID                                                       |
| `date`        | Trading session                                                   |
| `close_price` | Raw close; corporate actions are applied explicitly in the ledger |
| `price_flag`  | Quality flag; fills require an observed trade close (e.g. `TR`)   |

### `calendar.csv`

| Field  | Meaning                                         |
| ------ | ----------------------------------------------- |
| `date` | Trading day in the selected research-DB release |

May be derived as distinct dates from `daily_prices.csv`; kept as an explicit file for clarity and validation.

### `wrds_corporate_actions.csv`

Normalized research-DB distribution events (dividends, splits, etc.).

| Field                                                 | Meaning                                             |
| ----------------------------------------------------- | --------------------------------------------------- |
| `source`                                              | Source tag (e.g. research-DB label in extracts)     |
| `source_event_id`                                     | Unique ID within the source                         |
| `permno`                                              | Security ID                                         |
| `event_type`                                          | Normalized type (`CASH_DIVIDEND`, `STOCK_SPLIT`, …) |
| `ex_date`, `pay_date`, `announce_date`, `record_date` | Event calendar                                      |
| `cash_per_share`, `split_ratio`                       | Cash dividend or split factor                       |
| `currency`                                            | Currency                                            |
| `raw_ticker`, `raw_cusip`                             | Vendor identifiers before / beside `permno`         |
| `new_permno`                                          | Successor security when relevant                    |
| `source_detail`                                       | Vendor subtype detail                               |
| `requires_review`, `review_reason`                    | Flags for the review gate                           |

### `bloomberg_corporate_actions.csv`

Same schema as the research-DB normalized event table, after mapping CSV rows through historical identifiers. Distinct from the **raw** file under `data/raw/`.

### `wrds_id_history.csv`

Dated ticker / CUSIP ranges for aligning CSV identifiers to `permno`.

| Field                                   | Meaning                                          |
| --------------------------------------- | ------------------------------------------------ |
| `permno`                                | Security ID                                      |
| `secinfostartdt`, `secinfoenddt`        | Inclusive effective interval for the identifiers |
| `ticker`, `cusip`, `cusip9`, `hdrcusip` | Identifiers valid on that interval               |
| `securitynm`                            | Security name                                    |

### `wrds_delist_events.csv` (optional / secondary)

Same normalized event schema as corporate actions; delistings. The v1 ledger typically excludes these from automatic accounting.

### `wrds_adj_factors.csv` (optional / validation)

| Field                         | Meaning                                     |
| ----------------------------- | ------------------------------------------- |
| `permno`                      | Security ID                                 |
| `dlycaldt`                    | Calendar date                               |
| `dlycumfacpr`, `dlycumfacshr` | Cumulative price / share adjustment factors |

Used to **cross-check** splits, not as a substitute for explicit split accounting (avoids double-counting with cash dividends).

### Other cache files

| File                            | Role                                    |
| ------------------------------- | --------------------------------------- |
| `unified_corporate_actions.csv` | Reconciled dual-source match table      |
| `coverage.json`                 | Window, universe, and integrity digests |
| `manifest.json`                 | Table digests for cache verification    |
| `wrds_table_report.csv`         | Optional table inventory / diagnostics  |

---

## `data/review/`

Review is a hard gate: unapproved events must not enter the daily ledger. Every event candidate and every scope-quarantine row must be `APPROVE` or `EXCLUDE` with a non-empty rationale before `run_ledger` proceeds.

Layout:

```text
data/review/
├── event_decisions.csv    # human-edited (auto_pending may update PENDING rows only)
├── scope_decisions.csv    # human-edited (auto_pending may update PENDING rows only)
└── artifacts/             # generated / supporting files (do not hand-edit)
```

Root of `data/review/` holds **only** the two decision tables (plus `.gitkeep`). Everything else belongs under `artifacts/`.

### `event_decisions.csv`

Working table for in-universe corporate-action candidates: side-by-side source terms, match status, and the final decision. Written/refreshed as PENDING by `review_init`; edited by humans or `scripts/review_auto_pending.py`.

| Field                                 | Meaning                                                          |
| ------------------------------------- | ---------------------------------------------------------------- |
| `review_key`                          | Content fingerprint; approvals invalidate if source terms change |
| `match_status`                        | e.g. agree / conflict / research-DB-only / CSV-only              |
| `wrds_event_id`, `bloomberg_event_id` | Linked source IDs (column names are historical)                  |
| `permno`, `event_type`, `ex_date`     | Alignment key                                                    |
| `wrds_*` / `bloomberg_*`              | Parallel cash, split, and pay-date fields for comparison         |
| `decision`                            | `APPROVE`, `EXCLUDE`, or `PENDING`                               |
| `chosen_source_event_id`              | Which source row supplies booked terms when approving            |
| `rationale`                           | Required written reason when not PENDING                         |
| `evidence_url`                        | Optional issuer / regulator evidence                             |
| `requires_review`, `review_reason`    | Inherited normalization / conflict flags                         |

### `scope_decisions.csv`

Quarantine for records outside the experiment universe or unsupported types (missing `ex_date`, announcement-only spin-offs, etc.): `decision` (usually `EXCLUDE`), `rationale`, optional `evidence_url` / outcome fields. Ledger requires every row to be `EXCLUDE` with a rationale.

### `artifacts/` (generated)

Produced by `data_loader` / review helpers. Supporting files live under this subfolder so the review root stays decision-only.

| File                                          | Role                                                                                                                         |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `event_candidates.csv`                        | Fresh fingerprint view rewritten from cache at review/ledger time; must match decisions' `review_key` set                    |
| `scope_quarantine.csv`                        | Scoped-out source rows paired with `scope_decisions.csv`                                                                     |
| `split_factor_checks.csv`                     | Cumulative-factor cross-check for splits                                                                                     |
| `approved_events.csv`                         | Materialized rows eligible for `simulate` (ordinary USD cash dividends and ordinary splits in v1; `requires_review = False`) |
| `approval_audit.csv`                          | Audit trail for the approve gate                                                                                             |
| `complex_action_evidence.csv`                 | Optional issuer terms for complex actions still PENDING / EXCLUDE                                                            |
| `remaining_review.csv`, `price_review.csv`, … | Supporting audits from assisted review / price checks                                                                        |
| `backups/`                                    | Local timestamped copies (gitignored; not required for ledger)                                                               |

`scripts/review_auto_pending.py` updates **only** the two root decision CSVs; it does not create new artifact files.

---

## `output/data/`

Produced by the daily fund simulation; not vendor extracts. Cleared when `RESET_REVIEW_TO_PENDING=True`.

### `orders.csv`

| Field             | Meaning                              |
| ----------------- | ------------------------------------ |
| `decision_date`   | Date the target portfolio is decided |
| `permno`          | Security                             |
| `signed_quantity` | Signed shares (+ buy, − sell)        |
| `decision_price`  | Close used when sizing the order     |

### `fills.csv`

| Field                               | Meaning                                            |
| ----------------------------------- | -------------------------------------------------- |
| `decision_date`, `execution_date`   | Decision session and next-session close-proxy fill |
| `permno`                            | Security                                           |
| `signed_requested`, `signed_filled` | Requested vs filled signed quantity                |
| `unfilled_quantity`                 | Residual (e.g. cash shortfall)                     |
| `execution_price`, `decision_price` | Fill vs decision prices                            |
| `fee`                               | Fee amount (often zero in the prototype)           |
| `executed_reference_drift`          | Rough signed notional gap vs decision price        |

### `daily_holdings.csv`

| Field                                     | Meaning                      |
| ----------------------------------------- | ---------------------------- |
| `date`, `permno`, `shares`, `close_price` | End-of-day position snapshot |

### `daily_nav.csv`

| Field                    | Meaning                                                |
| ------------------------ | ------------------------------------------------------ |
| `date`                   | Valuation date                                         |
| `cash`                   | Cash balance                                           |
| `equity_value`           | Mark-to-market of holdings                             |
| `receivables`            | Accrued dividends not yet paid                         |
| `nav`                    | `cash + equity_value + receivables`                    |
| `external_flow`          | Subscriptions / redemptions                            |
| `fund_units`, `unit_nav` | Units and unit NAV                                     |
| `fees`                   | Fees charged that day                                  |
| `fund_return`            | Unit-NAV return (flows must not fabricate performance) |

### `action_journal.csv`

| Field                               | Meaning                              |
| ----------------------------------- | ------------------------------------ |
| `date`, `source_event_id`, `permno` | When and which approved event        |
| `share_change`                      | Share delta from splits              |
| `accrued`, `paid`                   | Dividend accrual and cash settlement |

Also written: `excluded_security_snapshots.csv`, `validation_summary.csv`, and flat
`run_provenance.json` (`start`/`end`/`aum`/`cash_buffer_bps`/`fee_bps` plus `files` sha256 of
manifest, coverage, the two decision CSVs, `research_pipeline.py`, and `data_loader.py`).

---

## How tables connect (pipeline)

```text
data/raw/<authorized CA CSV>
        + live DB fetch (INPUT_MODE=fetch_wrds) or reuse (INPUT_MODE=cache)
            → data/cache/ (weights, prices, calendar, events, id_history, …)
            → data/review/event_decisions.csv
               data/review/scope_decisions.csv
               data/review/artifacts/… (candidates, approved_events, audits)
            → simulate(weights, prices, calendar, approved_events)
            → output/data/ (orders, fills, holdings, nav, action_journal)
```

Finance summary:

- **Weights + prices** drive target holdings, orders, marks, and close-proxy fills.
- **Approved corporate actions** adjust shares and cash (accrual then payment for dividends; share multipliers for splits).
- Together they produce the **daily fund ledger**, which is the primary research output before execution / TCA extensions.

v1 bookable set: ordinary USD `CASH_DIVIDEND` and ordinary `STOCK_SPLIT`. Complex actions (spin-offs, mergers, CVRs, special cash, etc.) stay `EXCLUDE` or `PENDING` until accounting exists.

---

## Layout principles

1. **No Stage-named folders** — `raw` / `cache` / `review` / `output/data`.
2. **One cache** — refresh (`fetch_wrds`) vs reuse (`cache`) in one program.
3. **One entrypoint** — fund panel → corporate actions → review gate → daily ledger.
4. **Review gate preserved** — approvals/exclusions with rationales required; assisted review may fill PENDING but does not silently invent complex-event accounting.
5. **Simulation outputs first-class** under `output/data/`. Future execution-vendor mapping, benchmark, execution, and flow files remain optional extensions outside the v1 minimal cache.

In short: universe → prices → reviewed corporate actions → daily NAV, with a layout designed for one configurable program (`config.py` + `data_loader.py` + modules under `src/`).
