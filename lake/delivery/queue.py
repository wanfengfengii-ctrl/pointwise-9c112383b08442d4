from dataclasses import dataclass

from lake.delivery.attempts import AttemptLog
from lake.delivery.managed import ManagedAttemptLog
from lake.delivery.results import AttemptResult
from lake.obs.dimensions import require_text
from lake.release.envelope import release_issue_id
from lake.sqlite import open_database, transaction


class ShipmentConflict(ValueError):
    pass


@dataclass(frozen=True)
class Shipment:
    warehouse_id: str
    issue_id: str
    wire: str
    status: str
    receipt_id: int | None


def _shipment(row):
    return Shipment(row['warehouse_id'], row['issue_id'], row['wire'], row['status'], row['receipt_id'])


class ShipmentQueue:
    def __init__(self, path):
        self._db = open_database(path)
        try:
            self._db.execute('''
                CREATE TABLE IF NOT EXISTS shipments (
                    shipment_id INTEGER PRIMARY KEY,
                    warehouse_id TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    wire TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued', 'pending', 'acknowledged',
                                                           'stale', 'conflict')),
                    receipt_id INTEGER,
                    UNIQUE (warehouse_id, issue_id),
                    CHECK ((status = 'acknowledged' AND receipt_id IS NOT NULL)
                        OR (status != 'acknowledged' AND receipt_id IS NULL))
                )
            ''')
            self.attempts = AttemptLog(self._db)
            self.managed = ManagedAttemptLog(self._db)
        except BaseException:
            self.close()
            raise

    def close(self):
        self._db.close()

    def enqueue(self, warehouse_id: str, issue_id: str, wire: str) -> Shipment:
        require_text(warehouse_id, 'warehouse_id')
        require_text(issue_id, 'issue_id')
        if release_issue_id(wire) != issue_id:
            raise ShipmentConflict('shipment issue and release issue differ')
        with transaction(self._db, write=True):
            self._db.execute('''
                INSERT INTO shipments (warehouse_id, issue_id, wire, status) VALUES (?, ?, ?, 'queued')
                ON CONFLICT (warehouse_id, issue_id) DO NOTHING
            ''', (warehouse_id, issue_id, wire))
            saved = self.get_shipment(warehouse_id, issue_id)
            if saved.wire != wire:
                raise ShipmentConflict('shipment already has a different frozen copy')
            return saved

    def get_shipment(self, warehouse_id: str, issue_id: str) -> Shipment | None:
        require_text(warehouse_id, 'warehouse_id')
        require_text(issue_id, 'issue_id')
        row = self._db.execute('''
            SELECT * FROM shipments WHERE warehouse_id = ? AND issue_id = ?
        ''', (warehouse_id, issue_id)).fetchone()
        return None if row is None else _shipment(row)

    def list_retryable(self, warehouse_id: str) -> tuple[Shipment, ...]:
        require_text(warehouse_id, 'warehouse_id')
        rows = self._db.execute('''
            SELECT * FROM shipments WHERE warehouse_id = ? AND status IN ('queued', 'pending')
            ORDER BY shipment_id
        ''', (warehouse_id,))
        return tuple(_shipment(row) for row in rows)

    def begin_attempt(self, warehouse_id: str, issue_id: str):
        with transaction(self._db, write=True):
            if self.get_shipment(warehouse_id, issue_id) is None:
                raise LookupError('unknown shipment')
            attempt = self.attempts.start(warehouse_id, issue_id)
            self._db.execute('''
                UPDATE shipments SET status = 'pending'
                WHERE warehouse_id = ? AND issue_id = ? AND status = 'queued'
            ''', (warehouse_id, issue_id))
            return attempt

    def finish_attempt(self, attempt_id: int, result: AttemptResult):
        with transaction(self._db, write=True):
            attempt = self.attempts.finish(attempt_id, result)
            if result.status in {'acknowledged', 'stale', 'conflict'}:
                self._db.execute('''
                    UPDATE shipments SET status = ?, receipt_id = ?
                    WHERE warehouse_id = ? AND issue_id = ? AND status != 'acknowledged'
                ''', (result.status, result.receipt_id, attempt.warehouse_id, attempt.issue_id))
            return attempt
