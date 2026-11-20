import sqlite3

from lake.posting.eligibility import PostingConflict, decide_acceptance
from lake.posting.entries import build_reversal_entry, build_target_entry
from lake.release.envelope import decode_release
from lake.release.evidence import verify_target_evidence


class PostingCommitError(RuntimeError):
    pass


def decode_wire(wire):
    try:
        frozen = decode_release(wire)
        target = verify_target_evidence(frozen)
    except ValueError as error:
        raise PostingConflict(str(error)) from error
    return frozen, target


def post_release(warehouse, frozen, target):
    """Decide and append inside an already open write transaction."""
    decision = decide_acceptance(
        frozen,
        warehouse.find_issue_receipt(frozen.issue_id),
        warehouse.find_revision_receipt(target.observation_id, target.revision),
        warehouse.read_effective_entry(target.observation_id),
    )
    receipt = decision.replay
    if receipt is None:
        reversal = None
        if decision.replaces is not None:
            reversal = build_reversal_entry(decision.replaces)
        receipt = warehouse.append_posting(frozen, build_target_entry(frozen), reversal)
    warehouse.bind_issue_to_receipt(frozen.issue_id, receipt.receipt_id)
    return receipt


def accept(warehouse, wire):
    frozen, target = decode_wire(wire)
    try:
        with warehouse.write_transaction():
            return post_release(warehouse, frozen, target)
    except sqlite3.Error as error:
        raise PostingCommitError(str(error)) from error


def __getattr__(name):
    if name in ('accept_managed', 'settle_managed'):
        from lake.posting import managed
        return getattr(managed, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
