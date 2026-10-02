"""Business / personal separation and the owner <-> shop workflows."""
import unittest

from namareq import accounts, reports, transactions as tx, transfers
from namareq.db import FinanceError
from namareq.money import to_minor as m

from helpers import FinanceTestCase

JAN = ("2026-01-01", "2026-01-31")


class SeparationTests(FinanceTestCase):
    def pl(self, kind="business"):
        return reports.income_statement(self.conn, self.owner, kind, *JAN)

    def owner_tx(self, ttype, amount, **kw):
        kw.setdefault("business_account_id", self.bank)
        kw.setdefault("personal_account_id", self.pbank)
        return transfers.owner_transfer(self.conn, self.owner, transfer_type=ttype, txn_date="2026-01-10",
                                        amount_minor=m(amount), description=ttype, **kw)

    def test_transfer_type_is_required(self):
        with self.assertRaises(FinanceError) as cm:
            transfers.owner_transfer(self.conn, self.owner, transfer_type="", txn_date="2026-01-10",
                                     amount_minor=100, description="x", business_account_id=self.bank,
                                     personal_account_id=self.pbank)
        self.assertEqual(cm.exception.key, "error.transfer_type_required")

    def test_capital_contribution_is_not_revenue(self):
        self.owner_tx("capital_contribution", "2000")
        self.assertEqual(self.pl()["total_income_minor"], 0)
        self.assertEqual(self.pl("personal")["total_expenses_minor"], 0)
        self.assertEqual(self.bal(self.bank), m("12000"))
        self.assertEqual(self.bal(self.pbank), m("3000"))
        self.assertEqual(self.bal(self.sys("business", "owner_contributions")), m("2000"))
        self.assertBooksBalance()

    def test_withdrawal_does_not_reduce_business_profit(self):
        self.owner_tx("owner_withdrawal", "300")
        self.assertEqual(self.pl()["total_expenses_minor"], 0)
        self.assertEqual(self.pl("personal")["total_income_minor"], m("300"))

    def test_business_pays_personal_electricity_is_withdrawal(self):
        self.owner_tx("personal_paid_from_business", "45", personal_expense_category_id=self.pcat("utilities"))
        biz = self.pl()
        self.assertEqual(biz["net_minor"], 0, "a personal bill must never become a business expense")
        per = self.pl("personal")
        self.assertEqual(per["total_expenses_minor"], m("45"))
        self.assertEqual(self.bal(self.sys("business", "owner_withdrawals")), -m("45"))  # equity reduced
        entry = self.conn.execute("SELECT description FROM ledger_entries WHERE profile_id=1 AND entry_type=?",
                                  ("personal_paid_from_business",)).fetchone()
        self.assertNotIn("personal_paid_from_business", entry["description"])
        self.assertNotIn("electricity", entry["description"].lower())  # owner's description stays private

    def test_personal_payment_from_business_as_receivable_then_repaid(self):
        self.owner_tx("personal_paid_from_business", "100", personal_expense_category_id=self.pcat("utilities"),
                      settle_as="due_from_owner")
        self.assertEqual(self.bal(self.sys("business", "due_from_owner")), m("100"))
        with self.assertRaises(FinanceError):
            self.owner_tx("owner_repays_business", "150")
        self.owner_tx("owner_repays_business", "100")
        self.assertEqual(self.bal(self.sys("business", "due_from_owner")), 0)
        self.assertEqual(self.bal(self.sys("personal", "due_to_business")), 0)
        self.assertBooksBalance()

    def test_supplier_paid_from_personal_account_creates_business_debt_to_owner(self):
        self.owner_tx("business_paid_from_personal", "200",
                      business_expense_category_id=self.bcat("inventory_purchases"), business_account_id=None)
        self.assertEqual(self.pl()["total_cost_of_sales_minor"], m("200"))
        self.assertEqual(self.pl("personal")["total_expenses_minor"], 0)
        self.assertEqual(self.bal(self.sys("business", "due_to_owner")), m("200"))
        self.assertEqual(self.bal(self.sys("personal", "due_from_business")), m("200"))
        with self.assertRaises(FinanceError):
            self.owner_tx("reimburse_owner", "250")
        self.owner_tx("reimburse_owner", "200")
        self.assertEqual(self.bal(self.sys("business", "due_to_owner")), 0)
        self.assertBooksBalance()

    def test_owner_salary_and_distribution(self):
        self.owner_tx("owner_salary", "700")
        self.assertEqual(self.pl()["total_expenses_minor"], m("700"))
        self.assertEqual(self.pl("personal")["total_income_minor"], m("700"))
        # No profit recorded yet beyond the salary loss -> distribution blocked.
        with self.assertRaises(FinanceError) as cm:
            self.owner_tx("profit_distribution", "100")
        self.assertEqual(cm.exception.key, "error.distribution_exceeds_profit")
        self.owner_tx("profit_distribution", "100", allow_exceeding_profit=True)

    def test_loan_repayment_interest_split(self):
        self.owner_tx("owner_loan_to_business", "1000")
        self.owner_tx("business_loan_repayment_to_owner", "110", interest_minor=m("10"))
        self.assertEqual(self.bal(self.sys("business", "loan_from_owner")), m("900"))
        self.assertEqual(self.pl()["total_expenses_minor"], m("10"))
        self.assertEqual(self.pl("personal")["total_income_minor"], m("10"))
        self.assertBooksBalance()

    def test_combined_net_worth_unchanged_by_owner_transfers(self):
        def combined():
            return reports.combined_overview(self.conn, self.owner, "2026-01-31")["combined"]["net_worth_minor"]
        before = combined()
        self.owner_tx("capital_contribution", "500")
        self.owner_tx("owner_loan_to_business", "300")
        self.owner_tx("owner_withdrawal", "200")
        self.assertEqual(combined(), before)

    def test_internal_transfer_cannot_cross_profiles(self):
        with self.assertRaises(FinanceError) as cm:
            transfers.internal_transfer(self.conn, self.owner, profile="business", from_account_id=self.bank,
                                        to_account_id=self.pbank, txn_date="2026-01-05", amount_minor=100,
                                        description="x")
        self.assertEqual(cm.exception.key, "error.cross_profile_requires_owner_transfer")

    def test_account_type_restricted_to_profile(self):
        with self.assertRaises(FinanceError):
            accounts.create_account(self.conn, self.owner, profile="personal", account_type="business_cash",
                                    name_en="x", name_ar="س")
        with self.assertRaises(FinanceError):
            tx.record_expense(self.conn, self.owner, profile="business", account_id=self.pbank,
                              category_id=self.bcat("rent"), txn_date="2026-01-03", amount_minor=100)
        with self.assertRaises(FinanceError):
            tx.record_expense(self.conn, self.owner, profile="business", account_id=self.bank,
                              category_id=self.pcat("dining"), txn_date="2026-01-03", amount_minor=100)

    def test_shop_sales_cannot_be_duplicated(self):
        tx.post_shop_sale(self.conn, self.owner, sale_id=7, txn_date="2026-01-03", account_id=self.cash,
                          net_minor=m("100"), tax_minor=m("16"))
        with self.assertRaises(FinanceError) as cm:
            tx.post_shop_sale(self.conn, self.owner, sale_id=7, txn_date="2026-01-03", account_id=self.cash,
                              net_minor=m("100"), tax_minor=m("16"))
        self.assertEqual(cm.exception.key, "error.duplicate_source")
        with self.assertRaises(FinanceError) as cm:
            tx.record_income(self.conn, self.owner, profile="business", account_id=self.cash,
                             category_id=self.bin("product_sales"), txn_date="2026-01-03", amount_minor=m("100"))
        self.assertEqual(cm.exception.key, "error.category_system_only")
        self.assertEqual(self.bal(self.sys("business", "sales_tax_payable")), m("16"))

    def test_personal_tax_never_recoverable(self):
        with self.assertRaises(FinanceError):
            tx.record_expense(self.conn, self.owner, profile="personal", account_id=self.pbank,
                              category_id=self.pcat("shopping"), txn_date="2026-01-03", amount_minor=100,
                              tax_minor=16, tax_recoverable=True)

    def test_unpaid_expense_then_payment_and_reversal(self):
        eid = tx.record_expense(self.conn, self.owner, profile="business", account_id=self.bank,
                                category_id=self.bcat("marketing"), txn_date="2026-01-03", amount_minor=m("80"),
                                paid=False, payee="Ad agency")
        self.assertEqual(self.bal(self.sys("business", "supplier_payables")), m("80"))
        tx.pay_expense(self.conn, self.owner, eid, "2026-01-20", self.bank)
        self.assertEqual(self.bal(self.sys("business", "supplier_payables")), 0)
        tx.reverse_transaction(self.conn, self.owner, "expense_transactions", eid, "2026-01-21", "wrong supplier")
        self.assertEqual(self.pl()["total_expenses_minor"], 0)
        self.assertEqual(self.bal(self.bank), m("10000"))
        self.assertBooksBalance()

    def test_personal_withholding_tax(self):
        tx.record_income(self.conn, self.owner, profile="personal", account_id=self.pbank,
                         category_id=self.pin("freelance"), txn_date="2026-01-05", amount_minor=m("100"),
                         tax_minor=m("5"))
        self.assertEqual(self.bal(self.pbank), m("5095"))
        self.assertEqual(self.pl("personal")["total_income_minor"], m("100"))


if __name__ == "__main__":
    unittest.main()
