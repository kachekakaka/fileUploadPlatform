# 测试运行入口

选测、阶段、隔离与证据规则见[测试规范](../docs/测试/README.md)，业务要求见[需求](../docs/需求说明.md)。原实施记录见[Core MVP 实施结果](../archive/方案/20261116-Core-MVP实施方案.md#产品实施结果)，最新补验见[审查修复结果](../archive/方案/20261116-Core-MVP审查修复.md#实际结果)。

## 环境和安装

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

退出码 0 且断言通过才表示该轮通过；同时检查日志异常与警告。完整范围是下表所有已实现测试，不含真实平台或 Dashboard。

| 范围 | 命令 | 实际覆盖 |
| --- | --- | --- |
| 单元 | `python -m pytest tests/unit -q` | 状态图、聚合、元数据、YAML、路径、准备与写入失败、重试配置、时间/DST |
| SQLite/文件/并发 | `python -m pytest tests/integration -q` | 真库重开、进程/连接并发初始化、中断、约束、事务回滚、幂等/丢响应、轮次、租约、派发接管、迟到、junction；真实写锁等待跨越过期/续租/恢复/重试/规则计划点 |
| 契约 | `python -m pytest tests/contract -q` | 实际 FastAPI 处理链、来源、错误、分页、版本、请求日志、IPC/JSON Schema；PATCH 省略/null 区分与内部默认值规范化 |
| Windows 故障 | `python -m pytest tests/recovery -q` | 真宿主成功/失败/取消/超时、启动与退出核验前后的 Worker 崩溃、Job 丢失但子进程存活、持久退出证据恢复、身份不符不误杀、恢复竞争、人工处置、取消/成功竞争、真实旧代次迟到结果拒写 |
| 实际入口 | `python -m pytest tests/e2e -q` | CLI → HTTP API → 独立 Worker → Fake Host → 查询/重启；运行中取消及迟到成功、周期管理、SSE 续接、重放、服务不可达不写库 |

时间边界用可控 Clock 和真实 SQLite，不等待整天；进程/HTTP 用真实时间与子进程。媒体均为最小合成字节，Fake 不解析视频内容。新增账号只在测试库使用，不接触真实账号。

`integration/test_lock_time_boundaries.py` 在第二连接的 BEGIN IMMEDIATE 实际等待时推进 Clock；`recovery/test_review_boundaries.py` 用真实宿主输出、SQLite 接管及提交屏障覆盖审查缺陷。取消测试在取消前取得宿主/子进程句柄，结束后直接核验操作系统退出状态，不以再次调用被测恢复函数作为退出证明。late_result 按 duration_seconds 产生结果，分别验证当前有效结果可由恢复者采用、错误代次拒绝、旧执行者不能提交或覆盖后继任务。

`helpers/worker_once.py` 只供故障测试注入强制退出。缺少 PID 的启动窗口在产品侧保守阻塞；测试用独立证据回收自己创建的挂起进程，这种夹具清理不算产品自动恢复通过。保留失败数据库与证据，不按名称终止或删除未知临时目录。

## 契约再生成

```powershell
$env:UPLOAD_PLATFORM_DATA_DIR = Join-Path $testRun 'contract-export'
python tools/export_contracts.py
```

生成 `schemas/`，不初始化业务库；生成后检查契约差异，文件存在不证明行为正确。本轮 Starlette TestClient 对 httpx 有弃用提示；实际 HTTP 另由独立 server/CLI 测试验证，后续升级客户端依赖需按影响范围验证。
