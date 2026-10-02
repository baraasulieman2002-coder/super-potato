"""Bilingual interface: catalogue completeness, RTL/LTR rendering of every
page, language switching and access control through the web layer."""
import os
import re
import shutil
import string
import tempfile
import unittest
from pathlib import Path

from namareq import db, demo, i18n
from namareq.i18n.catalog import CATALOG
from namareq.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent / "namareq"
ARABIC = re.compile(r"[؀-ۿ]")
TODAY = "2026-10-02"


def placeholders(text):
    return {f[1] for f in string.Formatter().parse(text) if f[1]}


class CatalogueTests(unittest.TestCase):
    def test_both_languages_complete_and_consistent(self):
        self.assertEqual(set(i18n.EN), set(i18n.AR))
        for key, (en, ar) in CATALOG.items():
            self.assertTrue(en.strip() and ar.strip(), key)
            self.assertEqual(placeholders(en), placeholders(ar), key)
            if key != "lang.switch":
                self.assertRegex(ar, ARABIC, f"{key} has no Arabic text")

    def test_every_literal_key_in_code_exists(self):
        used = set()
        for path in ROOT.rglob("*"):
            if path.suffix in (".py", ".html"):
                text = path.read_text(encoding="utf-8")
                used |= set(re.findall(r"""\bt\(\s*['"]([a-z_]+\.[a-z0-9_.]+[a-z0-9_])['"]\s*[,)]""", text))
                used |= set(re.findall(r"""FinanceError\(\s*['"]([a-z_.0-9]+)['"]""", text))
                used |= set(re.findall(r"""['"]((?:num|assume|benefit|risk)\.[a-z0-9_]+)['"]""", text))
        missing = sorted(k for k in used if k not in i18n.EN)
        self.assertEqual(missing, [])

    def test_all_advice_rules_have_messages(self):
        from namareq import advisor
        for rule in advisor.RULES:
            code = rule.__name__[len("rule_"):]
            code = {"duplicate_expenses": "duplicate_expense"}.get(code, code)
            self.assertIn(f"advice.{code}.title", i18n.EN)
            self.assertIn(f"advice.{code}.why", i18n.EN)


def _pages():
    pages = ["/finance", "/finance/overview", "/finance/transfers", "/finance/forecast/combined",
             "/finance/settings/access", "/finance/backup", "/finance/business/loans/1?extra=50",
             "/finance/business/bills/1", "/finance/personal/budgets/1"]
    for p in ("business", "personal"):
        pages += [f"/finance/{p}/{s}" for s in ("", "accounts", "income", "expenses", "bills", "recurring", "loans",
                                               "debts", "assets", "budgets", "goals", "forecast?h=365",
                                               "reports", "reconciliation")]
        pages.append(f"/finance/advisor/{p}")
    reports = {"business": ("profit_loss", "balance_sheet", "cash_flow", "trial_balance", "sales_gross_profit",
                            "expenses_by_category", "inventory_valuation", "receivables", "payables", "loan_balances",
                            "tax_summary", "owner_equity", "net_worth", "register_reconciliation"),
               "personal": ("income_expense", "cash_flow", "net_worth", "expense_analysis", "bills_report",
                            "debt_report", "loan_amortization", "savings", "goal_progress", "budget_vs_actual",
                            "monthly_summary", "trial_balance")}
    for p, names in reports.items():
        pages += [f"/finance/{p}/reports/{n}?start=2026-06-01&end={TODAY}" for n in names]
    return pages


PAGES = _pages()


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, "demo.sqlite")
        conn = db.connect(cls.path)
        demo.build(conn, TODAY)
        conn.close()
        cls.app = create_app(cls.path, today=TODAY)
        cls.app.testing = True

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def client(self, username="owner", password="owner-pass-123"):
        c = self.app.test_client()
        c.get("/login")
        r = c.post("/login", data={"username": username, "password": password, "_csrf": self.csrf(c)})
        self.assertEqual(r.status_code, 302)
        return c

    @staticmethod
    def csrf(c):
        with c.session_transaction() as s:
            return s.get("csrf") or s.setdefault("csrf", "x")

    def set_lang(self, c, lang):
        with c.session_transaction() as s:
            s["lang"] = lang

    def test_every_page_renders_in_both_languages(self):
        c = self.client()
        c.post("/finance/advisor/business", data={"_csrf": self.csrf(c), "action": "refresh"})
        i18n.MISSING.clear()
        for lang, direction in (("ar", "rtl"), ("en", "ltr")):
            self.set_lang(c, lang)
            for url in PAGES:
                r = c.get(url)
                self.assertEqual(r.status_code, 200, f"{lang} {url}")
                body = r.get_data(as_text=True)
                self.assertIn(f'lang="{lang}" dir="{direction}"', body, url)
                if lang == "ar" and url != "/finance":
                    self.assertRegex(body, ARABIC)
        self.assertEqual(i18n.MISSING, set(), "translation keys requested but not defined")

    def test_profile_banner_always_identifies_the_books(self):
        c = self.client()
        self.set_lang(c, "en")
        self.assertIn("Shop finances", c.get("/finance/business/expenses").get_data(as_text=True))
        body = c.get("/finance/personal/expenses").get_data(as_text=True)
        self.assertIn("Owner personal finances", body)
        self.assertIn("Private — owner only", body)
        self.assertIn("Combined owner overview", c.get("/finance/overview").get_data(as_text=True))

    def test_language_switch_returns_to_same_page(self):
        c = self.client()
        self.set_lang(c, "ar")
        r = c.post("/lang", data={"_csrf": self.csrf(c), "lang": "en", "next": "/finance/business/bills?month=2026-11"})
        self.assertEqual(r.headers["Location"], "/finance/business/bills?month=2026-11")
        self.assertIn('dir="ltr"', c.get(r.headers["Location"]).get_data(as_text=True))
        for evil in ("//evil.example", "/\\evil.example", "https://evil.example"):
            r = c.post("/lang", data={"_csrf": self.csrf(c), "lang": "ar", "next": evil})
            self.assertEqual(r.headers["Location"], "/finance")

    def test_cashier_cannot_reach_personal_or_owner_pages(self):
        c = self.client("cashier", "cashier-pass-12")
        for url in ("/finance/personal/", "/finance/personal/reports/net_worth", "/finance/advisor/personal",
                    "/finance/overview", "/finance/transfers", "/finance/business/", "/finance/settings/access"):
            self.assertEqual(c.get(url).status_code, 403, url)
        hub = c.get("/finance").get_data(as_text=True)
        self.assertNotIn("/finance/personal/", hub)

    def test_accountant_sees_business_but_not_personal(self):
        c = self.client("accountant", "accountant-pass-1")
        self.assertEqual(c.get("/finance/business/reports/profit_loss").status_code, 200)
        self.assertEqual(c.get("/finance/personal/").status_code, 403)
        self.assertEqual(c.get("/finance/overview").status_code, 403)

    def test_shared_hub_shows_no_personal_figures(self):
        c = self.client()
        self.set_lang(c, "en")
        body = c.get("/finance").get_data(as_text=True)
        self.assertNotIn("Housing Bank", body)
        self.assertNotIn("Wallet", body)

    def test_csrf_required_for_posts(self):
        c = self.client()
        self.assertEqual(c.post("/finance/business/expenses", data={"amount": "1"}).status_code, 400)

    def test_posting_expense_through_form_and_error_in_arabic(self):
        c = self.client()
        self.set_lang(c, "ar")
        r = c.post("/finance/business/expenses", data={
            "_csrf": self.csrf(c), "date": TODAY, "category_id": "2", "amount": "abc", "account_id": "1"})
        body = r.get_data(as_text=True)
        self.assertIn("أدخل مبلغاً صحيحاً", body)
        self.assertIn('value="abc"', body, "form input is kept after a validation error")

    def test_personal_export_is_encrypted(self):
        c = self.client()
        url = "/finance/personal/reports/net_worth/export"
        r = c.post(url, data={"_csrf": self.csrf(c), "start": "2026-09-01", "end": TODAY,
                              "owner_password": "owner-pass-123", "passphrase": "a long passphrase"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"NMRQP1"))
        r = c.post("/finance/business/reports/profit_loss/export",
                   data={"_csrf": self.csrf(c), "start": "2026-09-01", "end": TODAY})
        self.assertTrue(r.data.startswith("﻿".encode()))


if __name__ == "__main__":
    unittest.main()


class FormPostTests(unittest.TestCase):
    """Submit every major form through the web layer on a fresh demo copy."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        path = os.path.join(self.tmp, "demo.sqlite")
        conn = db.connect(path)
        self.ctx = demo.build(conn, TODAY)
        conn.close()
        self.app = create_app(path, today=TODAY)
        self.app.testing = True
        self.c = self.app.test_client()
        self.c.get("/login")
        self.c.post("/login", data={"username": "owner", "password": "owner-pass-123", "_csrf": self.csrf()})
        with self.c.session_transaction() as s:
            s["lang"] = "en"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def csrf(self):
        with self.c.session_transaction() as s:
            return s.setdefault("csrf", "token")

    def post(self, url, **data):
        data["_csrf"] = self.csrf()
        r = self.c.post(url, data=data, follow_redirects=True)
        self.assertEqual(r.status_code, 200, url)
        body = r.get_data(as_text=True)
        self.assertNotIn('class="flash error"', body, f"{url}: {re.findall(r'flash error[^<]*<', body)}")
        return body

    def test_forms(self):
        bank, pbank, savings = self.ctx["bank"], self.ctx["pbank"], self.ctx["savings"]
        cat = lambda kind, code: str((db.expense_category if kind[0] == "e" else db.income_category)(  # noqa: E731
            self.conn(), kind[1:], code)["id"])
        self.post("/finance/business/accounts", account_type="savings", name_en="Reserve", name_ar="احتياطي",
                  currency="JOD", opening_balance="100", opening_date=TODAY)
        self.post("/finance/personal/internal-transfer", from_account=str(pbank), to_account=str(savings),
                  amount="10", date=TODAY, description="save")
        self.post("/finance/business/income", date=TODAY, category_id=cat("ibusiness", "repair_services"),
                  amount="25", tax="4", account_id=str(bank))
        self.post("/finance/personal/expenses", date=TODAY, category_id=cat("epersonal", "fuel"), amount="30",
                  account_id=str(pbank), payee="Total station")
        for ttype, extra in (("capital_contribution", {}), ("owner_withdrawal", {}),
                             ("personal_paid_from_business", {"personal_expense_category_id": cat("epersonal", "utilities"),
                                                              "settle_as_ppb": "withdrawal"}),
                             ("business_paid_from_personal", {"business_expense_category_id": cat("ebusiness", "equipment"),
                                                              "settle_as_bpp": "due_to_owner"})):
            self.post("/finance/transfers", transfer_type=ttype, date=TODAY, amount="20", description=ttype,
                      business_account_id=str(bank), personal_account_id=str(pbank), **extra)
        self.post("/finance/owner-loans", direction="owner_to_business", principal="600", annual_rate="0",
                  interest_method="none", frequency="monthly", term_count="3", start_date=TODAY,
                  first_payment_date="2026-11-02", business_account_id=str(bank), personal_account_id=str(pbank))
        self.post("/finance/business/bills", payee="Cleaner", category_id=cat("ebusiness", "misc_business"),
                  due_date="2026-10-20", amount="40", frequency="monthly", reminder_days="3")
        bill = self.conn().execute("SELECT id FROM bills WHERE payee='Cleaner'").fetchone()[0]
        self.post(f"/finance/business/bills/{bill}/pay", amount="15", date=TODAY, account_id=str(bank))
        self.post("/finance/personal/loans", role="borrower", loan_type="personal_loan", borrower="Me", lender="Bank",
                  principal="1000", annual_rate="8", interest_method="reducing_balance", frequency="monthly",
                  term_count="10", start_date=TODAY, first_payment_date="2026-11-01", cash_account_id=str(pbank))
        loan = self.conn().execute("SELECT id FROM loans WHERE lender='Bank'").fetchone()[0]
        self.post(f"/finance/personal/loans/{loan}", action="pay", amount="150", date="2026-11-01", account_id=str(pbank))
        self.post("/finance/business/debts", debt_type="customer", debtor="New client", creditor="Shop", amount="90",
                  issue_date=TODAY, origin="credit_sale", income_category_id=cat("ibusiness", "installation"),
                  plan="2026-11-01 45\n2026-12-01 45")
        self.post("/finance/business/assets", action="create", asset_class="furniture", name_en="Counter",
                  name_ar="كاونتر", purchase_value="500", purchase_date=TODAY, funding="cash",
                  cash_account_id=str(bank), depreciation_method="straight_line", useful_life_months="60")
        self.post("/finance/personal/budgets", name="Next month", period_type="monthly", start_date="2026-11-01",
                  line_type=["expense", "savings"], line_category=[cat("epersonal", "fuel"), ""],
                  line_area=["", ""], line_amount=["80", "100"])
        self.post("/finance/personal/goals", goal_type="travel", name_en="Trip", name_ar="رحلة", target="900",
                  start_date=TODAY, target_date="2027-06-01", frequency="monthly", priority="2")
        self.post("/finance/business/reconciliation", account_id=str(bank), kind="bank_statement",
                  statement_date=TODAY, statement_balance="1000")
        self.post("/finance/settings/access", action="grant", user_id=str(self.ctx["accountant_id"]),
                  profile_kind="personal", owner_password="owner-pass-123", confirm="1")
        r = self.c.post("/finance/backup", data={"_csrf": self.csrf(), "scope": "business"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"SQLite format 3"))

    def conn(self):
        if not hasattr(self, "_conn"):
            self._conn = db.connect(os.path.join(self.tmp, "demo.sqlite"))
            self.addCleanup(self._conn.close)
        return self._conn
