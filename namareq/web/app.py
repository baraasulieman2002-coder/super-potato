"""Namareq Finance web interface (Arabic RTL / English LTR)."""
from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import tempfile
from datetime import date, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request, send_file, session,
                   url_for)

from .. import (accounts, advisor, assets, backup, bills, chart, dates, db, debts, forecast, i18n,
                ledger, loans, metrics, permissions, planning, reports, transactions, transfers)
from ..db import FinanceError
from ..money import MoneyError, fmt, to_minor

PROFILES = ("business", "personal")


def safe_next(target: str | None, fallback: str) -> str:
    """Only allow same-site relative paths (blocks //host and /\\host)."""
    if target and target.startswith("/") and not target.startswith(("//", "/\\")) and "\\" not in target:
        return target
    return fallback


def create_app(db_path: str | None = None, today: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("NAMAREQ_SECRET_KEY") or secrets.token_hex(32)
    app.config["DB_PATH"] = db_path or os.environ.get("NAMAREQ_DB", "namareq.sqlite")
    app.config["FIXED_TODAY"] = today
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    conn0 = db.connect(app.config["DB_PATH"])
    db.init_db(conn0)
    conn0.close()

    # ------------------------------------------------------------------ setup
    @app.before_request
    def _open():
        g.conn = db.connect(app.config["DB_PATH"])
        permissions.begin_request(g.conn)
        g.user = permissions.get_user(g.conn, session["uid"]) if session.get("uid") else None
        if g.user and not g.user["is_active"]:
            g.user = None
        g.lang = session.get("lang") or (g.user or {}).get("preferred_lang") or "ar"
        if request.method == "POST" and request.endpoint not in ("static",):
            if request.form.get("_csrf") != session.get("csrf"):
                abort(400)

    @app.teardown_request
    def _close(exc):
        conn = g.pop("conn", None)
        if conn is not None:
            permissions.end_request(conn)
            conn.close()

    def today() -> str:
        return app.config["FIXED_TODAY"] or date.today().isoformat()

    def t(key, **params):
        for k, v in list(params.items()):
            if k in i18n.MONEY_PARAMS and isinstance(v, int):
                params[k] = fmt(v, lang=g.lang)
        return i18n.translate(g.lang, key, **params)

    def nm(row, prefix="name"):
        if row is None:
            return ""
        key = f"{prefix}_{'ar' if g.lang == 'ar' else 'en'}"
        try:
            return row[key] or row[f"{prefix}_en"]
        except (KeyError, IndexError):
            return ""

    @app.context_processor
    def _ctx():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        kinds = permissions.accessible_kinds(g.conn, g.user) if g.user else []
        def acct_opts(rows_):
            return [(a["id"], f"{nm(a)} ({fmt(a.get('balance_minor', 0), a['currency'], g.lang)})") for a in rows_]

        def opts(rows_):
            return [(r["id"], nm(r)) for r in rows_]

        def enum_opts(values, prefix):
            return [(v, t(prefix + v)) for v in values]

        def advice_text(a, part="title"):
            params = dict(a["params"])
            for base in ("category", "goal"):
                if f"{base}_en" in params:
                    params[base] = params[f"{base}_{'ar' if g.lang == 'ar' else 'en'}"]
            if "over" in params:
                params["state"] = t("advice.state.over" if params["over"] else "advice.state.at_risk")
            return t(f"advice.{a['rule_code']}.{part}", **params)

        def num_value(value, unit):
            if unit == "money":
                return fmt(value, lang=g.lang)
            if unit == "pct":
                return f"{value}%"
            if unit == "months":
                return f"{value} {t('unit.months')}"
            if unit == "days":
                return f"{value} {t('unit.days')}"
            return str(value)

        def num_label(label):
            return label.split(":", 1)[1] if label.startswith("party:") else t(label)

        return {"plain": lambda m: fmt(m, lang="en", with_symbol=False).replace(",", ""),
                "advice_text": advice_text, "num_value": num_value, "num_label": num_label,
                "acct_opts": acct_opts, "opts": opts, "enum_opts": enum_opts, "t": t, "nm": nm, "lang": g.lang, "dir": i18n.DIRECTION[g.lang], "user": g.user,
                "money": lambda m, cur="JOD": fmt(m, cur, g.lang), "csrf": session["csrf"],
                "kinds": kinds, "today": today(), "chart": chart, "is_owner": bool(g.user and g.user["role"] == "owner"),
                "can_combined": "business" in kinds and "personal" in kinds}

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                return redirect(url_for("login", next=request.full_path))
            return fn(*a, **kw)
        return wrapper

    def profile_route(fn):
        """Validates <profile> and checks access before the view runs."""
        @wraps(fn)
        @login_required
        def wrapper(profile, *a, **kw):
            if profile not in PROFILES:
                abort(404)
            try:
                permissions.require(g.conn, g.user, profile, request.endpoint or "page",
                                    "write" if request.method == "POST" else "read")
            except FinanceError:
                return render_template("denied.html", scope=profile), 403
            g.profile = profile
            return fn(profile, *a, **kw)
        return wrapper

    def owner_both(fn):
        @wraps(fn)
        @login_required
        def wrapper(*a, **kw):
            try:
                permissions.require(g.conn, g.user, "business", request.endpoint, "read")
                permissions.require(g.conn, g.user, "personal", request.endpoint, "read")
            except FinanceError:
                return render_template("denied.html", scope="combined"), 403
            g.profile = "combined"
            return fn(*a, **kw)
        return wrapper

    def error_message(exc) -> str:
        if isinstance(exc, FinanceError):
            return t(exc.key, **exc.params)
        if isinstance(exc, MoneyError):
            return t("error.invalid_amount")
        return t("error.generic")

    def handle(action, success_key="msg.saved", redirect_to=None):
        """Run a write action; flash a translated message; keep form on error."""
        try:
            result = action()
        except (FinanceError, MoneyError, ValueError) as exc:
            flash(error_message(exc), "error")
            return None, False
        flash(t(success_key), "success")
        return result, True

    # ------------------------------------------------------------ form utils
    f = lambda name, default=None: (request.form.get(name) or "").strip() or default  # noqa: E731

    def money_field(name, required=True):
        raw = f(name)
        if raw is None:
            if required:
                raise FinanceError("error.amount_required")
            return 0
        return to_minor(raw)

    def int_field(name):
        raw = f(name)
        return int(raw) if raw not in (None, "") else None

    def check(name):
        return request.form.get(name) in ("1", "on", "true")

    def cats(profile, table):
        return db.rows(g.conn, f"SELECT * FROM {table} WHERE profile_kind=? AND is_active=1 ORDER BY id", (profile,))

    def accts(profile, types=None):
        out = accounts.list_accounts(g.conn, g.user, profile)
        return [a for a in out if types is None or a["account_type"] in types]

    MONEY_TYPES = transfers.MONEY_ACCOUNT_TYPES

    def period_args():
        t0 = date.fromisoformat(today())
        start = request.args.get("start") or dates.month_bounds(t0)[0].isoformat()
        end = request.args.get("end") or today()
        return start, end

    # ----------------------------------------------------------------- auth
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            user = permissions.authenticate(g.conn, f("username", ""), request.form.get("password", ""))
            if user:
                session.clear()
                session["uid"] = user["id"]
                session["lang"] = g.lang
                session["csrf"] = secrets.token_hex(16)
                return redirect(safe_next(request.args.get("next"), url_for("hub")))
            flash(t("auth.invalid"), "error")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        lang = g.lang
        session.clear()
        session["lang"] = lang
        return redirect(url_for("login"))

    @app.route("/lang", methods=["POST"])
    def set_lang():
        lang = request.form.get("lang")
        if lang in i18n.LANGS:
            session["lang"] = lang
        return redirect(safe_next(request.form.get("next"), url_for("hub")))

    @app.route("/")
    @login_required
    def root():
        return redirect(url_for("hub"))

    # ------------------------------------------------------------------ hub
    @app.route("/finance")
    @login_required
    def hub():
        """Shared landing page: shows only the profiles this user may see.
        Personal figures never appear here, even for the owner."""
        kinds = permissions.accessible_kinds(g.conn, g.user)
        g.profile = None
        business = None
        if "business" in kinds:
            business = {"liquid": accounts.liquid_balance(g.conn, "business", today()),
                        "alerts": bills.bill_alerts(g.conn, g.user, "business", today())}
        return render_template("hub.html", business=business)

    @app.route("/finance/<profile>/")
    @profile_route
    def dashboard(profile):
        bs = reports.balance_sheet(g.conn, g.user, profile, today())
        m_start = dates.month_bounds(today())[0].isoformat()
        stmt = reports.income_statement(g.conn, g.user, profile, m_start, today())
        alerts = bills.bill_alerts(g.conn, g.user, profile, today())
        fc = forecast.cash_forecast(g.conn, g.user, profile, today(), 30)
        advice = advisor.list_advice(g.conn, g.user, profile, today())[:5]
        return render_template("dashboard.html", bs=bs, stmt=stmt, alerts=alerts, fc=fc, advice=advice,
                               due=bills.due_recurring(g.conn, g.user, profile, today()))

    # ------------------------------------------------------------- accounts
    @app.route("/finance/<profile>/accounts", methods=["GET", "POST"])
    @profile_route
    def accounts_view(profile):
        if request.method == "POST":
            _, ok = handle(lambda: accounts.create_account(
                g.conn, g.user, profile=profile, account_type=f("account_type"), name_en=f("name_en", ""),
                name_ar=f("name_ar", ""), currency=f("currency", "JOD"),
                opening_balance_minor=to_minor(f("opening_balance", "0")), opening_date=f("opening_date"),
                institution=f("institution"), account_ref=f("account_ref"),
                credit_limit_minor=to_minor(f("credit_limit")) if f("credit_limit") else None,
                statement_day=int_field("statement_day"), payment_due_day=int_field("payment_due_day"),
                interest_rate=f("interest_rate"), notes=f("notes")))
            if ok:
                return redirect(url_for("accounts_view", profile=profile))
        rows_ = accounts.list_accounts(g.conn, g.user, profile, include_inactive=True)
        return render_template("accounts.html", rows=rows_, types=chart.allowed_types(profile),
                               money_accounts=[a for a in rows_ if a["account_type"] in MONEY_TYPES + ("savings", "investment") and a["is_active"]])

    @app.route("/finance/<profile>/accounts/<int:account_id>/toggle", methods=["POST"])
    @profile_route
    def account_toggle(profile, account_id):
        acct = db.account(g.conn, account_id)
        handle(lambda: accounts.update_account(g.conn, g.user, account_id, is_active=0 if acct["is_active"] else 1))
        return redirect(url_for("accounts_view", profile=profile))

    @app.route("/finance/<profile>/internal-transfer", methods=["POST"])
    @profile_route
    def internal_transfer(profile):
        handle(lambda: transfers.internal_transfer(
            g.conn, g.user, profile=profile, from_account_id=int_field("from_account"),
            to_account_id=int_field("to_account"), txn_date=f("date", today()), amount_minor=money_field("amount"),
            description=f("description", t("transfer.internal")), reference=f("reference")))
        return redirect(url_for("accounts_view", profile=profile))

    # ------------------------------------------------------ income/expenses
    @app.route("/finance/<profile>/income", methods=["GET", "POST"])
    @profile_route
    def income(profile):
        if request.method == "POST":
            _, ok = handle(lambda: transactions.record_income(
                g.conn, g.user, profile=profile, account_id=int_field("account_id"),
                category_id=int_field("category_id"), txn_date=f("date", today()),
                amount_minor=money_field("amount"), tax_minor=money_field("tax", False),
                source_name=f("source"), reference=f("reference"), notes=f("notes"),
                is_recurring=check("is_recurring")))
            if ok:
                return redirect(url_for("income", profile=profile))
        start, end = period_args()
        return render_template("txns.html", kind="income", start=start, end=end,
                               rows=transactions.list_transactions(g.conn, g.user, "income_transactions", profile, start, end),
                               categories=[c for c in cats(profile, "income_categories") if not c["system_only"]],
                               all_categories=cats(profile, "income_categories"),
                               pay_accounts=accts(profile, MONEY_TYPES))

    @app.route("/finance/<profile>/expenses", methods=["GET", "POST"])
    @profile_route
    def expenses(profile):
        dup = None
        if request.method == "POST":
            try:
                payee, amount, tax = f("payee"), money_field("amount"), money_field("tax", False)
                dups = transactions.find_duplicate_expenses(g.conn, profile, payee, amount + tax, f("date", today()))
            except (FinanceError, MoneyError, ValueError) as exc:
                flash(error_message(exc), "error")
                amount, dups = None, []
            if amount is not None and dups and not check("confirm_duplicate"):
                dup = dups[0]
                flash(t("expense.possible_duplicate", date=dup["txn_date"]), "warning")
            elif amount is not None:
                _, ok = handle(lambda: transactions.record_expense(
                    g.conn, g.user, profile=profile, account_id=int_field("account_id"),
                    category_id=int_field("category_id"), txn_date=f("date", today()), amount_minor=amount,
                    tax_minor=tax, tax_recoverable=check("tax_recoverable"), payee=payee,
                    subcategory=f("subcategory"), reference=f("reference"), paid=not check("unpaid"),
                    due_date=f("due_date"), business_purpose=f("business_purpose"), notes=f("notes"),
                    is_recurring=check("is_recurring")))
                if ok:
                    return redirect(url_for("expenses", profile=profile))
        start, end = period_args()
        return render_template("txns.html", kind="expense", start=start, end=end, dup=dup,
                               rows=transactions.list_transactions(g.conn, g.user, "expense_transactions", profile, start, end),
                               categories=cats(profile, "expense_categories"),
                               all_categories=cats(profile, "expense_categories"),
                               pay_accounts=accts(profile, MONEY_TYPES))

    @app.route("/finance/<profile>/<kind>/<int:txn_id>/reverse", methods=["POST"])
    @profile_route
    def reverse_txn(profile, kind, txn_id):
        table = {"income": "income_transactions", "expense": "expense_transactions"}.get(kind) or abort(404)
        row = g.conn.execute(f"SELECT profile_id FROM {table} WHERE id=?", (txn_id,)).fetchone()
        if row is None or row["profile_id"] != db.profile_id(g.conn, profile):
            abort(404)
        handle(lambda: transactions.reverse_transaction(g.conn, g.user, table, txn_id, f("date", today()),
                                                        f("reason", "")), "msg.reversed")
        return redirect(url_for("income" if kind == "income" else "expenses", profile=profile))

    @app.route("/finance/<profile>/expenses/<int:txn_id>/pay", methods=["POST"])
    @profile_route
    def pay_expense(profile, txn_id):
        handle(lambda: transactions.pay_expense(g.conn, g.user, txn_id, f("date", today()), int_field("account_id")))
        return redirect(url_for("expenses", profile=profile))

    @app.route("/finance/business/expenses/<int:txn_id>/approve", methods=["POST"])
    @login_required
    def approve_expense(txn_id):
        handle(lambda: transactions.set_approval(g.conn, g.user, txn_id, request.form.get("decision") == "approve"))
        return redirect(url_for("expenses", profile="business"))

    # ------------------------------------------------------------ transfers
    @app.route("/finance/transfers", methods=["GET", "POST"])
    @owner_both
    def owner_transfers():
        if request.method == "POST":
            _, ok = handle(lambda: transfers.owner_transfer(
                g.conn, g.user, transfer_type=f("transfer_type") or "", txn_date=f("date", today()),
                amount_minor=money_field("amount"), description=f("description", ""),
                business_account_id=int_field("business_account_id"),
                personal_account_id=int_field("personal_account_id"), reference=f("reference"),
                interest_minor=money_field("interest", False),
                business_expense_category_id=int_field("business_expense_category_id"),
                business_target_account_id=int_field("business_target_account_id"),
                personal_expense_category_id=int_field("personal_expense_category_id"),
                settle_as=(f("settle_as_bpp") if f("transfer_type") == "business_paid_from_personal"
                           else f("settle_as_ppb") if f("transfer_type") == "personal_paid_from_business" else None),
                allow_exceeding_profit=check("allow_exceeding_profit"),
                payee=f("payee")))
            if ok:
                return redirect(url_for("owner_transfers"))
        biz = transfers.list_transfers(g.conn, g.user, "business")
        return render_template(
            "transfers.html", rows=[r for r in biz if r["transfer_type"] != "internal"],
            types=transfers.OWNER_TRANSFER_TYPES, b_accounts=accts("business", MONEY_TYPES),
            p_accounts=accts("personal", MONEY_TYPES), b_cats=cats("business", "expense_categories"),
            p_cats=cats("personal", "expense_categories"),
            b_targets=[a for a in accts("business") if a["account_type"] in ("supplier_payable", "other_asset", "fixed_asset")],
            balances=debts.owner_business_balances(g.conn, g.user, today()),
            profit_available=transfers.retained_profit_available(g.conn, today()))

    @app.route("/finance/transfers/<int:tid>/reverse", methods=["POST"])
    @owner_both
    def reverse_owner_transfer(tid):
        handle(lambda: transfers.reverse_transfer(g.conn, g.user, tid, f("date", today()), f("reason", "")),
               "msg.reversed")
        return redirect(url_for("owner_transfers"))

    # ---------------------------------------------------------------- bills
    @app.route("/finance/<profile>/bills", methods=["GET", "POST"])
    @profile_route
    def bills_view(profile):
        if request.method == "POST":
            _, ok = handle(lambda: bills.create_bill(
                g.conn, g.user, profile=profile, payee=f("payee", ""), category_id=int_field("category_id"),
                due_date=f("due_date", ""), amount_minor=money_field("amount"), tax_minor=money_field("tax", False),
                description=f("description"), issue_date=f("issue_date"), period_start=f("period_start"),
                period_end=f("period_end"), frequency=f("frequency", "one_time"),
                custom_interval_days=int_field("custom_interval_days"),
                payment_account_id=int_field("payment_account_id"), autopay=check("autopay"),
                reminder_days=f("reminder_days", "7,1,0"), notes=f("notes"),
                allow_duplicate=check("allow_duplicate")))
            if ok:
                return redirect(url_for("bills_view", profile=profile))
        bills.generate_recurring_bills(g.conn, g.user, today())
        month = request.args.get("month") or today()[:7]
        m_start, m_end = dates.month_bounds(month + "-01")
        all_bills = bills.list_bills(g.conn, g.user, profile, today())
        cal = [b for b in all_bills if m_start.isoformat() <= b["due_date"] <= m_end.isoformat()]
        return render_template("bills.html", rows=[b for b in all_bills if b["status"] not in ("paid", "cancelled")],
                               closed=[b for b in all_bills if b["status"] in ("paid", "cancelled")][-20:],
                               calendar=_calendar(m_start, cal), month=month,
                               prev_month=dates.add_months(m_start, -1).isoformat()[:7],
                               next_month=dates.add_months(m_start, 1).isoformat()[:7],
                               alerts=bills.bill_alerts(g.conn, g.user, profile, today()),
                               forecast=bills.monthly_bill_forecast(g.conn, g.user, profile, today()),
                               categories=cats(profile, "expense_categories"), pay_accounts=accts(profile, MONEY_TYPES),
                               frequencies=dates.FREQUENCIES)

    def _calendar(m_start, items):
        first_weekday = (m_start.weekday() + 1) % 7  # weeks start on Sunday (Jordan)
        days = dates.month_bounds(m_start)[1].day
        cells = [None] * first_weekday + list(range(1, days + 1))
        cells += [None] * (-len(cells) % 7)
        by_day = {}
        for b in items:
            by_day.setdefault(int(b["due_date"][8:10]), []).append(b)
        return [[(d, by_day.get(d, [])) if d else None for d in cells[i:i + 7]] for i in range(0, len(cells), 7)]

    @app.route("/finance/<profile>/bills/<int:bill_id>/pay", methods=["POST"])
    @profile_route
    def pay_bill(profile, bill_id):
        handle(lambda: bills.pay_bill(g.conn, g.user, bill_id, amount_minor=money_field("amount"),
                                      pay_date=f("date", today()), account_id=int_field("account_id"),
                                      reference=f("reference")), "msg.payment_recorded")
        return redirect(url_for("bills_view", profile=profile))

    @app.route("/finance/<profile>/bills/<int:bill_id>/cancel", methods=["POST"])
    @profile_route
    def cancel_bill(profile, bill_id):
        handle(lambda: bills.cancel_bill(g.conn, g.user, bill_id, stop_series=check("stop_series")))
        return redirect(url_for("bills_view", profile=profile))

    @app.route("/finance/<profile>/bills/<int:bill_id>")
    @profile_route
    def bill_detail(profile, bill_id):
        bill = next((b for b in bills.list_bills(g.conn, g.user, profile, today()) if b["id"] == bill_id), None) or abort(404)
        return render_template("bill_detail.html", bill=bill, history=bills.payment_history(g.conn, g.user, bill_id),
                               pay_accounts=accts(profile, MONEY_TYPES))

    # ------------------------------------------------------------ recurring
    @app.route("/finance/<profile>/recurring", methods=["GET", "POST"])
    @profile_route
    def recurring_view(profile):
        if request.method == "POST":
            _, ok = handle(lambda: bills.create_recurring(
                g.conn, g.user, profile=profile, kind=f("kind", "expense"), name=f("name", ""),
                amount_minor=money_field("amount"), frequency=f("frequency", "monthly"),
                next_date=f("next_date", today()), account_id=int_field("account_id"),
                income_category_id=int_field("income_category_id"),
                expense_category_id=int_field("expense_category_id"),
                custom_interval_days=int_field("custom_interval_days"), end_date=f("end_date"), notes=f("notes")))
            if ok:
                return redirect(url_for("recurring_view", profile=profile))
        pid = db.profile_id(g.conn, profile)
        return render_template("recurring.html",
                               rows=db.rows(g.conn, "SELECT * FROM recurring_transactions WHERE profile_id=? ORDER BY is_active DESC, next_date", (pid,)),
                               due=bills.due_recurring(g.conn, g.user, profile, today()),
                               inc_cats=[c for c in cats(profile, "income_categories") if not c["system_only"]],
                               exp_cats=cats(profile, "expense_categories"), pay_accounts=accts(profile, MONEY_TYPES),
                               frequencies=dates.FREQUENCIES)

    @app.route("/finance/<profile>/recurring/<int:rec_id>/confirm", methods=["POST"])
    @profile_route
    def confirm_recurring(profile, rec_id):
        handle(lambda: bills.confirm_recurring(g.conn, g.user, rec_id, account_id=int_field("account_id"),
                                               amount_minor=money_field("amount", False) or None,
                                               skip=request.form.get("decision") == "skip"))
        return redirect(safe_next(request.form.get("next"), url_for("recurring_view", profile=profile)))

    # ---------------------------------------------------------------- loans
    @app.route("/finance/<profile>/loans", methods=["GET", "POST"])
    @profile_route
    def loans_view(profile):
        if request.method == "POST":
            lid, ok = handle(lambda: loans.create_loan(
                g.conn, g.user, profile=profile, role=f("role", "borrower"), borrower=f("borrower", ""),
                lender=f("lender", ""), loan_type=f("loan_type", ""), principal_minor=money_field("principal"),
                annual_rate=f("annual_rate", "0"), interest_method=f("interest_method", "reducing_balance"),
                start_date=f("start_date", today()), first_payment_date=f("first_payment_date", ""),
                frequency=f("frequency", "monthly"), term_count=int_field("term_count") or 0,
                cash_account_id=int_field("cash_account_id"), fees_minor=money_field("fees", False),
                collateral=f("collateral"), early_settlement_fee_pct=f("early_fee_pct", "0"),
                early_payment_conditions=f("early_conditions"), notes=f("notes"),
                post_origination=not check("existing_loan")))
            if ok:
                return redirect(url_for("loan_detail", profile=profile, loan_id=lid))
        preview = None
        if request.args.get("preview"):
            try:
                from .. import loan_math
                sched = loan_math.build_schedule(to_minor(request.args["principal"]), request.args.get("rate", "0"),
                                                 int(request.args["n"]), request.args.get("frequency", "monthly"),
                                                 request.args.get("method", "reducing_balance"),
                                                 request.args.get("first") or today())
                preview = {"rows": sched, "totals": loan_math.totals(sched),
                           "affordability": loans.affordability(g.conn, g.user, profile, sched[0].payment_minor,
                                                                request.args.get("frequency", "monthly"), today())}
            except (ValueError, MoneyError, KeyError, FinanceError) as exc:
                flash(error_message(exc), "error")
        return render_template("loans.html", rows=loans.list_loans(g.conn, g.user, profile, today()),
                               loan_types=[x for x in loans.LOAN_TYPES if x not in loans.OWNER_LINKED],
                               methods=loans.INTEREST_METHODS, frequencies=loans.LOAN_FREQUENCIES,
                               pay_accounts=accts(profile, MONEY_TYPES), preview=preview)

    @app.route("/finance/<profile>/loans/<int:loan_id>", methods=["GET", "POST"])
    @profile_route
    def loan_detail(profile, loan_id):
        loan = g.conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if loan is None or loan["profile_id"] != db.profile_id(g.conn, profile):
            abort(404)
        if request.method == "POST":
            action = request.form.get("action")
            if action == "pay":
                if loan["loan_type"] in loans.OWNER_LINKED:
                    bl = loan_id if profile == "business" else loan["counterpart_loan_id"]
                    handle(lambda: loans.repay_owner_business_loan(
                        g.conn, g.user, bl, amount_minor=money_field("amount"), pay_date=f("date", today()),
                        business_account_id=int_field("business_account_id"),
                        personal_account_id=int_field("personal_account_id")), "msg.payment_recorded")
                else:
                    handle(lambda: loans.record_payment(g.conn, g.user, loan_id, amount_minor=money_field("amount"),
                                                        pay_date=f("date", today()),
                                                        cash_account_id=int_field("account_id")), "msg.payment_recorded")
            elif action == "settle":
                handle(lambda: loans.settle_early(g.conn, g.user, loan_id, pay_date=f("date", today()),
                                                  cash_account_id=int_field("account_id")), "msg.payment_recorded")
            elif action == "status":
                handle(lambda: loans.set_status(g.conn, g.user, loan_id, f("status", "")))
            return redirect(url_for("loan_detail", profile=profile, loan_id=loan_id))
        whatif = None
        if request.args.get("extra"):
            try:
                whatif = loans.what_if_extra(g.conn, g.user, loan_id, to_minor(request.args["extra"]))
            except (FinanceError, MoneyError, ValueError) as exc:
                flash(error_message(exc), "error")
        stmt = loans.loan_statement(g.conn, g.user, loan_id, today())
        quote = loans.settlement_quote(g.conn, g.user, loan_id, today()) if stmt["loan"]["outstanding_principal_minor"] else None
        return render_template("loan_detail.html", s=stmt, whatif=whatif, quote=quote,
                               pay_accounts=accts(profile, MONEY_TYPES),
                               b_accounts=accts("business", MONEY_TYPES) if loan["loan_type"] in loans.OWNER_LINKED else [],
                               p_accounts=accts("personal", MONEY_TYPES) if loan["loan_type"] in loans.OWNER_LINKED else [])

    @app.route("/finance/owner-loans", methods=["POST"])
    @owner_both
    def owner_loan():
        res, ok = handle(lambda: loans.create_owner_business_loan(
            g.conn, g.user, direction=f("direction", ""), principal_minor=money_field("principal"),
            annual_rate=f("annual_rate", "0"), interest_method=f("interest_method", "none"),
            start_date=f("start_date", today()), first_payment_date=f("first_payment_date", ""),
            frequency=f("frequency", "monthly"), term_count=int_field("term_count") or 0,
            business_account_id=int_field("business_account_id"), personal_account_id=int_field("personal_account_id"),
            owner_name=g.user["display_name"], shop_name=t("app.shop"), notes=f("notes")))
        return redirect(url_for("loan_detail", profile="business", loan_id=res[0]) if ok else url_for("owner_transfers"))

    # ---------------------------------------------------------------- debts
    @app.route("/finance/<profile>/debts", methods=["GET", "POST"])
    @profile_route
    def debts_view(profile):
        types = [k for k, v in debts.DEBT_TYPES.items() if v[0] == profile]
        if request.method == "POST":
            _, ok = handle(lambda: debts.create_debt(
                g.conn, g.user, debt_type=f("debt_type", ""), debtor=f("debtor", ""), creditor=f("creditor", ""),
                original_minor=money_field("amount"), issue_date=f("issue_date", today()), origin=f("origin", ""),
                due_date=f("due_date"), cash_account_id=int_field("cash_account_id"),
                income_category_id=int_field("income_category_id"), expense_category_id=int_field("expense_category_id"),
                installment_plan=debts.parse_plan(f("plan", "")) or None, annual_rate=f("annual_rate", "0"),
                reference=f("reference"), notes=f("notes")))
            if ok:
                return redirect(url_for("debts_view", profile=profile))
        as_of = request.args.get("as_of") or today()
        return render_template("debts.html", rows=debts.list_debts(g.conn, g.user, profile), types=types,
                               origins=debts.ORIGINS, as_of=as_of,
                               recv=debts.aging_report(g.conn, g.user, profile, "receivable", as_of),
                               pay=debts.aging_report(g.conn, g.user, profile, "payable", as_of),
                               owner_bal=debts.owner_business_balances(g.conn, g.user, as_of) if profile == "business" else None,
                               pay_accounts=accts(profile, MONEY_TYPES),
                               inc_cats=[c for c in cats(profile, "income_categories") if not c["system_only"]],
                               exp_cats=cats(profile, "expense_categories"), buckets=debts.AGING_BUCKETS)

    @app.route("/finance/<profile>/debts/<int:debt_id>/pay", methods=["POST"])
    @profile_route
    def pay_debt(profile, debt_id):
        if request.form.get("action") == "write_off":
            handle(lambda: debts.write_off(g.conn, g.user, debt_id, f("date", today()), f("reason", "")))
        else:
            handle(lambda: debts.record_payment(g.conn, g.user, debt_id, amount_minor=money_field("amount"),
                                                pay_date=f("date", today()), cash_account_id=int_field("account_id"),
                                                interest_minor=money_field("interest", False)), "msg.payment_recorded")
        return redirect(url_for("debts_view", profile=profile))

    # ------------------------------------------------------ assets/liabs
    @app.route("/finance/<profile>/assets", methods=["GET", "POST"])
    @profile_route
    def assets_view(profile):
        if request.method == "POST":
            action = request.form.get("action", "create")
            if action == "create":
                handle(lambda: assets.create_asset(
                    g.conn, g.user, profile=profile, asset_class=f("asset_class", ""), name_en=f("name_en", ""),
                    name_ar=f("name_ar", ""), purchase_value_minor=money_field("purchase_value"),
                    purchase_date=f("purchase_date", today()), funding=f("funding", "cash"),
                    cash_account_id=int_field("cash_account_id"),
                    current_value_minor=money_field("current_value", False) or None,
                    useful_life_months=int_field("useful_life_months"),
                    salvage_value_minor=money_field("salvage", False),
                    depreciation_method=f("depreciation_method", "none"), notes=f("notes")))
            elif action == "depreciate":
                # Depreciate through the end of the last completed month.
                last_month_end = (dates.month_bounds(today())[0] - timedelta(days=1)).isoformat()
                handle(lambda: assets.depreciate_all(g.conn, g.user, last_month_end))
            elif action == "revalue":
                handle(lambda: assets.revalue(g.conn, g.user, int_field("asset_id"), money_field("value"), f("date", today())))
            elif action == "dispose":
                handle(lambda: assets.dispose(g.conn, g.user, int_field("asset_id"), on=f("date", today()),
                                              proceeds_minor=money_field("proceeds", False),
                                              cash_account_id=int_field("account_id")))
            elif action == "liability":
                handle(lambda: assets.create_liability(
                    g.conn, g.user, profile=profile, liability_class=f("liability_class", ""),
                    name_en=f("name_en", ""), name_ar=f("name_ar", ""), account_id=int_field("account_id"),
                    due_date=f("due_date"), notes=f("notes")))
            return redirect(url_for("assets_view", profile=profile))
        liab_accounts = [a for a in accts(profile) if chart.account_class(a["account_type"]) == "liability"]
        return render_template("assets.html", rows=assets.list_assets(g.conn, g.user, profile),
                               liabilities=assets.list_liabilities(g.conn, g.user, profile, today()),
                               classes=assets.ASSET_CLASSES[profile], liability_classes=assets.LIABILITY_CLASSES,
                               pay_accounts=accts(profile, MONEY_TYPES), liab_accounts=liab_accounts,
                               bs=reports.balance_sheet(g.conn, g.user, profile, today()))

    # -------------------------------------------------------------- budgets
    @app.route("/finance/<profile>/budgets", methods=["GET", "POST"])
    @profile_route
    def budgets_view(profile):
        if request.method == "POST":
            bid, ok = handle(lambda: planning.create_budget(
                g.conn, g.user, profile=profile, name=f("name", ""), period_type=f("period_type", "monthly"),
                start_date=f("start_date", today()), lines=_budget_lines(), rollover=check("rollover")))
            if ok:
                return redirect(url_for("budget_detail", profile=profile, budget_id=bid))
        return render_template("budgets.html", rows=planning.list_budgets(g.conn, g.user, profile),
                               inc_cats=cats(profile, "income_categories"), exp_cats=cats(profile, "expense_categories"),
                               line_types=planning.LINE_TYPES)

    def _budget_lines():
        """Budget lines from the form's parallel line_* fields."""
        lines = []
        for i in range(len(request.form.getlist("line_type"))):
            amt = request.form.getlist("line_amount")[i].strip()
            if not amt:
                continue
            cat = request.form.getlist("line_category")[i]
            lt = request.form.getlist("line_type")[i]
            lines.append({"line_type": lt, "amount_minor": to_minor(amt),
                          "income_category_id": int(cat) if lt == "income" and cat else None,
                          "expense_category_id": int(cat) if lt == "expense" and cat else None,
                          "area": request.form.getlist("line_area")[i].strip() or None})
        return lines

    @app.route("/finance/<profile>/budgets/<int:budget_id>", methods=["GET", "POST"])
    @profile_route
    def budget_detail(profile, budget_id):
        b = g.conn.execute("SELECT * FROM budgets WHERE id=?", (budget_id,)).fetchone()
        if b is None or b["profile_id"] != db.profile_id(g.conn, profile):
            abort(404)
        if request.method == "POST":
            nid, ok = handle(lambda: planning.copy_budget(g.conn, g.user, budget_id, f("new_start", "")))
            if ok:
                return redirect(url_for("budget_detail", profile=profile, budget_id=nid))
        return render_template("budget_detail.html", r=planning.budget_vs_actual(g.conn, g.user, budget_id, today()))

    # ---------------------------------------------------------------- goals
    @app.route("/finance/<profile>/goals", methods=["GET", "POST"])
    @profile_route
    def goals_view(profile):
        if request.method == "POST":
            if request.form.get("action") == "contribute":
                handle(lambda: planning.add_contribution(g.conn, g.user, int_field("goal_id"),
                                                         amount_minor=money_field("amount"), on=f("date", today()),
                                                         notes=f("notes")))
            else:
                handle(lambda: planning.create_goal(
                    g.conn, g.user, profile=profile, goal_type=f("goal_type", ""), name_en=f("name_en", ""),
                    name_ar=f("name_ar", ""), target_minor=money_field("target"), target_date=f("target_date", ""),
                    start_date=f("start_date", today()), starting_minor=money_field("starting", False),
                    contribution_frequency=f("frequency", "monthly"), linked_account_id=int_field("linked_account_id"),
                    priority=int_field("priority") or 3, notes=f("notes")))
            return redirect(url_for("goals_view", profile=profile))
        return render_template("goals.html", rows=planning.list_goals(g.conn, g.user, profile, today()),
                               goal_types=planning.GOAL_TYPES[profile], link_accounts=accts(profile))

    # ------------------------------------------------------------- forecast
    @app.route("/finance/<profile>/forecast")
    @profile_route
    def forecast_view(profile):
        h = int(request.args.get("h", 30))
        fc = forecast.cash_forecast(g.conn, g.user, profile, today(), h, include_baseline=request.args.get("baseline", "1") == "1")
        return render_template("forecast.html", fc=fc, weeks=forecast.weekly_rollup(fc["series"]),
                               horizons=forecast.HORIZONS, svg=_line_svg([s["balance_minor"] for s in fc["series"]]))

    @app.route("/finance/forecast/combined")
    @owner_both
    def forecast_combined():
        h = int(request.args.get("h", 30))
        cf = forecast.combined_forecast(g.conn, g.user, today(), h)
        return render_template("forecast_combined.html", cf=cf, horizons=forecast.HORIZONS,
                               svg=_line_svg([s["total_minor"] for s in cf["series"]],
                                             [s["business_minor"] for s in cf["series"]],
                                             [s["personal_minor"] for s in cf["series"]]))

    def _line_svg(*series, width=640, height=180):
        """Tiny dependency-free line chart.  In Arabic the time axis runs
        right-to-left, like the rest of the RTL layout."""
        pts_all = [v for s in series for v in s]
        if not pts_all:
            return ""
        lo, hi = min(min(pts_all), 0), max(max(pts_all), 1)
        span = (hi - lo) or 1
        n = max(len(series[0]) - 1, 1)
        rtl = g.lang == "ar"

        def xy(i, v):
            x = (i / n) * (width - 20) + 10
            if rtl:
                x = width - x
            return x, height - 10 - (v - lo) / span * (height - 20)

        zero_y = xy(0, 0)[1]
        colors = ("var(--accent)", "var(--biz)", "var(--per)")
        paths = []
        for k, s in enumerate(series):
            d = " ".join(f"{'M' if i == 0 else 'L'}{xy(i, v)[0]:.1f},{xy(i, v)[1]:.1f}" for i, v in enumerate(s))
            paths.append(f'<path d="{d}" fill="none" stroke="{colors[k % 3]}" stroke-width="{2.5 if k == 0 else 1.5}"/>')
        return (f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{t("forecast.chart")}">'
                f'<line x1="0" x2="{width}" y1="{zero_y:.1f}" y2="{zero_y:.1f}" class="zero"/>{"".join(paths)}</svg>')

    # -------------------------------------------------------------- reports
    REPORTS = {
        "business": ("profit_loss", "balance_sheet", "cash_flow", "trial_balance", "sales_gross_profit",
                     "expenses_by_category", "inventory_valuation", "receivables", "payables", "loan_balances",
                     "tax_summary", "owner_equity", "net_worth", "register_reconciliation"),
        "personal": ("income_expense", "cash_flow", "net_worth", "expense_analysis", "bills_report",
                     "debt_report", "loan_amortization", "savings", "goal_progress", "budget_vs_actual",
                     "monthly_summary", "trial_balance"),
    }

    @app.route("/finance/<profile>/reports")
    @profile_route
    def reports_index(profile):
        return render_template("reports_index.html", names=REPORTS[profile])

    def build_report(profile, name, start, end):
        u = g.user
        if name in ("profit_loss", "income_expense", "sales_gross_profit"):
            return reports.income_statement(g.conn, u, profile, start, end)
        if name in ("balance_sheet", "net_worth"):
            return reports.balance_sheet(g.conn, u, profile, end)
        if name == "cash_flow":
            return reports.cash_flow_statement(g.conn, u, profile, start, end)
        if name == "trial_balance":
            return reports.trial_balance(g.conn, u, profile, end)
        if name in ("expenses_by_category", "expense_analysis"):
            return reports.expense_analysis(g.conn, u, profile, end)
        if name == "inventory_valuation":
            return reports.inventory_valuation(g.conn, u, end)
        if name in ("receivables", "payables"):
            return debts.aging_report(g.conn, u, profile, "receivable" if name == "receivables" else "payable", end)
        if name in ("loan_balances", "loan_amortization"):
            return {"loans": loans.list_loans(g.conn, u, profile, end)}
        if name == "tax_summary":
            return reports.tax_summary(g.conn, u, start, end)
        if name == "owner_equity":
            return reports.owner_equity_report(g.conn, u, start, end)
        if name == "register_reconciliation":
            return {"rows": [r for r in reports.reconciliation_report(g.conn, u, profile)]}
        if name == "bills_report":
            return {"rows": bills.list_bills(g.conn, u, profile, today(), start, end)}
        if name == "debt_report":
            return {"recv": debts.aging_report(g.conn, u, profile, "receivable", end),
                    "pay": debts.aging_report(g.conn, u, profile, "payable", end)}
        if name == "savings":
            return reports.savings_report(g.conn, u, start, end)
        if name == "goal_progress":
            return {"goals": planning.list_goals(g.conn, u, profile, end)}
        if name == "budget_vs_actual":
            return {"budgets": [planning.budget_vs_actual(g.conn, u, b["id"], end)
                                for b in planning.list_budgets(g.conn, u, profile)
                                if b["start_date"] <= end and b["end_date"] >= start]}
        if name == "monthly_summary":
            return reports.monthly_summary(g.conn, u, profile, end)
        abort(404)

    @app.route("/finance/<profile>/reports/<name>")
    @profile_route
    def report_view(profile, name):
        if name not in REPORTS[profile]:
            abort(404)
        start, end = period_args()
        return render_template("report.html", name=name, r=build_report(profile, name, start, end), start=start, end=end)

    @app.route("/finance/<profile>/reports/<name>/export", methods=["POST"])
    @profile_route
    def report_export(profile, name):
        if name not in REPORTS[profile]:
            abort(404)
        start, end = f("start"), f("end")
        r = build_report(profile, name, start, end)
        header, data = _flatten(name, r)
        payload = backup.table_to_csv(header, data)
        fname = f"namareq-{profile}-{name}-{end}"
        if profile == "personal":
            try:
                blob = backup.protected_personal_export(g.conn, g.user, request.form.get("owner_password", ""),
                                                        request.form.get("passphrase", ""), name, payload)
            except FinanceError as exc:
                flash(error_message(exc), "error")
                return redirect(url_for("report_view", profile=profile, name=name, start=start, end=end))
            return send_file(io.BytesIO(blob), as_attachment=True, download_name=fname + ".csv.nmrq",
                             mimetype="application/octet-stream")
        return send_file(io.BytesIO(payload), as_attachment=True, download_name=fname + ".csv", mimetype="text/csv")

    def _flatten(name, r):
        """Turn a report into rows for CSV in the user's language."""
        money = lambda m: fmt(m, lang=g.lang, with_symbol=False)  # noqa: E731
        if "income" in r and "expenses" in r:
            rows_ = [[t("report.section.income"), nm(x), money(x["amount_minor"])] for x in r["income"]]
            rows_ += [[t("report.section.cost_of_sales"), nm(x), money(x["amount_minor"])] for x in r["cost_of_sales"]]
            rows_ += [[t("report.section.expenses"), nm(x), money(x["amount_minor"])] for x in r["expenses"]]
            rows_.append([t("report.net"), "", money(r["net_minor"])])
            return [t("col.section"), t("col.item"), t("col.amount")], rows_
        if "assets" in r:
            rows_ = [[t(f"report.section.{sec}"), nm(x), money(x["amount_minor"])]
                     for sec in ("assets", "liabilities", "equity") for x in r[sec]]
            rows_.append([t("report.net_worth"), "", money(r["net_worth_minor"])])
            return [t("col.section"), t("col.item"), t("col.amount")], rows_
        if "lines" in r and name == "trial_balance":
            return ([t("col.item"), t("col.debit"), t("col.credit")],
                    [[nm(x), money(x["debit"]), money(x["credit"])] for x in r["lines"]])
        if "lines" in r:
            return ([t("col.category"), t("col.amount"), t("col.avg"), t("col.change")],
                    [[nm(x), money(x["amount_minor"]), money(x["avg_minor"]), x["change_pct"]] for x in r["lines"]])
        if "parties" in r:
            return ([t("col.party")] + [t(f"aging.{b}") for b in debts.AGING_BUCKETS] + [t("col.total")],
                    [[p["party"]] + [money(p[b]) for b in debts.AGING_BUCKETS] + [money(p["total"])] for p in r["parties"]])
        flat = [[k, json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v]
                for k, v in r.items()]
        return [t("col.item"), t("col.value")], flat

    # -------------------------------------------------------- reconciliation
    @app.route("/finance/<profile>/reconciliation", methods=["GET", "POST"])
    @profile_route
    def reconciliation(profile):
        result = None
        if request.method == "POST":
            result, _ = handle(lambda: accounts.reconcile(
                g.conn, g.user, int_field("account_id"), f("statement_date", today()), money_field("statement_balance"),
                kind=f("kind", "bank_statement"), post_adjustment=check("post_adjustment"), notes=f("notes")))
        return render_template("reconciliation.html", result=result,
                               rows=reports.reconciliation_report(g.conn, g.user, profile),
                               rec_accounts=accts(profile, MONEY_TYPES + ("savings",)),
                               periods=db.rows(g.conn, "SELECT * FROM financial_periods WHERE profile_id=? ORDER BY start_date DESC",
                                               (db.profile_id(g.conn, profile),)))

    @app.route("/finance/<profile>/close-period", methods=["POST"])
    @profile_route
    def close_period(profile):
        if g.user["role"] not in ("owner", "accountant"):
            abort(403)
        handle(lambda: ledger.close_period(g.conn, g.user, db.profile_id(g.conn, profile), f("start", ""), f("end", "")))
        return redirect(url_for("reconciliation", profile=profile))

    # -------------------------------------------------------------- advisor
    @app.route("/finance/advisor/<scope>", methods=["GET", "POST"])
    @login_required
    def advisor_view(scope):
        if scope not in PROFILES:
            abort(404)
        if not permissions.can_access(g.conn, g.user, scope):
            if scope == "personal":
                permissions.log_personal_access(g.conn, g.user, "advisor", "read", False)
            return render_template("denied.html", scope=scope), 403
        g.profile = scope
        if request.method == "POST":
            if request.form.get("action") == "refresh":
                handle(lambda: advisor.generate(g.conn, g.user, today()), "advisor.refreshed")
            else:
                handle(lambda: advisor.act(g.conn, g.user, int_field("advice_id"), f("decision", ""), f("note"),
                                           f("postpone_until")))
            return redirect(url_for("advisor_view", scope=scope))
        m = metrics.business_metrics(g.conn, g.user, today()) if scope == "business" else metrics.personal_metrics(g.conn, g.user, today())
        return render_template("advisor.html", scope=scope, items=advisor.list_advice(g.conn, g.user, scope, today()),
                               history=[a for a in advisor.list_advice(g.conn, g.user, scope, today(), include_closed=True)
                                        if a["status"] not in ("open",)][:30], m=m)

    # ------------------------------------------------------------- overview
    @app.route("/finance/overview")
    @owner_both
    def overview():
        return render_template("overview.html", o=reports.combined_overview(g.conn, g.user, today()))

    # ------------------------------------------------------- access/backup
    @app.route("/finance/settings/access", methods=["GET", "POST"])
    @login_required
    def access_settings():
        if g.user["role"] != "owner":
            abort(403)
        g.profile = None
        if request.method == "POST":
            if request.form.get("action") == "revoke":
                handle(lambda: permissions.revoke_access(g.conn, g.user, int_field("grant_id")))
            elif request.form.get("action") == "user":
                handle(lambda: permissions.create_user(g.conn, f("username", ""), f("display_name", ""),
                                                       request.form.get("password", ""), f("role", ""), f("lang", "ar")))
            else:
                if not check("confirm"):
                    flash(t("access.confirm_required"), "error")
                else:
                    handle(lambda: permissions.grant_access(g.conn, g.user, int_field("user_id"), f("profile_kind", ""),
                                                            request.form.get("owner_password", ""), f("note")))
            return redirect(url_for("access_settings"))
        return render_template("access.html",
                               users=db.rows(g.conn, "SELECT id, username, display_name, role, is_active FROM users ORDER BY id"),
                               grants=db.rows(g.conn, """SELECT gr.*, u.display_name FROM finance_access_grants gr
                                                         JOIN users u ON u.id=gr.user_id ORDER BY gr.id DESC"""),
                               log=db.rows(g.conn, """SELECT l.*, u.display_name FROM personal_access_log l
                                                      LEFT JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 50"""),
                               roles=permissions.ROLES)

    @app.route("/finance/backup", methods=["GET", "POST"])
    @login_required
    def backup_view():
        if g.user["role"] not in ("owner", "accountant"):
            abort(403)
        g.profile = None
        if request.method == "POST":
            tmpdir = tempfile.mkdtemp(prefix="namareq-")
            stamp = today()
            if request.form.get("scope") == "personal":
                path = os.path.join(tmpdir, f"namareq-personal-{stamp}.nmrq")
                try:
                    backup.personal_backup(g.conn, g.user, path, request.form.get("owner_password", ""),
                                           request.form.get("passphrase", ""))
                except FinanceError as exc:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    flash(error_message(exc), "error")
                    return redirect(url_for("backup_view"))
            else:
                path = os.path.join(tmpdir, f"namareq-business-{stamp}.sqlite")
                backup.business_backup(g.conn, g.user, path)
            # Serve from memory and remove the temporary copy straight away.
            with open(path, "rb") as fh:
                payload = io.BytesIO(fh.read())
            shutil.rmtree(tmpdir, ignore_errors=True)
            return send_file(payload, as_attachment=True, download_name=os.path.basename(path),
                             mimetype="application/octet-stream")
        return render_template("backup.html")

    @app.errorhandler(403)
    def _forbidden(_e):
        return render_template("denied.html", scope=None), 403

    return app
