# Phase 0 order schema (SIMP)

Implemented in [`src/order_schema.py`](../src/order_schema.py). Version: see `SCHEMA_VERSION` / `output/0_schema/schema_version.txt`.

Quantities are **unsigned whole shares**; `side` carries direction. Timestamps are tz-naive America/New_York wall clock. This is SIMP’s personal contract (not a column-for-column copy of the team `tca/schema`).

```text
pm_orders 1 ──< trader_orders 1 ──< broker_orders 1 ──< fills
 (portfolio)      (desk strategy)      (child @ MIC)       (execution)
```

## `pm_orders`

| column | type | notes |
| --- | --- | --- |
| `order_id` | str | Stable id (blotter `order_id` when mapped from Phase 2) |
| `fund_id` | str | Default `SIMP_SP500_10B` |
| `decision_date`, `decision_ts` | date, ts | Decision session + 16:00 |
| `trade_date` | date | Session the order must complete |
| `permno`, `ticker?` | int, str | CRSP id; optional ticker for venue-id lookup later |
| `side` | BUY/SELL | |
| `shares` | int | Unsigned whole shares |
| `decision_price` | float | Decision-session close |
| `reason` | INITIAL/REBALANCE/INFLOW/OUTFLOW | |
| `benchmark` | CLOSE/ARRIVAL/VWAP | CLOSE for index; ARRIVAL for flows |
| `urgency` | LOW/MEDIUM/HIGH | HIGH for OUTFLOW |
| `notional_usd` | float | shares × decision_price |

## `trader_orders`

| column | type | notes |
| --- | --- | --- |
| `trader_order_id`, `order_id` | str | FK → PM |
| `strategy` | CLOSE_AUCTION/VWAP/ARRIVAL_IS | |
| `side`, `shares`, `trade_date` | | Σ shares over a PM ≤ PM shares |
| `start_ts`, `end_ts` | ts | Working window |
| `max_participation` | float (0, 1] | Of expected volume in the window |
| `limit_price?` | float | |
| `primary_mic` | str | Listing venue for MOC |
| `is_spill?` | bool | Overflow leg above participation cap |

## `broker_orders`

| column | type | notes |
| --- | --- | --- |
| `child_order_id`, `trader_order_id` | str | |
| `mic` | str | Venue; `XADF` = off-exchange / dark |
| `order_type` | MOC/LOC/LIMIT/MARKET/MIDPOINT_PEG | |
| `tif` | AT_CLOSE/DAY/IOC/GTT | MOC/LOC must be AT_CLOSE |
| `side`, `shares`, `limit_price?` | | Σ shares = trader shares |
| `submit_ts`, `bucket_start`, `bucket_end` | ts | Bucket inside trader window |

## `fills`

| column | type | notes |
| --- | --- | --- |
| `fill_id`, `child_order_id`, `mic`, `fill_ts` | | |
| `side`, `shares`, `price` | | Σ shares ≤ child shares |
| `liquidity` | AUCTION/ADD/REMOVE/MIDPOINT/DARK | |
| `fee_usd`, `impact_bps?` | float | Modelled; Phase 0 example uses zeros |

## Invariants (`validate` / `validate_links`)

- Keys unique and non-null; every child references an existing parent.
- Child `side` matches parent.
- Shares never over-allocate a parent.
- MOC/LOC require `AT_CLOSE`; broker buckets lie inside the trader window.

## Phase 0 artifacts

`python main_0_schema.py` always writes the four example chain tables under `output/0_schema/`. Set `PHASE0_WRITE_EXTRA=True` in `config.py` for field dictionary, conservation report, `schema_version.txt`, and `validation_report.json`. No WRDS or external venue feed required.
