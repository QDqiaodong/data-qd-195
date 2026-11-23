# data-qd-195 · 多罐混合传输与组分守恒

每步开罐浓度决定同时转移携带的组分量，全部进出量决定闭步容量与新浓度，下一步必须使用实际更新状态。

mixing.py 以开步快照同时计算传输；service.py 将浓度、逐传输证据和全局守恒连接到连续步骤。 详细字段、单位和失败边界见 [数据契约](docs/contract.md)。全部样例为自制合成数据，许可见 [来源](fixtures/SOURCES.md)。

## 独立安装与演示

Python >=3.11，本批实际使用CPython3.12.14，运行/测试仅标准库。每仓建立独立.venv，不共享其他项目运行环境；复用公共构建缓存，锁定setuptools80.9.0/wheel0.45.1。以下命令已在本机已有uv、Python与公共缓存时实际离线执行通过；其他机器首次缺缓存时需准备依赖。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/tank-transfer --source fixtures/input.json --output run/report-initial.json
```

实际6项测试通过，演示摘要：

```json
{
  "conserved": true,
  "species": 1,
  "steps": 2,
  "tanks": 3,
  "total_mass_g": {
    "salt": "100"
  },
  "total_volume_l": "20",
  "transfers": 3
}
```

报告原子新建，不覆盖旧文件或符号链接。演示路径已存在时请改用新输出名；输入结构/约束或文件错误退出2，正常业务诊断见契约。已实际核验重复JSON键拒绝且不生成结果、同名输出字节不变及符号链接不覆盖。

## 样本发布登记册

离线样本发布登记册见 [登记册文档](docs/catalog.md)：按 v1 原模型计算逐来源证据，发布可追溯凭证，更正计划时仅撤回证据真的变了的凭证。命令入口：

```sh
python -m tank_transfer.catalog --store PATH init --revision r1 --source fixtures/catalog-r1.json
python -m tank_transfer.catalog --store PATH issue --receipt rc-draw --kind transfer --step s2 --item draw
python -m tank_transfer.catalog --store PATH revise --revision r2 --base r1 --source fixtures/catalog-r2.json
python -m tank_transfer.catalog --store PATH read --receipt rc-draw --archive
```

合成样例为 `fixtures/catalog-r1.json` 与 `fixtures/catalog-r2.json`；自动测试 `tests/test_catalog.py` 覆盖最低验收场景、限额、幂等更正重试，以及提交前/提交后的进程强制终止恢复。

## 盐浓度分级补测决策

离线补测决策工具见 [assay 文档](docs/assay.md)：容纳仅有上下界的未知初始盐质量，精确判断目标罐等级是否已确定；若未确定，在候选化验点中选出“无论读到哪一档都能判级”的最省一项。只化验员报告的档位参与决策，不拟合最可能初值、不访问设备。

```sh
python -m tank_transfer.assay plan   --input fixtures/assay/example-1.json --output run/assay-1.json
python -m tank_transfer.assay verify --input fixtures/assay/example-1.json --report run/assay-1.json
```

报告同时给出每个可达等级的完整初值见证、每个不可达等级的加权矛盾证据，以及全部候选、全部档的可达等级集合；`verify` 仅凭 JOB 与报告独立复核见证、矛盾与推荐。合成样例见 `fixtures/assay/`，自动测试 `tests/test_assay.py` 覆盖两个必测例子、观测矛盾、已有化验分级、同价候选、非零固定贡献与分数闭步浓度、非法定位与越界物理、已有报告不被覆盖，以及报告篡改被拒。既有模拟与登记册入口的输入、输出与异常契约不变。

## 泵操作工单离线排程

离线工单排程见 [dispatch 文档](docs/dispatch.md)：工单只写每笔转移的量、泵与前置（可另附送出液体的浓度门槛），不写时隙；本工具在 `tick_limit` 内求出能执行整张工单的最短排程（平局取各槽 ID 列表字典序最小者），交付现有模拟器能直接读取的 v1 计划；无解时明确报告 `unschedulable`，绝不交付部分计划。

```sh
python -m tank_transfer.dispatch --input fixtures/dispatch/scenario1-exact-2.json --output run/dispatch-1.json
```

合成样例见 `fixtures/dispatch/`；自动测试 `tests/test_dispatch.py` 覆盖三个验收场景、字典序平局规则、输入拒绝、已有输出不覆盖，以及交付计划送回原 v1 入口复核逐笔送出质量。既有模拟、登记册与化验入口的输入、输出与异常契约不变。

## 当前能力与后续集成

封闭完全混合离散模型，只检查开/闭步状态；无化学反应、泵控制或外部进料。 合理新增需求可沿现有身份和结果接入外部进料/排放、配方切换和采样记录；当前尚未实现这些扩展，不保证固定数量未来题目。
