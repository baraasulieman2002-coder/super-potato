"""Loans: borrowing and lending for both profiles, with schedules.

Payment allocation rule: each payment settles installments oldest-first,
interest before principal, for every installment due on or before the payment
date (or, when nothing is due yet, the next upcoming installment).  Anything
beyond that is an extra principal payment, after which the remaining schedule is rebuilt (see loan_math for
the assumptions).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from . import dates, ledger, loan_math, transfers
from .db import (FinanceError, account, audit, expense_category, income_category, profile_id,
                 rows, system_account, transaction)
from .permissions import require

LOAN_TYPES = ("business_loan", "personal_loan", "owner_loan_to_business", "business_loan_to_owner",
              "bank_loan", "family_friend_loan", "customer_financing", "employee_advance",
              "other_loan_receivable", "other_loan_payable")
OWNER_LINKED = ("owner_loan_to_business", "business_loan_to_owner")
INTEREST_METHODS = ("reducing_balance", "flat", "none")
LOAN_FREQUENCIES = ("weekly", "monthly", "quarterly", "semi_annually", "annually")


def _kind(conn, pid):
    return conn.execute("SELECT kind FROM financial_profiles WHERE id=?", (pid,)).fetchone()["kind"]


def _loan(conn, loan_id) -> dict:
    row = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if row is None:
        raise FinanceError("error.unknown_record")
    return dict(row)


def _validate_terms(principal_minor, annual_rate, method, frequency, term_count):
    if principal_minor <= 0:
        raise FinanceError("error.amount_must_be_positive")
    if method not in INTEREST_METHODS:
        raise FinanceError("error.invalid_interest_method")
    if frequency not in LOAN_FREQUENCIES:
        raise FinanceError("error.invalid_frequency")
    if term_count <= 0 or term_count > 600:
        raise FinanceError("error.invalid_term")
    try:
        rate = Decimal(str(annual_rate))
    except Exception as exc:
        raise FinanceError("error.invalid_rate") from exc
    if rate < 0 or rate > 100:
        raise FinanceError("error.invalid_rate")
    if method == "none" and rate != 0:
        raise FinanceError("error.invalid_rate")


def _insert_loan(conn, user, *, pid, role, borrower, lender, loan_type, principal_minor, currency,
                 annual_rate, method, start_date, first_payment_date, frequency, term_count,
                 cash_account_id, loan_account_id, fees_minor, collateral, early_fee_pct,
                 early_conditions, notes, counterpart=None, origination_entry=None) -> int:
    schedule = loan_math.build_schedule(principal_minor, annual_rate, term_count, frequency, method,
                                        first_payment_date)
    cur = conn.execute(
        """INSERT INTO loans(profile_id, role, borrower, lender, loan_type, principal_minor, currency,
               annual_rate, interest_method, start_date, first_payment_date, maturity_date, frequency,
               term_count, installment_minor, cash_account_id, loan_account_id, counterpart_loan_id,
               collateral, fees_minor, early_settlement_fee_pct, early_payment_conditions,
               origination_entry_id, notes, created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, role, borrower, lender, loan_type, principal_minor, currency, str(annual_rate), method,
         start_date, first_payment_date, schedule[-1].due_date, frequency, term_count,
         schedule[0].payment_minor, cash_account_id, loan_account_id, counterpart, collateral,
         fees_minor, str(early_fee_pct), early_conditions, origination_entry, notes, user["id"]))
    loan_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO loan_installments(loan_id, seq, due_date, principal_minor, interest_minor)
           VALUES (?,?,?,?,?)""",
        [(loan_id, r.seq, r.due_date, r.principal_minor, r.interest_minor) for r in schedule])
    audit(conn, user["id"], "loans", loan_id, "create", {"type": loan_type}, pid)
    return loan_id


def create_loan(conn, user: dict, *, profile: str, role: str, borrower: str, lender: str,
                loan_type: str, principal_minor: int, annual_rate, interest_method: str,
                start_date: str, first_payment_date: str, frequency: str, term_count: int,
                cash_account_id: int, loan_account_id: int | None = None, fees_minor: int = 0,
                collateral: str | None = None, early_settlement_fee_pct="0",
                early_payment_conditions: str | None = None, notes: str | None = None,
                post_origination: bool = True) -> int:
    """Create a loan with a third party.  ``post_origination`` posts the cash
    received (borrower) or paid out (lender); set it False for a loan that
    already exists and whose balance came in through an opening balance."""
    require(conn, user, profile, "loans", "write")
    if loan_type not in LOAN_TYPES:
        raise FinanceError("error.invalid_loan_type")
    if loan_type in OWNER_LINKED:
        raise FinanceError("error.use_owner_loan_workflow")
    if role not in ("borrower", "lender"):
        raise FinanceError("error.invalid_loan_role")
    _validate_terms(principal_minor, annual_rate, interest_method, frequency, term_count)
    start_date = ledger.validate_date(start_date)
    first_payment_date = ledger.validate_date(first_payment_date)
    if first_payment_date < start_date:
        raise FinanceError("error.invalid_date_range")
    pid = profile_id(conn, profile)
    cash = account(conn, cash_account_id)
    if cash["profile_id"] != pid:
        raise FinanceError("error.account_wrong_profile")
    if fees_minor < 0 or fees_minor >= principal_minor:
        raise FinanceError("error.invalid_fees")
    with transaction(conn):
        if loan_account_id is None:
            counterparty = lender if role == "borrower" else borrower
            acct_type = "loan_payable" if role == "borrower" else "loan_receivable"
            cur = conn.execute(
                """INSERT INTO financial_accounts(profile_id, account_type, name_en, name_ar, currency, created_by)
                   VALUES (?,?,?,?,?,?)""",
                (pid, acct_type, f"Loan — {counterparty}", f"قرض — {counterparty}", cash["currency"],
                 user["id"]))
            loan_account_id = cur.lastrowid
        lacct = account(conn, loan_account_id)
        if lacct["profile_id"] != pid:
            raise FinanceError("error.account_wrong_profile")
        expected = "loan_payable" if role == "borrower" else "loan_receivable"
        if lacct["account_type"] != expected:
            raise FinanceError("error.invalid_loan_account")
        entry = None
        if post_origination:
            fee_cat = expense_category(conn, profile, "bank_charges")["id"]
            if role == "borrower":
                lines = [ledger.dr(cash_account_id, principal_minor - fees_minor),
                         ledger.dr_expense(fee_cat, fees_minor, "loan fees"),
                         ledger.cr(loan_account_id, principal_minor)]
            else:
                lines = [ledger.dr(loan_account_id, principal_minor), ledger.cr(cash_account_id, principal_minor)]
            entry = ledger.post_entry(conn, profile_id=pid, entry_date=start_date,
                                      entry_type="loan_origination",
                                      description=f"Loan {borrower} ← {lender}", lines=lines,
                                      currency=cash["currency"], user_id=user["id"])
        loan_id = _insert_loan(
            conn, user, pid=pid, role=role, borrower=borrower, lender=lender, loan_type=loan_type,
            principal_minor=principal_minor, currency=cash["currency"], annual_rate=annual_rate,
            method=interest_method, start_date=start_date, first_payment_date=first_payment_date,
            frequency=frequency, term_count=term_count, cash_account_id=cash_account_id,
            loan_account_id=loan_account_id, fees_minor=fees_minor, collateral=collateral,
            early_fee_pct=early_settlement_fee_pct, early_conditions=early_payment_conditions,
            notes=notes, origination_entry=entry)
    return loan_id


def create_owner_business_loan(conn, user: dict, *, direction: str, principal_minor: int,
                               annual_rate, interest_method: str, start_date: str,
                               first_payment_date: str, frequency: str, term_count: int,
                               business_account_id: int, personal_account_id: int,
                               owner_name: str = "Owner", shop_name: str = "Shop",
                               notes: str | None = None) -> tuple[int, int]:
    """Owner lends to the shop (``owner_to_business``) or the shop lends to the
    owner (``business_to_owner``).  Posts one linked transfer and creates a
    mirrored loan record in each profile."""
    if direction not in ("owner_to_business", "business_to_owner"):
        raise FinanceError("error.invalid_loan_role")
    _validate_terms(principal_minor, annual_rate, interest_method, frequency, term_count)
    ttype = "owner_loan_to_business" if direction == "owner_to_business" else "business_loan_to_owner"
    bpid, ppid = profile_id(conn, "business"), profile_id(conn, "personal")
    with transaction(conn):
        tid = transfers.owner_transfer(
            conn, user, transfer_type=ttype, txn_date=start_date, amount_minor=principal_minor,
            description=f"{'Owner loan to shop' if direction == 'owner_to_business' else 'Shop loan to owner'}",
            business_account_id=business_account_id, personal_account_id=personal_account_id)
        if direction == "owner_to_business":
            b_role, p_role = "borrower", "lender"
            b_acct = system_account(conn, "business", "loan_from_owner")["id"]
            p_acct = system_account(conn, "personal", "loan_to_shop")["id"]
            borrower, lender = shop_name, owner_name
        else:
            b_role, p_role = "lender", "borrower"
            b_acct = system_account(conn, "business", "loan_to_owner")["id"]
            p_acct = system_account(conn, "personal", "loan_from_shop")["id"]
            borrower, lender = owner_name, shop_name
        common = dict(borrower=borrower, lender=lender, loan_type=ttype, principal_minor=principal_minor,
                      currency="JOD", annual_rate=annual_rate, method=interest_method,
                      start_date=start_date, first_payment_date=first_payment_date, frequency=frequency,
                      term_count=term_count, fees_minor=0, collateral=None, early_fee_pct="0",
                      early_conditions=None, notes=notes)
        b_loan = _insert_loan(conn, user, pid=bpid, role=b_role, cash_account_id=business_account_id,
                              loan_account_id=b_acct, **common)
        p_loan = _insert_loan(conn, user, pid=ppid, role=p_role, cash_account_id=personal_account_id,
                              loan_account_id=p_acct, counterpart=b_loan, **common)
        conn.execute("UPDATE loans SET counterpart_loan_id=? WHERE id=?", (p_loan, b_loan))
        conn.execute("UPDATE transfers SET details_json=json_set(COALESCE(details_json,'{}'),'$.loan_ids',json_array(?,?)) WHERE id=?",
                     (b_loan, p_loan, tid))
    return b_loan, p_loan


# ---------------------------------------------------------------------------
# Balances, allocation and payments
# ---------------------------------------------------------------------------
def outstanding_principal(conn, loan_id: int) -> int:
    row = conn.execute("""SELECT COALESCE(SUM(principal_minor - paid_principal_minor),0) FROM loan_installments
                          WHERE loan_id=? AND status <> 'waived'""", (loan_id,)).fetchone()
    return row[0]


def installments(conn, loan_id: int) -> list[dict]:
    return rows(conn, "SELECT * FROM loan_installments WHERE loan_id=? ORDER BY seq", (loan_id,))


def _plan_allocation(conn, loan: dict, amount: int, pay_date: str) -> tuple[list, int, int, int]:
    """Return ([(installment_id, interest, principal)], interest, principal, extra)."""
    plan, interest_total, principal_total = [], 0, 0
    remaining = amount
    next_upcoming_taken = False
    for inst in installments(conn, loan["id"]):
        if inst["status"] in ("paid", "waived") or remaining == 0:
            continue
        if inst["due_date"] > pay_date:
            # Paying ahead covers the next installment only when nothing is
            # currently due; otherwise the surplus is an extra principal payment.
            if next_upcoming_taken or plan:
                break
            next_upcoming_taken = True
        i_due = inst["interest_minor"] - inst["paid_interest_minor"]
        p_due = inst["principal_minor"] - inst["paid_principal_minor"]
        i_pay = min(remaining, i_due)
        remaining -= i_pay
        p_pay = min(remaining, p_due)
        remaining -= p_pay
        if i_pay or p_pay:
            plan.append((inst["id"], i_pay, p_pay))
            interest_total += i_pay
            principal_total += p_pay
    outstanding_after = outstanding_principal(conn, loan["id"]) - principal_total
    extra = min(remaining, outstanding_after)
    if remaining > extra:
        raise FinanceError("error.exceeds_outstanding_balance",
                           outstanding=amount - (remaining - extra))
    return plan, interest_total, principal_total, extra


def _apply_allocation(conn, loan: dict, plan, extra: int, pay_date: str) -> None:
    for inst_id, i_pay, p_pay in plan:
        conn.execute(
            """UPDATE loan_installments SET paid_interest_minor = paid_interest_minor + ?,
                   paid_principal_minor = paid_principal_minor + ?, paid_date = ?,
                   status = CASE WHEN paid_interest_minor + ? >= interest_minor
                                  AND paid_principal_minor + ? >= principal_minor THEN 'paid'
                                 ELSE 'partial' END
               WHERE id = ?""", (i_pay, p_pay, pay_date, i_pay, p_pay, inst_id))
    if extra:
        _apply_extra_principal(conn, loan, extra)
    _refresh_status(conn, loan["id"], pay_date)


def _apply_extra_principal(conn, loan: dict, extra: int) -> None:
    future = rows(conn, """SELECT * FROM loan_installments WHERE loan_id=? AND status='scheduled'
                           AND paid_principal_minor=0 AND paid_interest_minor=0 ORDER BY seq""",
                  (loan["id"],))
    if not future:
        raise FinanceError("error.exceeds_outstanding_balance", outstanding=0)
    if loan["interest_method"] == "reducing_balance":
        balance = sum(r["principal_minor"] for r in future) - extra
        conn.execute("DELETE FROM loan_installments WHERE id IN (%s)" % ",".join("?" * len(future)),
                     [r["id"] for r in future])
        if balance > 0:
            new = loan_math.build_schedule(balance, loan["annual_rate"], len(future), loan["frequency"],
                                           "reducing_balance", future[0]["due_date"],
                                           installment_minor=loan["installment_minor"],
                                           start_seq=future[0]["seq"])
            conn.executemany(
                "INSERT INTO loan_installments(loan_id, seq, due_date, principal_minor, interest_minor) VALUES (?,?,?,?,?)",
                [(loan["id"], r.seq, r.due_date, r.principal_minor, r.interest_minor) for r in new])
    else:
        # Flat / interest-free: reduce principal from the last installment
        # backwards.  No interest rebate is assumed (lender policy decides).
        left = extra
        for inst in reversed(future):
            cut = min(left, inst["principal_minor"])
            conn.execute("UPDATE loan_installments SET principal_minor = principal_minor - ? WHERE id=?",
                         (cut, inst["id"]))
            left -= cut
            if left == 0:
                break
        conn.execute("""UPDATE loan_installments SET status='waived' WHERE loan_id=? AND status='scheduled'
                        AND principal_minor=0 AND interest_minor=0""", (loan["id"],))


def _refresh_status(conn, loan_id: int, today: str) -> str:
    loan = _loan(conn, loan_id)
    if loan["status"] in ("defaulted", "restructured"):
        return loan["status"]
    if outstanding_principal(conn, loan_id) == 0 and not conn.execute(
            """SELECT 1 FROM loan_installments WHERE loan_id=? AND status NOT IN ('paid','waived')
               AND interest_minor > paid_interest_minor""", (loan_id,)).fetchone():
        status = "completed"
    elif conn.execute("""SELECT 1 FROM loan_installments WHERE loan_id=? AND due_date < ?
                         AND status NOT IN ('paid','waived')""", (loan_id, today)).fetchone():
        status = "overdue"
    else:
        status = "active"
    conn.execute("UPDATE loans SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                 (status, loan_id))
    return status


def _payment_lines(conn, loan: dict, kind: str, cash_account_id: int, principal: int, interest: int,
                   fee: int = 0) -> list:
    total = principal + interest + fee
    if loan["role"] == "borrower":
        int_cat = expense_category(conn, kind, "interest_expense")["id"]
        fee_cat = expense_category(conn, kind, "bank_charges")["id"]
        return [ledger.dr(loan["loan_account_id"], principal), ledger.dr_expense(int_cat, interest),
                ledger.dr_expense(fee_cat, fee, "early settlement fee"), ledger.cr(cash_account_id, total)]
    int_cat = income_category(conn, kind, "interest_income")["id"]
    return [ledger.dr(cash_account_id, total), ledger.cr(loan["loan_account_id"], principal),
            ledger.cr_income(int_cat, interest + fee)]


def record_payment(conn, user: dict, loan_id: int, *, amount_minor: int, pay_date: str,
                   cash_account_id: int | None = None) -> int:
    """Record an installment (or extra) payment on a third-party loan."""
    loan = _loan(conn, loan_id)
    kind = _kind(conn, loan["profile_id"])
    require(conn, user, kind, "loans", "write")
    if loan["loan_type"] in OWNER_LINKED:
        raise FinanceError("error.use_owner_loan_workflow")
    if amount_minor <= 0:
        raise FinanceError("error.amount_must_be_positive")
    pay_date = ledger.validate_date(pay_date)
    cash_account_id = cash_account_id or loan["cash_account_id"]
    if account(conn, cash_account_id)["profile_id"] != loan["profile_id"]:
        raise FinanceError("error.account_wrong_profile")
    plan, interest, principal, extra = _plan_allocation(conn, loan, amount_minor, pay_date)
    with transaction(conn):
        entry = ledger.post_entry(
            conn, profile_id=loan["profile_id"], entry_date=pay_date, entry_type="loan_payment",
            description=f"Loan payment: {loan['borrower']} → {loan['lender']}",
            lines=_payment_lines(conn, loan, kind, cash_account_id, principal + extra, interest),
            currency=loan["currency"], user_id=user["id"], related_entity_type="loan",
            related_entity_id=loan_id)
        _apply_allocation(conn, loan, plan, extra, pay_date)
        cur = conn.execute(
            """INSERT INTO loan_payments(loan_id, pay_date, principal_minor, interest_minor, kind, entry_id, created_by)
               VALUES (?,?,?,?,?,?,?)""",
            (loan_id, pay_date, principal + extra, interest, "extra" if extra else "installment", entry,
             user["id"]))
    return cur.lastrowid


def repay_owner_business_loan(conn, user: dict, business_loan_id: int, *, amount_minor: int,
                              pay_date: str, business_account_id: int, personal_account_id: int) -> int:
    """Repay a loan between the owner and the shop, posting both sides."""
    bloan = _loan(conn, business_loan_id)
    if bloan["loan_type"] not in OWNER_LINKED or _kind(conn, bloan["profile_id"]) != "business":
        raise FinanceError("error.use_owner_loan_workflow")
    ploan = _loan(conn, bloan["counterpart_loan_id"])
    pay_date = ledger.validate_date(pay_date)
    plan, interest, principal, extra = _plan_allocation(conn, bloan, amount_minor, pay_date)
    ttype = ("business_loan_repayment_to_owner" if bloan["loan_type"] == "owner_loan_to_business"
             else "owner_loan_repayment_to_business")
    with transaction(conn):
        tid = transfers.owner_transfer(
            conn, user, transfer_type=ttype, txn_date=pay_date, amount_minor=amount_minor,
            interest_minor=interest, description=f"Loan repayment #{business_loan_id}",
            business_account_id=business_account_id, personal_account_id=personal_account_id)
        p_plan, *_rest, p_extra = _plan_allocation(conn, ploan, amount_minor, pay_date)
        _apply_allocation(conn, bloan, plan, extra, pay_date)
        _apply_allocation(conn, ploan, p_plan, p_extra, pay_date)
        for lid in (bloan["id"], ploan["id"]):
            conn.execute(
                """INSERT INTO loan_payments(loan_id, pay_date, principal_minor, interest_minor, kind, transfer_id, created_by)
                   VALUES (?,?,?,?,?,?,?)""",
                (lid, pay_date, principal + extra, interest, "extra" if extra else "installment", tid,
                 user["id"]))
    return tid


def settlement_quote(conn, user: dict, loan_id: int, on: str) -> dict:
    loan = _loan(conn, loan_id)
    require(conn, user, _kind(conn, loan["profile_id"]), "loans", "read")
    balance = outstanding_principal(conn, loan_id)
    unpaid_interest_due = conn.execute(
        """SELECT COALESCE(SUM(interest_minor - paid_interest_minor),0) FROM loan_installments
           WHERE loan_id=? AND due_date <= ? AND status NOT IN ('paid','waived')""", (loan_id, on)).fetchone()[0]
    last_due = conn.execute("SELECT MAX(due_date) FROM loan_installments WHERE loan_id=? AND due_date <= ?",
                            (loan_id, on)).fetchone()[0] or loan["start_date"]
    days = (date.fromisoformat(on) - date.fromisoformat(last_due)).days
    quote = loan_math.early_settlement_amount(balance, loan["annual_rate"], days,
                                              loan["early_settlement_fee_pct"], loan["interest_method"])
    quote["unpaid_interest_due_minor"] = unpaid_interest_due
    quote["total_minor"] += unpaid_interest_due
    quote["days_accrued"] = days
    return quote


def settle_early(conn, user: dict, loan_id: int, *, pay_date: str, cash_account_id: int | None = None) -> int:
    loan = _loan(conn, loan_id)
    kind = _kind(conn, loan["profile_id"])
    require(conn, user, kind, "loans", "write")
    if loan["loan_type"] in OWNER_LINKED:
        raise FinanceError("error.use_owner_loan_workflow")
    q = settlement_quote(conn, user, loan_id, pay_date)
    cash_account_id = cash_account_id or loan["cash_account_id"]
    interest = q["accrued_interest_minor"] + q["unpaid_interest_due_minor"]
    with transaction(conn):
        entry = ledger.post_entry(
            conn, profile_id=loan["profile_id"], entry_date=pay_date, entry_type="loan_settlement",
            description=f"Early settlement: {loan['borrower']} → {loan['lender']}",
            lines=_payment_lines(conn, loan, kind, cash_account_id, q["principal_minor"], interest,
                                 q["fee_minor"]),
            currency=loan["currency"], user_id=user["id"], related_entity_type="loan", related_entity_id=loan_id)
        conn.execute("""UPDATE loan_installments SET paid_principal_minor = principal_minor,
                            paid_interest_minor = CASE WHEN due_date <= ? THEN interest_minor ELSE paid_interest_minor END,
                            status = CASE WHEN due_date <= ? THEN 'paid' ELSE 'waived' END, paid_date = ?
                        WHERE loan_id = ? AND status NOT IN ('paid','waived')""",
                     (pay_date, pay_date, pay_date, loan_id))
        conn.execute("""INSERT INTO loan_payments(loan_id, pay_date, principal_minor, interest_minor, fee_minor, kind, entry_id, created_by)
                        VALUES (?,?,?,?,?,'settlement',?,?)""",
                     (loan_id, pay_date, q["principal_minor"], interest, q["fee_minor"], entry, user["id"]))
        _refresh_status(conn, loan_id, pay_date)
    return entry


def set_status(conn, user: dict, loan_id: int, status: str) -> None:
    loan = _loan(conn, loan_id)
    require(conn, user, _kind(conn, loan["profile_id"]), "loans", "write")
    if status not in ("defaulted", "restructured", "active"):
        raise FinanceError("error.invalid_status")
    with transaction(conn):
        conn.execute("UPDATE loans SET status=? WHERE id=?", (status, loan_id))
        audit(conn, user["id"], "loans", loan_id, "status", {"status": status}, loan["profile_id"])
        if status == "active":
            _refresh_status(conn, loan_id, date.today().isoformat())


def loan_summary(conn, loan: dict, today: str) -> dict:
    insts = installments(conn, loan["id"])
    remaining = [i for i in insts if i["status"] not in ("paid", "waived")]
    missed = [i for i in remaining if i["due_date"] < today]
    out = dict(loan)
    out.update(
        outstanding_principal_minor=outstanding_principal(conn, loan["id"]),
        remaining_interest_minor=sum(i["interest_minor"] - i["paid_interest_minor"] for i in remaining),
        paid_principal_minor=sum(i["paid_principal_minor"] for i in insts),
        paid_interest_minor=sum(i["paid_interest_minor"] for i in insts),
        total_projected_interest_minor=sum(i["interest_minor"] for i in insts if i["status"] != "waived"),
        missed_count=len(missed),
        missed_amount_minor=sum(i["principal_minor"] + i["interest_minor"] - i["paid_principal_minor"]
                                - i["paid_interest_minor"] for i in missed),
        next_installment=remaining[0] if remaining else None,
        remaining_count=len(remaining),
    )
    out["progress_pct"] = round(100 * out["paid_principal_minor"] / loan["principal_minor"], 1)
    return out


def list_loans(conn, user: dict, profile: str, today: str) -> list[dict]:
    require(conn, user, profile, "loans", "read")
    out = []
    for loan in rows(conn, "SELECT * FROM loans WHERE profile_id=? ORDER BY start_date", (profile_id(conn, profile),)):
        if loan["status"] not in ("completed", "defaulted", "restructured"):
            _refresh_status(conn, loan["id"], today)
            loan = _loan(conn, loan["id"])
        out.append(loan_summary(conn, loan, today))
    return out


def loan_statement(conn, user: dict, loan_id: int, today: str) -> dict:
    loan = _loan(conn, loan_id)
    require(conn, user, _kind(conn, loan["profile_id"]), "loans", "read")
    return {"loan": loan_summary(conn, loan, today), "installments": installments(conn, loan_id),
            "payments": rows(conn, "SELECT * FROM loan_payments WHERE loan_id=? ORDER BY pay_date, id", (loan_id,)),
            "assumptions": loan_math.__doc__}


def what_if_extra(conn, user: dict, loan_id: int, extra_minor: int) -> dict:
    loan = _loan(conn, loan_id)
    require(conn, user, _kind(conn, loan["profile_id"]), "loans", "read")
    if loan["interest_method"] != "reducing_balance":
        raise FinanceError("error.what_if_reducing_only")
    balance = outstanding_principal(conn, loan_id)
    result = loan_math.simulate_extra_payment(balance, loan["annual_rate"], loan["frequency"],
                                              loan["installment_minor"], extra_minor)
    result.update(balance_minor=balance, installment_minor=loan["installment_minor"],
                  extra_minor=extra_minor, annual_rate=loan["annual_rate"], frequency=loan["frequency"])
    return result


def monthly_equivalent(amount_minor: int, frequency: str) -> int:
    return round(amount_minor * dates.PERIODS_PER_YEAR[frequency] / 12)


def affordability(conn, user: dict, profile: str, new_installment_minor: int, frequency: str,
                  today: str, months: int = 3, guideline_pct: int = 40) -> dict:
    """Debt-service ratio before and after a proposed loan, using average
    monthly income over the last ``months`` full months.  ``guideline_pct``
    is an adjustable rule of thumb, not a lender's approval criterion."""
    require(conn, user, profile, "loans", "read")
    pid = profile_id(conn, profile)
    month_start = dates.month_bounds(today)[0]
    start = dates.add_months(month_start, -months)
    end = month_start - timedelta(days=1)
    income = sum(ledger.category_totals(conn, pid, "income", start.isoformat(), end.isoformat()).values())
    avg_income = income // months
    existing = sum(monthly_equivalent(ln["installment_minor"], ln["frequency"])
                   for ln in rows(conn, """SELECT * FROM loans WHERE profile_id=? AND role='borrower'
                                           AND status IN ('active','overdue')""", (pid,)))
    new_monthly = monthly_equivalent(new_installment_minor, frequency)

    def ratio(x):
        return round(100 * x / avg_income, 1) if avg_income > 0 else None

    return {"avg_monthly_income_minor": avg_income, "existing_monthly_debt_minor": existing,
            "new_monthly_installment_minor": new_monthly, "ratio_before_pct": ratio(existing),
            "ratio_after_pct": ratio(existing + new_monthly),
            "period": (start.isoformat(), end.isoformat()), "guideline_pct": guideline_pct}
