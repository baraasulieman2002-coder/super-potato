"""Financial reports, built only from posted ledger lines.

Business and personal reports are separate functions over separate
profiles.  The combined owner overview shows each profile side by side and
adds only balance-sheet totals (assets, liabilities, net worth, liquid cash);
it never merges business income/expenses with personal income/expenses.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from . import accounts as accounts_mod
from . import bills as bills_mod
from . import chart, dates, debts, ledger, loans, planning
from .db import profile_id, rows, system_account
from .money import split_proportionally
from .permissions import require


def _cats(conn, kind: str, table: str) -> dict[int, dict]:
    return {r["id"]: r for r in rows(conn, f"SELECT * FROM {table} WHERE profile_kind=?", (kind,))}


def _pct(part: int, whole: int):
    return round(100 * part / whole, 1) if whole else None


# ---------------------------------------------------------------------------
# Income statement (business P&L / personal income & expense statement)
# ---------------------------------------------------------------------------
def income_statement(conn, user: dict, profile: str, start: str, end: str) -> dict:
    require(conn, user, profile, "reports", "read")
    pid = profile_id(conn, profile)
    inc_cats = _cats(conn, profile, "income_categories")
    exp_cats = _cats(conn, profile, "expense_categories")
    inc = ledger.category_totals(conn, pid, "income", start, end)
    exp = ledger.category_totals(conn, pid, "expense", start, end)
    income_lines = [{"id": k, "name_en": inc_cats[k]["name_en"], "name_ar": inc_cats[k]["name_ar"],
                     "amount_minor": v} for k, v in sorted(inc.items(), key=lambda kv: -kv[1]) if v]
    cogs_lines, opex_lines = [], []
    for k, v in sorted(exp.items(), key=lambda kv: -kv[1]):
        if not v:
            continue
        line = {"id": k, "name_en": exp_cats[k]["name_en"], "name_ar": exp_cats[k]["name_ar"],
                "amount_minor": v, "fixed": bool(exp_cats[k]["is_fixed_cost"])}
        (cogs_lines if exp_cats[k]["is_cost_of_sales"] else opex_lines).append(line)
    revenue = sum(x["amount_minor"] for x in income_lines)
    cogs = sum(x["amount_minor"] for x in cogs_lines)
    opex = sum(x["amount_minor"] for x in opex_lines)
    gross = revenue - cogs
    net = gross - opex
    return {"profile": profile, "start": start, "end": end, "income": income_lines, "total_income_minor": revenue,
            "cost_of_sales": cogs_lines, "total_cost_of_sales_minor": cogs, "gross_profit_minor": gross,
            "expenses": opex_lines, "total_expenses_minor": opex, "net_minor": net,
            "gross_margin_pct": _pct(gross, revenue), "net_margin_pct": _pct(net, revenue)}


# ---------------------------------------------------------------------------
# Balance sheet / net worth
# ---------------------------------------------------------------------------
def balance_sheet(conn, user: dict, profile: str, as_of: str) -> dict:
    require(conn, user, profile, "reports", "read")
    pid = profile_id(conn, profile)
    sections = {"asset": [], "liability": [], "equity": []}
    for a in rows(conn, "SELECT * FROM financial_accounts WHERE profile_id=? ORDER BY account_type, name_en", (pid,)):
        bal = ledger.account_balance(conn, a["id"], as_of)
        if not bal:
            continue
        cls = chart.account_class(a["account_type"])
        item = {"id": a["id"], "name_en": a["name_en"], "name_ar": a["name_ar"], "type": a["account_type"],
                "amount_minor": -bal if cls == "contra_asset" else bal,
                "liquid": chart.is_liquid(a["account_type"])}
        sections["asset" if cls == "contra_asset" else cls].append(item)
    inc = sum(ledger.category_totals(conn, pid, "income", None, as_of).values())
    exp = sum(ledger.category_totals(conn, pid, "expense", None, as_of).values())
    earnings = inc - exp
    total_assets = sum(i["amount_minor"] for i in sections["asset"])
    total_liabilities = sum(i["amount_minor"] for i in sections["liability"])
    total_equity = sum(i["amount_minor"] for i in sections["equity"]) + earnings
    return {"profile": profile, "as_of": as_of, "assets": sections["asset"], "liabilities": sections["liability"],
            "equity": sections["equity"], "retained_earnings_minor": earnings,
            "total_assets_minor": total_assets, "total_liabilities_minor": total_liabilities,
            "total_equity_minor": total_equity, "net_worth_minor": total_assets - total_liabilities,
            "liquid_minor": sum(i["amount_minor"] for i in sections["asset"] if i["liquid"]),
            "balanced": total_assets == total_liabilities + total_equity}


def trial_balance(conn, user: dict, profile: str, as_of: str) -> dict:
    require(conn, user, profile, "reports", "read")
    lines = ledger.trial_balance(conn, profile_id(conn, profile), as_of)
    d, c = sum(x["debit"] for x in lines), sum(x["credit"] for x in lines)
    return {"profile": profile, "as_of": as_of, "lines": lines, "total_debit_minor": d,
            "total_credit_minor": c, "balanced": d == c}


# ---------------------------------------------------------------------------
# Cash-flow statement (direct method)
# ---------------------------------------------------------------------------
FINANCING_TYPES = ("loan_payable", "loan_receivable", "owner_equity")
INVESTING_TYPES = ("fixed_asset", "investment", "accumulated_depreciation")


def _flow_class(line, acct_types: dict) -> str:
    if line["account_id"] is None:
        return "operating"
    t = acct_types[line["account_id"]]
    if t in FINANCING_TYPES:
        return "financing"
    if t in INVESTING_TYPES:
        return "investing"
    return "operating"


def cash_flow_statement(conn, user: dict, profile: str, start: str, end: str) -> dict:
    """Movement of cash, bank, card-clearing and savings accounts, classified
    by the counterpart lines of each entry.  Mixed entries (e.g. a loan
    installment: principal = financing, interest = operating) are split in
    proportion to the counterpart amounts.  Moves between two liquid accounts
    of the same profile are excluded (they are not cash flows)."""
    require(conn, user, profile, "reports", "read")
    pid = profile_id(conn, profile)
    acct_types = {a["id"]: a["account_type"] for a in
                  rows(conn, "SELECT id, account_type FROM financial_accounts WHERE profile_id=?", (pid,))}
    liquid_ids = {i for i, t in acct_types.items() if chart.is_liquid(t) or t == "savings"}
    flows = {"operating": 0, "investing": 0, "financing": 0}
    detail: dict[str, dict[str, int]] = {k: defaultdict(int) for k in flows}
    entries = rows(conn, "SELECT id, entry_type FROM ledger_entries WHERE profile_id=? AND entry_date BETWEEN ? AND ?",
                   (pid, start, end))
    for e in entries:
        lines = ledger.entry_lines(conn, e["id"])
        delta = sum(ln["debit_minor"] - ln["credit_minor"] for ln in lines if ln["account_id"] in liquid_ids)
        if delta == 0:
            continue
        others = [ln for ln in lines if ln["account_id"] not in liquid_ids]
        weights = [abs(ln["debit_minor"] - ln["credit_minor"]) for ln in others]
        parts = split_proportionally(abs(delta), weights)
        sign = 1 if delta > 0 else -1
        for ln, part in zip(others, parts):
            cls = _flow_class(ln, acct_types)
            flows[cls] += sign * part
            label = e["entry_type"] if e["entry_type"] != "reversal" else "reversal"
            detail[cls][label] += sign * part
    bs_open = sum(ledger.account_balance(conn, i, (date.fromisoformat(start) - timedelta(days=1)).isoformat())
                  for i in liquid_ids)
    bs_close = sum(ledger.account_balance(conn, i, end) for i in liquid_ids)
    net = sum(flows.values())
    return {"profile": profile, "start": start, "end": end, "opening_cash_minor": bs_open,
            "closing_cash_minor": bs_close, "net_change_minor": net, **{f"{k}_minor": v for k, v in flows.items()},
            "detail": {k: dict(v) for k, v in detail.items()}, "reconciles": bs_open + net == bs_close}


# ---------------------------------------------------------------------------
# Category analysis
# ---------------------------------------------------------------------------
def expense_analysis(conn, user: dict, profile: str, month: str, history_months: int = 3) -> dict:
    """Expenses by category for ``month`` (YYYY-MM-01 or any date in it)
    compared with the average of the previous ``history_months`` months."""
    require(conn, user, profile, "reports", "read")
    pid = profile_id(conn, profile)
    m_start, m_end = dates.month_bounds(month)
    current = ledger.category_totals(conn, pid, "expense", m_start.isoformat(), m_end.isoformat())
    h_start = dates.add_months(m_start, -history_months)
    hist = ledger.category_totals(conn, pid, "expense", h_start.isoformat(),
                                  (m_start - timedelta(days=1)).isoformat())
    cats = _cats(conn, profile, "expense_categories")
    total = sum(current.values())
    lines = []
    for cid in set(current) | set(hist):
        cur, avg = current.get(cid, 0), round(hist.get(cid, 0) / history_months)
        if not cur and not avg:
            continue
        lines.append({"id": cid, "name_en": cats[cid]["name_en"], "name_ar": cats[cid]["name_ar"],
                      "amount_minor": cur, "share_pct": _pct(cur, total), "avg_minor": avg,
                      "change_pct": _pct(cur - avg, avg)})
    lines.sort(key=lambda x: -x["amount_minor"])
    return {"profile": profile, "month": m_start.isoformat()[:7], "lines": lines, "total_minor": total,
            "history_window": (h_start.isoformat(), (m_start - timedelta(days=1)).isoformat())}


# ---------------------------------------------------------------------------
# Business-specific
# ---------------------------------------------------------------------------
def tax_summary(conn, user: dict, start: str, end: str) -> dict:
    """General sales tax collected vs recoverable input tax (business only).
    Personal expenses are never included.  Figures support — but do not
    replace — a filing prepared or reviewed by a qualified Jordanian accountant."""
    require(conn, user, "business", "reports", "read")
    out_acct = system_account(conn, "business", "sales_tax_payable")["id"]
    in_acct = system_account(conn, "business", "input_tax")["id"]
    out_d, out_c = ledger.account_movement(conn, out_acct, start, end)
    in_d, in_c = ledger.account_movement(conn, in_acct, start, end)
    pid = profile_id(conn, "business")
    tax_cat = conn.execute("SELECT id FROM expense_categories WHERE profile_kind='business' AND code='taxes'").fetchone()["id"]
    taxes_paid = ledger.category_totals(conn, pid, "expense", start, end).get(tax_cat, 0)
    return {"start": start, "end": end, "output_tax_minor": out_c - out_d, "input_tax_minor": in_d - in_c,
            "net_payable_minor": (out_c - out_d) - (in_d - in_c),
            "balance_payable_minor": ledger.account_balance(conn, out_acct, end),
            "other_taxes_expensed_minor": taxes_paid,
            "notice_key": "report.tax_disclaimer"}


def owner_equity_report(conn, user: dict, start: str, end: str) -> dict:
    require(conn, user, "business", "reports", "read")
    items = rows(conn, """SELECT o.*, t.description, t.transfer_type FROM owner_equity_transactions o
                          JOIN transfers t ON t.id=o.transfer_id WHERE o.txn_date BETWEEN ? AND ?
                          ORDER BY o.txn_date, o.id""", (start, end))
    totals: dict[str, int] = defaultdict(int)
    for i in items:
        totals[i["kind"]] += i["amount_minor"]
    return {"start": start, "end": end, "items": items, "totals": dict(totals)}


def inventory_valuation(conn, user: dict, as_of: str) -> dict:
    """Book value of inventory from the ledger.  Item-level valuation (by
    product, FIFO/average cost) belongs to the inventory module and will
    replace this figure once that module posts to the inventory account."""
    require(conn, user, "business", "reports", "read")
    acct = system_account(conn, "business", "inventory")["id"]
    return {"as_of": as_of, "book_value_minor": ledger.account_balance(conn, acct, as_of),
            "item_level_available": False}


def reconciliation_report(conn, user: dict, profile: str) -> list[dict]:
    require(conn, user, profile, "reconciliation", "read")
    return rows(conn, """SELECT r.*, a.name_en, a.name_ar, a.account_type FROM account_reconciliations r
                         JOIN financial_accounts a ON a.id=r.account_id WHERE a.profile_id=?
                         ORDER BY r.statement_date DESC, r.id DESC""", (profile_id(conn, profile),))


def savings_report(conn, user: dict, start: str, end: str, profile: str = "personal") -> dict:
    require(conn, user, profile, "reports", "read")
    pid = profile_id(conn, profile)
    accts = []
    for a in rows(conn, "SELECT * FROM financial_accounts WHERE profile_id=? AND account_type IN ('savings','investment')", (pid,)):
        d, c = ledger.account_movement(conn, a["id"], start, end)
        accts.append({**a, "balance_minor": ledger.account_balance(conn, a["id"], end), "net_in_minor": d - c})
    inc = sum(ledger.category_totals(conn, pid, "income", start, end).values())
    saved = sum(x["net_in_minor"] for x in accts)
    return {"start": start, "end": end, "accounts": accts, "total_saved_minor": saved,
            "income_minor": inc, "savings_rate_pct": _pct(saved, inc)}


def monthly_summary(conn, user: dict, profile: str, month: str) -> dict:
    m_start, m_end = dates.month_bounds(month)
    s, e = m_start.isoformat(), m_end.isoformat()
    stmt = income_statement(conn, user, profile, s, e)
    bs_end = balance_sheet(conn, user, profile, e)
    bs_prev = balance_sheet(conn, user, profile, (m_start - timedelta(days=1)).isoformat())
    out = {"month": s[:7], "income_minor": stmt["total_income_minor"],
           "expenses_minor": stmt["total_expenses_minor"] + stmt["total_cost_of_sales_minor"],
           "net_minor": stmt["net_minor"], "net_worth_minor": bs_end["net_worth_minor"],
           "net_worth_change_minor": bs_end["net_worth_minor"] - bs_prev["net_worth_minor"],
           "liquid_minor": bs_end["liquid_minor"], "top_expenses": stmt["expenses"][:5]}
    if profile == "personal":
        out["savings"] = savings_report(conn, user, s, e)
    return out


# ---------------------------------------------------------------------------
# Combined owner overview
# ---------------------------------------------------------------------------
def upcoming_payments(conn, user: dict, profile: str, today: str, days: int = 30) -> list[dict]:
    pid = profile_id(conn, profile)
    end = (date.fromisoformat(today) + timedelta(days=days)).isoformat()
    out = [{"date": o["date"], "label": o["payee"], "amount_minor": o["amount_minor"], "kind": "bill",
            "overdue": o["overdue"], "projected": o["projected"]}
           for o in bills_mod.projected_bill_occurrences(conn, pid, today, end)]
    for inst in rows(conn, """SELECT i.*, l.lender, l.borrower, l.role FROM loan_installments i JOIN loans l ON l.id=i.loan_id
                              WHERE l.profile_id=? AND l.role='borrower' AND i.status NOT IN ('paid','waived')
                              AND i.due_date <= ?""", (pid, end)):
        out.append({"date": max(inst["due_date"], today), "label": inst["lender"], "kind": "loan",
                    "amount_minor": inst["principal_minor"] + inst["interest_minor"]
                    - inst["paid_principal_minor"] - inst["paid_interest_minor"],
                    "overdue": inst["due_date"] < today, "projected": False})
    for d in debts.list_debts(conn, user, profile):
        if d["direction"] == "payable":
            for due, amt in debts.outstanding_pieces(conn, d):
                if due <= end:
                    out.append({"date": max(due, today), "label": d["creditor"], "kind": "debt",
                                "amount_minor": amt, "overdue": due < today, "projected": False})
    return sorted(out, key=lambda x: x["date"])


def combined_overview(conn, user: dict, today: str) -> dict:
    """Owner-only.  Shows business and personal side by side; combined
    figures are limited to balance-sheet totals."""
    require(conn, user, "business", "overview", "read")
    require(conn, user, "personal", "overview", "read")
    sides = {}
    for kind in ("business", "personal"):
        bs = balance_sheet(conn, user, kind, today)
        up = upcoming_payments(conn, user, kind, today, 30)
        sides[kind] = {"total_assets_minor": bs["total_assets_minor"],
                       "total_liabilities_minor": bs["total_liabilities_minor"],
                       "net_worth_minor": bs["net_worth_minor"], "liquid_minor": bs["liquid_minor"],
                       "obligations_30d_minor": sum(u["amount_minor"] for u in up), "upcoming": up[:10],
                       "goals": planning.list_goals(conn, user, kind, today)}
    combined = {k: sides["business"][k] + sides["personal"][k]
                for k in ("total_assets_minor", "total_liabilities_minor", "net_worth_minor", "liquid_minor",
                          "obligations_30d_minor")}
    return {"as_of": today, "business": sides["business"], "personal": sides["personal"], "combined": combined,
            "note_key": "overview.no_double_count"}


def loan_balances(conn, user: dict, profile: str, today: str) -> list[dict]:
    return loans.list_loans(conn, user, profile, today)


def accounts_snapshot(conn, user: dict, profile: str, as_of: str) -> list[dict]:
    return accounts_mod.list_accounts(conn, user, profile, as_of=as_of)
