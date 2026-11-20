from dataclasses import replace

from lake.obs.dimensions import require_text
from lake.obs.units import format_integer, parse_integer
from lake.posting.eligibility import PostingConflict
from lake.posting.entries import LedgerEntry
from lake.posting.outcomes import ManagedOutcomes
from lake.posting.receipts import make_receipt
from lake.release.envelope import decode_release, encode_release
from lake.sqlite import open_database, transaction

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS warehouse (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    warehouse_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS postings (
    receipt_id INTEGER PRIMARY KEY,
    observation_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    wire TEXT NOT NULL,
    UNIQUE (observation_id, revision)
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL REFERENCES postings (receipt_id),
    kind TEXT NOT NULL CHECK (kind IN ('target', 'reversal')),
    observation_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    day TEXT NOT NULL,
    district TEXT NOT NULL,
    millimeters TEXT NOT NULL,
    reverses_entry_id INTEGER UNIQUE REFERENCES ledger_entries (entry_id),
    reverses_receipt_id INTEGER REFERENCES postings (receipt_id),
    UNIQUE (receipt_id, kind),
    CHECK ((kind = 'reversal' AND reverses_entry_id IS NOT NULL AND reverses_receipt_id IS NOT NULL)
        OR (kind = 'target' AND reverses_entry_id IS NULL AND reverses_receipt_id IS NULL))
);
CREATE INDEX IF NOT EXISTS ledger_day_order ON ledger_entries (day, entry_id);
CREATE INDEX IF NOT EXISTS ledger_district_order ON ledger_entries (day, district, entry_id);
CREATE TABLE IF NOT EXISTS effective (
    observation_id TEXT PRIMARY KEY NOT NULL,
    entry_id INTEGER NOT NULL UNIQUE REFERENCES ledger_entries (entry_id)
);
CREATE TABLE IF NOT EXISTS issue_receipts (
    issue_id TEXT PRIMARY KEY NOT NULL,
    receipt_id INTEGER NOT NULL REFERENCES postings (receipt_id)
);
CREATE TABLE IF NOT EXISTS managed_outcomes (
    attempt_id TEXT PRIMARY KEY NOT NULL,
    warehouse_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    wire TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('acknowledged', 'closed')),
    receipt_id INTEGER REFERENCES postings (receipt_id),
    CHECK ((status = 'acknowledged' AND receipt_id IS NOT NULL)
        OR (status = 'closed' AND receipt_id IS NULL))
);
'''

_RECEIPT_QUERY = '''
    SELECT posting.receipt_id, posting.wire,
           target.entry_id AS target_id, reversal.entry_id AS reversal_id
    FROM postings AS posting
    JOIN ledger_entries AS target ON target.receipt_id = posting.receipt_id AND target.kind = 'target'
    LEFT JOIN ledger_entries AS reversal
        ON reversal.receipt_id = posting.receipt_id AND reversal.kind = 'reversal'
'''


def _entry(row):
    if row is None:
        return None
    return LedgerEntry(
        row['kind'], row['observation_id'], parse_integer(row['revision']), row['day'],
        row['district'], parse_integer(row['millimeters']), row['entry_id'], row['receipt_id'],
        row['reverses_entry_id'], row['reverses_receipt_id'],
    )


class JournalStore:
    def __init__(self, path, warehouse_id: str, timeout=5.0):
        self._warehouse_id = require_text(warehouse_id, 'warehouse_id')
        self._db = open_database(path, timeout)
        try:
            self._db.executescript(_SCHEMA)
            self.managed_outcomes = ManagedOutcomes(self._db)
            with self.write_transaction():
                self._db.execute('''
                    INSERT INTO warehouse (slot, warehouse_id) VALUES (1, ?)
                    ON CONFLICT (slot) DO NOTHING
                ''', (self.warehouse_id,))
                row = self._db.execute('SELECT warehouse_id FROM warehouse WHERE slot = 1').fetchone()
                if row['warehouse_id'] != self.warehouse_id:
                    raise ValueError('path belongs to a different warehouse')
        except BaseException:
            self.close()
            raise

    @property
    def warehouse_id(self):
        return self._warehouse_id

    def close(self):
        self._db.close()

    def write_transaction(self):
        return transaction(self._db, write=True)

    def read_snapshot(self):
        return transaction(self._db, write=False)

    def _find_receipt(self, condition, parameters):
        row = self._db.execute(_RECEIPT_QUERY + condition, parameters).fetchone()
        if row is None:
            return None
        return make_receipt(
            self.warehouse_id, row['receipt_id'], decode_release(row['wire']),
            row['target_id'], row['reversal_id'],
        )

    def find_issue_receipt(self, issue_id):
        return self._find_receipt('''
            WHERE posting.receipt_id = (SELECT receipt_id FROM issue_receipts WHERE issue_id = ?)
        ''', (issue_id,))

    def find_revision_receipt(self, observation_id, revision):
        return self._find_receipt(' WHERE posting.observation_id = ? AND posting.revision = ?',
                                  (observation_id, format_integer(revision)))

    def read_effective_entry(self, observation_id):
        return _entry(self._db.execute('''
            SELECT * FROM ledger_entries
            WHERE entry_id = (SELECT entry_id FROM effective WHERE observation_id = ?)
        ''', (observation_id,)).fetchone())

    def _insert_entry(self, receipt_id, entry):
        cursor = self._db.execute('''
            INSERT INTO ledger_entries (receipt_id, kind, observation_id, revision, day, district,
                                        millimeters, reverses_entry_id, reverses_receipt_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            receipt_id, entry.kind, entry.observation_id, format_integer(entry.revision), entry.day,
            entry.district, format_integer(entry.millimeters), entry.reverses_entry_id,
            entry.reverses_receipt_id,
        ))
        return replace(entry, receipt_id=receipt_id, entry_id=cursor.lastrowid)

    def append_posting(self, frozen, target, reversal=None):
        if not self._db.in_transaction:
            raise RuntimeError('append_posting requires a write transaction')
        cursor = self._db.execute('''
            INSERT INTO postings (observation_id, revision, wire) VALUES (?, ?, ?)
        ''', (target.observation_id, format_integer(target.revision), encode_release(frozen)))
        receipt_id = cursor.lastrowid
        reversal_id = None
        if reversal is not None:
            reversal_id = self._insert_entry(receipt_id, reversal).entry_id
        posted = self._insert_entry(receipt_id, target)
        self._db.execute('''
            INSERT INTO effective (observation_id, entry_id) VALUES (?, ?)
            ON CONFLICT (observation_id) DO UPDATE SET entry_id = excluded.entry_id
        ''', (target.observation_id, posted.entry_id))
        return make_receipt(self.warehouse_id, receipt_id, frozen, posted.entry_id, reversal_id)

    def bind_issue_to_receipt(self, issue_id, receipt_id):
        if not self._db.in_transaction:
            raise RuntimeError('binding an issue requires a write transaction')
        self._db.execute('''
            INSERT INTO issue_receipts (issue_id, receipt_id) VALUES (?, ?)
            ON CONFLICT (issue_id) DO NOTHING
        ''', (issue_id, receipt_id))
        row = self._db.execute('SELECT receipt_id FROM issue_receipts WHERE issue_id = ?',
                               (issue_id,)).fetchone()
        if row['receipt_id'] != receipt_id:
            raise PostingConflict('issue identity is already bound to another receipt')

    def list_entries(self, day, district=None):
        if district is None:
            rows = self._db.execute('SELECT * FROM ledger_entries WHERE day = ? ORDER BY entry_id',
                                    (day,))
        else:
            rows = self._db.execute('''
                SELECT * FROM ledger_entries WHERE day = ? AND district = ? ORDER BY entry_id
            ''', (day, district))
        return tuple(_entry(row) for row in rows)

    def list_effective_entries(self, day):
        rows = self._db.execute('''
            SELECT entry.* FROM effective
            JOIN ledger_entries AS entry ON entry.entry_id = effective.entry_id
            WHERE entry.day = ? ORDER BY entry.entry_id
        ''', (day,))
        return tuple(_entry(row) for row in rows)

    def get_receipt(self, receipt_id):
        return self._find_receipt(' WHERE posting.receipt_id = ?', (receipt_id,))

    def get_entry(self, entry_id):
        return _entry(self._db.execute('SELECT * FROM ledger_entries WHERE entry_id = ?',
                                       (entry_id,)).fetchone())
