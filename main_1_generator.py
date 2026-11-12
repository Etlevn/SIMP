#!/usr/bin/env python3
"""Phase 1: synthetic multi-MIC order generator (no Phase 2 required).

Builds random PM orders near same-day VWAP (synthetic tape), routes them through
the Phase 0 desk → broker → fill chain, and writes core tables under
``output/1_generator/``.

Default disk: ``pm_orders.csv``, ``trader_orders.csv``, ``broker_orders.csv``,
``fills.csv``. Set ``PHASE1_WRITE_EXTRA=True`` for ``stock_days.csv`` and
``generator_meta.json`` / ``venue_days.csv``. Tape mode: ``TAPE_MODE`` + optional
``SIMP_TAPE_PLUGIN_DIR`` (gitignored plugin; public default is synthetic).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from order_generator import GeneratorConfig, generate_order_chain
from order_schema import SCHEMA_VERSION
from paths import OUTPUT_1_GENERATOR, clear_output_dir, ensure_phase_output_dir
from preview import print_csv

CORE_TABLES = (
    ("pm_orders.csv", "pm"),
    ("trader_orders.csv", "trader"),
    ("broker_orders.csv", "broker"),
    ("fills.csv", "fills"),
)
EXTRA_TABLES = (
    ("stock_days.csv", "stock_days"),
    ("venue_days.csv", "venue_days"),
)


def main() -> int:
    write_extra = bool(getattr(config, "PHASE1_WRITE_EXTRA", False))
    tape_mode = str(getattr(config, "TAPE_MODE", "synthetic"))
    cfg = GeneratorConfig(
        seed=int(getattr(config, "GENERATOR_SEED", 7)),
        n_orders=int(getattr(config, "GENERATOR_N_ORDERS", 40)),
        trade_date=getattr(config, "GENERATOR_TRADE_DATE", None),
        strategy=str(getattr(config, "GENERATOR_STRATEGY", "VWAP")),
        share_min=int(getattr(config, "GENERATOR_SHARE_MIN", 100)),
        share_max=int(getattr(config, "GENERATOR_SHARE_MAX", 5000)),
        buy_prob=float(getattr(config, "GENERATOR_BUY_PROB", 0.55)),
        reason=str(getattr(config, "GENERATOR_REASON", "REBALANCE")),
        price_noise=float(getattr(config, "GENERATOR_PRICE_NOISE", 0.002)),
        fund_id=str(getattr(config, "GENERATOR_FUND_ID", "SIMP_SP500_10B")),
    )
    result = generate_order_chain(cfg, tape_mode=tape_mode)

    ensure_phase_output_dir(OUTPUT_1_GENERATOR)
    clear_output_dir(OUTPUT_1_GENERATOR)
    result["pm_orders"].to_csv(OUTPUT_1_GENERATOR / "pm_orders.csv", index=False)
    result["trader_orders"].to_csv(OUTPUT_1_GENERATOR / "trader_orders.csv", index=False)
    result["broker_orders"].to_csv(OUTPUT_1_GENERATOR / "broker_orders.csv", index=False)
    result["fills"].to_csv(OUTPUT_1_GENERATOR / "fills.csv", index=False)

    if write_extra:
        result["stock_days"].to_csv(OUTPUT_1_GENERATOR / "stock_days.csv", index=False)
        result["venue_days"].to_csv(OUTPUT_1_GENERATOR / "venue_days.csv", index=False)
        (OUTPUT_1_GENERATOR / "generator_meta.json").write_text(
            json.dumps(
                {
                    "phase": "1_generator",
                    "schema_version": SCHEMA_VERSION,
                    "config": result["config"],
                    "tape": result["tape"],
                    "counts": {
                        "pm_orders": int(len(result["pm_orders"])),
                        "trader_orders": int(len(result["trader_orders"])),
                        "broker_orders": int(len(result["broker_orders"])),
                        "fills": int(len(result["fills"])),
                    },
                },
                indent=2,
                default=str,
            )
            + "\n"
        )

    trade_date = str(pd_trade_date(result["pm_orders"]))
    print(
        f"phase=1_generator schema_version={SCHEMA_VERSION} "
        f"trade_date={trade_date} strategy={cfg.strategy} "
        f"pm={len(result['pm_orders'])} trader={len(result['trader_orders'])} "
        f"broker={len(result['broker_orders'])} fills={len(result['fills'])} "
        f"tape={result['tape']['source']} mode={tape_mode} write_extra={write_extra}"
    )
    for name, title in CORE_TABLES:
        print_csv(OUTPUT_1_GENERATOR / name, title=title, blank_before=True)
    if write_extra:
        for name, title in EXTRA_TABLES:
            print_csv(OUTPUT_1_GENERATOR / name, title=title, blank_before=True)
    return 0


def pd_trade_date(pm) -> object:
    import pandas as pd

    return pd.to_datetime(pm.trade_date).dt.normalize().iloc[0].date()


if __name__ == "__main__":
    raise SystemExit(main())
