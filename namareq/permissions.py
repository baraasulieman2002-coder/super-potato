"""Users, roles, profile access and personal-finance privacy.

Rules (spec section 16):
* The owner sees everything.
* The accountant sees business finance by default.
* Personal finance is visible only to the owner unless the owner explicitly
  grants access, re-confirming with their password.  Only the accountant role
  may ever receive a personal grant; managers, cashiers, technicians and
  stockkeepers can never see personal finances.
* Every access attempt on personal records is logged (allowed or denied).
"""
from __future__ import annotations

import hashlib
import hmac
import os

from .db import FinanceError, audit, profile_kind, transaction, utcnow

ROLES = ("owner", "manager", "accountant", "cashier", "technician", "stockkeeper")
PERSONAL_GRANTABLE_ROLES = ("accountant",)
BUSINESS_GRANTABLE_ROLES = ("manager", "accountant")

_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex),
                                     int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def create_user(conn, username: str, display_name: str, password: str, role: str,
                lang: str = "ar") -> int:
    if role not in ROLES:
        raise FinanceError("error.invalid_role")
    if len(password) < 8:
        raise FinanceError("error.password_too_short")
    cur = conn.execute(
        """INSERT INTO users(username, display_name, password_hash, role, preferred_lang)
           VALUES (?,?,?,?,?)""",
        (username, display_name, hash_password(password), role, lang),
    )
    return cur.lastrowid


def authenticate(conn, username: str, password: str):
    row = conn.execute("SELECT * FROM users WHERE username = ? AND is_active = 1",
                       (username,)).fetchone()
    if row and verify_password(password, row["password_hash"]):
        return dict(row)
    return None


def get_user(conn, user_id: int):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def _active_grant(conn, user_id: int, kind: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM finance_access_grants
           WHERE user_id = ? AND profile_kind = ? AND revoked_at IS NULL""",
        (user_id, kind),
    ).fetchone() is not None


def can_access(conn, user: dict, kind: str) -> bool:
    if not user or not user.get("is_active", 1):
        return False
    role = user["role"]
    if role == "owner":
        return True
    if kind == "business":
        return role == "accountant" or (role in BUSINESS_GRANTABLE_ROLES and
                                         _active_grant(conn, user["id"], "business"))
    # personal
    return role in PERSONAL_GRANTABLE_ROLES and _active_grant(conn, user["id"], "personal")


_REQUEST_SEEN: dict[int, set] = {}


def begin_request(conn) -> None:
    """Called by the web layer per request: personal-access log entries are
    then written once per (resource, action, outcome) per request instead of
    once per internal service call.  Without it, every check is logged."""
    _REQUEST_SEEN[id(conn)] = set()


def end_request(conn) -> None:
    _REQUEST_SEEN.pop(id(conn), None)


def log_personal_access(conn, user: dict, resource: str, action: str, allowed: bool) -> None:
    seen = _REQUEST_SEEN.get(id(conn))
    if seen is not None:
        key = (user["id"] if user else 0, resource, action, allowed)
        if key in seen:
            return
        seen.add(key)
    conn.execute(
        "INSERT INTO personal_access_log(user_id, resource, action, allowed) VALUES (?,?,?,?)",
        (user["id"] if user else 0, resource, action, int(allowed)),
    )


def require(conn, user: dict, kind: str, resource: str = "finance", action: str = "read") -> None:
    """Raise unless ``user`` may use profile ``kind``.  Logs personal access."""
    allowed = can_access(conn, user, kind)
    if kind == "personal" and user:
        log_personal_access(conn, user, resource, action, allowed)
    if not allowed:
        raise FinanceError("error.access_denied")


def require_profile(conn, user: dict, pid: int, resource="finance", action="read") -> str:
    kind = profile_kind(conn, pid)
    require(conn, user, kind, resource, action)
    return kind


def require_owner(user: dict) -> None:
    if not user or user["role"] != "owner":
        raise FinanceError("error.owner_only")


def accessible_kinds(conn, user: dict) -> list[str]:
    return [k for k in ("business", "personal") if can_access(conn, user, k)]


def grant_access(conn, owner: dict, user_id: int, kind: str, owner_password: str,
                 note: str | None = None) -> int:
    """Grant profile access.  Personal grants need the owner's password again
    as explicit confirmation, and only an accountant can receive one."""
    require_owner(owner)
    target = get_user(conn, user_id)
    if target is None:
        raise FinanceError("error.unknown_user")
    stored = conn.execute("SELECT password_hash FROM users WHERE id = ?", (owner["id"],)).fetchone()
    if not verify_password(owner_password, stored["password_hash"]):
        raise FinanceError("error.confirmation_failed")
    allowed_roles = PERSONAL_GRANTABLE_ROLES if kind == "personal" else BUSINESS_GRANTABLE_ROLES
    if target["role"] not in allowed_roles:
        raise FinanceError("error.role_cannot_receive_grant", role=target["role"])
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO finance_access_grants(user_id, profile_kind, granted_by, confirmed_at, note)
               VALUES (?,?,?,?,?)""",
            (user_id, kind, owner["id"], utcnow(), note),
        )
        audit(conn, owner["id"], "finance_access_grants", cur.lastrowid, "grant",
              {"user_id": user_id, "profile_kind": kind})
    return cur.lastrowid


def revoke_access(conn, owner: dict, grant_id: int) -> None:
    require_owner(owner)
    with transaction(conn):
        conn.execute(
            "UPDATE finance_access_grants SET revoked_at = ?, revoked_by = ? WHERE id = ? AND revoked_at IS NULL",
            (utcnow(), owner["id"], grant_id),
        )
        audit(conn, owner["id"], "finance_access_grants", grant_id, "revoke")
