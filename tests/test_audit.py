from lake.audit.balances import balance
from lake.audit.receipt_view import receipt
from lake.audit.reconcile import reconcile
from lake.obs.intake import record
from lake.release.freeze import freeze
from tests import DAY, LakeCase, emit_scenario, ledger_rows


class AuditTests(LakeCase):
    def test_offline_receipt(self):
        self.stage_history()
        warehouse = self.warehouse()
        first = self.accept_issue(warehouse, 'R1')
        second = self.accept_issue(warehouse, 'R2')
        original_view = receipt(warehouse, second.receipt_id)
        self.assertEqual(original_view.reversal_evidence.original_receipt, first)
        third = self.accept_issue(warehouse, 'R3')
        self.source.close()
        warehouse.close()
        warehouse = self.warehouse()
        reopened = receipt(warehouse, second.receipt_id)
        self.assertEqual(reopened, original_view)
        linked = reopened.reversal_evidence
        self.assertEqual(linked.original_entry.entry_id, first.target_entry_id)
        latest = receipt(warehouse, third.receipt_id)
        actual = [linked.original_receipt.release.target.provenance,
                  latest.reversal_evidence.original_receipt.release.target.provenance]
        self.assertEqual(actual, ['log:1', 'log:2'])
        downstream = [balance(warehouse, DAY, 'A').millimeters, balance(warehouse, DAY, 'B').millimeters]
        self.assertEqual(downstream, [13, 0])
        emit_scenario('v8', ['R1', 'R2', 'R3', 'close source', 'reopen review'],
                      ['log:1', 'log:2'], actual, [13, 0], downstream,
                      ['lake/audit/provenance.py', 'lake/audit/receipt_view.py', 'lake/audit/reconcile.py'],
                      'R2 offsets the reading in log:1 and R3 offsets the reading in log:2')

    def test_partition_recompute(self):
        self.stage_history()
        warehouse = self.warehouse()
        for issue in ('R1', 'R2', 'R3'):
            self.accept_issue(warehouse, issue)
        record(self.source, 'P-09', 1, DAY, 'A', 1, 'cm', 'table:9')
        freeze(self.source, self.register, 'R4', 'P-09', 1)
        self.accept_issue(warehouse, 'R4')
        day = balance(warehouse, DAY)
        self.assertEqual(ledger_rows(day), [['target', 'A', 7, 1], ['reversal', 'A', -7, 1],
                                           ['target', 'B', 11, 2], ['reversal', 'B', -11, 2],
                                           ['target', 'A', 13, 3], ['target', 'A', 10, 1]])
        zero = balance(warehouse, DAY, 'B')
        self.assertEqual(ledger_rows(zero), [['target', 'B', 11, 2], ['reversal', 'B', -11, 2]])
        self.assertEqual(zero.millimeters, 0)
        self.assertEqual(day.millimeters, 23)
        report = reconcile(warehouse, DAY)
        self.assertEqual([(row.district, row.ledger_mm, row.effective_mm, row.difference_mm)
                          for row in report.partitions], [('A', 23, 23, 0), ('B', 0, 0, 0)])
        self.assertTrue(report.matches)

    def test_empty_query(self):
        self.stage_history()
        warehouse = self.warehouse()
        self.accept_issue(warehouse, 'R1')
        empty_day = '2026-02-19'
        for day, district in ((empty_day, None), (DAY, 'unused')):
            result = balance(warehouse, day, district)
            self.assertEqual((result.entries, result.millimeters), ((), 0))
        report = reconcile(warehouse, empty_day)
        self.assertEqual(report.partitions, ())
        self.assertTrue(report.matches)

    def test_bad_query(self):
        warehouse = self.warehouse()
        for day in ('2026-02-30', '20260218', '2026-2-18', None):
            with self.subTest(day=day):
                with self.assertRaises(ValueError):
                    balance(warehouse, day)
                with self.assertRaises(ValueError):
                    reconcile(warehouse, day)
        for district in ('', ' A', 23):
            with self.subTest(district=district), self.assertRaises(ValueError):
                balance(warehouse, DAY, district)
        self.assertIsNone(receipt(warehouse, 999))
        self.assertIsNone(receipt(warehouse, 1 << 100))
        with self.assertRaises(ValueError):
            receipt(warehouse, True)

    def test_cross_day_signed_and_large_exact_values(self):
        warehouse = self.warehouse()
        second_day = '2026-02-19'
        large = 1 << 80
        for issue, revision, day, district, raw, unit in (
            ('SIGNED', 1, DAY, 'A', -1000, 'um'),
            ('LARGE', 20, second_day, 'B', large, 'cm'),
            ('ZERO', 21, second_day, 'B', 0, 'mm'),
        ):
            record(self.source, 'P', revision, day, district, raw, unit, issue)
            freeze(self.source, self.register, issue, 'P', revision)
        first = self.accept_issue(warehouse, 'SIGNED')
        middle = self.accept_issue(warehouse, 'LARGE')
        self.assertEqual(balance(warehouse, second_day).millimeters, 10 * large)
        zero = self.accept_issue(warehouse, 'ZERO')
        self.source.close()
        warehouse.close()
        warehouse = self.warehouse()
        self.assertEqual(ledger_rows(balance(warehouse, DAY)),
                         [['target', 'A', -1, 1], ['reversal', 'A', 1, 1]])
        self.assertEqual(ledger_rows(balance(warehouse, second_day)),
                         [['target', 'B', 10 * large, 20], ['reversal', 'B', -10 * large, 20],
                          ['target', 'B', 0, 21]])
        self.assertEqual(receipt(warehouse, middle.receipt_id).reversal_evidence.original_receipt, first)
        self.assertEqual(receipt(warehouse, zero.receipt_id).reversal_evidence.original_receipt, middle)
        self.assertEqual(warehouse.read_effective_entry('P').revision, 21)
        for day in (DAY, second_day):
            self.assertEqual(balance(warehouse, day).millimeters, 0)
            self.assertTrue(reconcile(warehouse, day).matches)
