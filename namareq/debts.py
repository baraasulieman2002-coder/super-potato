"""Debts and receivables with payment history and aging."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from . import ledger
from .accounts import opening_equity_account
from .db import (FinanceError, account, audit, expense_category, income_category, profile_id,
                 rows, system_account, transaction)
from .money import to_minor
from .permissions import require, require_owner

# debt_type -> (profile, direction, default system account)
DEBT_TYPES = {
    "customer": ("business", "receivable", "customer_receivables"),
    "supplier": ("business", "payable", "supplier_payables"),
    "employee_advance": ("business", "receivable", "employee_advances"),
    "business_other_receivable": ("business", "receivable", "other_receivables"),
    "business_other_payable": ("business", "payable", "other_payables"),
    "owed_to_owner": ("personal", "receivable", "personal_receivables"),
    "owed_by_owner": ("personal", "payable", "personal_payables"),
    "personal_other_receivable": ("personal", "receivable", "other_receivables"),
    "personal_other_payable": ("personal", "payable", "other_payables"),
}
ORIGINS = ("credit_sale", "credit_purchase", "cash_lent", "cash_borrowed", "opening", "already_posted")
AGING_BUCKETS = ("current", "d1_30", "d31_60", "d61_90", "d90_plus")


def _debt(conn, debt_id) -> dict:
    row = conn.execute("SELECT * FROM debt_records WHERE id=?", (debt_id,)).fetchone()
    if row is None:
        raise FinanceError("error.unknown_record")
    return dict(row)


def _kind(conn, pid):
    return conn.execute("SELECT kind FROM financial_profiles WHERE id=?", (pid,)).fetchone()["kind"]


def create_debt(conn, user: dict, *, debt_type: str, debtor: str, creditor: str,
                original_minor: int, issue_date: str, origin: str, due_date: str | None = None,
                account_id: int | None = None, cash_account_id: int | None = None,
                income_category_id: int | None = None, expense_category_id: int | None = None,
                installment_plan: list[dict] | None = None, annual_rate: str = "0",
                reference: str | None = None, notes: str | None = None) -> int:
    """Create a debt and post how it arose (``origin``):

    credit_sale      receivable: Dr receivable / Cr income category
    credit_purchase  payable:    Dr expense category / Cr payable
    cash_lent        receivable: Dr receivable / Cr cash account
    cash_borrowed    payable:    Dr cash account / Cr payable
    opening          balance carried in from before Namareq (opening equity)
    already_posted   the ledger already holds it (e.g. an unpaid expense)
    """
    if debt_type not in DEBT_TYPES:
        raise FinanceError("error.invalid_debt_type")
    profile, direction, default_code = DEBT_TYPES[debt_type]
    require(conn, user, profile, "debts", "write")
    if origin not in ORIGINS:
        raise FinanceError("error.invalid_origin")
    if original_minor <= 0:
        raise FinanceError("error.amount_must_be_positive")
    issue_date = ledger.validate_date(issue_date)
    if due_date:
        due_date = ledger.validate_date(due_date)
        if due_date < issue_date:
            raise FinanceError("error.invalid_date_range")
    pid = profile_id(conn, profile)
    account_id = account_id or system_account(conn, profile, default_code)["id"]
    acct = account(conn, account_id)
    if acct["profile_id"] != pid:
        raise FinanceError("error.account_wrong_profile")
    if installment_plan:
        plan_total = sum(int(i["amount_minor"]) for i in installment_plan)
        if plan_total != original_minor:
            raise FinanceError("error.plan_total_mismatch", plan=plan_total, original=original_minor)
        installment_plan = sorted(({"due_date": ledger.validate_date(i["due_date"]),
                                    "amount_minor": int(i["amount_minor"])} for i in installment_plan),
                                  key=lambda i: i["due_date"])
        due_date = due_date or installment_plan[-1]["due_date"]

    amt = original_minor
    is_recv = direction == "receivable"
    if origin == "credit_sale":
        if not is_recv or income_category_id is None:
            raise FinanceError("error.invalid_origin")
        cat = income_category(conn, profile, int(income_category_id))
        if cat["system_only"]:
            raise FinanceError("error.category_system_only", category=cat["name_en"])
        lines = [ledger.dr(account_id, amt), ledger.cr_income(cat["id"], amt)]
    elif origin == "credit_purchase":
        if is_recv or expense_category_id is None:
            raise FinanceError("error.invalid_origin")
        cat = expense_category(conn, profile, int(expense_category_id))
        lines = [ledger.dr_expense(cat["id"], amt), ledger.cr(account_id, amt)]
    elif origin in ("cash_lent", "cash_borrowed"):
        if (origin == "cash_lent") != is_recv or cash_account_id is None:
            raise FinanceError("error.invalid_origin")
        if account(conn, cash_account_id)["profile_id"] != pid:
            raise FinanceError("error.account_wrong_profile")
        lines = ([ledger.dr(account_id, amt), ledger.cr(cash_account_id, amt)] if is_recv
                 else [ledger.dr(cash_account_id, amt), ledger.cr(account_id, amt)])
    elif origin == "opening":
        oe = opening_equity_account(conn, profile, acct["currency"])
        lines = ([ledger.dr(account_id, amt), ledger.cr(oe, amt)] if is_recv
                 else [ledger.dr(oe, amt), ledger.cr(account_id, amt)])
    else:
        lines = None
    with transaction(conn):
        entry = None
        if lines:
            entry = ledger.post_entry(conn, profile_id=pid, entry_date=issue_date, entry_type="debt",
                                      description=f"Debt: {debtor} → {creditor}", lines=lines,
                                      currency=acct["currency"], user_id=user["id"], reference=reference)
        cur = conn.execute(
            """INSERT INTO debt_records(profile_id, direction, debt_type, debtor, creditor, original_minor,
                   currency, issue_date, due_date, installment_plan_json, annual_rate, account_id, origin,
                   reference, entry_id, notes, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, direction, debt_type, debtor.strip(), creditor.strip(), amt, acct["currency"], issue_date,
             due_date, json.dumps(installment_plan) if installment_plan else None, str(annual_rate),
             account_id, origin, reference, entry, notes, user["id"]))
        audit(conn, user["id"], "debt_records", cur.lastrowid, "create", {"origin": origin}, pid)
    return cur.lastrowid


def paid_total(conn, debt_id: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount_minor),0) FROM debt_payments WHERE debt_id=?",
                        (debt_id,)).fetchone()[0]


def remaining(conn, debt: dict) -> int:
    if debt["status"] == "written_off":
        return 0
    return debt["original_minor"] - paid_total(conn, debt["id"])


def record_payment(conn, user: dict, debt_id: int, *, amount_minor: int, pay_date: str,
                   cash_account_id: int, interest_minor: int = 0, notes: str | None = None) -> int:
    debt = _debt(conn, debt_id)
    kind = _kind(conn, debt["profile_id"])
    require(conn, user, kind, "debts", "write")
    if debt["status"] != "open":
        raise FinanceError("error.debt_not_open")
    if amount_minor <= 0 or interest_minor < 0:
        raise FinanceError("error.amount_must_be_positive")
    left = remaining(conn, debt)
    if amount_minor > left:
        raise FinanceError("error.exceeds_outstanding_balance", outstanding=left)
    if account(conn, cash_account_id)["profile_id"] != debt["profile_id"]:
        raise FinanceError("error.account_wrong_profile")
    total = amount_minor + interest_minor
    if debt["direction"] == "receivable":
        lines = [ledger.dr(cash_account_id, total), ledger.cr(debt["account_id"], amount_minor),
                 ledger.cr_income(income_category(conn, kind, "interest_income")["id"], interest_minor)]
    else:
        lines = [ledger.dr(debt["account_id"], amount_minor),
                 ledger.dr_expense(expense_category(conn, kind, "interest_expense")["id"], interest_minor),
                 ledger.cr(cash_account_id, total)]
    with transaction(conn):
        entry = ledger.post_entry(conn, profile_id=debt["profile_id"], entry_date=pay_date,
                                  entry_type="debt_payment",
                                  description=f"Debt payment: {debt['debtor']} → {debt['creditor']}",
                                  lines=lines, currency=debt["currency"], user_id=user["id"],
                                  related_entity_type="debt", related_entity_id=debt_id)
        cur = conn.execute(
            """INSERT INTO debt_payments(debt_id, pay_date, amount_minor, account_id, entry_id, notes, created_by)
               VALUES (?,?,?,?,?,?,?)""", (debt_id, pay_date, amount_minor, cash_account_id, entry, notes, user["id"]))
        if amount_minor == left:
            conn.execute("UPDATE debt_records SET status='settled', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                         (debt_id,))
    return cur.lastrowid


def write_off(conn, user: dict, debt_id: int, on: str, reason: str) -> int:
    """Write off an uncollectable receivable (owner decision only)."""
    require_owner(user)
    debt = _debt(conn, debt_id)
    kind = _kind(conn, debt["profile_id"])
    if debt["direction"] != "receivable" or debt["status"] != "open":
        raise FinanceError("error.debt_not_open")
    if not reason.strip():
        raise FinanceError("error.reason_required")
    left = remaining(conn, debt)
    with transaction(conn):
        entry = ledger.post_entry(
            conn, profile_id=debt["profile_id"], entry_date=on, entry_type="debt_write_off",
            description=f"Write-off: {debt['debtor']} — {reason}",
            lines=[ledger.dr_expense(expense_category(conn, kind, "bad_debts")["id"], left),
                   ledger.cr(debt["account_id"], left)],
            currency=debt["currency"], user_id=user["id"], related_entity_type="debt", related_entity_id=debt_id)
        conn.execute("UPDATE debt_records SET status='written_off', notes=COALESCE(notes,'') || ? WHERE id=?",
                     (f"\n[write-off {on}] {reason}", debt_id))
        audit(conn, user["id"], "debt_records", debt_id, "write_off", {"amount": left, "reason": reason},
              debt["profile_id"])
    return entry


def _bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "d1_30"
    if days_overdue <= 60:
        return "d31_60"
    if days_overdue <= 90:
        return "d61_90"
    return "d90_plus"


def outstanding_pieces(conn, debt: dict) -> list[tuple[str, int]]:
    """Unpaid amounts with their due dates.  With an installment plan,
    payments settle the oldest installments first."""
    left = remaining(conn, debt)
    if left <= 0:
        return []
    if not debt["installment_plan_json"]:
        return [(debt["due_date"] or debt["issue_date"], left)]
    paid = debt["original_minor"] - left
    pieces = []
    for inst in json.loads(debt["installment_plan_json"]):
        amt = inst["amount_minor"]
        cover = min(paid, amt)
        paid -= cover
        if amt - cover:
            pieces.append((inst["due_date"], amt - cover))
    return pieces


def aging_report(conn, user: dict, profile: str, direction: str, as_of: str) -> dict:
    require(conn, user, profile, "debts", "read")
    pid = profile_id(conn, profile)
    on = date.fromisoformat(as_of)
    totals = {b: 0 for b in AGING_BUCKETS}
    by_party: dict[str, dict] = defaultdict(lambda: {b: 0 for b in AGING_BUCKETS} | {"total": 0})
    for debt in rows(conn, "SELECT * FROM debt_records WHERE profile_id=? AND direction=? AND status='open'",
                     (pid, direction)):
        party = debt["debtor"] if direction == "receivable" else debt["creditor"]
        for due, amt in outstanding_pieces(conn, debt):
            b = _bucket((on - date.fromisoformat(due)).days)
            totals[b] += amt
            by_party[party][b] += amt
            by_party[party]["total"] += amt
    parties = sorted(({"party": k, **v} for k, v in by_party.items()), key=lambda r: -r["total"])
    return {"as_of": as_of, "direction": direction, "totals": totals,
            "grand_total": sum(totals.values()), "parties": parties}


def list_debts(conn, user: dict, profile: str, status: str | None = "open") -> list[dict]:
    require(conn, user, profile, "debts", "read")
    sql = "SELECT * FROM debt_records WHERE profile_id=?"
    params: list = [profile_id(conn, profile)]
    if status:
        sql += " AND status=?"
        params.append(status)
    out = []
    for d in rows(conn, sql + " ORDER BY COALESCE(due_date, issue_date)", params):
        d["remaining_minor"] = remaining(conn, d)
        d["paid_minor"] = paid_total(conn, d["id"])
        d["payments"] = rows(conn, "SELECT * FROM debt_payments WHERE debt_id=? ORDER BY pay_date", (d["id"],))
        out.append(d)
    return out


def owner_business_balances(conn, user: dict, as_of: str | None = None) -> dict:
    """Balances between owner and shop, read straight from the ledger."""
    require(conn, user, "business", "debts", "read")
    bal = lambda code: ledger.account_balance(conn, system_account(conn, "business", code)["id"], as_of)
    return {"business_owes_owner_minor": bal("due_to_owner") + bal("loan_from_owner"),
            "owner_owes_business_minor": bal("due_from_owner") + bal("loan_to_owner"),
            "due_to_owner_minor": bal("due_to_owner"), "loan_from_owner_minor": bal("loan_from_owner"),
            "due_from_owner_minor": bal("due_from_owner"), "loan_to_owner_minor": bal("loan_to_owner")}


def parse_plan(text: str, currency="JOD") -> list[dict]:
    """Parse 'YYYY-MM-DD amount' lines from a form into a plan."""
    plan = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            plan.append({"due_date": parts[0], "amount_minor": to_minor(parts[1], currency)})
    return plan
