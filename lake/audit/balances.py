from dataclasses import dataclass
from typing import Iterable

from lake.obs.dimensions import validate_day, validate_district
from lake.posting.entries import LedgerEntry
from lake.posting.store import JournalStore


@dataclass(frozen=True)
class BalanceView:
    day: str
    district: str | None
    entries: tuple[LedgerEntry, ...]
    millimeters: int


def sum_entries(entries: Iterable[LedgerEntry]) -> int:
    return sum(entry.millimeters for entry in entries)


def balance(warehouse: JournalStore, day: str, district=None) -> BalanceView:
    validate_day(day)
    if district is not None:
        validate_district(district)
    with warehouse.read_snapshot():
        entries = warehouse.list_entries(day, district)
        return BalanceView(day, district, entries, sum_entries(entries))
