import sqlite3
import unittest
from decimal import Decimal

from namareq import db, ledger
from namareq.db import FinanceError
from namareq.money import MoneyError, fmt, from_minor, split_proportionally, to_minor

from helpers import FinanceTestCase


class MoneyTests(unittest.TestCase):
    def test_jod_has_three_decimals_and_rounds_half_up(self):
        self.assertEqual(to_minor("12.5"), 12500)
        self.assertEqual(to_minor("0.0005"), 1)
        self.assertEqual(to_minor("0.0004"), 0)
        self.assertEqual(to_minor("1,234.567"), 1234567)
        self.assertEqual(to_minor("10.005", "USD"), 1001)
        self.assertEqual(from_minor(1234567), Decimal("1234.567"))

    def test_arabic_digits_are_accepted(self):
        self.assertEqual(to_minor("١٢٣٫٤٥"), 123450)

    def test_floats_and_garbage_rejected(self):
        for bad in (0.1, "abc", "", "NaN", True):
            with self.assertRaises(MoneyError):
                to_minor(bad)

    def test_split_proportionally_is_exact(self):
        parts = split_proportionally(100, [1, 1, 1])
        self.assertEqual(sum(parts), 100)
        self.assertEqual(sorted(parts), [33, 33, 34])
        self.assertEqual(split_proportionally(50_000, [100_000, 16_000]), [43103, 6897])

    def test_format(self):
        self.assertEqual(fmt(-1234500), "(1,234.500) JOD")
        self.assertEqual(fmt(1500, lang="ar"), "1.500 د.أ")


class LedgerTests(FinanceTestCase):
    def test_unbalanced_entry_rejected(self):
        with self.assertRaises(FinanceError) as cm:
            ledger.post_entry(self.conn, profile_id=1, entry_date="2026-01-02", entry_type="x", description="x",
                              lines=[ledger.dr(self.bank, 100), ledger.cr(self.cash, 99)])
        self.assertEqual(cm.exception.key, "error.unbalanced_entry")

    def test_posted_entries_are_immutable_in_the_database(self):
        for sql in ("UPDATE ledger_entries SET description='hack'", "DELETE FROM ledger_entries",
                    "UPDATE ledger_lines SET debit_minor = debit_minor + 1", "DELETE FROM ledger_lines"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql)

    def test_database_rejects_line_from_other_profile(self):
        bpid = db.profile_id(self.conn, "business")
        cur = self.conn.execute("INSERT INTO ledger_entries(profile_id, entry_date, entry_type, currency, description) "
                                "VALUES (?, '2026-01-01', 't', 'JOD', 'x')", (bpid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO ledger_lines(entry_id, account_id, debit_minor) VALUES (?,?,5)",
                              (cur.lastrowid, self.pbank))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO ledger_lines(entry_id, expense_category_id, debit_minor) VALUES (?,?,5)",
                              (cur.lastrowid, self.pcat("dining")))

    def test_service_rejects_cross_profile_lines(self):
        with self.assertRaises(FinanceError) as cm:
            ledger.post_entry(self.conn, profile_id=db.profile_id(self.conn, "business"), entry_date="2026-01-02",
                              entry_type="x", description="x",
                              lines=[ledger.dr(self.bank, 100), ledger.cr(self.pbank, 100)])
        self.assertEqual(cm.exception.key, "error.account_wrong_profile")

    def test_reversal_nets_to_zero_and_keeps_history(self):
        pid = db.profile_id(self.conn, "business")
        e = ledger.post_entry(self.conn, profile_id=pid, entry_date="2026-01-02", entry_type="x", description="x",
                              lines=[ledger.dr(self.cash, 500), ledger.cr(self.bank, 500)])
        r = ledger.reverse_entry(self.conn, e, user_id=1, reversal_date="2026-01-03", reason="typo")
        self.assertEqual(self.bal(self.cash), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ledger_entries WHERE id IN (?,?)", (e, r)).fetchone()[0], 2)
        with self.assertRaises(FinanceError):
            ledger.reverse_entry(self.conn, e, user_id=1, reversal_date="2026-01-03", reason="again")
        with self.assertRaises(FinanceError):
            ledger.reverse_entry(self.conn, r, user_id=1, reversal_date="2026-01-03", reason="undo")

    def test_closed_period_blocks_posting(self):
        pid = db.profile_id(self.conn, "business")
        ledger.close_period(self.conn, self.owner, pid, "2026-01-01", "2026-01-31")
        with self.assertRaises(FinanceError) as cm:
            ledger.post_entry(self.conn, profile_id=pid, entry_date="2026-01-15", entry_type="x", description="x",
                              lines=[ledger.dr(self.cash, 1), ledger.cr(self.bank, 1)])
        self.assertEqual(cm.exception.key, "error.period_closed")
        # The personal profile is unaffected by closing the business period.
        ppid = db.profile_id(self.conn, "personal")
        ledger.post_entry(self.conn, profile_id=ppid, entry_date="2026-01-15", entry_type="x", description="x",
                          lines=[ledger.dr(self.savings, 1), ledger.cr(self.pbank, 1)])

    def test_every_change_is_audited(self):
        n = self.conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name='ledger_entries'").fetchone()[0]
        self.assertGreaterEqual(n, 2)  # the two opening balances


if __name__ == "__main__":
    unittest.main()
