# Data reality: what is real vs what is stand-in

**Status:** living note (default path has no live venue-tape client in-tree)  
**Updated:** 2026-11-12

This page records how SIMP uses **licensed day-level research data**, where it
uses **labeled stand-ins**, and how a private **plugin tape** can replace the
intraday / multi-MIC layer later. It is meant to stop mixing up:

- overnight close-to-close drift with execution cost (TCA);
- synthetic microstructure with a live closing-auction feed;
- research cap-weight proxies with official index weights.

Plugin wiring details: [`docs/tape_plugin.md`](tape_plugin.md).  
CSV / field schemas (all phases): [`docs/dataset.md`](dataset.md).  
Phase map: [`docs/blueprint.md`](blueprint.md).  
Operator entry: [`README.md`](../README.md).

---

## 1. One-line boundary

SIMP runs on **WRDS/CRSP flat files under `data/`** plus an enhanced
**synthetic market tape** (`TAPE_MODE=synthetic`). A real multi-MIC /
closing-auction feed is **not** required to run Phase 0–3. That feed is
optional via a **gitignored** plugin (`TAPE_MODE=plugin` +
`SIMP_TAPE_PLUGIN_DIR`).

---

## 2. What is real (local `data/`, not in Git)

Prepared by `fetch_data.py` (credentials stay in gitignored `.env`):

| Input | Role |
| --- | --- |
| `weights.csv` | Point-in-time membership + **cap-weight proxies** |
| `daily_prices.csv` | Raw closes / `price_flag` for marks and close-proxy fills |
| `calendar.csv` | Trading sessions |
| `events.csv` | Ordinary USD cash dividends and stock splits |
| `benchmark_sp500_tr.csv` | CRSP S&P 500 **total return** for TD / TE |

These are research extracts. They are **not** redistributed with the repo
(`data/` contents are gitignored).

---

## 3. What substitutes for a live venue / auction tape

Supervisor-style TCA asks for auction price/volume/imbalance, ADV, spread, and
intraday VWAP. Public SIMP does **not** ship a venue client. Instead:

| Layer | Stand-in | Where |
| --- | --- | --- |
| Consolidated stock-day tape | Enhanced synthetic: real close anchors, Brownian-bridge VWAP path, event-day higher auction share, ADV/spread/σ draws | `src/market_tape.py` → `SyntheticMarketTape` |
| Per-MIC continuous volume | Synthetic `venue_days` (dirichlet venue shares) | same |
| Desk MIC routing | Prefer same-day `venue_days.volume`; else static `VENUE_WEIGHTS` | `src/tca_desk.py` |
| Impact / fees | Literature defaults (`TCA_IMPACT_*`); **uncalibrated** | `src/tca_execution.py`, `config.py` |
| Tape provenance | `tape_source=synthetic_offline` or `plugin` | Phase 1/3 outputs |

**Dual mode**

| `TAPE_MODE` | Behavior |
| --- | --- |
| `synthetic` (default) | Offline path above |
| `plugin` | Load `$SIMP_TAPE_PLUGIN_DIR/tape_plugin.py` → `build_tape(...)`; **hard fail** if missing (no silent fallback) |

The plugin directory (`/vendor_tape/` by convention) is gitignored so private
clients and licensed extracts never land on the public remote.

---

## 4. Other stand-ins (not “venue tape”, still not fully “live reality”)

Even with a plugin tape, these research approximations remain:

| Topic | What SIMP does | Honesty rule |
| --- | --- | --- |
| Phase 2 fills | Reference-close / CRSP close as **closing-auction proxy** | Not a microstructure fill; not TCA |
| “Slippage” in the daybook | Mostly **overnight** decision→execution close move | Do **not** call it execution cost |
| Investor flows | Seeded synthetic subscriptions/redemptions (`FLOW_*`) | Scenario, not observed AUM flows |
| Index weights | Cap-weight proxies | Not official float-adjusted weights |
| Hard exclusions | Reverse splits / bad prices dropped + renormalize | Simulator simplification; disclose mass |
| Padded quotes | Carry-back closes for missing sessions | Look-ahead fallback; disclose |
| Dividends | Default accrual (ex receivable → pay cash); optional sensitivity | `DIVIDEND_SENSITIVITY` contrasts rules, not a vendor feed |
| Phase 0 examples | Hand-built schema examples | Contract only |
| Phase 1 orders | Random PM sizes near tape VWAP | Tests schema/TCA without a full fund |
| Fund TD/TE beside TCA | Phase 2 fund vs S&P TR scalars on Phase 3 summary | **Not** execution-cost-adjusted tracking |

---

## 5. Mental model

```text
Real (local data/, not in Git)
  WRDS/CRSP: weights, closes, calendar, CA events, S&P TR
        │
        ├─ Phase 2 daybook ──► close-proxy fills + synthetic flows
        │
        └─ close anchors ──► synthetic stock_days + venue_days
                              + uncalibrated impact
                              ──► Phase 1 / Phase 3

Optional (gitignored plugin; delivery only)
  Live venue / closing-auction feed ──► same MarketTape tables
```

---

## 6. What the default path is good for vs not

**Good for**

- Reproducible Phase 0–3 **mechanics** offline.
- Schema, conservation, notional-weighted TCA plumbing, liquidity buckets.
- Clear separation between fund accounting and execution simulation.

**Not good for (until plugin + calibration)**

- Quoting absolute execution costs or “true” auction participation.
- Claiming live multi-MIC volume shares or imbalance-based MOC costs.
- Selling Phase 2 overnight drift as TCA.

---

## 7. Related knobs

| Knob | File | Meaning |
| --- | --- | --- |
| `TAPE_MODE` | `config.py` | `synthetic` \| `plugin` |
| `SIMP_TAPE_PLUGIN_DIR` | env | Directory with `tape_plugin.py` |
| `TCA_IMPACT_*` | `config.py` | Uncalibrated impact coefficients |
| `FLOW_*` | `config.py` | Synthetic flow scenario |
| `DIVIDEND_SENSITIVITY` | `config.py` | Accrual vs cash-at-ex vs no-cash-div |
| `PHASE*_WRITE_EXTRA` | `config.py` | Optional audit dumps (`stock_days` / `venue_days`, etc.) |
