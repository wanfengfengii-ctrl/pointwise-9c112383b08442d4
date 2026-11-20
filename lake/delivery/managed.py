import json
import uuid

from lake.obs.dimensions import require_text
from lake.release.envelope import release_issue_id
from lake.sqlite import transaction

_PACKET_FIELDS = ('attempt_id', 'warehouse_id', 'issue_id', 'wire')
_REPLY_FIELDS = frozenset(('packet', 'status', 'receipt_id'))
_TERMINAL_STATUSES = frozenset(('acknowledged', 'closed'))


def build_packet(attempt_id, warehouse_id, issue_id, wire):
    return {
        'attempt_id': require_text(attempt_id, 'attempt_id'),
        'warehouse_id': require_text(warehouse_id, 'warehouse_id'),
        'issue_id': require_text(issue_id, 'issue_id'),
        'wire': require_text(wire, 'wire'),
    }


def parse_packet(packet):
    if type(packet) is not dict or frozenset(packet) != frozenset(_PACKET_FIELDS):
        raise ValueError(
            'managed packet must contain exactly attempt_id, warehouse_id, issue_id and wire')
    values = tuple(packet[name] for name in _PACKET_FIELDS)
    for value in values:
        require_text(value, 'managed packet field')
    return values


def validate_managed_reply(reply, binding):
    if type(reply) is not dict or frozenset(reply) != _REPLY_FIELDS:
        raise ValueError('managed reply must contain exactly packet, status and receipt_id')
    status = reply['status']
    receipt_id = reply['receipt_id']
    if status not in _TERMINAL_STATUSES:
        raise ValueError('managed reply status must be acknowledged or closed')
    try:
        packet_binding = parse_packet(reply['packet'])
    except ValueError as error:
        raise ValueError('managed reply carries an invalid packet') from error
    if packet_binding != binding:
        raise ValueError('managed reply packet does not match the attempt binding')
    if status == 'acknowledged':
        if type(receipt_id) is not int or receipt_id <= 0:
            raise ValueError('acknowledged managed reply needs a positive receipt_id')
    elif receipt_id is not None:
        raise ValueError('a closed managed reply must carry receipt_id None')
    return reply


def _dump_reply(reply):
    return json.dumps(reply, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


class ManagedAttemptLog:
    """Terminal-state records for managed attempts, held by the distribution library."""

    def __init__(self, db):
        self._db = db
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS managed_attempts (
                attempt_id TEXT PRIMARY KEY,
                warehouse_id TEXT NOT NULL,
                issue_id TEXT NOT NULL,
                wire TEXT NOT NULL,
                reply_json TEXT,
                FOREIGN KEY (warehouse_id, issue_id)
                    REFERENCES shipments (warehouse_id, issue_id)
            );
        ''')

    def start(self, attempt_id, warehouse_id, issue_id, wire):
        self._db.execute('''
            INSERT INTO managed_attempts (attempt_id, warehouse_id, issue_id, wire)
            VALUES (?, ?, ?, ?)
        ''', (attempt_id, warehouse_id, issue_id, wire))

    def row(self, attempt_id):
        return self._db.execute('''
            SELECT * FROM managed_attempts WHERE attempt_id = ?
        ''', (attempt_id,)).fetchone()

    def require_row(self, attempt_id):
        row = self.row(attempt_id)
        if row is None:
            raise LookupError('unknown managed delivery attempt')
        return row

    @staticmethod
    def stored_reply(row):
        return None if row['reply_json'] is None else json.loads(row['reply_json'])

    def save_reply(self, attempt_id, reply):
        existing = self.stored_reply(self.require_row(attempt_id))
        if existing is not None and existing != reply:
            raise ValueError('managed attempt already keeps a different terminal reply')
        self._db.execute('''
            UPDATE managed_attempts SET reply_json = ?
            WHERE attempt_id = ? AND reply_json IS NULL
        ''', (_dump_reply(reply), attempt_id))
        row = self.require_row(attempt_id)
        stored = self.stored_reply(row)
        if stored != reply:
            raise ValueError('managed attempt already keeps a different terminal reply')
        return stored


def begin_managed(queue, warehouse, issue_id):
    warehouse_id = warehouse.warehouse_id
    require_text(issue_id, 'issue_id')
    shipment = queue.get_shipment(warehouse_id, issue_id)
    if shipment is None:
        raise LookupError('no staged frozen release for this warehouse and issue')
    if release_issue_id(shipment.wire) != issue_id:
        raise LookupError('staged release does not match the requested issue')
    attempt_id = uuid.uuid4().hex
    packet = build_packet(attempt_id, warehouse_id, issue_id, shipment.wire)
    with transaction(queue._db, write=True):
        queue.managed.start(attempt_id, warehouse_id, issue_id, shipment.wire)
    return packet


def get_managed(queue, attempt_id):
    require_text(attempt_id, 'attempt_id')
    row = queue.managed.require_row(attempt_id)
    packet = build_packet(row['attempt_id'], row['warehouse_id'], row['issue_id'], row['wire'])
    return packet, ManagedAttemptLog.stored_reply(row)


def record_managed_reply(queue, attempt_id, reply):
    require_text(attempt_id, 'attempt_id')
    row = queue.managed.require_row(attempt_id)
    binding = (row['attempt_id'], row['warehouse_id'], row['issue_id'], row['wire'])
    if reply is None:
        return ManagedAttemptLog.stored_reply(row)
    validate_managed_reply(reply, binding)
    stored = ManagedAttemptLog.stored_reply(row)
    if stored is not None and stored != reply:
        raise ValueError('managed attempt already keeps a different terminal reply')
    with transaction(queue._db, write=True):
        return queue.managed.save_reply(attempt_id, reply)
