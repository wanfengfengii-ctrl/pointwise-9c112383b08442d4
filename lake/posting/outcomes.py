class ManagedOutcomes:
    """Persisted terminal outcomes of managed attempts inside a review warehouse."""

    def __init__(self, db):
        self._db = db

    def get(self, attempt_id):
        return self._db.execute('''
            SELECT * FROM managed_outcomes WHERE attempt_id = ?
        ''', (attempt_id,)).fetchone()

    def insert(self, attempt_id, warehouse_id, issue_id, wire, status, receipt_id):
        self._db.execute('''
            INSERT INTO managed_outcomes
                (attempt_id, warehouse_id, issue_id, wire, status, receipt_id)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (attempt_id, warehouse_id, issue_id, wire, status, receipt_id))
