#!/usr/bin/env python3
"""Phase 0: versioned PM → Trader → Broker → Fill contract + worked example.

Default disk (core): ``example_{pm,trader,broker,fills}.csv``.
Set ``PHASE0_WRITE_EXTRA=True`` in ``config.py`` for field dictionary,
conservation report, schema_version.txt, and validation_report.json.

Independent of Phase 2 fund replay (synthetic sample only).
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
from order_schema import SCHEMA_VERSION, field_dictionary, validate_links
from paths import OUTPUT_0_SCHEMA, clear_output_dir, ensure_phase_output_dir
from preview import print_csv, print_frame
from schema_example import build_worked_example

CORE_TABLES = (
    ("example_pm_orders.csv", "pm"),
    ("example_trader_orders.csv", "trader"),
    ("example_broker_orders.csv", "broker"),
    ("example_fills.csv", "fills"),
)
EXTRA_TABLES = (
    ("conservation_report.csv", "conservation"),
    ("field_dictionary.csv", "fields"),
)


def main() -> int:
    write_extra = bool(getattr(config, "PHASE0_WRITE_EXTRA", False))
    ensure_phase_output_dir(OUTPUT_0_SCHEMA)
    clear_output_dir(OUTPUT_0_SCHEMA)

    example = build_worked_example()
    validate_links(
        example["pm_orders"],
        example["trader_orders"],
        example["broker_orders"],
        example["fills"],
    )
    conservation_ok = bool(example["conservation_report"]["ok"].all())

    example["pm_orders"].to_csv(OUTPUT_0_SCHEMA / "example_pm_orders.csv", index=False)
    example["trader_orders"].to_csv(
        OUTPUT_0_SCHEMA / "example_trader_orders.csv", index=False
    )
    example["broker_orders"].to_csv(
        OUTPUT_0_SCHEMA / "example_broker_orders.csv", index=False
    )
    example["fills"].to_csv(OUTPUT_0_SCHEMA / "example_fills.csv", index=False)

    report = {
        "phase": "0_schema",
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "write_extra": write_extra,
        "pm_orders": int(len(example["pm_orders"])),
        "trader_orders": int(len(example["trader_orders"])),
        "broker_orders": int(len(example["broker_orders"])),
        "fills": int(len(example["fills"])),
        "conservation_ok": conservation_ok,
        "residual_shares_total": int(
            example["conservation_report"]["residual_shares"].sum()
        ),
    }

    if write_extra:
        fields = field_dictionary()
        example["conservation_report"].to_csv(
            OUTPUT_0_SCHEMA / "conservation_report.csv", index=False
        )
        fields.to_csv(OUTPUT_0_SCHEMA / "field_dictionary.csv", index=False)
        (OUTPUT_0_SCHEMA / "schema_version.txt").write_text(SCHEMA_VERSION + "\n")
        (OUTPUT_0_SCHEMA / "validation_report.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )

    print(
        f"phase=0_schema schema_version={SCHEMA_VERSION} "
        f"write_extra={write_extra} "
        f"pm={report['pm_orders']} trader={report['trader_orders']} "
        f"broker={report['broker_orders']} fills={report['fills']} "
        f"conservation_ok={conservation_ok}"
    )
    for name, title in CORE_TABLES:
        print_csv(OUTPUT_0_SCHEMA / name, title=title, blank_before=True)
    if write_extra:
        for name, title in EXTRA_TABLES:
            print_csv(OUTPUT_0_SCHEMA / name, title=title, blank_before=True)
    else:
        print_frame(
            example["conservation_report"],
            title="conservation (not written; PHASE0_WRITE_EXTRA=False)",
            blank_before=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
