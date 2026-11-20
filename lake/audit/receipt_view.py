from dataclasses import dataclass

from lake.audit.provenance import ReversalEvidence, follow_reversal
from lake.posting.entries import LedgerEntry
from lake.posting.receipts import Receipt
from lake.posting.store import JournalStore


@dataclass(frozen=True)
class ReceiptView:
    receipt: Receipt
    entries: tuple[LedgerEntry, ...]
    reversal_evidence: ReversalEvidence | None


def receipt(warehouse: JournalStore, receipt_id: int) -> ReceiptView | None:
    if type(receipt_id) is not int or receipt_id <= 0:
        raise ValueError('receipt_id must be a positive integer')
    if receipt_id > (1 << 63) - 1:
        return None
    with warehouse.read_snapshot():
        saved = warehouse.get_receipt(receipt_id)
        if saved is None:
            return None
        target = warehouse.get_entry(saved.target_entry_id)
        if saved.reversal_entry_id is None:
            return ReceiptView(saved, (target,), None)
        reversal = warehouse.get_entry(saved.reversal_entry_id)
        evidence = follow_reversal(warehouse, reversal)
        return ReceiptView(saved, (reversal, target), evidence)
