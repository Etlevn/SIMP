# Simulated Investment Manager Platform (SIMP)

SIMP is a research prototype that models a **passive large-cap U.S. equity index fund** (illustrative **~$10B AUM**, S&P 500–style). It turns a historical membership and price tape into a reproducible daily fund daybook: target holdings, rebalance and investor-flow orders, close-based fills, and NAV.

Long-term arc:

**fund / portfolio → synthetic orders → execution → TCA → reporting**

This repository implements the first half of that arc—a modular Python pipeline that loads a minimal flat `data/` bundle, then replays the daybook with **index rebalance timing** and **synthetic subscriptions/redemptions**. Richer execution (auction / VWAP / TWAP) and transaction-cost analysis (TCA) are intended next steps, not part of the current main path.

It is a _work in progress_, not a production backtester or live strategy. Licensed market data is **not** shipped in the repository.

## How the simulation thinks

A passive index fund is largely **rule-driven**. SIMP mirrors that logic at daily close resolution:

1. **Mandate → targets.** Point-in-time index membership and **capitalization-weight proxies** (not official float-adjusted index weights) define each snapshot’s target portfolio. Whole-share targets follow `shares ≈ weight × AUM / price`, with a configurable cash buffer set aside when sizing trades.

2. **Two order sources.** Orders come from (a) **index rebalances** around quarterly reference dates and (b) **seeded synthetic investor flows** translated into pro-rata basket trades. Flows are always applied in Stage 2; knobs live in `config.py`.

3. **Reference-close timing.** On the session *before* an index `reference_date`, the simulator decides orders at that day’s close; it fills at the **reference close** (typically Thursday decision → Friday fill). Close is an illustrative execution price for an index fund, not a microstructure fill model. If cash cannot fund all buys plus fees, buy quantities are scaled pro-rata.

4. **Daily book vs quarterly targets.** Target weights can be unchanged for weeks, but dividends/splits, subscriptions/redemptions, cash, and fees still move holdings and NAV every session—so the daybook is daily even when the index snapshot is not.

5. **Hard-name exclusions (fixed simulator simplification).** Before simulation, SIMP always drops names with a **reverse stock split** in the window (`STOCK_SPLIT` with `split_ratio < 1`) and snapshot rows whose holding window has missing/nonpositive closes or whose decision/execution days lack a `TR` trade close. Remaining weights are renormalized to 1. Real index funds usually adjust shares or use cash-in-lieu instead of dropping names; this path keeps accounting simple.

6. **What you get.** Stage 2 writes four tables under `output/2_fund/`:

| Table | Meaning |
| ----- | ------- |
| `daily_nav.csv` | End-of-day cash, positions mark, and NAV |
| `daily_holdings.csv` | Per-`permno` shares and valuation |
| `orders.csv` | Intended trades (rebalance and/or flow), with decision prices |
| `fills.csv` | Executed quantity/price and drift vs decision |

Schedule, flow path, and action journal stay in memory; the terminal prints scalar summaries and table previews.

## Data and pipeline

### Inputs (`data/`)

Four required CSVs (primary join key: `permno`):

| File | Role |
| ---- | ---- |
| `weights.csv` | Snapshot membership and cap-weight proxies (`date`, `reference_date`, …) |
| `daily_prices.csv` | Raw closes and `price_flag` for valuation / fills |
| `calendar.csv` | Trading sessions in the experiment window |
| `events.csv` | Ordinary USD cash dividends and stock splits |

`coverage.json` is an optional WRDS-fetch snapshot. Stage 1 **does not** use it as a validity gate. Column-level schemas: [`docs/dataset.md`](docs/dataset.md).

### Stage 1 — prepare data

`scripts/run_1_data.py` validates the four CSVs against the experiment window in `config.py`. If the cache passes, it is reused; if not, Stage 1 pulls a minimal WRDS extract, **overwrites** the four CSVs, and writes a fresh `coverage.json`. Force a refresh by deleting or corrupting `data/` and re-running Stage 1. WRDS credentials are read from a gitignored `.env` only when a fetch is required.

### Stage 2 — fund daybook

`scripts/run_2_fund.py` is local-only: validate inputs → hard exclusions → reference-close simulation with synthetic flows → write the four output CSVs under `output/2_fund/`.

### Layout

```text
SIMP/
├── README.md
├── docs/dataset.md         # Flat data/ and output schemas
├── pyproject.toml
├── config.py               # Experiment window, fund & flow knobs
├── .env.template           # WRDS credential placeholders
├── src/                    # Reusable modules (PYTHONPATH=src)
├── scripts/
│   ├── run_1_data.py       # Stage 1: validate / fetch
│   └── run_2_fund.py       # Stage 2: daybook
├── data/                   # Flat inputs (contents gitignored)
└── output/2_fund/          # Stage 2 outputs (gitignored; placeholder kept)
```

Edit **`config.py`** for dates, AUM, cash buffer, fees, and `FLOW_*` parameters. Keep experiment knobs out of `src/`.

## Quick start

Use **Python 3.12** (the official `wrds` package supports through 3.12).

```bash
python3.12 -m venv .venv
source .venv/bin/activate             # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

1. Copy `.env.template` → `.env` and set `WRDS_USERNAME` / `WRDS_PASSWORD` (needed when Stage 1 must fetch).
2. Align `START_DATE` / `END_DATE` (and related knobs) in [`config.py`](config.py) with the data you expect.
3. Prepare inputs, then run the fund:

```bash
PYTHONPATH=src python scripts/run_1_data.py
PYTHONPATH=src python scripts/run_2_fund.py
```

If a valid local `data/` cache already covers the window, Stage 1 does not contact WRDS. Stage 2 never fetches.

## Research conventions and limitations

1. **Point-in-time / reference-close.** Weights observed at a close are not tradeable earlier. Stage 2 always decides on the session before `reference_date` and fills at that reference close.
2. **Index tracking.** Cap-weight proxies are research stand-ins, not official float-adjusted index weights.
3. **Execution.** Daily close is an illustrative fill proxy. Auction / VWAP / TWAP execution and full TCA remain future work on top of the order/fill tables this pipeline already emits.
4. **Scope.** The main path stops at the daily fund daybook driven by flat inputs and synthetic flows.
5. **Data rights.** Access and redistribution follow your institutional licenses. Keep licensed extracts and credentials out of Git.

## Intended use

Personal / academic research prototype for methodology development: a clean, extensible path from index mandate to synthetic fund order flow. No market data is redistributed with the code.

## License / data

Code: see the repository license if present. Market data: obtain via your WRDS / vendor entitlements; do not commit extracts or `.env`.
