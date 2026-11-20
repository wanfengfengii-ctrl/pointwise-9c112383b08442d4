from dataclasses import dataclass

from lake.obs.dimensions import require_text
from lake.obs.revisions import ObservationRevision, validate_revision


@dataclass(frozen=True)
class FrozenRelease:
    issue_id: str
    target: ObservationRevision

    @property
    def target_evidence(self) -> ObservationRevision:
        return self.target


def make_frozen_release(issue_id: str, revision: ObservationRevision) -> FrozenRelease:
    frozen = FrozenRelease(issue_id, revision)
    verify_target_evidence(frozen)
    return frozen


def verify_target_evidence(frozen: FrozenRelease) -> ObservationRevision:
    if type(frozen) is not FrozenRelease:
        raise ValueError('expected a frozen release')
    require_text(frozen.issue_id, 'issue_id')
    target = frozen.target_evidence
    validate_revision(target)
    return target
