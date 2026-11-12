# SIMP dataset layout

Schemas for flat inputs under `data/` and Phase deliverables under
`output/{0_schema,1_generator,2_fund,3_tca}/`. Permanent security ID `permno` is
the primary join key. Phase 0 four-level contract:
[`docs/order_schema.md`](order_schema.md). Real vs stand-in data:
[`docs/data_reality.md`](data_reality.md).

**Publishing note:** contents of `data/` (except placeholders such as `.gitkeep`)
are **gitignored**. The entire `output/` tree is gitignored. Do not commit
licensed extracts or generated ledgers.

Product intent: [`docs/blueprint.md`](blueprint.md). How to run: root
[`README.md`](../README.md).

---

## Directory hierarchy

```text
SIMP/
├── data/
│   ├── weights.csv                 # required
│   ├── daily_prices.csv            # required
│   ├── calendar.csv                # required
│   ├── events.csv                  # required (WRDS distributions → dividend/split rows)
│   ├── benchmark_sp500_tr.csv      # required for Phase 2 (CRSP S&P 500 TR)
│   ├── benchmark_sp500_tr.json     # fetch meta (sha256); written with the CSV
│   └── coverage.json               # optional WRDS-fetch snapshot (not a validity gate)
├── output/0_schema/                # Phase 0 example order chain
├── output/1_generator/             # Phase 1 synthetic multi-MIC chain
├── output/2_fund/                  # Phase 2 core handoff (+ optional extras)
├── output/3_tca/                   # Phase 3 order_tca + strategy_summary
├── fetch_data.py                   # independent data tool
├── main_0_schema.py                # Phase 0 entrypoint
├── main_1_generator.py             # Phase 1 entrypoint
├── main_2_fund.py                  # Phase 2 entrypoint
├── main_3_tca.py                   # Phase 3 entrypoint
├── config.py                       # experiment window, fund & flow knobs
├── docs/dataset.md                 # this file
└── pyproject.toml
```

| `config.py` setting | Meaning |
| ------------------- | ------- |
| `START_DATE` / `END_DATE` | Experiment window (`fetch_data` validate/extract; Phase 2 snapshots / fund end) |
| `WEIGHTS_START` / `STOCK_SCHEMA` / `BATCH_SIZE` | Used when `fetch_data` must fetch WRDS |
| `PHASE*_WRITE_EXTRA` | Per-phase audit dumps (default core-only) |
| `TAPE_MODE` | `synthetic` \| `plugin` (Phase 1 / 3 market tape) |
| `GENERATOR_*` | Phase 1 random-order knobs |
| `INITIAL_CASH` / `CASH_BUFFER_BPS` / `FEE_BPS` | Phase 2 fund sizing |
| `FLOW_*` / `FLOW_BAND_BPS` | Phase 2 synthetic investor-flow knobs (always applied) |
| `DIVIDEND_SENSITIVITY` | When True, also write `dividend_sensitivity.csv` |
| `TCA_*` | Phase 3 date filter, strategies, impact knobs, `TCA_ALL_REBALANCES` |

Phase 2 rebalance fills are always **reference-close** (decide on the session before `weights.reference_date`, fill at that reference close).

### `fetch_data.py` — validate or overwrite

`fetch_data.py` checks the four CSVs against the experiment window (`coverage.json` is ignored as a gate). Hard failure (missing files/columns, missing `START_DATE` weight snapshot, or incomplete START–END calendar/price span) triggers a WRDS overwrite of the four CSVs plus a fresh `coverage.json`. Missing sessions *before* the first `reference_date` only warn — Phase 2 may pad them.

After the four-file step, `fetch_data` **ensures** `benchmark_sp500_tr.csv` covers the experiment calendar **and** reference-close decision/reference sessions implied by `weights.reference_date` (even when those days are absent from `calendar.csv`). Invalid/missing benchmark triggers a WRDS CRSP `dsp500` / `dsp500_v2` fetch (`vwretd`).

Force refresh: delete or corrupt `data/` then re-run `fetch_data.py`.

```bash
PYTHONPATH=src python fetch_data.py
```

### Phase 2 — fund daybook (local only)

```bash
PYTHONPATH=src python main_2_fund.py
```

Phase 2 requires a usable `data/benchmark_sp500_tr.csv`. Missing coverage fails with a clear `fetch_data` / WRDS entitlement message (no silent skip).

Reference-close needs `weights.reference_date` and calendar/prices covering the session **before** the first reference date (typically Thursday decision / Friday fill). A WRDS fetch pads ~14 calendar days before `START_DATE` for that purpose.

---

## `data/weights.csv`

Point-in-time index membership and **capitalization-weight proxies**. fetch_data write path keeps only:

| Field | Meaning |
| ----- | ------- |
| `date` | Post-rebalance snapshot session (often Monday after the reference Friday) |
| `reference_date` | Date used for market-cap / membership observation |
| `permno` | Security ID |
| `cap_weight_proxy` | Weight in [0, 1]; should sum to 1 per `date` |

Readers still tolerate extra columns on older fat caches until the next fetch_data overwrite.

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

Corporate actions that enter the daybook. Normative source: WRDS/CRSP `stkdistributions` via `fetch_wrds`, filtered to `CASH_DIVIDEND` and `STOCK_SPLIT` only (no dual-source reconcile on the main path). fetch_data write path keeps only:

| Field | Meaning |
| ----- | ------- |
| `source_event_id` | Unique ID within the source |
| `permno` | Security ID |
| `event_type` | `CASH_DIVIDEND` or `STOCK_SPLIT` |
| `ex_date`, `pay_date` | Event calendar |
| `split_ratio`, `cash_per_share` | Cash dividend or split factor |
| `currency` | Currency (USD for the simple ledger) |

Upstream normalize may still compute `requires_review` in memory; `fetch_data` refuses to write while any such flag is true (resolve or drop before `write_flat_inputs`).

---

## `data/coverage.json`

Optional **WRDS-fetch provenance** only. Written when `fetch_data` overwrites via WRDS; **not** read by `validate_local` or Phase 2. Stale or missing coverage does not trigger a refetch. Changing `START_DATE` does not require editing this file.

| Field | Meaning |
| ----- | ------- |
| `start`, `end` | Window used for that fetch |
| `schema` | Research-DB stock schema (e.g. `crsp_q_stock`) |
| `universe` | List of `permno` in that fetch |
| `extra_sha256` | Digests for `daily_prices.csv` / `calendar.csv` at fetch time |

---

## `data/benchmark_sp500_tr.csv`

CRSP S&P 500 **total return** (`vwretd`) for fund tracking. `fetch_data` writes/refreshes it; Phase 2 always consumes it. Companion `benchmark_sp500_tr.json` stores row count, sha256, and source table id.

| Field | Meaning |
| ----- | ------- |
| `date` | Session |
| `benchmark_return` | Daily total return (decimal) |
| `source` | Discovered table id (e.g. `crsp_q_indexes.dsp500_v2.vwretd`) |
| optional | `price_return` / `sp_price_return` / `sp_index_level` when present in the release |

Gaps are never filled. Synthetic offline stand-ins are for mechanics only and must not be quoted as research results.

---

## `output/2_fund/`

Phase 2 (`main_2_fund.py`) **always** writes core handoff: `daily_nav.csv`, `fills.csv`, `pm_orders.csv`. Set `DIVIDEND_SENSITIVITY=True` to also write `dividend_sensitivity.csv` (accrual vs `cash_at_ex` vs `no_cash_div`, with ΔNAV / ΔTD / ΔTE vs accrual). Set `PHASE2_WRITE_EXTRA=True` in `config.py` for holdings, blotter `orders`, audit / benchmark panels, notional summary, and `run_manifest.json`. The terminal always prints scalar summaries (including tracking difference / error) plus previews of written tables.

### Hard-name exclusions (fixed)

Always applied before simulation (not a config switch):

- Drop any `permno` with a reverse stock split in the window (`STOCK_SPLIT` with `split_ratio < 1`).
- Drop snapshot rows whose holding window has missing/nonpositive closes, or whose decision/execution days lack a `TR` trade close.
- Renormalize remaining weights to sum to 1.

This is a **simulator simplification** (see README). Excluded rows are written to `excluded_security_snapshots.csv`; original-weight mass by snapshot is recorded in `run_manifest.json`.

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

### `pm_orders.csv`

Phase 0 PM handoff from the blotter (`src/order_schema.py`). Shares are **unsigned**; `side` carries direction. Full four-level contract: [`docs/order_schema.md`](order_schema.md).

| Field | Meaning |
| ----- | ------- |
| `order_id` | Same as blotter `order_id` when present |
| `fund_id` | Default `SIMP_SP500_10B` |
| `decision_date`, `decision_ts` | Decision session + 16:00 NY wall clock |
| `trade_date` | Expected / execution session |
| `permno`, `side`, `shares`, `decision_price` | Instrument, direction, whole shares, decision close |
| `reason` | `INITIAL` / `REBALANCE` / `INFLOW` / `OUTFLOW` |
| `benchmark` | `CLOSE` for INITIAL/REBALANCE; `ARRIVAL` for flows |
| `urgency` | `HIGH` for OUTFLOW; else `MEDIUM` |
| `notional_usd` | `shares × decision_price` |

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

### `dividend_sensitivity.csv` (when `DIVIDEND_SENSITIVITY=True`)

| Field | Meaning |
| ----- | ------- |
| `mode` | `accrual` (baseline) / `cash_at_ex` / `no_cash_div` |
| `ending_nav`, `ending_cash`, `ending_receivables` | End-of-window levels |
| `tracking_difference_annualized_bps`, `tracking_error_annualized_bps` | vs S&P 500 TR |
| `delta_ending_nav`, `delta_td_bps`, `delta_te_bps` | vs accrual baseline |

Core handoff tables remain the accrual baseline only.

### Extra audit tables (`PHASE2_WRITE_EXTRA=True`)

| File | Meaning |
| ---- | ------- |
| `daily_holdings.csv` | Per-`permno` shares and valuation |
| `orders.csv` | Simulator blotter (signed qty); prefer `pm_orders.csv` for handoff |
| `rebalance_schedule.csv` | Snapshot → decision → reference execution dates |
| `synthetic_flows.csv` | Seeded flow path (`date`, `flow_bps`, `is_lumpy`) |
| `action_journal.csv` | Corporate-action / accounting journal from the simulator |
| `excluded_security_snapshots.csv` | Dropped `(date, permno)` rows with `exclusion_reason` (+ weight when available) |
| `padded_quotes.csv` | Offline carry-back quotes (`date`, `permno`, `source_date`). **Look-ahead fallback** — not live auction evidence |
| `benchmark_daily.csv` | Per-session fund vs S&P 500 TR active returns / indices |
| `benchmark_by_year.csv` | Calendar-year fund vs benchmark returns and tracking difference |
| `order_notional_summary.csv` | Notional by `reason` (+ `ALL` row) |
| `run_manifest.json` | Config, input sha256s, exclusions, TD/TE scalars |

---

## Data flow

```text
data/{weights, daily_prices, calendar, events, benchmark_sp500_tr}
        → load / verify (coverage ignored as a gate)
        → hard-name exclusions + renormalize
        → Phase 2: fund_simulator.simulate_fund
          (reference-close schedule + synthetic flows)
        → output/2_fund/ daily_nav + fills + pm_orders
          (+ dividend_sensitivity if DIVIDEND_SENSITIVITY; + extras if PHASE2_WRITE_EXTRA)
        → Phase 3: MarketTape stock_days/venue_days + desk strategies + impact fills
          (TAPE_MODE; one date or TCA_ALL_REBALANCES)
        → output/3_tca/ order_tca + strategy_summary + liquidity_buckets
          (+ extras if PHASE3_WRITE_EXTRA)
```

### `output/0_schema/` (Phase 0 core)

Example chain proving the personal contract (same column sets as Phase 1/2 PM handoff
and Phase 3 desk tables). See [`docs/order_schema.md`](order_schema.md).

| File | Meaning |
| ---- | ------- |
| `example_pm_orders.csv` | Two illustrative PM orders |
| `example_trader_orders.csv` | Desk legs (incl. spill) |
| `example_broker_orders.csv` | Child orders @ MIC |
| `example_fills.csv` | Fills with fees / impact_bps |

Extras (`PHASE0_WRITE_EXTRA`): field dictionary, conservation report, schema version, validation JSON.

### `output/1_generator/` (Phase 1 core)

| File | Meaning |
| ---- | ------- |
| `pm_orders.csv` | Random PM orders near tape VWAP (Phase 0 columns) |
| `trader_orders.csv` | Desk legs for `GENERATOR_STRATEGY` |
| `broker_orders.csv` | Multi-MIC children (volumes from `venue_days` when present) |
| `fills.csv` | Impact fills (uncalibrated mechanics) |

Extras (`PHASE1_WRITE_EXTRA=True`): `stock_days.csv`, `venue_days.csv`, `generator_meta.json`.
Tape modes: [`docs/tape_plugin.md`](tape_plugin.md).

### Tape tables (Phase 1/3 extras): `stock_days` / `venue_days`

Produced by `MarketTape` (`src/market_tape.py`). Same columns for synthetic and plugin.

**`stock_days.csv`**

| Field | Meaning |
| ----- | ------- |
| `date`, `permno` | Stock-day key |
| `primary_mic` | Listing MIC for MOC |
| `prev_close`, `arrival_price`, `close_price` | Anchors (arrival ≈ open / open-auction proxy) |
| `day_vwap` | Consolidated day VWAP (synthetic: bridge path; plugin: feed) |
| `close_auction_volume`, `day_volume`, `adv_shares` | Size / liquidity |
| `spread_bps`, `sigma_daily` | Spread and daily vol stand-ins or feed fields |
| `is_index_event` | True on rebalance / event dates (higher auction share in synthetic) |

**`venue_days.csv`**

| Field | Meaning |
| ----- | ------- |
| `date`, `permno`, `mic` | Venue-day key |
| `is_primary` | Primary listing flag |
| `volume`, `vwap` | Continuous-session volume and VWAP on that MIC |

Desk continuous legs allocate child shares by `venue_days.volume` for the same
`(date, permno)`; if no rows match, static `VENUE_WEIGHTS` are used.

### `output/3_tca/` (Phase 3 core)

Built from Phase 2 `pm_orders` + `MarketTape`. Cost sign: **positive bps = cost to the
fund** (buy above / sell below the reference). Unfilled shares are charged at the
trade-date close (opportunity cost). Impact is uncalibrated.

| File | Meaning |
| ---- | ------- |
| `order_tca.csv` | One row per PM order × desk strategy |
| `strategy_summary.csv` | Notional-weighted means by strategy |
| `liquidity_buckets.csv` | Same costs by strategy × `pct_adv` bucket |

Extras (`PHASE3_WRITE_EXTRA`): `stock_days`, `venue_days`, per-strategy
`trader_*` / `broker_*` / `fills_*`, `impact_model.json`.

**`order_tca.csv` (key columns)**

| Field | Meaning |
| ----- | ------- |
| PM columns | From Phase 2 / schema (`order_id`, `side`, `shares`, `decision_price`, …) |
| `strategy` | Desk label: `CLOSE_AUCTION` / `VWAP` / `ARRIVAL_IS` |
| `filled`, `avg_price`, `unfilled` | Execution aggregate for that strategy |
| `spill_shares`, `auction_shares` | Spill leg and auction liquidity shares |
| `vs_close_bps` | Cost vs trade-date close (primary index benchmark) |
| `is_vs_decision_bps` | Cost vs decision price |
| `vs_arrival_bps`, `vs_vwap_bps` | Cost vs arrival / day VWAP |
| `fees_bps`, `cost_vs_close_usd` | Fees and USD cost vs close |
| `pct_adv` | Order shares / `adv_shares` |
| `tape_source` | `synthetic_offline` or `plugin` |

**`strategy_summary.csv`**

| Field | Meaning |
| ----- | ------- |
| `strategy`, `orders`, `n_trade_dates`, `notional_usd` | Coverage |
| cost columns | Notional-weighted means of the `*_bps` fields above |
| `fund_td_bps`, `fund_te_bps` | Phase 2 fund vs S&P TR (window scalars; **not** cost-adjusted) |

**`liquidity_buckets.csv`**

| Field | Meaning |
| ----- | ------- |
| `strategy`, `pct_adv_bucket` | `lt_1pct` / `1_5pct` / `5_10pct` / `ge_10pct` |
| costs / notional | Same notional-weighted metrics within the bucket |

`fetch_data` WRDS overwrite refreshes the four core flat files and writes
`coverage.json` as a fetch snapshot; it also ensures the S&P 500 TR benchmark CSV.
Requires **Python 3.12** and a project install including `wrds`. Events come only
from CRSP `stkdistributions`.

---

## Library helpers (not the main path)

`ledger.simulate` / `pipeline.run_ledger` remain available as library helpers
(including next-session control). Older dual-source review helpers under `src/`
are **not** wired into `fetch_data` / `main_2_fund`.
