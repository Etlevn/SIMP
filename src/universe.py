"""S&P 500 membership and capitalization-weight proxies."""
from __future__ import annotations

import re

import pandas as pd

ALLOWED_MEMBERSHIP_SCHEMAS = {"crsp_a_indexes", "crsp_q_indexes"}
ALLOWED_STOCK_SCHEMAS = {"crsp_a_stock", "crsp_q_stock"}

MEMBER_COLUMNS = {"permno", "mbrstartdt", "mbrenddt"}
STOCK_COLUMNS = {"permno", "permco", "ticker", "cusip", "hdrcusip", "siccd", "nasdissuno", "dlycaldt", "dlycap"}

# Validation helpers
# --------------------------------------------------------------------------
def _validate_schema(schema, allowed, label):
    if schema not in allowed:
        raise ValueError(f"Choose an explicit CRSP {label} release schema: {sorted(allowed)}")
    if not re.fullmatch(r"[a-z_]+", schema):  # identifiers are interpolated
        raise ValueError(f"Invalid {label} schema identifier.")


def _require_columns(db, schema, table, required):
    meta = db.describe_table(library=schema, table=table)
    actual = set(meta["name"].str.lower())
    missing = set(required) - actual
    if missing:
        raise ValueError(f"{schema}.{table} missing columns: {sorted(missing)}")

def _run_checks(db, membership_schema, stock_schema):
    """Validate schemas and required columns once; return (member_table, stock_table)."""
    _validate_schema(membership_schema, ALLOWED_MEMBERSHIP_SCHEMAS, "index")
    _validate_schema(stock_schema, ALLOWED_STOCK_SCHEMAS, "stock")
    _require_columns(db, membership_schema, "dsp500list_v2", MEMBER_COLUMNS)
    _require_columns(db, stock_schema, "dsf_v2", STOCK_COLUMNS)
    return f"{membership_schema}.dsp500list_v2", f"{stock_schema}.dsf_v2"

# --------------------------------------------------------------------------
# Date resolution
# --------------------------------------------------------------------------
def get_price_bounds(db, stock_table):
    """First/last observation date in the stock release."""
    df = db.raw_sql(f"""
        SELECT MIN(dlycaldt) AS first_date, MAX(dlycaldt) AS last_date
        FROM {stock_table}
    """)
    first, last = pd.Timestamp(df.loc[0, "first_date"]), pd.Timestamp(df.loc[0, "last_date"])
    if pd.isna(first) or pd.isna(last):
        raise ValueError(f"No observations in {stock_table}.")
    return first, last


def resolve_effective_date(db, stock_table, target, first_price, last_price, max_rollback):
    """Latest trading day on/before target within the rollback window."""
    if target < first_price or target > last_price:
        raise ValueError(
            f"Requested {target.date()}, but {stock_table} covers "
            f"{first_price.date()} through {last_price.date()}. "
            "Use a suitable release; do not silently substitute stale data."
        )
    df = db.raw_sql(
        f"""
        SELECT MAX(dlycaldt) AS effective_date
        FROM {stock_table}
        WHERE dlycaldt <= %(target)s
          AND dlycaldt >= CAST(%(target)s AS date) - %(rollback)s
        """,
        params={"target": target.date(), "rollback": int(max_rollback)},
    )
    asof = pd.Timestamp(df.loc[0, "effective_date"])
    if pd.isna(asof):
        raise ValueError("No trading date within the allowed rollback window.")
    return asof

def rebalance_dates(start_date, end_date):
    """Third Friday of Mar/Jun/Sep/Dec within [start_date, end_date]."""
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if start > end:
        raise ValueError(f"start_date {start.date()} is after end_date {end.date()}.")
    fridays = pd.date_range(start, end, freq="WOM-3FRI")
    dates = list(fridays[fridays.month.isin([3, 6, 9, 12])])
    if not dates:
        raise ValueError(f"No quarterly rebalance date between {start.date()} and {end.date()}.")
    return dates


def resolve_next_trading_date(db, stock_table, after, max_forward):
    """First trading day strictly after `after` (within max_forward days), else None."""
    df = db.raw_sql(
        f"""
        SELECT MIN(dlycaldt) AS next_date
        FROM {stock_table}
        WHERE dlycaldt > %(after)s
          AND dlycaldt <= CAST(%(after)s AS date) + %(forward)s
        """,
        params={"after": after.date(), "forward": int(max_forward)},
    )
    nxt = pd.Timestamp(df.loc[0, "next_date"])
    return None if pd.isna(nxt) else nxt

# --------------------------------------------------------------------------
# Data pulls and transforms
# --------------------------------------------------------------------------
def get_membership_bounds(db, member_table):
    """Diagnostic only; open memberships may use far-future end dates."""
    df = db.raw_sql(f"""
        SELECT MIN(mbrstartdt) AS first_start,
               MAX(mbrstartdt) AS latest_start,
               MAX(mbrenddt)   AS latest_end
        FROM {member_table}
    """)
    return df.to_dict("records")[0]


def fetch_constituents(db, member_table, stock_table, asof):
    """S&P 500 members on `asof` with PERMNO, PERMCO, ticker, CUSIP and market cap."""
    data = db.raw_sql(
        f"""
        WITH members AS (
            SELECT DISTINCT permno
            FROM {member_table}
            WHERE mbrstartdt <= %(asof)s
              AND (mbrenddt >= %(asof)s OR mbrenddt IS NULL)
        )
        SELECT m.permno,
               s.permco,
               s.ticker,
               s.cusip,
               s.hdrcusip,
               s.siccd,
               s.nasdissuno,
               s.dlycaldt AS date,
               s.dlycap   AS market_cap
        FROM members AS m
        LEFT JOIN {stock_table} AS s
          ON s.permno = m.permno
         AND s.dlycaldt = %(asof)s
        ORDER BY m.permno
        """,
        params={"asof": asof.date()},
    )
    if data.empty:
        raise ValueError(
            f"No membership returned for {asof.date()}. "
            "Check membership coverage and release freshness."
        )
    if data["permno"].duplicated().any():
        raise ValueError("Duplicate PERMNO rows after the price join.")
    return data


def validate_market_caps(data):
    data["market_cap"] = pd.to_numeric(data["market_cap"], errors="coerce")
    bad = data["market_cap"].isna() | (data["market_cap"] <= 0)
    if bad.any():
        raise ValueError(
            f"{bad.sum()} constituents have missing/nonpositive market cap. "
            f"Investigate rather than renormalizing: {data.loc[bad, 'permno'].tolist()}"
        )
    return data


def add_cap_weights(data, asof):
    out = data.copy()
    out["date"] = asof
    out["cap_weight_proxy"] = out["market_cap"] / out["market_cap"].sum()
    out["cap_weight_proxy_pct"] = 100 * out["cap_weight_proxy"]
    cols = ["date", "permno", "permco", "ticker", "cusip", "hdrcusip", "siccd", "nasdissuno", "market_cap",
            "cap_weight_proxy", "cap_weight_proxy_pct"]
    return (out[cols]
            .sort_values("cap_weight_proxy", ascending=False)
            .reset_index(drop=True))


def build_diagnostics(target, asof, member_table, stock_table, last_price, member_bounds, data):
    return {
        "requested_date": target.date(),
        "effective_date": asof.date(),
        "membership_table": member_table,
        "stock_table": stock_table,
        "stock_latest_date": last_price.date(),
        "membership_bounds": member_bounds,
        "n_securities": len(data),
        "weight_sum": float(data["cap_weight_proxy"].sum()),
        "official_sp500_weights": False,
    }

# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------
def sp500_cap_weights(
    db,
    target_date,
    membership_schema="crsp_q_indexes",
    stock_schema="crsp_q_stock",
    max_calendar_rollback=7,
):
    """
    Historical S&P 500 membership with CRSP capitalization weights.

    Returns (DataFrame, diagnostics dict). Columns: date, permno, permco,
    ticker, cusip, market_cap, cap_weight_proxy, cap_weight_proxy_pct.

    cap_weight_proxy is NOT the official float-adjusted S&P index weight.
    Date convention: close of the latest trading day on/before target_date;
    refuses requests beyond the stock release's latest observation.
    """
    member_table, stock_table = _run_checks(db, membership_schema, stock_schema)

    target = pd.Timestamp(target_date).normalize()
    first_price, last_price = get_price_bounds(db, stock_table)
    asof = resolve_effective_date(
        db, stock_table, target, first_price, last_price, max_calendar_rollback
    )

    member_bounds = get_membership_bounds(db, member_table)
    data = fetch_constituents(db, member_table, stock_table, asof)
    data = validate_market_caps(data)
    data = add_cap_weights(data, asof)

    diagnostics = build_diagnostics(
        target, asof, member_table, stock_table, last_price, member_bounds, data
    )
    return data, diagnostics

def sp500_cap_weights_quarterly(
    db,
    start_date,
    end_date,
    membership_schema="crsp_q_indexes",
    stock_schema="crsp_q_stock",
    max_calendar_rollback=7,
    on_unavailable="raise",
):
    """
    S&P 500 constituents and CRSP cap weights on the first trading day after each
    quarterly rebalance (third Friday of Mar/Jun/Sep/Dec; the Thursday if that
    Friday is a market holiday) with the rebalance date in [start_date, end_date].

    Panel columns: nominal_date (third Friday), reference_date (actual rebalance
    trading day), then the same columns as sp500_cap_weights, where `date` is the
    snapshot day. Weights are normalized within each snapshot.

    on_unavailable: "raise" fails if a snapshot is outside the stock release's
    coverage; "skip" drops it (listed in diagnostics["skipped_dates"]).
    Returns (panel, diagnostics); diagnostics["per_quarter"] is a summary DataFrame.
    """
    if on_unavailable not in {"raise", "skip"}:
        raise ValueError("on_unavailable must be 'raise' or 'skip'.")

    nominal_dates = rebalance_dates(start_date, end_date)
    member_table, stock_table = _run_checks(db, membership_schema, stock_schema)
    first_price, last_price = get_price_bounds(db, stock_table)

    plan, skipped = [], []
    for nominal in nominal_dates:
        snap = None
        if first_price <= nominal <= last_price:
            ref = resolve_effective_date(
                db, stock_table, nominal, first_price, last_price, max_calendar_rollback
            )
            snap = resolve_next_trading_date(db, stock_table, ref, max_calendar_rollback)
        if snap is None:
            skipped.append(nominal)
        else:
            plan.append((nominal, ref, snap))

    if skipped and on_unavailable == "raise":
        raise ValueError(
            f"No snapshot inside {stock_table} coverage ({first_price.date()} to "
            f"{last_price.date()}) for: {[d.date().isoformat() for d in skipped]}. "
            "Shorten the range or pass on_unavailable='skip'."
        )
    if not plan:
        raise ValueError("None of the requested dates fall inside the release's coverage.")

    frames, summary = [], []
    for nominal, ref, snap in plan:
        data = fetch_constituents(db, member_table, stock_table, snap)
        data = add_cap_weights(validate_market_caps(data), snap)
        data.insert(0, "reference_date", ref)
        data.insert(0, "nominal_date", nominal)
        frames.append(data)
        summary.append({
            "nominal_date": nominal, "reference_date": ref, "date": snap,
            "n_securities": len(data), "n_permcos": int(data["permco"].nunique()),
            "weight_sum": float(data["cap_weight_proxy"].sum()),
        })

    diagnostics = {
        "stock_latest_date": last_price.date(),
        "skipped_dates": [d.date() for d in skipped],
        "per_quarter": pd.DataFrame(summary),
    }
    return pd.concat(frames, ignore_index=True), diagnostics


__all__ = [
    "ALLOWED_MEMBERSHIP_SCHEMAS",
    "ALLOWED_STOCK_SCHEMAS",
    "MEMBER_COLUMNS",
    "STOCK_COLUMNS",
    "add_cap_weights",
    "build_diagnostics",
    "fetch_constituents",
    "get_membership_bounds",
    "get_price_bounds",
    "rebalance_dates",
    "resolve_effective_date",
    "resolve_next_trading_date",
    "sp500_cap_weights",
    "sp500_cap_weights_quarterly",
    "validate_market_caps",
    "_validate_schema",
    "_require_columns",
]
