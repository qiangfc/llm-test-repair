# LLM 辅助软件测试工具

## 从 GitHub 获取项目

```powershell
git clone --recurse-submodules https://github.com/qiangfc/llm-test-repair.git
cd llm-test-repair
```

已普通克隆的仓库执行 `git submodule update --init --recursive` 获取固定版本的 boltons 测试目标。
GitHub 仓库不包含本地 `.env`、API 密钥、缓存和 `runs/` 运行产物。请复制 `.env.example` 为 `.env` 并填写自己的配置；下文历史运行报告链接需要配套的本地运行产物才能打开。

基于 CoverUp v0.6.3 的源码定位与上下文模块，补充黑盒单元测试、多轮执行反馈、缺陷分类修复、回归验证和验收报告。当前提供 Python 项目的命令行版本，LLM 使用兼容 Chat Completions 的接口。

**已于 2026-10-08 完成 DeepSeek 连接检查、演示项目验证、boltons 实测及报告证据核对。** 本次使用 Anaconda `llm-test` 环境、Python 3.11.17、DeepSeek Flash 和 `--backend local`。原始 `功能需求.md` 保留不变。

## 本次实际验收结果

- DeepSeek 连接检查通过：[连接记录](runs/connection-20261008-232239-9ec3fd/check.json)。
- 演示项目：22 个生成测试＋2 个原有测试，最终 **24/24 通过**；黑盒、白盒、合并后的行/分支覆盖率均为 **100% / 100%**。发现的两个人工注入问题均修复；四条复现记录对应两个根因，其他复现由同一补丁消除。运行进行了 2 轮生成、10 次模型调用。
- boltons：累计生成 48 个测试模块，复核排除 7 个错误用例，保留黑盒 21 个、白盒 20 个，连同原有 11 个测试，最终 **52/52 通过**。四个接口在两种方式下均具有正常、边界、异常场景记录。
- boltons 最终黑盒覆盖率：行 **72.57%（82/113）**、分支 **70%（35/50）**；白盒：行 **93.81%（106/113）**、分支 **86%（43/50）**；黑白盒合并：行 **98.23%（111/113）**、分支 **94%（47/50）**；加入原有测试的完整回归：行 **99.12%（112/113）**、分支 **96%（48/50）**。范围仅为 mathutils 模块。
- boltons 位宽边界发现：`Bits(4, 2)` 原先接受不能用 2 位表示的数值，依据固定位宽约定应抛出 ValueError。候选补丁把 `val > 2 ** len_` 改为 `val >= 2 ** len_`；修复前复现连续失败、修复后连续通过，原有测试继续通过。**这是一项按本地接口约定确认并验证的发现，尚未获得上游维护者确认。**
- boltons 运行包含 3 轮初始生成和 2 个复核后补充阶段，共 22 次模型调用。模型曾写错二进制索引、十六进制前导零和未明确约定的零位字符串预期；这些错误用例已排除。对应 6 条候选记录中，1 条保留为已修复发现、5 条为测试错误。5 次候选修复尝试只有 1 次验证通过，其余差异与失败原因均保留。
- 工具自身 **11 项确定性测试通过**。最终证据核对覆盖源码/测试完整性、黑白盒输入分离、真实合并覆盖率、复现前后、最终回归和密钥未进入产物；演示项目 59 项检查通过，boltons 检查结果见其 `audit.json`。

报告入口：

- [演示项目 HTML](runs/20261008-232537-fd526567/report.html)、[JSON](runs/20261008-232537-fd526567/report.json)、[证据核对](runs/20261008-232537-fd526567/audit.json)。
- [boltons HTML](runs/20261008-232540-12db4984/report.html)、[JSON](runs/20261008-232540-12db4984/report.json)、[证据核对](runs/20261008-232540-12db4984/audit.json)、[最终补丁](runs/20261008-232540-12db4984/patches/final.patch)。
- [验收记录与限制](docs/验收记录.md)。`runs/` 已被 Git 忽略；需要交作业或迁移时请单独保留报告和证据。

最初的运行 `runs/20261008-232253-36ed50d4/` 因模型输出截断而未得到有效测试，保留为配置问题记录，不作为成功验收证据。上述覆盖率是最终测量值；补充生成与修复前的测试集合/源码版本可能不同，不能将两者直接当作相同条件下的模型能力对比。

## 已编写的功能

- 黑盒生成器只接收需求、接口签名、参数/结果约定及本模式的执行反馈，不接收目标源码、源码路径、覆盖位置或调用栈。
- 白盒生成器使用 CoverUp 定位未覆盖函数、方法和类，读取源码上下文，结合缺失行、分支和报错信息补充用例。两种方式的正确预期均以接口约定为依据。
- 每个用例记录测试方式、需求编号、正常/边界/异常场景、设计理由、输入、预期和实际结果；保留完整源码及重复执行记录。
- 按最大轮数、覆盖率目标、连续无提升轮数停止生成；记录每轮可运行测试数、行/分支覆盖率和 LLM 调用数。失败不直接等同于程序缺陷，错误测试、环境问题与不稳定用例会被排除。
- 分别执行原有测试、黑盒测试、白盒测试、黑白盒合并、完整回归测试，实际测量各自覆盖率。合并覆盖率不是两个百分比的平均值。
- 按边界、空值/类型、异常处理和逻辑四类使用不同修复策略；需求原文证据和置信度不足时保留为待审查候选。支持人工复核。
- 候选补丁只修改相关源文件；复现测试修复前连续失败、修复后连续通过，原先通过的测试继续通过，且没有新增失败或减少测试时才保留补丁。否则还原候选改动。
- 输出 HTML、JSON、Markdown 报告，完整记录缺陷证据、分类、补丁差异、修复尝试数和各类尝试验证通过率。

## 安装与日常启动（Anaconda）

环境和依赖本次已安装，后续在 Anaconda Prompt 中只需：

```bat
conda activate llm-test
cd /d D:\check_fix
```

在新机器首次安装时执行：

```bat
conda create -n llm-test python=3.11 -y
conda activate llm-test
cd /d D:\check_fix
python -m pip install -e .
```

当前依赖为 pytest、coverage、python-dotenv；本次安装版本分别为 8.4.2、7.16.2、1.2.4。`pip install -e .` 自动安装依赖，无须重复安装 requirements.txt；直接执行 main.py 时也可用 `python -m pip install -r requirements.txt`。

本目录 `.env` 已配置。新机器可复制 `.env.example` 后填写自己的密钥；本次 DeepSeek 设置如下：

```dotenv
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=你的密钥
LLM_MODEL=deepseek-flash
LLM_MAX_TOKENS=12000
LLM_EXTRA_BODY={"thinking":{"type":"disabled"},"response_format":{"type":"json_object"}}
```

`LLM_BASE_URL` 必须包含供应商要求的 `/v1` 等前缀。程序在其后添加 `/chat/completions`；也可以用 `LLM_CHAT_ENDPOINT` 直接指定完整接口地址。支持 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL` 别名。已有环境变量优先于 `.env`。原生 Responses、Anthropic 等非兼容协议尚无适配器。

DeepSeek 使用 OpenAI 兼容格式，实际请求地址为 `https://api.deepseek.com/chat/completions`，不使用 `/anthropic`。本次关闭思考模式并开启 JSON 输出，避免思考消耗输出预算导致 JSON 截断。其他供应商的额外参数需按其文档调整；参考 [DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。

其他参数见 `.env.example`：超时、重试、最大调用次数、最大 Token 预算以及供应商额外请求字段。默认调用预算 60 次，失败 HTTP 请求也计数；供应商不返回 Token 用量时按保守估算计费。密钥不写入运行配置和报告，不传入测试容器。LLM 调用记录包含所测源码、需求和结果，请按项目资料管理这些记录。

**本次使用本地模式，不需要 Docker。** 在以下 run 命令中保留 `--backend local`，测试默认使用启动工具的 Anaconda Python。若被测项目需要其他环境，可在项目 JSON 中设置 `"local_python": "D:/Anaconda/envs/target-env/python.exe"`，该环境必须同时安装 pytest、coverage 与目标项目依赖。`python_paths` 仅表示目标项目内的模块搜索目录。

示例项目 JSON 的默认 backend 仍为 docker，命令行参数会覆盖它。Docker 是可选的隔离执行方式，本次没有验收 Docker 路径。需要时先启动 Docker Desktop，再构建镜像：

```powershell
docker build -t llm-test-runner:0.1 -f docker/Dockerfile.runner .
```

测试容器关闭网络、只读挂载源码、限制资源，不包含 LLM 密钥。目标项目如需第三方依赖，请在派生镜像中预先安装，并修改项目配置的 `docker_image`。当前准备的 boltons 数学模块和演示项目不需要额外目标依赖。

若暂时不用 Docker，可显式传入 `--backend local`。本地模式为每次执行创建独立副本并检查 Python 文件是否被改写，但**不是安全沙箱**；只对可信目标和可信生成代码使用。不会自动从 Docker 降级为本地执行。

## 真实开源目标

已在 `targets/boltons` 准备 boltons **25.0.0** 的原始源码，版本固定为 [`c23dbdadb6fecdf505eb4231559561913b84c13f`](https://github.com/mahmoud/boltons/tree/c23dbdadb6fecdf505eb4231559561913b84c13f)（点击查看本次固定版本的完整源码），保留原许可证。测试对象是 `boltons/mathutils.py`，原有回归集是 `tests/test_mathutils.py`。

`examples/boltons-contracts.json` 定义 clamp、ceil、floor 和 Bits 的接口约定。验收中澄清了索引方向、十六进制前导零及零位字符串格式未约定的边界；补充阶段保存了单独的约定快照，最初配置快照保持原样。原始 boltons 文件未修改，已验证补丁仅保存在运行工作副本中。

```powershell
python main.py run --config examples/boltons-project.json --env .env --backend local
```

若先观察生成与覆盖率而不自动修复：

```powershell
python main.py run --config examples/boltons-project.json --env .env --backend local --no-repair
```

需要人工确认后再修复时，将配置 `confirmation_policy` 改为 `manual`。默认 `llm` 使用需求原文、重复复现及置信度阈值进行机器确认，报告会明确区分机器确认和人工确认。

## 演示项目

`examples/demo_project` 是含人工注入边界与空序列缺陷的教学样例，仅演示流程，不能替代真实开源目标的缺陷发现验收。

```powershell
python main.py run --config examples/demo-project.json --env .env --backend local
```

## 接入自己的 Python 项目

可以从已有配置复制，也可以先生成骨架：

```powershell
python main.py init --project D:\my_project --source src --tests tests --python-path src --output configs\my-project\project.json
```

`init` 只读取公开符号与签名，**不会从实现推测正确预期**。生成的 `contracts.json` 必须先填写 `description`、`expected_behavior` 及正常/边界/异常场景，才能运行。每个约定包含 id、module、symbol、signature、source_file；source_file 仅供白盒分析和修复定位。项目目录相对配置文件解析，源码/测试/模块搜索路径相对目标项目解析；只支持目录或文件，不支持 pytest 节点选择语法。

配置可调整 `max_rounds`、`max_tests_per_round`、`stagnation_rounds`、`target_line_coverage`、`target_branch_coverage`、`repeat_tests`、`timeout_seconds`、`max_repair_attempts`、`max_changed_lines`。需要 pytest 插件时先装进执行环境，再在 `pytest_plugins` 指定模块名；默认关闭第三方插件自动加载。

## 报告、复核与继续修复

每次运行创建 `runs/日期时间-编号/`，包括：

- `report.html` / `report.json` / `report.md`：验收报告及完整数据。
- `state.json` / `config.snapshot.json`：状态与不含密钥的配置快照。
- `workspace/`：包含生成测试和已通过验证修复的工作副本；原项目不会自动改写。
- `original_sources/`：源文件原始快照与可对照的版本指纹。
- `generated/`：全部生成用例，排除的错误/不稳定用例也保留。
- `executions/`：每次执行的输入、stdout/stderr、测试结果和 coverage.py 原始报告。
- `llm_calls/`：生成、判断、修复各次调用及用量。
- `patches/缺陷编号/`：候选差异和验证记录；`patches/final.patch` 为已保留修复的累计差异。

查看报告后可人工记录决定：

```powershell
python main.py review --run runs\本次目录 --defect D001 --decision confirm --reason "违反接口约定中指定的边界行为"
python main.py repair --run runs\本次目录 --env .env
python main.py report --run runs\本次目录
```

人工决定包括 confirm、test_error、environment、uncertain。人工排除错误用例后旧覆盖率会标记过期，继续 repair 才会重新测量；report 只重建展示，不执行测试。继续 repair 会沿用该次记录的调用/Token 预算，不覆盖旧调用日志。中断的生成流程目前不能从半轮续生，可新建 run；已保留候选可继续审查和修复。

人工复核时可以追加 `--category boundary` 等选项修正分类。合并执行中首次出现的失败会重复执行并额外审查，相关模型调用单独记入报告。被先前验证通过的补丁同时消除的其他候选记录会标记 `resolved_by_other_patch`，不会伪造新的独立修复尝试。

助手代为复核时使用 `--reviewer assistant` 记录实际来源，默认 human 供用户自行复核。排除错误用例后如果缺少需求场景，可以补充生成；补充阶段独立保存源码和约定快照，继续使用原运行的调用预算：

```powershell
python main.py supplement --run runs\本次目录 --env .env --rounds 1 --contracts-file examples/boltons-contracts.json
```

supplement 会补齐缺少场景的测试方式，然后继续修复已确认候选并重新测量完整回归。`--contracts-file` 可省略以沿用原约定；传入时需保留原需求编号。每个新增阶段有独立记录，不能与初始生成轮数混淆。

执行记录的 input、expected、actual 同时保存最终值和 `property_history`，循环内多组输入不会仅剩最后一组。报告区分“生成时失败”与“最终回归通过”，流程状态 completed 仅表示流程结束。

复用本次检查入口：

```powershell
python scripts/check_llm.py
python -m pytest tests -q -p no:cacheprovider
python scripts/audit_run.py runs/20261008-232540-12db4984
```

连接检查会调用模型；pytest 和 audit 不调用模型。audit 按本次严格验收标准检查最终全部通过、无未解决候选、场景完整、覆盖率集合、补丁与源文件一致性等；尚有候选的研究运行可能不通过这项最终检查。

完整回归允许原项目原有失败继续存在，但任何原先通过的测试都必须继续通过，不能引入新的失败。零个可测分支显示“不适用/无可测数据”；没有覆盖数据不会伪造 100%。相同根因可能对应多条候选记录，分类数量以记录为单位，不宣称是去重后的根因数量。修复前后源码可能发生变化，报告同时保留源码指纹和两阶段覆盖率。

本次已验证 DeepSeek Flash、Anaconda 本地执行、两个目标的生成与回归，以及边界和空序列异常处理修复；未验证 Docker、其他模型、复杂外部依赖目标、多语言与跨进程覆盖归并。空值/类型和逻辑分类策略已有代码，但本次没有真实缺陷样本证明它们的端到端修复效果。当前没有 Web 界面，也未进行依赖漏洞扫描。测试通过不代表所有软件均可自动测对，模型判断与接口解释仍需复核。

CoverUp 的复用范围和许可证说明见 `THIRD_PARTY_NOTICES.md`；功能与代码入口对应关系见 `docs/功能实现说明.md`。
