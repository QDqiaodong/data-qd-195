# 离线配方规划

教学实验组在正式演练前要为一组目标罐配出满足组分范围的混合方案：老师给出一批**可选**的定量转移动作，但不要求全部执行；选哪些、按什么顺序执行会共同决定终态。本工具从候选动作中选出总代价最低、能满足全部终态浓度指标并保留供料罐安全库存的完整计划，并交付现有 v1 模拟器能直接读取的计划；算不出时明确报告无解，绝不交付部分方案。

- 只做离线精确计算，不连接实验设备、不访问外部服务；全部样例为合成演练数据。
- 全部体积、质量、浓度按 `Fraction` 精确计算，任何不等号都不使用浮点容差；目标浓度闭区间两端都包含。
- 物理完全沿用 [v1 契约](contract.md)：每个动作就是一个 v1 步，读开步状态；同一步入流不能资助出流；闭步按实际状态进入下一步。因为一步只有一个转移，同名动作的先后变化会改变目标罐的终态读数，所以“选中的动作集合”本身不是答案，执行顺序也是方案的一部分。

```sh
python -m tank_transfer.recipe --input JOB --output RESULT
```

RESULT 原子新建，绝不覆盖已有文件或符号链接。JOB 不合法、物理计算失败或文件读写失败时退出 2，原因写 stderr，不产生新结果。`planned` 与 `no_plan` 都是正常业务结果，均退出 0。

## JOB

严格 UTF-8 JSON 对象，最多 524288 字节；拒绝重复键、额外字段、缺失字段、非有限数（`NaN`/`Infinity`）与非法数值类型。顶层恰好四个字段：

| 字段 | 说明 |
| --- | --- |
| `plan` | 不含步骤的 v1 初始计划：必须有 `species`、`tanks`，`steps` 必须为空数组 |
| `actions` | 1..12 个候选定量转移动作 |
| `targets` | 1..12 个终态浓度目标 |
| `reserves` | 0..12 个供料罐安全库存 |

### `plan`

`plan` 是 v1 计划去掉步骤后的形状，`species`、`tanks` 的校验完全沿用 v1（组分、罐、容量、体积、质量与数值格式）。本入口另限 **8 个组分、12 个罐**。`steps` 可省略；若出现必须是空数组，带任何步骤的 `plan` 都是非法输入（退出 2），因为初始状态必须清晰。

### `actions`

```json
{"id": "feed", "source": "B", "target": "A", "volume_l": "1", "cost": 1}
```

- `id`、`source`、`target`、`volume_l` 沿用 v1 转移规则；动作 ID 在整张 JOB 内唯一，源和目标必须是不同的已知罐。
- `cost` 是 1..10000 的 **JSON 整数**；布尔值 `true`/`false` 不是整数，拒绝。
- 每个候选动作最多执行一次：不拆分、不改量、不改向、不新增动作。可以选择不执行某些动作。

### `targets`

```json
{"tank": "C", "species": "salt", "min_g_per_l": "2", "max_g_per_l": "2"}
```

- `tank` 必须是已知罐，`species` 必须是已定义组分；每个罐最多出现一次目标。
- 两个浓度是非负十进制字符串，`min_g_per_l <= max_g_per_l`，区间两端都包含。
- 该目标罐的**最终体积必须为正**；空罐没有可交付浓度，因此不能作为目标罐的终态。

### `reserves`

```json
{"tank": "A", "min_volume_l": "1"}
```

- `tank` 必须是已知罐，每个罐最多出现一次；最小保留体积是非负十进制字符串。
- 最终体积必须**不小于**该值。保留量是配方交付条件，不是初始库存校验的替代：初始体积仍需满足 v1 的容量规则。

格式合法但无可行方案的 JOB 是正常业务结果 `no_plan`，不是输入错误。`actions`、`targets`、`reserves` 的数组顺序不参与任何优先级或平局判决。

## 最优方案与交付

在所有满足条件的“动作子集 + 执行顺序”中按以下顺序唯一选择：

1. 最小化所选动作的 `cost` 总和；
2. 总代价相同时，最小化动作数；
3. 仍相同时，把执行顺序写成动作 ID 数组，按 ID 原字符逐项比较取字典序最小者；共同前缀时较短数组在前。

成功时 RESULT 为：

```json
{"status": "planned", "total_cost": 2, "action_ids": ["feed", "draw"],
 "plan": PLAN, "final": FINAL}
```

- `total_cost` 是所选动作的代价总和（JSON 整数）。
- `action_ids` 是执行顺序，逐项对应 `plan` 的步骤顺序。
- `PLAN` 是独立 v1 计划：沿用 JOB 的初始 `species` 与 `tanks`，步骤按执行顺序命名为 `s1`、`s2`……，每步只含对应动作的一个转移，字段为 v1 的 `id`、`source`、`target`、`volume_l`。把它单独存为文件即可用原 `tank-transfer` 入口复算。
- `final` 给出每个目标罐的闭步体积、每个目标组分的精确质量与浓度，以及每个 reserve 罐的最终体积；所有值为既约分数或整数字符串。目标罐与 reserve 罐的键合并去重。形状如下：

```json
{"A": {"volume_l": "2"},
 "C": {"volume_l": "1",
       "species": {"salt": {"mass_g": "2", "concentration_g_per_l": "2"}}}}
```

零动作方案允许：当初始状态已经满足全部目标与保留量时，最优方案是空序列，`action_ids` 为 `[]`、`plan.steps` 为 `[]`、`total_cost` 为 `0`。

无解时 RESULT 恰好为 `{"status": "no_plan"}`，不附带任何部分计划。

## 合成样例

`fixtures/recipe/`：

| 文件 | 内容 | 结果 |
| --- | --- | --- |
| `scenario1-order.json` | A(2 L,0 g)、B(2 L,12 g)、C(空)；`feed` B→A 1 L cost 1、`draw` A→C 1 L cost 1；C 的 salt 恰为 2 g/L，A 至少留 1 L | `["feed", "draw"]`，总代价 2 |
| `scenario1-reserve-tight.json` | 同上，但 A 至少留 3 L | `no_plan`（动作后 A 仅余 2 L） |
| `scenario2-all-four.json` | A(2 L,0 g)、B(2 L,20 g)、C/D 空；a/b cost 2，c/d cost 1；C、D 的 salt 都恰为 5 g/L | `["a","b","c","d"]`，总代价 6 |
| `scenario2-d-zero.json` | 同上，只把 D 的目标改为 0 g/L | `["a","b","c"]`，总代价 5，不执行多余的 `d` |
| `scenario3-max.json` | 12 个动作、12 个罐、8 个组分；四组相互竞争的供料/混合/终态目标，多个同总代价候选 | `["b","d","e","f"]`，总代价 4 |

场景 1 只在先 `feed` 注入含盐液、后 `draw` 时才让 C 读到 2 g/L：先 `draw` 则 C 为 0 g/L。场景 2 的 C、D 两目标要求同罐先送低浓度液再送高浓度液，说明“可执行动作集合”不能当作可交换集合，先后顺序会改变同一目标的最终读数。

### 最大场景的固定规范结果

`fixtures/recipe/scenario3-max.json` 的固定 JOB 与预期规范结果（用于复核，不得因输入数组顺序改变）：

```json
{"status": "planned", "total_cost": 4, "action_ids": ["b", "d", "e", "f"]}
```

该场景在此 `(总代价 4, 动作数 4)` 分组内共有 12 个可行序列：固定含 `d`，`b` 或 `c` 二选一，`e` 或 `g` 二选一，且 `f`/`h`/`k` 三选一，共 2×2×3=12 种。`["b","d","e","f"]` 是按 ID 原字符逐项比较后的字典序最小者。更便宜的分组都不存在可行方案：`(3,3)` 与 `(4,3)` 各有若干子集能通过初始体积预筛，但逐一搜索后没有任何执行顺序能满足全部目标，因此不是“有更便宜方案却选了它”。

### 资源上限复核

在 Linux、CPython 3.11+ 下对 `scenario3-max.json` 给 CLI 进程 20 秒 CPU 时间与 256 MiB 虚拟内存，限制内必须返回上表的准确唯一最优结果；复核方法见下（`PYTHONPATH=src` 指向本仓源码，已安装包时可省略）：

```sh
PYTHONPATH=src python3 -c 'import resource,runpy,sys
resource.setrlimit(resource.RLIMIT_CPU,(20,20))
resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024))
sys.argv=["tank_transfer.recipe","--input","fixtures/recipe/scenario3-max.json","--output","run/recipe-max.json"]
runpy.run_module("tank_transfer.recipe",run_name="__main__")'
```

同时在 Linux、CPython 3.11+ 实测（默认资源、无外部依赖）本仓该场景约 0.15 秒完成。搜索不做浮点近似，也不把首个可行方案当答案：先按子集代价预筛，再在固定 `(总代价, 动作数)` 的分组内逐子集证明其所有排列都不可行后才进入更贵分组；只有整组被证明无解才会跳过，因此得到的胜者是全局最优解并满足平局规则。

自动测试 `tests/test_recipe.py` 覆盖两个必测场景、空罐/排空/边界取等/空 reserve/零动作/字典序平局/多组分/非整数精确浓度/供料不足/容量溢出/部分目标不可满足不交付/非法输入/已有输出（含符号链接）不覆盖，以及把交付计划送回原 v1 模拟器核对动作顺序、精确终态、代价最优性与平局规则。
