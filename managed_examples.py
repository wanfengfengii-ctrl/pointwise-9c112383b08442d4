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


def joint_verification(root):
    from lake.delivery.dispatch import begin_managed, get_managed, record_managed_reply
    from lake.posting.accept import PostingCommitError, accept_managed, settle_managed
    from lake.posting.eligibility import PostingConflict

    queue_path, warehouse_path = root / "queue.db", root / "review.db"
    with (
        closing(ShipmentQueue(queue_path)) as queue,
        closing(JournalStore(warehouse_path, WAREHOUSE)) as warehouse,
    ):
        day = stage(root / "source.db", queue, warehouse)
        accept_issue = lambda issue: accept(warehouse, queue.get_shipment(WAREHOUSE, issue).wire)

        # 1) first-arrival rebind: the R2 attempt carrying the legal R3 frozen copy is refused,
        #    even though it reaches the warehouse before the original R2 packet.
        r2 = begin_managed(queue, warehouse, "R2")
        r3_wire = queue.get_shipment(WAREHOUSE, "R3").wire
        rebound = {**r2, "wire": r3_wire}
        for call in (accept_managed, settle_managed):
            try:
                call(warehouse, rebound)
            except PostingConflict as error:
                rebind_error = type(error).__name__
            else:
                raise AssertionError("rebound packet must be rejected")
        assert warehouse.managed_outcomes.get(r2["attempt_id"]) is None
        r2_reply = accept_managed(warehouse, r2)
        assert (r2_reply["status"], r2_reply["receipt_id"]) == ("acknowledged", 2)

        # 2) target-warehouse swap: a packet bound for another warehouse is refused there.
        other_path = root / "other.db"
        with closing(JournalStore(other_path, "other")) as other:
            try:
                accept_managed(other, r2)
            except PostingConflict:
                warehouse_swap = "PostingConflict"
            else:
                raise AssertionError("warehouse swap must be rejected")
            assert other.managed_outcomes.get(r2["attempt_id"]) is None

        # 3) reply mismatch: beta's real receipt handed back against alpha's binding is refused,
        #    while a late unknown (None) never overwrites the stored terminal.
        alpha = begin_managed(queue, warehouse, "R3")
        beta = begin_managed(queue, warehouse, "R3")
        alpha_reply = accept_managed(warehouse, alpha)
        beta_reply = accept_managed(warehouse, beta)
        assert alpha_reply["receipt_id"] == beta_reply["receipt_id"] == 3
        record_managed_reply(queue, alpha["attempt_id"], alpha_reply)
        try:
            record_managed_reply(queue, alpha["attempt_id"], beta_reply)
        except ValueError:
            mismatch = "ValueError"
        else:
            raise AssertionError("mismatched reply must be refused")
        assert get_managed(queue, alpha["attempt_id"]) == (alpha, alpha_reply)
        assert record_managed_reply(queue, alpha["attempt_id"], None) == alpha_reply
        record_managed_reply(queue, beta["attempt_id"], beta_reply)
        assert [row[1] for row in ledger(warehouse, day)] == ["A", "A", "B", "B", "A"]
        assert ledger(warehouse, day) == [
            ("target", "A", 7), ("reversal", "A", -7), ("target", "B", 11),
            ("reversal", "B", -11), ("target", "A", 13),
        ]

        result = dict(rebind=rebind_error, warehouse_swap=warehouse_swap,
                      reply_mismatch=mismatch, ledger=ledger(warehouse, day))

        # 4) blocked accept commit: only A7 stays, no terminal outcome, original binding retries.
        blocked_root = root / "blocked"
        blocked_root.mkdir()
        blocked_q, blocked_w = blocked_root / "queue.db", blocked_root / "review.db"
        with (
            closing(ShipmentQueue(blocked_q)) as bqueue,
            closing(JournalStore(blocked_w, WAREHOUSE)) as bwarehouse,
        ):
            stage(blocked_root / "source.db", bqueue, bwarehouse)
            accept_issue("R1")
            attempt = begin_managed(bqueue, bwarehouse, "R2")
            with block_commit(bwarehouse):
                try:
                    accept_managed(bwarehouse, attempt)
                except PostingCommitError:
                    accept_block = "PostingCommitError"
                else:
                    raise AssertionError("blocked commit must surface PostingCommitError")
            assert ledger(bwarehouse, day) == BEFORE
            assert bwarehouse.managed_outcomes.get(attempt["attempt_id"]) is None
            retried = accept_managed(bwarehouse, attempt)
            assert (retried["status"], retried["receipt_id"]) == ("acknowledged", 2)
            assert ledger(bwarehouse, day) == AFTER
        result["blocked_accept"] = accept_block

        # 5) blocked settle commit rolls back, so the original packet can still be accepted.
        settle_root = root / "settle"
        settle_root.mkdir()
        settle_q, settle_w = settle_root / "queue.db", settle_root / "review.db"
        with (
            closing(ShipmentQueue(settle_q)) as squeue,
            closing(JournalStore(settle_w, WAREHOUSE)) as swarehouse,
        ):
            stage(settle_root / "source.db", squeue, swarehouse)
            accept_issue("R1")
            attempt = begin_managed(squeue, swarehouse, "R2")
            with block_commit(swarehouse):
                try:
                    settle_managed(swarehouse, attempt)
                except PostingCommitError:
                    settle_block = "PostingCommitError"
                else:
                    raise AssertionError("blocked settle must surface PostingCommitError")
            assert swarehouse.managed_outcomes.get(attempt["attempt_id"]) is None
            reply = accept_managed(swarehouse, attempt)
            assert (reply["status"], reply["receipt_id"]) == ("acknowledged", 2)
            assert ledger(swarehouse, day) == AFTER
        result["blocked_settle"] = settle_block

    print(json.dumps(dict(case="managed_joint_verification", **result), ensure_ascii=False))


FACTS = (
    "事实依据：\n"
    "1. 过账资格——仅当 packet 的目标仓、发行号与 V1 冻结件三者自洽且同一 attempt_id 未改绑时，\n"
    "   仓端才在写事务内按 V1 口径裁决；重投命中同一证据即幂等取回原首执，已结清的尝试只回放 closed。\n"
    "2. 仓端提交——首执/冲销/正项与该次 managed 终局在同一个写事务提交；COMMIT 被拒则整组回滚，\n"
    "   账项与终局都不留痕，原绑定可重试；先提交成功的一方（接纳或结清）决定该次终局。\n"
    "3. 分发回记——record_managed_reply 只接受 packet 绑定与本地 attempt 完全一致、且状态/首执号自洽的回包；\n"
    "   错配或矛盾抛 ValueError 且保留原终局，None 表示结果未知、永不覆盖已存终局，重开双库可按 attempt_id 查回。"
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("v1", "managed"))
    if parser.parse_args().mode == "v1":
        check_v1()
        return
    for order in ("closed-first", "first-received", "second-received"):
        with TemporaryDirectory(prefix="binpu-managed-") as directory:
            managed_history(Path(directory), order)
    with TemporaryDirectory(prefix="binpu-joint-") as directory:
        joint_verification(Path(directory))
    print(FACTS)


if __name__ == "__main__":
    main()
