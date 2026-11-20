from lake.audit.balances import balance
from lake.audit.receipt_view import receipt
from lake.delivery.dispatch import send
from lake.delivery.retry import retry_pending
from lake.posting.accept import accept
from tests import DAY, LakeCase, drop_after_commit, emit_scenario


class DeliveryTests(LakeCase):
    def test_lost_reply(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        send(self.queue, warehouse, 'R1')
        self.source.close()
        outcome = send(self.queue, warehouse, 'R2', transport=drop_after_commit)
        self.assertEqual((outcome.status, outcome.receipt_id), ('pending', None))
        original = warehouse.find_issue_receipt('R2')
        self.assertEqual(original.release.target.millimeters, 11)
        self.assertEqual(self.queue.get_shipment('review', 'R2').status, 'pending')
        self.assertEqual([a.result for a in self.queue.attempts.list_for_shipment('review', 'R2')],
                         [outcome])
        self.queue.close()
        queue = self.open_queue()
        self.assertEqual([item.issue_id for item in queue.list_retryable('review')], ['R2', 'R3'])
        send(queue, warehouse, 'R3')
        before = balance(warehouse, DAY)
        historical_view = receipt(warehouse, original.receipt_id)
        warehouse.close()
        warehouse = self.warehouse()
        retried = retry_pending(queue, warehouse)
        self.assertEqual(len(retried), 1)
        issue, recovered = retried[0]
        actual = [issue, recovered.status, recovered.receipt_id]
        self.assertEqual(actual, ['R2', 'acknowledged', original.receipt_id])
        self.assertEqual(warehouse.find_issue_receipt('R2'), original)
        self.assertEqual(receipt(warehouse, recovered.receipt_id), historical_view)
        self.assertEqual(queue.get_shipment('review', 'R2').receipt_id, original.receipt_id)
        self.assertEqual([a.result for a in queue.attempts.list_for_shipment('review', 'R2')],
                         [outcome, recovered])
        self.assertEqual(balance(warehouse, DAY), before)
        downstream = [len(before.entries), before.millimeters]
        self.assertEqual(downstream, [5, 13])
        emit_scenario('v5', ['R1', 'commit R2 without reply', 'R3', 'reopen', 'retry R2'],
                      ['R2', 'acknowledged', 2], actual, [5, 13], downstream,
                      ['lake/delivery/dispatch.py', 'lake/delivery/retry.py', 'lake/posting/eligibility.py'],
                      'R2 is the second original posting; repeating it adds no journal rows')

    def test_stale_unposted(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        self.source.close()
        send(self.queue, warehouse, 'R1')
        send(self.queue, warehouse, 'R3')
        before = balance(warehouse, DAY)
        warehouse.close()
        warehouse = self.warehouse()
        self.assertIsNone(warehouse.find_issue_receipt('R2'))
        outcome = send(self.queue, warehouse, 'R2')
        self.assertEqual((outcome.status, outcome.receipt_id), ('stale', None))
        queued = self.queue.get_shipment('review', 'R2')
        self.assertEqual((queued.status, queued.receipt_id), ('stale', None))
        self.assertEqual([attempt.result.status for attempt in
                          self.queue.attempts.list_for_shipment('review', 'R2')], ['stale'])
        self.assertEqual(retry_pending(self.queue, warehouse), ())
        self.assertIsNone(warehouse.find_revision_receipt('P-17', 2))
        self.assertIsNone(warehouse.find_issue_receipt('R2'))
        self.assertEqual(balance(warehouse, DAY), before)
        downstream = [warehouse.read_effective_entry('P-17').revision, len(before.entries),
                      before.millimeters]
        self.assertEqual(downstream, [3, 3, 13])
        emit_scenario('v6', ['R1', 'R3', 'reopen', 'R2'], ['stale', None],
                      [outcome.status, outcome.receipt_id], [3, 3, 13], downstream,
                      ['lake/posting/eligibility.py', 'lake/delivery/results.py', 'lake/delivery/queue.py'],
                      'R2 is older than the active R3 and has no historical posting')

    def test_unfinished_attempt_remains_unknown_when_retry_succeeds(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        send(self.queue, warehouse, 'R1')
        staged = self.queue.get_shipment('review', 'R2')
        interrupted = self.queue.begin_attempt('review', 'R2')
        first_receipt = accept(warehouse, staged.wire)
        self.source.close()
        self.queue.close()
        warehouse.close()
        queue = self.open_queue()
        warehouse = self.warehouse()
        self.assertEqual(queue.attempts.list_for_shipment('review', 'R2'), (interrupted,))
        self.assertEqual(queue.get_shipment('review', 'R2').status, 'pending')
        send(queue, warehouse, 'R3')
        ((issue, replay),) = retry_pending(queue, warehouse)
        self.assertEqual((issue, replay.status, replay.receipt_id),
                         ('R2', 'acknowledged', first_receipt.receipt_id))
        earlier, later = queue.attempts.list_for_shipment('review', 'R2')
        self.assertIsNone(earlier.result)
        self.assertGreater(later.attempt_id, earlier.attempt_id)
        self.assertEqual(later.result, replay)
        self.assertEqual(len(balance(warehouse, DAY).entries), 5)

    def test_late_unknown_reply_preserves_confirmed_receipt(self):
        self.stage_history('review')
        warehouse = self.warehouse()
        send(self.queue, warehouse, 'R1')

        def deliver_out_of_order(target, wire):
            accept(target, wire)
            later = send(self.queue, target, 'R2')
            self.assertEqual(later.status, 'acknowledged')
            return None

        older_reply = send(self.queue, warehouse, 'R2', transport=deliver_out_of_order)
        self.assertEqual(older_reply.status, 'pending')
        self.assertEqual(self.queue.get_shipment('review', 'R2').status, 'acknowledged')
        first, second = self.queue.attempts.list_for_shipment('review', 'R2')
        self.assertEqual((first.result.status, second.result.status), ('pending', 'acknowledged'))
        self.assertEqual(second.result.receipt_id, warehouse.find_issue_receipt('R2').receipt_id)
        self.assertEqual(len(balance(warehouse, DAY).entries), 3)

    def test_unrelated_success_reply_is_not_recorded_as_acknowledged(self):
        self.stage_history('review', 'other')
        warehouse = self.warehouse()
        other = self.warehouse('other')
        unrelated = accept(other, self.queue.get_shipment('other', 'R1').wire)

        def reply_from_other_warehouse(target, wire):
            accept(target, wire)
            return unrelated

        response = send(self.queue, warehouse, 'R1', transport=reply_from_other_warehouse)
        self.assertEqual((response.status, response.receipt_id), ('pending', None))
        self.assertEqual(self.queue.get_shipment('review', 'R1').status, 'pending')
        recovered = send(self.queue, warehouse, 'R1')
        self.assertEqual((recovered.status, recovered.receipt_id), ('acknowledged', 1))
        self.assertEqual(len(balance(warehouse, DAY).entries), 1)
