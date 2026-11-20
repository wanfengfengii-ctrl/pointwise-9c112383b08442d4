from dataclasses import dataclass

from lake.posting.entries import LedgerEntry
from lake.posting.receipts import Receipt
from lake.release.evidence import FrozenRelease


class PostingConflict(ValueError):
    pass


class StaleRevision(ValueError):
    pass


@dataclass(frozen=True)
class AcceptanceDecision:
    replay: Receipt | None = None
    replaces: LedgerEntry | None = None


def decide_acceptance(frozen: FrozenRelease, issued, historical, current):
    target = frozen.target_evidence
    if issued is not None and issued.release.target_evidence != target:
        raise PostingConflict('issue identity is already bound to different evidence')
    if historical is not None and historical.release.target_evidence != target:
        raise PostingConflict('revision identity is already posted with different evidence')
    replay = issued if issued is not None else historical
    if replay is not None:
        return AcceptanceDecision(replay=replay)
    if current is not None and target.revision <= current.revision:
        raise StaleRevision('this unapplied revision is older than the effective revision')
    return AcceptanceDecision(replaces=current)
