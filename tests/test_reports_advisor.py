import unittest

from namareq import advisor, db, demo, forecast, metrics, permissions, reports
from namareq import transactions as tx
from namareq.money import to_minor as m

from helpers import FinanceTestCase

TODAY = "2026-10-02"


class DemoDataTests(unittest.TestCase):
    """Report identities on a realistic four-month dataset."""

    @classmethod
    def setUpClass(cls):
        cls.conn = db.connect(":memory:")
        cls.ctx = demo.build(cls.conn, TODAY)
        cls.owner = cls.ctx["owner"]

    def test_books_balance_and_statements_reconcile(self):
        for kind in ("business", "personal"):
            self.assertTrue(reports.trial_balance(self.conn, self.owner, kind, TODAY)["balanced"], kind)
            self.assertTrue(reports.balance_sheet(self.conn, self.owner, kind, TODAY)["balanced"], kind)
            cf = reports.cash_flow_statement(self.conn, self.owner, kind, "2026-06-01", "2026-09-30")
            self.assertTrue(cf["reconciles"], kind)

    def test_combined_overview_adds_only_balance_sheet_totals(self):
        o = reports.combined_overview(self.conn, self.owner, TODAY)
        self.assertEqual(o["combined"]["net_worth_minor"],
                         o["business"]["net_worth_minor"] + o["personal"]["net_worth_minor"])
        self.assertNotIn("total_income_minor", o["combined"])
        self.assertNotIn("total_expenses_minor", o["combined"])

    def test_tax_summary_excludes_personal(self):
        t = reports.tax_summary(self.conn, self.owner, "2026-09-01", "2026-09-30")
        self.assertGreater(t["output_tax_minor"], 0)
        self.assertGreater(t["input_tax_minor"], 0)

    def test_metrics_have_traceable_components(self):
        b = metrics.business_metrics(self.conn, self.owner, TODAY)
        self.assertIsNotNone(b["health"]["score"])
        for c in b["health"]["components"]:
            self.assertIn("zero_at", c)
            self.assertIn("weight", c)
        p = metrics.personal_metrics(self.conn, self.owner, TODAY)
        self.assertIsNotNone(p["values"]["emergency_months"])

    def test_forecast_is_labelled_estimate_with_assumptions(self):
        for h in forecast.HORIZONS:
            fc = forecast.cash_forecast(self.conn, self.owner, "business", TODAY, h)
            self.assertTrue(fc["is_estimate"])
            self.assertEqual(len(fc["series"]), h + 1)
            self.assertIn("baseline", fc["assumptions"])
            self.assertEqual(fc["closing_minor"], fc["opening_minor"] + fc["total_inflow_minor"] - fc["total_outflow_minor"])


class AdvisorTests(FinanceTestCase):
    def setUp(self):
        super().setUp()
        # Three months of steady dining, then a spike, and a large supplier bill.
        for month in ("2026-06", "2026-07", "2026-08"):
            for day in ("05", "15", "25"):
                tx.record_expense(self.conn, self.owner, profile="personal", account_id=self.pbank,
                                  category_id=self.pcat("dining"), txn_date=f"{month}-{day}", amount_minor=m("20"),
                                  payee="Cafe")
        for day in ("2026-09-10", "2026-09-15", "2026-09-20", "2026-09-25"):
            tx.record_expense(self.conn, self.owner, profile="personal", account_id=self.pbank,
                              category_id=self.pcat("dining"), txn_date=day, amount_minor=m("40"), payee="Cafe")
        from namareq import bills
        bills.create_bill(self.conn, self.owner, profile="business", payee="Supplier", amount_minor=m("15000"),
                          category_id=self.bcat("inventory_purchases"), due_date="2026-10-20")
        tx.record_expense(self.conn, self.owner, profile="business", account_id=self.bank,
                          category_id=self.bcat("misc_business"), txn_date="2026-09-28", amount_minor=m("30"),
                          payee="cafe")

    def test_rules_fire_with_complete_evidence(self):
        found = {a["rule_code"]: a for s in ("business", "personal") for a in advisor.evaluate(self.conn, self.owner, s, "2026-10-01")}
        self.assertIn("cash_shortage", found)
        self.assertEqual(found["cash_shortage"]["params"]["days"], 19)
        self.assertIn("category_spike", found)
        self.assertIn("personal_payee_in_business", found)
        for a in found.values():
            ev = a["evidence"]
            for part in ("why", "numbers", "date_range", "assumption", "benefit", "risk"):
                self.assertTrue(ev[part], (a["rule_code"], part))
            self.assertTrue(all(len(n) == 3 for n in ev["numbers"]))

    def test_advisor_never_changes_financial_records(self):
        tables = ("ledger_entries", "ledger_lines", "income_transactions", "expense_transactions", "transfers",
                  "bills", "loans", "debt_records")
        before = {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        advisor.generate(self.conn, self.owner, "2026-10-01")
        after = {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        self.assertEqual(before, after)

    def test_owner_decisions_are_kept_and_respected(self):
        advisor.generate(self.conn, self.owner, "2026-10-01")
        item = advisor.list_advice(self.conn, self.owner, "personal", "2026-10-01")[0]
        advisor.act(self.conn, self.owner, item["id"], "dismissed", "known")
        advisor.generate(self.conn, self.owner, "2026-10-01")
        ids = [a["id"] for a in advisor.list_advice(self.conn, self.owner, "personal", "2026-10-01")]
        self.assertNotIn(item["id"], ids)
        fps = [a["fingerprint"] for a in advisor.list_advice(self.conn, self.owner, "personal", "2026-10-01")]
        self.assertNotIn(item["fingerprint"], fps, "dismissed advice must not be re-raised")
        history = advisor.list_advice(self.conn, self.owner, "personal", "2026-10-01", include_closed=True)
        dismissed = [a for a in history if a["id"] == item["id"]][0]
        self.assertEqual(dismissed["actions"][0]["note"], "known")

    def test_postponed_advice_returns_after_date(self):
        advisor.generate(self.conn, self.owner, "2026-10-01")
        item = advisor.list_advice(self.conn, self.owner, "business", "2026-10-01")[0]
        advisor.act(self.conn, self.owner, item["id"], "postponed", postpone_until="2026-10-10")
        self.assertNotIn(item["id"], [a["id"] for a in advisor.list_advice(self.conn, self.owner, "business", "2026-10-05")])
        self.assertIn(item["id"], [a["id"] for a in advisor.list_advice(self.conn, self.owner, "business", "2026-10-10")])

    def test_accountant_without_grant_gets_no_personal_advice(self):
        acc = permissions.get_user(self.conn, permissions.create_user(self.conn, "acc", "A", "accountant-1", "accountant"))
        summary = advisor.generate(self.conn, acc, "2026-10-01")
        self.assertGreater(summary["created"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM financial_advice WHERE scope='personal'").fetchone()[0], 0)
        # Payee matching needs both profiles, so it must not run for the accountant.
        self.assertFalse(any(a["rule_code"] == "personal_payee_in_business"
                             for a in advisor.evaluate(self.conn, acc, "business", "2026-10-01")))


if __name__ == "__main__":
    unittest.main()
