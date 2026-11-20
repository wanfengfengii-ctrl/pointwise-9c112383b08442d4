from collections import defaultdict
from dataclasses import dataclass

from lake.audit.balances import sum_entries
from lake.obs.dimensions import validate_day
from lake.posting.store import JournalStore


@dataclass(frozen=True)
class PartitionComparison:
    district: str
    ledger_mm: int
    effective_mm: int

    @property
    def difference_mm(self) -> int:
        return self.ledger_mm - self.effective_mm


@dataclass(frozen=True)
class ReconciliationReport:
    day: str
    partitions: tuple[PartitionComparison, ...]

    @property
    def matches(self) -> bool:
        return all(part.difference_mm == 0 for part in self.partitions)


def _partition_totals(entries):
    grouped = defaultdict(list)
    for entry in entries:
        grouped[entry.district].append(entry)
    return {district: sum_entries(rows) for district, rows in grouped.items()}


def reconcile(warehouse: JournalStore, day: str) -> ReconciliationReport:
    validate_day(day)
    with warehouse.read_snapshot():
        ledger = _partition_totals(warehouse.list_entries(day))
        effective = _partition_totals(warehouse.list_effective_entries(day))
        return ReconciliationReport(day, tuple(
            PartitionComparison(district, ledger.get(district, 0), effective.get(district, 0))
            for district in sorted(ledger.keys() | effective.keys())
        ))
