import json
import sqlite3
from contextlib import closing
from pathlib import Path

from lake.audit.balances import balance
from lake.delivery.dispatch import (
    begin_managed,
    get_managed,
    record_managed_reply,
)
from lake.delivery.queue import ShipmentQueue
from lake.posting.accept import accept_managed, settle_managed
from lake.posting.eligibility import PostingConflict
from lake.posting.store import JournalStore
from tests import DAY, LakeCase


def _block_commits(warehouse):
    def authorize(action, operation, *_):
        if action == sqlite3.SQLITE_TRANSACTION and operation == 'COMMIT':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    warehouse._db.set_authorizer(authorize)


class ManagedPacketTests(LakeCase):
    def test_begin_requires_staged_release_and_gives_distinct_identities(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        with self.assertRaises(LookupError):
            begin_managed(self.queue, warehouse, 'R9')
        first = begin_managed(self.queue, warehouse, 'R2')
        second = begin_managed(self.queue, warehouse, 'R2')
        self.assertEqual(set(first), {'attempt_id', 'warehouse_id', 'issue_id', 'wire'})
        self.assertTrue(all(type(value) is str and value for value in first.values()))
        self.assertNotEqual(first['attempt_id'], second['attempt_id'])
        self.assertEqual(first['wire'], second['wire'])
        self.assertEqual((first['warehouse_id'], first['issue_id']), ('review', 'R2'))
        self.assertEqual(first['wire'], self.queue.get_shipment('review', 'R2').wire)

    def test_get_and_reopen_preserve_binding_and_original_characters(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        packet = begin_managed(self.queue, warehouse, 'R2')
        self.assertEqual(get_managed(self.queue, packet['attempt_id']), (packet, None))
        self.queue.close()
        queue = self.open_queue()
        reloaded, reply = get_managed(queue, packet['attempt_id'])
        self.assertEqual(reloaded, packet)
        self.assertIsNone(reply)
        with self.assertRaises(LookupError):
            get_managed(queue, 'deadbeef')
        with self.assertRaises(ValueError):
            get_managed(queue, '   ')

    def test_wire_is_original_v1_frozen_text(self):
        self.stage_history('review')
        packet = begin_managed(self.queue, self.warehouse(), 'R2')
        envelope = json.loads(packet['wire'])
        self.assertEqual(envelope['version'], 1)
        self.assertEqual(envelope['issue_id'], 'R2')
        self.assertEqual(packet['wire'], self.register.get_frozen('R2'))


class ManagedAcceptanceTests(LakeCase):
    def test_accept_posts_and_replays_same_terminal(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        self.accept_issue(warehouse, 'R1')
        packet = begin_managed(self.queue, warehouse, 'R2')
        reply = accept_managed(warehouse, packet)
        self.assertEqual(reply, {'packet': packet, 'status': 'acknowledged', 'receipt_id': 2})
        # the receipt and the attempt terminal state committed together
        self.assertEqual(accept_managed(warehouse, packet), reply)
        row = warehouse.managed_outcomes.get(packet['attempt_id'])
        self.assertEqual((row['status'], row['receipt_id']), ('acknowledged', 2))
        self.assertEqual(balance(warehouse, DAY).entries[1].millimeters, -7)

    def test_second_attempt_gets_actual_original_receipt_idempotently(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        self.accept_issue(warehouse, 'R1')
        first = begin_managed(self.queue, warehouse, 'R2')
        second = begin_managed(self.queue, warehouse, 'R2')
        first_reply = accept_managed(warehouse, first)
        second_reply = accept_managed(warehouse, second)
        self.assertEqual(first_reply['receipt_id'], second_reply['receipt_id'])
        self.assertEqual(second_reply['status'], 'acknowledged')
        # only one R2 posting and its two ledger rows exist
        self.assertEqual(len(balance(warehouse, DAY).entries), 3)
        self.assertEqual(second_reply['packet'], second)

    def test_settle_before_accept_closes_attempt_without_touching_ledger(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        packet = begin_managed(self.queue, warehouse, 'R2')
        before = balance(warehouse, DAY)
        reply = settle_managed(warehouse, packet)
        self.assertEqual(reply, {'packet': packet, 'status': 'closed', 'receipt_id': None})
        self.assertEqual(balance(warehouse, DAY), before)
        self.assertIsNone(warehouse.find_issue_receipt('R2'))
        # the late original packet of this attempt can never post through the managed entry
        again = accept_managed(warehouse, packet)
        self.assertEqual(again, reply)
        self.assertEqual(balance(warehouse, DAY), before)
        # settlement stays terminal on repeated calls, even after reopen
        warehouse.close()
        warehouse = self.warehouse()
        self.assertEqual(settle_managed(warehouse, packet), reply)

    def test_new_attempt_still_delivers_original_after_a_settlement(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        self.accept_issue(warehouse, 'R1')
        closed = begin_managed(self.queue, warehouse, 'R2')
        settle_managed(warehouse, closed)
        fresh = begin_managed(self.queue, warehouse, 'R2')
        reply = accept_managed(warehouse, fresh)
        self.assertEqual((reply['status'], reply['receipt_id']), ('acknowledged', 2))
        self.assertEqual([row.millimeters for row in balance(warehouse, DAY).entries], [7, -7, 11])

    def test_settle_after_accept_returns_the_actual_receipt(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        packet = begin_managed(self.queue, warehouse, 'R2')
        acknowledged = accept_managed(warehouse, packet)
        self.assertEqual(settle_managed(warehouse, packet), acknowledged)
        self.assertEqual(accept_managed(warehouse, packet), acknowledged)

    def test_accept_replay_even_when_another_attempt_took_the_receipt(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        self.accept_issue(warehouse, 'R1')
        first = begin_managed(self.queue, warehouse, 'R2')
        second = begin_managed(self.queue, warehouse, 'R2')
        accept_managed(warehouse, second)
        first_reply = accept_managed(warehouse, first)
        self.assertEqual(first_reply['receipt_id'], 2)
        self.assertEqual(first_reply['packet'], first)

    def test_closed_packet_never_enters_v1_posting_through_the_attempt(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        packet = begin_managed(self.queue, warehouse, 'R2')
        settle_managed(warehouse, packet)
        before = balance(warehouse, DAY)
        # the managed attempt stays closed; replaying its packet never creates a receipt
        self.assertEqual(accept_managed(warehouse, packet)['status'], 'closed')
        self.assertIsNone(warehouse.find_issue_receipt('R2'))
        self.assertEqual(balance(warehouse, DAY), before)


class ManagedBindingConflictTests(LakeCase):
    def _two_warehouses(self):
        self.stage_history('review', 'other')
        return self.warehouse('review'), self.warehouse('other')

    def test_first_arrival_rebound_to_another_legal_release_is_rejected(self):
        review, _other = self._two_warehouses()
        packet = begin_managed(self.queue, review, 'R2')
        r3_wire = self.queue.get_shipment('review', 'R3').wire
        # the same R2 attempt identity arrives carrying the legal but different R3 frozen copy
        swapped = {**packet, 'wire': r3_wire}
        before = balance(review, DAY)
        with self.assertRaises(PostingConflict):
            accept_managed(review, swapped)
        with self.assertRaises(PostingConflict):
            settle_managed(review, swapped)
        self.assertEqual(balance(review, DAY), before)
        self.assertIsNone(review.managed_outcomes.get(packet['attempt_id']))
        # the untouched original packet still processes normally
        reply = accept_managed(review, packet)
        self.assertEqual(reply['receipt_id'], 1)

    def test_target_warehouse_swap_is_rejected(self):
        review, other = self._two_warehouses()
        packet = begin_managed(self.queue, review, 'R2')
        before = balance(other, DAY)
        with self.assertRaises(PostingConflict):
            accept_managed(other, packet)
        with self.assertRaises(PostingConflict):
            settle_managed(other, packet)
        self.assertEqual(balance(other, DAY), before)
        self.assertIsNone(other.managed_outcomes.get(packet['attempt_id']))

    def test_issue_rebinding_and_invalid_wire_are_rejected(self):
        review, _other = self._two_warehouses()
        packet = begin_managed(self.queue, review, 'R2')
        for bad in (
            {**packet, 'issue_id': 'R3'},
            {**packet, 'wire': 'not-json'},
            {**packet, 'attempt_id': ''},
            {'only': 'two', 'fields': 4},
            None,
        ):
            with self.subTest(packet=bad), self.assertRaises(PostingConflict):
                accept_managed(review, bad)
        self.assertIsNone(review.managed_outcomes.get(packet['attempt_id']))


class ManagedCommitFailureTests(LakeCase):
    def test_failed_accept_rolls_back_ledger_and_terminal_then_retries(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        self.accept_issue(warehouse, 'R1')
        packet = begin_managed(self.queue, warehouse, 'R2')
        before = balance(warehouse, DAY)
        _block_commits(warehouse)
        from lake.posting.accept import PostingCommitError
        with self.assertRaises(PostingCommitError):
            accept_managed(warehouse, packet)
        warehouse._db.set_authorizer(None)
        self.assertEqual(balance(warehouse, DAY), before)
        self.assertIsNone(warehouse.managed_outcomes.get(packet['attempt_id']))
        self.assertIsNone(warehouse.find_issue_receipt('R2'))
        # the original binding retries successfully
        reply = accept_managed(warehouse, packet)
        self.assertEqual(reply['receipt_id'], 2)
        self.assertEqual([r.millimeters for r in balance(warehouse, DAY).entries], [7, -7, 11])

    def test_failed_settle_rolls_back_and_original_can_still_accept(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        packet = begin_managed(self.queue, warehouse, 'R2')
        _block_commits(warehouse)
        from lake.posting.accept import PostingCommitError
        with self.assertRaises(PostingCommitError):
            settle_managed(warehouse, packet)
        warehouse._db.set_authorizer(None)
        self.assertIsNone(warehouse.managed_outcomes.get(packet['attempt_id']))
        reply = accept_managed(warehouse, packet)
        self.assertEqual((reply['status'], reply['receipt_id']), ('acknowledged', 1))


class ManagedReplyTests(LakeCase):
    def test_reply_round_trip_and_unknown_attempt(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        packet = begin_managed(self.queue, warehouse, 'R2')
        attempt_id = packet['attempt_id']
        reply = accept_managed(warehouse, packet)
        saved = record_managed_reply(self.queue, attempt_id, reply)
        self.assertEqual(saved, reply)
        self.assertEqual(get_managed(self.queue, attempt_id), (packet, reply))

    def test_duplicate_reply_returns_original_and_none_keeps_terminal(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        packet = begin_managed(self.queue, warehouse, 'R2')
        reply = accept_managed(warehouse, packet)
        record_managed_reply(self.queue, packet['attempt_id'], reply)
        self.assertEqual(record_managed_reply(self.queue, packet['attempt_id'], reply), reply)
        self.assertEqual(record_managed_reply(self.queue, packet['attempt_id'], None), reply)
        # before any terminal, None means still unknown
        other = begin_managed(self.queue, warehouse, 'R3')
        self.assertIsNone(record_managed_reply(self.queue, other['attempt_id'], None))
        self.assertEqual(get_managed(self.queue, other['attempt_id']), (other, None))
        with self.assertRaises(LookupError):
            record_managed_reply(self.queue, 'unknown', None)

    def test_mismatched_or_contradictory_reply_is_rejected_and_kept(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        alpha = begin_managed(self.queue, warehouse, 'R2')
        beta = begin_managed(self.queue, warehouse, 'R2')
        alpha_reply = accept_managed(warehouse, alpha)
        beta_reply = accept_managed(warehouse, beta)
        record_managed_reply(self.queue, alpha['attempt_id'], alpha_reply)
        # beta's real receipt handed to alpha carries a different packet binding
        with self.assertRaises(ValueError):
            record_managed_reply(self.queue, alpha['attempt_id'], beta_reply)
        self.assertEqual(get_managed(self.queue, alpha['attempt_id']), (alpha, alpha_reply))
        # malformed shapes and contradicting statuses
        for bad in (
            {'packet': alpha, 'status': 'pending', 'receipt_id': 2},
            {'packet': alpha, 'status': 'acknowledged', 'receipt_id': None},
            {'packet': alpha, 'status': 'closed', 'receipt_id': 2},
            {'packet': beta, 'status': 'acknowledged', 'receipt_id': 2},
            {'status': 'acknowledged', 'receipt_id': 2},
        ):
            with self.subTest(reply=bad), self.assertRaises(ValueError):
                record_managed_reply(self.queue, alpha['attempt_id'], bad)
        self.assertEqual(get_managed(self.queue, alpha['attempt_id']), (alpha, alpha_reply))

    def test_late_unknown_result_does_not_overwrite_terminal(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        packet = begin_managed(self.queue, warehouse, 'R2')
        reply = accept_managed(warehouse, packet)
        record_managed_reply(self.queue, packet['attempt_id'], reply)
        self.assertEqual(record_managed_reply(self.queue, packet['attempt_id'], None), reply)
        self.assertEqual(get_managed(self.queue, packet['attempt_id']), (packet, reply))

    def test_contradictory_status_is_refused_and_original_terminal_kept(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        packet = begin_managed(self.queue, warehouse, 'R2')
        closed = settle_managed(warehouse, packet)
        record_managed_reply(self.queue, packet['attempt_id'], closed)
        contradiction = {'packet': packet, 'status': 'acknowledged', 'receipt_id': 1}
        with self.assertRaises(ValueError):
            record_managed_reply(self.queue, packet['attempt_id'], contradiction)
        self.assertEqual(get_managed(self.queue, packet['attempt_id']), (packet, closed))
        # malformed reply shapes never reach storage
        for bad in ((), 'x', 5, {}, {'packet': packet, 'status': 'closed'}):
            with self.subTest(reply=bad), self.assertRaises(ValueError):
                record_managed_reply(self.queue, packet['attempt_id'], bad)
        self.assertEqual(get_managed(self.queue, packet['attempt_id']), (packet, closed))


class ManagedReopenTests(LakeCase):
    def test_dropped_reply_recovered_after_both_libraries_reopen(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        packet = begin_managed(self.queue, warehouse, 'R2')
        # warehouse commits; distribution never records the reply, then both close
        accept_managed(warehouse, packet)
        self.source.close()
        self.queue.close()
        warehouse.close()
        with (
            closing(ShipmentQueue(self.queue_path)) as queue,
            closing(JournalStore(self.root / 'review.db', 'review')) as reopened,
        ):
            stored, reply = get_managed(queue, packet['attempt_id'])
            self.assertEqual(stored, packet)
            self.assertIsNone(reply)
            recovered = settle_managed(reopened, packet)
            self.assertEqual((recovered['status'], recovered['receipt_id']),
                             ('acknowledged', 1))
            self.assertEqual(record_managed_reply(queue, packet['attempt_id'], recovered),
                             recovered)
            self.assertEqual(get_managed(queue, packet['attempt_id']), (packet, recovered))
            self.assertEqual(accept_managed(reopened, packet), recovered)

    def test_closed_terminal_survives_reopen(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        packet = begin_managed(self.queue, warehouse, 'R2')
        closed = settle_managed(warehouse, packet)
        warehouse.close()
        warehouse = self.warehouse()
        self.assertEqual(settle_managed(warehouse, packet), closed)
        self.assertEqual(accept_managed(warehouse, packet), closed)

    def test_same_issue_from_distinct_queues_gets_distinct_attempt_identities(self):
        self.stage_history('review')
        second_path = self.root / 'delivery-2.db'
        warehouse = self.warehouse()
        with closing(ShipmentQueue(second_path)) as other_queue:
            other_queue.enqueue('review', 'R2', self.register.get_frozen('R2'))
            first = begin_managed(self.queue, warehouse, 'R2')
            second = begin_managed(other_queue, warehouse, 'R2')
            self.assertNotEqual(first['attempt_id'], second['attempt_id'])
            self.assertEqual(first['wire'], second['wire'])
