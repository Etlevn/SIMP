"""WRDS/CRSP query and normalization helpers (no Jupyter runtime)."""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from getpass import getpass
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import wrds
except ImportError:  # pragma: no cover
    wrds = None

userdata = None
IN_COLAB = False

def display(*args, **kwargs):
    return None







WRDS_PGPASS_HOST = "wrds-pgdata.wharton.upenn.edu:9737:wrds"
ALLOWED_MEMBERSHIP_SCHEMAS = {"crsp_a_indexes", "crsp_q_indexes"}
ALLOWED_STOCK_SCHEMAS = {"crsp_a_stock", "crsp_q_stock"}

MEMBER_COLUMNS = {"permno", "mbrstartdt", "mbrenddt"}
STOCK_COLUMNS = {"permno", "permco", "ticker", "cusip", "hdrcusip", "siccd", "nasdissuno", "dlycaldt", "dlycap"}

# --------------------------------------------------------------------------
# WRDS connection helpers
# --------------------------------------------------------------------------
def _get_colab_secret(name):
    if userdata is None:
        raise RuntimeError("Not running in Colab; set credentials another way.")
    try:
        return userdata.get(name)
    except Exception as exc:  # SecretNotFoundError / NotebookAccessError
        raise ValueError(
            f"Secret '{name}' not found or notebook access is disabled. "
            "Add it under Colab Secrets (key icon) and enable Notebook access."
        ) from exc


def _get_wrds_credential(value, key, *, secret=False):
    """Explicit argument, then environment variables / interactive prompt."""
    if value:
        return value
    if IN_COLAB:
        return _get_colab_secret(key)
    value = os.environ.get(key)
    if not value and key == "WRDS_USERNAME":
        value = os.environ.get("WRDS_USER")
    if not value:
        value = getpass(f"{key}: ") if secret else input(f"{key}: ").strip()
    if not value:
        raise ValueError(f"{key} must not be empty.")
    return value


def setup_wrds_credentials(username=None, password=None, user_key="WRDS_USERNAME", pass_key="WRDS_PASSWORD"):
    """Use Colab Secrets in Colab; environment variables or prompts on a PC.

    Explicit arguments override either source. Credentials are also written to the
    PostgreSQL password file used by WRDS, never into notebook source or output.

    Returns ``(username, password)``.
    """
    username = _get_wrds_credential(username, user_key)
    password = _get_wrds_credential(password, pass_key, secret=True)
    if any(char in username + password for char in "\r\n"):
        raise ValueError("Credentials must not contain newline characters.")
    os.environ["WRDS_USER"] = username

    # libpq uses a different default password-file location on Windows.
    default_path = (Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
                    / "postgresql" / "pgpass.conf" if os.name == "nt"
                    else Path.home() / ".pgpass")
    pgpass_path = Path(os.environ.get("PGPASSFILE", str(default_path))).expanduser()
    pgpass_path.parent.mkdir(parents=True, exist_ok=True)
    kept = []
    if pgpass_path.exists():
        kept = [ln for ln in pgpass_path.read_text().splitlines()
                if ln.strip() and not ln.startswith(WRDS_PGPASS_HOST + ":")]
    def escape(value):
        return value.replace("\\", "\\\\").replace(":", "\\:")
    kept.append(f"{WRDS_PGPASS_HOST}:{escape(username)}:{escape(password)}")
    with pgpass_path.open("w") as f:
        if os.name != "nt":
            os.chmod(pgpass_path, 0o600)
        f.write("\n".join(kept) + "\n")
    os.environ["PGPASSFILE"] = str(pgpass_path)

    print("✓ WRDS login environment configured.")
    return username, password


def connect_wrds(username=None, password=None):
    print("Connecting to WRDS...")
    if wrds is None:
        raise ImportError("wrds package is not installed")
    username, password = setup_wrds_credentials(username=username, password=password)
    # autoconnect=False + raise_err avoids wrds.Connection.connect()'s interactive
    # username/password prompts when the first attempt fails (empty password, network, etc.).
    db = wrds.Connection(
        autoconnect=False,
        wrds_username=username,
        wrds_password=password,
    )
    db._Connection__make_sa_engine_conn(raise_err=True)
    db.load_library_list()
    return db


def disconnect_wrds(db):
    db.close()
    print("✓ WRDS database connection closed.")


@contextmanager
def wrds_session(username=None, password=None):
    """with wrds_session(username, password) as db: ...  (always closes the connection)"""
    db = connect_wrds(username=username, password=password)
    try:
        yield db
    finally:
        disconnect_wrds(db)

# --------------------------------------------------------------------------
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


FUND_AUM = 10_000_000_000.0       # simulated starting cash / NAV
CASH_BUFFER_BPS = 10.0           # 10 bps = 0.10% target cash reserve
REFERENCE_SNAPSHOT_DATE = "2026-03-23"  # post-rebalance close; change for another quarter
ILLUSTRATIVE_FEE_BPS = 0.0       # not a calibrated broker commission or impact model

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

def construct_target_portfolio(snapshot_weights, close_quotes, decision_date,
                               aum=FUND_AUM, cash_buffer_bps=CASH_BUFFER_BPS):
    """Compute whole-share long-only targets from information at decision close."""
    if not np.isfinite(aum) or aum <= 0:
        raise ValueError("AUM must be finite and strictly positive.")
    if not 0 <= cash_buffer_bps < 10000:
        raise ValueError("cash_buffer_bps must be in [0, 10000).")
    asof = pd.Timestamp(decision_date).normalize()
    w = snapshot_weights.copy()
    required = {"date", "permno", "ticker", "cap_weight_proxy"}
    if not required.issubset(w.columns):
        raise ValueError(f"Missing weight columns: {sorted(required-set(w.columns))}")
    w["date"] = pd.to_datetime(w["date"]).dt.normalize()
    if not w["date"].eq(asof).all():
        raise ValueError("Weights must be observed on the decision date, not a future date.")
    if w["permno"].isna().any() or w["permno"].duplicated().any():
        raise ValueError("PERMNO identifiers must be present and unique.")
    w["cap_weight_proxy"] = pd.to_numeric(w["cap_weight_proxy"], errors="coerce")
    if (not np.isfinite(w["cap_weight_proxy"].to_numpy(dtype=float)).all()
        or (w["cap_weight_proxy"] < 0).any()
        or not np.isclose(w["cap_weight_proxy"].sum(), 1.0, atol=1e-8)):
        raise ValueError("Weights must be finite, nonnegative, and sum to one.")
    q = _unique_positive_quotes(close_quotes, w["permno"], asof)
    merged = w[["permno", "ticker", "cap_weight_proxy"]].merge(
        q[["permno", "close_price"]], on="permno", how="left", validate="one_to_one"
    )
    investable = aum * (1.0 - cash_buffer_bps / 10000.0)
    merged["target_weight"] = merged["cap_weight_proxy"]
    merged["target_dollars"] = investable * merged["target_weight"]
    merged["decision_price"] = merged["close_price"]
    merged["target_shares"] = np.floor(
        merged["target_dollars"] / merged["decision_price"]
    ).astype("int64")
    merged["planned_dollars"] = merged["target_shares"] * merged["decision_price"]
    merged["rounding_cash"] = merged["target_dollars"] - merged["planned_dollars"]
    merged.insert(0, "decision_date", asof)
    return merged[["decision_date", "permno", "ticker", "target_weight",
                   "decision_price", "target_dollars", "target_shares",
                   "planned_dollars", "rounding_cash"]].sort_values("permno").reset_index(drop=True)


def empty_fund_positions():
    return pd.DataFrame({"permno": pd.Series(dtype="int64"),
                         "ticker": pd.Series(dtype="object"),
                         "shares": pd.Series(dtype="int64")})

def generate_rebalance_orders(targets, current_holdings, decision_quotes,
                              decision_date, reason="QUARTERLY_REBALANCE"):
    """Generate integer-share buys/sells, keyed by PERMNO, including universe exits."""
    asof = pd.Timestamp(decision_date).normalize()
    if "decision_date" in targets.columns and not pd.to_datetime(targets["decision_date"]).dt.normalize().eq(asof).all():
        raise ValueError("Target portfolio was computed on a different decision date.")
    t = targets[["permno", "ticker", "target_shares"]].copy()
    h = current_holdings[["permno", "ticker", "shares"]].copy()
    if t["permno"].duplicated().any() or h["permno"].duplicated().any():
        raise ValueError("Duplicate PERMNO in targets or starting holdings.")
    if t["target_shares"].isna().any() or h["shares"].isna().any():
        raise ValueError("Missing quantities are not allowed.")
    if (t["target_shares"] < 0).any() or (h["shares"] < 0).any():
        raise ValueError("This baseline does not support short positions.")
    universe = set(map(int, t["permno"])) | set(map(int, h["permno"]))
    q = _unique_positive_quotes(decision_quotes, universe, asof, allow_extra=True)
    merged = t.merge(h, on="permno", how="outer", suffixes=("_target", "_current"), validate="one_to_one")
    merged["ticker"] = merged["ticker_target"].combine_first(merged["ticker_current"])
    merged["target_shares"] = merged["target_shares"].fillna(0).astype("int64")
    merged["shares"] = merged["shares"].fillna(0).astype("int64")
    merged["signed_quantity"] = merged["target_shares"] - merged["shares"]
    merged = merged.loc[merged["signed_quantity"].ne(0)].copy()
    if merged.empty:
        return pd.DataFrame(columns=["date", "permno", "ticker", "side", "quantity",
                                     "reason", "decision_price", "current_shares",
                                     "target_shares", "estimated_notional"])
    merged = merged.merge(q[["permno", "close_price"]], on="permno", how="left", validate="one_to_one")
    merged["side"] = np.where(merged["signed_quantity"] > 0, "BUY", "SELL")
    merged["quantity"] = merged["signed_quantity"].abs().astype("int64")
    merged["date"] = asof
    merged["reason"] = reason
    merged["decision_price"] = merged["close_price"]
    merged["estimated_notional"] = merged["quantity"] * merged["decision_price"]
    merged = merged.rename(columns={"shares": "current_shares"})
    return (merged[["date", "permno", "ticker", "side", "quantity", "reason",
                    "decision_price", "current_shares", "target_shares", "estimated_notional"]]
            .sort_values(["side", "permno"]).reset_index(drop=True))

def execute_at_next_close(orders, current_holdings, starting_cash, close_quotes,
                          execution_date, illustrative_fee_bps=0.0,
                          require_trade_close=True):
    """Replay hypothetical closing-price fills with cash and inventory constraints."""
    date = pd.Timestamp(execution_date).normalize()
    if not np.isfinite(starting_cash) or starting_cash < 0:
        raise ValueError("Starting cash must be finite and nonnegative.")
    if not np.isfinite(illustrative_fee_bps) or illustrative_fee_bps < 0:
        raise ValueError("Illustrative fee rate must be finite and nonnegative.")
    h = current_holdings.copy()
    if h["permno"].duplicated().any() or (h["shares"] < 0).any():
        raise ValueError("Bad starting holdings.")
    positions = {int(r.permno): int(r.shares) for r in h.itertuples()}
    tickers = {int(r.permno): r.ticker for r in h.itertuples()}
    o = orders.copy()
    if o.empty:
        raise ValueError("No orders to execute.")
    if o["permno"].duplicated().any() or (~o["side"].isin(["BUY", "SELL"])).any():
        raise ValueError("Exactly one valid BUY/SELL order per PERMNO is supported.")
    if (o["quantity"] <= 0).any() or o["quantity"].isna().any():
        raise ValueError("Order quantities must be positive integers.")
    if not pd.to_datetime(o["date"]).dt.normalize().lt(date).all():
        raise ValueError("Every order must be decided before its execution day.")
    q = _unique_positive_quotes(close_quotes, o["permno"], date, allow_extra=True)
    if require_trade_close and "price_flag" in q.columns:
        bad = q["price_flag"].astype(str).str.upper().ne("TR")
        if bad.any():
            raise ValueError(f"Non-trade CRSP close flags for PERMNOs: {q.loc[bad, 'permno'].tolist()[:15]}. Resolve quote policy; do not silently fill bid/ask estimates.")
    o = o.merge(q[["permno", "close_price"]], on="permno", validate="one_to_one")
    o = o.rename(columns={"close_price": "execution_price"})
    factor = illustrative_fee_bps / 10000.0
    cash = float(starting_cash)
    records = []

    def record_fill(r, quantity):
        notional = float(quantity) * float(r.execution_price)
        fee = notional * factor
        signed_slippage = float(quantity) * (float(r.execution_price)-float(r.decision_price)) * (1 if r.side == "BUY" else -1)
        records.append({
            "decision_date": pd.Timestamp(r.date), "execution_date": date,
            "permno": int(r.permno), "ticker": r.ticker, "reason": r.reason,
            "side": r.side, "requested_quantity": int(r.quantity),
            "filled_quantity": int(quantity), "unfilled_quantity": int(r.quantity)-int(quantity),
            "decision_price": float(r.decision_price), "execution_price": float(r.execution_price),
            "filled_notional": notional, "illustrative_fee_dollars": fee,
            "slippage_dollars_executed_only": signed_slippage,
        })
        return notional, fee

    for r in o.loc[o["side"].eq("SELL")].sort_values("permno").itertuples():
        if positions.get(int(r.permno), 0) < int(r.quantity):
            raise ValueError(f"Cannot sell more than held: PERMNO {r.permno}")
        notional, fee = record_fill(r, int(r.quantity))
        cash += notional - fee
        positions[int(r.permno)] -= int(r.quantity)
        tickers[int(r.permno)] = r.ticker

    buys = o.loc[o["side"].eq("BUY")].sort_values("permno").copy()
    required_cash = (buys["quantity"] * buys["execution_price"] * (1 + factor)).sum()
    fraction = min(1.0, max(0.0, cash / required_cash)) if required_cash > 0 else 1.0
    if not buys.empty:
        buys["fill_quantity"] = np.floor(buys["quantity"] * fraction + 1e-10).astype("int64")
    for r in buys.itertuples():
        qty = int(r.fill_quantity)
        notional, fee = record_fill(r, qty)
        if (notional + fee) > cash + 1e-6:
            raise RuntimeError("Cash constraint violated by buy allocation.")
        cash -= notional + fee
        positions[int(r.permno)] = positions.get(int(r.permno), 0) + qty
        tickers[int(r.permno)] = r.ticker
    if cash < -1e-6:
        raise RuntimeError("Negative cash after execution.")
    new_holdings = pd.DataFrame([
        {"permno": p, "ticker": tickers[p], "shares": qty}
        for p, qty in sorted(positions.items()) if qty > 0
    ], columns=["permno", "ticker", "shares"])
    new_holdings["shares"] = new_holdings["shares"].astype("int64")
    fills = pd.DataFrame(records).sort_values(["side", "permno"]).reset_index(drop=True)
    return fills, new_holdings, float(max(0.0, cash))


def value_portfolio_at_close(holdings, cash, close_quotes, valuation_date, targets=None):
    """Mark the actual post-trade share ledger to this day's observed close."""
    d = pd.Timestamp(valuation_date).normalize()
    h = holdings.copy()
    if targets is not None:
        if targets["permno"].duplicated().any():
            raise ValueError("Duplicate PERMNO in target weights.")
        # Include target names with zero executed shares, so tracking gaps stay visible.
        missing_targets = targets.loc[~targets["permno"].isin(h["permno"]), ["permno", "ticker"]].copy()
        missing_targets["shares"] = 0
        h = pd.concat([h, missing_targets], ignore_index=True)
    if h.empty:
        return h.copy(), float(cash)
    q = _unique_positive_quotes(close_quotes, h["permno"], d, allow_extra=True)
    v = h.merge(q[["permno", "close_price"]], on="permno", validate="one_to_one")
    v["market_value"] = v["shares"] * v["close_price"]
    nav = float(v["market_value"].sum() + cash)
    if nav <= 0:
        raise ValueError("NAV must be strictly positive.")
    v["actual_weight"] = v["market_value"] / nav
    if targets is not None:
        v = v.merge(targets[["permno", "target_weight"]], on="permno", how="left", validate="one_to_one")
        v["target_weight"] = v["target_weight"].fillna(0)
        v["weight_difference_bps"] = 10000 * (v["actual_weight"] - v["target_weight"])
    return v.sort_values("market_value", ascending=False).reset_index(drop=True), nav


def summarize_proxy_execution(fills, portfolio_nav, final_cash):
    filled = fills["filled_quantity"] > 0
    ref_notional = (fills.loc[filled, "filled_quantity"] * fills.loc[filled, "decision_price"]).sum()
    total_slippage = float(fills["slippage_dollars_executed_only"].sum())
    fees = float(fills["illustrative_fee_dollars"].sum())
    return {
        "nav_at_execution_close": float(portfolio_nav),
        "ending_cash": float(final_cash),
        "ending_cash_pct_nav": 100.0 * final_cash / portfolio_nav,
        "n_orders": int(len(fills)),
        "n_partially_or_unfilled": int((fills["unfilled_quantity"] > 0).sum()),
        "filled_dollars": float(fills["filled_notional"].sum()),
        "executed_only_reference_to_close_slippage_dollars": total_slippage,
        "executed_only_reference_to_close_slippage_bps":
            (10000 * total_slippage / ref_notional) if ref_notional else np.nan,
        "illustrative_fees_dollars": fees,
        "is_real_tca": False,
    }

from collections import Counter

RUN_LIVE_WRDS = True
RUN_BLOOMBERG_IMPORT = True
BLOOMBERG_EXPORT_CSV = "data/BMLL_Bloomberg_Corporate_Actions.csv"
CA_START_DATE = "2022-01-01"
CA_END_DATE = "2026-06-30"  # choose an end date covered by your WRDS release
CA_QUERY_BATCH_SIZE = 150
CA_SAMPLE_MAX_SECURITIES = 15       # small first validation; full history later
CA_STOCK_SCHEMAS = ["crsp_q_stock", "crsp_a_stock", "crsp"]
CA_TABLE_NAMES = ["stkdistributions", "stkdelists", "stkdlycumulativeadjfactor", "stksecurityinfohist"]
CA_FIELDS = {
    "stkdistributions": ["permno", "disexdt", "disseqnbr", "distype", "disfreqtype", "dispaymenttype", "disdetailtype", "disorigcurtype", "disdivamt", "disfacpr", "disfacshr", "disdeclaredt", "disrecorddt", "dispaydt", "dispermno", "dispermco"],
    "stkdelists": ["permno", "delistingdt", "delactiontype", "delreasontype", "delstatustype", "delret", "deldivamt", "delnextdt", "delpermno", "delpermco"],
    "stkdlycumulativeadjfactor": ["permno", "dlycaldt", "dlycumfacpr", "dlycumfacshr"],
    "stksecurityinfohist": ["permno", "secinfostartdt", "secinfoenddt", "ticker", "cusip", "cusip9", "hdrcusip", "securitynm"],
}
CA_DATE_FIELD = {"stkdistributions": "disexdt", "stkdelists": "delistingdt", "stkdlycumulativeadjfactor": "dlycaldt"}
CA_REQUIRED = {
    "stkdistributions": {"permno", "disexdt", "distype"},
    "stkdelists": {"permno", "delistingdt"},
    "stkdlycumulativeadjfactor": {"permno", "dlycaldt", "dlycumfacshr"},
    "stksecurityinfohist": {"permno", "secinfostartdt", "secinfoenddt"},
}

CA_CANONICAL_COLUMNS = [
    "source", "source_event_id", "permno", "event_type", "ex_date", "pay_date",
    "announce_date", "record_date", "cash_per_share", "split_ratio", "currency",
    "raw_ticker", "raw_cusip", "new_permno", "source_detail", "requires_review", "review_reason"
]

def empty_ca_frame():
    return pd.DataFrame(columns=CA_CANONICAL_COLUMNS)


def discover_ca_tables(db, schemas=CA_STOCK_SCHEMAS):
    """Inspect actual WRDS entitlements and column names; never guess a schema."""
    rows, locations = [], {}
    for schema in schemas:
        try:
            available = {t.lower() for t in db.list_tables(library=schema)}
        except Exception as exc:
            rows.append({"schema": schema, "table": "(library)", "available": False,
                         "missing_required": "not accessible", "note": str(exc)[:120]})
            continue
        for table in CA_TABLE_NAMES:
            if table not in available:
                rows.append({"schema": schema, "table": table, "available": False,
                             "missing_required": "table missing", "note": ""})
                continue
            try:
                observed = set(db.describe_table(library=schema, table=table)["name"].str.lower())
                missing = sorted(CA_REQUIRED[table] - observed)
                rows.append({"schema": schema, "table": table, "available": not missing,
                             "missing_required": ",".join(missing),
                             "note": f"{len(observed)} columns"})
                if not missing and table not in locations:
                    locations[table] = {"schema": schema, "table": table, "columns": observed}
            except Exception as exc:
                rows.append({"schema": schema, "table": table, "available": False,
                             "missing_required": "describe failed", "note": str(exc)[:120]})
    return pd.DataFrame(rows), locations


def sample_security_universe(max_securities=CA_SAMPLE_MAX_SECURITIES):
    """First demo from existing snapshot. Replace with union of all historical holdings before backtest."""
    for variable in ("weights", "decision_weights", "target_holdings"):
        frame = globals().get(variable)
        if isinstance(frame, pd.DataFrame) and "permno" in frame.columns and len(frame):
            return sorted(frame["permno"].dropna().astype(int).unique().tolist())[:max_securities]
    raise ValueError("First run WRDS weight cells, or set CA_PERMNOS from an approved historical universe.")

# OPTIONAL override for full historical former + current constituent universe:
CA_PERMNOS = None  # e.g., sorted(quarterly_weights['permno'].unique().tolist())

def _ca_sql_chunks(values, chunk_size=CA_QUERY_BATCH_SIZE):
    vals = sorted({int(p) for p in values if pd.notna(p)})
    if not vals:
        raise ValueError("PERMNO universe cannot be empty.")
    if chunk_size <= 0 or chunk_size > 500:
        raise ValueError("Batch size must be 1–500.")
    for i in range(0, len(vals), chunk_size):
        yield vals[i:i + chunk_size]


def fetch_wrds_ca_table(db, discovered, table, permnos, start_date, end_date):
    """Fetch one table, bounded by security universe and date window."""
    if table not in CA_TABLE_NAMES or table not in discovered:
        raise ValueError(f"Table {table} unavailable. Run discover_ca_tables() and inspect its report.")
    meta = discovered[table]
    schema, observed = meta["schema"], meta["columns"]
    if schema not in CA_STOCK_SCHEMAS or table not in CA_TABLE_NAMES:
        raise ValueError("Unexpected schema/table, refusing SQL interpolation.")
    begin, finish = pd.Timestamp(start_date).normalize(), pd.Timestamp(end_date).normalize()
    if pd.isna(begin) or pd.isna(finish) or begin > finish:
        raise ValueError("Invalid corporate-action date window.")
    cols = [c for c in CA_FIELDS[table] if c in observed]
    missing = CA_REQUIRED[table] - set(cols)
    if missing:
        raise ValueError(f"Missing required {table} columns: {sorted(missing)}")
    # Aliases stay as native CRSP column names; missing optional fields added as nulls later.
    parts = []
    for securities in _ca_sql_chunks(permnos):
        args = {f"p_{i}": p for i, p in enumerate(securities)}
        args.update({"begin": begin.date(), "finish": finish.date()})
        placeholders = ",".join(f"%(p_{i})s" for i in range(len(securities)))
        if table == "stksecurityinfohist":
            window = "secinfostartdt <= %(finish)s AND (secinfoenddt IS NULL OR secinfoenddt >= %(begin)s)"
        else:
            window = f"{CA_DATE_FIELD[table]} BETWEEN %(begin)s AND %(finish)s"
        sql = (f"SELECT {', '.join(cols)} FROM {schema}.{table} "
               f"WHERE permno IN ({placeholders}) AND {window}")
        parts.append(db.raw_sql(sql, params=args, date_cols=[c for c in cols if c.endswith("dt")]))
    result = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)
    for field in CA_FIELDS[table]:
        if field not in result:
            result[field] = pd.NA
    result.attrs["wrds_table"] = f"{schema}.{table}"
    result.attrs["start_date"] = str(begin.date())
    result.attrs["end_date"] = str(finish.date())
    return result


def fetch_wrds_ca_bundle(db, permnos, start_date, end_date):
    report, discovered = discover_ca_tables(db)
    results = {}
    for table in CA_TABLE_NAMES:
        if table not in discovered:
            print(f"UNAVAILABLE: {table}. Keep as a coverage gap; do not substitute guessed columns.")
            results[table] = pd.DataFrame(columns=CA_FIELDS[table])
            continue
        results[table] = fetch_wrds_ca_table(db, discovered, table, permnos, start_date, end_date)
        print(f"{table}: {len(results[table]):,} rows from {discovered[table]['schema']}")
    return report, results

CA_EVENT_TYPES = {
    "CD": "CASH_DIVIDEND", "FRS": "STOCK_SPLIT", "SD": "SPECIAL_CASH_DIVIDEND",
    "ROC": "RETURN_OF_CAPITAL", "SP": "SECURITY_DISTRIBUTION", "CP": "CAPITAL_PAYMENT",
    "CG": "CAPITAL_GAINS"
}


def _ca_ts(x):
    return pd.NaT if pd.isna(x) or str(x).strip() == "" else pd.Timestamp(x).normalize()


def _ca_num(x):
    return float(x) if pd.notna(x) and str(x).strip() != "" else np.nan


def normalize_wrds_distributions(raw):
    rows = []
    for index, r in raw.reset_index(drop=True).iterrows():
        p = int(r["permno"])
        d = _ca_ts(r["disexdt"])
        typ = str(r.get("distype", "")).strip().upper()
        event_type = CA_EVENT_TYPES.get(typ, "OTHER")
        if typ == "FRS" and str(r.get("disdetailtype", "")).strip().upper() == "STKDIV":
            event_type = "STOCK_DIVIDEND"
        fac = _ca_num(r.get("disfacshr", np.nan))
        cash = _ca_num(r.get("disdivamt", np.nan)) if typ in {"CD", "SD", "ROC", "CP", "CG"} else np.nan
        # disfacshr is incremental (2:1 split often reports 1.0); multiplier is 1+disfacshr.
        split_ratio = (1.0 + fac) if event_type in {"STOCK_SPLIT", "STOCK_DIVIDEND"} and np.isfinite(fac) and fac > -1 else np.nan
        pay = _ca_ts(r.get("dispaydt", pd.NaT))
        issues = []
        if event_type not in {"CASH_DIVIDEND", "STOCK_SPLIT"}: issues.append("unsupported_or_review_event_type")
        if event_type in {"CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND"} and (not np.isfinite(cash) or cash < 0): issues.append("missing_cash_amount")
        if event_type in {"CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND"} and (pd.isna(pay) or (pd.notna(d) and pay < d)): issues.append("invalid_or_missing_pay_date")
        if event_type in {"STOCK_SPLIT", "STOCK_DIVIDEND"} and (not np.isfinite(split_ratio) or split_ratio <= 0):
            issues.append("missing_share_factor")
        if pd.isna(d): issues.append("missing_ex_date")
        seq = r.get("disseqnbr", pd.NA)
        seqtext = str(int(seq)) if pd.notna(seq) and str(seq).replace(".", "", 1).isdigit() else str(index)
        rows.append({
            "source": "WRDS_CRSP", "source_event_id": f"CRSP:DIST:{p}:{str(d.date()) if pd.notna(d) else 'NA'}:{seqtext}",
            "permno": p, "event_type": event_type, "ex_date": d, "pay_date": pay,
            "announce_date": _ca_ts(r.get("disdeclaredt", pd.NaT)), "record_date": _ca_ts(r.get("disrecorddt", pd.NaT)),
            "cash_per_share": cash, "split_ratio": split_ratio,
            "currency": str(r.get("disorigcurtype", "")).strip().upper() if pd.notna(r.get("disorigcurtype", pd.NA)) else pd.NA,
            "raw_ticker": pd.NA, "raw_cusip": pd.NA,
            "new_permno": r.get("dispermno", pd.NA), "source_detail": str(typ) + ":" + str(r.get("disdetailtype", "")),
            "requires_review": bool(issues), "review_reason": ";".join(issues),
        })
    return pd.DataFrame(rows, columns=CA_CANONICAL_COLUMNS)


def normalize_wrds_delists(raw):
    rows = []
    for _, r in raw.iterrows():
        p, d = int(r["permno"]), _ca_ts(r["delistingdt"])
        rows.append({
            "source": "WRDS_CRSP", "source_event_id": f"CRSP:DELIST:{p}:{d.date() if pd.notna(d) else 'NA'}",
            "permno": p, "event_type": "DELISTING", "ex_date": d, "pay_date": pd.NaT,
            "announce_date": pd.NaT, "record_date": pd.NaT,
            "cash_per_share": np.nan, "split_ratio": np.nan, "currency": pd.NA,
            "raw_ticker": pd.NA, "raw_cusip": pd.NA, "new_permno": r.get("delpermno", pd.NA),
            "source_detail": f"action={r.get('delactiontype', '')};reason={r.get('delreasontype', '')};delret={r.get('delret', '')}",
            "requires_review": True, "review_reason": "delisting_requires_specific_payoff_and_position_handling"
        })
    return pd.DataFrame(rows, columns=CA_CANONICAL_COLUMNS)


def normalize_crsp_events(raw_distributions, raw_delists):
    events = pd.concat([normalize_wrds_distributions(raw_distributions), normalize_wrds_delists(raw_delists)], ignore_index=True)
    if events["source_event_id"].duplicated().any():
        raise ValueError("Duplicate CRSP event IDs; inspect distribution sequences and delist rows.")
    return events.sort_values(["ex_date", "permno", "event_type"]).reset_index(drop=True)

BLOOMBERG_COLUMNS = ["event_id", "permno", "ticker", "cusip", "event_type", "ex_date", "pay_date", "announce_date", "record_date", "cash_per_share", "split_ratio", "currency", "notes"]
BLOOMBERG_EVENT_MAP = {
    "SPECIAL DIVIDEND": "SPECIAL_CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND": "SPECIAL_CASH_DIVIDEND",
    "DIVIDEND": "CASH_DIVIDEND", "CASH DIVIDEND": "CASH_DIVIDEND", "CASH_DIVIDEND": "CASH_DIVIDEND",
    "SPLIT": "STOCK_SPLIT", "STOCK SPLIT": "STOCK_SPLIT", "STOCK_SPLIT": "STOCK_SPLIT",
    "STOCK DIVIDEND": "STOCK_DIVIDEND", "STOCK_DIVIDEND": "STOCK_DIVIDEND",
    "DELIST": "DELISTING", "DELISTING": "DELISTING", "MERGER": "MERGER",
    "SPINOFF": "SPINOFF", "SPIN-OFF": "SPINOFF", "RIGHTS": "RIGHTS",
    "RETURN OF CAPITAL": "RETURN_OF_CAPITAL", "RETURN_OF_CAPITAL": "RETURN_OF_CAPITAL",
}


def export_bloomberg_csv_template(path="bloomberg_corporate_actions_template.csv"):
    pd.DataFrame(columns=BLOOMBERG_COLUMNS).to_csv(path, index=False)
    return str(Path(path).resolve())


def _canonical_cusip(value):
    if pd.isna(value): return ""
    s = "".join(c for c in str(value).upper() if c.isalnum())
    return s[:8] if len(s) in {8, 9} else ""


def _canonical_ticker(value):
    if pd.isna(value): return ""
    return str(value).strip().upper().split(" ")[0].replace("/", ".")


def _resolve_bloomberg_permno(bloomberg_row, id_history):
    """Return (permno or NA, reason). NEVER auto-resolve ambiguous mapping."""
    explicit = bloomberg_row.get("permno", pd.NA)
    if pd.notna(explicit) and str(explicit).strip():
        return int(explicit), "explicit_permno_unverified"
    d = bloomberg_row.get("ex_date", pd.NaT)
    if pd.isna(d): return pd.NA, "missing_ex_date"
    if id_history.empty: return pd.NA, "missing_historical_id_mapping"
    h = id_history.copy()
    for col in ["secinfostartdt", "secinfoenddt"]:
        if col not in h: h[col] = pd.NaT
        h[col] = pd.to_datetime(h[col], errors="coerce")
    h = h[(h["secinfostartdt"].isna() | (h["secinfostartdt"] <= d)) &
          (h["secinfoenddt"].isna() | (h["secinfoenddt"] >= d))].copy()
    cusip = _canonical_cusip(bloomberg_row.get("cusip", pd.NA))
    if cusip:
        ids = set()
        for col in ["cusip", "cusip9"]:
            if col in h:
                ids |= set(h.loc[h[col].apply(_canonical_cusip).eq(cusip), "permno"].dropna().astype(int))
        if len(ids) == 1: return next(iter(ids)), "historical_cusip"
        if len(ids) > 1: return pd.NA, "ambiguous_historical_cusip"
    ticker = _canonical_ticker(bloomberg_row.get("ticker", pd.NA))
    if ticker and "ticker" in h:
        ids = set(h.loc[h["ticker"].apply(_canonical_ticker).eq(ticker), "permno"].dropna().astype(int))
        if len(ids) == 1: return next(iter(ids)), "ticker_only_review"
        if len(ids) > 1: return pd.NA, "ambiguous_historical_ticker"
    return pd.NA, "unmatched_security"


def normalize_bloomberg_export(bloomberg_frame, id_history):
    raw = bloomberg_frame.copy()
    missing = {"event_type", "ex_date"} - set(raw.columns)
    if missing: raise ValueError(f"Bloomberg export missing: {sorted(missing)}")
    if not ({"permno", "cusip", "ticker"} & set(raw.columns)):
        raise ValueError("Bloomberg export needs one of permno / cusip / ticker.")
    for col in BLOOMBERG_COLUMNS:
        if col not in raw: raw[col] = pd.NA
    rows = []
    for n, r in raw.iterrows():
        event_type = BLOOMBERG_EVENT_MAP.get(str(r["event_type"]).strip().upper().replace("-", "-"), "OTHER")
        ex_date = _ca_ts(r["ex_date"])
        permno, mapping_reason = _resolve_bloomberg_permno({**r.to_dict(), "ex_date": ex_date}, id_history)
        issues = []
        if mapping_reason != "historical_cusip" and mapping_reason != "explicit_permno_unverified":
            issues.append(mapping_reason)
        if mapping_reason == "explicit_permno_unverified": issues.append("check_explicit_permno")
        if event_type not in {"CASH_DIVIDEND", "STOCK_SPLIT"}: issues.append("unsupported_or_review_event_type")
        cash, ratio = _ca_num(r["cash_per_share"]), _ca_num(r["split_ratio"])
        if event_type in {"CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND"} and (not np.isfinite(cash) or cash < 0): issues.append("missing_cash_amount")
        pay_date = _ca_ts(r["pay_date"])
        if event_type in {"CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND"} and (pd.isna(pay_date) or pd.notna(ex_date) and pay_date < ex_date):
            issues.append("invalid_or_missing_pay_date")
        if event_type == "STOCK_SPLIT" and (not np.isfinite(ratio) or ratio <= 0): issues.append("missing_split_ratio")
        if pd.isna(ex_date): issues.append("missing_ex_date")
        source_id = str(r["event_id"]) if pd.notna(r["event_id"]) and str(r["event_id"]).strip() else f"BLOOMBERG:ROW:{n}"
        rows.append({
            "source": "BLOOMBERG", "source_event_id": source_id, "permno": permno,
            "event_type": event_type, "ex_date": ex_date, "pay_date": pay_date,
            "announce_date": _ca_ts(r["announce_date"]), "record_date": _ca_ts(r["record_date"]),
            "cash_per_share": cash, "split_ratio": ratio, "currency": r["currency"],
            "raw_ticker": r["ticker"], "raw_cusip": r["cusip"], "new_permno": pd.NA,
            "source_detail": str(r["notes"]) if pd.notna(r["notes"]) else "",
            "requires_review": bool(issues), "review_reason": ";".join(issues),
        })
    result = pd.DataFrame(rows, columns=CA_CANONICAL_COLUMNS)
    if result["source_event_id"].duplicated().any():
        raise ValueError("Bloomberg event IDs must be unique; review duplicated rows.")
    return result

def _ca_differs(a, b, atol=1e-6):
    if pd.isna(a) and pd.isna(b): return False
    if pd.isna(a) != pd.isna(b): return True
    return not np.isclose(float(a), float(b), rtol=0, atol=atol)


def _ca_dates_differ(a, b):
    if pd.isna(a) and pd.isna(b): return False
    if pd.isna(a) != pd.isna(b): return True
    return pd.Timestamp(a).normalize() != pd.Timestamp(b).normalize()


def reconcile_corporate_actions(wrds_events, bloomberg_events, cash_tol=0.005, ratio_tol=1e-6):
    """Outer reconciliation; one row per event or unique cross-source pair."""
    a, b = wrds_events.reset_index(drop=True).copy(), bloomberg_events.reset_index(drop=True).copy()
    for df in (a, b):
        for col in CA_CANONICAL_COLUMNS:
            if col not in df: df[col] = pd.NA
        df["ex_date"] = pd.to_datetime(df["ex_date"], errors="coerce").dt.normalize()
        df["permno"] = pd.to_numeric(df["permno"], errors="coerce").astype("Int64")
    key = ["permno", "event_type", "ex_date"]
    def as_key(row):
        return tuple(row[c] for c in key) if pd.notna(row["permno"]) and pd.notna(row["ex_date"]) else None
    akw = [as_key(r) for _, r in a.iterrows()]
    bkw = [as_key(r) for _, r in b.iterrows()]
    ca, cb = Counter(k for k in akw if k is not None), Counter(k for k in bkw if k is not None)
    taken = set()
    output = []
    for i, ra in a.iterrows():
        k = akw[i]
        choices = [j for j, kb in enumerate(bkw) if kb == k] if k is not None else []
        unique = bool(k is not None and ca[k] == cb[k] == 1 and len(choices) == 1)
        j = choices[0] if unique else None
        if j is not None:
            taken.add(j)
            rb = b.iloc[j]
            issues = [str(x) for x in [ra["review_reason"], rb["review_reason"]] if pd.notna(x) and str(x).strip()]
            et = ra["event_type"]
            if et in {"CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND"} and _ca_differs(ra["cash_per_share"], rb["cash_per_share"], cash_tol):
                issues.append("cash_amount_conflict")
            if et == "STOCK_SPLIT" and _ca_differs(ra["split_ratio"], rb["split_ratio"], ratio_tol):
                issues.append("split_ratio_conflict")
            if _ca_dates_differ(ra["pay_date"], rb["pay_date"]): issues.append("pay_date_conflict")
            if pd.notna(ra["currency"]) and pd.notna(rb["currency"]) and str(ra["currency"]).upper() != str(rb["currency"]).upper():
                issues.append("currency_conflict")
            row = {"match_status": "MATCHED_CONFLICT" if issues else "MATCHED_AGREE",
                   "wrds_event_id": ra["source_event_id"], "bloomberg_event_id": rb["source_event_id"],
                   "permno": ra["permno"], "event_type": et, "ex_date": ra["ex_date"],
                   "wrds_cash_per_share": ra["cash_per_share"], "bloomberg_cash_per_share": rb["cash_per_share"],
                   "wrds_split_ratio": ra["split_ratio"], "bloomberg_split_ratio": rb["split_ratio"],
                   "wrds_pay_date": ra["pay_date"], "bloomberg_pay_date": rb["pay_date"],
                   "review_reason": ";".join(dict.fromkeys(";".join(issues).split(";"))) if issues else "",
                   "requires_review": bool(issues), "review_status": "PENDING"}
        else:
            ambiguous = k is not None and (ca[k] > 1 or cb[k] > 1)
            reason = "ambiguous_same_day_events" if ambiguous else "wrds_only_not_cross_checked"
            if pd.notna(ra["review_reason"]) and str(ra["review_reason"]).strip(): reason += ";" + str(ra["review_reason"])
            row = {"match_status": "AMBIGUOUS" if ambiguous else "WRDS_ONLY", "wrds_event_id": ra["source_event_id"],
                   "bloomberg_event_id": pd.NA, "permno": ra["permno"], "event_type": ra["event_type"],
                   "ex_date": ra["ex_date"], "wrds_cash_per_share": ra["cash_per_share"],
                   "bloomberg_cash_per_share": np.nan, "wrds_split_ratio": ra["split_ratio"],
                   "bloomberg_split_ratio": np.nan, "wrds_pay_date": ra["pay_date"],
                   "bloomberg_pay_date": pd.NaT, "review_reason": reason, "requires_review": True, "review_status": "PENDING"}
        output.append(row)
    for j, rb in b.iterrows():
        if j in taken: continue
        k = bkw[j]
        ambiguous = k is not None and (cb[k] > 1 or ca[k] > 1)
        reason = "ambiguous_same_day_events" if ambiguous else "bloomberg_only_not_cross_checked"
        if pd.notna(rb["review_reason"]) and str(rb["review_reason"]).strip(): reason += ";" + str(rb["review_reason"])
        output.append({
            "match_status": "AMBIGUOUS" if ambiguous else "BLOOMBERG_ONLY",
            "wrds_event_id": pd.NA, "bloomberg_event_id": rb["source_event_id"],
            "permno": rb["permno"], "event_type": rb["event_type"], "ex_date": rb["ex_date"],
            "wrds_cash_per_share": np.nan, "bloomberg_cash_per_share": rb["cash_per_share"],
            "wrds_split_ratio": np.nan, "bloomberg_split_ratio": rb["split_ratio"],
            "wrds_pay_date": pd.NaT, "bloomberg_pay_date": rb["pay_date"],
            "review_reason": reason, "requires_review": True, "review_status": "PENDING",
        })
    columns = ["match_status", "wrds_event_id", "bloomberg_event_id", "permno", "event_type", "ex_date",
               "wrds_cash_per_share", "bloomberg_cash_per_share", "wrds_split_ratio", "bloomberg_split_ratio",
               "wrds_pay_date", "bloomberg_pay_date", "review_reason", "requires_review", "review_status"]
    return pd.DataFrame(output, columns=columns).sort_values(["ex_date", "permno"], na_position="last").reset_index(drop=True)


def prepare_review_queue(unified):
    return unified.loc[unified["requires_review"] | unified["review_status"].ne("APPROVED")].copy()


def select_approved_actions(unified, wrds_events, bloomberg_events):
    """Explicit approval is required. Preserve the chosen primary source in the approved record."""
    ready = unified.loc[unified["review_status"].eq("APPROVED")].copy()
    if ready.empty: return empty_ca_frame()
    if ready["requires_review"].any():
        raise ValueError("APPROVED rows still contain conflicts/review flags. Resolve, document, then clear requires_review.")
    source_index = pd.concat([wrds_events, bloomberg_events], ignore_index=True).set_index("source_event_id", drop=False)
    records = []
    for _, r in ready.iterrows():
        chosen_id = r["wrds_event_id"] if pd.notna(r["wrds_event_id"]) else r["bloomberg_event_id"]
        if chosen_id not in source_index.index: raise ValueError("Missing source event " + str(chosen_id))
        event = source_index.loc[chosen_id].copy()
        if isinstance(event, pd.DataFrame): raise ValueError("Nonunique source event ID")
        if event["event_type"] not in {"CASH_DIVIDEND", "STOCK_SPLIT"}:
            raise ValueError(f"Unsupported automatic ledger event: {event['event_type']}")
        event["requires_review"] = False
        event["review_reason"] = ""
        records.append(event.to_dict())
    return pd.DataFrame(records, columns=CA_CANONICAL_COLUMNS)

def empty_ca_receivables():
    return pd.DataFrame(columns=["source_event_id", "permno", "pay_date", "receivable_amount"])


def step_corporate_action_ledger(positions, cash, receivables, approved_events, asof_date):
    """Positions are pre-ex-date integer-share holdings; return (positions, cash, receivables)."""
    day = pd.Timestamp(asof_date).normalize()
    h = positions.copy()
    if not {"permno", "shares"}.issubset(h.columns):
        raise ValueError("Positions need permno and shares.")
    if h["permno"].duplicated().any(): raise ValueError("Duplicate PERMNO positions")
    h["shares"] = pd.to_numeric(h["shares"], errors="raise").astype(float)
    if (h["shares"] < 0).any(): raise ValueError("Long-only ledger cannot start with short positions.")
    cash = float(cash)
    recv = receivables.copy()
    events = approved_events.copy()
    if events.empty: events = empty_ca_frame()
    for datecol in ["ex_date", "pay_date"]:
        events[datecol] = pd.to_datetime(events[datecol], errors="coerce").dt.normalize()
    if events["source_event_id"].duplicated().any(): raise ValueError("Duplicate approved event IDs")
    today = events.loc[events["ex_date"].eq(day)].copy()
    same_security = today.groupby("permno")["event_type"].agg(set)
    if any({"CASH_DIVIDEND", "STOCK_SPLIT"} <= types for types in same_security):
        raise ValueError("Same-day split + dividend: sequencing/entitlement must be manually verified.")
    if today["requires_review"].fillna(True).any():
        raise ValueError("Unverified corporate action passed to ledger.")
    shares = h.set_index("permno")["shares"].to_dict()
    pending = []
    for _, e in today.iterrows():
        p = int(e["permno"])
        n = float(shares.get(p, 0))
        if n <= 0: continue
        if e["event_type"] == "CASH_DIVIDEND":
            amt = _ca_num(e["cash_per_share"])
            paydate = e["pay_date"]
            if not np.isfinite(amt) or amt < 0 or pd.isna(paydate) or paydate < day:
                raise ValueError("Invalid dividend amount/payment date; cannot create entitlement.")
            if e["source_event_id"] in set(recv.get("source_event_id", pd.Series(dtype=object)).values):
                raise ValueError("Dividend already accrued; do not replay an event twice")
            pending.append({"source_event_id": e["source_event_id"], "permno": p,
                            "pay_date": paydate, "receivable_amount": n * amt})
        elif e["event_type"] == "STOCK_SPLIT":
            ratio = _ca_num(e["split_ratio"])
            if not np.isfinite(ratio) or ratio <= 0: raise ValueError("Invalid split ratio")
            updated = n * ratio
            if not np.isclose(updated, round(updated), atol=1e-7):
                raise ValueError(f"Fractional split position for {p}; cash-in-lieu model required.")
            shares[p] = int(round(updated))
        else:
            raise NotImplementedError(f"Corporate action {e['event_type']} requires separate treatment")
    h["shares"] = h["permno"].map(shares).astype("int64")
    recv = pd.concat([recv, pd.DataFrame(pending)], ignore_index=True)
    if not recv.empty:
        recv["pay_date"] = pd.to_datetime(recv["pay_date"]).dt.normalize()
        paytoday = recv["pay_date"].eq(day)
        cash += float(recv.loc[paytoday, "receivable_amount"].sum())
        recv = recv.loc[~paytoday].reset_index(drop=True)
    if cash < -1e-8: raise ValueError("Negative cash after corporate actions")
    return h, cash, recv


def economic_nav(raw_position_values, cash, receivables):
    """Raw share mark-to-market + cash + unpaid dividend receivables."""
    return (float(raw_position_values) + float(cash) +
            (float(receivables["receivable_amount"].sum()) if not receivables.empty else 0.0))

def check_split_factors(events, factor_panel, atol=1e-5):
    """Cross-check event-level share ratio with adjacent daily cumulative factors."""
    columns = ["source_event_id", "permno", "ex_date", "reported_split_ratio", "implied_split_ratio", "status"]
    if factor_panel.empty or events.empty: return pd.DataFrame(columns=columns)
    factors = factor_panel.copy()
    factors["dlycaldt"] = pd.to_datetime(factors["dlycaldt"], errors="coerce").dt.normalize()
    factors["dlycumfacshr"] = pd.to_numeric(factors["dlycumfacshr"], errors="coerce")
    rows = []
    for _, e in events.loc[events["event_type"].eq("STOCK_SPLIT")].iterrows():
        d = pd.Timestamp(e["ex_date"]).normalize()
        series = factors.loc[factors["permno"].eq(e["permno"])].sort_values("dlycaldt")
        earlier = series.loc[series["dlycaldt"] < d]
        later = series.loc[series["dlycaldt"] >= d]
        ratio = np.nan
        if len(earlier) and len(later):
            before, after = earlier["dlycumfacshr"].iat[-1], later["dlycumfacshr"].iat[0]
            if np.isfinite(before) and np.isfinite(after) and after != 0: ratio = before / after
        reported = _ca_num(e["split_ratio"])
        status = ("INSUFFICIENT_FACTOR_COVERAGE" if not np.isfinite(ratio) else
                  "AGREE" if np.isfinite(reported) and np.isclose(reported, ratio, atol=atol, rtol=0) else "REVIEW_FACTOR_MISMATCH")
        rows.append({"source_event_id": e["source_event_id"], "permno": e["permno"],
                     "ex_date": d, "reported_split_ratio": reported,
                     "implied_split_ratio": ratio, "status": status})
    return pd.DataFrame(rows, columns=columns)


def export_ca_outputs(output_dir="bmll_ca_outputs", allowed_by_license=False):
    """Explicit export guard: set allowed_by_license=True only after verifying rights."""
    if not allowed_by_license:
        raise PermissionError("Review WRDS/Bloomberg academic-use and export restrictions first.")
    folder = Path(output_dir)
    folder.mkdir(exist_ok=True, parents=True)
    datasets = {
        "wrds_distributions": wrds_corporate_actions,
        "wrds_delist_events": wrds_delist_events,
        "wrds_daily_adjustment_factors": wrds_adj_factors,
        "wrds_historical_ids": wrds_id_history,
        "bloomberg_events": bloomberg_corporate_actions,
        "unified_events_for_review": unified_corporate_actions,
        "manual_review_queue": corporate_action_review_queue,
    }
    for name, frame in datasets.items():
        frame.to_csv(folder / f"{name}.csv", index=False)
    print("Exported to", folder.resolve())
    return folder

# For a blank Bloomberg template (no proprietary data):
# print(export_bloomberg_csv_template())
# For real exports, verify licenses, then call:
# export_ca_outputs(allowed_by_license=True)
