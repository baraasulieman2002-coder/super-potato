"""Budgets and financial goals.

Budget actuals come from the ledger:
* income / expense lines: net amounts posted to the category;
* savings: net money moved into savings/investment accounts;
* debt_payment: principal reductions on loans, credit cards and payables;
* capital: purchases of fixed assets.
Projected period-end = actual / elapsed days * period days (linear pace).
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from . import chart, dates, ledger
from .db import FinanceError, audit, profile_id, rows, transaction
from .permissions import require

LINE_TYPES = ("income", "expense", "savings", "debt_payment", "capital")
GOAL_TYPES = {
    "personal": ("emergency_fund", "debt_payoff", "vehicle", "home_deposit", "education", "travel",
                 "investment", "retirement", "custom"),
    "business": ("revenue_target", "profit_target", "cash_reserve", "inventory_fund", "equipment",
                 "expansion", "new_branch", "debt_payoff", "marketing", "custom"),
}


def _kind(conn, pid):
    return conn.execute("SELECT kind FROM financial_profiles WHERE id=?", (pid,)).fetchone()["kind"]


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------
def _period(period_type: str, start: str) -> tuple[str, str]:
    s = date.fromisoformat(start)
    if period_type == "monthly":
        s = s.replace(day=1)
        return s.isoformat(), dates.month_bounds(s)[1].isoformat()
    if period_type == "annual":
        s = s.replace(month=1, day=1)
        return s.isoformat(), s.replace(month=12, day=31).isoformat()
    raise FinanceError("error.invalid_period_type")


def create_budget(conn, user: dict, *, profile: str, name: str, period_type: str, start_date: str,
                  lines: list[dict], rollover: bool = False) -> int:
    require(conn, user, profile, "budgets", "write")
    pid = profile_id(conn, profile)
    start, end = _period(period_type, start_date)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM budgets WHERE profile_id=? AND period_type=? AND start_date=?",
                        (pid, period_type, start)).fetchone():
            raise FinanceError("error.budget_exists")
        cur = conn.execute(
            """INSERT INTO budgets(profile_id, name, period_type, start_date, end_date, rollover, created_by)
               VALUES (?,?,?,?,?,?,?)""", (pid, name, period_type, start, end, int(rollover), user["id"]))
        bid = cur.lastrowid
        for ln in lines:
            add_budget_line(conn, user, bid, **ln)
        audit(conn, user["id"], "budgets", bid, "create", None, pid)
    return bid


def add_budget_line(conn, user: dict, budget_id: int, *, line_type: str, amount_minor: int,
                    income_category_id: int | None = None, expense_category_id: int | None = None,
                    area: str | None = None, notes: str | None = None) -> int:
    b = conn.execute("SELECT * FROM budgets WHERE id=?", (budget_id,)).fetchone()
    if b is None:
        raise FinanceError("error.unknown_record")
    kind = _kind(conn, b["profile_id"])
    require(conn, user, kind, "budgets", "write")
    if line_type not in LINE_TYPES:
        raise FinanceError("error.invalid_line_type")
    if line_type == "income" and income_category_id is None:
        raise FinanceError("error.category_required")
    if line_type == "expense" and expense_category_id is None:
        raise FinanceError("error.category_required")
    if amount_minor < 0:
        raise FinanceError("error.amount_must_be_positive")
    table, cat = (("income_categories", income_category_id) if line_type == "income"
                  else ("expense_categories", expense_category_id))
    if cat is not None:
        row = conn.execute(f"SELECT profile_kind FROM {table} WHERE id=?", (cat,)).fetchone()
        if row is None or row["profile_kind"] != kind:
            raise FinanceError("error.category_wrong_profile")
    cur = conn.execute(
        """INSERT INTO budget_lines(budget_id, line_type, income_category_id, expense_category_id, area,
               amount_minor, notes) VALUES (?,?,?,?,?,?,?)""",
        (budget_id, line_type, income_category_id if line_type == "income" else None,
         expense_category_id if line_type == "expense" else None, area, amount_minor, notes))
    return cur.lastrowid


def _account_type_movement(conn, pid: int, types: tuple, start: str, end: str, debit_side=True) -> int:
    placeholders = ",".join("?" * len(types))
    row = conn.execute(
        f"""SELECT COALESCE(SUM(l.debit_minor),0) d, COALESCE(SUM(l.credit_minor),0) c
            FROM ledger_lines l JOIN ledger_entries e ON e.id=l.entry_id
            JOIN financial_accounts a ON a.id=l.account_id
            WHERE e.profile_id=? AND a.account_type IN ({placeholders})
              AND e.entry_date BETWEEN ? AND ? AND e.entry_type NOT IN ('opening_balance')
              AND NOT (e.entry_type = 'loan_origination' AND l.credit_minor > 0)""",
        [pid, *types, start, end]).fetchone()
    return row["d"] - row["c"] if debit_side else row["c"] - row["d"]


def line_actual(conn, pid: int, line: dict, start: str, end: str) -> int:
    t = line["line_type"]
    if t == "income":
        return ledger.category_totals(conn, pid, "income", start, end).get(line["income_category_id"], 0)
    if t == "expense":
        return ledger.category_totals(conn, pid, "expense", start, end).get(line["expense_category_id"], 0)
    if t == "savings":
        return _account_type_movement(conn, pid, ("savings", "investment"), start, end)
    if t == "debt_payment":
        placeholders = ",".join("?" * len(chart.DEBT_PAYMENT_TYPES))
        row = conn.execute(
            f"""SELECT COALESCE(SUM(l.debit_minor),0) FROM ledger_lines l JOIN ledger_entries e ON e.id=l.entry_id
                JOIN financial_accounts a ON a.id=l.account_id
                WHERE e.profile_id=? AND a.account_type IN ({placeholders}) AND e.entry_date BETWEEN ? AND ?
                  AND e.entry_type <> 'reversal'""",
            [pid, *chart.DEBT_PAYMENT_TYPES, start, end]).fetchone()
        return row[0]
    if t == "capital":
        return _account_type_movement(conn, pid, ("fixed_asset",), start, end)
    return 0


def _previous_period(budget: dict) -> tuple[str, str]:
    if budget["period_type"] == "monthly":
        s = dates.add_months(date.fromisoformat(budget["start_date"]), -1)
        return s.isoformat(), dates.month_bounds(s)[1].isoformat()
    s = date.fromisoformat(budget["start_date"]).replace(year=date.fromisoformat(budget["start_date"]).year - 1)
    return s.isoformat(), s.replace(month=12, day=31).isoformat()


def _line_key(line: dict):
    return (line["line_type"], line["income_category_id"], line["expense_category_id"], line["area"])


def _rollover_in(conn, budget: dict, line: dict, depth: int = 0) -> int:
    """Unused amount carried from the previous budget's matching expense line."""
    if not budget["rollover"] or line["line_type"] != "expense" or depth > 24:
        return 0
    prev_start, prev_end = _previous_period(budget)
    prev = conn.execute("SELECT * FROM budgets WHERE profile_id=? AND period_type=? AND start_date=?",
                        (budget["profile_id"], budget["period_type"], prev_start)).fetchone()
    if prev is None or not prev["rollover"]:
        return 0
    prev = dict(prev)
    for pl in rows(conn, "SELECT * FROM budget_lines WHERE budget_id=?", (prev["id"],)):
        if _line_key(pl) == _line_key(line):
            available = pl["amount_minor"] + _rollover_in(conn, prev, pl, depth + 1)
            return max(0, available - line_actual(conn, prev["profile_id"], pl, prev_start, prev_end))
    return 0


def budget_vs_actual(conn, user: dict, budget_id: int, as_of: str) -> dict:
    b = conn.execute("SELECT * FROM budgets WHERE id=?", (budget_id,)).fetchone()
    if b is None:
        raise FinanceError("error.unknown_record")
    b = dict(b)
    kind = _kind(conn, b["profile_id"])
    require(conn, user, kind, "budgets", "read")
    start, end = b["start_date"], b["end_date"]
    actual_end = min(as_of, end)
    total_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    elapsed = max(0, min(total_days, (date.fromisoformat(as_of) - date.fromisoformat(start)).days + 1))
    prev_start, prev_end = _previous_period(b)
    out_lines = []
    for ln in rows(conn, """SELECT bl.*, ic.name_en inc_en, ic.name_ar inc_ar, ec.name_en exp_en, ec.name_ar exp_ar
                           FROM budget_lines bl LEFT JOIN income_categories ic ON ic.id=bl.income_category_id
                           LEFT JOIN expense_categories ec ON ec.id=bl.expense_category_id
                           WHERE bl.budget_id=? ORDER BY bl.line_type, bl.id""", (budget_id,)):
        rollover = _rollover_in(conn, b, ln)
        budgeted = ln["amount_minor"] + rollover
        actual = line_actual(conn, b["profile_id"], ln, start, actual_end) if elapsed else 0
        projected = round(actual * total_days / elapsed) if 0 < elapsed < total_days else actual
        prior = line_actual(conn, b["profile_id"], ln, prev_start, prev_end)
        is_cost = ln["line_type"] in ("expense", "capital")
        over = is_cost and actual > budgeted
        at_risk = is_cost and not over and projected > budgeted
        out_lines.append({
            **ln, "rollover_minor": rollover, "budgeted_minor": budgeted, "actual_minor": actual,
            "remaining_minor": budgeted - actual,
            "pct_used": round(100 * actual / budgeted, 1) if budgeted else None,
            "projected_minor": projected, "prior_minor": prior,
            "prior_change_pct": round(100 * (actual - prior) / prior, 1) if prior else None,
            "over_budget": over, "at_risk": at_risk,
            "below_target": ln["line_type"] in ("income", "savings", "debt_payment") and projected < budgeted,
        })
    return {"budget": b, "profile": kind, "as_of": as_of, "elapsed_days": elapsed, "total_days": total_days,
            "lines": out_lines, "prior_period": (prev_start, prev_end),
            "assumption": "projected = actual / elapsed days × days in period (linear pace)"}


def list_budgets(conn, user: dict, profile: str) -> list[dict]:
    require(conn, user, profile, "budgets", "read")
    return rows(conn, "SELECT * FROM budgets WHERE profile_id=? ORDER BY start_date DESC",
                (profile_id(conn, profile),))


def copy_budget(conn, user: dict, budget_id: int, new_start: str) -> int:
    b = dict(conn.execute("SELECT * FROM budgets WHERE id=?", (budget_id,)).fetchone())
    kind = _kind(conn, b["profile_id"])
    lines = [{k: ln[k] for k in ("line_type", "amount_minor", "income_category_id", "expense_category_id",
                                 "area", "notes")}
             for ln in rows(conn, "SELECT * FROM budget_lines WHERE budget_id=?", (budget_id,))]
    return create_budget(conn, user, profile=kind, name=b["name"], period_type=b["period_type"],
                         start_date=new_start, lines=lines, rollover=bool(b["rollover"]))


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------
def create_goal(conn, user: dict, *, profile: str, goal_type: str, name_en: str, name_ar: str,
                target_minor: int, target_date: str, start_date: str, starting_minor: int = 0,
                contribution_frequency: str = "monthly", linked_account_id: int | None = None,
                priority: int = 3, notes: str | None = None) -> int:
    require(conn, user, profile, "goals", "write")
    if goal_type not in GOAL_TYPES[profile]:
        raise FinanceError("error.invalid_goal_type")
    if target_minor <= 0:
        raise FinanceError("error.amount_must_be_positive")
    if not name_en.strip() or not name_ar.strip():
        raise FinanceError("error.bilingual_name_required")
    target_date, start_date = ledger.validate_date(target_date), ledger.validate_date(start_date)
    if target_date <= start_date:
        raise FinanceError("error.invalid_date_range")
    pid = profile_id(conn, profile)
    if linked_account_id is not None:
        acct = conn.execute("SELECT profile_id FROM financial_accounts WHERE id=?", (linked_account_id,)).fetchone()
        if acct is None or acct["profile_id"] != pid:
            raise FinanceError("error.account_wrong_profile")
    cur = conn.execute(
        """INSERT INTO financial_goals(profile_id, goal_type, name_en, name_ar, target_minor, starting_minor,
               start_date, target_date, contribution_frequency, linked_account_id, priority, notes, created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, goal_type, name_en.strip(), name_ar.strip(), target_minor, starting_minor, start_date,
         target_date, contribution_frequency, linked_account_id, priority, notes, user["id"]))
    return cur.lastrowid


def add_contribution(conn, user: dict, goal_id: int, *, amount_minor: int, on: str,
                     notes: str | None = None, transfer_id: int | None = None) -> int:
    g = conn.execute("SELECT * FROM financial_goals WHERE id=?", (goal_id,)).fetchone()
    if g is None:
        raise FinanceError("error.unknown_record")
    require(conn, user, _kind(conn, g["profile_id"]), "goals", "write")
    if g["linked_account_id"]:
        # Progress of a linked goal is the account balance; manual
        # contributions would double count.
        raise FinanceError("error.goal_linked_to_account")
    cur = conn.execute(
        "INSERT INTO goal_contributions(goal_id, contrib_date, amount_minor, transfer_id, notes, created_by) VALUES (?,?,?,?,?,?)",
        (goal_id, ledger.validate_date(on), amount_minor, transfer_id, notes, user["id"]))
    return cur.lastrowid


def goal_current(conn, goal: dict, as_of: str) -> int:
    pid = goal["profile_id"]
    if goal["goal_type"] == "revenue_target":
        return sum(ledger.category_totals(conn, pid, "income", goal["start_date"], as_of).values())
    if goal["goal_type"] == "profit_target":
        inc = sum(ledger.category_totals(conn, pid, "income", goal["start_date"], as_of).values())
        exp = sum(ledger.category_totals(conn, pid, "expense", goal["start_date"], as_of).values())
        return inc - exp
    if goal["linked_account_id"]:
        acct = conn.execute("SELECT account_type FROM financial_accounts WHERE id=?",
                            (goal["linked_account_id"],)).fetchone()
        bal = ledger.account_balance(conn, goal["linked_account_id"], as_of)
        if chart.account_class(acct["account_type"]) == "liability":
            # Debt payoff: progress is how much of the target has been repaid.
            return max(0, goal["target_minor"] - bal)
        return bal
    contributed = conn.execute(
        "SELECT COALESCE(SUM(amount_minor),0) FROM goal_contributions WHERE goal_id=? AND contrib_date<=?",
        (goal["id"], as_of)).fetchone()[0]
    return goal["starting_minor"] + contributed


def _value_at(conn, goal, on: str) -> int:
    return goal_current(conn, goal, on) if on >= goal["start_date"] else goal["starting_minor"]


def goal_progress(conn, goal: dict, as_of: str) -> dict:
    current = goal_current(conn, goal, as_of)
    remaining = max(0, goal["target_minor"] - current)
    months_left = dates.months_between(as_of, goal["target_date"])
    required = math.ceil(remaining / months_left) if months_left > 0 else remaining
    lookback = (date.fromisoformat(as_of) - timedelta(days=90)).isoformat()
    window_start = max(lookback, goal["start_date"])
    window_months = max(dates.months_between(window_start, as_of), 1)
    pace = (current - _value_at(conn, goal, window_start)) / window_months
    forecast = None
    if remaining == 0:
        forecast = as_of
    elif pace > 0:
        forecast = (date.fromisoformat(as_of) + timedelta(days=round(remaining / pace * 30.4375))).isoformat()
    return {
        **goal, "current_minor": current, "remaining_minor": remaining,
        "progress_pct": round(min(100.0, 100 * current / goal["target_minor"]), 1),
        "months_left": round(months_left, 1), "required_monthly_minor": required,
        "recent_monthly_pace_minor": round(pace), "pace_window": (window_start, as_of),
        "forecast_completion": forecast,
        "on_track": forecast is not None and forecast <= goal["target_date"],
    }


def list_goals(conn, user: dict, profile: str, as_of: str) -> list[dict]:
    require(conn, user, profile, "goals", "read")
    return [goal_progress(conn, g, as_of) for g in rows(
        conn, "SELECT * FROM financial_goals WHERE profile_id=? AND status IN ('active','achieved') ORDER BY priority, target_date",
        (profile_id(conn, profile),))]
