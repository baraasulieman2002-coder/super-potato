"""Loan calculations are checked against independently known values."""
import unittest
from decimal import Decimal

from namareq import loan_math, loans
from namareq.db import FinanceError
from namareq.money import to_minor as m

from helpers import FinanceTestCase


class LoanMathTests(unittest.TestCase):
    def test_annuity_matches_textbook_value(self):
        # 10,000 at 12%/yr monthly for 12 months: payment 888.4879 -> 888.488 JOD
        sched = loan_math.build_schedule(m("10000"), "12", 12, "monthly", "reducing_balance", "2026-02-01")
        self.assertEqual(sched[0].payment_minor, 888_488)
        self.assertEqual(sched[0].interest_minor, 100_000)  # 1% of 10,000
        totals = loan_math.totals(sched)
        self.assertEqual(totals["total_principal_minor"], m("10000"))
        self.assertEqual(sched[-1].closing_minor, 0)
        self.assertAlmostEqual(totals["total_interest_minor"] / 1000, 661.85, delta=0.01)

    def test_zero_rate_and_flat(self):
        free = loan_math.build_schedule(m("1000"), "0", 3, "monthly", "none", "2026-02-01")
        self.assertEqual([r.principal_minor for r in free], [333334, 333333, 333333])
        flat = loan_math.build_schedule(m("10000"), "5", 24, "monthly", "flat", "2026-02-01")
        self.assertEqual(loan_math.totals(flat)["total_interest_minor"], m("1000"))  # 10,000 x 5% x 2 years
        eff = loan_math.effective_annual_rate(m("10000"), flat[0].payment_minor, 24, "monthly")
        self.assertTrue(Decimal("9.2") < eff < Decimal("9.4"), eff)  # flat 5% ~ 9.3% effective

    def test_month_end_dates_are_clamped_and_anchored(self):
        sched = loan_math.build_schedule(m("300"), "0", 3, "monthly", "none", "2026-01-31")
        self.assertEqual([r.due_date for r in sched], ["2026-01-31", "2026-02-28", "2026-03-31"])

    def test_extra_payment_simulation(self):
        res = loan_math.simulate_extra_payment(m("10000"), "12", "monthly", 888_488, m("200"))
        self.assertGreater(res["interest_saved_minor"], 0)
        self.assertGreater(res["periods_saved"], 0)
        self.assertEqual(res["base_periods"], 12)

    def test_early_settlement_formula(self):
        q = loan_math.early_settlement_amount(m("5000"), "10", 30, "1", "reducing_balance")
        self.assertEqual(q["accrued_interest_minor"], round(5_000_000 * 0.10 * 30 / 365))
        self.assertEqual(q["fee_minor"], m("50"))


class LoanServiceTests(FinanceTestCase):
    def make_loan(self, **kw):
        args = dict(profile="business", role="borrower", borrower="Shop", lender="Bank", loan_type="bank_loan",
                    principal_minor=m("10000"), annual_rate="12", interest_method="reducing_balance",
                    start_date="2026-01-01", first_payment_date="2026-02-01", frequency="monthly", term_count=12,
                    cash_account_id=self.bank)
        args.update(kw)
        return loans.create_loan(self.conn, self.owner, **args)

    def test_origination_and_installment_split(self):
        lid = self.make_loan(fees_minor=m("100"))
        self.assertEqual(self.bal(self.bank), m("19900"))
        loans.record_payment(self.conn, self.owner, lid, amount_minor=888_488, pay_date="2026-02-01")
        loan = self.conn.execute("SELECT loan_account_id FROM loans WHERE id=?", (lid,)).fetchone()
        self.assertEqual(self.bal(loan["loan_account_id"]), m("10000") - 788_488)
        pay = self.conn.execute("SELECT * FROM loan_payments WHERE loan_id=?", (lid,)).fetchone()
        self.assertEqual((pay["principal_minor"], pay["interest_minor"]), (788_488, 100_000))
        self.assertBooksBalance()

    def test_extra_payment_reamortizes_and_shortens(self):
        lid = self.make_loan()
        loans.record_payment(self.conn, self.owner, lid, amount_minor=888_488 + m("3000"), pay_date="2026-02-01")
        insts = loans.installments(self.conn, lid)
        self.assertLess(len(insts), 12)
        self.assertEqual(loans.outstanding_principal(self.conn, lid), m("10000") - 788_488 - m("3000"))
        # Remaining schedule fully amortizes the remaining principal.
        unpaid = sum(i["principal_minor"] for i in insts if i["status"] == "scheduled")
        self.assertEqual(unpaid, loans.outstanding_principal(self.conn, lid))

    def test_missed_payments_tracked(self):
        lid = self.make_loan()
        summary = [l for l in loans.list_loans(self.conn, self.owner, "business", "2026-04-15") if l["id"] == lid][0]
        self.assertEqual(summary["missed_count"], 3)
        self.assertEqual(summary["status"], "overdue")

    def test_early_settlement_completes_loan(self):
        lid = self.make_loan(early_settlement_fee_pct="1")
        loans.record_payment(self.conn, self.owner, lid, amount_minor=888_488, pay_date="2026-02-01")
        q = loans.settlement_quote(self.conn, self.owner, lid, "2026-02-16")
        self.assertEqual(q["days_accrued"], 15)
        loans.settle_early(self.conn, self.owner, lid, pay_date="2026-02-16")
        self.assertEqual(loans.outstanding_principal(self.conn, lid), 0)
        self.assertEqual(self.conn.execute("SELECT status FROM loans WHERE id=?", (lid,)).fetchone()[0], "completed")
        self.assertBooksBalance()

    def test_overpayment_rejected(self):
        lid = self.make_loan(principal_minor=m("100"), term_count=1)
        with self.assertRaises(FinanceError):
            loans.record_payment(self.conn, self.owner, lid, amount_minor=m("500"), pay_date="2026-02-01")

    def test_owner_business_loan_is_mirrored(self):
        bl, pl = loans.create_owner_business_loan(
            self.conn, self.owner, direction="owner_to_business", principal_minor=m("1200"), annual_rate="0",
            interest_method="none", start_date="2026-01-05", first_payment_date="2026-02-05", frequency="monthly",
            term_count=4, business_account_id=self.bank, personal_account_id=self.pbank)
        loans.repay_owner_business_loan(self.conn, self.owner, bl, amount_minor=m("300"), pay_date="2026-02-05",
                                        business_account_id=self.bank, personal_account_id=self.pbank)
        self.assertEqual(loans.outstanding_principal(self.conn, bl), m("900"))
        self.assertEqual(loans.outstanding_principal(self.conn, pl), m("900"))
        self.assertEqual(self.bal(self.sys("business", "loan_from_owner")), m("900"))
        self.assertEqual(self.bal(self.sys("personal", "loan_to_shop")), m("900"))
        with self.assertRaises(FinanceError):
            loans.create_loan(self.conn, self.owner, profile="business", role="borrower", borrower="Shop",
                              lender="Owner", loan_type="owner_loan_to_business", principal_minor=100, annual_rate="0",
                              interest_method="none", start_date="2026-01-01", first_payment_date="2026-02-01",
                              frequency="monthly", term_count=1, cash_account_id=self.bank)
        self.assertBooksBalance()

    def test_affordability(self):
        from namareq import transactions as tx
        for month in ("2026-01-15", "2026-02-15", "2026-03-15"):
            tx.record_income(self.conn, self.owner, profile="personal", account_id=self.pbank,
                             category_id=self.pin("salary"), txn_date=month, amount_minor=m("1000"))
        a = loans.affordability(self.conn, self.owner, "personal", m("250"), "monthly", "2026-04-10")
        self.assertEqual(a["avg_monthly_income_minor"], m("1000"))
        self.assertEqual(a["ratio_after_pct"], 25.0)


if __name__ == "__main__":
    unittest.main()
