"""Daily prices and trading calendar."""
from __future__ import annotations

import numpy as np
import pandas as pd

from universe import ALLOWED_STOCK_SCHEMAS, _require_columns, _validate_schema

PRICE_COLUMNS = {"permno", "dlycaldt", "dlyprc", "dlyprcflg"}


def _unique_positive_quotes(quotes, expected_ids, asof, price_label="close_price", allow_extra=False):
    """Validate complete, one-to-one prices for the requested PERMNO universe."""
    q = quotes.copy()
    q["date"] = pd.to_datetime(q["date"]).dt.normalize()
    day = pd.Timestamp(asof).normalize()
    q = q.loc[q["date"].eq(day)].copy()
    if q["permno"].duplicated().any():
        raise ValueError(f"Duplicate PERMNO quotes on {day.date()}.")
    expected = set(map(int, expected_ids))
    found = set(map(int, q["permno"]))
    if (expected - found) or (not allow_extra and (found - expected)):
        raise ValueError(f"Missing PERMNO={sorted(expected-found)[:15]}; unexpected={sorted(found-expected)[:15]} on {day.date()}.")
    q = q.loc[q["permno"].isin(expected)].copy()
    q[price_label] = pd.to_numeric(q[price_label], errors="coerce")
    if not np.isfinite(q[price_label].to_numpy(dtype=float)).all() or (q[price_label] <= 0).any():
        raise ValueError(f"Missing/nonpositive/nonfinite {price_label} on {day.date()}.")
    return q


def fetch_crsp_closes_for_two_days(db, permnos, decision_date, execution_date,
                                   stock_schema="crsp_q_stock"):
    """Fetch strictly two observed CRSP CIZ daily closes, with no stale-price fallback."""
    _validate_schema(stock_schema, ALLOWED_STOCK_SCHEMAS, "stock")
    _require_columns(db, stock_schema, "dsf_v2", PRICE_COLUMNS)
    decision_date, execution_date = pd.Timestamp(decision_date), pd.Timestamp(execution_date)
    decision_date, execution_date = decision_date.normalize(), execution_date.normalize()
    if decision_date >= execution_date:
        raise ValueError("Decision close must precede execution close.")
    universe = sorted({int(p) for p in permnos})
    if not universe:
        raise ValueError("The PERMNO universe cannot be empty.")
    table = f"{stock_schema}.dsf_v2"
    raw = db.raw_sql(f"""
        SELECT permno, dlycaldt AS date, dlyprc AS close_price,
               dlyprcflg AS price_flag
        FROM {table}
        WHERE dlycaldt IN (%(decision_date)s, %(execution_date)s)
          AND permno = ANY(%(permnos)s)
        ORDER BY dlycaldt, permno
    """, params={
        "decision_date": decision_date.date(),
        "execution_date": execution_date.date(),
        "permnos": universe,
    })
    raw["date"] = pd.to_datetime(raw["date"]).dt.normalize()
    for date in (decision_date, execution_date):
        _unique_positive_quotes(raw, universe, date)
    return raw.sort_values(["date", "permno"]).reset_index(drop=True)



def fetch_daily_prices(db, permnos, start, end, schema="crsp_q_stock", batch_size=150):
    if schema not in {"crsp_q_stock", "crsp_a_stock"}:
        raise ValueError("Choose an explicit stock release")
    if not 1 <= batch_size <= 500:
        raise ValueError("Batch size must be 1–500")
    ids = sorted(set(map(int, permnos)))
    parts = []
    for i in range(0, len(ids), batch_size):
        parts.append(db.raw_sql(
            f"SELECT permno, dlycaldt AS date, dlyprc AS close_price, "
            f"dlyprcflg AS price_flag FROM {schema}.dsf_v2 "
            "WHERE permno = ANY(%(ids)s) AND dlycaldt BETWEEN %(start)s AND %(end)s",
            params={"ids": ids[i:i + batch_size], "start": str(pd.Timestamp(start).date()),
                    "end": str(pd.Timestamp(end).date())}))
    return pd.concat(parts, ignore_index=True)


def trading_calendar(db, start, end, schema="crsp_q_stock"):
    if schema not in {"crsp_q_stock", "crsp_a_stock"}:
        raise ValueError("Choose an explicit stock release")
    return db.raw_sql(
        f"SELECT DISTINCT dlycaldt AS date FROM {schema}.dsf_v2 "
        "WHERE dlycaldt BETWEEN %(start)s AND %(end)s ORDER BY date",
        params={"start": str(pd.Timestamp(start).date()),
                "end": str(pd.Timestamp(end).date())})


__all__ = ["fetch_daily_prices", "trading_calendar", "fetch_crsp_closes_for_two_days"]
