from lake.delivery.queue import ShipmentQueue
from lake.delivery.results import classify_reply
from lake.posting.accept import PostingCommitError, accept
from lake.posting.eligibility import PostingConflict, StaleRevision
from lake.release.register import ReleaseNotFound


def send(queue: ShipmentQueue, warehouse, issue_id, register=None, transport=None):
    warehouse_id = warehouse.warehouse_id
    shipment = queue.get_shipment(warehouse_id, issue_id)
    if shipment is None:
        wire = None if register is None else register.get_frozen(issue_id)
        if wire is None:
            raise ReleaseNotFound('no frozen copy is available for shipment')
        shipment = queue.enqueue(warehouse_id, issue_id, wire)
    attempt = queue.begin_attempt(warehouse_id, issue_id)
    deliver = accept if transport is None else transport
    try:
        reply = deliver(warehouse, shipment.wire)
    except (StaleRevision, PostingConflict, PostingCommitError, ConnectionError) as error:
        reply = error
    result = classify_reply(reply, warehouse_id, shipment.wire)
    queue.finish_attempt(attempt.attempt_id, result)
    return result
