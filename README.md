# 滨浦监测观测日批湖仓

滨浦监测资料中心的资料员整理测站原读，白班分发员交付冻结的观测更正，复核员登记实际收到的修订，夜班使用复核仓和纸面首执结班。不同单位先精确归一。跳送和丢回包时，逐笔账说明仓内实际换过哪一笔，分区净额用于核对当班结果。

## 运行

需要 Python 3.11，使用标准库。所有命令从项目根目录执行。

```text
python3 -B check_project.py
python3 -B -m lake demo
```

`check_project.py` 由运行平台提供，执行 `tests` 包内的完整确定性回归。在只有源码的副本中也可运行 `python3 -B -m unittest discover -v`。演示在测试临时目录初始化三个库角色，从 `samples/upstream.json` 载入原读，用 `samples/schedule.json` 安排两条独立的复核仓历史，对照 `samples/hand-ledger.json` 的逐笔手算及查回结果。

## 一天的工作

1. 资料员从测站台账取得原读及出处，录入具名修订。1 厘米归一为 10 毫米，可在首执中复查原值和单位。
2. 白班指明修订和发行号进行冻结，再按收件仓将编码件暂存到分发库。
3. 复核仓接纳首版，提交正项和首执。分发库单独登记收到的首执。
4. 同一观测的 A7、B11、A13 依次录入并冻结。源库关停后，分发使用已暂存的原件。
5. B11 滞留时，A13 到仓只冲 A7。B11 已提交但丢回包时，A13 只冲 B11。两条账史最终均为 A 区 13、B 区 0。
6. 分发重开后查回 B11。曾入账的件取回原首执；从未入账的旧件登记为过时。
7. 夜班重开复核仓，按日区复算逐笔账，并与同一快照的当前有效正项比较。
8. 夜班用首执查看当次分录，沿冲销引用取得原正项、原首执及测站出处。

## Python 接口

| 职责 | 入口与结果 |
| --- | --- |
| 源库 | `lake.obs.store.RevisionStore(path)` 持有源库；`lake.obs.intake.record(source, observation_id, revision, day, district, raw_value, unit, provenance)` 返回不可变 `ObservationRevision`。同参复用，异参抛 `RevisionConflict`，输入错误抛 `ValueError`。 |
| 冻结 | `lake.release.register.ReleaseRegister(source)` 使用源库；`lake.release.freeze.freeze(source, register, issue_id, observation_id, revision)` 返回 `FrozenRelease`。同号同参复用，同号异参抛 `ReleaseConflict`，缺失源修订抛 `RevisionNotFound`。`get_frozen(issue_id)` 返回原编码或 `None`。 |
| 暂存和投递 | `lake.delivery.queue.ShipmentQueue(path)` 持有分发库；`enqueue(warehouse_id, issue_id, wire)` 固定原件，重复入队保留处理状态。`lake.delivery.dispatch.send(queue, warehouse, issue_id, register=None, transport=None)` 返回 `AttemptResult`。未暂存时可给登记簿以完成首次暂存。`lake.delivery.retry.retry_pending(queue, warehouse, transport=None)` 依入队顺序查回待投和待查件。 |
| 过账 | `lake.posting.store.JournalStore(path, warehouse_id, timeout=5.0)` 打开或创建指定复核仓；重开核对仓标识。`lake.posting.accept.accept(warehouse, wire)` 提交后返回 `Receipt`，或者抛 `PostingConflict`、`StaleRevision`、`PostingCommitError`。 |
| 核账 | `lake.audit.balances.balance(warehouse, day, district=None)` 返回有序分录和净额；`lake.audit.receipt_view.receipt(warehouse, receipt_id)` 返回原冻结证据、当次分录及可选原账证，未知号返回 `None`；`lake.audit.reconcile.reconcile(warehouse, day)` 返回各分区逐笔净额和有效正项合计。 |

三个库的路径由调用方提供，使用完毕调用 `close()`。初始化始终保留既有记录。分发端的传输接口接收 `(warehouse, wire)`，正常调用复核仓后交回 `Receipt`。实际回包不可用时返回 `None` 或抛 `ConnectionError`，此时 `pending` 表示未知。`acknowledged` 提供仓内首执号，`stale` 和 `conflict` 分别说明业务拒绝，`failed` 表示已知提交失败并可重投。尝试日志保留每次结果，未结束的尝试其结果为 `None`。可按仓和发行号通过 `queue.attempts.list_for_shipment` 读取。

## 数据及记账口径

- 观测号、发行号、分区、仓标识和出处使用去除外层空白的非空文本。日期格式为 `YYYY-MM-DD`。版序为观测号内的正整数，允许缺号。原读为整数，支持 `mm`、`cm`、`um`，其中 `um` 必须能整除一千。零和带符号原读按同样口径处理。
- 固定件使用 V1 JSON，带签号的十六进制字符串保存整数，SQLite 也以文本保存原读、毫米及版序。换算、取反及核账使用 Python 整数。材料中的出处保存上游定位，复核仓核验结构、唯一业务身份及与原读的换算一致性。
- 复核仓允许直接接纳任意比当前版新的有效目标。在自己的写事务中读取实际有效正项，按原日区及金额记冲销，再按目标日区记目标正项，最后同次提交有效项、首执与接纳绑定。正项是分录类别。每个观测至多有一个当前有效正项。
- 已入账的同参重投取回历史首执。新发行号递交相同目标证据也可绑定原首执。未入账的迟到旧版过时，同号异参或证据冲突对应拒绝，不增加分录。提交失败回滚整组；提交后失联按重投查证。
- 读账按分录日区筛选并按入账顺序返回，换账组内冲销先于目标正项。合法空账的净额为 0。核账保留有分录而净额为 0 的分区，原首执及其冲销证据独立于当前有效版和源库。
