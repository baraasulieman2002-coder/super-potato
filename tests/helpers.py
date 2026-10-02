import unittest

from namareq import accounts, db, permissions
from namareq.money import to_minor as m


class FinanceTestCase(unittest.TestCase):
    """Fresh in-memory database with an owner and basic accounts."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.owner = permissions.get_user(
            self.conn, permissions.create_user(self.conn, "owner", "Owner", "owner-pass-123", "owner"))
        o = self.owner
        self.bank = accounts.create_account(self.conn, o, profile="business", account_type="business_bank",
                                            name_en="Shop bank", name_ar="بنك المحل",
                                            opening_balance_minor=m("10000"), opening_date="2026-01-01")
        self.cash = accounts.create_account(self.conn, o, profile="business", account_type="business_cash",
                                            name_en="Register", name_ar="الصندوق")
        self.pbank = accounts.create_account(self.conn, o, profile="personal", account_type="personal_bank",
                                             name_en="Personal bank", name_ar="بنك شخصي",
                                             opening_balance_minor=m("5000"), opening_date="2026-01-01")
        self.savings = accounts.create_account(self.conn, o, profile="personal", account_type="savings",
                                               name_en="Savings", name_ar="توفير")

    def bcat(self, code):
        return db.expense_category(self.conn, "business", code)["id"]

    def pcat(self, code):
        return db.expense_category(self.conn, "personal", code)["id"]

    def bin(self, code):
        return db.income_category(self.conn, "business", code)["id"]

    def pin(self, code):
        return db.income_category(self.conn, "personal", code)["id"]

    def bal(self, account_id, as_of=None):
        from namareq import ledger
        return ledger.account_balance(self.conn, account_id, as_of)

    def sys(self, kind, code):
        return db.system_account(self.conn, kind, code)["id"]

    def assertBooksBalance(self):
        from namareq import ledger
        for kind in ("business", "personal"):
            tb = ledger.trial_balance(self.conn, db.profile_id(self.conn, kind))
            self.assertEqual(sum(r["debit"] for r in tb), sum(r["credit"] for r in tb), kind)
