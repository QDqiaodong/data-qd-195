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

## 当前能力与后续集成

封闭完全混合离散模型，只检查开/闭步状态；无化学反应、泵控制或外部进料。 合理新增需求可沿现有身份和结果接入外部进料/排放、配方切换和采样记录；当前尚未实现这些扩展，不保证固定数量未来题目。
