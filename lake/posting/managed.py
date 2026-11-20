import sqlite3

from lake.delivery.managed import parse_packet
from lake.posting.accept import PostingCommitError, decode_wire, post_release
from lake.posting.eligibility import PostingConflict
from lake.posting.outcomes import ManagedOutcomes  # noqa: F401


def _check_binding(warehouse, packet):
    try:
        attempt_id, packet_warehouse, issue_id, wire = parse_packet(packet)
    except ValueError as error:
        raise PostingConflict(str(error)) from error
    if packet_warehouse != warehouse.warehouse_id:
        raise PostingConflict('managed packet targets a different warehouse')
    try:
        frozen, target = decode_wire(wire)
    except PostingConflict:
        raise
    if frozen.issue_id != issue_id:
        raise PostingConflict('managed packet issue does not match its frozen release')
    return attempt_id, issue_id, wire, frozen, target


def _reply(packet, status, receipt_id):
    return {'packet': packet, 'status': status, 'receipt_id': receipt_id}


def accept_managed(warehouse, packet):
    attempt_id, issue_id, wire, frozen, target = _check_binding(warehouse, packet)
    try:
        with warehouse.write_transaction():
            existing = warehouse.managed_outcomes.get(attempt_id)
            if existing is not None:
                return _reply(packet, existing['status'], existing['receipt_id'])
            receipt = post_release(warehouse, frozen, target)
            warehouse.managed_outcomes.insert(
                attempt_id, warehouse.warehouse_id, issue_id, wire,
                'acknowledged', receipt.receipt_id)
            return _reply(packet, 'acknowledged', receipt.receipt_id)
    except sqlite3.Error as error:
        raise PostingCommitError(str(error)) from error


def settle_managed(warehouse, packet):
    attempt_id, issue_id, wire, _frozen, _target = _check_binding(warehouse, packet)
    try:
        with warehouse.write_transaction():
            existing = warehouse.managed_outcomes.get(attempt_id)
            if existing is not None:
                return _reply(packet, existing['status'], existing['receipt_id'])
            warehouse.managed_outcomes.insert(
                attempt_id, warehouse.warehouse_id, issue_id, wire, 'closed', None)
            return _reply(packet, 'closed', None)
    except sqlite3.Error as error:
        raise PostingCommitError(str(error)) from error
