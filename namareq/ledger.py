"""Immutable double-entry ledger.

Every financial event is posted as a balanced entry (total debits equal total
credits) inside exactly one financial profile.  Events that cross profiles
(owner contributions, withdrawals, ...) post two entries — one per profile —
linked by a ``transfers`` row.  Posted entries cannot be updated or deleted
(enforced by database triggers); corrections are reversal entries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import chart
from .db import FinanceError, account, audit, profile_kind, transaction


@dataclass(frozen=True)
class Line:
    debit: int = 0
    credit: int = 0
    account_id: int | None = None
    income_category_id: int | None = None
    expense_category_id: int | None = None
    memo: str | None = None


def dr(account_id: int, amount: int, memo=None) -> Line:
    return Line(debit=amount, account_id=account_id, memo=memo)


def cr(account_id: int, amount: int, memo=None) -> Line:
    return Line(credit=amount, account_id=account_id, memo=memo)


def dr_expense(category_id: int, amount: int, memo=None) -> Line:
    return Line(debit=amount, expense_category_id=category_id, memo=memo)


def cr_expense(category_id: int, amount: int, memo=None) -> Line:
    return Line(credit=amount, expense_category_id=category_id, memo=memo)


def cr_income(category_id: int, amount: int, memo=None) -> Line:
    return Line(credit=amount, income_category_id=category_id, memo=memo)


def dr_income(category_id: int, amount: int, memo=None) -> Line:
    return Line(debit=amount, income_category_id=category_id, memo=memo)


def validate_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise FinanceError("error.invalid_date") from exc


def period_is_closed(conn, pid: int, on: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM financial_periods WHERE profile_id = ? AND status = 'closed'
           AND start_date <= ? AND end_date >= ?""",
        (pid, on, on),
    ).fetchone() is not None


def post_entry(conn, *, profile_id: int, entry_date: str, entry_type: str, description: str,
               lines: list[Line], currency: str = "JOD", user_id: int | None = None,
               reference: str | None = None, source_type: str | None = None,
               source_id: int | None = None, related_entity_type: str | None = None,
               related_entity_id: int | None = None, transfer_id: int | None = None,
               reverses_entry_id: int | None = None) -> int:
    entry_date = validate_date(entry_date)
    lines = [ln for ln in lines if ln.debit or ln.credit]
    if len(lines) < 2:
        raise FinanceError("error.entry_needs_two_lines")
    for ln in lines:
        if ln.debit < 0 or ln.credit < 0 or (ln.debit and ln.credit):
            raise FinanceError("error.invalid_line_amount")
    debits = sum(ln.debit for ln in lines)
    credits = sum(ln.credit for ln in lines)
    if debits != credits:
        raise FinanceError("error.unbalanced_entry", debits=debits, credits=credits)
    if period_is_closed(conn, profile_id, entry_date):
        raise FinanceError("error.period_closed", date=entry_date)
    for ln in lines:
        if ln.account_id is not None:
            acct = account(conn, ln.account_id)
            if acct["profile_id"] != profile_id:
                raise FinanceError("error.account_wrong_profile")
            if acct["currency"] != currency:
                raise FinanceError("error.currency_mismatch", account=acct["name_en"])
            if not acct["is_active"] and reverses_entry_id is None:
                raise FinanceError("error.account_inactive", account=acct["name_en"])

    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO ledger_entries(profile_id, entry_date, entry_type, currency, description,
                   reference, source_type, source_id, related_entity_type, related_entity_id,
                   transfer_id, reverses_entry_id, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (profile_id, entry_date, entry_type, currency, description, reference, source_type,
             source_id, related_entity_type, related_entity_id, transfer_id, reverses_entry_id,
             user_id),
        )
        entry_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO ledger_lines(entry_id, account_id, income_category_id,
                   expense_category_id, debit_minor, credit_minor, memo)
               VALUES (?,?,?,?,?,?,?)""",
            [(entry_id, ln.account_id, ln.income_category_id, ln.expense_category_id,
              ln.debit, ln.credit, ln.memo) for ln in lines],
        )
        audit(conn, user_id, "ledger_entries", entry_id, "post",
              {"type": entry_type, "amount": debits, "currency": currency}, profile_id)
    return entry_id


def entry_lines(conn, entry_id: int) -> list:
    return conn.execute("SELECT * FROM ledger_lines WHERE entry_id = ? ORDER BY id",
                        (entry_id,)).fetchall()


def is_reversed(conn, entry_id: int) -> bool:
    return conn.execute("SELECT 1 FROM ledger_entries WHERE reverses_entry_id = ?",
                        (entry_id,)).fetchone() is not None


def reverse_entry(conn, entry_id: int, *, user_id: int | None, reversal_date: str,
                  reason: str) -> int:
    """Post the mirror image of an entry.  The original stays untouched."""
    original = conn.execute("SELECT * FROM ledger_entries WHERE id = ?", (entry_id,)).fetchone()
    if original is None:
        raise FinanceError("error.unknown_entry")
    if original["reverses_entry_id"] is not None:
        raise FinanceError("error.cannot_reverse_reversal")
    if is_reversed(conn, entry_id):
        raise FinanceError("error.already_reversed")
    if not reason or not reason.strip():
        raise FinanceError("error.reason_required")
    lines = [Line(debit=ln["credit_minor"], credit=ln["debit_minor"], account_id=ln["account_id"],
                  income_category_id=ln["income_category_id"],
                  expense_category_id=ln["expense_category_id"], memo=ln["memo"])
             for ln in entry_lines(conn, entry_id)]
    return post_entry(
        conn, profile_id=original["profile_id"], entry_date=reversal_date,
        entry_type="reversal", description=f"Reversal of #{entry_id}: {reason}", lines=lines,
        currency=original["currency"], user_id=user_id, reference=original["reference"],
        source_type=original["source_type"], source_id=original["source_id"],
        transfer_id=original["transfer_id"], reverses_entry_id=entry_id,
    )


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------
def _date_clause(start: str | None, end: str | None) -> tuple[str, list]:
    clause, params = "", []
    if start:
        clause += " AND e.entry_date >= ?"
        params.append(start)
    if end:
        clause += " AND e.entry_date <= ?"
        params.append(end)
    return clause, params


def account_balance(conn, account_id: int, as_of: str | None = None) -> int:
    """Balance on the account's normal side (assets positive when debit,
    liabilities/equity positive when credit)."""
    acct = account(conn, account_id)
    clause, params = _date_clause(None, as_of)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(l.debit_minor),0) d, COALESCE(SUM(l.credit_minor),0) c
            FROM ledger_lines l JOIN ledger_entries e ON e.id = l.entry_id
            WHERE l.account_id = ? {clause}""",
        [account_id, *params],
    ).fetchone()
    net = row["d"] - row["c"]
    return net if chart.is_debit_normal(acct["account_type"]) else -net


def account_movement(conn, account_id: int, start: str | None, end: str | None) -> tuple[int, int]:
    clause, params = _date_clause(start, end)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(l.debit_minor),0) d, COALESCE(SUM(l.credit_minor),0) c
            FROM ledger_lines l JOIN ledger_entries e ON e.id = l.entry_id
            WHERE l.account_id = ? {clause}""",
        [account_id, *params],
    ).fetchone()
    return row["d"], row["c"]


def category_totals(conn, pid: int, kind: str, start: str | None, end: str | None) -> dict[int, int]:
    """Net amount per income (credit-normal) or expense (debit-normal)
    category within a profile and date range.  Reversals net out naturally."""
    col = "income_category_id" if kind == "income" else "expense_category_id"
    clause, params = _date_clause(start, end)
    result = {}
    for row in conn.execute(
        f"""SELECT l.{col} cat, SUM(l.debit_minor) d, SUM(l.credit_minor) c
            FROM ledger_lines l JOIN ledger_entries e ON e.id = l.entry_id
            WHERE e.profile_id = ? AND l.{col} IS NOT NULL {clause}
            GROUP BY l.{col}""",
        [pid, *params],
    ):
        result[row["cat"]] = (row["c"] - row["d"]) if kind == "income" else (row["d"] - row["c"])
    return result


def trial_balance(conn, pid: int, as_of: str | None = None) -> list[dict]:
    clause, params = _date_clause(None, as_of)
    out = []
    for row in conn.execute(
        f"""SELECT 'account' kind, a.id, a.name_en, a.name_ar, a.account_type,
                   SUM(l.debit_minor) d, SUM(l.credit_minor) c
            FROM ledger_lines l JOIN ledger_entries e ON e.id = l.entry_id
            JOIN financial_accounts a ON a.id = l.account_id
            WHERE e.profile_id = ? {clause} GROUP BY a.id
            UNION ALL
            SELECT 'income', ic.id, ic.name_en, ic.name_ar, 'income', SUM(l.debit_minor), SUM(l.credit_minor)
            FROM ledger_lines l JOIN ledger_entries e ON e.id = l.entry_id
            JOIN income_categories ic ON ic.id = l.income_category_id
            WHERE e.profile_id = ? {clause} GROUP BY ic.id
            UNION ALL
            SELECT 'expense', ec.id, ec.name_en, ec.name_ar, 'expense', SUM(l.debit_minor), SUM(l.credit_minor)
            FROM ledger_lines l JOIN ledger_entries e ON e.id = l.entry_id
            JOIN expense_categories ec ON ec.id = l.expense_category_id
            WHERE e.profile_id = ? {clause} GROUP BY ec.id""",
        [pid, *params, pid, *params, pid, *params],
    ):
        net = row["d"] - row["c"]
        out.append({"kind": row["kind"], "id": row["id"], "name_en": row["name_en"],
                    "name_ar": row["name_ar"], "type": row["account_type"],
                    "debit": net if net > 0 else 0, "credit": -net if net < 0 else 0})
    return out


def close_period(conn, user: dict, pid: int, start: str, end: str) -> int:
    start, end = validate_date(start), validate_date(end)
    if end < start:
        raise FinanceError("error.invalid_date_range")
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO financial_periods(profile_id, start_date, end_date, status, closed_by, closed_at)
               VALUES (?,?,?,'closed',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
               ON CONFLICT(profile_id, start_date) DO UPDATE SET status='closed',
                   end_date=excluded.end_date, closed_by=excluded.closed_by, closed_at=excluded.closed_at""",
            (pid, start, end, user["id"]),
        )
        audit(conn, user["id"], "financial_periods", cur.lastrowid, "close",
              {"start": start, "end": end}, pid)
    return cur.lastrowid


def profile_kind_of_entry(conn, entry_id: int) -> str:
    row = conn.execute("SELECT profile_id FROM ledger_entries WHERE id = ?", (entry_id,)).fetchone()
    return profile_kind(conn, row["profile_id"])
