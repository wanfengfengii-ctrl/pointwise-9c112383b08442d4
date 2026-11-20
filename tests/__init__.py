import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lake.delivery.queue import ShipmentQueue
from lake.obs.intake import record
from lake.obs.store import RevisionStore
from lake.posting.accept import accept
from lake.posting.store import JournalStore
from lake.release.freeze import freeze
from lake.release.register import ReleaseRegister

DAY = '2026-02-18'


def drop_after_commit(warehouse, wire):
    accept(warehouse, wire)
    return None


def ledger_rows(view):
    return [[entry.kind, entry.district, entry.millimeters, entry.revision] for entry in view.entries]


def emit_scenario(case_id, inputs, expected, actual, downstream_expected, downstream_actual,
                  code_paths, oracle):
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': case_id, 'input': inputs, 'expected': expected, 'actual': actual,
        'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
        'code_paths': code_paths, 'oracle': oracle,
    }, ensure_ascii=False, sort_keys=True))


class LakeCase(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix='binpu-test-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source_path = self.root / 'source.db'
        self.queue_path = self.root / 'delivery.db'
        self.source = self.open_source()
        self.register = ReleaseRegister(self.source)
        self.queue = self.open_queue()

    def open_source(self):
        source = RevisionStore(self.source_path)
        self.addCleanup(source.close)
        return source

    def open_queue(self):
        queue = ShipmentQueue(self.queue_path)
        self.addCleanup(queue.close)
        return queue

    def warehouse(self, name='review', timeout=5.0):
        warehouse = JournalStore(self.root / f'{name}.db', name, timeout=timeout)
        self.addCleanup(warehouse.close)
        return warehouse

    def stage_history(self, *warehouse_ids):
        for revision, district, raw, unit in ((1, 'A', 7, 'mm'), (2, 'B', 11000, 'um'),
                                              (3, 'A', 13, 'mm')):
            issue = f'R{revision}'
            record(self.source, 'P-17', revision, DAY, district, raw, unit, f'log:{revision}')
            freeze(self.source, self.register, issue, 'P-17', revision)
            for warehouse_id in warehouse_ids:
                self.queue.enqueue(warehouse_id, issue, self.register.get_frozen(issue))

    def accept_issue(self, warehouse, issue):
        return accept(warehouse, self.register.get_frozen(issue))
