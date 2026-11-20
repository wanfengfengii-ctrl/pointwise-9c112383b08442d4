import json
import sqlite3
from contextlib import closing
from dataclasses import replace

from lake.audit.balances import balance
from lake.audit.receipt_view import receipt
from lake.audit.reconcile import reconcile
from lake.delivery.dispatch import send
from lake.posting.accept import PostingCommitError, accept
from lake.posting.eligibility import PostingConflict, StaleRevision
from lake.release.envelope import decode_release, encode_release
from lake.release.evidence import verify_target_evidence
from lake.release.freeze import freeze
from tests import DAY, LakeCase, drop_after_commit, emit_scenario, ledger_rows


class PostingTests(LakeCase):
    def test_sequential_change(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        first = self.accept_issue(warehouse, 'R1')
        second = self.accept_issue(warehouse, 'R2')
        view = receipt(warehouse, second.receipt_id)
        actual = ledger_rows(view)
        expected = [['reversal', 'A', -7, 1], ['target', 'B', 11, 2]]
        self.assertEqual(actual, expected)
        reversal, target = view.entries
        self.assertEqual((reversal.day, target.day), (DAY, DAY))
        self.assertEqual(reversal.reverses_entry_id, first.target_entry_id)
        self.assertEqual(reversal.reverses_receipt_id, first.receipt_id)
        self.assertEqual(view.reversal_evidence.original_receipt, first)
        self.assertEqual(view.reversal_evidence.original_receipt.release.target.provenance, 'log:1')
        downstream = [balance(warehouse, DAY, 'A').millimeters, balance(warehouse, DAY, 'B').millimeters]
        self.assertEqual(downstream, [0, 11])
        emit_scenario('v3', [['A', 7], ['B', 11]], expected, actual, [0, 11], downstream,
                      ['lake/posting/accept.py', 'lake/posting/entries.py', 'lake/audit/balances.py'],
                      'A has 7 minus 7 and B has its new target 11')

    def test_identity_conflict(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        first = self.accept_issue(warehouse, 'R1')
        self.accept_issue(warehouse, 'R3')
        frozen = first.release
        changed = replace(frozen.target, raw_value=8, millimeters=8)
        before = balance(warehouse, DAY)
        for release in (replace(frozen, target=changed), replace(frozen, issue_id='OTHER', target=changed)):
            with self.subTest(issue=release.issue_id), self.assertRaises(PostingConflict):
                accept(warehouse, encode_release(release))
        issue_conflict = self.register.get_frozen('R2')
        self.queue.enqueue('review', 'ALTERED', encode_release(replace(frozen, issue_id='ALTERED', target=changed)))
        reply = send(self.queue, warehouse, 'ALTERED')
        self.assertEqual(reply.status, 'conflict')
        self.assertEqual(self.queue.get_shipment('review', 'ALTERED').status, 'conflict')
        self.assertEqual(self.queue.attempts.list_for_shipment('review', 'ALTERED')[0].result, reply)
        with self.assertRaises(StaleRevision):
            accept(warehouse, issue_conflict)
        self.assertEqual(accept(warehouse, encode_release(frozen)), first)
        self.assertEqual(balance(warehouse, DAY), before)
        self.assertIsNone(warehouse.find_issue_receipt('ALTERED'))

    def test_same_target_new_issue(self):
        self.stage_history()
        warehouse = self.warehouse()
        first = self.accept_issue(warehouse, 'R1')
        self.accept_issue(warehouse, 'R3')
        before = balance(warehouse, DAY)
        freeze(self.source, self.register, 'R1-ALIAS', 'P-17', 1)
        self.assertEqual(self.accept_issue(warehouse, 'R1-ALIAS'), first)
        self.assertEqual(warehouse.find_issue_receipt('R1-ALIAS'), first)
        different_target = decode_release(self.register.get_frozen('R3')).target
        with self.assertRaises(PostingConflict):
            accept(warehouse, encode_release(replace(first.release, issue_id='R1-ALIAS',
                                                     target=different_target)))
        self.assertEqual(balance(warehouse, DAY), before)

    def test_skip_change(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        first = send(self.queue, warehouse, 'R1')
        self.source.close()
        third = send(self.queue, warehouse, 'R3')
        self.assertEqual((first.status, third.status), ('acknowledged', 'acknowledged'))
        view = receipt(warehouse, third.receipt_id)
        expected = [['target', 'A', 7, 1], ['reversal', 'A', -7, 1], ['target', 'A', 13, 3]]
        actual = ledger_rows(balance(warehouse, DAY))
        self.assertEqual(actual, expected, '跳版冲销依据必须来自实际入账正项')
        self.assertEqual(view.entries[0].reverses_receipt_id, first.receipt_id)
        downstream = [balance(warehouse, DAY, 'A').millimeters, balance(warehouse, DAY, 'B').millimeters]
        self.assertEqual(downstream, [13, 0])
        self.assertIsNone(warehouse.find_revision_receipt('P-17', 2))
        emit_scenario('v4', [[1, 'A', 7], [2, 'B', 11], [3, 'A', 13], ['delivered', 1, 3]],
                      expected, actual, [13, 0], downstream,
                      ['lake/release/envelope.py', 'lake/delivery/dispatch.py', 'lake/posting/accept.py'],
                      'Only revision 1 reached the warehouse before revision 3')

    def test_absolute_target_and_evidence_rejection(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        send(self.queue, warehouse, 'R1')
        self.source.close()
        wire = self.queue.get_shipment('review', 'R3').wire
        release = decode_release(wire)
        target = verify_target_evidence(release)
        self.assertEqual((target.observation_id, target.revision, target.day, target.district,
                          target.millimeters, target.raw_value, target.unit, target.provenance),
                         ('P-17', 3, DAY, 'A', 13, 13, 'mm', 'log:3'))
        self.assertEqual(encode_release(release), wire)
        changed = json.loads(wire)
        del changed['target']['provenance']
        before = balance(warehouse, DAY)
        invalid_wires = [json.dumps(changed), wire.replace('"version":1', '"version":true'),
                         wire.replace('"version":1', '"version":1,"version":1'),
                         encode_release(replace(release, target=replace(target, millimeters=91)))]
        for invalid_wire in invalid_wires:
            with self.subTest(wire=invalid_wire), self.assertRaises(PostingConflict):
                accept(warehouse, invalid_wire)
        self.assertEqual(balance(warehouse, DAY), before)
        self.assertEqual(accept(warehouse, wire).release, release)

    def test_two_histories(self):
        lagged, lost = self.warehouse('lagged'), self.warehouse('lost')
        self.stage_history('lagged', 'lost')
        self.source.close()
        first_lagged = send(self.queue, lagged, 'R1')
        send(self.queue, lost, 'R1')
        reply_lost = send(self.queue, lost, 'R2', transport=drop_after_commit)
        self.assertEqual(reply_lost.status, 'pending')
        second = lost.find_revision_receipt('P-17', 2)
        lagged_third = send(self.queue, lagged, 'R3')
        lost_third = send(self.queue, lost, 'R3')
        for warehouse, outcome, amount, district, predecessor, origin, row_count in (
            (lagged, lagged_third, -7, 'A', first_lagged.receipt_id, 'log:1', 3),
            (lost, lost_third, -11, 'B', second.receipt_id, 'log:2', 5),
        ):
            with self.subTest(warehouse=warehouse.warehouse_id):
                view = receipt(warehouse, outcome.receipt_id)
                reversal = view.entries[0]
                self.assertEqual((reversal.millimeters, reversal.district, reversal.reverses_receipt_id),
                                 (amount, district, predecessor))
                original = view.reversal_evidence.original_receipt
                self.assertEqual(original.release.target.provenance, origin)
                self.assertEqual(reversal.reverses_entry_id, original.target_entry_id)
                self.assertEqual((balance(warehouse, DAY, 'A').millimeters,
                                  balance(warehouse, DAY, 'B').millimeters), (13, 0))
                self.assertEqual(len(balance(warehouse, DAY).entries), row_count)

    def test_interleaved_write(self):
        self.stage_history()
        warehouse = self.warehouse()
        rival = self.warehouse(timeout=0)
        first = self.accept_issue(warehouse, 'R1')
        wire = self.register.get_frozen('R3')
        preview = warehouse.read_effective_entry('P-17')
        self.assertEqual(preview.receipt_id, first.receipt_id)
        second = self.accept_issue(rival, 'R2')
        third = accept(warehouse, wire)
        reversal = receipt(warehouse, third.receipt_id).entries[0]
        self.assertEqual((reversal.reverses_entry_id, reversal.reverses_receipt_id,
                          reversal.district, reversal.millimeters),
                         (second.target_entry_id, second.receipt_id, 'B', -11))
        self.assertTrue(reconcile(warehouse, DAY).matches)

    def test_competing_write_cannot_enter_the_authoritative_window(self):
        self.stage_history()
        warehouse = self.warehouse()
        rival = self.warehouse(timeout=0)
        first = self.accept_issue(warehouse, 'R1')
        second_wire = self.register.get_frozen('R2')
        competing_errors = []

        def deliver_second():
            try:
                accept(rival, second_wire)
            except PostingCommitError as error:
                competing_errors.append(error.__cause__.sqlite_errorcode)
            return 0

        warehouse._db.create_function('deliver_second', 0, deliver_second)
        warehouse._db.execute('''
            CREATE TEMP TRIGGER compete_during_append AFTER INSERT ON ledger_entries
            WHEN NEW.revision = '3' AND NEW.kind = 'target'
            BEGIN SELECT deliver_second(); END
        ''')
        third = self.accept_issue(warehouse, 'R3')
        self.assertEqual(competing_errors, [sqlite3.SQLITE_BUSY])
        self.assertEqual(receipt(warehouse, third.receipt_id).entries[0].reverses_receipt_id,
                         first.receipt_id)
        with self.assertRaises(StaleRevision):
            accept(rival, second_wire)
        self.assertEqual(len(balance(warehouse, DAY).entries), 3)

    def test_commit_failure(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        self.accept_issue(warehouse, 'R1')
        first = warehouse.find_issue_receipt('R1')
        before = balance(warehouse, DAY)
        db_path = self.root / 'review.db'
        with closing(sqlite3.connect(db_path)) as setup:
            setup.executescript('''
                CREATE TABLE required_note (note_id INTEGER PRIMARY KEY);
                CREATE TABLE posting_note (
                    note_id INTEGER REFERENCES required_note(note_id) DEFERRABLE INITIALLY DEFERRED
                );
                CREATE TRIGGER note_at_replacement AFTER UPDATE ON effective
                BEGIN INSERT INTO posting_note VALUES (1); END;
            ''')
        observed = []
        wire = self.register.get_frozen('R2')

        def see_staged_rows():
            observed.append((len(warehouse.list_entries(DAY)), warehouse.read_effective_entry('P-17').revision))
            return 0

        warehouse._db.create_function('see_staged_rows', 0, see_staged_rows)
        warehouse._db.execute('''
            CREATE TEMP TRIGGER observe_posting BEFORE INSERT ON issue_receipts
            BEGIN SELECT see_staged_rows(); END
        ''')
        with self.assertRaises(PostingCommitError) as failed:
            accept(warehouse, wire)
        self.assertIsInstance(failed.exception.__cause__, sqlite3.IntegrityError)
        self.assertEqual(observed, [(3, 2)])
        failure_reply = send(self.queue, warehouse, 'R2')
        self.assertEqual(failure_reply.status, 'failed')
        self.assertEqual(self.queue.get_shipment('review', 'R2').status, 'pending')
        warehouse.close()
        warehouse = self.warehouse()
        self.assertEqual(balance(warehouse, DAY), before)
        effective = warehouse.read_effective_entry('P-17')
        self.assertEqual((effective.revision, effective.entry_id), (1, first.target_entry_id))
        self.assertEqual(warehouse.find_issue_receipt('R1'), first)
        self.assertIsNone(warehouse.find_revision_receipt('P-17', 2))
        self.assertIsNone(warehouse.find_issue_receipt('R2'))
        actual = [len(balance(warehouse, DAY).entries), effective.revision,
                  warehouse.find_issue_receipt('R2')]
        self.assertEqual(actual, [1, 1, None])
        with closing(sqlite3.connect(db_path)) as setup:
            self.assertEqual(setup.execute('SELECT COUNT(*) FROM posting_note').fetchone()[0], 0)
            setup.execute('DROP TRIGGER note_at_replacement')
        successful = send(self.queue, warehouse, 'R2')
        self.assertEqual(successful.status, 'acknowledged')
        self.assertEqual(send(self.queue, warehouse, 'R2'), successful)
        downstream = [len(balance(warehouse, DAY).entries), balance(warehouse, DAY).millimeters]
        self.assertEqual(downstream, [3, 11])
        emit_scenario('v7', ['A7 posted', 'B11 staged', 'deferred foreign key failed'],
                      [1, 1, None], actual, [3, 11], downstream,
                      ['lake/posting/store.py', 'lake/sqlite.py', 'lake/delivery/dispatch.py'],
                      'failed commit preserves the singleton A7; one successful retry adds two rows')
