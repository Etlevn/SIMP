"""Daily prices and trading calendar."""
from __future__ import annotations

import pandas as pd

from wrds_helpers import fetch_crsp_closes_for_two_days


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
