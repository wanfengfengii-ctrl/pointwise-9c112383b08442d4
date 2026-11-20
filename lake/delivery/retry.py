from lake.delivery.dispatch import send
from lake.delivery.queue import ShipmentQueue


def retry_pending(queue: ShipmentQueue, warehouse, transport=None):
    results = []
    for shipment in queue.list_retryable(warehouse.warehouse_id):
        result = send(queue, warehouse, shipment.issue_id, transport=transport)
        results.append((shipment.issue_id, result))
    return tuple(results)
