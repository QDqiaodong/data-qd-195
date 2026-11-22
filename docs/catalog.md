# 样本发布登记册

离线登记的样本凭证：以冻结的 v1 物理模型计算每个样本的**逐来源证据**，发布后允许下游读取。不连接真实设备或外部服务。

```sh
python -m tank_transfer.catalog --store PATH <command>
```

成功退出 0，stdout 恰好一个 JSON 对象；拒绝退出 2，原因写在 stderr，stdout 为空，已有业务状态不变。登记册为单机、单写者文件存储；同一登记册不并发执行命令。

## 证据模型

在现有 v1 模型之上新增独立来源层，v1 的输入字段、物理约束、结果与 CLI 契约全部不变。

- 每个**有正体积**的初始罐是一个来源，来源 ID 即初始罐 ID；空罐不产生来源项。有体积但各组分质量均为零的来源仍然保留。
- 来源层与 v1 层一起推进：每条转移按 `转移体积 / 开步源罐体积` 从开步源罐的各来源取走体积与每个组分的质量。入流不资助同一步出流，出流组成取开步状态。
- 闭步时留存与流入按来源合并，同一来源从不同路径流入同一罐合为一项。
- 每一步都交叉校验：每个样本各来源之和精确等于 v1 总体积与各组分质量；每个来源的全系统体积和各组分质量在闭步分别守恒。
- 数值全程 `Fraction` 精确计算，输出既约分数或整数字符串；只保留正体积来源项。

证据对象：

```json
{
  "volume_l": "4",
  "mass_g": {"salt": "40"},
  "lots": {
    "A": {"volume_l": "2", "mass_g": {"salt": "20"}},
    "C": {"volume_l": "2", "mass_g": {"salt": "20"}}
  },
  "source": "C",
  "target": "E"
}
```

`source`/`target` 只出现在 `transfer` 证据中。证据比较使用以上字段的精确值，与对象键顺序、转移列表重排、输入十进制写法、无关样本、闭步罐容量、计划版本及其他步骤无关。**步 ID 本身是样本身份**，不是步序号：插入或删除前面的步骤不会改变同一步 ID 的样本身份。

## 命令

### `init --revision ID --source FILE`

以有效 v1 计划建立新登记册；不覆盖已有路径。返回 `{"revision": ID}`。

### `head`

返回 `{"revision": ID}`，即当前版本。

### `issue --receipt ID --kind closing|transfer --step ID --item ID`

只从**当前版本**发布一个存在且非空的样本：

- `closing`：`step` 是步 ID，`item` 是罐 ID，表示该步闭步库存。
- `transfer`：`step` 是步 ID，`item` 是步内转移 ID，表示该转移携带的物料。

凭证 ID 在同一登记册中永不复用（包括已撤回）。成功返回：

```json
{
  "receipt": "rc-draw",
  "issued_at": "r1",
  "selector": {"kind": "transfer", "step": "s2", "item": "draw"},
  "evidence": {"...": "..."},
  "revocation": null
}
```

选择器不存在或样本体积为零均拒绝。

### `read --receipt ID [--archive]`

- 普通读取只允许仍有效的凭证；未知或已撤回的凭证拒绝（退出 2）。即使当前版本更新，返回对象与签发时相同，不重签、不改写。
- `--archive` 是审计读取：总能取回签发时的证据与身份。若已撤回，`revocation` 为 `{"revision": "撤回版本 ID", "reason": "missing|empty|changed"}`。

登记册保存已提交版本的完整计划副本，原始输入文件被删除或覆盖后仍可正常工作。

### `revise --revision ID --base ID --source FILE`

输入是该版本的**完整计划**（不是补丁）。新版本 `base` 必须是当前版本；罐 ID 集合与组分 ID 集合沿用初始化时的集合；其余 v1 字段可更正，步骤和转移可插入、删除、重排。仅当整版输入和物理过程有效才采纳。

采纳后用新计划重新计算所有**仍有效**凭证的同一选择器证据：

| 情况 | 原因 | 处理 |
| --- | --- | --- |
| 选择器不存在 | `missing` | 撤回 |
| 存在但样本体积为零 | `empty` | 撤回 |
| 非空但证据不同 | `changed` | 撤回 |
| 证据相同 | — | 保留，不重签 |

撤回永久有效；后续更正即使恢复旧值也不复活旧凭证。返回：

```json
{"revision": "r2", "base": "r1",
 "revoked": [{"receipt": "rc-draw", "reason": "changed"}]}
```

`revoked` 只含本次新撤回的凭证，按凭证 ID 排序。允许内容相同的新版本（此时无需撤回）。

### 更正重试（幂等）

同一已成功更正的版本 ID、`base` 和解析后相同的源 JSON 是对原更正的**重试**：返回原回执，不重新比较、不改变当前版本，即使 base 已非当前版本或版本额度已满。可解析输入中对象键顺序和 JSON 排版不算变化；数组顺序与数值字符串内容算变化。

- 已用更正 ID 携不同 `base` 或不同源 JSON：拒绝。
- 把初始版本 ID 用作更正 ID：拒绝。
- 没有提交过的失败更正不占用 ID，后续可用同一 ID 提交合法计划。
- 重试时仍执行输入文件大小上限和 JSON 语法检查。

## 限额

| 项目 | 上限（含端点） |
| --- | --- |
| 单版输入文件 | 524288 字节 |
| 每版罐 | 16 |
| 每版组分 | 8 |
| 每版步骤 | 64 |
| 每版转移合计 | 256 |
| 每册成功版本（含初始） | 32 |
| 每册已签发凭证（含撤回） | 1024 |

超限拒绝，且不占用版本或凭证名额。版本与凭证 ID 沿用仓库现有 ID 规则：`[A-Za-z0-9_:-]{1,64}`。

## 崩溃恢复

整个已提交状态（head、不可变更正回执、全部凭证及其撤回）保存于一个文件，通过「写入临时文件 → fsync → 原子 rename → fsync 目录」提交。因此采纳版本、登记回执和本次全部撤回对后继命令一起生效。

- 提交前终止：重开登记册仍是完整的旧版本与旧凭证，可原样重试。
- 提交后、调用方看到回执前终止：重开登记册为完整的新版本；重试同一更正是幂等重试，返回原回执而非再次采纳。
- 凭证证据在签发后不可被任何修复操作改写。

自动测试 `tests/test_catalog.py` 使用 `TANK_TRANSFER_CRASH_POINT=before-commit|after-commit` 在真实子进程中触发 `SIGKILL`，再由新进程核对状态与重试结果。

## 范围

仅支持单机本地文件系统；同一登记册不并发执行命令。需处理进程被强制终止，不要求机器掉电或跨主机一致性。不连接真实设备、不发布到外部服务。原 `tank-transfer --source ... --output ...` 与 `service.process` 的输入限制、输出、异常及不覆盖旧输出的契约保持不变。

## 样例

- `fixtures/catalog-r1.json`：初次计划（A、B 各 10 L/100 g，C 5 L/50 g，D、E 空）。
- `fixtures/catalog-r2.json`：仅交换 s1 两条转移的目的罐。`draw` 与 E 闭步总量不变，但来源 A 被 B 替换，对应旧凭证以 `changed` 撤回；s2 的 A 闭步证据不变。

```sh
STORE=$(mktemp -d)/catalog
python -m tank_transfer.catalog --store "$STORE" init --revision r1 --source fixtures/catalog-r1.json
python -m tank_transfer.catalog --store "$STORE" issue --receipt rc-draw --kind transfer --step s2 --item draw
python -m tank_transfer.catalog --store "$STORE" issue --receipt rc-a --kind closing --step s2 --item A
python -m tank_transfer.catalog --store "$STORE" revise --revision r2 --base r1 --source fixtures/catalog-r2.json
python -m tank_transfer.catalog --store "$STORE" read --receipt rc-draw --archive
```
