from dataclasses import dataclass

from lake.posting.entries import LedgerEntry
from lake.posting.receipts import Receipt


@dataclass(frozen=True)
class ReversalEvidence:
    original_entry: LedgerEntry
    original_receipt: Receipt


def follow_reversal(warehouse, reversal: LedgerEntry) -> ReversalEvidence:
    if (reversal.kind != 'reversal' or reversal.reverses_entry_id is None
            or reversal.reverses_receipt_id is None):
        raise ValueError('expected a reversal with original journal references')
    original = warehouse.get_entry(reversal.reverses_entry_id)
    receipt = warehouse.get_receipt(reversal.reverses_receipt_id)
    if original is None or receipt is None:
        raise ValueError('reversal references missing journal evidence')
    if (original.kind != 'target' or receipt.target_entry_id != original.entry_id
            or original.receipt_id != receipt.receipt_id):
        raise ValueError('reversal references an inconsistent original receipt')
    target = receipt.release.target_evidence
    if (original.observation_id != target.observation_id or original.revision != target.revision
            or original.day != target.day or original.district != target.district
            or original.millimeters != target.millimeters):
        raise ValueError('original entry does not match its frozen target')
    if (reversal.observation_id != original.observation_id or reversal.revision != original.revision
            or reversal.day != original.day or reversal.district != original.district
            or reversal.millimeters != -original.millimeters):
        raise ValueError('reversal does not offset its original entry')
    return ReversalEvidence(original, receipt)
