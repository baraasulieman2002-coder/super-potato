"""Financial accounts (bank, cash, cards, loans, equity ...)."""
from __future__ import annotations

from . import chart, ledger
from .db import FinanceError, audit, profile_id, rows, transaction
from .money import decimals_for
from .permissions import require


def mask_reference(ref: str | None) -> str | None:
    """Keep only the last four characters of an account/card number.  Full
    numbers, passwords and card security codes are never stored."""
    if not ref:
        return None
    cleaned = "".join(ch for ch in str(ref) if ch.isalnum())
    if len(cleaned) <= 4:
        return cleaned
    return "•••• " + cleaned[-4:]


def opening_equity_account(conn, kind: str, currency: str) -> int:
    """Opening-balance equity account for a profile/currency pair."""
    pid = profile_id(conn, kind)
    code = "opening_equity" if currency == "JOD" else f"opening_equity_{currency}"
    row = conn.execute("SELECT id FROM financial_accounts WHERE profile_id=? AND system_code=?",
                       (pid, code)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        """INSERT INTO financial_accounts(profile_id, account_type, name_en, name_ar, currency, system_code)
           VALUES (?, 'owner_equity', ?, ?, ?, ?)""",
        (pid, f"Opening balance equity ({currency})", f"حقوق ملكية افتتاحية ({currency})",
         currency, code),
    )
    return cur.lastrowid


def create_account(conn, user: dict, *, profile: str, account_type: str, name_en: str,
                   name_ar: str, currency: str = "JOD", opening_balance_minor: int = 0,
                   opening_date: str | None = None, institution: str | None = None,
                   account_ref: str | None = None, credit_limit_minor: int | None = None,
                   statement_day: int | None = None, payment_due_day: int | None = None,
                   interest_rate: str | None = None, notes: str | None = None) -> int:
    require(conn, user, profile, "accounts", "write")
    if account_type not in chart.ACCOUNT_TYPES:
        raise FinanceError("error.invalid_account_type")
    if profile not in chart.ACCOUNT_TYPES[account_type][1]:
        raise FinanceError("error.account_type_wrong_profile")
    if not name_en.strip() or not name_ar.strip():
        raise FinanceError("error.bilingual_name_required")
    decimals_for(currency)  # validates currency
    if opening_balance_minor and not opening_date:
        raise FinanceError("error.opening_date_required")
    pid = profile_id(conn, profile)
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO financial_accounts(profile_id, account_type, name_en, name_ar, currency,
                   opening_balance_minor, opening_date, institution, account_ref_masked,
                   credit_limit_minor, statement_day, payment_due_day, interest_rate, notes, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, account_type, name_en.strip(), name_ar.strip(), currency, opening_balance_minor,
             opening_date, institution, mask_reference(account_ref), credit_limit_minor,
             statement_day, payment_due_day, interest_rate, notes, user["id"]),
        )
        acct_id = cur.lastrowid
        if opening_balance_minor:
            oe = opening_equity_account(conn, profile, currency)
            amt = abs(opening_balance_minor)
            # A positive opening balance sits on the account's normal side.
            normal_debit = chart.is_debit_normal(account_type)
            if opening_balance_minor < 0:
                normal_debit = not normal_debit
            lines = ([ledger.dr(acct_id, amt), ledger.cr(oe, amt)] if normal_debit
                     else [ledger.dr(oe, amt), ledger.cr(acct_id, amt)])
            ledger.post_entry(conn, profile_id=pid, entry_date=opening_date,
                              entry_type="opening_balance",
                              description=f"Opening balance: {name_en}", lines=lines,
                              currency=currency, user_id=user["id"],
                              related_entity_type="financial_account", related_entity_id=acct_id)
        audit(conn, user["id"], "financial_accounts", acct_id, "create",
              {"type": account_type}, pid)
    return acct_id


UPDATABLE = ("name_en", "name_ar", "institution", "credit_limit_minor", "statement_day",
             "payment_due_day", "interest_rate", "notes", "is_active")


def update_account(conn, user: dict, account_id: int, **fields) -> None:
    """Edit descriptive fields.  Balances can only change through postings."""
    acct = conn.execute("SELECT * FROM financial_accounts WHERE id=?", (account_id,)).fetchone()
    if acct is None:
        raise FinanceError("error.unknown_account")
    kind = conn.execute("SELECT kind FROM financial_profiles WHERE id=?",
                        (acct["profile_id"],)).fetchone()["kind"]
    require(conn, user, kind, "accounts", "write")
    if "account_ref" in fields:
        fields["account_ref_masked"] = mask_reference(fields.pop("account_ref"))
    bad = set(fields) - set(UPDATABLE) - {"account_ref_masked"}
    if bad:
        raise FinanceError("error.field_not_editable", fields=",".join(sorted(bad)))
    if fields.get("is_active") == 0 and acct["system_code"]:
        raise FinanceError("error.system_account_locked")
    if not fields:
        return
    with transaction(conn):
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE financial_accounts SET {sets}, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                     [*fields.values(), account_id])
        audit(conn, user["id"], "financial_accounts", account_id, "update", fields, acct["profile_id"])


def list_accounts(conn, user: dict, profile: str, include_inactive=False,
                  as_of: str | None = None) -> list[dict]:
    require(conn, user, profile, "accounts", "read")
    pid = profile_id(conn, profile)
    sql = "SELECT * FROM financial_accounts WHERE profile_id = ?"
    if not include_inactive:
        sql += " AND is_active = 1"
    out = []
    for acct in rows(conn, sql + " ORDER BY system_code IS NOT NULL, account_type, name_en", (pid,)):
        acct["balance_minor"] = ledger.account_balance(conn, acct["id"], as_of)
        acct["class"] = chart.account_class(acct["account_type"])
        if acct["credit_limit_minor"]:
            acct["available_credit_minor"] = acct["credit_limit_minor"] - acct["balance_minor"]
        out.append(acct)
    return out


def liquid_balance(conn, profile: str, as_of: str | None = None, currency="JOD") -> int:
    pid = profile_id(conn, profile)
    total = 0
    for acct in conn.execute("SELECT id, account_type FROM financial_accounts WHERE profile_id=? AND currency=?",
                             (pid, currency)):
        if chart.is_liquid(acct["account_type"]):
            total += ledger.account_balance(conn, acct["id"], as_of)
    return total


def reconcile(conn, user: dict, account_id: int, statement_date: str, statement_balance_minor: int,
              kind: str = "bank_statement", post_adjustment: bool = False,
              notes: str | None = None) -> dict:
    """Compare a bank statement or a physical cash count with the ledger.
    An adjustment is only posted when the user explicitly asks for it."""
    acct = conn.execute("SELECT * FROM financial_accounts WHERE id=?", (account_id,)).fetchone()
    if acct is None:
        raise FinanceError("error.unknown_account")
    pkind = conn.execute("SELECT kind FROM financial_profiles WHERE id=?",
                         (acct["profile_id"],)).fetchone()["kind"]
    require(conn, user, pkind, "reconciliation", "write")
    statement_date = ledger.validate_date(statement_date)
    ledger_bal = ledger.account_balance(conn, account_id, statement_date)
    diff = statement_balance_minor - ledger_bal
    adj_entry = None
    with transaction(conn):
        if diff and post_adjustment:
            cat_code = "cash_over_short" if pkind == "business" else "misc_personal"
            cat = conn.execute("SELECT id FROM expense_categories WHERE profile_kind=? AND code=?",
                               (pkind, cat_code)).fetchone()["id"]
            amt = abs(diff)
            debit_normal = chart.is_debit_normal(acct["account_type"])
            increase = diff > 0
            # Increasing an asset (or decreasing a liability) is a gain.
            gain = increase if debit_normal else not increase
            acct_line = (ledger.dr(account_id, amt) if increase == debit_normal
                         else ledger.cr(account_id, amt))
            cat_line = ledger.cr_expense(cat, amt) if gain else ledger.dr_expense(cat, amt)
            adj_entry = ledger.post_entry(
                conn, profile_id=acct["profile_id"], entry_date=statement_date,
                entry_type="reconciliation_adjustment",
                description=f"Reconciliation adjustment: {acct['name_en']}",
                lines=[acct_line, cat_line], currency=acct["currency"], user_id=user["id"])
        cur = conn.execute(
            """INSERT INTO account_reconciliations(account_id, kind, statement_date,
                   statement_balance_minor, ledger_balance_minor, difference_minor, status,
                   adjustment_entry_id, notes, reconciled_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (account_id, kind, statement_date, statement_balance_minor, ledger_bal, diff,
             "balanced" if diff == 0 else "difference", adj_entry, notes, user["id"]),
        )
    return {"id": cur.lastrowid, "ledger_balance": ledger_bal, "difference": diff,
            "adjustment_entry_id": adj_entry}
