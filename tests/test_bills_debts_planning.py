import unittest

from namareq import assets, bills, debts, planning, transactions as tx, transfers
from namareq.db import FinanceError
from namareq.money import to_minor as m

from helpers import FinanceTestCase


class BillTests(FinanceTestCase):
    def bill(self, **kw):
        args = dict(profile="business", payee="JEPCO", category_id=self.bcat("electricity"), due_date="2026-01-31",
                    amount_minor=m("100"), tax_minor=m("16"))
        args.update(kw)
        return bills.create_bill(self.conn, self.owner, **args)

    def status(self, bid, today):
        return [b for b in bills.list_bills(self.conn, self.owner, "business", today) if b["id"] == bid][0]

    def test_partial_payment_status_and_tax_split(self):
        bid = self.bill()
        self.assertEqual(self.status(bid, "2026-01-10")["status"], "scheduled")
        bills.pay_bill(self.conn, self.owner, bid, amount_minor=m("58"), pay_date="2026-01-20", account_id=self.bank)
        b = self.status(bid, "2026-01-21")
        self.assertEqual((b["status"], b["remaining_minor"]), ("partially_paid", m("58")))
        self.assertEqual(self.status(bid, "2026-02-02")["status"], "overdue")
        exp = self.conn.execute("SELECT amount_minor, tax_minor FROM expense_transactions WHERE bill_id=?", (bid,)).fetchone()
        self.assertEqual((exp[0], exp[1]), (m("50"), m("8")))
        with self.assertRaises(FinanceError):
            bills.pay_bill(self.conn, self.owner, bid, amount_minor=m("60"), pay_date="2026-01-22", account_id=self.bank)
        bills.pay_bill(self.conn, self.owner, bid, amount_minor=m("58"), pay_date="2026-01-22", account_id=self.bank)
        self.assertEqual(self.status(bid, "2026-02-02")["status"], "paid")

    def test_duplicate_detection(self):
        self.bill()
        with self.assertRaises(FinanceError) as cm:
            self.bill(due_date="2026-02-02")
        self.assertEqual(cm.exception.key, "error.possible_duplicate_bill")
        self.bill(due_date="2026-02-02", allow_duplicate=True)

    def test_recurring_generation_keeps_month_end(self):
        self.bill(frequency="monthly", due_date="2026-01-31")
        bills.generate_recurring_bills(self.conn, self.owner, "2026-03-25", horizon_days=10)
        dues = [b["due_date"] for b in bills.list_bills(self.conn, self.owner, "business", "2026-03-25")]
        # Mar 31 falls inside the 10-day horizon, so its successor (Apr 30) exists too.
        self.assertEqual(dues, ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"])
        # Running again creates nothing new.
        self.assertEqual(bills.generate_recurring_bills(self.conn, self.owner, "2026-03-25", horizon_days=10), [])

    def test_alerts_and_forecast(self):
        self.bill(due_date="2026-01-10")
        self.bill(payee="Orange", due_date="2026-01-15", amount_minor=m("30"), tax_minor=0, autopay=True)
        self.bill(payee="Water", due_date="2026-01-20", amount_minor=m("20"), tax_minor=0, frequency="monthly")
        alerts = bills.bill_alerts(self.conn, self.owner, "business", "2026-01-15")
        self.assertEqual(len(alerts["overdue"]), 1)
        self.assertEqual(len(alerts["due_today"]), 1)
        self.assertEqual(len(alerts["autopay_to_confirm"]), 1)
        self.assertEqual(len(alerts["upcoming"]), 1)
        fc = bills.monthly_bill_forecast(self.conn, self.owner, "business", "2026-01-15", months=3)
        self.assertEqual([x["amount_minor"] for x in fc], [m("166"), m("20"), m("20")])

    def test_recurring_items_wait_for_confirmation(self):
        rid = bills.create_recurring(self.conn, self.owner, profile="personal", kind="income", name="Rent from flat",
                                     amount_minor=m("250"), frequency="monthly", next_date="2026-01-05",
                                     income_category_id=self.pin("rental_income"), account_id=self.pbank)
        self.assertEqual(len(bills.due_recurring(self.conn, self.owner, "personal", "2026-01-06")), 1)
        self.assertEqual(self.bal(self.pbank), m("5000"))  # nothing posted automatically
        bills.confirm_recurring(self.conn, self.owner, rid)
        self.assertEqual(self.bal(self.pbank), m("5250"))
        self.assertEqual(len(bills.due_recurring(self.conn, self.owner, "personal", "2026-01-06")), 0)


class DebtTests(FinanceTestCase):
    def test_aging_buckets_with_installment_plan(self):
        did = debts.create_debt(self.conn, self.owner, debt_type="customer", debtor="Ahmad", creditor="Shop",
                                original_minor=m("300"), issue_date="2026-01-01", origin="credit_sale",
                                income_category_id=self.bin("repair_services"),
                                installment_plan=[{"due_date": "2026-01-15", "amount_minor": m("100")},
                                                  {"due_date": "2026-02-15", "amount_minor": m("100")},
                                                  {"due_date": "2026-05-15", "amount_minor": m("100")}])
        debts.record_payment(self.conn, self.owner, did, amount_minor=m("50"), pay_date="2026-01-20",
                             cash_account_id=self.cash)
        aging = debts.aging_report(self.conn, self.owner, "business", "receivable", "2026-03-01")
        self.assertEqual(aging["totals"], {"current": m("100"), "d1_30": m("100"), "d31_60": m("50"),
                                           "d61_90": 0, "d90_plus": 0})
        self.assertEqual(self.bal(self.sys("business", "customer_receivables")), m("250"))

    def test_plan_must_match_and_payment_cannot_exceed(self):
        with self.assertRaises(FinanceError):
            debts.create_debt(self.conn, self.owner, debt_type="customer", debtor="A", creditor="Shop",
                              original_minor=m("300"), issue_date="2026-01-01", origin="opening",
                              installment_plan=[{"due_date": "2026-01-15", "amount_minor": m("100")}])
        did = debts.create_debt(self.conn, self.owner, debt_type="owed_by_owner", debtor="Me", creditor="Brother",
                                original_minor=m("200"), issue_date="2026-01-01", origin="cash_borrowed",
                                cash_account_id=self.pbank)
        with self.assertRaises(FinanceError):
            debts.record_payment(self.conn, self.owner, did, amount_minor=m("201"), pay_date="2026-01-10",
                                 cash_account_id=self.pbank)
        debts.record_payment(self.conn, self.owner, did, amount_minor=m("200"), pay_date="2026-01-10",
                             cash_account_id=self.pbank)
        self.assertEqual(debts.list_debts(self.conn, self.owner, "personal", status="settled")[0]["remaining_minor"], 0)
        self.assertBooksBalance()

    def test_write_off_is_owner_only(self):
        from namareq import permissions
        did = debts.create_debt(self.conn, self.owner, debt_type="customer", debtor="Gone", creditor="Shop",
                                original_minor=m("80"), issue_date="2026-01-01", origin="opening")
        acc = permissions.get_user(self.conn, permissions.create_user(self.conn, "acc", "Acc", "accountant-1", "accountant"))
        with self.assertRaises(FinanceError):
            debts.write_off(self.conn, acc, did, "2026-03-01", "moved abroad")
        debts.write_off(self.conn, self.owner, did, "2026-03-01", "moved abroad")
        self.assertEqual(self.bal(self.sys("business", "customer_receivables")), 0)


class PlanningTests(FinanceTestCase):
    def test_budget_actual_projection_and_rollover(self):
        dining = self.pcat("dining")
        planning.create_budget(self.conn, self.owner, profile="personal", name="Jan", period_type="monthly",
                               start_date="2026-01-01", rollover=True,
                               lines=[{"line_type": "expense", "expense_category_id": dining, "amount_minor": m("100")},
                                      {"line_type": "savings", "amount_minor": m("200")}])
        tx.record_expense(self.conn, self.owner, profile="personal", account_id=self.pbank, category_id=dining,
                          txn_date="2026-01-10", amount_minor=m("60"))
        transfers.internal_transfer(self.conn, self.owner, profile="personal", from_account_id=self.pbank,
                                    to_account_id=self.savings, txn_date="2026-01-12", amount_minor=m("150"),
                                    description="save")
        feb = planning.create_budget(self.conn, self.owner, profile="personal", name="Feb", period_type="monthly",
                                     start_date="2026-02-01", rollover=True,
                                     lines=[{"line_type": "expense", "expense_category_id": dining, "amount_minor": m("100")}])
        jan = self.conn.execute("SELECT id FROM budgets WHERE name='Jan'").fetchone()[0]
        r = planning.budget_vs_actual(self.conn, self.owner, jan, "2026-01-15")
        dine, save = r["lines"][0], r["lines"][1]
        self.assertEqual((dine["actual_minor"], dine["pct_used"]), (m("60"), 60.0))
        self.assertEqual(dine["projected_minor"], round(m("60") * 31 / 15))
        self.assertTrue(dine["at_risk"])
        self.assertEqual(save["actual_minor"], m("150"))
        r2 = planning.budget_vs_actual(self.conn, self.owner, feb, "2026-02-10")
        self.assertEqual(r2["lines"][0]["rollover_minor"], m("40"))
        self.assertEqual(r2["lines"][0]["budgeted_minor"], m("140"))
        self.assertEqual(r2["lines"][0]["prior_minor"], m("60"))

    def test_goal_linked_account_and_forecast(self):
        gid = planning.create_goal(self.conn, self.owner, profile="personal", goal_type="emergency_fund",
                                   name_en="Emergency", name_ar="طوارئ", target_minor=m("1200"),
                                   start_date="2026-01-01", target_date="2026-12-31", linked_account_id=self.savings)
        for d in ("2026-01-15", "2026-02-15", "2026-03-15"):
            transfers.internal_transfer(self.conn, self.owner, profile="personal", from_account_id=self.pbank,
                                        to_account_id=self.savings, txn_date=d, amount_minor=m("100"), description="s")
        g = planning.list_goals(self.conn, self.owner, "personal", "2026-03-31")[0]
        self.assertEqual(g["id"], gid)
        self.assertEqual(g["current_minor"], m("300"))
        self.assertEqual(g["progress_pct"], 25.0)
        self.assertIsNotNone(g["forecast_completion"])
        with self.assertRaises(FinanceError):
            planning.add_contribution(self.conn, self.owner, gid, amount_minor=1, on="2026-03-31")


class AssetTests(FinanceTestCase):
    def test_straight_line_and_disposal_gain(self):
        aid = assets.create_asset(self.conn, self.owner, profile="business", asset_class="computers", name_en="PC",
                                  name_ar="حاسوب", purchase_value_minor=m("1200"), purchase_date="2026-01-10",
                                  funding="cash", cash_account_id=self.bank, useful_life_months=12,
                                  depreciation_method="straight_line")
        assets.post_depreciation(self.conn, self.owner, aid, "2026-04-30")
        a = assets.list_assets(self.conn, self.owner, "business")[0]
        self.assertEqual(a["accumulated_depreciation_minor"], m("300"))
        self.assertEqual(a["carrying_value_minor"], m("900"))
        assets.post_depreciation(self.conn, self.owner, aid, "2026-04-30")  # idempotent
        self.assertEqual(assets.list_assets(self.conn, self.owner, "business")[0]["carrying_value_minor"], m("900"))
        assets.dispose(self.conn, self.owner, aid, on="2026-04-30", proceeds_minor=m("1000"), cash_account_id=self.bank)
        from namareq import reports
        pl = reports.income_statement(self.conn, self.owner, "business", "2026-01-01", "2026-04-30")
        self.assertEqual(pl["total_income_minor"], m("100"))  # gain on sale
        self.assertBooksBalance()

    def test_declining_balance_never_below_salvage(self):
        sched = assets.depreciation_schedule(m("1000"), m("100"), 24, "declining_balance", "2026-01-01")
        self.assertEqual(sched[-1]["nbv_minor"], m("100"))
        self.assertTrue(all(s["amount_minor"] >= 0 for s in sched))

    def test_personal_assets_revalue_net_worth(self):
        from namareq import reports
        aid = assets.create_asset(self.conn, self.owner, profile="personal", asset_class="gold", name_en="Gold",
                                  name_ar="ذهب", purchase_value_minor=m("2000"), purchase_date="2025-01-01",
                                  funding="opening")
        before = reports.balance_sheet(self.conn, self.owner, "personal", "2026-02-01")["net_worth_minor"]
        assets.revalue(self.conn, self.owner, aid, m("2300"), "2026-02-01")
        after = reports.balance_sheet(self.conn, self.owner, "personal", "2026-02-01")
        self.assertEqual(after["net_worth_minor"] - before, m("300"))
        self.assertEqual(reports.income_statement(self.conn, self.owner, "personal", "2026-01-01", "2026-02-28")["total_income_minor"], 0)
        self.assertTrue(after["balanced"])
        with self.assertRaises(FinanceError):
            assets.create_asset(self.conn, self.owner, profile="personal", asset_class="vehicles", name_en="Car",
                                name_ar="سيارة", purchase_value_minor=1, purchase_date="2025-01-01", funding="opening",
                                useful_life_months=12, depreciation_method="straight_line")


if __name__ == "__main__":
    unittest.main()
