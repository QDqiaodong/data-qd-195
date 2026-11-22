# 盐浓度分级补测决策（离线）

本工具回答两个问题：目标罐的等级在现有记录下是否已经确定；若未确定且预算只允许再化验一次，哪一个候选定位能保证无论读到哪一档都能判级。

- 只做离线精确计算，不连接实验设备、不访问外部服务、不拟合“最可能”的初始质量。
- 全部质量与浓度按 `Fraction` 精确计算，不以容差解释任何不等号。
- 复用仓库 v1 物理模型：同步转移读开步状态，入流不资助同一步出流，闭步混合，下一步才看到结果。
- 本批样例均为自制合成数据，许可见 [fixtures/SOURCES.md](../fixtures/SOURCES.md)。

```sh
python -m tank_transfer.assay plan   --input JOB --output REPORT
python -m tank_transfer.assay verify --input JOB --report REPORT
```

`plan` 原子新建报告，绝不覆盖已有文件或符号链接；`verify` 只读取 JOB 与报告，不改写任何文件。业务输入不合法、文件读写失败或验证不通过时退出 2，原因写 stderr、stdout 为空。合法但观测本身矛盾是业务结果：`plan` 仍写出 `inconsistent` 报告并退出 0。

## JOB

严格 UTF-8 JSON 对象，最多 524288 字节；拒绝重复键、额外字段、缺失字段、非有限数（`NaN`/`Infinity`）与非法数值类型。顶层恰好六个字段：

| 字段 | 说明 |
| --- | --- |
| `plan` | 现有 v1 计划；字段、物理约束与数值规则完全沿用 [v1 契约](contract.md) |
| `species` | 从 `plan.species` 中选一个组分，只为它估计初始质量 |
| `unknown` | 1..4 条未知初值 |
| `observations` | 0..8 条已有化验 |
| `target` | 目标定位与阈值 |
| `probes` | 0..6 条候选补测 |

在 v1 之外另加限制：罐 ≤16、组分 ≤8、步骤 ≤48、转移合计 ≤96。ID 沿用仓库规则 `[A-Za-z0-9_:-]{1,64}`；十进制字符串沿用仓库 `number` 格式。

### `unknown`

```json
{"tank": "A", "min_g": "0", "max_g": "10"}
```

- 1 至 4 条，不重复列罐。
- 罐必须在计划中，初始体积为正，且所选组分在计划中必须填 `"0"` 作占位。
- `0 <= min_g <= max_g`；实际初始质量可取两端点在内的任意有理值。
- 未列罐的所选组分质量固定为计划中的值。
- 所有记录共用同一组未知初值，不能为每个罐样单独挑一组数。
- 其它组分的初值全部固定，不参与估计。

### 定位 `{step,tank}`

指现有步 ID 的**闭步罐**（该步结束后的库存）。步必须存在、罐必须存在且闭步体积非零。目标、化验、候选都使用该定位；候选定位不得与目标相同。

### `observations`

```json
{"id": "o1", "at": {"step": "s1", "tank": "C"},
 "min_g_per_l": "5", "max_g_per_l": "5"}
```

每条是两端包含的浓度约束：`min_g_per_l <= 浓度 <= max_g_per_l`，`0 <= min_g_per_l <= max_g_per_l`，ID 唯一，定位可重复，所有化验必须同时成立。

### `target`

```json
{"at": {"step": "s2", "tank": "D"}, "limit_g_per_l": "5"}
```

浓度 `<= limit_g_per_l` 判为 `low`，否则判为 `high`；阈值非负。

### `probes`

```json
{"id": "pB", "at": {"step": "s1", "tank": "B"},
 "cut_g_per_l": ["5"], "cost": 3}
```

- ID 唯一；`cost` 是 1..10000 的 JSON 整数（不是字符串）。
- `cut_g_per_l` 是 1..3 个严格递增的非负切点。切点从小到大把读数分成按 0 起编号的档；**等于切点归右边档**。有 `k` 个切点就有 `k+1` 档。
- 化验员只报告读数所在档，不报告精确数值；在知道读数之前必须选定候选，且只允许选一项。
- 判定与推荐不得依赖 `observations` 或 `probes` 在输入数组中的先后；实现按 ID 排序处理。

## 决策规则

记目标等级集合为 `{low, high}`。一个等级**可达**，表示存在一套未知初值，同时满足全部上下界、全部化验及该等级条件。

1. 未补测时两等级都不可达：`inconsistent`。
2. 只有一个可达：`classified`，给出该等级；不输出候选分析。
3. 两个都可达：逐个候选、逐个档给出精确的可达等级集合。
   - 空集表示不可能读到该档，不影响候选合格。
   - 两个等级都可达表示即使读到该档仍不能判级。
   - 仅当候选的**每一个可达档**都能唯一判级时，该候选合格。
4. 在合格候选中按 `cost` 最小、再按 ID 字典序选一个：`probe`，给出候选 ID 与“档 → 等级”的完整对应。
5. 没有合格候选：`unresolved`。

`inconsistent` 与 `classified` 不输出候选分析。不能因找到首个合格点而省略其他候选及其所有档。

## 报告格式（机器可读）

报告本身是 UTF-8 JSON 对象（`plan` 以 canonical 键序、缩进 2、结尾换行原子写出）。顶层字段固定为：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema` | `"tank-transfer-assay/1"` | 格式版本 |
| `job_fingerprint` | `"sha256:<hex>"` | 解析后 JOB 的 canonical JSON 摘要；与排版/键序无关 |
| `species` | 字符串 | 所选组分 |
| `unknown_tanks` | 字符串数组 | 未知罐，升序 |
| `target` | `{at,limit_g_per_l}` | 与 JOB 一致，阈值为既约分数/整数字符串 |
| `baseline` | `{low:证据,high:证据}` | 未补测时两等级的精确证据 |
| `baseline_levels` | 字符串数组 | 可达等级，固定按 `low,high` 顺序 |
| `status` | `inconsistent\|classified\|probe\|unresolved` | 结论 |
| `classified_level` | `low\|high\|null` | 仅 `classified` 非空 |
| `candidates` | 候选数组 | 仅 `probe`/`unresolved` 非空，按 ID 升序 |
| `recommendation` | `{probe,bucket_levels}\|null` | 仅 `probe` 非空 |

候选对象：

```json
{"id": "pB", "cost": 5,
 "at": {"step": "s1", "tank": "B"},
 "cuts_g_per_l": ["5"],
 "qualified": true,
 "buckets": [
   {"bucket": 0, "reachable": true, "reachable_levels": ["high"],
    "levels": {"low": {"reachable": false, "proof": {"...": "..."}},
               "high": {"reachable": true, "witness": {"masses_g": {"A": "10", "B": "0"}}}}},
   {"bucket": 1, "reachable": true, "reachable_levels": ["low"], "levels": {"...": "..."}}
 ]}
```

- `buckets` 必须恰好覆盖 `0..len(cuts)` 且按序；`reachable_levels` 是 `levels` 中被证据判为可达的等级，固定 `low,high` 顺序、无重复。
- `reachable` 当且仅当 `reachable_levels` 非空。
- `qualified` 当且仅当所有档的 `reachable_levels` 长度都不等于 2。
- `recommendation.bucket_levels` 恰好覆盖 `0..len(cuts)`：唯一可达等级写该等级，空档写 `null`。

### 证据：见证与矛盾

每个被判为可达的等级附**见证**，完整给出每个未知罐的初始质量（既约分数或整数字符串）：

```json
{"reachable": true, "witness": {"masses_g": {"A": "5", "B": "5"}}}
```

每个被判为不可达的等级附**矛盾证据**：对原始约束的非负权重组合。

```json
{"reachable": false,
 "proof": {
   "weights": [
     {"constraint": {"ref": {"kind": "observation_min", "id": "o1"},
                     "terms": [{"tank": "A", "coefficient": "-1/2"},
                               {"tank": "B", "coefficient": "-1/2"}],
                     "rhs": "-5", "strict": false},
      "weight": "1"},
     {"constraint": {"ref": {"kind": "target_low"},
                     "terms": [{"tank": "A", "coefficient": "1/2"}],
                     "rhs": "5/2", "strict": false},
      "weight": "1"},
     {"constraint": {"ref": {"kind": "bucket_max", "probe": "pB", "bucket": 0},
                     "terms": [{"tank": "B", "coefficient": "1/2"}],
                     "rhs": "5/2", "strict": true},
      "weight": "1"}
   ],
   "sum_rhs": "0",
   "strict": true}}
```

每条约束行统一写成未知**质量**的线性式：

```
sum(coefficient * x_tank) <= rhs      (strict=false)
sum(coefficient * x_tank) <  rhs      (strict=true)
```

固定质量贡献已移到常数侧。`terms` 只列非零系数，按 `unknown_tanks` 顺序。权重必须非负、至少一条为正；`strict=true` 的约束权重必须为正。权重组合相加后所有未知系数必须恰好为 0，`sum_rhs` 必须等于按权重求和的 `rhs`，并出现：

- 全为非严格约束：`0 <= sum_rhs` 且 `sum_rhs < 0`（即 `0 <= 负数`）；
- 至少一条严格约束：`sum_rhs <= 0`（即 `0 < 0`）。

### 约束引用规则

每条证据只能引用**当前结论作用域内**的原约束：

| 作用域 | 可用约束 |
| --- | --- |
| `baseline.<level>` | 每个未知量的上下界 + 每条已有化验的两侧 + 目标该等级的一侧 |
| 候选某档某等级 | 同上 + **该候选该档**的档边界 |

- 不允许把来自另一等级、另一档或另一候选的约束用于当前结论。
- 每个未知量的固定上下界都必须可被引用。
- 引用由 JOB 唯一决定，`verify` 会按引用重建原约束并逐项比对。

引用种类与对应的原始约束如下（`mass` 为定位闭步罐中该组分的质量，`V>0` 为其闭步体积，`mass = const + sum(c_j x_j)`）：

| `ref` | 原约束 |
| --- | --- |
| `{"kind":"unknown_min","tank":T}` | `-x_T <= -min_g` |
| `{"kind":"unknown_max","tank":T}` | `x_T <= max_g` |
| `{"kind":"observation_min","id":I}` | `-(mass) <= const - min_g_per_l*V` |
| `{"kind":"observation_max","id":I}` | `mass <= max_g_per_l*V - const` |
| `{"kind":"target_low"}` | `mass <= limit_g_per_l*V - const` |
| `{"kind":"target_high"}` | `-mass < const - limit_g_per_l*V` |
| `{"kind":"bucket_min","probe":P,"bucket":b}` | `-(mass) <= const - cut[b-1]*V` |
| `{"kind":"bucket_max","probe":P,"bucket":b}` | `mass < cut[b]*V - const` |

第 `b` 档表示 `cut[b-1] <= 浓度 < cut[b]`（首档无下界、末档无上界；`cut` 处归右档），因此下边界是非严格、上边界是严格。

## 独立核验

`verify` 只用 JOB 与报告，不导入 `plan` 的决策逻辑、可行性搜索或优化器，也不以“重新生成报告后比较”来核验。它：

1. 校验 JOB；校验报告字段、schema、指纹、组分、未知罐、目标与 JOB 一致。
2. 按引用从 JOB 重建原约束，直接检查每个见证是否精确满足作用域内全部约束，并逐项检查每个加权组合是否真的消去所有未知量并产生矛盾。
3. 从**核验过的事实**推出每个等级、每个候选、每一档的可达集合，检查报告的可达集合、`reachable`、`qualified`、状态与分类是否与之逐一对应。
4. 检查候选覆盖恰好是 JOB 的候选集合；推荐必须来自合格候选，且按 `cost` 最小、再按 ID 字典序确为最小；`bucket_levels` 必须从已核验的可达集合推出。
5. 被推荐的候选之外的所有候选与所有档同样核验；空档由证据支持，不能仅凭缺失视为不可能。

`verify` 不修改 JOB 或报告，有效时退出 0 并在 stdout 输出核验摘要，不需要联网或保存跨次状态。

## 样例

```sh
python -m tank_transfer.assay plan --input fixtures/assay/example-1.json --output run/assay-1.json
python -m tank_transfer.assay verify --input fixtures/assay/example-1.json --report run/assay-1.json
```

- `fixtures/assay/example-1.json`：只有 salt，A、B 各 1 L 且未知初值 x、y 均在 `[0,10]` g；s1 C 闭步恰为 5 g/L。便宜候选 `pA` 只能读到兼容两等级的一档，必须改选较贵但可判级的 `pB`；B 第 0 档“目标不为 low”的严格边界证明可独立核验。
- `fixtures/assay/example-2.json`：同一笔 C 化验改为 4.5 g/L，两候选都不合格，结果为 `unresolved`；拿 example-1 的报告核验 example-2 因指纹不符被拒绝。
- `fixtures/assay/inconsistent-observations.json`：多条化验各自可能、共同矛盾 → `inconsistent`。
- `fixtures/assay/classified-before-probe.json`：已有化验足以分级 → `classified`，不输出候选分析。
- `fixtures/assay/equal-cost-probes.json`：两个合格候选同价，按 ID 字典序选取。
- `fixtures/assay/fixed-contribution.json`：存在非零固定质量贡献（另罐固定盐），并出现分数闭步浓度。
- `fixtures/assay/fractional-closing.json`：闭步体积为分数、切点为分数，输出一律既约分数。

这些样例与 `tests/test_assay.py` 一起覆盖上述场景，以及非法定位（含空罐）、末步非法物理转移不产出报告、已有报告字节不被覆盖、报告被篡改时 `verify` 拒绝等验收点。

## 与既有入口的关系

本工具的 `plan`/`verify` 是独立子命令，复用 v1 的字段/物理校验与 `Fraction` 规则，但不改变 `tank-transfer`、`service.process` 或样本登记册的输入、输出与异常契约。`plan` 复用仓库的原子新建发布（临时文件 → fsync → `os.link`），因此已有目标或符号链接一律拒绝，绝不覆盖字节。

## 范围

仅本机本地文件系统；不并发写同一报告路径；不连接实验设备或外部服务；不重构既有 `tank-transfer` 与登记册入口及其数据协议。
