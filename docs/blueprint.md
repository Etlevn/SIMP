# SIMP Project Blueprint

**Status:** living personal research plan (not a claim that every phase is complete)  
**Updated:** 2026-11-12

This document is the **product vision** for this repository: what SIMP is for, how work is divided into **phases**, what exists today, and what the next code refactor should look like. It is a personal planning note for an independent research prototype.

**Layout (landed):** root entrypoints `fetch_data.py` + `main_<n>_*.py`; logic in `src/`; inputs in `data/`; deliverables in `output/<n>_*/`.

Related operator docs: [`README.md`](../README.md), [`docs/dataset.md`](dataset.md).

---

## 1. Purpose

SIMP models a **simulated asset manager**: a reproducible path from market/reference data → a passive index-fund daybook → a traceable **PM → Trader → Broker** order chain → **execution simulation and TCA**, starting from an illustrative **~$10B S&P 500–style** passive equity fund.

It is **not** only a portfolio backtester, and **not** primarily a toxicity-prediction paper. The goal is a believable **fund → orders → execution → cost** stack that can later host other fund types.

---

## 2. Phase model (product taxonomy)

SIMP’s **product stages** follow the simulated-asset-manager phase model (Schema + Phase 1–3). These names are the source of truth for roadmap, folders, and (after refactor) scripts.

| Phase | Name | What it produces |
| --- | --- | --- |
| **0 — Schema** | Three-level order data model | Versioned PM → Trader → Broker child / Fill contract + validators |
| **1 — Generator** | Synthetic multi-MIC order generator | Random daily order chains priced near same-day VWAP (or documented synthetic stand-in) to exercise the schema and TCA tools **without** a full fund |
| **2 — Index fund** | Index-fund simulation | Membership/prices/events → NAV, holdings, rebalance + flow orders/fills, S&P 500 TR tracking, audit artifacts |
| **3 — Exec / TCA** | Execution & transaction-cost analysis | One rebalance date, then all dates: auction / VWAP / arrival+impact; notional-weighted costs |

**Invariant:** every investment decision that reaches the market must be reconstructible as:

```text
PM Order  →  Trader Order(s)  →  Broker child order(s) / Fill(s)
```

### 2.1 Entrypoints (locked)

| Role | Root file | Writes |
| --- | --- | --- |
| Data prepare (**not** a phase) | `fetch_data.py` | `data/` only |
| Phase 0 | `main_0_schema.py` | `output/0_schema/` |
| Phase 1 | `main_1_generator.py` | `output/1_generator/` |
| Phase 2 | `main_2_fund.py` | `output/2_fund/` |
| Phase 3 | `main_3_tca.py` | `output/3_tca/` |

`fetch_data.py` prepares flat inputs for Phase 2; it is **not** Phase 1. Do **not** invent a parallel “Stage 1/2/3/4” product ladder or a root `main.py` orchestrator.

### 2.2 Build order vs implementation reality

Recommended **delivery order** (schema and generator before scaling TCA):

```text
Phase 0 Schema  →  Phase 1 Generator  →  Phase 2 Fund (finish + PM handoff)  →  Phase 3 TCA
```

**Reality in this repo today:** Phase 0 schema, Phase 2 fund, and Phase 3 one-date synthetic TCA are implemented; Phase 1 is a stub. Next: Phase 3 all-rebalance scale and/or Phase 1 generator; calibrate impact before quoting.

Optional side track (not a numbered phase): flow toxicity / adverse selection—separate interface only.

---

## 3. End-state architecture

```text
                    ┌──────────────────────────┐
                    │ Phase 0 — Order schema   │
                    │ (shared contract)        │
                    └────────────┬─────────────┘
                                 │
          ┌──────────────────────┼──────────────────────┐
          ▼                                             ▼
┌─────────────────────┐                     ┌─────────────────────┐
│ Phase 1 — Generator │                     │ Phase 2 — Index fund│
│ multi-MIC synthetic │                     │ data + daybook      │
│ PM/Trader/Broker…   │                     │ → blotter + PM map  │
└──────────┬──────────┘                     └──────────┬──────────┘
           │                                             │
           └──────────────────┬──────────────────────────┘
                              ▼
                    ┌─────────────────────┐
                    │ Phase 3 — Exec/TCA  │
                    │ auction/VWAP/arrival│
                    │ one date → all dates│
                    └─────────────────────┘
```

Phase 2 internally (logic, not product phase numbers):

```text
reference data (WRDS, …)
        → prepare flat data/ + S&P 500 TR
        → hard exclusions + reference-close schedule + synthetic flows
        → daily_nav / holdings / orders / fills
        → pm_orders + notional + audit + benchmark + run_manifest
        → (later) map blotter → schema PM orders for Phase 3
```

---

## 4. What exists today (mapped onto phases)

### Phase 0 — Schema *(implemented)*

`main_0_schema.py` writes the four example chain tables under `output/0_schema/` by default; extras (`PHASE0_WRITE_EXTRA`) add field dictionary / conservation / validation artifacts. Contract: `src/order_schema.py` + [`docs/order_schema.md`](order_schema.md). Phase 2 emits the same PM columns in `pm_orders.csv` (core handoff; audit extras via `PHASE2_WRITE_EXTRA`).

### Phase 1 — Generator *(stub entrypoint)*

`main_1_generator.py` writes `output/1_generator/STATUS.txt`. No multi-MIC synthetic generator yet.

### Phase 2 — Index fund *(partially implemented)*

| Piece | Status | Entrypoint |
| --- | --- | --- |
| Flat data prepare | Done | `fetch_data.py` |
| Reference-close daybook + flows | Done | `main_2_fund.py` |
| Audit artifacts + run manifest | Done | same |
| S&P 500 TR TD / TE | Done (needs real WRDS TR for quotable numbers) | `fetch_data` ensure + fund run |
| Schema-mapped PM orders | Done | `pm_orders.csv` via `src/order_schema.py` |
| Notional-weighted blotter summaries | Done | `order_notional_summary.csv` |
| Benchmark by-year panel | Done | `benchmark_by_year.csv` |
| Dividend-rule sensitivity | Missing | — |

Already true on the Phase 2 path:

- Reference-close timing (CRSP close as auction-price proxy).
- Seeded synthetic subscriptions/redemptions → flow orders.
- Ordinary splits / USD cash dividends; hard exclusions with renormalization.
- Padded quotes disclosed on disk (look-ahead fallback—not live auction evidence).

Close-to-close “slippage” in the daybook is largely **overnight move**, not execution cost—do not call it TCA.

### Phase 3 — Exec / TCA *(3a one-date landed)*

`main_3_tca.py` reads Phase 2 `pm_orders`, builds a synthetic stock-day tape from CRSP closes, runs CLOSE_AUCTION / VWAP / ARRIVAL_IS desk+impact fills, and writes notional-weighted `order_tca.csv` + `strategy_summary.csv` under `output/3_tca/`. Impact is uncalibrated (mechanics only). Extras via `PHASE3_WRITE_EXTRA`.

---

## 5. Phase specs (target)

### Phase 0 — Order schema

| Level | Question | Core fields (SIMP) |
| --- | --- | --- |
| **PM order** | What / how much / why / when / decision price | `order_id`, `fund_id`, `permno`, `side`, `shares`, `reason`, `decision_ts`, `decision_price` |
| **Trader order** | How to work the PM order | `trader_order_id`, `order_id`, `strategy`, `shares`, `start_ts`, `end_ts` |
| **Broker child / fill** | Where / when / price printed | `child_order_id` / `fill_id`, `mic`, `fill_ts`, `price`, `shares` |

Deliverables (landed): field dictionary, worked example (PM → ≥2 trader legs → children/fills), `validate` / `validate_links`, conservation report, `schema_version`. Open: full child-order lifecycle / status machine; live venue weights (Phase 1/3).

### Phase 1 — Synthetic order generator

- Random daily orders across multiple **MICs**.
- Prices near same-day **VWAP** (or documented synthetic stand-in).
- Emits tables that satisfy Phase 0 (at least PM/Trader; fills if this phase simulates them).
- Runnable **without** Phase 2 fund replay.
- Document seed, universe, MIC list, size, noise parameters.

Layout: `main_1_generator.py` → `output/1_generator/`.

### Phase 2 — Index fund simulation

Quality bar:

| Theme | Direction |
| --- | --- |
| Timing | Effective-date closing-auction proxy (reference close) |
| Benchmark | S&P 500 Total Return; tracking difference & error |
| Flows | Synthetic % of AUM → flow orders beside rebalances |
| Costs | Do not treat next-close zero-cost fills as TCA |
| Weighting | Prefer **notional**-weighted summaries |
| Audit | Schedule, flows, journal, exclusions, padding, run manifest |
| Handoff | Blotter → Phase 0 **PM orders** (`REBALANCE` / subscription / redemption reasons) |

Layout: `fetch_data.py` → `data/`; `main_2_fund.py` → `output/2_fund/`.

### Phase 3 — Execution & TCA

1. One rebalance date: auction price/volume/imbalance, ADV, spread, intraday VWAP (or labeled synthetic).
2. Compare **MOC**, **VWAP**, **arrival + spread/impact**.
3. Per order: size/ADV, size/auction volume, bps vs benchmarks; **notional-weighted** aggregates.
4. Scale to all rebalance dates; liquidity buckets; tracking difference beside costs.
5. Calibrate impact before quoting; synthetic tape = mechanics only.

Layout: `main_3_tca.py` → `output/3_tca/`.

---

## 6. Layout rules (landed)

1. Product language is **Phase 0–3**; `fetch_data.py` is independent.
2. Thin root entrypoints call `src/`; no `scripts/` product stages; no root `main.py` orchestrator.
3. One phase → `output/<n>_<name>/`.
4. Prefer one coherent TCA/order stack over a second incompatible design.

---

## 7. Recommended personal sequence

| Step | Focus | Notes |
| --- | --- | --- |
| **R** | Repo relabel / root entrypoints | *(done)* |
| **0** | Freeze Phase 0 schema + validators | *(done)* |
| **1** | Phase 1 multi-MIC generator | Tests chain without fund complexity |
| **2a** | Phase 2 gaps: dividend-rule sensitivity (PM handoff + notional done) | Fund + PM layer largely runs |
| **3a** | Phase 3 one-date E2E | *(done — synthetic tape)* |
| **3b** | Phase 3 all-rebalance scale | Full-window reports |
| **Q** | Licensed-data sign-off; calibrate impact | Before quoting costs or TE |

Use disposable checks for schemas, ID linkage, NAV identities, and conservation (delete temp tests after they pass).

---

## 8. Data, rights, and quoting rules

- Licensed market extracts and credentials stay out of Git (`data/` contents and all of `output/` are gitignored).
- Synthetic / offline TCA demonstrates **mechanics**; calibrated venue-backed runs are required for research quotes.
- Cap-weight proxies are not official float-adjusted index weights.
- Hard exclusions and flow scenarios bias results; disclose parameters and excluded weight.
- Overnight close-to-close drift ≠ execution cost.
- Running `fetch_data` / `main_2_fund` does **not** mean Phase 1 or Phase 3 are done. Phase 2 still lacks dividend-rule sensitivity; PM handoff + notional summaries are on disk.

---

## 9. Success criteria

For a declared experiment window, treat the main path as research-ready when:

1. **Phase 0–1:** versioned schema; generator produces valid multi-MIC chains.
2. **Phase 2:** reproducible fund run with audit manifest; NAV identity; reference-close schedule; flows + rebalances; TR tracking; blotter maps to PM orders.
3. **Phase 3:** one date then all rebalances compare auction / VWAP / arrival+impact with notional-weighted costs and size-vs-liquidity diagnostics.
4. **Honesty:** padded prices, exclusions, uncalibrated impact, and synthetic tapes are labeled; overnight drift is not sold as TCA.

---

## 10. Document map

| Doc | Role |
| --- | --- |
| [`docs/blueprint.md`](blueprint.md) | Phase model, layout, sequence |
| [`README.md`](../README.md) | Operator quick start |
| [`docs/dataset.md`](dataset.md) | CSV schemas for `data/` and `output/2_fund/` |

Next: Phase 3b all-rebalance scale, impact calibration, and/or Phase 1 generator.
