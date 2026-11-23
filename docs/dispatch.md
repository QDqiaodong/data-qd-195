# 泵操作工单离线排程

教学实验组把多罐混合演练拆成一张泵操作工单：每笔转移写清搬多少、用哪台泵、有哪些前置，但不写时隙。本工具在 `tick_limit` 个时隙内离线算出能执行整张工单的最短排程，并交付现有 v1 模拟器能直接读取的计划；算不出时明确报告无解，绝不交付只执行了部分操作的计划。

- 只做离线精确计算，不连接实验设备、不访问外部服务；全部样例为合成数据。
- 全部体积、质量、浓度按 `Fraction` 精确计算，不以容差解释任何不等号。
- 物理完全沿用 [v1 契约](contract.md)：同槽转移读开步状态，入流不资助同槽出量，闭步混合后下一槽才看到新浓度。即使两批已完成的操作相同、各罐余量也相同，浓度不同仍会导致后续操作能否送货不同，排程状态因此包含全部精确质量。

```sh
python -m tank_transfer.dispatch --input JOB --output RESULT
```

RESULT 原子新建，绝不覆盖已有文件或符号链接。工单不合法或文件读写失败时退出 2，原因写 stderr，不交付新结果。`scheduled` 与 `unschedulable` 都是正常业务结果，均退出 0。

## JOB

严格 UTF-8 JSON 对象，最多 524288 字节；拒绝重复键、额外字段、缺失字段、非有限数（`NaN`/`Infinity`）与非法数值类型。顶层恰好四个字段：

| 字段 | 说明 |
| --- | --- |
| `species` | 1..8 个组分，定义与校验完全沿用 v1 |
| `tanks` | 1..16 个初始罐，定义与校验完全沿用 v1 |
| `orders` | 1..14 笔转移操作 |
| `tick_limit` | 时隙上限，1..14 的 JSON 整数（布尔值不是整数） |

ID 沿用仓库规则 `[A-Za-z0-9_:-]{1,64}`；十进制字符串沿用仓库 `number` 格式。

### `orders`

```json
{"id": "r", "source": "A", "target": "D", "volume_l": "1",
 "pump": "pump-r", "after": ["p", "q"],
 "accept": {"species": "salt", "min_g_per_l": "2", "max_g_per_l": "2"}}
```

- `id`、`source`、`target`、`volume_l` 沿用 v1 转移规则；操作 ID 在整张工单内唯一。
- `pump` 是仓库 ID 格式的泵标识；相同 ID 即同一台泵，不需要另附泵表。
- `after` 必填，是本工单中其他操作 ID 的列表（可为空），不得重复。列出的每笔都必须在**严格更早**的时隙完成，同一时隙不算满足前置。未知前置、自依赖、依赖环属于非法工单（退出 2）。
- `accept` 可选；出现时恰好三个字段。`species` 须已定义；`min_g_per_l`、`max_g_per_l` 为 `number` 格式的非负十进制字符串，`min <= max`，两端点都包含。它约束的是**该笔送出的液体**的浓度（等于其开步源罐浓度），不是目标罐闭步混合后的浓度。

合法工单不保证存在排程：单笔体积超过可能取得的库存、时隙不足、浓度门槛无法达到等，都是业务结果 `unschedulable`，不是输入格式错误。

## 排程规则

- 每笔操作完整执行一次：不拆分、不改方向、不改量、不新增操作。
- 一个时隙对应一个非空的 v1 步，最多同时执行两笔，且两笔不得共用一台泵。
- 同槽的操作一起满足 v1 的开步总出量与闭步容量规定；本槽到货既不能资助本槽出量，也不影响本槽浓度门槛的判定；下一槽使用真实的闭步状态。
- 在不超过 `tick_limit` 的排法中先取时隙最少者；仍有多个时，把每槽操作 ID 升序排列，按“各槽 ID 列表组成的序列”取字典序最小者（前缀相同则较短列表在前），ID 按原字符比较。输入中 `orders` 的列表位置不作为任何优先级。

## RESULT

成功时：

```json
{"status": "scheduled", "plan": PLAN, "accept_readings_g_per_l": {"r": "2"}}
```

- `plan` 是独立的 v1 对象：装入 JOB 的组分和初始罐，步骤从 `s1` 顺序命名，槽内转移按操作 ID 升序，只保留 v1 的 `id`、`source`、`target`、`volume_l` 四个字段。把 `plan` 单独存为文件即可用原 `tank-transfer` 入口生成报告；原报告中逐笔送出质量除以体积与 `accept_readings_g_per_l` 中的判定一致。
- `accept_readings_g_per_l` 只包含带 `accept` 的操作，以操作 ID 为键、当次判定的精确浓度为值，写作既约分数或整数字符串。

无解时 RESULT 恰好为 `{"status": "unschedulable"}`，不附带任何部分计划。

## 合成样例

`fixtures/dispatch/`：

| 文件 | 内容 | 结果 |
| --- | --- | --- |
| `scenario1-exact-2.json` | p、q、r 三泵三罐链，r 要求 salt 恰为 2 g/L，上限 3 槽 | 3 槽 `[[p],[q],[r]]`，r 实读 `2` |
| `scenario1-exact-3.json` | 同上但 r 要求恰为 3 g/L | 2 槽 `[[p,q],[r]]`，首槽 q 送出 0 g、次槽 r 送出 3 g |
| `scenario2-after.json` | 两对不连通罐，c 前置 b，两对分用泵 P/Q，限 2 槽 | `[[b,d],[a,c]]` |
| `scenario2-open.json` | 同上但无前置 | `[[a,c],[b,d]]`，打乱工单顺序结果不变 |
| `scenario2-pump-conflict.json` | 有前置且 c 改用泵 P | `unschedulable` |
| `scenario3-max.json` | 14 笔上限工单：A 容量收紧到 2 L，另加 11 笔同泵 U→V，限 11 槽 | `unschedulable`（p 无法单独先注入 A，同槽或 q 先做都给不出 2 g/L） |
| `scenario3-uv-only.json` | 仅 11 笔 U→V，限 11 槽 | 11 槽全部完成，证明场景 3 不能靠时隙数或初始总量判无解 |

在 Linux、CPython 3.11+ 下给 CLI 进程 30 秒 CPU 时间与 256 MiB 虚拟内存复核 `scenario3-max.json`，限制内正常交付 `unschedulable`；其他工单没有统一的耗时承诺。

自动测试 `tests/test_dispatch.py` 覆盖以上全部场景、字典序平局规则、输入拒绝、已有输出（含符号链接）不被覆盖，以及把交付计划重新送入原 v1 模拟器核对逐笔送出质量。
