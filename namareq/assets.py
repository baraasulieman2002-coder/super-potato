"""Assets (with depreciation) and the liabilities register.

Valuation policy:
* Business assets use the cost model: carrying value = cost - accumulated
  depreciation.  The "current estimated value" is informational only and
  never changes business profit.
* Personal assets (property, vehicles, gold, investments) are carried at the
  owner's estimated value.  Revaluations post against a personal
  revaluation reserve so personal net worth reflects estimates while
  personal income is not distorted.
"""
from __future__ import annotations

from decimal import Decimal

from . import chart, dates, ledger
from .accounts import opening_equity_account
from .db import (FinanceError, account, audit, expense_category, income_category, profile_id,
                 rows, system_account, transaction)
from .money import round_minor
from .permissions import require

ASSET_CLASSES = {
    "business": ("inventory", "shop_equipment", "computers", "furniture", "vehicles", "deposits",
                 "other_business_asset"),
    "personal": ("property", "vehicles", "gold", "investments", "other_personal_asset"),
}
LIABILITY_CLASSES = ("loan", "credit_card", "tax", "salary", "deposit_received", "other")


def _kind(conn, pid):
    return conn.execute("SELECT kind FROM financial_profiles WHERE id=?", (pid,)).fetchone()["kind"]


def _class_account(conn, profile: str, asset_class: str, contra: bool = False) -> int:
    pid = profile_id(conn, profile)
    code = f"{'accdep' if contra else 'asset'}_{asset_class}"
    row = conn.execute("SELECT id FROM financial_accounts WHERE profile_id=? AND system_code=?",
                       (pid, code)).fetchone()
    if row:
        return row["id"]
    if asset_class == "inventory" and not contra:
        return system_account(conn, "business", "inventory")["id"]
    label_en = asset_class.replace("_", " ").title()
    acct_type = "accumulated_depreciation" if contra else (
        "investment" if asset_class == "investments" else "fixed_asset")
    cur = conn.execute(
        """INSERT INTO financial_accounts(profile_id, account_type, name_en, name_ar, system_code)
           VALUES (?,?,?,?,?)""",
        (pid, acct_type, f"{'Accumulated depreciation — ' if contra else ''}{label_en}",
         f"{'مجمع الاستهلاك — ' if contra else 'أصول — '}{ASSET_CLASS_AR.get(asset_class, asset_class)}",
         code))
    return cur.lastrowid


ASSET_CLASS_AR = {
    "inventory": "المخزون", "shop_equipment": "معدات المحل", "computers": "أجهزة الحاسوب",
    "furniture": "الأثاث", "vehicles": "المركبات", "deposits": "التأمينات المستردة",
    "other_business_asset": "أصول تجارية أخرى", "property": "العقارات", "gold": "الذهب",
    "investments": "الاستثمارات", "other_personal_asset": "أصول شخصية أخرى",
}


def depreciation_schedule(cost_minor: int, salvage_minor: int, life_months: int, method: str,
                          purchase_date: str) -> list[dict]:
    """Monthly depreciation from the month after purchase (full-month
    convention).  Straight line: (cost - salvage) / life.  Declining balance:
    double-declining rate 2 / life applied to net book value, never going
    below salvage; the last month takes the remainder."""
    if method == "none" or not life_months:
        return []
    if life_months <= 0 or salvage_minor > cost_minor:
        raise FinanceError("error.invalid_depreciation_terms")
    base = cost_minor - salvage_minor
    out, nbv, accumulated = [], cost_minor, 0
    for m in range(1, life_months + 1):
        month_end = dates.month_bounds(dates.add_months(dates.month_bounds(purchase_date)[0], m))[1]
        if method == "straight_line":
            amount = round_minor(Decimal(base) * m / life_months) - accumulated
        else:
            amount = round_minor(Decimal(nbv) * Decimal(2) / life_months)
            if m == life_months or nbv - amount < salvage_minor:
                amount = nbv - salvage_minor
        amount = max(0, amount)
        accumulated += amount
        nbv -= amount
        out.append({"month_end": month_end.isoformat(), "amount_minor": amount,
                    "accumulated_minor": accumulated, "nbv_minor": nbv})
        if nbv <= salvage_minor:
            break
    return out


def create_asset(conn, user: dict, *, profile: str, asset_class: str, name_en: str, name_ar: str,
                 purchase_value_minor: int, purchase_date: str, funding: str,
                 cash_account_id: int | None = None, current_value_minor: int | None = None,
                 useful_life_months: int | None = None, salvage_value_minor: int = 0,
                 depreciation_method: str = "none", related_loan_id: int | None = None,
                 notes: str | None = None) -> int:
    """``funding``: 'cash' (paid from ``cash_account_id``), 'opening' (owned
    before Namareq), or 'already_posted' (the purchase is already in the
    ledger, e.g. financed through a recorded loan)."""
    require(conn, user, profile, "assets", "write")
    if asset_class not in ASSET_CLASSES[profile]:
        raise FinanceError("error.invalid_asset_class")
    if not name_en.strip() or not name_ar.strip():
        raise FinanceError("error.bilingual_name_required")
    if purchase_value_minor < 0:
        raise FinanceError("error.amount_must_be_positive")
    if depreciation_method not in ("none", "straight_line", "declining_balance"):
        raise FinanceError("error.invalid_depreciation_terms")
    if profile == "personal" and depreciation_method != "none":
        raise FinanceError("error.personal_assets_use_revaluation")
    if depreciation_method != "none" and not useful_life_months:
        raise FinanceError("error.invalid_depreciation_terms")
    purchase_date = ledger.validate_date(purchase_date)
    pid = profile_id(conn, profile)
    asset_acct = _class_account(conn, profile, asset_class)
    accdep = _class_account(conn, profile, asset_class, contra=True) if depreciation_method != "none" else None
    with transaction(conn):
        entry = None
        if funding == "cash":
            if cash_account_id is None or account(conn, cash_account_id)["profile_id"] != pid:
                raise FinanceError("error.account_wrong_profile")
            credit = cash_account_id
        elif funding == "opening":
            credit = opening_equity_account(conn, profile, "JOD")
        elif funding == "already_posted":
            credit = None
        else:
            raise FinanceError("error.invalid_origin")
        if credit and purchase_value_minor:
            entry = ledger.post_entry(conn, profile_id=pid, entry_date=purchase_date,
                                      entry_type="asset_acquisition", description=f"Asset: {name_en}",
                                      lines=[ledger.dr(asset_acct, purchase_value_minor),
                                             ledger.cr(credit, purchase_value_minor)],
                                      user_id=user["id"])
        cur = conn.execute(
            """INSERT INTO assets(profile_id, asset_class, name_en, name_ar, purchase_value_minor,
                   current_value_minor, purchase_date, useful_life_months, salvage_value_minor,
                   depreciation_method, asset_account_id, accum_dep_account_id, related_loan_id,
                   acquisition_entry_id, notes, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, asset_class, name_en.strip(), name_ar.strip(), purchase_value_minor,
             current_value_minor if current_value_minor is not None else purchase_value_minor,
             purchase_date, useful_life_months, salvage_value_minor, depreciation_method, asset_acct,
             accdep, related_loan_id, entry, notes, user["id"]))
        asset_id = cur.lastrowid
        if profile == "personal" and current_value_minor is not None and current_value_minor != purchase_value_minor:
            _post_revaluation(conn, user, pid, asset_acct, current_value_minor - purchase_value_minor,
                              purchase_date, name_en)
        audit(conn, user["id"], "assets", asset_id, "create", None, pid)
    return asset_id


def _post_revaluation(conn, user, pid, asset_acct, delta, on, name):
    reserve = system_account(conn, "personal", "revaluation")["id"]
    amt = abs(delta)
    lines = ([ledger.dr(asset_acct, amt), ledger.cr(reserve, amt)] if delta > 0
             else [ledger.dr(reserve, amt), ledger.cr(asset_acct, amt)])
    return ledger.post_entry(conn, profile_id=pid, entry_date=on, entry_type="revaluation",
                             description=f"Revaluation: {name}", lines=lines, user_id=user["id"])


def _asset(conn, asset_id) -> dict:
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if row is None:
        raise FinanceError("error.unknown_record")
    return dict(row)


def accumulated_depreciation(conn, asset: dict, as_of: str | None = None) -> int:
    clause = " AND e.entry_date <= ?" if as_of else ""
    params = [asset["id"], *([as_of] if as_of else [])]
    row = conn.execute(
        f"""SELECT COALESCE(SUM(l.credit_minor - l.debit_minor),0) FROM ledger_lines l
            JOIN ledger_entries e ON e.id=l.entry_id
            WHERE e.related_entity_type='asset_depreciation' AND e.related_entity_id=? AND l.account_id IS NOT NULL {clause}""",
        params).fetchone()
    return row[0]


def post_depreciation(conn, user: dict, asset_id: int, through: str) -> int | None:
    """Post depreciation for every month-end up to ``through`` not yet posted."""
    asset = _asset(conn, asset_id)
    kind = _kind(conn, asset["profile_id"])
    require(conn, user, kind, "assets", "write")
    if asset["depreciation_method"] == "none" or asset["disposal_date"]:
        return None
    sched = depreciation_schedule(asset["purchase_value_minor"], asset["salvage_value_minor"],
                                  asset["useful_life_months"], asset["depreciation_method"],
                                  asset["purchase_date"])
    due = [s for s in sched if s["month_end"] <= through]
    if not due:
        return None
    target = due[-1]["accumulated_minor"]
    already = accumulated_depreciation(conn, asset)
    amount = target - already
    if amount <= 0:
        return None
    cat = expense_category(conn, kind, "depreciation")["id"]
    with transaction(conn):
        entry = ledger.post_entry(conn, profile_id=asset["profile_id"], entry_date=due[-1]["month_end"],
                                  entry_type="depreciation", description=f"Depreciation: {asset['name_en']}",
                                  lines=[ledger.dr_expense(cat, amount), ledger.cr(asset["accum_dep_account_id"], amount)],
                                  user_id=user["id"], related_entity_type="asset_depreciation",
                                  related_entity_id=asset_id)
        conn.execute("UPDATE assets SET depreciated_through=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                     (due[-1]["month_end"], asset_id))
    return entry


def revalue(conn, user: dict, asset_id: int, new_value_minor: int, on: str) -> int | None:
    asset = _asset(conn, asset_id)
    kind = _kind(conn, asset["profile_id"])
    require(conn, user, kind, "assets", "write")
    delta = new_value_minor - asset["current_value_minor"]
    with transaction(conn):
        entry = None
        if kind == "personal" and delta:
            entry = _post_revaluation(conn, user, asset["profile_id"], asset["asset_account_id"], delta,
                                      on, asset["name_en"])
        conn.execute("UPDATE assets SET current_value_minor=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                     (new_value_minor, asset_id))
        audit(conn, user["id"], "assets", asset_id, "revalue", {"from": asset["current_value_minor"],
                                                                 "to": new_value_minor}, asset["profile_id"])
    return entry


def carrying_value(conn, asset: dict) -> int:
    if asset["disposal_date"]:
        return 0
    if _kind(conn, asset["profile_id"]) == "personal":
        return asset["current_value_minor"]
    return asset["purchase_value_minor"] - accumulated_depreciation(conn, asset)


def dispose(conn, user: dict, asset_id: int, *, on: str, proceeds_minor: int,
            cash_account_id: int | None = None) -> int:
    asset = _asset(conn, asset_id)
    kind = _kind(conn, asset["profile_id"])
    require(conn, user, kind, "assets", "write")
    if asset["disposal_date"]:
        raise FinanceError("error.asset_already_disposed")
    if kind == "business":
        post_depreciation(conn, user, asset_id, on)
    carrying = carrying_value(conn, asset)
    accdep = accumulated_depreciation(conn, asset) if kind == "business" else 0
    cost_on_books = carrying + accdep  # personal: estimated value; business: cost
    gain = proceeds_minor - carrying
    lines = []
    if proceeds_minor:
        if cash_account_id is None or account(conn, cash_account_id)["profile_id"] != asset["profile_id"]:
            raise FinanceError("error.account_wrong_profile")
        lines.append(ledger.dr(cash_account_id, proceeds_minor))
    if accdep:
        lines.append(ledger.dr(asset["accum_dep_account_id"], accdep))
    lines.append(ledger.cr(asset["asset_account_id"], cost_on_books))
    if gain:
        if kind == "business":
            lines.append(ledger.cr_income(income_category(conn, "business", "other_business_income")["id"], gain)
                         if gain > 0 else
                         ledger.dr_expense(expense_category(conn, "business", "misc_business")["id"], -gain))
        else:
            reserve = system_account(conn, "personal", "revaluation")["id"]
            lines.append(ledger.cr(reserve, gain) if gain > 0 else ledger.dr(reserve, -gain))
    with transaction(conn):
        entry = None
        if sum(1 for ln in lines if ln.debit or ln.credit) >= 2:
            entry = ledger.post_entry(conn, profile_id=asset["profile_id"], entry_date=on,
                                      entry_type="asset_disposal", description=f"Disposal: {asset['name_en']}",
                                      lines=lines, user_id=user["id"], related_entity_type="asset",
                                      related_entity_id=asset_id)
        conn.execute("""UPDATE assets SET disposal_date=?, sale_proceeds_minor=?, current_value_minor=0,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?""",
                     (on, proceeds_minor, asset_id))
    return entry


def list_assets(conn, user: dict, profile: str, include_disposed=False) -> list[dict]:
    require(conn, user, profile, "assets", "read")
    sql = "SELECT * FROM assets WHERE profile_id=?"
    if not include_disposed:
        sql += " AND disposal_date IS NULL"
    out = []
    for a in rows(conn, sql + " ORDER BY asset_class, name_en", (profile_id(conn, profile),)):
        a["accumulated_depreciation_minor"] = accumulated_depreciation(conn, a) if profile == "business" else 0
        a["carrying_value_minor"] = carrying_value(conn, a)
        out.append(a)
    return out


def create_liability(conn, user: dict, *, profile: str, liability_class: str, name_en: str, name_ar: str,
                     account_id: int, related_loan_id: int | None = None, due_date: str | None = None,
                     notes: str | None = None) -> int:
    """Register entry describing a liability.  The amount always comes from
    the linked account's ledger balance, so the register can never disagree
    with the books."""
    require(conn, user, profile, "liabilities", "write")
    if liability_class not in LIABILITY_CLASSES:
        raise FinanceError("error.invalid_liability_class")
    acct = account(conn, account_id)
    if acct["profile_id"] != profile_id(conn, profile):
        raise FinanceError("error.account_wrong_profile")
    if chart.account_class(acct["account_type"]) != "liability":
        raise FinanceError("error.not_a_liability_account")
    cur = conn.execute(
        """INSERT INTO liabilities(profile_id, liability_class, name_en, name_ar, account_id, related_loan_id,
               due_date, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?)""",
        (acct["profile_id"], liability_class, name_en, name_ar, account_id, related_loan_id, due_date, notes,
         user["id"]))
    return cur.lastrowid


def list_liabilities(conn, user: dict, profile: str, as_of: str | None = None) -> list[dict]:
    """All liability accounts with balances, annotated with register details."""
    require(conn, user, profile, "liabilities", "read")
    out = []
    for a in rows(conn, "SELECT * FROM financial_accounts WHERE profile_id=? ORDER BY account_type, name_en",
                  (profile_id(conn, profile),)):
        if chart.account_class(a["account_type"]) != "liability":
            continue
        bal = ledger.account_balance(conn, a["id"], as_of)
        reg = conn.execute("SELECT * FROM liabilities WHERE account_id=?", (a["id"],)).fetchone()
        if bal or reg:
            out.append({"account": a, "balance_minor": bal, "register": dict(reg) if reg else None})
    return out


def depreciate_all(conn, user: dict, through: str) -> list[int]:
    entries = []
    for a in rows(conn, """SELECT id FROM assets WHERE depreciation_method <> 'none' AND disposal_date IS NULL
                           AND profile_id = ?""", (profile_id(conn, "business"),)):
        e = post_depreciation(conn, user, a["id"], through)
        if e:
            entries.append(e)
    return entries

