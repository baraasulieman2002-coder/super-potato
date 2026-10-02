"""Database connection, initialisation, seeding and shared helpers."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import chart

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class FinanceError(Exception):
    """A user-facing error.  ``key`` is an i18n message key so the error is
    shown in Arabic or English; ``params`` fill the message placeholders."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params

    def __str__(self) -> str:  # useful in logs and test failures
        return f"{self.key} {self.params}" if self.params else self.key


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), detect_types=0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


class transaction:
    """Context manager giving an atomic unit of work (supports nesting via
    savepoints, so services can call each other safely)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        depth = _DEPTH.get(id(self.conn), 0)
        self.name = f"sp{depth}"
        if depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute(f"SAVEPOINT {self.name}")
        _DEPTH[id(self.conn)] = depth + 1
        self.depth = depth
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        _DEPTH[id(self.conn)] = self.depth
        if exc_type is None:
            if self.depth == 0:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute(f"RELEASE {self.name}")
        else:
            if self.depth == 0:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute(f"ROLLBACK TO {self.name}")
                self.conn.execute(f"RELEASE {self.name}")
        return False


_DEPTH: dict[int, int] = {}


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed(conn)


def seed(conn: sqlite3.Connection) -> None:
    """Idempotently create the two profiles, system accounts and categories."""
    with transaction(conn):
        for kind, (en, ar) in chart.PROFILES.items():
            conn.execute(
                "INSERT OR IGNORE INTO financial_profiles(kind, name_en, name_ar) VALUES (?,?,?)",
                (kind, en, ar),
            )
        for kind, rows in chart.SYSTEM_ACCOUNTS.items():
            pid = profile_id(conn, kind)
            for code, acct_type, en, ar in rows:
                conn.execute(
                    """INSERT OR IGNORE INTO financial_accounts
                       (profile_id, account_type, name_en, name_ar, system_code)
                       VALUES (?,?,?,?,?)""",
                    (pid, acct_type, en, ar, code),
                )
        for kind, rows in chart.INCOME_CATEGORIES.items():
            for code, en, ar, system_only in rows:
                conn.execute(
                    """INSERT OR IGNORE INTO income_categories
                       (profile_kind, code, name_en, name_ar, system_only) VALUES (?,?,?,?,?)""",
                    (kind, code, en, ar, system_only),
                )
        for kind, rows in chart.EXPENSE_CATEGORIES.items():
            for code, en, ar, fixed, cogs in rows:
                conn.execute(
                    """INSERT OR IGNORE INTO expense_categories
                       (profile_kind, code, name_en, name_ar, is_fixed_cost, is_cost_of_sales)
                       VALUES (?,?,?,?,?,?)""",
                    (kind, code, en, ar, fixed, cogs),
                )


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def profile_id(conn, kind: str) -> int:
    row = conn.execute("SELECT id FROM financial_profiles WHERE kind = ?", (kind,)).fetchone()
    if row is None:
        raise FinanceError("error.unknown_profile", profile=kind)
    return row["id"]


def profile_kind(conn, pid: int) -> str:
    row = conn.execute("SELECT kind FROM financial_profiles WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise FinanceError("error.unknown_profile", profile=pid)
    return row["kind"]


def system_account(conn, kind: str, code: str) -> sqlite3.Row:
    row = conn.execute(
        """SELECT a.* FROM financial_accounts a JOIN financial_profiles p ON p.id = a.profile_id
           WHERE p.kind = ? AND a.system_code = ?""",
        (kind, code),
    ).fetchone()
    if row is None:
        raise FinanceError("error.missing_system_account", code=code)
    return row


def account(conn, account_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM financial_accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise FinanceError("error.unknown_account")
    return row


def income_category(conn, kind: str, code_or_id) -> sqlite3.Row:
    col = "id" if isinstance(code_or_id, int) else "code"
    row = conn.execute(
        f"SELECT * FROM income_categories WHERE profile_kind = ? AND {col} = ?", (kind, code_or_id)
    ).fetchone()
    if row is None:
        raise FinanceError("error.category_wrong_profile")
    return row


def expense_category(conn, kind: str, code_or_id) -> sqlite3.Row:
    col = "id" if isinstance(code_or_id, int) else "code"
    row = conn.execute(
        f"SELECT * FROM expense_categories WHERE profile_kind = ? AND {col} = ?", (kind, code_or_id)
    ).fetchone()
    if row is None:
        raise FinanceError("error.category_wrong_profile")
    return row


def audit(conn, user_id, table: str, record_id, action: str, details=None, profile_id_=None):
    conn.execute(
        """INSERT INTO audit_log(user_id, profile_id, table_name, record_id, action, details_json)
           VALUES (?,?,?,?,?,?)""",
        (user_id, profile_id_, table, record_id, action,
         json.dumps(details, ensure_ascii=False, default=str) if details is not None else None),
    )


def rows(conn, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
