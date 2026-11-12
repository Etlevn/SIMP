# Simulated Investment Manager Platform (SIMP)

SIMP is a research prototype that models a **passive large-cap U.S. equity index fund** (illustrative **~$10B AUM**, S&P 500–style). It turns a historical membership and price tape into a reproducible daily fund daybook: target holdings, rebalance and investor-flow orders, close-based fills, and NAV.

Product phases (see [`docs/blueprint.md`](docs/blueprint.md)):

**Phase 0 schema → Phase 1 generator → Phase 2 index fund → Phase 3 exec/TCA**

This repository implements **Phase 0** (order schema), **Phase 2** (fund daybook), and **Phase 3a** (one-date synthetic MOC/VWAP/arrival TCA). Phase 1 is a stub. Impact coefficients are uncalibrated — mechanics only until a vendor tape is fitted.

It is a _work in progress_, not a production backtester or live strategy. Licensed market data is **not** shipped in the repository.

## How the simulation thinks

A passive index fund is largely **rule-driven**. SIMP mirrors that logic at daily close resolution:

1. **Mandate → targets.** Point-in-time index membership and **capitalization-weight proxies** (not official float-adjusted index weights) define each snapshot’s target portfolio. Whole-share targets follow `shares ≈ weight × AUM / price`, with a configurable cash buffer set aside when sizing trades.

2. **Two order sources.** Orders come from (a) **index rebalances** around quarterly reference dates and (b) **seeded synthetic investor flows** translated into pro-rata basket trades. Flows are always applied in Phase 2; knobs live in `config.py`.

3. **Reference-close timing.** On the session *before* an index `reference_date`, the simulator decides orders at that day’s close; it fills at the **reference close** (typically Thursday decision → Friday fill). Close is an illustrative execution price for an index fund, not a microstructure fill model. If cash cannot fund all buys plus fees, buy quantities are scaled pro-rata.

4. **Daily book vs quarterly targets.** Target weights can be unchanged for weeks, but dividends/splits, subscriptions/redemptions, cash, and fees still move holdings and NAV every session—so the daybook is daily even when the index snapshot is not.

5. **Hard-name exclusions (fixed simulator simplification).** Before simulation, SIMP always drops names with a **reverse stock split** in the window (`STOCK_SPLIT` with `split_ratio < 1`) and snapshot rows whose holding window has missing/nonpositive closes or whose decision/execution days lack a `TR` trade close. Remaining weights are renormalized to 1. Real index funds usually adjust shares or use cash-in-lieu instead of dropping names; this path keeps accounting simple.

6. **What you get.** Default `output/` is core only; each phase has one `PHASE*_WRITE_EXTRA` switch in `config.py` for audit dumps.

| Phase 2 core | Meaning |
| ----- | ------- |
| `daily_nav.csv` | End-of-day cash, positions mark, and NAV |
| `pm_orders.csv` | Phase 0 PM handoff (`order_id`/`fund_id`/`shares`, benchmark, urgency, notional) |
| `fills.csv` | Executed quantity/price and drift vs decision |

Extras (`PHASE2_WRITE_EXTRA=True`): holdings, blotter, schedule/flows/journal/exclusions/padded, benchmark panels, notional, `run_manifest.json`. Terminal always prints TD/TE scalars and previews of written tables.

## Data and pipeline

### Inputs (`data/`)

Four required CSVs (primary join key: `permno`):

| File | Role |
| ---- | ---- |
| `weights.csv` | Snapshot membership and cap-weight proxies (`date`, `reference_date`, …) |
| `daily_prices.csv` | Raw closes and `price_flag` for valuation / fills |
| `calendar.csv` | Trading sessions in the experiment window |
| `events.csv` | Ordinary USD cash dividends and stock splits |

`coverage.json` is an optional WRDS-fetch snapshot. `fetch_data.py` **does not** use it as a validity gate. Column-level schemas: [`docs/dataset.md`](docs/dataset.md). Vision / phase model: [`docs/blueprint.md`](docs/blueprint.md).

### `fetch_data.py` — prepare inputs (not a phase)

Validates the four CSVs against the experiment window in `config.py`. If the cache passes, it is reused; if not, it pulls a minimal WRDS extract, **overwrites** the four CSVs, and writes a fresh `coverage.json`. It then ensures `data/benchmark_sp500_tr.csv` (CRSP S&P 500 total return) for the same window, including reference-close decision sessions. Force a refresh by deleting or corrupting `data/` and re-running. WRDS credentials are read from a gitignored `.env` only when a fetch is required.

### Phase 0 — `main_0_schema.py`

Offline: write synthetic PM→Trader→Broker→Fill example tables under `output/0_schema/`. Optional extras via `PHASE0_WRITE_EXTRA`. See [`docs/order_schema.md`](docs/order_schema.md).

### Phase 2 — `main_2_fund.py`

Local-only: validate inputs → hard exclusions → reference-close simulation with synthetic flows → write core handoff under `output/2_fund/` (`daily_nav`, `fills`, `pm_orders`). Optional audit extras via `PHASE2_WRITE_EXTRA`. The CLI prints TD/TE vs the S&P 500 TR series from `fetch_data.py`.

### Phase 3 — `main_3_tca.py`

Reads Phase 2 `pm_orders`, picks one rebalance day (`TCA_TRADE_DATE` or latest REBALANCE), builds a synthetic tape from CRSP closes, compares CLOSE_AUCTION / VWAP / ARRIVAL_IS with uncalibrated impact, writes `order_tca.csv` + `strategy_summary.csv` under `output/3_tca/`. Extras via `PHASE3_WRITE_EXTRA`.

### Layout

```text
SIMP/
├── README.md
├── docs/
│   ├── blueprint.md          # Phase 0–3 vision
│   ├── order_schema.md       # Phase 0 contract
│   └── dataset.md            # data/ and output schemas
├── pyproject.toml
├── config.py                 # Experiment window, fund & flow knobs
├── .env.template
├── fetch_data.py             # Independent: refresh data/ only
├── main_0_schema.py          # Phase 0 → output/0_schema/
├── main_1_generator.py       # Phase 1 stub → output/1_generator/
├── main_2_fund.py            # Phase 2 → output/2_fund/
├── main_3_tca.py             # Phase 3 → output/3_tca/
├── src/                      # Reusable modules (PYTHONPATH=src)
├── data/                     # Flat inputs (gitignored contents)
└── output/                   # Phase deliverables (gitignored entirely)
    ├── 0_schema/
    ├── 1_generator/
    ├── 2_fund/
    └── 3_tca/
```

Edit **`config.py`** for dates, AUM, cash buffer, fees, and `FLOW_*` parameters. Keep experiment knobs out of `src/`. There is no root `main.py` orchestrator—run each phase entrypoint separately.

## Quick start

Use **Python 3.12** (the official `wrds` package supports through 3.12).

```bash
python3.12 -m venv .venv
source .venv/bin/activate             # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

1. Copy `.env.template` → `.env` and set `WRDS_USERNAME` / `WRDS_PASSWORD` (needed when `fetch_data` must contact WRDS).
2. Align `START_DATE` / `END_DATE` (and related knobs) in [`config.py`](config.py) with the data you expect.
3. Run Phase 0 (no market data), then prepare inputs and Phase 2:

```bash
PYTHONPATH=src python main_0_schema.py
PYTHONPATH=src python fetch_data.py
PYTHONPATH=src python main_2_fund.py
PYTHONPATH=src python main_3_tca.py
```

If a valid local `data/` cache already covers the window, `fetch_data.py` does not contact WRDS. `main_2_fund.py` never fetches.

## Research conventions and limitations

1. **Point-in-time / reference-close.** Weights observed at a close are not tradeable earlier. Phase 2 always decides on the session before `reference_date` and fills at that reference close.
2. **Index tracking.** Cap-weight proxies are research stand-ins, not official float-adjusted index weights.
3. **Execution.** Phase 2 daybook fills are reference-close proxies. Phase 3 compares MOC / VWAP / arrival+impact on a synthetic tape (uncalibrated).
4. **Scope.** Main path: Phase 0 + `fetch_data` + Phase 2 + Phase 3a. Phase 1 and Phase 3b (all-rebalance / calibration) remain open.
5. **Data rights.** Access and redistribution follow your institutional licenses. Keep licensed extracts and credentials out of Git.

## Intended use

Personal / academic research prototype for methodology development: a clean, extensible path from index mandate to synthetic fund order flow. No market data is redistributed with the code.

## License / data

Code: see the repository license if present. Market data: obtain via your WRDS / vendor entitlements; do not commit extracts or `.env`.
