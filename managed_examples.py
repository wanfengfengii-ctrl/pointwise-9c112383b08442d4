import argparse
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from lake.audit.balances import balance
from lake.delivery.dispatch import send
from lake.delivery.queue import ShipmentQueue
from lake.obs.intake import record
from lake.obs.store import RevisionStore
from lake.posting.accept import accept
from lake.posting.store import JournalStore
from lake.release.freeze import freeze
from lake.release.register import ReleaseRegister

ROOT = Path(__file__).resolve().parent
WAREHOUSE = "review"
BEFORE = [("target", "A", 7)]
AFTER = BEFORE + [("reversal", "A", -7), ("target", "B", 11)]


def stage(source_path, queue, warehouse):
    sample = json.loads((ROOT / "samples/upstream.json").read_text(encoding="utf-8"))
    with closing(RevisionStore(source_path)) as source:
        register = ReleaseRegister(source)
        for row in sample["readings"]:
            if row["issue_id"] not in {"R1", "R2", "R3"}:
                continue
            material = row.copy()
            issue = material.pop("issue_id")
            target = record(source, **material)
            freeze(source, register, issue, target.observation_id, target.revision)
            queue.enqueue(warehouse.warehouse_id, issue, register.get_frozen(issue))
    assert send(queue, warehouse, "R1").status == "acknowledged"
    return sample["day"]


def ledger(warehouse, day):
    return [(row.kind, row.district, row.millimeters) for row in balance(warehouse, day).entries]


@contextmanager
def block_commit(warehouse):
    def authorize(action, operation, *_):
        if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    warehouse._db.set_authorizer(authorize)
    try:
        yield
    finally:
        warehouse._db.set_authorizer(None)


def v1_history(first_arrived):
    with (
        closing(ShipmentQueue(":memory:")) as queue,
        closing(JournalStore(":memory:", WAREHOUSE)) as warehouse,
    ):
        day = stage(":memory:", queue, warehouse)
        delayed = []

        def lose_reply(target, wire):
            if first_arrived:
                accept(target, wire)
            else:
                delayed.append(wire)
            return None

        send(queue, warehouse, "R2", transport=lose_reply)
        before = ledger(warehouse, day)
        assert before == (AFTER if first_arrived else BEFORE)
        assert (warehouse.find_issue_receipt("R2") is not None) == first_arrived

        def second_delivery(target, wire):
            if delayed:
                assert delayed[0] == wire
            return accept(target, wire)

        reply = send(queue, warehouse, "R2", transport=second_delivery)
        statuses = [item.result.status for item in queue.attempts.list_for_shipment(WAREHOUSE, "R2")]
        return dict(statuses=statuses, receipt_id=reply.receipt_id, ledger=ledger(warehouse, day))


def check_v1():
    expected = dict(statuses=["pending", "acknowledged"], receipt_id=2, ledger=AFTER)
    only_second_arrived = v1_history(False)
    assert only_second_arrived == v1_history(True) == expected
    print(json.dumps(dict(case="v1_identical_issue_views", actual=only_second_arrived)))


def managed_history(root, order):
    from lake.delivery.dispatch import begin_managed, get_managed, record_managed_reply
    from lake.posting.accept import accept_managed, settle_managed

    queue_path, warehouse_path = root / "queue.db", root / "review.db"
    with (
        closing(ShipmentQueue(queue_path)) as queue,
        closing(JournalStore(warehouse_path, WAREHOUSE)) as warehouse,
    ):
        day = stage(root / "source.db", queue, warehouse)
        first = begin_managed(queue, warehouse, "R2")
        second = begin_managed(queue, warehouse, "R2")
        first_id = first["attempt_id"]
        assert first_id != second["attempt_id"] and first["wire"] == second["wire"]
        received = None
        if order == "first-received":
            received = accept_managed(warehouse, first)
        if order == "second-received":
            accept_managed(warehouse, second)
        assert get_managed(queue, first_id) == (first, None)
        assert ledger(warehouse, day) == (BEFORE if order == "closed-first" else AFTER)
        missed_settlement = settle_managed(warehouse, first)

    with (
        closing(ShipmentQueue(queue_path)) as queue,
        closing(JournalStore(warehouse_path, WAREHOUSE)) as warehouse,
    ):
        assert get_managed(queue, first_id) == (first, None)
        audit_reply = settle_managed(warehouse, first)
        assert audit_reply == missed_settlement
        if order == "first-received":
            assert audit_reply == received
            assert (audit_reply["status"], audit_reply["receipt_id"]) == ("acknowledged", 2)
        else:
            assert (audit_reply["status"], audit_reply["receipt_id"]) == ("closed", None)
        assert record_managed_reply(queue, first_id, audit_reply) == audit_reply
        assert record_managed_reply(queue, first_id, None) == audit_reply
        assert get_managed(queue, first_id) == (first, audit_reply)
        assert accept_managed(warehouse, first) == audit_reply
        assert ledger(warehouse, day) == (BEFORE if order == "closed-first" else AFTER)
        second_reply = accept_managed(warehouse, second)
        assert (second_reply["status"], second_reply["receipt_id"]) == ("acknowledged", 2)
        assert record_managed_reply(queue, second["attempt_id"], second_reply) == second_reply
        actual = ledger(warehouse, day)
        assert actual == AFTER
        print(json.dumps(dict(case=order, first=audit_reply["status"],
                              second_receipt=second_reply["receipt_id"], ledger=actual)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("v1", "managed"))
    if parser.parse_args().mode == "v1":
        check_v1()
        return
    for order in ("closed-first", "first-received", "second-received"):
        with TemporaryDirectory(prefix="binpu-managed-") as directory:
            managed_history(Path(directory), order)


if __name__ == "__main__":
    main()
