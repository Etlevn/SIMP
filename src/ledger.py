"""Daily fund ledger simulation."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _dates(values):
    return pd.DatetimeIndex(pd.to_datetime(values)).normalize()


def prepare_exclusion_scenario(weights, events, approval_audit, *, exclude_reverse_splits=True):
    """Construct an explicitly restricted portfolio, not a full-index backtest."""
    reasons = {}
    for row in approval_audit.loc[approval_audit.decision.eq("EXCLUDE")].itertuples():
        if pd.notna(row.permno):
            reasons.setdefault(int(row.permno), set()).add("excluded_corporate_action")
    if exclude_reverse_splits and not events.empty:
        reverse = events.event_type.eq("STOCK_SPLIT") & pd.to_numeric(events.split_ratio).lt(1)
        for p in events.loc[reverse, "permno"]:
            reasons.setdefault(int(p), set()).add("reverse_split_fractional_settlement_not_implemented")
    w = weights.copy()
    if w.duplicated(["date", "permno"]).any():
        raise ValueError("Duplicate snapshot security")
    original_totals = w.groupby("date").cap_weight_proxy.sum()
    if not np.allclose(original_totals, 1, atol=1e-8, rtol=0):
        raise ValueError("Original snapshot weights must sum to one")
    removed = w[w.permno.isin(reasons)].copy()
    removed["exclusion_reason"] = removed.permno.map(lambda p: ";".join(sorted(reasons[int(p)])))
    kept = w[~w.permno.isin(reasons)].copy()
    if set(kept.date) != set(w.date):
        raise ValueError("Exclusions removed an entire snapshot")
    totals = kept.groupby("date").cap_weight_proxy.transform("sum")
    if (totals <= 0).any():
        raise ValueError("No positive investable weights remain")
    kept["original_cap_weight_proxy"] = kept.cap_weight_proxy
    kept["cap_weight_proxy"] = kept.cap_weight_proxy / totals
    if "cap_weight_proxy_pct" in kept:
        kept["cap_weight_proxy_pct"] = kept.cap_weight_proxy * 100
    filtered_events = events.loc[~events.permno.isin(reasons)].copy()
    return kept, filtered_events, removed


def simulate(weights, prices, calendar, events, start, end, *, initial_cash=10e9,
             cash_buffer_bps=10, fee_bps=0, flows=None, rebalance_on_flow=False):
    """All-cash inception, close decisions and next-session close proxy fills."""
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if start > end or not np.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("Invalid dates/initial cash")
    if not 0 <= cash_buffer_bps < 10000 or not 0 <= fee_bps < 10000:
        raise ValueError("Invalid cash buffer/fee")
    w, q, e = weights.copy(), prices.copy(), events.copy()
    w["date"], q["date"] = _dates(w.date), _dates(q.date)
    for frame in (w, q):
        if frame[["date", "permno"]].duplicated().any():
            raise ValueError("Duplicate date/PERMNO")
        if frame.permno.isna().any():
            raise ValueError("Missing PERMNO")
    w = w[w.date.between(start, end)]
    days = _dates(calendar)
    if days.has_duplicates or not days.is_monotonic_increasing:
        raise ValueError("Calendar must be unique and sorted")
    days = days[(days >= start) & (days <= end)]
    if days.empty or w.empty or w.date.min() != days[0] or not w.date.isin(days).all():
        raise ValueError("Start on the first decision snapshot; all decisions need calendar sessions")
    for _, snap in w.groupby("date"):
        vals = pd.to_numeric(snap.cap_weight_proxy, errors="raise").to_numpy(float)
        if not np.isfinite(vals).all() or (vals < 0).any() or not np.isclose(vals.sum(), 1, atol=1e-8, rtol=0):
            raise ValueError("Snapshot weights must be nonnegative and sum to one")
    if not e.empty:
        for col in ("ex_date", "pay_date"):
            e[col] = pd.to_datetime(e[col], errors="raise").dt.normalize()
        if e.source_event_id.duplicated().any() or e.ex_date.isna().any() or e.permno.isna().any():
            raise ValueError("Missing/duplicate action identity or date")
        if not e.event_type.isin(["CASH_DIVIDEND", "STOCK_SPLIT"]).all():
            raise ValueError("Unsupported corporate action")
        if "requires_review" not in e or not e.requires_review.eq(False).all():
            raise ValueError("Unreviewed action")
        cash_events = e.event_type.eq("CASH_DIVIDEND")
        if not e.loc[cash_events, "currency"].astype(str).str.upper().eq("USD").all():
            raise ValueError("Unverified/non-USD currency")
    f = pd.DataFrame(columns=["date", "amount"]) if flows is None else flows.copy()
    f["date"] = _dates(f.date)
    if not f.date.isin(days).all() or f.date.duplicated().any():
        raise ValueError("One flow per trading session; no out-of-window flows")
    if not np.isfinite(pd.to_numeric(f.amount).to_numpy(float)).all():
        raise ValueError("Invalid cash flows")
    flow_map = f.set_index("date").amount.to_dict()
    quotes = {d: x.set_index("permno") for d, x in q.groupby("date")}
    snapshots = {d: x.set_index("permno").cap_weight_proxy for d, x in w.groupby("date")}
    positions, receivables, pending = {}, {}, None
    cash, units = float(initial_cash), float(initial_cash / 100)
    ledger, holdings, fills, actions, orders = [], [], [], [], []
    latest_weights = None

    def price(day, p, *, fill=False):
        if day not in quotes or p not in quotes[day].index:
            raise ValueError(f"Missing raw close {day.date()} PERMNO={p}")
        row = quotes[day].loc[p]
        v = float(row.close_price)
        if not np.isfinite(v) or v <= 0:
            raise ValueError(f"Invalid raw close {day.date()} PERMNO={p}")
        if fill and ("price_flag" not in row or str(row.price_flag).upper() != "TR"):
            raise ValueError("Execution requires an observed TR close")
        return v

    for day in pd.date_range(start, end, freq="D"):
        today = e[e.ex_date.eq(day)] if not e.empty else e
        if not today.empty:
            kinds = today.groupby("permno").event_type.agg(set)
            if any(len(x) > 1 for x in kinds):
                raise ValueError("Same-day split/dividend sequencing requires review")
        for r in today.itertuples():
            p = int(r.permno)
            n = positions.get(p, 0)
            if r.event_type == "STOCK_SPLIT":
                ratio = float(r.split_ratio)
                if not np.isfinite(ratio) or ratio <= 0:
                    raise ValueError("Invalid split multiplier")
                updated = n * ratio
                if not np.isclose(updated, round(updated), atol=1e-7, rtol=0):
                    raise ValueError("Fractional split shares require cash-in-lieu")
                if n:
                    positions[p] = int(round(updated))
                if pending and p in pending["orders"]:
                    order = pending["orders"][p]
                    quantity = order["signed_quantity"] * ratio
                    if not np.isclose(quantity, round(quantity), atol=1e-7, rtol=0):
                        raise ValueError("Fractional pending split order requires a cancellation policy")
                    order["signed_quantity"] = int(round(quantity))
                    order["decision_price"] /= ratio
                actions.append({"date": day, "source_event_id": r.source_event_id,
                                "permno": p, "share_change": updated - n, "accrued": 0., "paid": 0.})
            else:
                amount = float(r.cash_per_share)
                if not np.isfinite(amount) or amount < 0 or pd.isna(r.pay_date) or r.pay_date < day:
                    raise ValueError("Invalid dividend amount/payment date")
                if n:
                    receivables[r.source_event_id] = (r.pay_date, n * amount, p)
                    actions.append({"date": day, "source_event_id": r.source_event_id,
                                    "permno": p, "share_change": 0, "accrued": n * amount, "paid": 0.})
        for event_id, (payday, amount, p) in list(receivables.items()):
            if payday == day:
                cash += amount
                del receivables[event_id]
                actions.append({"date": day, "source_event_id": event_id,
                                "permno": p, "share_change": 0, "accrued": 0., "paid": amount})
        if day not in days:
            continue
        recv = sum(x[1] for x in receivables.values())
        market_value = sum(n * price(day, p) for p, n in positions.items() if n)
        preflow_nav = cash + market_value + recv
        if preflow_nav <= 0:
            raise ValueError("Nonpositive NAV")
        amount = float(flow_map.get(day, 0))
        if cash + amount < -1e-6 or preflow_nav + amount <= 0:
            raise ValueError("Redemption exceeds available cash/NAV; schedule liquidation in advance")
        units += amount / (preflow_nav / units)
        cash += amount
        fees = 0.
        if pending:
            oo = pending["orders"]
            fee_rate = fee_bps / 10000
            needed = sum(v["signed_quantity"] * price(day, p, fill=True) * (1 + fee_rate)
                         for p, v in oo.items() if v["signed_quantity"] > 0)
            sale_cash = sum(-v["signed_quantity"] * price(day, p, fill=True) * (1 - fee_rate)
                            for p, v in oo.items() if v["signed_quantity"] < 0)
            fraction = min(1., (cash + sale_cash) / needed) if needed else 1.
            for p, order in sorted(oo.items(), key=lambda x: (x[1]["signed_quantity"] > 0, x[0])):
                qty = order["signed_quantity"]
                executed = qty if qty < 0 else int(np.floor(qty * fraction + 1e-10))
                px = price(day, p, fill=True)
                fee = abs(executed) * px * fee_rate
                if positions.get(p, 0) + executed < 0:
                    raise ValueError("Sell exceeds inventory")
                positions[p] = positions.get(p, 0) + executed
                cash -= executed * px + fee
                fees += fee
                fills.append({"decision_date": pending["date"], "execution_date": day,
                    "permno": p, "signed_requested": qty, "signed_filled": executed,
                    "unfilled_quantity": abs(qty - executed), "execution_price": px,
                    "decision_price": order["decision_price"], "fee": fee,
                    "executed_reference_drift": executed * (px - order["decision_price"])})
            pending = None
        if cash < -1e-5:
            raise ValueError("Negative cash")
        positions = {p: n for p, n in positions.items() if n}
        cash = max(0., cash)
        market_value = sum(n * price(day, p) for p, n in positions.items())
        nav = cash + market_value + recv
        if not np.isclose(nav, preflow_nav + amount - fees, atol=1e-4, rtol=1e-12):
            raise AssertionError("Trade/cash/NAV conservation failed")
        ledger.append({"date": day, "cash": cash, "equity_value": market_value,
                       "receivables": recv, "nav": nav, "external_flow": amount,
                       "fund_units": units, "unit_nav": nav / units, "fees": fees})
        holdings.extend({"date": day, "permno": p, "shares": n,
                         "close_price": price(day, p)} for p, n in sorted(positions.items()))
        if day in snapshots:
            latest_weights = snapshots[day]
        if day in snapshots or (rebalance_on_flow and amount):
            target = {int(p): int(np.floor((nav - recv) * (1 - cash_buffer_bps / 10000) * wt
                                          / price(day, p))) for p, wt in latest_weights.items()}
            oo = {}
            for p in sorted(set(target) | set(positions)):
                qty = target.get(p, 0) - positions.get(p, 0)
                if qty:
                    oo[p] = {"signed_quantity": qty, "decision_price": price(day, p)}
                    orders.append({"decision_date": day, "permno": p, **oo[p]})
            if oo:
                pending = {"date": day, "orders": oo}
    navs = pd.DataFrame(ledger)
    navs["fund_return"] = navs.unit_nav.pct_change()
    navs.loc[0, "fund_return"] = navs.loc[0, "unit_nav"] / 100 - 1
    return {"daily_nav": navs, "daily_holdings": pd.DataFrame(holdings),
            "fills": pd.DataFrame(fills), "orders": pd.DataFrame(orders),
            "action_journal": pd.DataFrame(actions), "pending_orders": pending,
            "ending_receivables": receivables}


def seeded_flows(calendar, aum=10e9, daily_sigma_bps=1, seed=42):
    """Exogenous dollar-flow scenarios, not a calibrated investor-behaviour model."""
    if daily_sigma_bps < 0 or aum <= 0:
        raise ValueError("Invalid flow assumptions")
    return pd.DataFrame({"date": _dates(calendar), "amount": np.random.default_rng(seed).normal(
        0, aum * daily_sigma_bps / 10000, len(calendar))})


def benchmark_report(navs, benchmark):
    """Compare unitized returns with supplied USD daily total-return observations."""
    b = benchmark.copy()
    b["date"] = _dates(b.date)
    if b.date.duplicated().any() or not navs.date.isin(b.date).all():
        raise ValueError("Benchmark must cover every valuation session uniquely")
    x = navs.merge(b[["date", "benchmark_return"]], on="date", validate="one_to_one")
    if not np.isfinite(x.benchmark_return.to_numpy(float)).all() or (x.benchmark_return <= -1).any():
        raise ValueError("Invalid benchmark returns")
    x["active_return"] = x.fund_return - x.benchmark_return
    return x, {"fund_total_return": float((1 + x.fund_return).prod() - 1),
               "benchmark_total_return": float((1 + x.benchmark_return).prod() - 1),
               "tracking_error_annualized": float(x.active_return.std(ddof=1) * np.sqrt(252))}


def validate_bmll_mapping(mapping, requested):
    """Validate dated mapping for requested date/PERMNO pairs; reject ambiguity."""
    m, r = mapping.copy(), requested[["date", "permno"]].drop_duplicates().copy()
    if not {"permno", "bmll_id", "valid_from", "valid_to"} <= set(m):
        raise ValueError("Missing mapping fields")
    for col in ("valid_from", "valid_to"):
        m[col] = pd.to_datetime(m[col], errors="raise")
    r["date"] = _dates(r.date)
    if m.bmll_id.isna().any() or m.valid_from.isna().any() or (m.valid_to < m.valid_from).any():
        raise ValueError("Invalid execution-vendor identifier/effective interval")
    merged = r.merge(m, on="permno", how="left")
    valid = merged[merged.date.ge(merged.valid_from) &
                   (merged.valid_to.isna() | merged.date.le(merged.valid_to))]
    counts = valid.groupby(["date", "permno"]).size()
    expected = pd.MultiIndex.from_frame(r[["date", "permno"]])
    if not counts.reindex(expected, fill_value=0).eq(1).all():
        raise ValueError("Execution-vendor mapping missing or ambiguous for requested security/date")
    return valid


def execution_comparison(observations):
    """TCA for independently supplied order outcomes and same-unit reference prices."""
    x = observations.copy()
    required = {"order_id", "side", "quantity", "filled_quantity", "average_fill_price",
                "arrival_price", "terminal_price", "fees", "vwap", "twap", "auction_price"}
    if not required <= set(x) or x.order_id.duplicated().any():
        raise ValueError("TCA needs unique orders and the documented columns")
    if not x.side.isin(["BUY", "SELL"]).all():
        raise ValueError("Invalid order side")
    for col in required - {"order_id", "side"}:
        x[col] = pd.to_numeric(x[col], errors="raise")
    for col in ["quantity", "filled_quantity", "arrival_price", "terminal_price", "fees"]:
        if not np.isfinite(x[col]).all():
            raise ValueError(f"Missing {col}")
    if ((x.quantity <= 0) | (x.filled_quantity < 0) | (x.filled_quantity > x.quantity)
        | (x.arrival_price <= 0) | (x.terminal_price <= 0) | (x.fees < 0)).any():
        raise ValueError("Invalid order amounts/prices")
    filled = x.filled_quantity > 0
    if (~np.isfinite(x.loc[filled, "average_fill_price"]) | (x.loc[filled, "average_fill_price"] <= 0)).any():
        raise ValueError("Filled orders require valid fill prices")
    sign = x.side.map({"BUY": 1, "SELL": -1})
    x["executed_cost"] = (sign * x.filled_quantity * (x.average_fill_price - x.arrival_price)).where(filled, 0.)
    x["opportunity_cost"] = sign * (x.quantity - x.filled_quantity) * (x.terminal_price - x.arrival_price)
    x["implementation_shortfall"] = x.executed_cost + x.opportunity_cost + x.fees
    for reference in ("vwap", "twap", "auction_price"):
        valid = np.isfinite(x[reference]) & x[reference].gt(0) & filled
        x[f"{reference}_slippage_bps"] = (10000 * sign * (x.average_fill_price / x[reference] - 1)).where(valid)
    return x
