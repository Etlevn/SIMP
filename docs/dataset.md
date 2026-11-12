# SIMP dataset layout

Schemas for the flat local inputs under `data/` and the Stage 2 fund outputs under `output/2_fund/`. Permanent security ID `permno` is the primary join key across tables.

**Publishing note:** contents of `data/` (except placeholders such as `.gitkeep`) and `output/` stage folders are **gitignored**. Do not commit licensed extracts or generated ledgers.

For product intent and how to run the pipeline, see the root [`README.md`](../README.md).

---

## Directory hierarchy

```text
SIMP/
├── data/
│   ├── weights.csv          # required
│   ├── daily_prices.csv     # required
│   ├── calendar.csv         # required
│   ├── events.csv           # required (WRDS distributions → dividend/split rows)
│   └── coverage.json        # optional WRDS-fetch snapshot (not a validity gate)
├── output/2_fund/           # Stage 2 fund artifacts (four CSVs)
├── config.py                # experiment window, fund & flow knobs
├── docs/dataset.md          # this file
└── pyproject.toml
```

| `config.py` setting | Meaning |
| ------------------- | ------- |
| `START_DATE` / `END_DATE` | Experiment window (Stage 1 validate/extract; Stage 2 snapshots / fund end) |
| `WEIGHTS_START` / `STOCK_SCHEMA` / `BATCH_SIZE` | Used when Stage 1 must fetch WRDS |
| `INITIAL_CASH` / `CASH_BUFFER_BPS` / `FEE_BPS` | Stage 2 fund sizing |
| `FLOW_*` / `FLOW_BAND_BPS` | Stage 2 synthetic investor-flow knobs (always applied) |

Stage 2 rebalance fills are always **reference-close** (decide on the session before `weights.reference_date`, fill at that reference close).

### Stage 1 — validate or overwrite

`scripts/run_1_data.py` checks the four CSVs against the experiment window (`coverage.json` is ignored as a gate). Hard failure (missing files/columns, missing `START_DATE` weight snapshot, or incomplete START–END calendar/price span) triggers a WRDS overwrite of the four CSVs plus a fresh `coverage.json`. Missing sessions *before* the first `reference_date` only warn — Stage 2 may pad them.

Force refresh: delete or corrupt `data/` then re-run Stage 1.

```bash
PYTHONPATH=src python scripts/run_1_data.py
```

### Stage 2 — fund daybook (local only)

```bash
PYTHONPATH=src python scripts/run_2_fund.py
```

Reference-close needs `weights.reference_date` and calendar/prices covering the session **before** the first reference date (typically Thursday decision / Friday fill). A WRDS fetch pads ~14 calendar days before `START_DATE` for that purpose.

---

## `data/weights.csv`

Point-in-time index membership and **capitalization-weight proxies**. Stage 1 write path keeps only:

| Field | Meaning |
| ----- | ------- |
| `date` | Post-rebalance snapshot session (often Monday after the reference Friday) |
| `reference_date` | Date used for market-cap / membership observation |
| `permno` | Security ID |
| `cap_weight_proxy` | Weight in [0, 1]; should sum to 1 per `date` |

Readers still tolerate extra columns on older fat caches until the next Stage 1 overwrite.

---

## `data/daily_prices.csv`

Raw (unadjusted) closes for valuation and close-proxy execution.

| Field | Meaning |
| ----- | ------- |
| `permno` | Security ID |
| `date` | Trading session |
| `close_price` | Raw close; corporate actions are applied explicitly in the ledger |
| `price_flag` | Quality flag; fills require an observed trade close (e.g. `TR`) |

---

## `data/calendar.csv`

| Field | Meaning |
| ----- | ------- |
| `date` | Trading day in the selected research-DB release |

---

## `data/events.csv`

Corporate actions that enter the daybook. Normative source: WRDS/CRSP `stkdistributions` via `fetch_wrds`, filtered to `CASH_DIVIDEND` and `STOCK_SPLIT` only (no dual-source reconcile on the main path). Stage 1 write path keeps only:

| Field | Meaning |
| ----- | ------- |
| `source_event_id` | Unique ID within the source |
| `permno` | Security ID |
| `event_type` | `CASH_DIVIDEND` or `STOCK_SPLIT` |
| `ex_date`, `pay_date` | Event calendar |
| `split_ratio`, `cash_per_share` | Cash dividend or split factor |
| `currency` | Currency (USD for the simple ledger) |

Upstream normalize may still compute `requires_review` in memory; Stage 1 refuses to write while any such flag is true (resolve or drop before `write_flat_inputs`).

---

## `data/coverage.json`

Optional **WRDS-fetch provenance** only. Written when Stage 1 overwrites via WRDS; **not** read by `validate_local` or Stage 2. Stale or missing coverage does not trigger a refetch. Changing `START_DATE` does not require editing this file.

| Field | Meaning |
| ----- | ------- |
| `start`, `end` | Window used for that fetch |
| `schema` | Research-DB stock schema (e.g. `crsp_q_stock`) |
| `universe` | List of `permno` in that fetch |
| `extra_sha256` | Digests for `daily_prices.csv` / `calendar.csv` at fetch time |

---

## `output/2_fund/`

Stage 2 (`run_2_fund`) writes **only** these four CSVs. `action_journal`, `rebalance_schedule`, `synthetic_flows`, and validation/provenance stay in memory. The terminal prints scalar summaries plus table previews of the disk files.

### Hard-name exclusions (fixed)

Always applied before simulation (not a config switch):

- Drop any `permno` with a reverse stock split in the window (`STOCK_SPLIT` with `split_ratio < 1`).
- Drop snapshot rows whose holding window has missing/nonpositive closes, or whose decision/execution days lack a `TR` trade close.
- Renormalize remaining weights to sum to 1.

This is a **simulator simplification** (see README). Exclusion counts print on the terminal only.

### `daily_nav.csv`

| Field | Meaning |
| ----- | ------- |
| `date` | Session |
| `cash` | Cash after fills / flows / fees |
| `equity_value` | Marked equity |
| `receivables` | Accrued dividends not yet paid |
| `nav` | Cash + equity + receivables |
| `external_flow` | Dealing cash flow applied that day |
| `fund_units` | Synthetic unit count |
| `unit_nav` | NAV per unit |
| `fees` | Trading fees that day |
| `fund_return` | Unit-NAV return |

### `daily_holdings.csv`

| Field | Meaning |
| ----- | ------- |
| `date`, `permno` | Session and security |
| `shares` | End-of-day shares |
| `close_price` | Valuation close |

### `orders.csv`

| Field | Meaning |
| ----- | ------- |
| `order_id` | Stable id for the intended trade |
| `decision_date` | Session when the order was sized |
| `permno`, `side`, `signed_quantity` | Instrument and signed size |
| `decision_price` | Close used at decision |
| `reason` | `INITIAL` / `REBALANCE` / `INFLOW` / `OUTFLOW` |
| `expected_execution_date` | Next session (reference close for rebalances) |
| `snapshot_date`, `reference_date` | Index snapshot metadata (NaT for pure flow orders) |

### `fills.csv`

| Field | Meaning |
| ----- | ------- |
| `order_id`, `reason` | Link back to the order |
| `decision_date`, `execution_date` | Decide vs fill sessions |
| `permno` | Security |
| `signed_requested`, `signed_filled`, `unfilled_quantity` | Requested vs executed |
| `decision_price`, `execution_price` | Prices at decide / fill |
| `fee` | Fee charged on the fill |
| `executed_reference_drift` | `filled × (execution_price − decision_price)` |

---

## Data flow

```text
data/{weights, daily_prices, calendar, events}
        → load / verify (coverage ignored as a gate)
        → hard-name exclusions + renormalize
        → Stage 2: fund_simulator.simulate_fund
          (reference-close schedule + synthetic flows)
        → output/2_fund/{daily_nav, daily_holdings, orders, fills}.csv
```

Stage 1 WRDS overwrite refreshes the four core flat files and writes `coverage.json` as a fetch snapshot. Requires **Python 3.12** and a project install including `wrds`. Events come only from CRSP `stkdistributions`.

---

## Library helpers (not the main path)

`ledger.simulate` / `pipeline.run_ledger` remain available as library helpers (including next-session control). Older dual-source review helpers under `src/` are **not** wired into `run_1_data` / `run_2_fund`.
