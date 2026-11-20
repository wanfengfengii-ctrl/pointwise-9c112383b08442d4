import sqlite3

from lake.posting.eligibility import PostingConflict, decide_acceptance
from lake.posting.entries import build_reversal_entry, build_target_entry
from lake.release.envelope import decode_release
from lake.release.evidence import verify_target_evidence


class PostingCommitError(RuntimeError):
    pass


def accept(warehouse, wire):
    try:
        frozen = decode_release(wire)
        target = verify_target_evidence(frozen)
    except ValueError as error:
        raise PostingConflict(str(error)) from error

    try:
        with warehouse.write_transaction():
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
    except sqlite3.Error as error:
        raise PostingCommitError(str(error)) from error
    return receipt
