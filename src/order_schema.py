"""SIMP Phase 0 order contract: PM → Trader → Broker → Fill.

Personal schema (not a mirror of the team ``tca/schema`` names). Quantities are
unsigned whole shares; ``side`` carries direction. Timestamps are tz-naive
America/New_York wall clock.
"""
from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import pandas as pd

SCHEMA_VERSION = "0.1.0"

SIDES = {"BUY", "SELL"}
REASONS = {"INITIAL", "REBALANCE", "INFLOW", "OUTFLOW"}
BENCHMARKS = {"CLOSE", "ARRIVAL", "VWAP"}
URGENCIES = {"LOW", "MEDIUM", "HIGH"}
STRATEGIES = {"CLOSE_AUCTION", "VWAP", "ARRIVAL_IS"}
ORDER_TYPES = {"MOC", "LOC", "LIMIT", "MARKET", "MIDPOINT_PEG"}
TIFS = {"AT_CLOSE", "DAY", "IOC", "GTT"}
LIQUIDITY = {"AUCTION", "ADD", "REMOVE", "MIDPOINT", "DARK"}

# column -> (kind, required). kinds: str/int/float/ts/date or enum set
PM_ORDER = {
    "order_id": ("str", True),
    "fund_id": ("str", True),
    "decision_date": ("date", True),
    "decision_ts": ("ts", True),
    "trade_date": ("date", True),
    "permno": ("int", True),
    "ticker": ("str", False),
    "side": (SIDES, True),
    "shares": ("int", True),
    "decision_price": ("float", True),
    "reason": (REASONS, True),
    "benchmark": (BENCHMARKS, True),
    "urgency": (URGENCIES, True),
    "notional_usd": ("float", True),
}
TRADER_ORDER = {
    "trader_order_id": ("str", True),
    "order_id": ("str", True),
    "strategy": (STRATEGIES, True),
    "side": (SIDES, True),
    "shares": ("int", True),
    "trade_date": ("date", True),
    "start_ts": ("ts", True),
    "end_ts": ("ts", True),
    "max_participation": ("float", True),
    "limit_price": ("float", False),
    "primary_mic": ("str", True),
    "is_spill": ("bool", False),
}
BROKER_ORDER = {
    "child_order_id": ("str", True),
    "trader_order_id": ("str", True),
    "mic": ("str", True),
    "order_type": (ORDER_TYPES, True),
    "tif": (TIFS, True),
    "side": (SIDES, True),
    "shares": ("int", True),
    "limit_price": ("float", False),
    "submit_ts": ("ts", True),
    "bucket_start": ("ts", True),
    "bucket_end": ("ts", True),
}
FILL = {
    "fill_id": ("str", True),
    "child_order_id": ("str", True),
    "mic": ("str", True),
    "fill_ts": ("ts", True),
    "side": (SIDES, True),
    "shares": ("int", True),
    "price": ("float", True),
    "liquidity": (LIQUIDITY, True),
    "fee_usd": ("float", True),
    "impact_bps": ("float", False),
}

SCHEMAS = {
    "pm_orders": PM_ORDER,
    "trader_orders": TRADER_ORDER,
    "broker_orders": BROKER_ORDER,
    "fills": FILL,
}
KEYS = {
    "pm_orders": "order_id",
    "trader_orders": "trader_order_id",
    "broker_orders": "child_order_id",
    "fills": "fill_id",
}

DEFAULT_FUND_ID = "SIMP_SP500_10B"


def make_id(*parts, n=24) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:n]


def empty(table: str) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in SCHEMAS[table]})


def field_dictionary() -> pd.DataFrame:
    """One-row-per-field catalog for Phase 0 documentation artifacts."""
    rows = []
    for table, spec in SCHEMAS.items():
        for col, (kind, required) in spec.items():
            rows.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "table": table,
                    "column": col,
                    "kind": (
                        "enum:" + "|".join(sorted(kind))
                        if isinstance(kind, set)
                        else kind
                    ),
                    "required": bool(required),
                    "is_key": col == KEYS[table],
                }
            )
    return pd.DataFrame(rows)


def validate(df: pd.DataFrame, table: str) -> bool:
    """Raise ValueError listing every schema violation in ``df``."""
    if table not in SCHEMAS:
        raise ValueError(f"unknown table {table}")
    spec, problems = SCHEMAS[table], []
    missing = [c for c, (_, req) in spec.items() if req and c not in df]
    if missing:
        raise ValueError(f"{table}: missing columns {missing}")
    key = KEYS[table]
    if df[key].isna().any() or df[key].duplicated().any():
        problems.append(f"{key} null/duplicated")
    for col, (kind, req) in spec.items():
        if col not in df:
            continue
        s = df[col]
        if req and s.isna().any():
            problems.append(f"{col} has nulls")
        v = s.dropna()
        if isinstance(kind, set):
            bad = set(v.astype(str)) - kind
            if bad:
                problems.append(f"{col} invalid values {sorted(bad)[:5]}")
        elif kind == "bool":
            if not set(v.map(type)) <= {bool, np.bool_}:
                # accept 0/1
                num = pd.to_numeric(v, errors="coerce")
                if num.isna().any() or not set(num.unique()) <= {0, 1}:
                    problems.append(f"{col} not boolean")
        elif kind in ("int", "float"):
            num = pd.to_numeric(v, errors="coerce")
            if num.isna().any() or not np.isfinite(num).all():
                problems.append(f"{col} not numeric/finite")
            elif kind == "int" and not np.allclose(num, np.round(num)):
                problems.append(f"{col} not whole")
        elif kind in ("ts", "date"):
            if pd.to_datetime(v, errors="coerce").isna().any():
                problems.append(f"{col} not a timestamp")
    if "shares" in df and (pd.to_numeric(df.shares, errors="coerce") <= 0).any():
        problems.append("shares must be positive")
    for col in ("decision_price", "price", "limit_price"):
        if col in df and (pd.to_numeric(df[col], errors="coerce").dropna() <= 0).any():
            problems.append(f"{col} must be positive")
    if table == "trader_orders" and len(df):
        if (pd.to_datetime(df.end_ts) < pd.to_datetime(df.start_ts)).any():
            problems.append("end_ts before start_ts")
        mp = pd.to_numeric(df.max_participation, errors="coerce")
        if not mp.between(0, 1, inclusive="right").all():
            problems.append("max_participation outside (0, 1]")
    if table == "broker_orders" and len(df):
        if (pd.to_datetime(df.bucket_end) < pd.to_datetime(df.bucket_start)).any():
            problems.append("bucket_end before bucket_start")
        moc = df.order_type.isin(["MOC", "LOC"])
        if (moc & df.tif.ne("AT_CLOSE")).any():
            problems.append("MOC/LOC orders must be AT_CLOSE")
    if problems:
        raise ValueError(f"{table}: " + "; ".join(problems))
    return True


def validate_links(
    pm: pd.DataFrame,
    trader: pd.DataFrame,
    broker: pd.DataFrame,
    fills: pd.DataFrame | None = None,
) -> bool:
    """Foreign keys, side consistency, and share conservation down the chain."""
    for df, name in (
        (pm, "pm_orders"),
        (trader, "trader_orders"),
        (broker, "broker_orders"),
    ):
        validate(df, name)
    problems: list[str] = []

    def check(child, parent, fk, label):
        keep = [fk, "side", "shares"]
        m = child.merge(
            parent[keep],
            on=fk,
            how="left",
            suffixes=("", "_parent"),
            indicator=True,
        )
        if (m._merge != "both").any():
            problems.append(f"{label}: {int((m._merge != 'both').sum())} orphan rows")
        if (m.side != m.side_parent).dropna().any():
            problems.append(f"{label}: side differs from parent")
        tot = child.groupby(fk).shares.sum()
        cap = parent.set_index(fk).shares.reindex(tot.index)
        if (tot > cap + 1e-9).any():
            problems.append(f"{label}: {int((tot > cap).sum())} parents over-allocated")

    check(trader, pm, "order_id", "trader->pm")
    check(broker, trader, "trader_order_id", "broker->trader")
    if len(broker) and len(trader):
        w = broker.merge(
            trader[["trader_order_id", "start_ts", "end_ts"]], on="trader_order_id"
        )
        if (
            (pd.to_datetime(w.bucket_start) < pd.to_datetime(w.start_ts))
            | (pd.to_datetime(w.bucket_end) > pd.to_datetime(w.end_ts))
        ).any():
            problems.append("broker bucket outside trader window")
    if fills is not None and len(fills):
        validate(fills, "fills")
        check(fills, broker, "child_order_id", "fill->broker")
    if problems:
        raise ValueError("; ".join(problems))
    return True


def conservation_report(
    pm: pd.DataFrame,
    trader: pd.DataFrame,
    broker: pd.DataFrame,
    fills: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Per-parent allocated vs capacity shares (audit table for Phase 0)."""
    rows: list[dict[str, Any]] = []

    def add(level, parent_key, parent, child, fk):
        if parent is None or parent.empty:
            return
        cap = parent.set_index(parent_key)["shares"]
        tot = (
            child.groupby(fk)["shares"].sum()
            if child is not None and len(child)
            else pd.Series(dtype=float)
        )
        for pid, capacity in cap.items():
            allocated = float(tot.get(pid, 0.0))
            rows.append(
                {
                    "level": level,
                    "parent_id": pid,
                    "capacity_shares": int(capacity),
                    "allocated_shares": int(allocated),
                    "residual_shares": int(capacity - allocated),
                    "ok": allocated <= capacity + 1e-9,
                }
            )

    add("trader->pm", "order_id", pm, trader, "order_id")
    add("broker->trader", "trader_order_id", trader, broker, "trader_order_id")
    if fills is not None:
        add("fill->broker", "child_order_id", broker, fills, "child_order_id")
    return pd.DataFrame(rows)


def pm_orders_from_blotter(
    blotter: pd.DataFrame,
    *,
    fund_id: str = DEFAULT_FUND_ID,
    trade_date_col: str | None = None,
    price_col: str | None = None,
    quantity_col: str | None = None,
    ticker_map=None,
    decision_time: str = "16:00",
) -> pd.DataFrame:
    """Fund simulator blotter → Phase 0 ``pm_orders``."""
    b = blotter.copy()
    if b.empty:
        return empty("pm_orders")
    q_col = quantity_col or (
        "signed_quantity_execution_basis"
        if "signed_quantity_execution_basis" in b
        else "signed_quantity"
    )
    p_col = price_col or (
        "decision_price_execution_basis"
        if "decision_price_execution_basis" in b
        else "decision_price"
    )
    t_col = trade_date_col or next(
        c for c in ("expected_execution_date", "execution_date") if c in b
    )
    qty = pd.to_numeric(b[q_col], errors="raise")
    reason = b["reason"] if "reason" in b else pd.Series("REBALANCE", index=b.index)
    out = pd.DataFrame(
        {
            "order_id": b["order_id"].astype(str)
            if "order_id" in b
            else [make_id(d, p, r) for d, p, r in zip(b.decision_date, b.permno, reason)],
            "fund_id": fund_id,
            "decision_date": pd.to_datetime(b.decision_date).dt.normalize(),
            "trade_date": pd.to_datetime(b[t_col]).dt.normalize(),
            "permno": b.permno.astype(int),
            "side": np.where(qty > 0, "BUY", "SELL"),
            "shares": qty.abs().round().astype(int),
            "decision_price": pd.to_numeric(b[p_col], errors="raise").astype(float),
            "reason": reason.astype(str).values,
        }
    )
    out["decision_ts"] = out.decision_date + pd.Timedelta(decision_time + ":00")
    out["benchmark"] = np.where(
        out.reason.isin(["INITIAL", "REBALANCE"]), "CLOSE", "ARRIVAL"
    )
    out["urgency"] = np.where(out.reason.eq("OUTFLOW"), "HIGH", "MEDIUM")
    out["notional_usd"] = out.shares * out.decision_price
    if ticker_map is not None:
        out["ticker"] = out.permno.map(ticker_map)
    out = out[out.shares > 0].reset_index(drop=True)
    validate(out, "pm_orders")
    return out


def notional_summary(pm_orders: pd.DataFrame) -> pd.DataFrame:
    """Notional-weighted blotter summary by reason (and overall)."""
    cols = [
        "reason",
        "orders",
        "notional_usd",
        "notional_share",
        "buy_notional_usd",
        "sell_notional_usd",
    ]
    if pm_orders is None or pm_orders.empty:
        return pd.DataFrame(columns=cols)
    p = pm_orders.copy()
    p["notional_usd"] = pd.to_numeric(p.notional_usd, errors="coerce")
    total = float(p.notional_usd.sum())
    rows = []
    for reason, g in p.groupby("reason", dropna=False):
        buy = g.loc[g.side.eq("BUY"), "notional_usd"].sum()
        sell = g.loc[g.side.eq("SELL"), "notional_usd"].sum()
        notion = float(g.notional_usd.sum())
        rows.append(
            {
                "reason": reason,
                "orders": int(len(g)),
                "notional_usd": notion,
                "notional_share": (notion / total) if total else 0.0,
                "buy_notional_usd": float(buy),
                "sell_notional_usd": float(sell),
            }
        )
    rows.append(
        {
            "reason": "ALL",
            "orders": int(len(p)),
            "notional_usd": total,
            "notional_share": 1.0 if total else 0.0,
            "buy_notional_usd": float(p.loc[p.side.eq("BUY"), "notional_usd"].sum()),
            "sell_notional_usd": float(p.loc[p.side.eq("SELL"), "notional_usd"].sum()),
        }
    )
    return pd.DataFrame(rows)

