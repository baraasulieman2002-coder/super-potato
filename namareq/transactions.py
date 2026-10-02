"""Income and expense transactions for both profiles."""
from __future__ import annotations

from datetime import date, timedelta

from . import ledger
from .db import (FinanceError, account, audit, expense_category, income_category, profile_id,
                 rows, system_account, transaction)
from .permissions import require

APPROVAL_THRESHOLD_MINOR = 200_000  # 200 JOD: non-owner business expenses above this need approval


def _check_account(conn, account_id: int, pid: int, cash_like_only: bool = False):
    acct = account(conn, account_id)
    if acct["profile_id"] != pid:
        raise FinanceError("error.account_wrong_profile")
    if cash_like_only and acct["account_type"] in ("owner_equity", "accumulated_depreciation"):
        raise FinanceError("error.invalid_payment_account")
    return acct


# ---------------------------------------------------------------------------
# Income
# ---------------------------------------------------------------------------
def record_income(conn, user: dict, *, profile: str, account_id: int, category_id: int,
                  txn_date: str, amount_minor: int, tax_minor: int = 0,
                  source_name: str | None = None, description: str | None = None,
                  reference: str | None = None, notes: str | None = None,
                  is_recurring: bool = False, recurring_id: int | None = None,
                  attachment_path: str | None = None, related_entity_type: str | None = None,
                  related_entity_id: int | None = None, source_type: str | None = None,
                  source_id: int | None = None, _system: bool = False) -> int:
    """Business: ``amount`` is net of sales tax and ``tax`` is collected
    general sales tax (credited to tax payable).
    Personal: ``amount`` is gross and ``tax`` is tax withheld at source."""
    require(conn, user, profile, "income", "write")
    pid = profile_id(conn, profile)
    cat = income_category(conn, profile, int(category_id))
    if cat["system_only"] and not _system:
        raise FinanceError("error.category_system_only", category=cat["name_en"])
    if amount_minor <= 0 or tax_minor < 0:
        raise FinanceError("error.amount_must_be_positive")
    acct = _check_account(conn, account_id, pid, cash_like_only=True)
    if source_type and conn.execute(
            "SELECT 1 FROM income_transactions WHERE source_type=? AND source_id=?",
            (source_type, source_id)).fetchone():
        raise FinanceError("error.duplicate_source", source=f"{source_type}#{source_id}")

    if profile == "business":
        tax_acct = system_account(conn, "business", "sales_tax_payable")["id"]
        lines = [ledger.dr(account_id, amount_minor + tax_minor), ledger.cr_income(cat["id"], amount_minor)]
        if tax_minor:
            lines.append(ledger.cr(tax_acct, tax_minor, "sales tax collected"))
    else:
        if tax_minor >= amount_minor:
            raise FinanceError("error.tax_exceeds_amount")
        tax_cat = expense_category(conn, "personal", "personal_taxes")["id"]
        lines = [ledger.dr(account_id, amount_minor - tax_minor), ledger.cr_income(cat["id"], amount_minor)]
        if tax_minor:
            lines.append(ledger.dr_expense(tax_cat, tax_minor, "tax withheld"))
    desc = description or f"{cat['name_en']}{' - ' + source_name if source_name else ''}"
    with transaction(conn):
        entry = ledger.post_entry(conn, profile_id=pid, entry_date=txn_date, entry_type="income",
                                  description=desc, lines=lines, currency=acct["currency"],
                                  user_id=user["id"], reference=reference, source_type=source_type,
                                  source_id=source_id, related_entity_type=related_entity_type,
                                  related_entity_id=related_entity_id)
        cur = conn.execute(
            """INSERT INTO income_transactions(profile_id, account_id, category_id, txn_date,
                   source_name, amount_minor, tax_minor, currency, description, is_recurring,
                   recurring_id, reference, notes, attachment_path, related_entity_type,
                   related_entity_id, source_type, source_id, entry_id, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, account_id, cat["id"], txn_date, source_name, amount_minor, tax_minor,
             acct["currency"], desc, int(is_recurring), recurring_id, reference, notes,
             attachment_path, related_entity_type, related_entity_id, source_type, source_id,
             entry, user["id"]),
        )
        audit(conn, user["id"], "income_transactions", cur.lastrowid, "create", None, pid)
    return cur.lastrowid


def post_shop_sale(conn, user: dict, *, sale_id: int, txn_date: str, account_id: int,
                   net_minor: int, tax_minor: int, category_code: str = "product_sales",
                   customer: str | None = None, reference: str | None = None) -> int:
    """Integration hook for the POS/sales module.  Each sale posts exactly
    once (unique source), and only through this path, so automatically
    generated shop sales are never manually duplicated as business income."""
    cat = income_category(conn, "business", category_code)
    return record_income(conn, user, profile="business", account_id=account_id,
                         category_id=cat["id"], txn_date=txn_date, amount_minor=net_minor,
                         tax_minor=tax_minor, source_name=customer, reference=reference,
                         source_type="sale", source_id=sale_id, related_entity_type="sale",
                         related_entity_id=sale_id, _system=True)


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------
def record_expense(conn, user: dict, *, profile: str, account_id: int, category_id: int,
                   txn_date: str, amount_minor: int, tax_minor: int = 0,
                   tax_recoverable: bool = False, payee: str | None = None,
                   subcategory: str | None = None, description: str | None = None,
                   reference: str | None = None, paid: bool = True,
                   payable_account_id: int | None = None, due_date: str | None = None,
                   is_recurring: bool = False, recurring_id: int | None = None,
                   bill_id: int | None = None, business_purpose: str | None = None,
                   attachment_path: str | None = None, notes: str | None = None,
                   related_entity_type: str | None = None,
                   related_entity_id: int | None = None) -> int:
    """Record an expense.  If ``paid`` is False the amount is owed: the credit
    goes to a payable account and ``pay_expense`` settles it later."""
    require(conn, user, profile, "expenses", "write")
    pid = profile_id(conn, profile)
    cat = expense_category(conn, profile, int(category_id))
    if amount_minor <= 0 or tax_minor < 0:
        raise FinanceError("error.amount_must_be_positive")
    acct = _check_account(conn, account_id, pid, cash_like_only=True)
    if profile == "personal" and tax_recoverable:
        # Personal expenses are never treated as recoverable business tax.
        raise FinanceError("error.personal_tax_not_recoverable")
    credit_acct = account_id
    if not paid:
        if payable_account_id is None:
            code = "supplier_payables" if profile == "business" else "personal_payables"
            payable_account_id = system_account(conn, profile, code)["id"]
        _check_account(conn, payable_account_id, pid)
        credit_acct = payable_account_id

    total = amount_minor + tax_minor
    lines = []
    if tax_recoverable and tax_minor:
        lines.append(ledger.dr_expense(cat["id"], amount_minor))
        lines.append(ledger.dr(system_account(conn, "business", "input_tax")["id"], tax_minor,
                               "recoverable input tax"))
    else:
        lines.append(ledger.dr_expense(cat["id"], total))
    lines.append(ledger.cr(credit_acct, total))

    approval = "not_required"
    if profile == "business" and user["role"] != "owner" and total > APPROVAL_THRESHOLD_MINOR:
        approval = "pending"
    desc = description or f"{cat['name_en']}{' - ' + payee if payee else ''}"
    with transaction(conn):
        entry = ledger.post_entry(conn, profile_id=pid, entry_date=txn_date, entry_type="expense",
                                  description=desc, lines=lines, currency=acct["currency"],
                                  user_id=user["id"], reference=reference,
                                  related_entity_type=related_entity_type,
                                  related_entity_id=related_entity_id)
        cur = conn.execute(
            """INSERT INTO expense_transactions(profile_id, account_id, category_id, subcategory,
                   txn_date, amount_minor, tax_minor, tax_recoverable, currency, payee, description,
                   reference, is_recurring, recurring_id, bill_id, due_date, paid_status,
                   business_purpose, approval_status, attachment_path, notes, related_entity_type,
                   related_entity_id, entry_id, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, account_id, cat["id"], subcategory, txn_date, amount_minor, tax_minor,
             int(tax_recoverable), acct["currency"], payee, desc, reference, int(is_recurring),
             recurring_id, bill_id, due_date, "paid" if paid else "unpaid", business_purpose,
             approval, attachment_path, notes, related_entity_type, related_entity_id, entry,
             user["id"]),
        )
        audit(conn, user["id"], "expense_transactions", cur.lastrowid, "create", None, pid)
    return cur.lastrowid


def pay_expense(conn, user: dict, expense_id: int, pay_date: str, account_id: int) -> int:
    exp = conn.execute("SELECT * FROM expense_transactions WHERE id=?", (expense_id,)).fetchone()
    if exp is None:
        raise FinanceError("error.unknown_record")
    kind = conn.execute("SELECT kind FROM financial_profiles WHERE id=?", (exp["profile_id"],)).fetchone()["kind"]
    require(conn, user, kind, "expenses", "write")
    if exp["paid_status"] == "paid" or exp["status"] != "posted":
        raise FinanceError("error.already_paid")
    _check_account(conn, account_id, exp["profile_id"], cash_like_only=True)
    payable = ledger.entry_lines(conn, exp["entry_id"])[-1]["account_id"]
    total = exp["amount_minor"] + exp["tax_minor"]
    with transaction(conn):
        entry = ledger.post_entry(conn, profile_id=exp["profile_id"], entry_date=pay_date,
                                  entry_type="expense_payment",
                                  description=f"Payment: {exp['description']}",
                                  lines=[ledger.dr(payable, total), ledger.cr(account_id, total)],
                                  currency=exp["currency"], user_id=user["id"],
                                  related_entity_type="expense", related_entity_id=expense_id)
        conn.execute("""UPDATE expense_transactions SET paid_status='paid', paid_entry_id=?,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?""",
                     (entry, expense_id))
    return entry


def set_approval(conn, user: dict, expense_id: int, approved: bool) -> None:
    if user["role"] != "owner":
        raise FinanceError("error.owner_only")
    with transaction(conn):
        conn.execute("""UPDATE expense_transactions SET approval_status=?,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                        WHERE id=? AND approval_status='pending'""",
                     ("approved" if approved else "rejected", expense_id))
        audit(conn, user["id"], "expense_transactions", expense_id,
              "approve" if approved else "reject")


def reverse_transaction(conn, user: dict, table: str, txn_id: int, reversal_date: str,
                        reason: str) -> int:
    """Correct an income/expense by reversal.  History is never overwritten."""
    if table not in ("income_transactions", "expense_transactions"):
        raise FinanceError("error.unknown_record")
    txn = conn.execute(f"SELECT * FROM {table} WHERE id=?", (txn_id,)).fetchone()
    if txn is None:
        raise FinanceError("error.unknown_record")
    kind = conn.execute("SELECT kind FROM financial_profiles WHERE id=?", (txn["profile_id"],)).fetchone()["kind"]
    require(conn, user, kind, table, "write")
    if txn["status"] == "reversed":
        raise FinanceError("error.already_reversed")
    with transaction(conn):
        rev = ledger.reverse_entry(conn, txn["entry_id"], user_id=user["id"],
                                   reversal_date=reversal_date, reason=reason)
        if table == "expense_transactions" and txn["paid_entry_id"]:
            ledger.reverse_entry(conn, txn["paid_entry_id"], user_id=user["id"],
                                 reversal_date=reversal_date, reason=reason)
        conn.execute(f"""UPDATE {table} SET status='reversed', reversal_entry_id=?,
                         updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?""", (rev, txn_id))
        audit(conn, user["id"], table, txn_id, "reverse", {"reason": reason}, txn["profile_id"])
    return rev


def find_duplicate_expenses(conn, profile: str, payee: str | None, amount_minor: int, on: str,
                            window_days: int = 3, exclude_id: int | None = None) -> list[dict]:
    if not payee:
        return []
    pid = profile_id(conn, profile)
    d = date.fromisoformat(on)
    return rows(conn, """SELECT * FROM expense_transactions WHERE profile_id=? AND status='posted'
                         AND lower(trim(payee)) = lower(trim(?)) AND amount_minor + tax_minor = ?
                         AND txn_date BETWEEN ? AND ? AND id IS NOT ?""",
                (pid, payee, amount_minor, (d - timedelta(days=window_days)).isoformat(),
                 (d + timedelta(days=window_days)).isoformat(), exclude_id))


def list_transactions(conn, user: dict, table: str, profile: str, start: str | None = None,
                      end: str | None = None, limit: int = 500) -> list[dict]:
    require(conn, user, profile, table, "read")
    pid = profile_id(conn, profile)
    cat_table = "income_categories" if table == "income_transactions" else "expense_categories"
    sql = f"""SELECT t.*, c.name_en cat_en, c.name_ar cat_ar, a.name_en acct_en, a.name_ar acct_ar
              FROM {table} t JOIN {cat_table} c ON c.id=t.category_id
              JOIN financial_accounts a ON a.id=t.account_id WHERE t.profile_id=?"""
    params: list = [pid]
    if start:
        sql += " AND t.txn_date >= ?"
        params.append(start)
    if end:
        sql += " AND t.txn_date <= ?"
        params.append(end)
    return rows(conn, sql + " ORDER BY t.txn_date DESC, t.id DESC LIMIT ?", [*params, limit])
