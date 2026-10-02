"""Bills (business and personal) and recurring transactions.

Accounting assumption: a bill is a commitment, not a posting.  Each payment
(full or partial) posts an expense on the payment date (cash basis), with
any tax split proportionally.  This avoids double-counting and keeps bills
editable until they are paid.  "Autopay" is only an indicator that the bank
debits the bill automatically: Namareq never posts or submits a payment on
its own — the owner confirms each payment.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from . import dates, ledger, transactions
from .db import FinanceError, audit, expense_category, profile_id, rows, transaction
from .money import split_proportionally
from .permissions import require

BILL_STATUSES = ("scheduled", "due_today", "overdue", "partially_paid", "paid", "cancelled")


def _bill(conn, bill_id):
    row = conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if row is None:
        raise FinanceError("error.unknown_record")
    return dict(row)


def _kind(conn, pid):
    return conn.execute("SELECT kind FROM financial_profiles WHERE id=?", (pid,)).fetchone()["kind"]


def paid_amount(conn, bill_id: int) -> int:
    """Total paid, excluding payments whose expense was later reversed."""
    return conn.execute(
        """SELECT COALESCE(SUM(bp.amount_minor),0) FROM bill_payments bp
           JOIN expense_transactions e ON e.id = bp.expense_txn_id
           WHERE bp.bill_id=? AND e.status='posted'""", (bill_id,)).fetchone()[0]


def bill_status(bill: dict, paid: int, today: str) -> str:
    total = bill["amount_minor"] + bill["tax_minor"]
    if bill["is_cancelled"]:
        return "cancelled"
    if paid >= total:
        return "paid"
    if bill["due_date"] < today:
        return "overdue"
    if bill["due_date"] == today:
        return "due_today"
    if paid > 0:
        return "partially_paid"
    return "scheduled"


def find_duplicate_bills(conn, pid: int, payee: str, total_minor: int, due_date: str,
                         exclude_id: int | None = None, window_days: int = 5) -> list[dict]:
    due = date.fromisoformat(due_date)
    return rows(conn, """SELECT * FROM bills WHERE profile_id=? AND is_cancelled=0
                         AND lower(trim(payee)) = lower(trim(?))
                         AND amount_minor + tax_minor = ? AND due_date BETWEEN ? AND ?
                         AND id IS NOT ?""",
                (pid, payee, total_minor, (due - timedelta(days=window_days)).isoformat(),
                 (due + timedelta(days=window_days)).isoformat(), exclude_id))


def create_bill(conn, user: dict, *, profile: str, payee: str, category_id: int, due_date: str,
                amount_minor: int, tax_minor: int = 0, description: str | None = None,
                issue_date: str | None = None, period_start: str | None = None,
                period_end: str | None = None, frequency: str = "one_time",
                custom_interval_days: int | None = None, payment_account_id: int | None = None,
                autopay: bool = False, reminder_days: str = "7,1,0", notes: str | None = None,
                attachment_path: str | None = None, currency: str = "JOD",
                allow_duplicate: bool = False, series_id: int | None = None) -> int:
    require(conn, user, profile, "bills", "write")
    pid = profile_id(conn, profile)
    expense_category(conn, profile, int(category_id))
    if not payee or not payee.strip():
        raise FinanceError("error.payee_required")
    if amount_minor <= 0 or tax_minor < 0:
        raise FinanceError("error.amount_must_be_positive")
    if frequency not in dates.FREQUENCIES:
        raise FinanceError("error.invalid_frequency")
    if frequency == "custom" and not custom_interval_days:
        raise FinanceError("error.custom_interval_required")
    due_date = ledger.validate_date(due_date)
    if payment_account_id is not None:
        acct = conn.execute("SELECT profile_id FROM financial_accounts WHERE id=?",
                            (payment_account_id,)).fetchone()
        if acct is None or acct["profile_id"] != pid:
            raise FinanceError("error.account_wrong_profile")
    if not allow_duplicate:
        dups = find_duplicate_bills(conn, pid, payee, amount_minor + tax_minor, due_date)
        if dups:
            raise FinanceError("error.possible_duplicate_bill", bill_id=dups[0]["id"])
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO bills(profile_id, payee, category_id, description, period_start, period_end,
                   issue_date, due_date, amount_minor, tax_minor, currency, frequency,
                   custom_interval_days, payment_account_id, autopay, reminder_days, series_id,
                   attachment_path, notes, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, payee.strip(), category_id, description, period_start, period_end, issue_date,
             due_date, amount_minor, tax_minor, currency, frequency, custom_interval_days,
             payment_account_id, int(autopay), reminder_days, series_id, attachment_path, notes,
             user["id"]))
        bill_id = cur.lastrowid
        if series_id is None and frequency != "one_time":
            conn.execute("UPDATE bills SET series_id=? WHERE id=?", (bill_id, bill_id))
        audit(conn, user["id"], "bills", bill_id, "create", None, pid)
    return bill_id


def pay_bill(conn, user: dict, bill_id: int, *, amount_minor: int, pay_date: str,
             account_id: int, reference: str | None = None) -> int:
    """Record a full or partial payment.  Posts an expense for the paid part."""
    bill = _bill(conn, bill_id)
    kind = _kind(conn, bill["profile_id"])
    require(conn, user, kind, "bills", "write")
    if bill["is_cancelled"]:
        raise FinanceError("error.bill_cancelled")
    total = bill["amount_minor"] + bill["tax_minor"]
    remaining = total - paid_amount(conn, bill_id)
    if amount_minor <= 0:
        raise FinanceError("error.amount_must_be_positive")
    if amount_minor > remaining:
        raise FinanceError("error.exceeds_outstanding_balance", outstanding=remaining)
    net_part, tax_part = split_proportionally(amount_minor, [bill["amount_minor"], bill["tax_minor"]])
    with transaction(conn):
        exp_id = transactions.record_expense(
            conn, user, profile=kind, account_id=account_id, category_id=bill["category_id"],
            txn_date=pay_date, amount_minor=net_part, tax_minor=tax_part,
            tax_recoverable=False, payee=bill["payee"],
            description=f"{bill['payee']} — {bill['description'] or 'bill'} #{bill_id}",
            reference=reference, bill_id=bill_id, is_recurring=bill["frequency"] != "one_time",
            related_entity_type="bill", related_entity_id=bill_id)
        cur = conn.execute(
            """INSERT INTO bill_payments(bill_id, pay_date, amount_minor, account_id, expense_txn_id, created_by)
               VALUES (?,?,?,?,?,?)""",
            (bill_id, pay_date, amount_minor, account_id, exp_id, user["id"]))
        conn.execute("UPDATE bills SET updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (bill_id,))
    return cur.lastrowid


def cancel_bill(conn, user: dict, bill_id: int, stop_series: bool = False) -> None:
    bill = _bill(conn, bill_id)
    require(conn, user, _kind(conn, bill["profile_id"]), "bills", "write")
    if paid_amount(conn, bill_id):
        raise FinanceError("error.bill_has_payments")
    with transaction(conn):
        conn.execute("UPDATE bills SET is_cancelled=1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                     (bill_id,))
        if stop_series:
            # Mark the series as generated so no further instances are created.
            conn.execute("UPDATE bills SET next_generated=1 WHERE series_id=?", (bill["series_id"],))
        audit(conn, user["id"], "bills", bill_id, "cancel", {"stop_series": stop_series},
              bill["profile_id"])


def generate_recurring_bills(conn, user: dict, today: str, horizon_days: int = 40) -> list[int]:
    """Create the next instance of every recurring bill whose current
    instance falls due within ``horizon_days``.  Bills are commitments, so
    creating them never moves money."""
    created = []
    limit = (date.fromisoformat(today) + timedelta(days=horizon_days)).isoformat()
    while True:
        pending = rows(conn, """SELECT * FROM bills WHERE frequency <> 'one_time' AND next_generated=0
                                AND is_cancelled=0 AND due_date <= ?""", (limit,))
        if not pending:
            break
        for b in pending:
            kind = _kind(conn, b["profile_id"])
            first = conn.execute("SELECT due_date FROM bills WHERE id=?", (b["series_id"],)).fetchone()
            anchor = date.fromisoformat(first["due_date"]).day if first else None
            nxt = dates.advance(b["due_date"], b["frequency"], 1, b["custom_interval_days"], anchor)
            shift = (nxt - date.fromisoformat(b["due_date"])).days
            with transaction(conn):
                conn.execute("UPDATE bills SET next_generated=1 WHERE id=?", (b["id"],))
                new_id = create_bill(
                    conn, user, profile=kind, payee=b["payee"], category_id=b["category_id"],
                    due_date=nxt.isoformat(), amount_minor=b["amount_minor"], tax_minor=b["tax_minor"],
                    description=b["description"],
                    issue_date=_shift(b["issue_date"], shift), period_start=_shift(b["period_start"], shift),
                    period_end=_shift(b["period_end"], shift), frequency=b["frequency"],
                    custom_interval_days=b["custom_interval_days"],
                    payment_account_id=b["payment_account_id"], autopay=bool(b["autopay"]),
                    reminder_days=b["reminder_days"], notes=b["notes"], currency=b["currency"],
                    allow_duplicate=True, series_id=b["series_id"])
            created.append(new_id)
    return created


def _shift(value, days):
    return (date.fromisoformat(value) + timedelta(days=days)).isoformat() if value else None


def list_bills(conn, user: dict, profile: str, today: str, start: str | None = None,
               end: str | None = None, status: str | None = None) -> list[dict]:
    require(conn, user, profile, "bills", "read")
    pid = profile_id(conn, profile)
    sql = """SELECT b.*, c.name_en cat_en, c.name_ar cat_ar FROM bills b
             JOIN expense_categories c ON c.id=b.category_id WHERE b.profile_id=?"""
    params: list = [pid]
    if start:
        sql += " AND b.due_date >= ?"
        params.append(start)
    if end:
        sql += " AND b.due_date <= ?"
        params.append(end)
    out = []
    for b in rows(conn, sql + " ORDER BY b.due_date, b.id", params):
        b["paid_minor"] = paid_amount(conn, b["id"])
        b["total_minor"] = b["amount_minor"] + b["tax_minor"]
        b["remaining_minor"] = 0 if b["is_cancelled"] else b["total_minor"] - b["paid_minor"]
        b["status"] = bill_status(b, b["paid_minor"], today)
        if status is None or b["status"] == status:
            out.append(b)
    return out


def bill_alerts(conn, user: dict, profile: str, today: str) -> dict:
    """Due-today, overdue and upcoming reminders (per bill reminder schedule)."""
    alerts = {"due_today": [], "overdue": [], "upcoming": [], "autopay_to_confirm": []}
    t = date.fromisoformat(today)
    for b in list_bills(conn, user, profile, today):
        if b["status"] in ("paid", "cancelled"):
            continue
        days_left = (date.fromisoformat(b["due_date"]) - t).days
        b["days_left"] = days_left
        if b["status"] == "overdue":
            alerts["overdue"].append(b)
        elif b["status"] == "due_today":
            alerts["due_today"].append(b)
            if b["autopay"]:
                alerts["autopay_to_confirm"].append(b)
        else:
            reminders = {int(x) for x in (b["reminder_days"] or "").split(",") if x.strip().isdigit()}
            if any(days_left <= r for r in reminders if r > 0):
                alerts["upcoming"].append(b)
    return alerts


def payment_history(conn, user: dict, bill_id: int) -> list[dict]:
    bill = _bill(conn, bill_id)
    require(conn, user, _kind(conn, bill["profile_id"]), "bills", "read")
    return rows(conn, """SELECT bp.*, e.status expense_status FROM bill_payments bp
                         JOIN expense_transactions e ON e.id=bp.expense_txn_id
                         WHERE bp.bill_id=? ORDER BY bp.pay_date""", (bill_id,))


def projected_bill_occurrences(conn, pid: int, start: str, end: str) -> list[dict]:
    """Outstanding bills plus future occurrences of recurring series that
    have not been generated yet, between start and end (inclusive)."""
    out = []
    for b in rows(conn, "SELECT * FROM bills WHERE profile_id=? AND is_cancelled=0", (pid,)):
        remaining = b["amount_minor"] + b["tax_minor"] - paid_amount(conn, b["id"])
        if remaining > 0 and b["due_date"] <= end:
            out.append({"date": max(b["due_date"], start), "amount_minor": remaining,
                        "payee": b["payee"], "bill_id": b["id"], "projected": False,
                        "category_id": b["category_id"], "overdue": b["due_date"] < start})
        if b["frequency"] != "one_time" and not b["next_generated"]:
            first = conn.execute("SELECT due_date FROM bills WHERE id=?", (b["series_id"],)).fetchone()
            anchor = date.fromisoformat(first["due_date"]).day if first else None
            n = 1
            while True:
                nxt = dates.advance(b["due_date"], b["frequency"], n, b["custom_interval_days"], anchor)
                if nxt.isoformat() > end:
                    break
                if nxt.isoformat() >= start:
                    out.append({"date": nxt.isoformat(), "amount_minor": b["amount_minor"] + b["tax_minor"],
                                "payee": b["payee"], "bill_id": None, "projected": True,
                                "category_id": b["category_id"], "overdue": False})
                n += 1
    return out


def monthly_bill_forecast(conn, user: dict, profile: str, today: str, months: int = 6) -> list[dict]:
    require(conn, user, profile, "bills", "read")
    pid = profile_id(conn, profile)
    start = dates.month_bounds(today)[0]
    end = dates.month_bounds(dates.add_months(start, months - 1))[1]
    totals: dict[str, int] = defaultdict(int)
    for occ in projected_bill_occurrences(conn, pid, start.isoformat(), end.isoformat()):
        totals[occ["date"][:7]] += occ["amount_minor"]
    return [{"month": dates.add_months(start, i).isoformat()[:7],
             "amount_minor": totals.get(dates.add_months(start, i).isoformat()[:7], 0)}
            for i in range(months)]


# ---------------------------------------------------------------------------
# Recurring income / expenses / savings / planned purchases
# ---------------------------------------------------------------------------
def create_recurring(conn, user: dict, *, profile: str, kind: str, name: str, amount_minor: int,
                     frequency: str, next_date: str, account_id: int | None = None,
                     income_category_id: int | None = None, expense_category_id: int | None = None,
                     custom_interval_days: int | None = None, end_date: str | None = None,
                     notes: str | None = None) -> int:
    require(conn, user, profile, "recurring", "write")
    pid = profile_id(conn, profile)
    if frequency not in dates.FREQUENCIES:
        raise FinanceError("error.invalid_frequency")
    if kind == "income" and income_category_id is None:
        raise FinanceError("error.category_required")
    if kind in ("expense", "planned_purchase") and expense_category_id is None:
        raise FinanceError("error.category_required")
    if amount_minor <= 0:
        raise FinanceError("error.amount_must_be_positive")
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO recurring_transactions(profile_id, kind, name, account_id, income_category_id,
                   expense_category_id, amount_minor, frequency, custom_interval_days, next_date,
                   end_date, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, kind, name, account_id, income_category_id, expense_category_id, amount_minor,
             frequency, custom_interval_days, ledger.validate_date(next_date), end_date, notes,
             user["id"]))
    return cur.lastrowid


def due_recurring(conn, user: dict, profile: str, today: str) -> list[dict]:
    """Recurring items whose next date has arrived.  They are listed for the
    owner to confirm; nothing is posted automatically."""
    require(conn, user, profile, "recurring", "read")
    return rows(conn, """SELECT * FROM recurring_transactions WHERE profile_id=? AND is_active=1
                         AND next_date <= ? ORDER BY next_date""", (profile_id(conn, profile), today))


def confirm_recurring(conn, user: dict, rec_id: int, *, account_id: int | None = None,
                      amount_minor: int | None = None, skip: bool = False) -> int | None:
    rec = conn.execute("SELECT * FROM recurring_transactions WHERE id=?", (rec_id,)).fetchone()
    if rec is None:
        raise FinanceError("error.unknown_record")
    kind = _kind(conn, rec["profile_id"])
    require(conn, user, kind, "recurring", "write")
    txn_id = None
    with transaction(conn):
        if not skip:
            acct = account_id or rec["account_id"]
            amt = amount_minor or rec["amount_minor"]
            if rec["kind"] == "income":
                txn_id = transactions.record_income(
                    conn, user, profile=kind, account_id=acct, category_id=rec["income_category_id"],
                    txn_date=rec["next_date"], amount_minor=amt, description=rec["name"],
                    is_recurring=True, recurring_id=rec_id)
            elif rec["kind"] in ("expense", "planned_purchase"):
                txn_id = transactions.record_expense(
                    conn, user, profile=kind, account_id=acct, category_id=rec["expense_category_id"],
                    txn_date=rec["next_date"], amount_minor=amt, description=rec["name"],
                    is_recurring=True, recurring_id=rec_id)
        if rec["frequency"] == "one_time":
            conn.execute("UPDATE recurring_transactions SET is_active=0 WHERE id=?", (rec_id,))
        else:
            nxt = dates.advance(rec["next_date"], rec["frequency"], 1, rec["custom_interval_days"])
            active = 0 if rec["end_date"] and nxt.isoformat() > rec["end_date"] else 1
            conn.execute("""UPDATE recurring_transactions SET next_date=?, is_active=?,
                            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?""",
                         (nxt.isoformat(), active, rec_id))
    return txn_id
