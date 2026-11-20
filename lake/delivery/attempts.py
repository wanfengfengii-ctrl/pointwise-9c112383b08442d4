from dataclasses import dataclass

from lake.delivery.results import AttemptResult


@dataclass(frozen=True)
class Attempt:
    attempt_id: int
    warehouse_id: str
    issue_id: str
    result: AttemptResult | None


def _as_attempt(row):
    result = None
    if row['status'] is not None:
        result = AttemptResult(row['status'], row['receipt_id'], row['detail'])
    return Attempt(row['attempt_id'], row['warehouse_id'], row['issue_id'], result)


class AttemptLog:
    def __init__(self, db):
        self._db = db
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id INTEGER PRIMARY KEY,
                warehouse_id TEXT NOT NULL,
                issue_id TEXT NOT NULL,
                status TEXT CHECK (status IN ('acknowledged', 'pending', 'stale', 'conflict', 'failed')),
                receipt_id INTEGER,
                detail TEXT,
                FOREIGN KEY (warehouse_id, issue_id) REFERENCES shipments (warehouse_id, issue_id),
                CHECK ((status = 'acknowledged' AND receipt_id IS NOT NULL)
                    OR ((status IS NULL OR status != 'acknowledged') AND receipt_id IS NULL))
            );
            CREATE INDEX IF NOT EXISTS attempts_by_shipment
                ON attempts (warehouse_id, issue_id, attempt_id);
        ''')

    def start(self, warehouse_id: str, issue_id: str) -> Attempt:
        cursor = self._db.execute('INSERT INTO attempts (warehouse_id, issue_id) VALUES (?, ?)',
                                  (warehouse_id, issue_id))
        return Attempt(cursor.lastrowid, warehouse_id, issue_id, None)

    def finish(self, attempt_id: int, result: AttemptResult) -> Attempt:
        self._db.execute('''
            UPDATE attempts SET status = ?, receipt_id = ?, detail = ?
            WHERE attempt_id = ? AND status IS NULL
        ''', (result.status, result.receipt_id, result.detail, attempt_id))
        row = self._db.execute('SELECT * FROM attempts WHERE attempt_id = ?', (attempt_id,)).fetchone()
        if row is None:
            raise LookupError('unknown delivery attempt')
        saved = _as_attempt(row)
        if saved.result != result:
            raise ValueError('attempt already finished with a different result')
        return saved

    def list_for_shipment(self, warehouse_id: str, issue_id: str) -> tuple[Attempt, ...]:
        rows = self._db.execute('''
            SELECT * FROM attempts WHERE warehouse_id = ? AND issue_id = ? ORDER BY attempt_id
        ''', (warehouse_id, issue_id))
        return tuple(_as_attempt(row) for row in rows)
