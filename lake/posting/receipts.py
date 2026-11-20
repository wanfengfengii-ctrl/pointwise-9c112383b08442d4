from dataclasses import dataclass

from lake.release.evidence import FrozenRelease


@dataclass(frozen=True)
class Receipt:
    warehouse_id: str
    receipt_id: int
    release: FrozenRelease
    target_entry_id: int
    reversal_entry_id: int | None


def make_receipt(warehouse_id, receipt_id, release, target_entry_id, reversal_entry_id=None):
    return Receipt(warehouse_id, receipt_id, release, target_entry_id, reversal_entry_id)
