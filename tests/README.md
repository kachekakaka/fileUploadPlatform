# 测试运行入口

选测、阶段、隔离与证据规则见[测试规范](../docs/测试/README.md)，业务要求见[需求](../docs/需求说明.md)。原实施记录见[Core MVP 实施结果](../archive/方案/20261116-Core-MVP实施方案.md#产品实施结果)，最新补验见[审查修复结果](../archive/方案/20261116-Core-MVP审查修复.md#实际结果)。

## 环境和安装

Bilibili 本地验证分为三个独立层次：Core 的独立 Python 协议 Fixture、私有引擎仓库的真实 Rust + HTTP 服务测试，以及本仓库的跨仓库联调。各轮结果、历次失败与真实平台验收范围见[集成实施记录](../archive/方案/20261116-Bilibili集成与自维护方案-v1.2.md#23-产品实施记录2026-11-17)。

本次环境为 Windows、PowerShell 7、Python 3.11.1。完整测试依赖 Windows Job Object、WMI、本机 SQLite、loopback HTTP 和创建 junction 的能力，不以跨平台 Mock 替代这些前提。依赖范围在 [pyproject.toml](../pyproject.toml)，精确版本在 [requirements-dev.lock](../requirements-dev.lock)。

从仓库根目录运行，环境放仓外，安装见[运行说明](../docs/operations/core-mvp.md#安装与启动)。开发安装可用 `python -m pip install -e ".[test]"`；复用本次版本用锁文件安装后执行 `python -m pip install --no-deps -e .`。

## 命令和覆盖

以下 python 指所选虚拟环境解释器。每轮建立新证据目录；pytest 默认在 `../fileUploadPlatform_tmp/core-mvp-tests/<随机UUID>/` 保留数据库、合成作品、执行结果、进程日志和 CLI 记录，不清除旧轮次。可用 `UPLOAD_PLATFORM_TEST_ROOT` 指定其他仓外父目录。

```powershell
$evidenceRoot = Join-Path (Split-Path (Get-Location) -Parent) 'fileUploadPlatform_tmp'
$testRun = Join-Path $evidenceRoot ('core-mvp-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $testRun | Out-Null
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python -m pytest tests -q --junitxml="$testRun/results.xml" *> "$testRun/pytest.log"
$LASTEXITCODE
```

退出码 0 且断言通过才表示该轮通过；同时检查日志异常与警告。Core 本机范围见下表。Dashboard 浏览器与 Rust 协议测试使用本文各自的环境配置，真实平台验收另记。

| 范围 | 命令 | 实际覆盖 |
| --- | --- | --- |
| 单元 | `python -m pytest tests/unit -q` | 状态图、聚合、元数据、YAML、路径、准备与写入失败、重试配置、时间/DST |
| SQLite/文件/并发 | `python -m pytest tests/integration -q` | 真库重开、进程/连接并发初始化、中断、约束、事务回滚、幂等/丢响应、轮次、租约、派发接管、迟到、junction；真实写锁等待跨越过期/续租/恢复/重试/规则计划点 |
| 契约 | `python -m pytest tests/contract -q` | 实际 FastAPI 处理链、来源、错误、分页、版本、请求日志、IPC/JSON Schema；PATCH 省略/null 区分与内部默认值规范化 |
| Windows 故障 | `python -m pytest tests/recovery -q` | 真宿主成功/失败/取消/超时、启动与退出核验前后的 Worker 崩溃、Job 丢失但子进程存活、持久退出证据恢复、身份不符不误杀、恢复竞争、人工处置、取消/成功竞争、真实旧代次迟到结果拒写 |
| 实际入口 | `python -m pytest tests/e2e -q` | CLI → HTTP API → 独立 Worker → Fake Host → 查询/重启；运行中取消及迟到成功、周期管理、SSE 续接、重放、服务不可达不写库 |

时间边界用可控 Clock 和真实 SQLite，不等待整天；进程/HTTP 用真实时间与子进程。媒体均为最小合成字节，Fake 不解析视频内容。新增账号只在测试库使用，不接触真实账号。

## Bilibili 独立引擎与跨仓库检查

Core 新增 `test_bilibili_database.py`、`test_bilibili_runtime.py`、`test_bilibili_crashes.py`、`test_account_maintenance.py`、`test_bilibili_queries.py` 和 `test_bilibili_cli.py`，覆盖旧库只读拒绝、交接取消、身份/结果验证、实际 Worker 崩溃、账号维护、版本提交回滚、迟到查询、三个账号名额及 REST/CLI 入口。`tests/helpers/bilibili_fixture.py` 是独立进程，只生成合成协议，不导入 Adapter，不访问 B 站。

跨仓库测试 `tests/integration/test_bilibili_real_engine.py` 需要先在独立引擎仓库构建 `cargo build --locked -p bilibili-upload-engine --features protocol-fixture`，再设置：

```powershell
$env:BUE_TEST_EXECUTABLE = '<fixture EXE 的绝对路径>'
$env:BUE_HTTP_TEST_MODULE = '<独立引擎仓库>/tests/test_maintenance.py'
python -m pytest tests/integration/test_bilibili_real_engine.py -q
```

该项运行真实 Core、真实 Rust、SQLite、Windows Job 和本地 HTTP，核验登录协调、凭据引用、两 P 投稿及审核查询；不访问真实平台。缺少上述环境时明确跳过，完整集成验证必须提供环境并实际通过，不能以其余用例代替。

引擎 Rust 测试使用 `cargo test --locked -p biliup -p bilibili-upload-engine`。本机系统代理可能干扰 loopback，测试进程应设置 `NO_PROXY=127.0.0.1,localhost`；真实网络线路探测测试保持其原有 ignored 状态。引擎 HTTP 测试在引擎仓库运行 `python -m pytest tests -q --basetemp=<本轮唯一仓外目录>`，避免复用或清理其他轮次。生产构建不启用 `protocol-fixture`，不可用合成登录/投稿测试声明真实平台验收通过。

`integration/test_lock_time_boundaries.py` 在第二连接的 BEGIN IMMEDIATE 实际等待时推进 Clock；`recovery/test_review_boundaries.py` 用真实宿主输出、SQLite 接管及提交屏障覆盖审查缺陷。取消测试在取消前取得宿主/子进程句柄，结束后直接核验操作系统退出状态，不以再次调用被测恢复函数作为退出证明。late_result 按 duration_seconds 产生结果，分别验证当前有效结果可由恢复者采用、错误代次拒绝、旧执行者不能提交或覆盖后继任务。

`helpers/worker_once.py` 只供故障测试注入强制退出。缺少 PID 的启动窗口在产品侧保守阻塞；测试用独立证据回收自己创建的挂起进程，这种夹具清理不算产品自动恢复通过。保留失败数据库与证据，不按名称终止或删除未知临时目录。

## B18 受控入口本地模拟

在上文的测试虚拟环境中运行。使用空的仓外临时目录，每次另取新目录名。

```powershell
python -m pytest tests/integration/test_b18_admission.py tests/e2e/test_b18.py -q --basetemp="$testRun/b18"
```

测试自行生成引擎配置、账号、作品与清单，使用独立 SQLite、Windows Job 和模拟引擎。端到端用例经过实际 CLI、Server 和 Worker，在 `simulated-platform/` 留下两份模拟稿件，再按号读取并与原请求核对。预期一个单 P 任务和一个两段的多 P 任务，各只交接一次。另检查换账号、改素材或配置、并发重放、定时与周期派发、重启、交接前撤下清单，以及单 P 回查失败时的拒绝。模拟端和受控入口共用的仅是协议，运行无需真实账号。[B18 验收结果](../archive/方案/20261116-Bilibili集成与自维护方案-v1.2.md#237-b18-受控入口本地验证2026-11-18)

## 契约再生成

```powershell
$env:UPLOAD_PLATFORM_DATA_DIR = Join-Path $testRun 'contract-export'
python tools/export_contracts.py
```

生成 `schemas/`，不初始化业务库；生成后检查契约差异，文件存在不证明行为正确。本轮 Starlette TestClient 对 httpx 有弃用提示；实际 HTTP 另由独立 server/CLI 测试验证，后续升级客户端依赖需按影响范围验证。

## Dashboard 与统一投稿

后端覆盖独立和网页入口、Windows junction、双路径、配置版本、回执查回、并发、事务回滚、旧入口共用提交记录，以及按 MuMu 约定生成的单片和跨 Job 多 P YAML。

```powershell
python -m pytest tests/integration/test_unified_submissions.py -q
npm --prefix web ci
npm --prefix web run build
python -m pip install playwright
$env:UPLOAD_PLATFORM_TEST_CHROME = '<本机 Chrome 可执行文件的绝对路径>'
python -m pytest tests/e2e/test_dashboard.py -q
```

浏览器用例启动独立 API 和 Worker，使用本机 Chrome、合成素材与 Fake 目标。保存概览和任务截图；浏览器未配置时显式跳过。原生目录验证需由交互式 Windows 会话启动 API，分别选择允许目录和取消，再检查页面是否回填、是否创建作品。真实 Bilibili 投稿仍走授权的独立验收流程。

### Windows 联调包验收

使用上文的测试环境、已构建的 ZIP 和实际 MuMu 工作区。在测试环境另外安装 MuMu 仓库 `requirements.txt`，使 Python 能正常导入实际客户端。验收使用 MuMu 客户端固定连接的本机端口 8765，输出放在本轮仓外目录。

```powershell
$env:UPLOAD_PLATFORM_TEST_CHROME = '<Chrome 可执行文件的绝对路径>'
$env:UPLOAD_PLATFORM_TEST_ZIP = '<Windows ZIP 的绝对路径>'
$env:UPLOAD_PLATFORM_TEST_MUMU = '<MuMu 仓库的绝对路径>'
python -m pytest tests/e2e/test_windows_package.py -q -s
```

用例解压后用包内 Python 直接运行 API 与 Worker，将进程纳入测试的 Windows Job，并使用 MuMu 的 `UploadPlatform` 客户端、清单生成器与两段合成素材。测试执行器只产生模拟投稿。首次受理后丢弃 HTTP 回执，核对同库重启、原键查回和原请求重放。换新库时用新库中的部署编号确认 API 已切换，再检查旧部署的请求返回 409。浏览器回执恢复由 `tests/e2e/test_dashboard.py` 覆盖。

### MuMu 便携版验收

在有交互桌面的 Windows 测试用户下，用空闲的本机 8765 端口和同一份候选 ZIP。用例在仓外独立解压目录和用户数据目录中验证，双击入口的进程环境只保留 Windows 系统路径。测试文件夹会自动加入中文与空格。Windows 默认路径长度下，从仓外选择较短的 `--basetemp`，留出解压依赖许可目录的空间。候选 ZIP 同时可用于上一节的 MuMu 客户端隔离验包。

```powershell
$env:UPLOAD_PLATFORM_TEST_PORTABLE_ZIP = '<Windows 便携 ZIP 的绝对路径>'
python -m pytest tests/e2e/test_windows_portable.py -q -s --basetemp '<较短的新仓外目录>'
```

故障夹具覆盖端口已被占用、引擎能力文件缺失或不符、Worker 启动失败、旧库和原库缺失。本用例与真实投稿、B 站传输异常分别记结果。[实机通过条件](../docs/方案/20261121-MuMu用Windows便携版实施方案.md)

## 界面扫码接口验证

Core/引擎联调仍使用上面的两个 `BUE_*` 环境变量，必须为支持 `login_interaction.version=1` 的实际 fixture 引擎。运行以下命令，覆盖 PNG/白名单 API、幂等、取消、账号互斥、真实 SQLite 提交失败、旧凭据恢复及 API 进程实际退出后的恢复；不依赖 mumu 或 Worker。

```powershell
python -m pytest tests/integration/test_login_sessions.py tests/recovery/test_gui_login_recovery.py tests/recovery/test_engine_launch.py -q
```

真实退出场景分别保留扫码中、凭据提交前后证据；有退出证据的扫码场景由测试独立持有原进程/Job 句柄，缺身份场景则要求保守恢复，不能把测试持有句柄当成产品无条件恢复能力。`helpers/gui_server.py` 仅是测试故障入口。引擎仓库 `tests/test_gui_login.py` 验证实际机器扫码 PNG、敏感信息隔离与未知平台码。共享 JobHost/维护服务变更时同时运行原 Fake、终端登录、上传与查询回归。mumu 账号页的真实扫码由其项目独立实测。适用部署与两项免除的现场验证见[本机真实扫码验收](../archive/方案/20261117-mumu界面扫码登录接入方案.md#14-本机真实扫码验收)。
