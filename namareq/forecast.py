"""Cash-flow forecasting (estimates, never guarantees).

Starting point: today's balance of cash, bank and card-clearing accounts
(savings are excluded from operating cash).  Day-by-day events are then
added:

* outstanding bills on their due date (overdue bills on day 0) and future
  occurrences of recurring bill series;
* active recurring income, expenses, savings transfers and planned purchases;
* loan installments (borrowed: outflow; lent: inflow; overdue borrowed
  installments on day 0, overdue lent installments excluded);
* debts: payables on their due date (overdue on day 0); receivables on their
  due date — overdue receivables are EXCLUDED (conservative) and reported;
* a baseline of other day-to-day income and spending equal to the daily
  average of the last 90 days, counting only transactions that are not
  recurring, not bill payments and not owner transfers (so nothing is
  counted twice); for the shop this baseline is the expected-sales input;
* the unpaid sales-tax balance, assumed payable 30 days from today.

Each forecast returns these assumptions with the figures used.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta

from . import accounts, bills, dates, debts
from .db import profile_id, rows, system_account, transaction
from . import ledger
from .permissions import require

HORIZONS = (7, 30, 90, 365)
BASELINE_DAYS = 90


def _baseline(conn, pid: int, today: str) -> dict:
    start = (date.fromisoformat(today) - timedelta(days=BASELINE_DAYS)).isoformat()
    end = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    inc = conn.execute(
        """SELECT COALESCE(SUM(t.amount_minor + t.tax_minor),0) FROM income_transactions t
           JOIN income_categories c ON c.id=t.category_id
           WHERE t.profile_id=? AND t.status='posted' AND t.recurring_id IS NULL
             AND t.txn_date BETWEEN ? AND ?""", (pid, start, end)).fetchone()[0]
    exp = conn.execute(
        """SELECT COALESCE(SUM(amount_minor + tax_minor),0) FROM expense_transactions
           WHERE profile_id=? AND status='posted' AND recurring_id IS NULL AND bill_id IS NULL
             AND paid_status='paid' AND txn_date BETWEEN ? AND ?""", (pid, start, end)).fetchone()[0]
    first = conn.execute("SELECT MIN(entry_date) FROM ledger_entries WHERE profile_id=? AND entry_type <> 'opening_balance'",
                         (pid,)).fetchone()[0]
    # If the books are younger than 90 days, average over the days we have.
    days = BASELINE_DAYS
    if first and first > start:
        days = max(1, (date.fromisoformat(today) - date.fromisoformat(first)).days)
    last30_start = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    inc30 = conn.execute(
        """SELECT COALESCE(SUM(amount_minor + tax_minor),0) FROM income_transactions WHERE profile_id=?
           AND status='posted' AND recurring_id IS NULL AND txn_date BETWEEN ? AND ?""",
        (pid, last30_start, end)).fetchone()[0]
    prior_days = max(1, days - 30)
    trend = None
    if days > 30 and inc - inc30 > 0:
        trend = round(100 * ((inc30 / 30) - ((inc - inc30) / prior_days)) / ((inc - inc30) / prior_days), 1)
    return {"window": (start, end), "days": days, "income_total_minor": inc, "expense_total_minor": exp,
            "daily_income_minor": round(inc / days), "daily_expense_minor": round(exp / days),
            "recent_income_trend_pct": trend}


def _events(conn, user: dict, profile: str, today: str, end: str) -> tuple[list[dict], dict]:
    pid = profile_id(conn, profile)
    ev: list[dict] = []
    excluded = {"overdue_receivables_minor": 0, "overdue_lent_installments_minor": 0}

    for o in bills.projected_bill_occurrences(conn, pid, today, end):
        ev.append({"date": o["date"], "amount_minor": -o["amount_minor"], "kind": "bill", "label": o["payee"],
                   "projected": o["projected"], "overdue": o["overdue"]})

    for r in rows(conn, "SELECT * FROM recurring_transactions WHERE profile_id=? AND is_active=1", (pid,)):
        first = r["next_date"]
        for occ in dates.occurrences(first, r["frequency"], end, r["custom_interval_days"], r["end_date"]):
            when = max(occ.isoformat(), today)
            sign = 1 if r["kind"] == "income" else -1
            ev.append({"date": when, "amount_minor": sign * r["amount_minor"], "kind": f"recurring_{r['kind']}",
                       "label": r["name"], "projected": True, "overdue": occ.isoformat() < today})

    for i in rows(conn, """SELECT i.*, l.role, l.lender, l.borrower FROM loan_installments i JOIN loans l ON l.id=i.loan_id
                           WHERE l.profile_id=? AND l.status NOT IN ('completed','defaulted')
                           AND i.status NOT IN ('paid','waived') AND i.due_date <= ?""", (pid, end)):
        amt = i["principal_minor"] + i["interest_minor"] - i["paid_principal_minor"] - i["paid_interest_minor"]
        if i["role"] == "borrower":
            ev.append({"date": max(i["due_date"], today), "amount_minor": -amt, "kind": "loan_installment",
                       "label": i["lender"], "projected": False, "overdue": i["due_date"] < today})
        elif i["due_date"] >= today:
            ev.append({"date": i["due_date"], "amount_minor": amt, "kind": "loan_collection",
                       "label": i["borrower"], "projected": False, "overdue": False})
        else:
            excluded["overdue_lent_installments_minor"] += amt

    for d in debts.list_debts(conn, user, profile):
        for due, amt in debts.outstanding_pieces(conn, d):
            if due > end:
                continue
            if d["direction"] == "payable":
                ev.append({"date": max(due, today), "amount_minor": -amt, "kind": "debt_payable",
                           "label": d["creditor"], "projected": False, "overdue": due < today})
            elif due >= today:
                ev.append({"date": due, "amount_minor": amt, "kind": "debt_receivable", "label": d["debtor"],
                           "projected": False, "overdue": False})
            else:
                excluded["overdue_receivables_minor"] += amt

    if profile == "business":
        tax_bal = ledger.account_balance(conn, system_account(conn, "business", "sales_tax_payable")["id"], today)
        tax_date = (date.fromisoformat(today) + timedelta(days=30)).isoformat()
        if tax_bal > 0 and tax_date <= end:
            ev.append({"date": tax_date, "amount_minor": -tax_bal, "kind": "sales_tax", "label": "sales_tax",
                       "projected": True, "overdue": False})
    return ev, excluded


def cash_forecast(conn, user: dict, profile: str, today: str, horizon_days: int,
                  include_baseline: bool = True, save: bool = False) -> dict:
    require(conn, user, profile, "forecast", "read")
    if horizon_days not in HORIZONS:
        horizon_days = min(HORIZONS, key=lambda h: abs(h - horizon_days))
    end = (date.fromisoformat(today) + timedelta(days=horizon_days)).isoformat()
    opening = accounts.liquid_balance(conn, profile, today)
    pid = profile_id(conn, profile)
    events, excluded = _events(conn, user, profile, today, end)
    base = _baseline(conn, pid, today)
    by_day: dict[str, list] = defaultdict(list)
    for e in events:
        by_day[e["date"]].append(e)
    balance, series = opening, []
    first_negative = None
    low = (opening, today)
    t0 = date.fromisoformat(today)
    for i in range(horizon_days + 1):
        day = (t0 + timedelta(days=i)).isoformat()
        inflow = sum(e["amount_minor"] for e in by_day.get(day, []) if e["amount_minor"] > 0)
        outflow = -sum(e["amount_minor"] for e in by_day.get(day, []) if e["amount_minor"] < 0)
        if include_baseline and i > 0:
            inflow += base["daily_income_minor"]
            outflow += base["daily_expense_minor"]
        balance += inflow - outflow
        series.append({"date": day, "inflow_minor": inflow, "outflow_minor": outflow, "balance_minor": balance})
        if balance < low[0]:
            low = (balance, day)
        if balance < 0 and first_negative is None:
            first_negative = day
    total_in = sum(s["inflow_minor"] for s in series)
    total_out = sum(s["outflow_minor"] for s in series)
    result = {
        "profile": profile, "as_of": today, "horizon_days": horizon_days, "end": end, "is_estimate": True,
        "opening_minor": opening, "closing_minor": balance, "total_inflow_minor": total_in,
        "total_outflow_minor": total_out, "lowest_balance_minor": low[0], "lowest_balance_date": low[1],
        "first_shortage_date": first_negative,
        "days_until_shortage": (date.fromisoformat(first_negative) - t0).days if first_negative else None,
        "series": series, "events": sorted(events, key=lambda e: e["date"]),
        "assumptions": {"baseline": base, "excluded": excluded, "include_baseline": include_baseline,
                        "method_doc_key": "forecast.method"},
    }
    if save:
        with transaction(conn):
            conn.execute("""INSERT INTO cash_flow_forecasts(scope, as_of, horizon_days, result_json, assumptions_json, created_by)
                            VALUES (?,?,?,?,?,?)""",
                         (profile, today, horizon_days,
                          json.dumps({k: v for k, v in result.items() if k not in ("series", "events", "assumptions")}),
                          json.dumps(result["assumptions"]), user["id"]))
    return result


def combined_forecast(conn, user: dict, today: str, horizon_days: int) -> dict:
    """High-level owner view: the two forecasts side by side plus total
    liquidity per day.  Categories are never merged."""
    b = cash_forecast(conn, user, "business", today, horizon_days)
    p = cash_forecast(conn, user, "personal", today, horizon_days)
    series = [{"date": bs["date"], "business_minor": bs["balance_minor"], "personal_minor": ps["balance_minor"],
               "total_minor": bs["balance_minor"] + ps["balance_minor"]}
              for bs, ps in zip(b["series"], p["series"])]
    return {"as_of": today, "horizon_days": b["horizon_days"], "is_estimate": True, "business": b, "personal": p,
            "series": series, "lowest_total_minor": min(s["total_minor"] for s in series)}


def weekly_rollup(series: list[dict]) -> list[dict]:
    out = []
    for i in range(0, len(series), 7):
        chunk = series[i:i + 7]
        out.append({"start": chunk[0]["date"], "end": chunk[-1]["date"],
                    "inflow_minor": sum(c["inflow_minor"] for c in chunk),
                    "outflow_minor": sum(c["outflow_minor"] for c in chunk),
                    "balance_minor": chunk[-1]["balance_minor"],
                    "low_minor": min(c["balance_minor"] for c in chunk)})
    return out
