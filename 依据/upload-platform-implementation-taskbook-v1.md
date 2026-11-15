# Upload Platform --- Implementation Taskbook v1.0

> **文档身份：初始输入与追溯参考。** 本任务书用于初步建立项目和编写后续实际实施方案，不再维护现行需求、实施状态或验收结果。下文的 Task、Batch、Sprint 和验收条目保留为初始拆分参考，不能直接作为开工指令；如与现行约定不一致，先在具体方案中对齐，不能静默沿用旧要求。

**定位：** 独立应用 + REST API / Python SDK / CLI / Web Dashboard\
**首批平台：** Bilibili、Baidu Netdisk\
**架构参考：** 同目录的 Architecture Design v1.0-rc2 ZIP；资料使用边界见[根 README](../README.md)\
**初始实施方向：** 使用当前独立仓库；旧项目只审查迁入；先 Core MVP，再真实平台\
**状态：** 初始参考；长期规则和需求已移交到 docs，产品实现尚未开始。\
**本轮同步：** 2026-11-15；不配置 CI，数据库迁移后续另行安排，本地成片直接引用，网页文件接收完成后使用，两种文件交接均不增加内容摘要校验。

现行业务预期见[需求说明](../docs/需求说明.md)，模块和流程见[设计说明](../docs/设计说明.md)，测试规则见[测试规范](../docs/测试/README.md)。项目协作规则见 [AGENTS.md](../AGENTS.md)，文档维护与方案状态见[文档入口](../docs/README.md)。后续先形成[具体实施方案](../docs/方案/README.md)，收到覆盖该范围的明确实施指令后，再按方案实施并验证、同步、归档。

## 0. 初始原则参考

1.  使用已拉取的 `kachekakaka/fileUploadPlatform` 独立仓库，不另建仓库，也不嵌套同名目录。
2.  旧项目仅作为资产来源；逐模块审查后迁移，不整体复制。
3.  先实现 Core 和 Fake Adapter，再接真实平台。
4.  API、CLI、SDK、Dashboard 共用 Application Services。
5.  Task、Schedule、ScheduleRun、Attempt 生命周期分离。
6.  所有任务状态、触发记录、Lease、事件和结果必须持久化。
7.  上传、网络请求、哈希、文件复制、进程等待不得在数据库写事务中执行。
8.  Lease 过期不等于远端操作停止；UNCERTAIN 不得自动重试。
9.  Core 不依赖第三方上传程序；第三方实现只能作为可替换 Adapter。
10. SQLite WAL 只用于单主机本地可靠文件系统，不得跨主机共享。
11. 每个 Batch 按范围交付实现、验证和相关文档；纯文档任务只做文档检查，涉及产品实现时提供可运行验收结果。数据库升级迁移由用户后续安排，不作为当前每批必交付项。
12. 发现架构缺口时提交 ADR，不得静默改变核心契约。
13. 不配置 CI。按改动影响选择本地验证；完整测试另行明确范围，不默认每次全跑。
14. 本地成片登记路径并直接读取，不默认复制，不检查提交后的文件变化；网页文件完整接收后才可使用。两种交接均不增加文件内容摘要校验，读写、接收和上传的实际错误正常处理。取舍见[文件交接决定](../docs/adr/0001-reference-local-upload-files.md)。

## 1. 初始里程碑参考

  Milestone   交付                                        结果
  ----------- ------------------------------------------- --------------------------
  M0          项目约束、长期文档、本地验证、Docker 基础   可安装、测试、启动
  M1          Domain、状态机、数据库                      领域模型可运行
  M2          Scheduler、ScheduleRun、持久队列            即时/定时/周期任务可调度
  M3          Worker、Lease、Adapter Host、Fake Adapter   可执行、取消、恢复
  M4          REST API、CLI、Python SDK                   外部程序可分配和查询任务
  M5          Bilibili Adapter                            完成真实 B 站上传
  M6          Baidu Adapter                               完成百度网盘上传和恢复
  M7          Dashboard、通知、运维                       完整独立应用

Dashboard 不阻塞 Core MVP。先交付 API 和 CLI。

## 2. 目标仓库结构

这是初始建议结构，不是当前已实现清单或必须逐项创建的目录。实际仓库根目录为 `D:\Projects\litleTools\fileUploadPlatform`，不在其中再创建同名子目录。文档归属以文档入口为准；代码、构建和运行目录在具体实施方案中确定，收到实施指令后再创建。

``` text
fileUploadPlatform/
├── AGENTS.md
├── README.md
├── CONTEXT.md
├── 依据/                       # 初始任务书、原始 ZIP 及展开副本，仅作参考
├── apps/
│   ├── server/
│   ├── worker/
│   └── cli/
├── packages/
│   └── upload_platform/
│       ├── domain/
│       ├── application/
│       ├── scheduling/
│       ├── execution/
│       ├── adapters/
│       ├── infrastructure/
│       ├── api/
│       └── sdk/
├── schemas/
├── migrations/                 # 数据库迁移后续另行安排，当前不建立
├── tests/
│   ├── README.md
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── recovery/
│   └── e2e/
├── deploy/
├── docs/
│   ├── README.md
│   ├── 需求说明.md
│   ├── 设计说明.md
│   ├── 待办.md
│   ├── 方案/                    # 活动方案；完成后移入 archive/docs/方案/
│   ├── 测试/                    # 长期规范与测试设计，tests/ 负责实际运行入口
│   ├── 证据/验证/               # 有长期价值的实际验证摘要
│   ├── adr/
│   └── operations/
├── archive/                    # 已完成方案等退出活动管理的历史文档
├── examples/
└── pyproject.toml
```

建议技术栈：Python 3.12+、FastAPI、Pydantic 2、SQLAlchemy
2、SQLite
WAL、asyncio、httpx、Typer、pytest、pytest-asyncio、Hypothesis、Ruff、Pyright
或 mypy（二选一）、Docker Compose。

数据库升级迁移策略与工具由用户后续安排，本轮不预定 Alembic revision 链或旧库升级方案；空库初始化、持久化和运行故障恢复仍属于产品要求。

## 3. Batch 0 --- 仓库与工程基础（初始拆分）

### Tasks

-   T0.1 使用已拉取的 `kachekakaka/fileUploadPlatform` 仓库及上述根目录，核对默认分支和远程地址；提交、推送与创建 PR 按明确任务执行。
-   T0.2
    按阶段建立目录结构及文档入口；先维护 AGENTS、README、CONTEXT、文档职责与测试规范，产品工程实施时再建立 `pyproject.toml`、CONTRIBUTING、CHANGELOG、LICENSE 等实际需要的文件，不预建空占位。
-   T0.3 配置 `.gitignore`，排除数据库、凭据、媒体、日志、checkpoint
    和构建产物。
-   T0.4 配置 Ruff、类型检查、pytest、Hypothesis。
-   T0.5 建立本地验证入口及执行说明：lint、format check、type check、unit tests、schema validation、空库初始化检查；Docker build 随相关改动或批次验收执行。不配置 CI，具体选测遵循测试入口。
-   T0.6 建立 API Server 和 Worker Runtime 两个启动入口。
-   T0.7 建立 Dockerfile / Compose、持久化卷、healthcheck、非 root
    用户。
-   T0.8 默认 API 仅监听 loopback；远程监听必须认证。

### 验收

全新 clone 后可安装；本批适用的本地检查通过；API health endpoint 可访问；Worker
可启动并注册；Docker Compose 可启动；无真实上传和凭据。

## 4. Batch 1 --- Domain Model 与数据库

### Tasks

-   T1.1 实现
    Account、Schedule、ScheduleRun、Task、Attempt、Asset、TaskAsset、Worker、TaskLease、AccountLease、Receipt、DomainEvent。
-   T1.2 统一 UUID、UTC aware datetime、状态枚举和状态转换服务。
-   T1.3 定义
    Repository：Task、Schedule、ScheduleRun、Attempt、Account、Asset、Lease、Event、Receipt。
-   T1.4 定义 Unit of Work 和 Transaction Manager。
-   T1.5 参考 rc2 数据模型形成实际数据库方案，先与现行需求和设计对齐，再按明确实施指令建立 SQLAlchemy 2 实现；不得原样采用存在已知冲突的候选字段。
-   T1.6 建立当前版本的空库初始化入口。数据库升级迁移由用户后续安排，本任务不预定历史版本迁移链，也不自动删除或重建已有业务库。
-   T1.7 测试 FK、CHECK、唯一索引、partial unique
    index、事务回滚和重复初始化行为。
-   T1.8 禁止 Domain 依赖 FastAPI、SQLAlchemy、SQLite、httpx 和具体
    Adapter。

### 验收

一个 ScheduleRun 最多一个 Task；一个 Task 最多一个活动 Attempt；一个账号
slot 最多一个活动 Lease；非法状态跳转被拒绝；当前结构可从空库初始化。数据库升级迁移不属于本轮已安排的验收项。

## 5. Batch 2 --- Asset Manager

### Tasks

-   T2.1 区分本地文件引用与网页文件接收：CLI/受控本机入口登记已有成片的位置，执行时直接读取原文件，不默认复制；Dashboard 接收从其他电脑传入的文件。提交方须将本地原文件保留到任务结束。
-   T2.2 远程 REST API 不接受任意服务器绝对路径。
-   T2.3 保留入口的路径准入、普通文件、大小、类型及符号/硬链接安全边界；不增加文件内容摘要校验，也不比对本地文件是否自提交后被修改或替换。读取不存在或不可读的文件时正常报告实际错误。
-   T2.4 网页文件接收使用 operation journal 记录接收及落位过程；未完成文件位于受控接收目录，完成后的落位遵循原子 rename 与 fsync 要求。本地引用不强制经过该复制暂存流程。
-   T2.5 网页文件正常完整接收后标记 AVAILABLE，不为内容比对再扫描整份文件；接收中断、写入失败等正常处理，未完成文件不得标为可用。接收完成不代表目标平台上传成功。
-   T2.6 实现 TaskAsset 多对多引用，支持
    VIDEO、COVER、SUBTITLE、ATTACHMENT 和 sort_order。
-   T2.7 实现保留策略和清理恢复；清理前重新检查 Task、Attempt、Recovery
    引用。清理只作用于平台拥有且符合清理条件的文件，不因登记本地引用而取得删除提交方原文件的权限。

### 验收

大文件不整体读入内存；本地成片不产生默认副本，不以文件内容变化为由拒绝上传；网页接收中断或失败不会把半成品标为可用，接收过程按记录恢复；两种交接都不增加内容摘要扫描。共享 Asset 不被提前删除，本地原件不被平台清理，平台自有文件的清理可幂等重试。

## 6. Batch 3 --- Scheduler 与 Persistent Queue

### Tasks

-   T3.1 实现
    TaskSubmissionService：immediate、scheduled、cancel、retry、get、list。
-   T3.2 Schedule 支持 ONCE、DAILY、WEEKLY、CRON 和 IANA timezone。
-   T3.3 Schedule revision 更新；修改 Schedule 不改变已生成 Task。
-   T3.4 Schedule Evaluator 计算 next_fire_at 并物化 PLANNED
    ScheduleRun。
-   T3.5 occurrence_key 使用规范化 UTC instant。
-   T3.6 Due Dispatcher 在单事务中处理 ScheduleRun、创建 Task、更新
    GENERATED、写 Event/Outbox。
-   T3.7 实现 RUN_IMMEDIATELY、SKIP、EXPIRE 和 max_lateness_seconds。
-   T3.8 实现
    DST：不存在的本地时刻跳过；重复时刻默认选择第一次实际发生的时刻。
-   T3.9 Queue 默认 FIFO，支持有限 priority，不允许无限插队。
-   T3.10 RETRY_WAIT 仅在 next_attempt_at 到期后可领取。
-   T3.11 Task / Schedule 创建支持 Idempotency-Key：同一次提交因回复丢失而重发时返回原资源；相同 key 对应不同请求内容时返回 409。这里比较提交内容，不要求读取视频文件，也不预定摘要算法。

### 验收

即时任务提交后可唤醒 Dispatcher；定时任务不提前执行；重启不丢触发；并发
Evaluator 不重复生成 Task；DST 和 misfire 测试通过。

## 7. Batch 4 --- Worker Runtime、Lease 与 Recovery

### Tasks

-   T4.1 实现 Worker Registry，记录 worker_id、hostname、host
    boot_id、process identity、role、heartbeat、status。
-   T4.2 实现 Task Lease、Account Slot Lease、Lease Token、单调 Fencing
    Token。
-   T4.3 Task Lease 与 Account Slot Lease 获取必须在同一个短事务内完成。
-   T4.4 原子 Claim：选择到期 Task、验证 Account、检查 slot、递增
    fencing counter、创建 Attempt 和两种 Lease、更新 Task、写
    Event/Outbox。
-   T4.5 Heartbeat 由 Worker Supervisor 负责；Adapter 不得自行续租。
-   T4.6 所有 Adapter 通过独立受监督 Adapter Execution Host 执行。
-   T4.7 Supervisor
    管理启动、进程身份、事件收集、续租、取消、退出、结果验证和状态提交。
-   T4.8 Recovery 检查进程、events、checkpoint、result、receipt
    和远端阶段。
-   T4.9 Lease 过期只进入 RECOVERY_PENDING；不得直接重新上传。
-   T4.10 旧 fencing generation 不得提交本地终态。
-   T4.11 实现 Fake Adapter 故障场景。

Fake Adapter 场景： - success - validation_failure -
retryable_failure_before_remote - permanent_failure -
crash_before_start - crash_during_upload - crash_after_remote_start -
timeout - delayed_result - malformed_event - malformed_result -
cancellation_safe - cancellation_uncertain

### 验收

多 Worker 不重复领取；账号并发额度生效；旧 fencing token
写入被拒绝；孤儿进程不会被忽略；UNCERTAIN 不自动重试；故障场景测试通过。

## 8. Batch 5 --- Event、Outbox 与 Observability

### Tasks

-   T5.1 定义 Event Envelope 和 schema version。
-   T5.2 每个 aggregate 的 sequence 单调递增。
-   T5.3 Event 和 Outbox 在同一事务写入。
-   T5.4 Publisher at-least-once；消费者按 event_id 去重。
-   T5.5 SSE 支持 Last-Event-ID。
-   T5.6 结构化日志、敏感信息脱敏。
-   T5.7 Worker 心跳、健康状态、错误分类和告警事件。

### 验收

状态变化有对应事件；重复投递不产生重复业务效果；SSE
可重连；日志不包含凭据明文。

## 9. Batch 6 --- REST API、CLI、Python SDK

### REST API

实现 `/api/v1`： - Tasks - Schedules - ScheduleRuns - Attempts -
Accounts - Assets - Events - Workers - System Health

以 OpenAPI 3.1 为公开契约。metadata 按 platform + Adapter Schema 校验。

### CLI

``` text
upload-platform server
upload-platform worker
upload-platform task create
upload-platform task list
upload-platform task status
upload-platform task cancel
upload-platform task retry
upload-platform schedule create
upload-platform schedule list
upload-platform schedule pause
upload-platform schedule resume
upload-platform account list
upload-platform asset import
upload-platform doctor
upload-platform reconcile
```

### Python SDK

``` python
client.tasks.create(...)
client.tasks.get(...)
client.tasks.list(...)
client.tasks.cancel(...)
client.schedules.create(...)
client.schedules.pause(...)
client.schedules.resume(...)
client.accounts.list(...)
client.assets.register(...)
```

SDK 调用公开 REST API，不得绕过 API 直接访问数据库。

### 验收

OpenAPI 完整；CLI 可提交即时和定时任务；SDK
可创建、查询、取消任务；各入口行为一致。

## 10. Batch 7 --- Bilibili Adapter

### Tasks

1.  登录和凭据管理；
2.  账号身份验证；
3.  投稿前校验；
4.  视频分片上传；
5.  多 P 视频；
6.  封面上传；
7.  投稿元数据；
8.  最终投稿；
9.  BVID/AID 结果验证；
10. Checkpoint；
11. 断点恢复；
12. UNCERTAIN 处置；
13. 专用测试账号故障注入。

优先自主实现。biliup Bridge 仅作为可选兼容后端，不成为 Core 必需依赖。

### 验收

专用测试账号真实上传成功；BVID/AID
有效；状态追踪准确；中断可安全恢复；最终投稿窗口故障不会盲目重复投稿。

## 11. Batch 8 --- Baidu Netdisk Adapter

### Tasks

1.  OAuth；
2.  Token refresh；
3.  账号身份验证；
4.  文件预创建；
5.  分片上传；
6.  分片状态持久化；
7.  创建远端文件；
8.  远端文件校验；
9.  断点恢复；
10. 文件冲突策略；
11. 大文件支持；
12. 远端状态不确定处理。

### 验收

普通文件和大文件上传成功；中断后可从已完成分片恢复；远端校验通过；冲突策略按配置执行。

## 12. Batch 9 --- Dashboard 与产品化

### Dashboard

-   Overview
-   Task Queue
-   Task Detail
-   Schedule Management
-   Account Management
-   Asset Library
-   Adapter Status
-   Attempt History
-   Event Timeline
-   Recovery Management
-   System Health

Dashboard 只通过 REST API / SSE 访问数据，不直连数据库或媒体目录。

### 产品化

-   用户认证和授权；
-   系统配置；
-   通知；
-   备份恢复；
-   数据清理；
-   Docker Compose 完整部署；
-   升级策略；
-   运维文档。

## 13. 旧项目迁移

本节指旧代码资产审查后的迁入，不是数据库升级迁移；数据库迁移仍由用户后续安排。

  旧模块               决策
  -------------------- -------------------------
  SQLite Queue         审查后迁移核心逻辑
  Recovery             保留不变量，重构实现
  Receipt              保留设计，适配新 Schema
  Adapter Supervisor   审查后迁移
  Adapter Protocol     以新协议为准
  Bilibili Bridge      可选兼容后端
  QR Login             审查安全和许可证后迁移
  Docker               重新整理
  Directory Intake     可选 Producer
  历史文档             归档

每个迁移模块记录原始文件、commit、功能、许可证、第三方来源、测试覆盖、迁移决策、新位置和验收结果。不得未经审查复制整个旧仓库。

## 14. 工作规范的现行承接

本任务书不再维护另一套执行规范。协作和数据边界见 [AGENTS.md](../AGENTS.md)，先方案、明确实施指令、状态和归档见[文档规范](../docs/README.md)，测试设计、范围、阶段、失败处理和证据规则见[测试规范](../docs/测试/README.md)。

Task/Batch 可以作为编写具体方案的参考索引，但不代替方案。当前文档修订完成不等于下列工程和产品任务已开始，也不授予后续编码权限。

## 15. 首个迭代参考：Sprint 1（尚非实施方案）

**初始目标：** 当前独立仓库可安装、测试、启动，并建立最小 Domain 与数据库基础。实际范围和顺序在后续方案中确定。

任务顺序：

-   S1-01 核对当前仓库的根目录、默认分支与远程，不重复创建仓库；
-   S1-02 建立目录结构和 pyproject；
-   S1-03 配置 Ruff、类型检查、pytest；
-   S1-04 建立本地验证入口及选测说明，不配置 CI；
-   S1-05 建立 UUID / UTC 时间基础类型；
-   S1-06 实现 Account、Task、Schedule、ScheduleRun、Attempt 实体；
-   S1-07 实现状态枚举和转换服务；
-   S1-08 建立 SQLAlchemy Base；
-   S1-09 完成 foundation 当前结构及空库初始化；数据库升级迁移后续另行安排；
-   S1-10 完成 SQLite integration tests；
-   S1-11 建立 API Server / Worker Runtime 空启动入口；
-   S1-12 建立 Docker Compose 最小运行环境。

Sprint 1 不实现真实上传，也不实现完整 Dashboard。

### Sprint 1 完成标准

-   全新 clone 后可安装；
-   本批适用的本地检查通过；
-   当前数据库结构可从空库初始化；
-   API health endpoint 可访问；
-   Worker Runtime 可启动并注册 Worker；
-   Domain 单元测试通过；
-   SQLite FK、唯一约束和事务测试通过；
-   Docker Compose 可启动；
-   无真实账号凭据。

## 16. 初始 Batch 交付记录参考

以下保留最初的记录样式，供追溯和取材，不是现行方案状态或强制交付模板。当前方案格式和状态由文档规范管理，实际验证摘要由测试规范管理；不能用下表的 DONE 替代方案验收与归档条件。

``` text
Batch:
Status: NOT_STARTED / IN_PROGRESS / BLOCKED / DONE

Implemented:
- ...

Tests:
- 选择的范围与阶段、实际结果、未执行项及必要证据位置

Migrations:
- 不适用 / 待用户安排 / 已明确安排的实际结果（不得把未执行记为完成）

Documentation:
- ...

Known issues:
- ...

Architecture deviations:
- ...

Next batch:
- ...
```

核心设计变化按现行文档规范处理；需要长期解释的取舍记录 ADR，实施范围与指令记录在对应方案中。

## 17. 初始最终交付愿景

最终产品必须具备： - 独立运行能力； - REST API、Python SDK、CLI、Web
Dashboard； - 即时、一次性定时、每日/每周/Cron 周期任务； -
持久化队列； - 多账号和可配置并发； - Bilibili 与 Baidu Netdisk
Adapter； - 进度、历史、取消、重试和恢复； - UNCERTAIN 人工处置； -
Docker 部署； - 备份与恢复。

完成标准不是"上传功能能跑"，而是系统能够长期运行、任务可追踪、故障可恢复、平台可扩展，且不依赖某个第三方上传项目持续维护。
