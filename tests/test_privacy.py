import os
import sqlite3
import tempfile
import unittest

from namareq import backup, permissions, reports, transactions as tx
from namareq.db import FinanceError

from helpers import FinanceTestCase


class PrivacyTests(FinanceTestCase):
    def user(self, role):
        return permissions.get_user(self.conn, permissions.create_user(self.conn, role, role, f"{role}-pass-1", role))

    def test_staff_roles_never_see_personal(self):
        for role in ("manager", "cashier", "technician", "stockkeeper"):
            u = self.user(role)
            self.assertFalse(permissions.can_access(self.conn, u, "personal"))
            with self.assertRaises(FinanceError):
                reports.balance_sheet(self.conn, u, "personal", "2026-01-31")
            with self.assertRaises(FinanceError) as cm:
                permissions.grant_access(self.conn, self.owner, u["id"], "personal", "owner-pass-123")
            self.assertEqual(cm.exception.key, "error.role_cannot_receive_grant")

    def test_accountant_business_by_default_personal_only_with_confirmed_grant(self):
        acc = self.user("accountant")
        self.assertTrue(permissions.can_access(self.conn, acc, "business"))
        self.assertFalse(permissions.can_access(self.conn, acc, "personal"))
        with self.assertRaises(FinanceError) as cm:
            permissions.grant_access(self.conn, self.owner, acc["id"], "personal", "wrong-password")
        self.assertEqual(cm.exception.key, "error.confirmation_failed")
        with self.assertRaises(FinanceError):
            permissions.grant_access(self.conn, acc, acc["id"], "personal", "accountant-pass-1")
        gid = permissions.grant_access(self.conn, self.owner, acc["id"], "personal", "owner-pass-123")
        self.assertTrue(permissions.can_access(self.conn, acc, "personal"))
        permissions.revoke_access(self.conn, self.owner, gid)
        self.assertFalse(permissions.can_access(self.conn, acc, "personal"))

    def test_manager_needs_grant_for_business(self):
        mgr = self.user("manager")
        self.assertFalse(permissions.can_access(self.conn, mgr, "business"))
        permissions.grant_access(self.conn, self.owner, mgr["id"], "business", "owner-pass-123")
        self.assertTrue(permissions.can_access(self.conn, mgr, "business"))

    def test_personal_access_is_logged_including_denials(self):
        cashier = self.user("cashier")
        with self.assertRaises(FinanceError):
            tx.list_transactions(self.conn, cashier, "expense_transactions", "personal")
        tx.list_transactions(self.conn, self.owner, "expense_transactions", "personal")
        rows = self.conn.execute("SELECT user_id, allowed FROM personal_access_log ORDER BY id").fetchall()
        self.assertIn((cashier["id"], 0), [tuple(r) for r in rows])
        self.assertIn((self.owner["id"], 1), [tuple(r) for r in rows])

    def test_business_backup_contains_no_personal_data(self):
        tx.record_expense(self.conn, self.owner, profile="personal", account_id=self.pbank,
                          category_id=self.pcat("healthcare"), txn_date="2026-01-05", amount_minor=99_000,
                          payee="Private clinic")
        from namareq import transfers
        transfers.owner_transfer(self.conn, self.owner, transfer_type="personal_paid_from_business",
                                 txn_date="2026-01-06", amount_minor=10_000, description="Son's school fees",
                                 business_account_id=self.bank, personal_expense_category_id=self.pcat("education"))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.sqlite")
            backup.business_backup(self.conn, self.owner, path)
            b = sqlite3.connect(path)
            self.assertEqual(b.execute("SELECT COUNT(*) FROM financial_profiles WHERE kind='personal'").fetchone()[0], 0)
            self.assertEqual(b.execute("SELECT COUNT(*) FROM expense_categories WHERE profile_kind='personal'").fetchone()[0], 0)
            self.assertEqual(b.execute("SELECT COUNT(*) FROM ledger_entries WHERE profile_id <> 1").fetchone()[0], 0)
            dump = "\n".join(b.iterdump())
            for secret in ("Private clinic", "Son's school fees", "Personal bank"):
                self.assertNotIn(secret, dump)
            self.assertEqual(b.execute("SELECT COUNT(*) FROM transfers WHERE personal_entry_id IS NOT NULL").fetchone()[0], 0)

    def test_personal_backup_requires_owner_password_and_is_encrypted(self):
        acc = self.user("accountant")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.nmrq")
            with self.assertRaises(FinanceError):
                backup.personal_backup(self.conn, acc, path, "accountant-pass-1", "a long passphrase")
            with self.assertRaises(FinanceError):
                backup.personal_backup(self.conn, self.owner, path, "bad", "a long passphrase")
            backup.personal_backup(self.conn, self.owner, path, "owner-pass-123", "a long passphrase")
            with open(path, "rb") as fh:
                blob = fh.read()
            self.assertNotIn(b"SQLite format", blob)
            with self.assertRaises(FinanceError):
                backup.decrypt_bytes(blob, "wrong passphrase!")
            plain = backup.decrypt_bytes(blob, "a long passphrase")
            self.assertTrue(plain.startswith(b"SQLite format 3"))

    def test_account_numbers_are_masked(self):
        from namareq import accounts
        aid = accounts.create_account(self.conn, self.owner, profile="personal", account_type="credit_card",
                                      name_en="Visa", name_ar="فيزا", account_ref="4111 1111 1111 1234")
        stored = self.conn.execute("SELECT account_ref_masked FROM financial_accounts WHERE id=?", (aid,)).fetchone()[0]
        self.assertEqual(stored, "•••• 1234")
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(financial_accounts)")]
        for forbidden in ("password", "cvv", "pin", "security_code"):
            self.assertFalse(any(forbidden in c for c in cols))


if __name__ == "__main__":
    unittest.main()
