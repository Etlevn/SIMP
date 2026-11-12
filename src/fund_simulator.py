"""Daily fund ledger with index-rebalance timing and investor flows.

Extends ``ledger.simulate`` (kept for library/tests) with fixed
**reference-close** rebalance timing: orders are decided at the close of the
session *before* the S&P reference date (typically the third Friday) and filled
in that Friday's closing auction — the same print the index rebalances at. The
CRSP close is the auction-price proxy until BMLL closing-auction prices join
the TCA layer.

Target quantities are proportional to *index shares*
(``cap_weight_proxy / reference_close``) and are sized from the decision-day
NAV at decision-day prices. No post-decision price enters an order quantity, so
the only gap to the index at the Friday close is cash, whole-share rounding and
decision-day to Friday NAV drift.

Investor flows: ``flows`` holds one row per *dealing* session with either
``amount`` (USD, + subscription / - redemption) or ``flow_bps`` (of the
notice-day NAV). Notice is received one session earlier. Flows do not trigger a
full rebalance; cash is invested or raised pro rata when projected cash leaves
its band (``flow_band_bps``). On rebalance decision days the known next-day
flow is folded into the rebalance budget.

Everything else (split/dividend ledger, sell-first cash allocation, scaling of
buys when cash is short, reconciliation assertions) follows ``simulate``.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd


def _dates(values):
    return pd.DatetimeIndex(pd.to_datetime(values, errors="raise")).normalize()


def order_id(decision_date, permno, reason):
    """Deterministic 24-hex order id (same width as the handoff blotter)."""
    key = f"{pd.Timestamp(decision_date).date()}|{int(permno)}|{reason}"
    return hashlib.sha1(key.encode()).hexdigest()[:24]


def _calendar_index(calendar):
    cal = _dates(calendar)
    if cal.has_duplicates or not cal.is_monotonic_increasing:
        raise ValueError("Calendar must be unique and sorted")
    return cal


def pad_reference_close_coverage(weights, prices, calendar):
    """Ensure decision + reference sessions exist for ``reference_close``.

    Offline caches often start on the Monday snapshot and omit the preceding
    Thursday/Friday. This inserts those business days into the calendar and
    carry-backs each name's next available close onto the missing sessions
    (typically the Monday print). Prefer a real WRDS pad when available.

    Returns ``(prices, calendar, pad_rows)`` where ``pad_rows`` lists inserted
    ``(date, permno, source_date)`` quotes.
    """
    w = weights.copy()
    w["date"] = _dates(w.date)
    if "reference_date" not in w.columns:
        raise ValueError("reference_close timing needs weights.reference_date")
    w["reference_date"] = _dates(w.reference_date)
    cal = _calendar_index(calendar)
    px = prices.copy()
    px["date"] = _dates(px.date)
    px["permno"] = px.permno.astype(int)

    needed_days = []
    need_permnos: dict[pd.Timestamp, set[int]] = {}
    for snap, part in w.groupby("date", sort=True):
        refs = part.reference_date.unique()
        if len(refs) != 1:
            raise ValueError(f"Snapshot {snap.date()} has several reference dates")
        ref = pd.Timestamp(refs[0]).normalize()
        if ref >= snap:
            raise ValueError("Reference date must precede its post-rebalance snapshot")
        prior = pd.bdate_range(end=ref, periods=2)[0]
        for day in (prior, ref):
            needed_days.append(day)
            need_permnos.setdefault(day, set()).update(part.permno.astype(int))

    needed_days = sorted(set(needed_days))
    missing_cal = [d for d in needed_days if d not in cal]
    if missing_cal:
        cal = pd.DatetimeIndex(sorted(set(cal).union(missing_cal)))

    by_permno = {int(pid): frame.sort_values("date") for pid, frame in px.groupby("permno")}
    existing = set(zip(px.date, px.permno.astype(int)))
    pad_rows = []
    new_quotes = []
    for day in needed_days:
        for pid in sorted(need_permnos.get(day, ())):
            if (day, pid) in existing:
                continue
            series = by_permno.get(pid)
            if series is None or series.empty:
                raise ValueError(
                    f"Cannot pad missing close {day.date()} PERMNO={pid}: no price series")
            later = series.loc[series.date.ge(day)]
            if later.empty:
                raise ValueError(
                    f"Cannot pad missing close {day.date()} PERMNO={pid}: no later quote")
            src = later.iloc[0]
            new_quotes.append({
                "date": day,
                "permno": pid,
                "close_price": float(src.close_price),
                "price_flag": str(src.price_flag) if "price_flag" in src.index else "TR",
            })
            pad_rows.append({
                "date": day,
                "permno": pid,
                "source_date": pd.Timestamp(src.date).normalize(),
            })
            existing.add((day, pid))

    if new_quotes:
        px = pd.concat([px, pd.DataFrame(new_quotes)], ignore_index=True)
        px = px.sort_values(["date", "permno"]).reset_index(drop=True)
    pad = pd.DataFrame(pad_rows, columns=["date", "permno", "source_date"])
    return px, cal, pad


def rebalance_schedule(weights, calendar):
    """One row per quarterly snapshot: Thursday decision / Friday reference fill.

    ``weights`` needs ``date`` (post-rebalance snapshot session) and
    ``reference_date`` (the third-Friday reference session).
    """
    cal = _calendar_index(calendar)
    w = weights.copy()
    w["date"] = _dates(w.date)
    rows = []
    for snap, part in w.groupby("date", sort=True):
        if snap not in cal:
            raise ValueError(f"Snapshot {snap.date()} is not a calendar session")
        if "reference_date" not in part:
            raise ValueError("reference_close timing needs weights.reference_date")
        refs = _dates(part.reference_date).unique()
        if len(refs) != 1:
            raise ValueError(f"Snapshot {snap.date()} has several reference dates")
        ref = refs[0]
        if ref not in cal:
            raise ValueError(f"Reference date {ref.date()} is not a calendar session; "
                             "extend the calendar/prices back before the first reference date")
        loc = cal.get_loc(ref)
        if loc == 0:
            raise ValueError("Need one calendar session before the first reference date "
                             "to place the closing-auction orders")
        if ref >= snap:
            raise ValueError("Reference date must precede its post-rebalance snapshot")
        decision, execution = cal[loc - 1], ref
        rows.append({"snapshot_date": snap, "reference_date": ref,
                     "decision_date": decision, "execution_date": execution})
    out = pd.DataFrame(rows)
    if out.decision_date.duplicated().any():
        raise ValueError("Two snapshots map to the same decision session")
    return out


def synthetic_flows(calendar, *, seed=7, daily_sigma_bps=5.0, annual_drift_bps=0.0,
                    lumpy_prob=0.01, lumpy_bps=(50.0, 300.0), max_redemption_bps=500.0):
    """Exogenous investor-flow scenario in bps of NAV per dealing session.

    Daily retail-like noise ~ N(drift, sigma) plus rare institutional tickets
    (probability ``lumpy_prob`` per session, size uniform in ``lumpy_bps``,
    random sign). The first session has no flow (no notice can precede it).
    This is a stress scenario, not a calibrated investor-behaviour model.
    """
    days = _dates(calendar)
    rng = np.random.default_rng(seed)
    n = len(days)
    bps = rng.normal(annual_drift_bps / 252, daily_sigma_bps, n)
    lumpy = rng.random(n) < lumpy_prob
    bps[lumpy] += rng.choice([-1, 1], lumpy.sum()) * rng.uniform(*lumpy_bps, lumpy.sum())
    bps = np.maximum(bps, -max_redemption_bps)
    if n:
        bps[0] = 0.
    return pd.DataFrame({"date": days, "flow_bps": bps, "is_lumpy": lumpy})


def simulate_fund(weights, prices, calendar, events, start, end, *, initial_cash=10e9,
                  cash_buffer_bps=10, fee_bps=0, flows=None, flow_band_bps=25.0):
    """Daily ledger with reference-close rebalance fills.

    ``start`` must be the first decision session (the session before the first
    reference Friday). Returns the same tables as ``simulate`` plus order metadata.
    """
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if start > end or not np.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("Invalid dates/initial cash")
    if not 0 <= cash_buffer_bps < 10000 or not 0 <= fee_bps < 10000 or flow_band_bps < 0:
        raise ValueError("Invalid cash buffer/fee/flow band")
    w, q, e = weights.copy(), prices.copy(), events.copy()
    w["date"], q["date"] = _dates(w.date), _dates(q.date)
    for frame in (w, q):
        if frame[["date", "permno"]].duplicated().any():
            raise ValueError("Duplicate date/PERMNO")
        if frame.permno.isna().any():
            raise ValueError("Missing PERMNO")
    days = _dates(calendar)
    if days.has_duplicates or not days.is_monotonic_increasing:
        raise ValueError("Calendar must be unique and sorted")
    days = days[(days >= start) & (days <= end)]
    w = w[w.date.between(start, end)]
    if days.empty or w.empty:
        raise ValueError("No sessions or snapshots inside the window")
    schedule = rebalance_schedule(w, days)
    if schedule.decision_date.iloc[0] != days[0]:
        raise ValueError(f"Start on the first decision session "
                         f"({schedule.decision_date.iloc[0].date()} for reference_close)")
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
        cash_events = e.event_type.eq("CASH_DIVIDEND")
        if not e.loc[cash_events, "currency"].astype(str).str.upper().eq("USD").all():
            raise ValueError("Unverified/non-USD currency")

    # Flows keyed by dealing session. amount (USD) or flow_bps of notice-day NAV.
    f = pd.DataFrame(columns=["date", "amount"]) if flows is None else flows.copy()
    f["date"] = _dates(f.date)
    if f.date.duplicated().any() or not f.date.isin(days).all():
        raise ValueError("One flow per trading session; no out-of-window flows")
    has_bps = "flow_bps" in f and "amount" not in f
    col = "flow_bps" if has_bps else "amount"
    vals = pd.to_numeric(f[col], errors="raise").to_numpy(float) if len(f) else np.array([])
    if not np.isfinite(vals).all():
        raise ValueError("Invalid cash flows")
    flow_map = {d: v for d, v in zip(f.date, vals) if v}
    if days[0] in flow_map:
        raise ValueError("No flow can deal on the first session (no prior notice)")
    session_after = dict(zip(days[:-1], days[1:]))

    quotes = {d: x.set_index("permno") for d, x in q.groupby("date")}
    snapshots = {d: x.set_index("permno") for d, x in w.groupby("date")}
    by_decision = schedule.set_index("decision_date")
    split_events = e[e.event_type.eq("STOCK_SPLIT")] if not e.empty else e

    positions, receivables, pending = {}, {}, None
    cash, units = float(initial_cash), float(initial_cash / 100)
    ledger, holdings, fills, actions, orders = [], [], [], [], []
    noticed = {}             # dealing date -> USD amount fixed at notice

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

    def split_ratio_between(p, after, through):
        """Product of split ratios with ex_date in (after, through]."""
        if split_events.empty:
            return 1.
        s = split_events[split_events.permno.eq(p) & split_events.ex_date.gt(after)
                         & split_events.ex_date.le(through)]
        return float(np.prod(s.split_ratio.astype(float))) if len(s) else 1.

    def add_order(oo, day, p, qty, reason, meta):
        oo[p] = {"signed_quantity": int(qty), "decision_price": price(day, p),
                 "reason": reason, "order_id": order_id(day, p, reason)}
        orders.append({"order_id": oo[p]["order_id"], "decision_date": day, "permno": p,
                       "signed_quantity": int(qty), "decision_price": oo[p]["decision_price"],
                       "side": "BUY" if qty > 0 else "SELL", "reason": reason,
                       "expected_execution_date": session_after.get(day, pd.NaT), **meta})

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
                if n: positions[p] = int(round(updated))
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
        # Dealing: units at pre-flow NAV, cash moves before the closing auction fills.
        amount = float(noticed.pop(day, 0.))
        if preflow_nav + amount <= 0:
            raise ValueError("Redemption exceeds NAV")
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
            fraction = min(1., max(0., cash + sale_cash) / needed) if needed else 1.
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
                fills.append({"order_id": order["order_id"], "reason": order["reason"],
                    "decision_date": pending["date"], "execution_date": day,
                    "permno": p, "signed_requested": qty, "signed_filled": executed,
                    "unfilled_quantity": abs(qty - executed), "execution_price": px,
                    "decision_price": order["decision_price"], "fee": fee,
                    "executed_reference_drift": executed * (px - order["decision_price"])})
            pending = None
        if cash < -1e-5:
            raise ValueError("Negative cash: redemption could not be funded from the "
                             "flow sells and cash buffer; widen the buffer or sell earlier")
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

        # Notice of tomorrow's dealing flow arrives at today's close.
        tomorrow = session_after.get(day)
        next_flow = 0.
        if tomorrow is not None and tomorrow in flow_map:
            raw = flow_map[tomorrow]
            next_flow = raw / 10000 * nav if has_bps else raw
            noticed[tomorrow] = next_flow

        oo = {}
        if day in by_decision.index:
            sched = by_decision.loc[day]
            snap = snapshots[sched.snapshot_date]
            budget = (nav - recv + next_flow) * (1 - cash_buffer_bps / 10000)
            meta = {"snapshot_date": sched.snapshot_date, "reference_date": sched.reference_date}
            ref = sched.reference_date
            shares = {}
            for p, wt in snap.cap_weight_proxy.items():
                p = int(p)
                # Index shares in the decision-day share basis.
                # (A split going ex in (decision, reference] multiplies shares, so divide.)
                shares[p] = wt / price(ref, p) / split_ratio_between(p, day, ref)
            denom = sum(h * price(day, p) for p, h in shares.items())
            target = {p: int(np.floor(budget * h / denom)) for p, h in shares.items()}
            reason = "INITIAL" if not positions else "REBALANCE"
            for p in sorted(set(target) | set(positions)):
                qty = target.get(p, 0) - positions.get(p, 0)
                if qty:
                    add_order(oo, day, p, qty, reason, meta)
        elif positions and next_flow:
            # Flow trades: keep cash inside its band, pro rata to current holdings.
            projected = cash + next_flow
            target_cash = (nav - recv + next_flow) * cash_buffer_bps / 10000
            deviation = projected - target_cash
            invest = deviation > flow_band_bps / 10000 * nav
            raise_cash = projected < 0.5 * target_cash or projected < 0
            if invest or raise_cash:
                meta = {"snapshot_date": pd.NaT, "reference_date": pd.NaT}
                reason = "INFLOW" if deviation > 0 else "OUTFLOW"
                for p, n in sorted(positions.items()):
                    dollars = deviation * n * price(day, p) / market_value
                    qty = (np.floor(dollars / price(day, p)) if dollars > 0
                           else -min(n, np.ceil(-dollars / price(day, p))))
                    if qty:
                        add_order(oo, day, p, qty, reason, meta)
        if oo:
            pending = {"date": day, "orders": oo}
    navs = pd.DataFrame(ledger)
    navs["fund_return"] = navs.unit_nav.pct_change()
    navs.loc[0, "fund_return"] = navs.loc[0, "unit_nav"] / 100 - 1
    return {"daily_nav": navs, "daily_holdings": pd.DataFrame(holdings),
            "fills": pd.DataFrame(fills), "orders": pd.DataFrame(orders),
            "action_journal": pd.DataFrame(actions), "pending_orders": pending,
            "ending_receivables": receivables, "schedule": schedule,
            "unsettled_flow_notices": noticed}
