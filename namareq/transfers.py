"""Transfers: owner <-> shop workflows and internal account transfers.

Each owner workflow posts one balanced entry in the business profile and one
in the personal profile, linked by a ``transfers`` row.  The caller must pick
the transfer type explicitly — there is no default — so personal money is
never booked as sales revenue and personal spending never reduces business
profit.

Posting map (B = business entry, P = personal entry):

capital_contribution / additional_investment
    B: Dr business cash/bank         Cr Owner capital contributions (equity)
    P: Dr Capital invested in shop   Cr personal cash/bank
owner_withdrawal
    B: Dr Owner withdrawals (equity) Cr business cash/bank
    P: Dr personal cash/bank         Cr income "Withdrawals from the shop"
owner_salary
    B: Dr expense "Owner salary"     Cr business cash/bank
    P: Dr personal cash/bank         Cr income "Salary"
profit_distribution
    B: Dr Profit distributions       Cr business cash/bank
    P: Dr personal cash/bank         Cr income "Business profit distribution"
reimburse_owner  (shop repays the owner for business costs the owner paid)
    B: Dr Due to owner               Cr business cash/bank
    P: Dr personal cash/bank         Cr Due from the shop
owner_repays_business  (owner repays personal costs the shop paid)
    B: Dr business cash/bank         Cr Due from owner
    P: Dr Due to the shop            Cr personal cash/bank
owner_loan_to_business
    B: Dr business cash/bank         Cr Loan payable to owner
    P: Dr Loan to the shop           Cr personal cash/bank
business_loan_repayment_to_owner (principal + optional interest)
    B: Dr Loan payable to owner, Dr expense "Loan interest"   Cr business cash/bank
    P: Dr personal cash/bank   Cr Loan to the shop, Cr income "Interest received"
business_loan_to_owner
    B: Dr Loan receivable from owner Cr business cash/bank
    P: Dr personal cash/bank         Cr Loan from the shop
owner_loan_repayment_to_business (principal + optional interest)
    B: Dr business cash/bank   Cr Loan receivable from owner, Cr income "Interest income"
    P: Dr Loan from the shop, Dr expense "Loan interest"   Cr personal cash/bank
business_paid_from_personal  (e.g. owner pays a supplier from a personal card)
    B: Dr business expense or account   Cr Due to owner  (or Owner contributions)
    P: Dr Due from the shop (or Capital invested in shop)  Cr personal account
personal_paid_from_business  (e.g. shop pays the owner's home electricity)
    B: Dr Owner withdrawals (or Due from owner)   Cr business cash/bank
    P: Dr personal expense category   Cr income "Withdrawals from the shop"
                                      (or Cr Due to the shop)
"""
from __future__ import annotations

import json

from . import ledger
from .db import (FinanceError, account, audit, expense_category, income_category, profile_id,
                 rows, system_account, transaction)
from .permissions import can_access, require

OWNER_TRANSFER_TYPES = (
    "capital_contribution",
    "additional_investment",
    "owner_withdrawal",
    "owner_salary",
    "profit_distribution",
    "reimburse_owner",
    "owner_repays_business",
    "owner_loan_to_business",
    "business_loan_repayment_to_owner",
    "business_loan_to_owner",
    "owner_loan_repayment_to_business",
    "business_paid_from_personal",
    "personal_paid_from_business",
)

EQUITY_KIND = {
    "capital_contribution": "contribution",
    "additional_investment": "additional_investment",
    "owner_withdrawal": "withdrawal",
    "owner_salary": "salary",
    "profit_distribution": "distribution",
    "owner_loan_to_business": "loan_in",
    "business_loan_repayment_to_owner": "loan_repayment",
}

MONEY_ACCOUNT_TYPES = ("business_cash", "personal_cash", "business_bank", "personal_bank",
                       "savings", "credit_card", "card_clearing")


def _money_account(conn, account_id, kind: str):
    if account_id is None:
        raise FinanceError("error.account_required")
    acct = account(conn, int(account_id))
    if acct["profile_id"] != profile_id(conn, kind):
        raise FinanceError("error.account_wrong_profile")
    if acct["account_type"] not in MONEY_ACCOUNT_TYPES:
        raise FinanceError("error.invalid_payment_account")
    return acct


def _sys(conn, kind, code) -> int:
    return system_account(conn, kind, code)["id"]


def retained_profit_available(conn, as_of: str | None = None) -> int:
    """Cumulative business net profit less distributions already paid."""
    pid = profile_id(conn, "business")
    inc = sum(ledger.category_totals(conn, pid, "income", None, as_of).values())
    exp = sum(ledger.category_totals(conn, pid, "expense", None, as_of).values())
    dist = ledger.account_balance(conn, _sys(conn, "business", "profit_distributions"), as_of)
    # profit_distributions is equity (credit-normal); distributions are debits.
    return inc - exp + dist


def owner_transfer(conn, user: dict, *, transfer_type: str, txn_date: str, amount_minor: int,
                   description: str, business_account_id: int | None = None,
                   personal_account_id: int | None = None, reference: str | None = None,
                   interest_minor: int = 0, business_expense_category_id: int | None = None,
                   business_target_account_id: int | None = None,
                   personal_expense_category_id: int | None = None,
                   settle_as: str | None = None, allow_exceeding_profit: bool = False,
                   payee: str | None = None) -> int:
    if transfer_type not in OWNER_TRANSFER_TYPES:
        raise FinanceError("error.transfer_type_required")
    require(conn, user, "business", "transfers", "write")
    require(conn, user, "personal", "transfers", "write")
    if amount_minor <= 0:
        raise FinanceError("error.amount_must_be_positive")
    if not description or not description.strip():
        raise FinanceError("error.description_required")
    txn_date = ledger.validate_date(txn_date)
    t = transfer_type
    B, P = [], []          # lines for business / personal entries
    b_acct = p_acct = None
    details: dict = {}

    needs_b_money = t not in ("business_paid_from_personal",)
    needs_p_money = t not in ("personal_paid_from_business",)
    if needs_b_money:
        b_acct = _money_account(conn, business_account_id, "business")
    if needs_p_money:
        p_acct = _money_account(conn, personal_account_id, "personal")
    currency = (b_acct or p_acct)["currency"]
    if b_acct and p_acct and b_acct["currency"] != p_acct["currency"]:
        raise FinanceError("error.currency_mismatch", account=p_acct["name_en"])
    amt = amount_minor

    if t in ("capital_contribution", "additional_investment"):
        B = [ledger.dr(b_acct["id"], amt), ledger.cr(_sys(conn, "business", "owner_contributions"), amt)]
        P = [ledger.dr(_sys(conn, "personal", "capital_in_shop"), amt), ledger.cr(p_acct["id"], amt)]
    elif t == "owner_withdrawal":
        B = [ledger.dr(_sys(conn, "business", "owner_withdrawals"), amt), ledger.cr(b_acct["id"], amt)]
        P = [ledger.dr(p_acct["id"], amt),
             ledger.cr_income(income_category(conn, "personal", "owner_draws")["id"], amt)]
    elif t == "owner_salary":
        B = [ledger.dr_expense(expense_category(conn, "business", "owner_salary")["id"], amt),
             ledger.cr(b_acct["id"], amt)]
        P = [ledger.dr(p_acct["id"], amt),
             ledger.cr_income(income_category(conn, "personal", "salary")["id"], amt)]
    elif t == "profit_distribution":
        available = retained_profit_available(conn, txn_date)
        details["profit_available"] = available
        if amt > available and not allow_exceeding_profit:
            raise FinanceError("error.distribution_exceeds_profit", available=available)
        B = [ledger.dr(_sys(conn, "business", "profit_distributions"), amt), ledger.cr(b_acct["id"], amt)]
        P = [ledger.dr(p_acct["id"], amt),
             ledger.cr_income(income_category(conn, "personal", "owner_distribution")["id"], amt)]
    elif t == "reimburse_owner":
        owed = ledger.account_balance(conn, _sys(conn, "business", "due_to_owner"), txn_date)
        if amt > owed:
            raise FinanceError("error.exceeds_outstanding_balance", outstanding=owed)
        B = [ledger.dr(_sys(conn, "business", "due_to_owner"), amt), ledger.cr(b_acct["id"], amt)]
        P = [ledger.dr(p_acct["id"], amt), ledger.cr(_sys(conn, "personal", "due_from_business"), amt)]
    elif t == "owner_repays_business":
        owed = ledger.account_balance(conn, _sys(conn, "business", "due_from_owner"), txn_date)
        if amt > owed:
            raise FinanceError("error.exceeds_outstanding_balance", outstanding=owed)
        B = [ledger.dr(b_acct["id"], amt), ledger.cr(_sys(conn, "business", "due_from_owner"), amt)]
        P = [ledger.dr(_sys(conn, "personal", "due_to_business"), amt), ledger.cr(p_acct["id"], amt)]
    elif t == "owner_loan_to_business":
        B = [ledger.dr(b_acct["id"], amt), ledger.cr(_sys(conn, "business", "loan_from_owner"), amt)]
        P = [ledger.dr(_sys(conn, "personal", "loan_to_shop"), amt), ledger.cr(p_acct["id"], amt)]
    elif t == "business_loan_repayment_to_owner":
        if interest_minor < 0 or interest_minor > amt:
            raise FinanceError("error.invalid_interest_split")
        principal = amt - interest_minor
        owed = ledger.account_balance(conn, _sys(conn, "business", "loan_from_owner"), txn_date)
        if principal > owed:
            raise FinanceError("error.exceeds_outstanding_balance", outstanding=owed)
        details.update(principal=principal, interest=interest_minor)
        B = [ledger.dr(_sys(conn, "business", "loan_from_owner"), principal),
             ledger.dr_expense(expense_category(conn, "business", "interest_expense")["id"], interest_minor),
             ledger.cr(b_acct["id"], amt)]
        P = [ledger.dr(p_acct["id"], amt), ledger.cr(_sys(conn, "personal", "loan_to_shop"), principal),
             ledger.cr_income(income_category(conn, "personal", "interest_income")["id"], interest_minor)]
    elif t == "business_loan_to_owner":
        B = [ledger.dr(_sys(conn, "business", "loan_to_owner"), amt), ledger.cr(b_acct["id"], amt)]
        P = [ledger.dr(p_acct["id"], amt), ledger.cr(_sys(conn, "personal", "loan_from_shop"), amt)]
    elif t == "owner_loan_repayment_to_business":
        if interest_minor < 0 or interest_minor > amt:
            raise FinanceError("error.invalid_interest_split")
        principal = amt - interest_minor
        owed = ledger.account_balance(conn, _sys(conn, "business", "loan_to_owner"), txn_date)
        if principal > owed:
            raise FinanceError("error.exceeds_outstanding_balance", outstanding=owed)
        details.update(principal=principal, interest=interest_minor)
        B = [ledger.dr(b_acct["id"], amt), ledger.cr(_sys(conn, "business", "loan_to_owner"), principal),
             ledger.cr_income(income_category(conn, "business", "interest_income")["id"], interest_minor)]
        P = [ledger.dr(_sys(conn, "personal", "loan_from_shop"), principal),
             ledger.dr_expense(expense_category(conn, "personal", "interest_expense")["id"], interest_minor),
             ledger.cr(p_acct["id"], amt)]
    elif t == "business_paid_from_personal":
        settle_as = settle_as or "due_to_owner"
        if settle_as not in ("due_to_owner", "capital_contribution"):
            raise FinanceError("error.invalid_settlement")
        if (business_expense_category_id is None) == (business_target_account_id is None):
            raise FinanceError("error.business_target_required")
        if business_expense_category_id is not None:
            target = ledger.dr_expense(
                expense_category(conn, "business", int(business_expense_category_id))["id"], amt)
        else:
            tgt = account(conn, int(business_target_account_id))
            if tgt["profile_id"] != profile_id(conn, "business"):
                raise FinanceError("error.account_wrong_profile")
            target = ledger.dr(tgt["id"], amt)
        if settle_as == "due_to_owner":
            B = [target, ledger.cr(_sys(conn, "business", "due_to_owner"), amt)]
            P = [ledger.dr(_sys(conn, "personal", "due_from_business"), amt), ledger.cr(p_acct["id"], amt)]
        else:
            B = [target, ledger.cr(_sys(conn, "business", "owner_contributions"), amt)]
            P = [ledger.dr(_sys(conn, "personal", "capital_in_shop"), amt), ledger.cr(p_acct["id"], amt)]
        details["settle_as"] = settle_as
    elif t == "personal_paid_from_business":
        settle_as = settle_as or "withdrawal"
        if settle_as not in ("withdrawal", "due_from_owner"):
            raise FinanceError("error.invalid_settlement")
        if personal_expense_category_id is None:
            raise FinanceError("error.personal_category_required")
        pcat = expense_category(conn, "personal", int(personal_expense_category_id))["id"]
        if settle_as == "withdrawal":
            B = [ledger.dr(_sys(conn, "business", "owner_withdrawals"), amt), ledger.cr(b_acct["id"], amt)]
            P = [ledger.dr_expense(pcat, amt),
                 ledger.cr_income(income_category(conn, "personal", "owner_draws")["id"], amt)]
        else:
            B = [ledger.dr(_sys(conn, "business", "due_from_owner"), amt), ledger.cr(b_acct["id"], amt)]
            P = [ledger.dr_expense(pcat, amt), ledger.cr(_sys(conn, "personal", "due_to_business"), amt)]
        details["settle_as"] = settle_as
    if payee:
        details["payee"] = payee

    bpid, ppid = profile_id(conn, "business"), profile_id(conn, "personal")
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO transfers(transfer_type, txn_date, amount_minor, currency,
                   business_account_id, personal_account_id, description, reference, details_json,
                   created_by) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (t, txn_date, amt, currency, b_acct["id"] if b_acct else None,
             p_acct["id"] if p_acct else None, description.strip(), reference,
             json.dumps(details), user["id"]),
        )
        tid = cur.lastrowid
        # The shop's books must not reveal what a personal payment was for.
        b_desc = description.strip()
        if t == "personal_paid_from_business":
            b_desc = ("Owner withdrawal (personal payment)" if settle_as == "withdrawal"
                      else "Amount due from owner (personal payment)")
        b_entry = ledger.post_entry(conn, profile_id=bpid, entry_date=txn_date, entry_type=t,
                                    description=b_desc, lines=B, currency=currency,
                                    user_id=user["id"], reference=reference, transfer_id=tid)
        p_entry = ledger.post_entry(conn, profile_id=ppid, entry_date=txn_date, entry_type=t,
                                    description=description.strip(), lines=P, currency=currency,
                                    user_id=user["id"], reference=reference, transfer_id=tid)
        conn.execute("UPDATE transfers SET business_entry_id=?, personal_entry_id=? WHERE id=?",
                     (b_entry, p_entry, tid))
        kind = EQUITY_KIND.get(t)
        if t == "business_paid_from_personal" and settle_as == "capital_contribution":
            kind = "contribution"
        if t == "personal_paid_from_business" and settle_as == "withdrawal":
            kind = "withdrawal"
        if kind:
            conn.execute(
                """INSERT INTO owner_equity_transactions(transfer_id, profile_id, kind, txn_date,
                       amount_minor, currency) VALUES (?,?,?,?,?,?)""",
                (tid, bpid, kind, txn_date, amt, currency))
        audit(conn, user["id"], "transfers", tid, "create", {"type": t, "amount": amt})
    return tid


def internal_transfer(conn, user: dict, *, profile: str, from_account_id: int, to_account_id: int,
                      txn_date: str, amount_minor: int, description: str,
                      reference: str | None = None) -> int:
    """Move money between two accounts of the SAME profile (e.g. bank to
    savings, or paying a credit card).  Cross-profile moves must use
    ``owner_transfer`` with an explicit type."""
    require(conn, user, profile, "transfers", "write")
    pid = profile_id(conn, profile)
    src, dst = account(conn, from_account_id), account(conn, to_account_id)
    if src["profile_id"] != pid or dst["profile_id"] != pid:
        raise FinanceError("error.cross_profile_requires_owner_transfer")
    if src["id"] == dst["id"]:
        raise FinanceError("error.same_account")
    if src["currency"] != dst["currency"]:
        raise FinanceError("error.currency_mismatch", account=dst["name_en"])
    if amount_minor <= 0:
        raise FinanceError("error.amount_must_be_positive")
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO transfers(transfer_type, txn_date, amount_minor, currency, from_account_id,
                   to_account_id, description, reference, created_by)
               VALUES ('internal',?,?,?,?,?,?,?,?)""",
            (txn_date, amount_minor, src["currency"], src["id"], dst["id"], description, reference,
             user["id"]))
        tid = cur.lastrowid
        entry = ledger.post_entry(conn, profile_id=pid, entry_date=txn_date, entry_type="internal_transfer",
                                  description=description,
                                  lines=[ledger.dr(dst["id"], amount_minor), ledger.cr(src["id"], amount_minor)],
                                  currency=src["currency"], user_id=user["id"], reference=reference,
                                  transfer_id=tid)
        col = "business_entry_id" if profile == "business" else "personal_entry_id"
        conn.execute(f"UPDATE transfers SET {col}=? WHERE id=?", (entry, tid))
    return tid


def reverse_transfer(conn, user: dict, transfer_id: int, reversal_date: str, reason: str) -> None:
    tr = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
    if tr is None:
        raise FinanceError("error.unknown_record")
    if tr["status"] == "reversed":
        raise FinanceError("error.already_reversed")
    for kind, col in (("business", "business_entry_id"), ("personal", "personal_entry_id")):
        if tr[col]:
            require(conn, user, kind, "transfers", "write")
    with transaction(conn):
        for col in ("business_entry_id", "personal_entry_id"):
            if tr[col]:
                ledger.reverse_entry(conn, tr[col], user_id=user["id"], reversal_date=reversal_date,
                                     reason=reason)
        conn.execute("UPDATE transfers SET status='reversed', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                     (transfer_id,))
        conn.execute(
            """INSERT INTO owner_equity_transactions(transfer_id, profile_id, kind, txn_date, amount_minor, currency)
               SELECT transfer_id, profile_id, kind, ?, -amount_minor, currency
               FROM owner_equity_transactions WHERE transfer_id=?""",
            (reversal_date, transfer_id))
        audit(conn, user["id"], "transfers", transfer_id, "reverse", {"reason": reason})


def list_transfers(conn, user: dict, profile: str) -> list[dict]:
    """Internal transfers of ``profile``; owner transfers touch both profiles
    and are only listed for users allowed to see both."""
    require(conn, user, profile, "transfers", "read")
    both = can_access(conn, user, "business") and can_access(conn, user, "personal")
    col = "business_entry_id" if profile == "business" else "personal_entry_id"
    sql = f"SELECT * FROM transfers WHERE {col} IS NOT NULL"
    if not both:
        sql += " AND transfer_type = 'internal'"
    return rows(conn, sql + " ORDER BY txn_date DESC, id DESC")
