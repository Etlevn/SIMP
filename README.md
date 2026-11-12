# Simulated Investment Manager Platform (SIMP)

SIMP is a research prototype of a **simulated asset manager**: from index membership
and prices → a passive **~$10B S&P 500–style** fund daybook → a traceable
**PM → Trader → Broker → Fill** order chain → **execution / TCA** mechanics.

It is a _work in progress_, not a production backtester or live strategy.
Licensed market extracts are **not** shipped in Git.

## Product layout (Phase 0–3)

| Role | Entrypoint | Writes |
| --- | --- | --- |
| Data prepare *(not a phase)* | `fetch_data.py` | `data/` |
| **0 Schema** | `main_0_schema.py` | `output/0_schema/` |
| **1 Generator** | `main_1_generator.py` | `output/1_generator/` |
| **2 Index fund** | `main_2_fund.py` | `output/2_fund/` |
| **3 Exec / TCA** | `main_3_tca.py` | `output/3_tca/` |

There is **no** root `main.py` orchestrator. Logic lives in `src/`; knobs in
[`config.py`](config.py).

Day-level inputs come from WRDS/CRSP under `data/`. Phase 1/3 market tape defaults
to **enhanced synthetic** (`TAPE_MODE=synthetic`). A live multi-MIC / closing-auction
client is optional via a **gitignored** plugin (`TAPE_MODE=plugin`). See
[`docs/data_reality.md`](docs/data_reality.md).

## What each phase delivers (core disk)

Default disk is **core only**. One switch per phase dumps audit extras:
`PHASE0_WRITE_EXTRA` … `PHASE3_WRITE_EXTRA` (all default `False` in product intent).

| Phase | Core files | Role |
| --- | --- | --- |
| **0** | `example_{pm,trader,broker,fills}.csv` | Worked PM→…→Fill example + validators |
| **1** | `pm/trader/broker/fills.csv` | Random multi-MIC chain (no Phase 2 needed) |
| **2** | `daily_nav.csv`, `fills.csv`, `pm_orders.csv` | Fund handoff; optional `dividend_sensitivity.csv` |
| **3** | `order_tca.csv`, `strategy_summary.csv`, `liquidity_buckets.csv` | Notional-weighted MOC / VWAP / arrival+impact |

Field-level schemas: [`docs/dataset.md`](docs/dataset.md) · order contract: [`docs/order_schema.md`](docs/order_schema.md).

## How the simulation thinks

1. **Mandate → targets.** Cap-weight **proxies** (not official float-adjusted index weights); whole shares with a cash buffer.
2. **Two order sources.** Quarterly **reference-close** rebalances + seeded **synthetic** subscriptions/redemptions (`FLOW_*`).
3. **Timing.** Decide on the session before `weights.reference_date`; fill at that reference close (CRSP close = auction-price proxy in Phase 2).
4. **Daily book.** Dividends/splits, flows, cash, and fees move NAV every session even when weights are quarterly.
5. **Hard exclusions.** Reverse splits / non-tradeable closes dropped and weights renormalized (simulator simplification).
6. **TCA vs daybook.** Phase 2 overnight close-to-close drift is **not** execution cost. Phase 3 costs are **mechanics** on a labeled tape (uncalibrated impact).

## Run order

Use **Python 3.12**.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.template .env   # set WRDS_* when fetch_data must hit WRDS
```

Edit [`config.py`](config.py) (`START_DATE` / `END_DATE`, fund/flow/TCA/tape knobs), then:

```bash
PYTHONPATH=src python main_0_schema.py      # offline schema examples
PYTHONPATH=src python main_1_generator.py   # offline synthetic orders (TAPE_MODE)
PYTHONPATH=src python fetch_data.py         # validate or WRDS-overwrite data/
PYTHONPATH=src python main_2_fund.py        # fund daybook → output/2_fund/
PYTHONPATH=src python main_3_tca.py         # TCA on Phase 2 pm_orders
```

Useful knobs (see comments in `config.py`):

| Knob | Meaning |
| --- | --- |
| `TAPE_MODE` | `synthetic` (default) or `plugin` (+ `SIMP_TAPE_PLUGIN_DIR`) |
| `DIVIDEND_SENSITIVITY` | Extra Phase 2 contrast table vs accrual |
| `TCA_ALL_REBALANCES` | Phase 3 over all matching rebalance days |
| `TCA_TRADE_DATE` | Single-day Phase 3 (ignored if all-rebalances) |
| `PHASE*_WRITE_EXTRA` | Audit / desk panels / `stock_days`+`venue_days` |

`fetch_data.py` reuses a valid local `data/` cache; `main_2_fund.py` never fetches.

## Layout

```text
SIMP/
├── README.md
├── docs/
│   ├── blueprint.md       # Phase vision / sequence
│   ├── data_reality.md    # Real vs stand-in data; quoting rules
│   ├── dataset.md         # data/ + output/ table & field schemas
│   ├── order_schema.md    # Phase 0 four-level contract
│   └── tape_plugin.md     # Private MarketTape plugin contract
├── config.py
├── fetch_data.py
├── main_0_schema.py … main_3_tca.py
├── src/                   # PYTHONPATH=src
├── data/                  # gitignored contents
└── output/{0_schema,1_generator,2_fund,3_tca}/   # gitignored
```

## Documentation map

| Doc | Use when |
| --- | --- |
| [`docs/blueprint.md`](docs/blueprint.md) | Why Phase 0–3; what is done vs next |
| [`docs/dataset.md`](docs/dataset.md) | Every core CSV / key columns |
| [`docs/order_schema.md`](docs/order_schema.md) | PM/Trader/Broker/Fill fields & invariants |
| [`docs/data_reality.md`](docs/data_reality.md) | What is real CRSP vs synthetic tape / flows |
| [`docs/tape_plugin.md`](docs/tape_plugin.md) | How to plug a private venue tape later |

## Research conventions

1. Cap-weight proxies ≠ official index weights.
2. Phase 2 fills = reference-close proxies; Phase 3 = labeled tape + uncalibrated impact — do not quote as fitted venue costs.
3. Keep licensed extracts, `.env`, `.cursor/`, `notes/`, `_*/` reference trees, and private `vendor_tape/` out of public remotes.
4. Next: implement a private tape plugin and calibrate impact — not a rewrite of the Phase chain.

## License / data

Code: see the repository license if present. Market data: obtain via your institutional entitlements; do not commit extracts or credentials.
