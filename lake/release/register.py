from lake.obs.dimensions import require_text
from lake.obs.store import RevisionStore
from lake.release.envelope import decode_release, encode_release
from lake.release.evidence import FrozenRelease, verify_target_evidence
from lake.sqlite import transaction


class ReleaseConflict(ValueError):
    pass


class ReleaseNotFound(LookupError):
    pass


class ReleaseRegister:
    def __init__(self, source: RevisionStore):
        self._db = source._db
        self._db.execute('''
            CREATE TABLE IF NOT EXISTS frozen_releases (
                issue_id TEXT PRIMARY KEY NOT NULL,
                wire TEXT NOT NULL
            )
        ''')

    def save_frozen(self, frozen: FrozenRelease) -> FrozenRelease:
        verify_target_evidence(frozen)
        wire = encode_release(frozen)
        with transaction(self._db, write=True):
            self._db.execute('''
                INSERT INTO frozen_releases (issue_id, wire) VALUES (?, ?)
                ON CONFLICT (issue_id) DO NOTHING
            ''', (frozen.issue_id, wire))
            existing = self.get_frozen(frozen.issue_id)
            if existing != wire:
                raise ReleaseConflict('issue identity already has different frozen content')
            return decode_release(existing)

    def get_frozen(self, issue_id: str) -> str | None:
        require_text(issue_id, 'issue_id')
        row = self._db.execute('''
            SELECT wire FROM frozen_releases WHERE issue_id = ?
        ''', (issue_id,)).fetchone()
        return None if row is None else row['wire']
