"""Separate business / personal backups and protected personal exports.

* An ordinary (business) backup never contains personal-finance data.
* Personal data is exported only when the owner selects it AND re-enters
  their password; the file is encrypted with a key derived from a
  passphrase the owner chooses (PBKDF2-SHA256 + Fernet/AES-128-CBC+HMAC).
* Every personal export is written to the personal access log.
"""
from __future__ import annotations

import base64
import csv
import io
import os
import sqlite3
from pathlib import Path

from .db import SCHEMA_PATH, FinanceError, audit, profile_id
from .permissions import log_personal_access, require_owner, verify_password

# table -> SQL predicate selecting rows of one profile (``:pid``/``:kind``).
PROFILE_FILTERS = {
    "financial_profiles": "id = :pid",
    "financial_accounts": "profile_id = :pid",
    "income_categories": "profile_kind = :kind",
    "expense_categories": "profile_kind = :kind",
    "financial_periods": "profile_id = :pid",
    "transfers": "id IN (SELECT transfer_id FROM ledger_entries WHERE profile_id = :pid AND transfer_id IS NOT NULL)",
    "ledger_entries": "profile_id = :pid",
    "ledger_lines": "entry_id IN (SELECT id FROM ledger_entries WHERE profile_id = :pid)",
    "income_transactions": "profile_id = :pid",
    "recurring_transactions": "profile_id = :pid",
    "bills": "profile_id = :pid",
    "expense_transactions": "profile_id = :pid",
    "bill_payments": "bill_id IN (SELECT id FROM bills WHERE profile_id = :pid)",
    "owner_equity_transactions": "profile_id = :pid",
    "loans": "profile_id = :pid",
    "loan_installments": "loan_id IN (SELECT id FROM loans WHERE profile_id = :pid)",
    "loan_payments": "loan_id IN (SELECT id FROM loans WHERE profile_id = :pid)",
    "debt_records": "profile_id = :pid",
    "debt_payments": "debt_id IN (SELECT id FROM debt_records WHERE profile_id = :pid)",
    "assets": "profile_id = :pid",
    "liabilities": "profile_id = :pid",
    "budgets": "profile_id = :pid",
    "budget_lines": "budget_id IN (SELECT id FROM budgets WHERE profile_id = :pid)",
    "financial_goals": "profile_id = :pid",
    "goal_contributions": "goal_id IN (SELECT id FROM financial_goals WHERE profile_id = :pid)",
    "cash_flow_forecasts": "scope = :kind",
    "financial_advice": "scope = :kind",
    "advice_actions": "advice_id IN (SELECT id FROM financial_advice WHERE scope = :kind)",
    "account_reconciliations": "account_id IN (SELECT id FROM financial_accounts WHERE profile_id = :pid)",
    "audit_log": "profile_id = :pid",
}
# Columns blanked in a single-profile backup because they point at the
# other profile's records.
CROSS_PROFILE_COLUMNS = {
    "business": {"transfers": ("personal_entry_id", "personal_account_id")},
    "personal": {"transfers": ("business_entry_id", "business_account_id"),
                 "loans": ("counterpart_loan_id",)},
}


def _copy_profile(conn, dest: str, kind: str) -> None:
    pid = profile_id(conn, kind)
    conn.execute("ATTACH DATABASE ? AS bk", (dest,))
    try:
        conn.execute("PRAGMA bk.foreign_keys = OFF")
        if kind == "business":
            conn.execute("INSERT OR IGNORE INTO bk.users SELECT * FROM main.users")
            conn.execute("INSERT OR IGNORE INTO bk.finance_access_grants SELECT * FROM main.finance_access_grants")
            conn.execute("INSERT OR IGNORE INTO bk.audit_log SELECT * FROM main.audit_log WHERE profile_id IS NULL")
        else:
            conn.execute("INSERT OR IGNORE INTO bk.users SELECT * FROM main.users WHERE role='owner'")
            conn.execute("INSERT OR IGNORE INTO bk.personal_access_log SELECT * FROM main.personal_access_log")
        for table, pred in PROFILE_FILTERS.items():
            conn.execute(f"INSERT OR IGNORE INTO bk.{table} SELECT * FROM main.{table} WHERE {pred}",
                         {"pid": pid, "kind": kind})
        for table, cols in CROSS_PROFILE_COLUMNS[kind].items():
            # The backup copy has fresh tables without the immutability
            # triggers on these (only ledger tables are protected).
            conn.execute(f"UPDATE bk.{table} SET " + ", ".join(f"{c} = NULL" for c in cols))
        if kind == "business":
            # Hide the owner's free-text description of cross-profile moves.
            conn.execute("UPDATE bk.transfers SET description = transfer_type WHERE transfer_type <> 'internal'")
    finally:
        conn.execute("DETACH DATABASE bk")


def _new_db(path: str) -> None:
    if os.path.exists(path):
        raise FinanceError("error.file_exists")
    target = sqlite3.connect(path)
    target.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    target.close()


def business_backup(conn, user: dict, dest: str) -> str:
    """Business-only backup (owner or accountant).  Contains no personal data."""
    if user["role"] not in ("owner", "accountant"):
        raise FinanceError("error.access_denied")
    _new_db(dest)
    _copy_profile(conn, dest, "business")
    audit(conn, user["id"], "backup", None, "business_backup", {"dest": os.path.basename(dest)})
    return dest


def _fernet(passphrase: str, salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode())))


MAGIC = b"NMRQP1"


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    if len(passphrase) < 10:
        raise FinanceError("error.passphrase_too_short")
    salt = os.urandom(16)
    return MAGIC + salt + _fernet(passphrase, salt).encrypt(data)


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    from cryptography.fernet import InvalidToken
    if not blob.startswith(MAGIC):
        raise FinanceError("error.not_namareq_export")
    salt, token = blob[len(MAGIC):len(MAGIC) + 16], blob[len(MAGIC) + 16:]
    try:
        return _fernet(passphrase, salt).decrypt(token)
    except InvalidToken as exc:
        raise FinanceError("error.confirmation_failed") from exc


def _confirm_owner(conn, user: dict, owner_password: str, what: str) -> None:
    require_owner(user)
    stored = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()["password_hash"]
    ok = verify_password(owner_password, stored)
    log_personal_access(conn, user, what, "export", ok)
    if not ok:
        raise FinanceError("error.confirmation_failed")


def personal_backup(conn, user: dict, dest: str, owner_password: str, passphrase: str) -> str:
    """Encrypted personal-only backup.  Owner only, password re-confirmed."""
    _confirm_owner(conn, user, owner_password, "personal_backup")
    tmp = dest + ".tmp.sqlite"
    _new_db(tmp)
    try:
        _copy_profile(conn, tmp, "personal")
        Path(dest).write_bytes(encrypt_bytes(Path(tmp).read_bytes(), passphrase))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest


def table_to_csv(header: list[str], data: list[list]) -> bytes:
    """UTF-8 with BOM so Excel shows Arabic correctly."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(data)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def protected_personal_export(conn, user: dict, owner_password: str, passphrase: str, name: str,
                              payload: bytes) -> bytes:
    """Encrypt an exported personal report after re-confirming the owner."""
    _confirm_owner(conn, user, owner_password, f"personal_export:{name}")
    return encrypt_bytes(payload, passphrase)
