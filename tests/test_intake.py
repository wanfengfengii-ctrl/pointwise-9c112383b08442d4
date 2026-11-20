from lake.audit.balances import balance
from lake.audit.receipt_view import receipt
from lake.delivery.dispatch import send
from lake.delivery.queue import ShipmentConflict
from lake.obs.intake import record
from lake.obs.store import RevisionConflict
from lake.release.envelope import encode_release
from lake.release.evidence import make_frozen_release
from lake.release.freeze import freeze
from lake.release.register import ReleaseConflict, ReleaseNotFound, ReleaseRegister
from lake.release.selection import RevisionNotFound
from tests import DAY, LakeCase, emit_scenario


class IntakeTests(LakeCase):
    def test_first_receipt(self):
        warehouse = self.warehouse()
        original = record(self.source, 'P-09', 1, DAY, 'A', 1, 'cm', 'row:17')
        record(self.source, 'P-09', 4, DAY, 'B', 18, 'mm', 'row:31')
        frozen = freeze(self.source, self.register, 'FIRST', 'P-09', 1)
        self.assertEqual(frozen.target_evidence, original)
        reply = send(self.queue, warehouse, 'FIRST', register=self.register)
        self.assertEqual(reply.status, 'acknowledged')
        view = receipt(warehouse, reply.receipt_id)
        target = view.receipt.release.target
        actual = [target.observation_id, target.revision, target.raw_value, target.unit,
                  target.millimeters, target.provenance]
        expected = ['P-09', 1, 1, 'cm', 10, 'row:17']
        self.assertEqual(actual, expected)
        self.assertEqual(len(view.entries), 1)
        self.assertEqual(view.entries[0].kind, 'target')
        self.assertIsNone(view.reversal_evidence)
        downstream = [balance(warehouse, DAY, 'A').millimeters, balance(warehouse, DAY, 'B').millimeters]
        self.assertEqual(downstream, [10, 0])
        emit_scenario('v1', [[1, 'A', 1, 'cm'], [4, 'B', 18, 'mm']], expected, actual,
                      [10, 0], downstream, ['lake/obs/units.py', 'lake/release/selection.py',
                                           'lake/posting/accept.py'],
                      '1 cm equals 10 mm, and the named first release excludes revision 4')

    def test_release_identity(self):
        self.stage_history()
        original = self.register.get_frozen('R1')
        self.assertEqual(encode_release(freeze(self.source, self.register, 'R1', 'P-17', 1)), original)
        with self.assertRaises(ReleaseConflict):
            freeze(self.source, self.register, 'R1', 'P-17', 2)
        self.source.close()
        register = ReleaseRegister(self.open_source())
        self.assertEqual(register.get_frozen('R1'), original)
        warehouse = self.warehouse()
        reply = send(self.queue, warehouse, 'R1', register=register)
        self.assertEqual(reply.status, 'acknowledged')
        saved = receipt(warehouse, reply.receipt_id).receipt.release.target
        self.assertEqual((saved.revision, saved.millimeters, saved.provenance), (1, 7, 'log:1'))

    def test_reopen_source_and_queue(self):
        warehouse = self.warehouse()
        user_revision = record(self.source, 'USER', 6, DAY, 'Z', 2, 'cm', 'user-sheet:9')
        release = freeze(self.source, self.register, 'USER-6', 'USER', 6)
        wire = self.register.get_frozen('USER-6')
        staged = self.queue.enqueue('review', 'USER-6', wire)
        reply = send(self.queue, warehouse, 'USER-6')
        self.assertEqual(reply.status, 'acknowledged')
        self.source.close()
        self.queue.close()
        source = self.open_source()
        register = ReleaseRegister(source)
        queue = self.open_queue()
        self.assertEqual(record(source, 'USER', 6, DAY, 'Z', 2, 'cm', 'user-sheet:9'), user_revision)
        with self.assertRaises(RevisionConflict):
            record(source, 'USER', 6, DAY, 'Z', 3, 'cm', 'user-sheet:10')
        self.assertEqual(source.get_revision('USER', 6), user_revision)
        self.assertEqual(freeze(source, register, 'USER-6', 'USER', 6), release)
        self.assertEqual(register.get_frozen('USER-6'), staged.wire)
        preserved = queue.enqueue('review', 'USER-6', wire)
        self.assertEqual((preserved.status, preserved.receipt_id), ('acknowledged', reply.receipt_id))
        altered = record(source, 'USER', 7, DAY, 'Z', 3, 'cm', 'user-sheet:10')
        different_wire = encode_release(make_frozen_release('USER-6', altered))
        with self.assertRaises(ShipmentConflict):
            queue.enqueue('review', 'USER-6', different_wire)
        self.assertEqual(queue.get_shipment('review', 'USER-6'), preserved)
        self.assertEqual([item.result for item in queue.attempts.list_for_shipment('review', 'USER-6')],
                         [reply])
        source.close()
        self.assertEqual(send(queue, warehouse, 'USER-6'), reply)
        self.assertEqual(balance(warehouse, DAY, 'Z').millimeters, 20)
        self.assertEqual(len(balance(warehouse, DAY).entries), 1)

    def test_fractional_mm(self):
        warehouse = self.warehouse()
        with self.assertRaisesRegex(ValueError, 'whole millimeters') as rejected:
            record(self.source, 'BAD', 1, DAY, 'A', 1, 'um', 'row:bad')
        with self.assertRaises(RevisionNotFound) as missing:
            freeze(self.source, self.register, 'BAD-1', 'BAD', 1)
        with self.assertRaises(ReleaseNotFound):
            send(self.queue, warehouse, 'BAD-1', register=self.register)
        downstream = [self.source.get_revision('BAD', 1), self.register.get_frozen('BAD-1'),
                      self.queue.get_shipment('review', 'BAD-1'), balance(warehouse, DAY).millimeters]
        self.assertEqual(downstream, [None, None, None, 0])
        emit_scenario('v2', [1, 'um'], ['ValueError', 'RevisionNotFound'],
                      [type(rejected.exception).__name__, type(missing.exception).__name__],
                      [None, None, None, 0], downstream, ['lake/obs/units.py', 'lake/obs/intake.py',
                                                          'lake/release/selection.py'],
                      '1 micrometer is one thousandth of a millimeter and cannot be posted')

    def test_invalid_input_does_not_reserve_identity(self):
        valid = dict(observation_id='P', revision=1, day=DAY, district='A', raw_value=5,
                     unit='mm', provenance='page:1')
        for invalid in ({'raw_value': True}, {'raw_value': 1.5}, {'unit': 'm'}, {'raw_value': None},
                        {'day': '2026-02-30'}, {'day': '20260218'}, {'district': ''},
                        {'district': ' A'}, {'provenance': None}, {'provenance': ' '},
                        {'revision': False}, {'revision': 0}, {'observation_id': ''}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                record(self.source, **(valid | invalid))
        self.assertIsNone(self.source.get_revision('P', 1))
        self.assertEqual(record(self.source, **valid).millimeters, 5)
