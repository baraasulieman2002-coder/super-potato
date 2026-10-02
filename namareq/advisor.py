"""Owner Financial Advisor — rule-based, traceable, read-only.

Every recommendation is generated from recorded data by an explicit rule and
stores its evidence: why it was generated, the supporting numbers, the date
range, the assumption, the potential benefit and the risk/uncertainty.  All
text is rendered from bilingual message templates (see i18n "advice.*").

Safeguards (spec section 14): the advisor only READS.  It never posts,
moves money, submits payments, takes loans, invests, or edits records.  The
owner can dismiss, postpone or complete each item; every decision is kept in
``advice_actions``.  No data is sent to any external service.  Rules that
need data Namareq does not hold yet (e.g. slow-moving stock needs the
inventory module) stay silent instead of guessing.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, timedelta

from . import debts, forecast, ledger, loans, metrics, planning
from .db import FinanceError, profile_id, rows, transaction, utcnow
from .permissions import can_access

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _advice(rule, scope, severity, key, params, *, numbers, date_range, assumption, benefit, risk,
            why=None, assumption_params=None):
    """``numbers`` is a list of (label_key, value, unit) where unit is
    'money' (minor units), 'pct', 'months', 'days', 'count' or 'ratio'."""
    return {"rule_code": rule, "scope": scope, "severity": severity, "fingerprint": f"{scope}:{rule}:{key}",
            "params": params,
            "evidence": {"why": why or f"advice.{rule}.why", "numbers": [list(n) for n in numbers],
                         "date_range": list(date_range), "assumption": assumption,
                         "assumption_params": assumption_params or {}, "benefit": benefit, "risk": risk}}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def rule_cash_shortage(conn, user, scope, today):
    fc = forecast.cash_forecast(conn, user, scope, today, 90)
    if not fc["first_shortage_date"]:
        return []
    days = fc["days_until_shortage"]
    base = fc["assumptions"]["baseline"]
    return [_advice(
        "cash_shortage", scope, "critical" if days <= 14 else "warning", fc["first_shortage_date"][:7],
        {"days": days, "date": fc["first_shortage_date"]},
        numbers=[("num.opening_cash", fc["opening_minor"], "money"),
                 ("num.scheduled_outflows", -sum(e["amount_minor"] for e in fc["events"] if e["amount_minor"] < 0), "money"),
                 ("num.scheduled_inflows", sum(e["amount_minor"] for e in fc["events"] if e["amount_minor"] > 0), "money"),
                 ("num.daily_baseline_income", base["daily_income_minor"], "money"),
                 ("num.daily_baseline_spending", base["daily_expense_minor"], "money"),
                 ("num.lowest_balance", fc["lowest_balance_minor"], "money")],
        date_range=(today, fc["end"]), assumption="assume.forecast",
        assumption_params={"days": base["days"],
                           "excluded": fc["assumptions"]["excluded"]["overdue_receivables_minor"]},
        benefit="benefit.cash_shortage", risk="risk.forecast")]


def rule_reserve_coverage(conn, user, scope, today):
    if scope != "business":
        return []
    m = metrics.business_metrics(conn, user, today)
    months = m["values"]["reserve_months"]
    if months is None or months >= 3:
        return []
    return [_advice(
        "reserve_coverage", scope, "warning" if months < 1 else "info", today[:7], {"months": months},
        numbers=[("num.liquid_cash", m["liquid_minor"] + m["savings_minor"], "money"),
                 ("num.avg_fixed_costs", m["avg_monthly_fixed_costs_minor"], "money"),
                 ("num.coverage_months", months, "months")],
        date_range=m["period"], assumption="assume.fixed_costs", benefit="benefit.reserve",
        risk="risk.reserve")]


def rule_receivable_concentration(conn, user, scope, today):
    if scope != "business":
        return []
    aging = debts.aging_report(conn, user, "business", "receivable", today)
    overdue_total = aging["grand_total"] - aging["totals"]["current"]
    if overdue_total <= 0:
        return []
    parties = sorted(({"party": p["party"], "overdue": p["total"] - p["current"]} for p in aging["parties"]),
                     key=lambda x: -x["overdue"])
    parties = [p for p in parties if p["overdue"] > 0]
    top = parties[:3]
    share = round(100 * sum(p["overdue"] for p in top) / overdue_total, 1)
    if len(parties) < 2 and overdue_total < 50_000:
        return []
    numbers = [("num.overdue_receivables", overdue_total, "money"), ("num.top_share", share, "pct")]
    numbers += [(f"party:{p['party']}", p["overdue"], "money") for p in top]
    return [_advice(
        "receivable_concentration", scope, "warning" if share >= 50 else "info", today[:7],
        {"count": len(top), "share": share},
        numbers=numbers, date_range=(today, today), assumption="assume.aging",
        benefit="benefit.collections", risk="risk.collections")]


def rule_personal_payee_in_business(conn, user, scope, today):
    """A business expense paid to a payee the owner also pays personally may
    be a personal cost that should be an owner withdrawal."""
    if scope != "business" or not can_access(conn, user, "personal"):
        return []
    bpid, ppid = profile_id(conn, "business"), profile_id(conn, "personal")
    since = (date.fromisoformat(today) - timedelta(days=60)).isoformat()
    out = []
    for e in rows(conn, """SELECT b.* FROM expense_transactions b WHERE b.profile_id=? AND b.status='posted'
                           AND b.txn_date >= ? AND b.payee IS NOT NULL AND trim(b.payee) <> ''
                           AND EXISTS (SELECT 1 FROM expense_transactions p WHERE p.profile_id=? AND p.status='posted'
                                       AND lower(trim(p.payee)) = lower(trim(b.payee)))""", (bpid, since, ppid)):
        out.append(_advice(
            "personal_payee_in_business", scope, "warning", f"exp{e['id']}",
            {"payee": e["payee"], "amount": e["amount_minor"] + e["tax_minor"], "date": e["txn_date"]},
            numbers=[("num.amount", e["amount_minor"] + e["tax_minor"], "money"), ("num.expense_ref", e["id"], "count")],
            date_range=(e["txn_date"], e["txn_date"]), assumption="assume.payee_match",
            benefit="benefit.separation", risk="risk.payee_match"))
    return out


def rule_duplicate_expenses(conn, user, scope, today):
    pid = profile_id(conn, scope)
    since = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    out = []
    for a in rows(conn, """SELECT a.id aid, b.id bid, a.payee, a.amount_minor + a.tax_minor amt, a.txn_date ad, b.txn_date bd
                           FROM expense_transactions a JOIN expense_transactions b
                             ON a.profile_id=b.profile_id AND a.id < b.id AND lower(trim(a.payee))=lower(trim(b.payee))
                            AND a.amount_minor + a.tax_minor = b.amount_minor + b.tax_minor
                            AND abs(julianday(a.txn_date) - julianday(b.txn_date)) <= 3
                           WHERE a.profile_id=? AND a.status='posted' AND b.status='posted' AND b.txn_date >= ?
                             AND a.payee IS NOT NULL AND a.bill_id IS NULL AND b.bill_id IS NULL""", (pid, since)):
        out.append(_advice(
            "duplicate_expense", scope, "warning", f"{a['aid']}-{a['bid']}",
            {"payee": a["payee"], "amount": a["amt"]},
            numbers=[("num.amount", a["amt"], "money"), ("num.expense_ref", a["aid"], "count"),
                     ("num.expense_ref", a["bid"], "count")],
            date_range=(a["ad"], a["bd"]), assumption="assume.duplicate",
            benefit="benefit.duplicate", risk="risk.duplicate"))
    return out


def rule_category_spike(conn, user, scope, today):
    """Last 30 days vs the average of the three preceding 30-day windows."""
    pid = profile_id(conn, scope)
    t = date.fromisoformat(today)
    cur = ledger.category_totals(conn, pid, "expense", (t - timedelta(days=29)).isoformat(), today)
    hist_start, hist_end = (t - timedelta(days=119)).isoformat(), (t - timedelta(days=30)).isoformat()
    hist = ledger.category_totals(conn, pid, "expense", hist_start, hist_end)
    first = conn.execute("SELECT MIN(entry_date) FROM ledger_entries WHERE profile_id=?", (pid,)).fetchone()[0]
    if not first or first > hist_start:
        return []  # not enough history for a fair comparison
    minimum = 50_000 if scope == "business" else 20_000
    table = "expense_categories"
    out = []
    for cid, amount in cur.items():
        avg = round(hist.get(cid, 0) / 3)
        if avg <= 0 or amount - avg < minimum:
            continue
        pct = round(100 * (amount - avg) / avg, 1)
        if pct < 20:
            continue
        cat = conn.execute(f"SELECT * FROM {table} WHERE id=?", (cid,)).fetchone()
        out.append(_advice(
            "category_spike", scope, "info", f"{cid}:{today[:7]}",
            {"category_en": cat["name_en"], "category_ar": cat["name_ar"], "pct": pct},
            numbers=[("num.last_30_days", amount, "money"), ("num.three_window_avg", avg, "money"),
                     ("num.change", pct, "pct")],
            date_range=(hist_start, today), assumption="assume.spike", benefit="benefit.spending",
            risk="risk.spike"))
    return out


def rule_unexpected_expense(conn, user, scope, today):
    """A recent expense more than 3x the median of its category (needs at
    least 5 earlier transactions in that category)."""
    pid = profile_id(conn, scope)
    t = date.fromisoformat(today)
    out = []
    for e in rows(conn, """SELECT * FROM expense_transactions WHERE profile_id=? AND status='posted'
                           AND txn_date >= ?""", (pid, (t - timedelta(days=14)).isoformat())):
        prior = [r["v"] for r in conn.execute(
            """SELECT amount_minor + tax_minor v FROM expense_transactions WHERE profile_id=? AND category_id=?
               AND status='posted' AND txn_date < ? AND txn_date >= ?""",
            (pid, e["category_id"], e["txn_date"], (t - timedelta(days=180)).isoformat()))]
        if len(prior) < 5:
            continue
        med = statistics.median(prior)
        amt = e["amount_minor"] + e["tax_minor"]
        if med > 0 and amt > 3 * med:
            out.append(_advice(
                "unexpected_expense", scope, "info", f"exp{e['id']}",
                {"payee": e["payee"] or e["description"], "amount": amt, "times": round(amt / med, 1)},
                numbers=[("num.amount", amt, "money"), ("num.category_median", round(med), "money"),
                         ("num.prior_count", len(prior), "count")],
                date_range=((t - timedelta(days=180)).isoformat(), today), assumption="assume.median",
                benefit="benefit.spending", risk="risk.unexpected"))
    return out


def rule_loan_extra_payment(conn, user, scope, today):
    pid = profile_id(conn, scope)
    out = []
    for ln in rows(conn, """SELECT * FROM loans WHERE profile_id=? AND role='borrower' AND status IN ('active','overdue')
                            AND interest_method='reducing_balance' AND CAST(annual_rate AS REAL) > 0""", (pid,)):
        extra = 50_000 if ln["installment_minor"] >= 100_000 else max(1000, ln["installment_minor"] // 10)
        w = loans.what_if_extra(conn, user, ln["id"], extra)
        if w["interest_saved_minor"] <= 0:
            continue
        out.append(_advice(
            "loan_extra_payment", scope, "info", f"loan{ln['id']}",
            {"extra": extra, "lender": ln["lender"], "saved": w["interest_saved_minor"],
             "periods": w["periods_saved"]},
            numbers=[("num.outstanding_principal", w["balance_minor"], "money"),
                     ("num.installment", w["installment_minor"], "money"), ("num.extra", extra, "money"),
                     ("num.interest_now", w["base_interest_minor"], "money"),
                     ("num.interest_with_extra", w["new_interest_minor"], "money"),
                     ("num.payments_now", w["base_periods"], "count"),
                     ("num.payments_with_extra", w["new_periods"], "count")],
            date_range=(today, ln["maturity_date"]), assumption="assume.loan_extra",
            assumption_params={"rate": ln["annual_rate"]},
            benefit="benefit.loan_extra", risk="risk.loan_extra"))
    return out


def rule_loan_missed(conn, user, scope, today):
    out = []
    for ln in loans.list_loans(conn, user, scope, today):
        if ln["role"] == "borrower" and ln["missed_count"]:
            out.append(_advice(
                "loan_missed", scope, "critical", f"loan{ln['id']}:{ln['missed_count']}",
                {"lender": ln["lender"], "count": ln["missed_count"], "amount": ln["missed_amount_minor"]},
                numbers=[("num.missed_installments", ln["missed_count"], "count"),
                         ("num.missed_amount", ln["missed_amount_minor"], "money")],
                date_range=(ln["start_date"], today), assumption="assume.schedule",
                benefit="benefit.loan_missed", risk="risk.loan_missed"))
    return out


def rule_overdue_bills(conn, user, scope, today):
    from . import bills
    overdue = bills.bill_alerts(conn, user, scope, today)["overdue"]
    if not overdue:
        return []
    total = sum(b["remaining_minor"] for b in overdue)
    oldest = min(b["due_date"] for b in overdue)
    return [_advice(
        "overdue_bills", scope, "warning", f"{today}", {"count": len(overdue), "amount": total},
        numbers=[("num.overdue_bills", len(overdue), "count"), ("num.amount", total, "money")]
        + [(f"party:{b['payee']}", b["remaining_minor"], "money") for b in overdue[:5]],
        date_range=(oldest, today), assumption="assume.schedule", benefit="benefit.bills",
        risk="risk.bills")]


def rule_supplier_pressure(conn, user, scope, today):
    if scope != "business":
        return []
    m = metrics.business_metrics(conn, user, today)
    due = m["payables_due_14d_minor"]
    if due <= 0 or due < 0.5 * max(m["liquid_minor"], 1):
        return []
    return [_advice(
        "supplier_pressure", scope, "warning", today[:7], {"amount": due},
        numbers=[("num.payables_14d", due, "money"), ("num.liquid_cash", m["liquid_minor"], "money"),
                 ("num.ratio", round(due / max(m["liquid_minor"], 1), 2), "ratio")],
        date_range=(today, (date.fromisoformat(today) + timedelta(days=14)).isoformat()),
        assumption="assume.schedule", benefit="benefit.suppliers", risk="risk.suppliers")]


def rule_budget_risk(conn, user, scope, today):
    pid = profile_id(conn, scope)
    out = []
    for b in rows(conn, "SELECT id FROM budgets WHERE profile_id=? AND start_date <= ? AND end_date >= ?", (pid, today, today)):
        bva = planning.budget_vs_actual(conn, user, b["id"], today)
        for ln in bva["lines"]:
            if not (ln["over_budget"] or ln["at_risk"]):
                continue
            name_en = ln["exp_en"] or ln["line_type"]
            name_ar = ln["exp_ar"] or ln["line_type"]
            out.append(_advice(
                "budget_risk", scope, "warning" if ln["over_budget"] else "info", f"{b['id']}:{ln['id']}",
                {"category_en": name_en, "category_ar": name_ar, "over": ln["over_budget"],
                 "pct": ln["pct_used"]},
                numbers=[("num.budgeted", ln["budgeted_minor"], "money"), ("num.actual", ln["actual_minor"], "money"),
                         ("num.projected", ln["projected_minor"], "money")],
                date_range=(bva["budget"]["start_date"], today), assumption="assume.budget_pace",
                benefit="benefit.spending", risk="risk.budget"))
    return out


def rule_emergency_fund(conn, user, scope, today):
    if scope != "personal":
        return []
    m = metrics.personal_metrics(conn, user, today)
    months = m["values"]["emergency_months"]
    if months is None or months >= 3:
        return []
    target = 3 * m["avg_monthly_expenses_minor"]
    return [_advice(
        "emergency_fund", scope, "warning" if months < 1 else "info", today[:7], {"months": months},
        numbers=[("num.liquid_cash", m["liquid_minor"] + m["savings_minor"], "money"),
                 ("num.avg_expenses", m["avg_monthly_expenses_minor"], "money"),
                 ("num.coverage_months", months, "months"), ("num.three_month_target", target, "money")],
        date_range=m["period"], assumption="assume.emergency", benefit="benefit.emergency",
        risk="risk.emergency")]


def rule_debt_to_income(conn, user, scope, today):
    if scope != "personal":
        return []
    m = metrics.personal_metrics(conn, user, today)
    dti = m["values"]["debt_to_income_pct"]
    if dti is None or dti < 40:
        return []
    return [_advice(
        "debt_to_income", scope, "warning", today[:7], {"pct": dti},
        numbers=[("num.monthly_debt", m["monthly_debt_service_minor"], "money"),
                 ("num.avg_income", m["avg_monthly_income_minor"], "money"), ("num.ratio_pct", dti, "pct")],
        date_range=m["period"], assumption="assume.dti", benefit="benefit.dti", risk="risk.dti")]


def rule_goal_behind(conn, user, scope, today):
    out = []
    for g in planning.list_goals(conn, user, scope, today):
        if g["status"] != "active" or g["on_track"] or g["remaining_minor"] == 0:
            continue
        out.append(_advice(
            "goal_behind", scope, "info", f"goal{g['id']}:{today[:7]}",
            {"goal_en": g["name_en"], "goal_ar": g["name_ar"], "required": g["required_monthly_minor"]},
            numbers=[("num.target", g["target_minor"], "money"), ("num.current", g["current_minor"], "money"),
                     ("num.required_monthly", g["required_monthly_minor"], "money"),
                     ("num.recent_pace", g["recent_monthly_pace_minor"], "money"),
                     ("num.months_left", g["months_left"], "months")],
            date_range=g["pace_window"], assumption="assume.goal_pace", benefit="benefit.goal", risk="risk.goal"))
    return out


RULES = (rule_cash_shortage, rule_reserve_coverage, rule_receivable_concentration,
         rule_personal_payee_in_business, rule_duplicate_expenses, rule_category_spike,
         rule_unexpected_expense, rule_loan_extra_payment, rule_loan_missed, rule_overdue_bills,
         rule_supplier_pressure, rule_budget_risk, rule_emergency_fund, rule_debt_to_income,
         rule_goal_behind)


# ---------------------------------------------------------------------------
# Generation, storage and owner decisions
# ---------------------------------------------------------------------------
def evaluate(conn, user: dict, scope: str, today: str) -> list[dict]:
    """Run every rule for a scope without storing anything."""
    out = []
    for rule in RULES:
        out.extend(rule(conn, user, scope, today))
    return out


def generate(conn, user: dict, today: str) -> dict:
    """Evaluate rules for every scope the user may see and store results.
    Existing open items are refreshed; items the owner dismissed or completed
    are not re-raised for the same fingerprint; items no longer triggered
    are marked superseded (kept for history)."""
    summary = {"created": 0, "updated": 0, "superseded": 0}
    for scope in ("business", "personal"):
        if not can_access(conn, user, scope):
            continue
        found = {a["fingerprint"]: a for a in evaluate(conn, user, scope, today)}
        with transaction(conn):
            for fp, a in found.items():
                existing = conn.execute("SELECT * FROM financial_advice WHERE fingerprint=? ORDER BY id DESC LIMIT 1",
                                        (fp,)).fetchone()
                if existing and existing["status"] in ("dismissed", "completed"):
                    continue
                if existing and existing["status"] in ("open", "postponed"):
                    conn.execute("""UPDATE financial_advice SET params_json=?, evidence_json=?, severity=?, as_of=?,
                                    updated_at=? WHERE id=?""",
                                 (json.dumps(a["params"], ensure_ascii=False), json.dumps(a["evidence"], ensure_ascii=False),
                                  a["severity"], today, utcnow(), existing["id"]))
                    summary["updated"] += 1
                    continue
                conn.execute("""INSERT INTO financial_advice(scope, rule_code, fingerprint, severity, params_json,
                                evidence_json, as_of) VALUES (?,?,?,?,?,?,?)""",
                             (scope, a["rule_code"], fp, a["severity"], json.dumps(a["params"], ensure_ascii=False),
                              json.dumps(a["evidence"], ensure_ascii=False), today))
                summary["created"] += 1
            for row in rows(conn, "SELECT id, fingerprint FROM financial_advice WHERE scope=? AND status='open'", (scope,)):
                if row["fingerprint"] not in found:
                    conn.execute("UPDATE financial_advice SET status='superseded', updated_at=? WHERE id=?",
                                 (utcnow(), row["id"]))
                    summary["superseded"] += 1
    return summary


def list_advice(conn, user: dict, scope: str, today: str, include_closed: bool = False) -> list[dict]:
    if not can_access(conn, user, scope):
        raise FinanceError("error.access_denied")
    if scope == "personal":
        from .permissions import log_personal_access
        log_personal_access(conn, user, "advice", "read", True)
    sql = "SELECT * FROM financial_advice WHERE scope=?"
    if not include_closed:
        sql += " AND (status='open' OR (status='postponed' AND COALESCE(postponed_until,'') <= ?))"
        params = (scope, today)
    else:
        params = (scope,)
    out = []
    for a in rows(conn, sql + " ORDER BY id DESC", params):
        a["params"] = json.loads(a.pop("params_json"))
        a["evidence"] = json.loads(a.pop("evidence_json"))
        a["actions"] = rows(conn, "SELECT * FROM advice_actions WHERE advice_id=? ORDER BY id", (a["id"],))
        out.append(a)
    out.sort(key=lambda a: (SEVERITY_ORDER[a["severity"]], -a["id"]))
    return out


def act(conn, user: dict, advice_id: int, action: str, note: str | None = None,
        postpone_until: str | None = None) -> None:
    """Record the owner's decision on a recommendation.  This changes only
    the advice item's status — never any financial record."""
    a = conn.execute("SELECT * FROM financial_advice WHERE id=?", (advice_id,)).fetchone()
    if a is None:
        raise FinanceError("error.unknown_record")
    if not can_access(conn, user, a["scope"]):
        raise FinanceError("error.access_denied")
    if action not in ("dismissed", "postponed", "completed", "reopened"):
        raise FinanceError("error.invalid_status")
    if action == "postponed":
        postpone_until = ledger.validate_date(postpone_until or "")
    status = {"reopened": "open"}.get(action, action)
    with transaction(conn):
        conn.execute("UPDATE financial_advice SET status=?, postponed_until=?, updated_at=? WHERE id=?",
                     (status, postpone_until if action == "postponed" else None, utcnow(), advice_id))
        conn.execute("INSERT INTO advice_actions(advice_id, user_id, action, note) VALUES (?,?,?,?)",
                     (advice_id, user["id"], action, note))
