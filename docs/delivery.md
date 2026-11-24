# 离线事实表交付

教学组把每次多罐演练的精确结果做成**事实表**，由一个生产库逐版发布；各使用方（课堂评分、实验数据归档等）各自维护**本地副本**，双方只通过 JSON 数据包交换，不直接读写对方的存储。不连接实验设备或外部服务，全部样例为合成数据。

- 生产库：`python -m tank_transfer.delivery --store PATH <command>`
- 本地副本：`python -m tank_transfer.replica --store PATH <command>`

成功退出 0，stdout 恰好一个紧凑 JSON 对象；`blocked` 与 `none` 是正常业务结果，同样退出 0。拒绝退出 2，原因写 stderr，stdout 为空，已有业务状态不变。两种库都是单机、单写者文件存储；同一库不并发执行命令。

## 事实表

每一版计划对应一张事实表：一个按键排序的 `{"key": 键, "value": 值}` 数组。键是字符串数组，按键中各字符串的原字符逐项比较，公共前缀相同则短键在前。含 0 的记录照常保留。物理口径完全沿用 [v1 契约](contract.md)：同一步的全部转移同时读取开步状态，入流不资助同一步出流，全程 `Fraction` 精确计算，不取整、不推算；每份计划**完整验证到最后一步**才采纳。

| 键 | 值 |
| --- | --- |
| `["initial", 罐ID]` | `{"volume_l": …, "mass_g": {组分: …}}` 初始体积与全部组分质量 |
| `["step", 步ID]` | `{"ordinal": n}` 该步在计划中的 1 起序号；没有转移的步也有一行 |
| `["transfer", 步ID, 转移ID]` | `{"source": …, "target": …, "volume_l": …, "mass_g": {…}}` 源、目的、体积与全部组分质量 |
| `["closing", 步ID, 罐ID]` | `{"volume_l": …, "mass_g": {…}}` 闭步体积与全部组分质量 |

体积与质量是既约分数或整数字符串（如 `"2/3"`、`"1"`）；`ordinal` 是 JSON 整数。只重排同一步的转移、罐或组分列表，或把数值改成等值十进制写法，不会改变事实表；步序变化是事实变化。步的身份是步 ID，`ordinal` 只是该版里的事实值，不能拿步序号替代步 ID。

## 指纹与规范 JSON

规范 JSON：UTF-8、对象键排序、无额外空白、无尾换行。指纹是对规范 JSON 字节计算的 SHA-256（64 位小写十六进制）。

- **快照指纹**：对事实表数组本身（排序后的 `{"key","value"}` 数组）计算。
- **包指纹**：对数据包除去 `fingerprint` 字段本身后的全部字段计算（指纹自身不参与其计算）。

## 交换格式

### 引导快照文档（生产库 → 使用方）

```json
{"drill": "drill-1", "version": 0, "fingerprint": "<快照指纹>",
 "facts": [{"key": ["initial", "A"], "value": {"volume_l": "2", "mass_g": {"salt": "4"}}}]}
```

### 增量包（生产库 → 使用方）

字段固定，不多不少：

```json
{"drill": "drill-1", "from_version": 0, "to_version": 1,
 "from_snapshot": "<起始快照指纹>", "to_snapshot": "<目标快照指纹>",
 "deletes": [["transfer", "s2", "draw"]],
 "writes": [{"key": ["closing", "s2", "C"], "value": {"volume_l": "0", "mass_g": {"salt": "0"}}}],
 "fingerprint": "<包指纹>"}
```

- `deletes` 是按键排列的删除键，只含目标版不存在的旧键；`writes` 是按键排列的新增/替换整行，只含新增或值确实变化的行。转移指令完全没变但实际送出组分质量变了，该行及受影响的闭步行都在 `writes` 中。
- 内容不变的合法发布仍形成新版与空增量（`deletes`、`writes` 皆空）；相同快照指纹不能替代版号。
- 数据包自包含：原计划文件之后被删除或覆盖，不影响数据包的读取。

### 游标（使用方 → 生产库）

```json
{"drill": "drill-1", "version": 1, "snapshot": "<该版快照指纹>"}
```

### 确认（使用方 → 生产库）

```json
{"consumer": "grading", "drill": "drill-1", "version": 2,
 "snapshot": "<目标版快照指纹>", "packet": "<使其到达该版的增量包指纹>"}
```

确认绑定使用方、演练、目标版号、目标快照指纹与增量包指纹。累计确认到更新的版本不必逐封回传；这里只做一致性校验，假定确认来自按本协议工作的本地副本，不签名、不鉴权、不联网。

## 生产库命令

### `init --drill ID --k K --plan FILE`

以一份完整 v1 计划建立新生产库，记为**第 0 版**，`floor=head=0`；不覆盖已有路径。`--k` 是保留容量（1..8 个相邻增量）。演练 ID 沿用仓库现有 ID 规则（`[A-Za-z0-9_:-]{1,64}`）。

### `publish --plan FILE --expect N`

提交一份**完整 v1 计划**和预期的当前版号；`--expect` 必须等于当前 `head`，成功后新版本为 `head+1`（从 1 起连续递增）。各版必须维持相同的罐 ID 与组分 ID 集合，其余内容可更正。非法计划或过时的预期版号**拒绝**（退出 2）并保持原状态，不是 `blocked`。

合法且基于当前 head 的发布在需要推进 `floor` 时检查：`新 floor = max(原 floor, 新 head - K)`，且不得超过任何已登记使用方的已确认位置（没有使用方则不受此限制）。若某方仍需要即将被清除的增量，返回：

```json
{"status": "blocked", "drill": "…", "floor": 0, "head": 2,
 "blockers": [{"consumer": "archive", "confirmed": 0}]}
```

列出全部阻挡方及其位置，不改变 head、floor 或任何数据包。收到确认本身不推进 floor；后续发布才能使用释放出来的额度。

成功返回 `{"status": "published", "version", "floor", "head", "snapshot", "packet", …}`。版本、增量与窗口推进在同一次原子提交中生效；推进 `floor` 的同时回收仅用于早于 `floor` 的快照、源计划和截止于 `floor` 或更早的增量内容，不另藏完整旧历史（只保留判断确认重试所需的少量元数据：各使用方当前确认的那一份确认）。

### `status`

返回 `{"drill", "k", "floor", "head", "versions", "deltas", "consumers"}`。`versions` 是仍保留快照的版号区间 `[floor..head]`，`deltas` 是仍保留内容的增量目标版号 `(floor..head]`——窗口外内容已回收的证据可由此查询。

### `register --consumer ID`

最多 8 个互不相同的使用方 ID。首次登记选择当时的 `floor` 作为其引导版，记录其已确认位置为该版（表示不需要更早的数据，不代表已取走引导快照），返回 `{"consumer", "bootstrap", "fingerprint"}`。重复登记同一 ID 不重置或推进任何位置，返回原登记信息；不做退订。

### `snapshot --consumer ID`

按 ID 取回该方的引导快照文档。在该方确认更高版本前，其引导版就是当时的 `floor`，快照必可取回；确认更高版本后 `floor` 可能越过引导版，此后取回被拒绝。

### `next --cursor FILE`

读游标之后的下一包。游标含演练 ID、版号与快照指纹；窗口外、异演练或指纹不匹配的游标拒绝（退出 2）。正确游标到达 `head` 时返回 `{"status": "none", …}`，否则返回 `version → version+1` 的增量包。读取或导出增量不等于确认。

### `confirm --consumer ID --ack FILE`

按使用方 ID 处理确认：拒绝错用另一方确认、未知版、错误指纹或倒退。重复收到该方当前已确认的同一份确认按成功处理，即使对应增量内容已回收。成功返回 `{"status": "confirmed", "consumer", "confirmed"}`。

## 本地副本命令

### `init --consumer ID --snapshot FILE`

以登记时取得的引导快照文档建立本地副本；校验文档形状与快照指纹，不覆盖已有路径。

### `apply --packet FILE`

只可从本地游标前进一步：检查演练 ID、起止版号、前置快照指纹、包指纹与操作合法性——拒绝重复键操作、删除不存在的键、原值不变的写入、形状错误及应用后指纹与目标不符；任何拒绝都不改变本地副本。跳版或更早的旧包均拒绝。

成功时整批数据、游标与确认一起原子提交，返回确认对象。仅在当前版本已落地且重送的是使它到达此版本的**同一规范增量包**（按包指纹比较身份，不是只看目标指纹）时，返回原确认而不改数据。

### `status` / `facts` / `ack`

`status` 返回本地游标信息 `{"consumer", "drill", "version", "snapshot"}`；`facts` 返回当前完整事实表；`ack` 重新取回当前版本的确认（尚未应用过任何包时拒绝）。

## 限额

| 项目 | 上限（含端点） |
| --- | --- |
| 单份计划文件 | 1048576 字节（1 MiB） |
| 每版罐 | 32 |
| 每版组分 | 8 |
| 每版步骤 | 64 |
| 每版转移合计 | 512 |
| 保留容量 K | 1..8 个相邻增量 |
| 每库使用方 | 8 |
| 单个交换文档（包/快照/游标/确认） | 8000000 字节 |

超限拒绝，不改变已有状态。演练 ID 与使用方 ID 沿用仓库现有 ID 规则。

## 崩溃恢复

每个库的已提交状态（生产库：版本、增量、窗口与使用方位置；本地副本：整批事实、游标与可重复取回的确认）保存于一个文件，通过「写入临时文件 → fsync → 原子 rename → fsync 目录」提交。

- 提交前终止：重开只看到完整的原状态，可原样重试。
- 提交后、调用方拿到返回值前终止：重开看到完整的新状态；本地副本重送同一规范增量包返回原确认，不二次落地。
- 拒绝或读操作不留下部分新业务状态。

自动测试 `tests/test_delivery.py` 使用 `TANK_TRANSFER_CRASH_POINT=before-commit|after-commit` 在真实子进程中触发 `SIGKILL`，再由新进程核对状态与重试结果。仅支持本机文件系统、每库串行调用；要求从进程被强制终止恢复，不要求掉电、并发写入或跨机事务。

## 样例

`fixtures/delivery/`：`v0.json`（A 2 L/4 g、B 2 L/0 g、C/D 空；s1 `feed` A→B 1 L，s2 `draw` B→C 1 L）、`v1.json`（`feed` 改 A→D，`draw` 指令不变但送出 0 g）、`v2.json`（移除 `draw`，保留空步 s2）、`v3.json`（与 v2 内容相同，演示空增量）、`invalid-last-step.json`（末步物理无效）。

```sh
PROD=$(mktemp -d)/prod
python -m tank_transfer.delivery --store "$PROD" init --drill drill-1 --k 2 --plan fixtures/delivery/v0.json
python -m tank_transfer.delivery --store "$PROD" register --consumer grading
python -m tank_transfer.delivery --store "$PROD" snapshot --consumer grading > snap0.json
python -m tank_transfer.replica --store grading-replica init --consumer grading --snapshot snap0.json
python -m tank_transfer.delivery --store "$PROD" publish --plan fixtures/delivery/v1.json --expect 0
```

既有 v1 模拟、样本登记册、化验、工单排程、配方与整数台账入口的输入、输出与异常契约保持不变。
