from dataclasses import dataclass

from lake.posting.accept import PostingCommitError
from lake.posting.eligibility import PostingConflict, StaleRevision
from lake.posting.receipts import Receipt
from lake.release.envelope import decode_release


@dataclass(frozen=True)
class AttemptResult:
    status: str
    receipt_id: int | None = None
    detail: str | None = None

    def __post_init__(self):
        if self.status == 'acknowledged':
            if type(self.receipt_id) is not int or self.receipt_id <= 0:
                raise ValueError('an acknowledged attempt needs a receipt identity')
        elif self.status not in {'pending', 'stale', 'conflict', 'failed'} or self.receipt_id is not None:
            raise ValueError('invalid attempt result')


def classify_reply(reply, warehouse_id: str, wire: str) -> AttemptResult:
    if isinstance(reply, StaleRevision):
        return AttemptResult('stale', detail=str(reply))
    if isinstance(reply, PostingConflict):
        return AttemptResult('conflict', detail=str(reply))
    if isinstance(reply, PostingCommitError):
        return AttemptResult('failed', detail=str(reply))
    if reply is None or isinstance(reply, ConnectionError):
        return AttemptResult('pending', detail='no confirmed reply')
    if isinstance(reply, Receipt) and reply.warehouse_id == warehouse_id:
        try:
            release = decode_release(wire)
        except ValueError:
            return AttemptResult('pending', detail='reply cannot be matched to the queued evidence')
        if reply.release.target_evidence == release.target_evidence:
            return AttemptResult('acknowledged', reply.receipt_id)
    return AttemptResult('pending', detail='reply does not match the queued target and warehouse')
