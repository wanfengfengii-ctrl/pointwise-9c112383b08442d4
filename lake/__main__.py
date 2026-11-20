import argparse
import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from lake.audit.balances import balance
from lake.audit.receipt_view import receipt
from lake.audit.reconcile import reconcile
from lake.delivery.dispatch import send
from lake.delivery.queue import ShipmentQueue
from lake.delivery.retry import retry_pending
from lake.obs.intake import record
from lake.obs.store import RevisionStore
from lake.posting.accept import accept
from lake.posting.store import JournalStore
from lake.release.freeze import freeze
from lake.release.register import ReleaseRegister


def _load_sample(name):
    path = Path(__file__).resolve().parent.parent / 'samples' / name
    return json.loads(path.read_text(encoding='utf-8'))


def _deliver_without_reply(warehouse, wire):
    accept(warehouse, wire)
    return None


def _stage_inputs(root, readings, schedule):
    with closing(RevisionStore(root / 'source.db')) as source, \
            closing(ShipmentQueue(root / 'delivery.db')) as queue:
        register = ReleaseRegister(source)
        for reading in readings:
            issue_id = reading['issue_id']
            values = {key: value for key, value in reading.items() if key != 'issue_id'}
            revision = record(source, **values)
            freeze(source, register, issue_id, revision.observation_id, revision.revision)
            for shift in schedule:
                queue.enqueue(shift['warehouse_id'], issue_id, register.get_frozen(issue_id))


def _work_shift(root, day, shift):
    warehouse_id = shift['warehouse_id']
    journal_path = root / f'{warehouse_id}.db'
    with closing(ShipmentQueue(root / 'delivery.db')) as queue, \
            closing(JournalStore(journal_path, warehouse_id)) as journal:
        statuses = []
        for issue_id, mode in shift['deliveries']:
            transport = _deliver_without_reply if mode == 'drop' else None
            result = send(queue, journal, issue_id, transport=transport)
            statuses.append([issue_id, result.status])
    with closing(ShipmentQueue(root / 'delivery.db')) as queue, \
            closing(JournalStore(journal_path, warehouse_id)) as journal:
        retried = retry_pending(queue, journal)
        replacement = journal.find_issue_receipt('R3')
        if replacement is None:
            raise RuntimeError('R3 has no committed receipt')
        view = receipt(journal, replacement.receipt_id)
        if view.reversal_evidence is None:
            raise RuntimeError('R3 has no reversal evidence')
        rows = balance(journal, day).entries
        ledger = [[row.kind, row.district, row.millimeters, row.revision,
                   row.receipt_id, row.reverses_receipt_id] for row in rows]
        reported = reconcile(journal, day)
        actual = {
            'ledger': ledger,
            'partitions': [[part.district, part.ledger_mm, part.effective_mm]
                           for part in reported.partitions],
            'target_origin': view.receipt.release.target.provenance,
            'reversal_origin': view.reversal_evidence.original_receipt.release.target.provenance,
        }
        downstream = {
            'statuses': statuses,
            'retried': [[issue, result.status, result.receipt_id] for issue, result in retried],
            'daily_net_mm': balance(journal, day).millimeters,
            'reconciles': reported.matches,
        }
        return actual, downstream


def demo():
    upstream = _load_sample('upstream.json')
    schedule = _load_sample('schedule.json')
    expected = _load_sample('hand-ledger.json')
    actual = {}
    downstream = {}
    with TemporaryDirectory(prefix='binpu-lake-') as directory:
        root = Path(directory)
        _stage_inputs(root, upstream['readings'], schedule)
        for shift in schedule:
            name = shift['warehouse_id']
            actual[name], downstream[name] = _work_shift(root, upstream['day'], shift)
    if actual != expected['journals'] or downstream != expected['downstream']:
        raise RuntimeError(f'demo ledger differs from hand ledger: {actual!r}, {downstream!r}')
    evidence = {
        'case_id': 'v4.two_histories',
        'input': {'upstream': upstream, 'schedule': schedule},
        'expected': expected['journals'], 'actual': actual,
        'downstream_expected': expected['downstream'], 'downstream_actual': downstream,
        'code_paths': ['lake/release/freeze.py', 'lake/delivery/dispatch.py', 'lake/posting/accept.py',
                       'lake/audit/receipt_view.py', 'lake/audit/reconcile.py'],
        'oracle': 'samples/hand-ledger.json offsets only the last posted target in each history',
    }
    print('POINTWISE_SCENARIO ' + json.dumps(evidence, ensure_ascii=False, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description='滨浦监测观测日批湖仓')
    parser.add_argument('command', choices=['demo'], help='运行两种送达历史的日批演示')
    parser.parse_args()
    demo()


if __name__ == '__main__':
    main()
