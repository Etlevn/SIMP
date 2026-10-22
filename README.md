# Simulated Investment Manager Platform (SIMP)

A research prototype for a **simulated large-cap US equity index fund** (illustrative ~$10B AUM) built from historical market data. The long-term arc is:

**Historical index universe → portfolio construction → order generation → execution simulation → transaction-cost analysis (TCA) → performance reporting.**

This repository implements a **modular Python pipeline** for the first research phase: build or reuse a local cache, reconcile corporate actions from a research database extract and an authorized third-party CSV, run a human review gate, then replay a daily fund ledger (orders, fills, holdings, NAV).

It is a _work in progress_, not a production backtester or live investment strategy. Licensed market data is **not** shipped in the repository.

## Current implementation

| Component                                    | Status                            | Notes                                                                               |
| -------------------------------------------- | --------------------------------- | ----------------------------------------------------------------------------------- |
| Historical index membership / universe       | Implemented (cache or live fetch) | Permanent security IDs (`permno`) as the primary join key                           |
| Capitalization-weight proxies                | Implemented                       | Derived from market-cap panels; **not** official float-adjusted index weights       |
| Quarterly target snapshots                   | Implemented                       | Snapshot dates must align with the configured experiment window                     |
| Fund targets & cash buffer                   | Implemented                       | Whole-share targets; configurable buffer / fee knobs in `config.py`                 |
| Rebalance orders & close-price fill proxy    | Implemented                       | Next-session close as an illustrative execution price, not a calibrated fill model  |
| Corporate-action extract + CSV normalization | Implemented                       | Live database pull optional; CSV is a local authorized export (no vendor API)       |
| Event reconciliation & review gate           | Implemented                       | Agreement / conflict / quarantine; `APPROVE` / `EXCLUDE` with rationale             |
| Ordinary split & cash-dividend ledger        | Implemented                       | Multi-session accrual/payment for approved ordinary USD dividends and splits        |
| Daily fund outputs                           | Implemented                       | Written under `output/data/` via `data_loader.py`                                   |
| Assisted PENDING review                      | Implemented                       | Optional `scripts/review_auto_pending.py` (remaining PENDING default to `EXCLUDE`)  |
| Auction / VWAP / TWAP execution & TCA        | Not in the main path              | Kept as later extensions; helpers may exist but are not wired into `data_loader.py` |
| Flows, benchmarks, vendor ID mapping         | Not in the main path              | Optional inputs documented in [`dataset.md`](dataset.md)                            |

**Validation caveat:** Unit and integration tests exercise logic on synthetic fixtures. Correctness on your licensed live extracts depends on entitlements, review decisions, and sign-off for the chosen quarter.

## Repository layout

```text
SIMP/
├── README.md
├── dataset.md              # Data layout, table schemas, path mapping notes
├── requirements.txt
├── config.py               # User-editable run parameters (edit this)
├── data_loader.py          # Entrypoint
├── research_pipeline.py    # Compatibility module; main flow does not use it
├── .env.template           # Credential placeholders (copy to gitignored .env)
├── corporate_actions_template.csv
├── src/                    # Runtime modules (import with PYTHONPATH=src)
├── scripts/                # Optional helpers (assisted review)
├── tests/
├── data/                   # raw / cache / review (gitignored contents; placeholders only)
└── output/data/            # Ledger outputs (gitignored; placeholder only)
```

Licensed extracts and generated ledgers stay local. Details for every cache/review/output table: **[`dataset.md`](dataset.md)**.

## Quick start

Python **3.10–3.15** (3.15 needs `pandas>=3`).

```bash
python -m venv .venv
source .venv/bin/activate             # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### Credentials (only if fetching live data)

```bash
cp .env.template .env
# Edit .env with your institutional database username/password.
# Never commit .env.
```

If you already have a validated `data/cache/`, you can skip live login and use cache mode below.

### Corporate-action CSV

Place an authorized, schema-compatible corporate-action export under `data/raw/` (see [`dataset.md`](dataset.md) for the expected filename and columns). Start from the empty schema in [`corporate_actions_template.csv`](corporate_actions_template.csv). The pipeline does **not** call a market-data vendor API for this file.

### One-shot ledger run (reuse local cache)

1. In [`config.py`](config.py), set `INPUT_MODE = "cache"` and `RESET_REVIEW_TO_PENDING = False`.
2. Confirm `START_DATE` / `END_DATE` match `data/cache/coverage.json`.
3. Ensure review decisions are complete (`APPROVE` / `EXCLUDE` with rationales), or use the assisted-review step below.
4. Run:

```bash
python data_loader.py
```

Outputs appear under `output/data/` (including flat `run_provenance.json` with window knobs and file hashes).

### Rebuild review for a window (three steps)

1. Set `RESET_REVIEW_TO_PENDING = True` → `python data_loader.py`  
   (rewrites PENDING decisions, clears stale `output/data/`, then **stops**).
2. Optional assisted fill of PENDING rows:

```bash
PYTHONPATH=src python scripts/review_auto_pending.py
```

By default, rows still PENDING after rules are marked `EXCLUDE` so the ledger gate can pass. Set `EXCLUDE_REMAINING_PENDING = False` in that script to leave them PENDING for manual work.

3. Set `RESET_REVIEW_TO_PENDING = False` → `python data_loader.py`  
   (runs the ledger).

### Live cache refresh

Set `INPUT_MODE = "fetch_wrds"` in `config.py` (config value name is historical), ensure `.env` credentials work, then run `python data_loader.py`. After a successful pull you can switch back to `"cache"`.

### Tests

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

## Corporate-actions pipeline (summary)

```text
Research DB extracts                         Authorized CA CSV
  distributions / delists / factors / IDs         normalized columns
             |                                         |
             +------------- normalization -------------+
                                |
                    Identifier & date mapping
                                |
                    Event reconciliation + review queue
                                |
              Approved ordinary splits / dividends → daily ledger
```

**Review policy (high level):**

- Candidate key: security (`permno`), normalized event type, and **ex-date**.
- Content fingerprint (`review_key`): approvals become stale if source terms change.
- Preserve both sources; flag conflicts for humans.
- Only reviewed ordinary splits and USD cash dividends enter prototype accounting.
- Mergers, spin-offs, rights, special distributions, and similar events stay `EXCLUDE` / `PENDING` until accounting exists.

**Avoid double counting:** If you book explicit dividend/split cash and shares, do not also treat total-return-adjusted prices as if distributions were unpaid.

## Research conventions and limitations

1. **Point-in-time:** Weights observed at a close are not tradeable at an earlier close. Default replay uses decision close then next-session fill proxy.
2. **Index tracking:** Cap-weight proxies are not official float-adjusted index weights.
3. **Execution:** Daily close is an illustrative price, not a microstructure-aware fill simulator.
4. **Scope:** Main path stops at the reviewed daily fund ledger. Execution venues, TCA, broker allocation, and multi-strategy fund types are future work.
5. **Data rights:** Access and redistribution follow your institutional licenses. A public git host does **not** grant redistribution rights. Keep licensed extracts and credentials out of Git.

## Work remaining

- [ ] Harden live entitlement checks and full historical membership coverage.
- [ ] Dated security-ID mapping across research DB, execution-data vendor, and CA CSV.
- [ ] Broader live CA adjudication beyond the current ordinary split/dividend set.
- [ ] Optional investor flows, benchmarks, and restricted-portfolio scenarios in the main entrypoint.
- [ ] Feasible execution assumptions and TCA once order/fill integration is defined.
- [ ] Broader reproducibility (CI, documented signed-off quarters).

## Collaboration

Prefer feature branches and pull requests over commits directly to the default branch. Keep large or proprietary extracts outside Git. Do not commit `.env`, passwords, or cleared notebook outputs that embed licensed tables.

## Intended use

Academic / industry-sponsored research prototype for teaching and methodology development. No market data is included in the repository. Consult your data-vendor license terms before sharing or publishing derived datasets.

## License

This software is released under the [MIT License](LICENSE). Licensed market data and third-party extracts are **not** included and remain subject to their own terms.
