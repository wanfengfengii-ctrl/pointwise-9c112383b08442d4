from dataclasses import dataclass

from lake.release.evidence import FrozenRelease


@dataclass(frozen=True)
class LedgerEntry:
    kind: str
    observation_id: str
    revision: int
    day: str
    district: str
    millimeters: int
    entry_id: int | None = None
    receipt_id: int | None = None
    reverses_entry_id: int | None = None
    reverses_receipt_id: int | None = None


def build_target_entry(frozen: FrozenRelease) -> LedgerEntry:
    target = frozen.target_evidence
    return LedgerEntry(
        'target', target.observation_id, target.revision, target.day, target.district,
        target.millimeters,
    )


def build_reversal_entry(original: LedgerEntry) -> LedgerEntry:
    if original.kind != 'target' or original.entry_id is None or original.receipt_id is None:
        raise ValueError('reversal requires a posted target entry')
    return LedgerEntry(
        'reversal', original.observation_id, original.revision, original.day, original.district,
        -original.millimeters, reverses_entry_id=original.entry_id,
        reverses_receipt_id=original.receipt_id,
    )
