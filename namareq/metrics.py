"""Financial health metrics.

Every metric returns its inputs, the formula and the date range so the
dashboard can show the calculation.  Health scores are weighted sums of
component scores; thresholds are common rules of thumb (listed in
``*_SCORE_RULES``) — they are adjustable and are not professional or
regulatory standards.  Components without enough data are left out and the
remaining weights are re-normalised (and the dashboard says so).
"""
from __future__ import annotations

from datetime import date, timedelta

from . import dates, debts, ledger, loans, reports
from .db import profile_id, rows, system_account
from .permissions import require

BUSINESS_SCORE_RULES = {
    "current_ratio": {"weight": 15, "zero_at": 0.5, "full_at": 2.0},
    "obligation_cover": {"weight": 20, "zero_at": 0.5, "full_at": 1.5},
    "reserve_months": {"weight": 20, "zero_at": 0.0, "full_at": 3.0},
    "net_margin_pct": {"weight": 20, "zero_at": 0.0, "full_at": 15.0},
    "overdue_receivables_pct": {"weight": 15, "zero_at": 50.0, "full_at": 0.0},
    "dscr": {"weight": 10, "zero_at": 1.0, "full_at": 2.0},
}
PERSONAL_SCORE_RULES = {
    "emergency_months": {"weight": 35, "zero_at": 0.0, "full_at": 6.0},
    "debt_to_income_pct": {"weight": 25, "zero_at": 50.0, "full_at": 20.0},
    "savings_rate_pct": {"weight": 25, "zero_at": 0.0, "full_at": 20.0},
    "obligation_cover": {"weight": 15, "zero_at": 0.5, "full_at": 1.5},
}

CURRENT_ASSET_TYPES = ("business_cash", "personal_cash", "business_bank", "personal_bank", "card_clearing",
                       "savings", "customer_receivable", "personal_receivable", "other_asset")
CURRENT_LIABILITY_TYPES = ("credit_card", "supplier_payable", "personal_payable", "tax_payable",
                           "salary_payable", "other_liability")


def _component(value, rule) -> float | None:
    if value is None:
        return None
    lo, hi = rule["zero_at"], rule["full_at"]
    frac = (value - lo) / (hi - lo)
    return round(max(0.0, min(1.0, frac)) * 100, 1)


def _score(values: dict, rules: dict) -> dict:
    parts, total_w, acc = [], 0, 0.0
    for key, rule in rules.items():
        s = _component(values.get(key), rule)
        parts.append({"metric": key, "value": values.get(key), "score": s, "weight": rule["weight"],
                      "zero_at": rule["zero_at"], "full_at": rule["full_at"]})
        if s is not None:
            total_w += rule["weight"]
            acc += s * rule["weight"]
    return {"score": round(acc / total_w) if total_w else None, "components": parts,
            "coverage_pct": total_w, "rules_doc_key": "metrics.score_rules"}


def _trailing_months(today: str, n: int) -> tuple[str, str]:
    """The ``n`` most recent FULL calendar months before ``today``."""
    first_this = dates.month_bounds(today)[0]
    start = dates.add_months(first_this, -n)
    return start.isoformat(), (first_this - timedelta(days=1)).isoformat()


def _balance_by_types(conn, pid: int, types: tuple, as_of: str) -> int:
    total = 0
    for a in rows(conn, "SELECT id, account_type FROM financial_accounts WHERE profile_id=?", (pid,)):
        if a["account_type"] in types:
            total += ledger.account_balance(conn, a["id"], as_of)
    return total


def monthly_debt_service(conn, pid: int) -> int:
    return sum(loans.monthly_equivalent(ln["installment_minor"], ln["frequency"])
               for ln in rows(conn, """SELECT * FROM loans WHERE profile_id=? AND role='borrower'
                                       AND status IN ('active','overdue')""", (pid,)))


def business_metrics(conn, user: dict, today: str, months: int = 3) -> dict:
    require(conn, user, "business", "metrics", "read")
    pid = profile_id(conn, "business")
    start, end = _trailing_months(today, months)
    stmt = reports.income_statement(conn, user, "business", start, end)
    liquid = _balance_by_types(conn, pid, ("business_cash", "business_bank", "card_clearing"), today)
    savings = _balance_by_types(conn, pid, ("savings",), today)
    current_assets = _balance_by_types(conn, pid, CURRENT_ASSET_TYPES, today)
    loan_principal_12m = sum(
        i["principal_minor"] - i["paid_principal_minor"]
        for i in rows(conn, """SELECT i.* FROM loan_installments i JOIN loans l ON l.id=i.loan_id
                               WHERE l.profile_id=? AND l.role='borrower' AND i.status NOT IN ('paid','waived')
                               AND i.due_date <= ?""",
                      (pid, (date.fromisoformat(today) + timedelta(days=365)).isoformat())))
    current_liabilities = _balance_by_types(conn, pid, CURRENT_LIABILITY_TYPES, today) + loan_principal_12m
    fixed = sum(x["amount_minor"] for x in stmt["expenses"] if x["fixed"]) / months
    opex = stmt["total_expenses_minor"] / months
    net_monthly = stmt["net_minor"] / months
    interest_cat = conn.execute("SELECT id FROM expense_categories WHERE profile_kind='business' AND code='interest_expense'").fetchone()["id"]
    dep_cat = conn.execute("SELECT id FROM expense_categories WHERE profile_kind='business' AND code='depreciation'").fetchone()["id"]
    exp_by_cat = ledger.category_totals(conn, pid, "expense", start, end)
    noi_monthly = (stmt["net_minor"] + exp_by_cat.get(interest_cat, 0) + exp_by_cat.get(dep_cat, 0)) / months
    debt_service = monthly_debt_service(conn, pid)
    recv = debts.aging_report(conn, user, "business", "receivable", today)
    overdue_recv = recv["grand_total"] - recv["totals"]["current"]
    pay_soon_end = (date.fromisoformat(today) + timedelta(days=14)).isoformat()
    payables_14d = sum(
        amt for d in debts.list_debts(conn, user, "business") if d["direction"] == "payable"
        for due, amt in debts.outstanding_pieces(conn, d) if due <= pay_soon_end)
    inventory = ledger.account_balance(conn, system_account(conn, "business", "inventory")["id"], today)
    cogs_annual = stmt["total_cost_of_sales_minor"] * 12 / months
    # Bills are commitments (not yet liabilities), so cover them explicitly.
    obligations = sum(u["amount_minor"] for u in reports.upcoming_payments(conn, user, "business", today, 30))
    values = {
        "obligation_cover": round(liquid / obligations, 2) if obligations > 0 else None,
        "current_ratio": round(current_assets / current_liabilities, 2) if current_liabilities > 0 else None,
        "reserve_months": round((liquid + savings) / fixed, 2) if fixed > 0 else None,
        "net_margin_pct": stmt["net_margin_pct"],
        "overdue_receivables_pct": round(100 * overdue_recv / recv["grand_total"], 1) if recv["grand_total"] else 0.0,
        "dscr": (round(noi_monthly / debt_service, 2) if debt_service else None),
    }
    return {
        "as_of": today, "period": (start, end), "months": months,
        "liquid_minor": liquid, "savings_minor": savings, "current_assets_minor": current_assets,
        "current_liabilities_minor": current_liabilities, "working_capital_minor": current_assets - current_liabilities,
        "loan_principal_due_12m_minor": loan_principal_12m,
        "avg_monthly_fixed_costs_minor": round(fixed), "avg_monthly_opex_minor": round(opex),
        "avg_monthly_net_minor": round(net_monthly),
        "cash_runway_months": round(liquid / -net_monthly, 1) if net_monthly < 0 else None,
        "gross_margin_pct": stmt["gross_margin_pct"], "net_margin_pct": stmt["net_margin_pct"],
        "revenue_minor": stmt["total_income_minor"], "net_profit_minor": stmt["net_minor"],
        "monthly_debt_service_minor": debt_service, "avg_monthly_noi_minor": round(noi_monthly),
        "receivables_minor": recv["grand_total"], "overdue_receivables_minor": overdue_recv,
        "receivable_parties": recv["parties"][:5], "payables_due_14d_minor": payables_14d,
        "obligations_30d_minor": obligations,
        "inventory_minor": inventory,
        "inventory_turnover": round(cogs_annual / inventory, 2) if inventory else None,
        "slow_moving_inventory_available": False,
        "values": values, "health": _score(values, BUSINESS_SCORE_RULES),
    }


def personal_metrics(conn, user: dict, today: str, months: int = 3) -> dict:
    require(conn, user, "personal", "metrics", "read")
    pid = profile_id(conn, "personal")
    start, end = _trailing_months(today, months)
    stmt = reports.income_statement(conn, user, "personal", start, end)
    liquid = _balance_by_types(conn, pid, ("personal_cash", "personal_bank", "card_clearing"), today)
    savings = _balance_by_types(conn, pid, ("savings",), today)
    avg_income = stmt["total_income_minor"] / months
    avg_expenses = stmt["total_expenses_minor"] / months
    debt_service = monthly_debt_service(conn, pid)
    saved = reports.savings_report(conn, user, start, end)["total_saved_minor"]
    upcoming = reports.upcoming_payments(conn, user, "personal", today, 30)
    obligations = sum(u["amount_minor"] for u in upcoming)
    card_debt = _balance_by_types(conn, pid, ("credit_card",), today)
    values = {
        "emergency_months": round((liquid + savings) / avg_expenses, 2) if avg_expenses > 0 else None,
        "debt_to_income_pct": round(100 * debt_service / avg_income, 1) if avg_income > 0 else None,
        "savings_rate_pct": round(100 * saved / stmt["total_income_minor"], 1) if stmt["total_income_minor"] > 0 else None,
        "obligation_cover": round(liquid / obligations, 2) if obligations > 0 else None,
    }
    return {
        "as_of": today, "period": (start, end), "months": months, "liquid_minor": liquid, "savings_minor": savings,
        "avg_monthly_income_minor": round(avg_income), "avg_monthly_expenses_minor": round(avg_expenses),
        "monthly_debt_service_minor": debt_service, "saved_minor": saved, "credit_card_debt_minor": card_debt,
        "obligations_30d_minor": obligations, "values": values, "health": _score(values, PERSONAL_SCORE_RULES),
    }

