# fileUploadPlatform：Dashboard、Web 接入与统一提交实施方案

- 创建日期：2026-11-20。归档日期：2026-11-21。状态：已转交。fileUploadPlatform 0.2.0 本轮联调交付收尾，余项见[归档结论](#17-归档结论)。
- 基线：`origin/main`，提交 `45c81fb6eb2f35dedc4b6cc51c276024d66fea49`
- 关联正文：[需求 R01、R03、R10](../../docs/需求说明.md)、[设计 D01、D03、D04](../../docs/设计说明.md)。配套工作见 [MuMu 统一上传平台客户端改造实施方案](https://github.com/kachekakaka/mumu_lbcut_tool/blob/86b280968399293e30f2a6f4257c88be7bb48445/archive/docs/%E6%96%B9%E6%A1%88/20261120-mumu%E7%BB%9F%E4%B8%80%E4%B8%8A%E4%BC%A0%E5%B9%B3%E5%8F%B0%E5%AE%A2%E6%88%B7%E7%AB%AF%E6%94%B9%E9%80%A0%E5%AE%9E%E6%96%BD%E6%96%B9%E6%A1%88.md)。
- 决策依据：R1—R8 定向 Review、自动场次生成 YAML 的修订，以及 2026-11-20 用户对免登录、开放投稿和旧任务范围的确认。
- 授权范围：用户于 2026-11-20 明确授权实施本方案中属于上传平台仓库的工作，并要求从阶段 0 开始按方案推进。MuMu 改造和需要另行授权的真实 B 站操作仍按各自的范围执行。

## 1. 目标与非目标

在现有 Core、Application Services、Worker、Adapter、SQLite、REST API 和 SSE 基础上，增加一个可独立使用、也可由 MuMu iframe 嵌入的本机 Dashboard。

平台负责读取统一 `upload.yaml`/Manifest、登记 Work、预览和校验配置、创建 Task、冻结 Snapshot、管理账号和上传生命周期、管理每日/每周 Schedule，并提供六大 Dashboard 页面。

本轮不实施百度网盘真实 Adapter、动态插件市场、低代码表单引擎、多用户权限、远程部署、反向代理、浏览器大文件上传和 Cron。

本项目尚未发布。历史查询和断网查回只验本版本创建的任务。旧版任务不做适配、迁移、补投或查回演练，原有数据库和素材保留在原处。

## 2. 已确认架构决策

| 编号 | 决策 |
|---|---|
| R1 | 每个作品 `upload.yaml` 是投稿配置来源；Work 保存登记版本，Task Snapshot 冻结任务版本。 |
| R2 | 上传平台是 Work/Task/Attempt/Event/Receipt 和执行状态唯一权威。 |
| R3 | Windows 本机、单用户。独立和内嵌面板首次打开即可使用，无需上传平台账号或首次访问确认。 |
| R4 | 上传平台绑定 `127.0.0.1:8765`；MuMu `127.0.0.1:8000` iframe 嵌入。 |
| R5 | Vue 3 模块化单体、轻量 Registry、通用组件和必要专属组件。 |
| R6 | Overview、Accounts、Works、Tasks、Schedules、Settings；Works 支持本机登记。 |
| R7 | 统一提交 API 接受本机程序和不同来源的网页投稿，调用方免登记。预览与提交版本一致，并提供任务筛选、操作能力、系统概览和 Schedule Run 摘要。 |
| R8 | 上传平台先行；MuMu 后续 Direct 切换，不提供双轨模式。 |

## 3. 当前基础与复用原则

现有后端已有 Work、Task、Attempt、Event、Receipt、Account、Bilibili 扫码登录会话、Schedule、Schedule Run、SSE、Fake Adapter 和 Bilibili Adapter 基础。实施前必须重新核对代码，不得把本方案中的新接口/字段误认为已经存在。

原则：

1. Dashboard 只作为现有 Application Services 的新入口，不复制上传业务规则。
2. REST API、CLI、Worker 的业务语义保持一致。
3. 原 `/works` 和 `/works/{id}/submit` 保留现有请求和响应，包括直接传入作品描述的用法。用新建作品回归这些接口。MuMu、Dashboard 和其他调用方可使用新增的统一入口。
4. `fake` 用于测试，可在普通用户 UI 隐藏；Bilibili 是本轮唯一真实平台。
5. 新平台必须由后端 Adapter/契约和前端 Registry 共同支持；Registry 不会自动产生上传能力。

## 4. 部署与运行模型

- Dashboard/API：`http://127.0.0.1:8765`
- MuMu：`http://127.0.0.1:8000`
- API 与 Dashboard 静态资源由同一 FastAPI 服务提供；Worker 继续独立运行。
- API 与 Worker 共用仓外数据目录和 SQLite。
- 不绑定 `0.0.0.0`，不开放局域网，不使用反向代理。
- 两个项目统一使用 `127.0.0.1`，不得混用 `localhost`。

## 5. Web 安全接入

### 5.1 面板打开即用

用户在本机直接打开 Dashboard，或从 MuMu 打开内嵌页面，首次和刷新都直接进入目标页。平台不设置登录页、密码或首次访问确认框。用户在“账号”页添加或重新登录 Bilibili 投稿账号时，才进入 B 站扫码流程。

### 5.2 不同程序和网页投稿

统一提交、该次投稿的预览和按原请求号查回，对本机程序和在这台电脑的浏览器中打开的其他来源网页采用同一契约。新增软件或网站即可按公开接口交单，无需登记调用方、申请专用标识或取得 Dashboard Cookie。合法的自动提交也可以受理。

- 新入口按请求内容校验配置版本、允许的文件目录、目标账号、投稿选项和幂等键。调用方自报的 `source` 仅用于作品关联。网页来自哪个 Origin、程序是否发送浏览器头，都不决定投稿资格。
- 网页可通过浏览器正常的跨来源预检发送 JSON 和 `Idempotency-Key`，并读取受理结果或业务错误。预检不创建 Work 或 Task。公开投稿流程不依赖管理页面的 CSRF Token。
- 预览返回本次作品的投稿内容和校验结果。统一提交返回本次作品和任务的标识、受理状态及配置版本，查回只返回对应请求的结果。B 站凭据和扫码数据只由账号管理流程处理。

### 5.3 本机地址与管理操作

当前 Core 的 `local_only` 只接受非浏览器请求。按路由接入新版面板和公开投稿流程，现有 CLI 和 MuMu 桥接接口遵循各自的客户端契约。

- 服务只监听本机回环地址。所有入口核对请求的目标 Host，拒绝转发头。提交时由后端核对配置文件和素材的准入范围，调用方传入的路径本身不授予目录访问权。
- Dashboard 的账号维护、任务管理、定时规则修改和原生目录选择使用面板自身来源。后台对管理写操作做来源和防跨站请求校验，页面自动完成所需交互。iframe 内的操作也由上传平台页面发起。
- 只读查询不产生投稿。二维码和敏感状态响应使用 `Cache-Control: no-store`，B 站 Cookie、扫码 token 和 API 密钥不写入网页存储、URL 或父子页面消息。扫码轮询和写操作沿用限流要求。

### 5.4 iframe

- CSP `frame-ancestors` 仅允许 `http://127.0.0.1:8000`。
- 独立 Dashboard 仍可顶层访问；嵌入模式不等于授权绕过。
- `frame-ancestors` 通过 HTTP 响应头设置。
- 父子消息校验 `event.origin`、`event.source`、协议版本和消息类型；发送使用精确 `targetOrigin`。
- iframe 路由和 postMessage 不传凭据、YAML 全文或任意文件路径。
- `frame-ancestors` 和父子消息的来源检查只约束谁能嵌入或控制管理页面。其他网站可以直接使用第 5.2 节的投稿接口。

## 6. 前端工程

### 6.1 技术栈

Vue 3、TypeScript、Vite、Vue Router、Element Plus。上传平台单独构建、单独依赖、单独发布。

### 6.2 建议目录

```text
web/
└── src/
    ├── app/                  # 初始化、路由、独立/嵌入布局
    ├── features/
    │   ├── overview/
    │   ├── accounts/
    │   ├── works/
    │   ├── tasks/
    │   ├── schedules/
    │   └── settings/
    ├── platforms/
    │   ├── registry.ts
    │   ├── types.ts
    │   ├── bilibili/
    │   └── fake/
    ├── embed/
    ├── api/
    └── shared/
```

### 6.3 Registry 边界

每个平台 UI module 可提供 `id/name/icon`、后端启用状态和能力、可选 LoginComponent、TaskDetailComponent、ResultComponent，以及状态/错误码展示映射。

简单差异用配置，复杂差异用专属 Vue 组件。不得建设动态安装 JS 插件、低代码 Schema 表单引擎或单一巨大 PlatformOptions 类型。投稿字段来自 YAML，Dashboard 默认只读展示，不重复编辑平台投稿参数。

后端 Target 继续采用按 platform 判别的严格类型；前端能力显示以服务端为准，区分已实现、已配置、已开放和账号可用四种状态。

## 7. 六大页面

### 7.1 Overview

显示 API、Worker、数据库、平台 Adapter/引擎状态；账号数量；QUEUED/RUNNING/RETRY_WAIT/UNCERTAIN/终态任务统计；近期任务和异常。不能把排队误报为 Worker 正在执行。

### 7.2 Accounts

平台账号列表、扫码登录会话、二维码、轮询、完成/过期/取消、账号身份/MID/可用状态/阻塞原因、重新登录。二维码和登录会话敏感信息不缓存、不写入 URL。

### 7.3 Works

- 作品列表、来源、登记时间、素材数量、目标平台、任务统计。
- 作品详情只读展示当前登记 Manifest、素材路径、upload unit、multipart 顺序、目标账号和 options。
- 展示配置版本标识和来源。
- 展示本版本中与作品关联的当前任务和历次任务。
- 不提供 YAML 编辑器。
- 来源元数据可用 `mumu_manual`、`mumu_session`、`local_import`、`external` 分类；它不是 Manifest 的平台必填字段。
- 磁盘 YAML 后续变化不会静默改写已登记 Work 或既有 Task Snapshot。

### 7.4 Tasks

- 服务端分页筛选：平台、账号、Work、状态、创建时间。
- 详情展示 Task Snapshot、目标、素材顺序、状态时间线。
- Attempt、Event、Receipt、远端查询和人工核验记录。
- 使用 SSE 订阅单任务事件，支持 Last-Event-ID；断线后 REST 回查。
- 根据服务端 `available_actions` 展示取消、重提、查询和核验；后端执行时重新校验。
- UNCERTAIN 明确显示“结果待核验”，不得当作普通失败自动重传。
- 只显示 Worker/Adapter 实际提供的进度，不虚构百分比或速度。

### 7.5 Schedules

复用已有 daily/weekly 规则：创建、查看、更新、启停；显示下一次计划、最近 Run、发现作品数、创建任务数、跳过数、失败原因。使用 expected_version。Run 触发成功不等于上传成功。本轮不扩展 Cron。

### 7.6 Settings

显示服务版本、API/数据库健康、Worker 心跳、数据目录、Bilibili 引擎配置和诊断信息。第一版不做复杂部署编辑器和用户权限管理。

## 8. YAML、Manifest、Work 与 Task Snapshot

### 8.1 唯一配置来源

所有来源作品最终都提供独立 `upload.yaml`，遵循现有 `manifest-v1`：普通作品、MuMu 自动场次、Dashboard 本机登记和其他客户端作品。上传平台读取和校验，不负责编辑投稿字段。Work Manifest 是登记版本，Task Snapshot 是实际执行依据。

### 8.2 Manifest 文件路径与素材资产根目录

当前 `manifest.py` 使用传入 directory 作为资产根目录，并由 `asset_info/root_path` 校验规范路径和越界。因此必须区分：

1. YAML 文件所在位置（manifest path）。
2. Manifest 相对素材路径的资产根目录（asset root）。

普通作品的 `upload.yaml` 通常就在素材根目录。MuMu 场次 YAML 位于 `output_dir/sessions/<work-uuid>/upload.yaml`，素材仍位于 `output_dir/job-*`。新入口分别保存 `manifest_path` 和 `asset_root`。读取配置时核对它位于允许的配置目录内，读取素材时按允许的素材根解析并核对最终位置。继承现有的路径遍历、UNC 和网络路径限制。

这是 P0 设计门槛，先用真实临时目录验证，再冻结 OpenAPI 和 Manifest 说明。不能仅靠 `../` 相对路径，也不能要求 MuMu 复制或移动媒体文件。

### 8.3 配置版本一致性

- 严格解析 YAML：拒绝重复键、未知字段、无效编码、超大文件、符号链接/重解析越界和非法相对路径。
- 配置版本标识为本次读取的原始 YAML 字节的 BLAKE2b-256，编码为 64 位小写十六进制。改动注释、空白或换行也会改变版本。作品身份使用 `work.id`。
- Preview 响应包含 `preview_id`、Work ID、配置版本、规范化预览、校验错误/警告和过期时间。
- 手动提交带上用户所见的预览标识。自动客户端可在一次请求中校验并提交，须带上本次冻结的预期配置版本。首次受理时，服务端用实际读取的 YAML 核对版本。版本已变化就返回冲突，由调用方重新预览。
- 原请求已经受理时，按第 9.3 节返回保存的结果。预览过期、YAML 后来修改或移走，都不影响查回原单。
- Task 创建时冻结规范化 Manifest、展开后的单元/目标/素材引用、目录映射、账号标识和执行策略。
- 已创建 Task 不因 YAML 后续修改而变化。

媒体文件继续按现有文件引用策略在执行前复核路径、普通文件、可读性和必要属性；不默认复制、扫描或计算媒体内容哈希。

## 9. 统一提交 API

### 9.1 新增入口

```http
POST /api/v1/submissions
Idempotency-Key: <UUIDv4>
Content-Type: application/json
```

请求语义草案：

```json
{
  "manifest_path": "受控 upload.yaml 路径",
  "asset_root": "受控本机资产根目录",
  "expected_version": "自动客户端冻结的配置版本",
  "execution": {"type": "immediate"},
  "source": {
    "application": "mumu",
    "kind": "session",
    "external_reference": "场次关联标识"
  }
}
```

上例为自动投稿。手动预览使用 `POST /api/v1/submissions/preview`，请求携带 `manifest_path` 与 `asset_root`。手动提交携带 `preview_id`，由服务端取回它绑定的配置版本和目录。查回使用 `GET /api/v1/submissions/result`，在请求头携带原 `Idempotency-Key`。新入口的键使用随机 UUIDv4，作为仅用于本单查回的凭据。来源元数据可省略。正式契约由实际 Pydantic DTO 和 FastAPI 路由导出。

### 9.2 统一服务流程

1. 校验请求格式和 UUID 幂等键，按操作和键查已保存的规范请求与响应。相同请求直接返回原结果，同键换正文返回冲突。此步先于预览有效期检查和文件读取。
2. 首次受理时，解析允许的配置路径与素材根，读取一次 YAML。核对手动预览或自动提交绑定的版本，再校验素材、目标平台和账号。
3. 展开 upload units。在同一 SQLite 短事务中复查请求，登记或复用 Work，按共享提交记录复用已有 Task 或创建新 Task，冻结新任务的 Snapshot，保存幂等响应。并发命中仍返回同一结果。
4. 事务提交后返回 Work ID、Task ID 列表、受理状态和配置版本。Worker 按 Task Snapshot 执行。

本轮 MuMu 自动场次一个 Work、一个 multipart unit、一个启用目标，预期一个 Task。通用 API 仍须遵循 Manifest 多 unit/target 语义，不能假设所有作品都只有一个 Task。

### 9.3 幂等和事务

- 同一 Idempotency-Key + 相同规范请求返回原结果。
- 同一 Key + 不同请求内容返回冲突。
- 按原 Key 的只读查回既不读 YAML，也不创建资源。无受理记录时如实返回未查到。客户端仍要提交时才重放原请求，已经撤回的意图只做查回。
- 防止 Work 登记成功但 Task 创建丢失造成重复。
- 数据库事务提交前不得向调用方宣称已受理。
- 不确定结果通过持久化幂等记录恢复，不用新 Key 盲目重试。
- 原 `/works` 和 `/works/{id}/submit` 保留兼容并复用同一 Application Service。

## 10. API 补齐清单

| 优先级 | API/能力 | 要求 |
|---|---|---|
| P0 | 免登录面板与跨来源投稿 | 独立及内嵌页首次直接可用。独立程序和其他来源网页可提交、读取回执并查回原单。 |
| P0 | Manifest path + asset root | 受控双路径解析 |
| P0 | Preview/version | 预览与提交使用同一配置版本 |
| P0 | `POST /submissions` | 统一 Work 登记与 Task 创建 |
| P0 | 幂等结果查询/重放 | 响应丢失不重复建单 |
| P1 | `GET /platforms` | 平台目录、部署状态、能力和不可用原因 |
| P1 | Task 列表查询 | 平台、账号、Work、状态、时间、排序、分页 |
| P1 | Task available_actions | 允许操作及禁用原因 |
| P1 | System overview | Core、DB、Worker、Adapter、引擎、版本 |
| P1 | Schedule Run 摘要 | 发现/创建/跳过/失败统计和下一次执行 |
| P1 | Work 来源和配置版本 | Dashboard 展示和审计 |
| P2 | 统一响应 DTO | 错误、分页、时间和 OpenAPI 类型 |

任务列表必须服务端过滤和分页。所有操作接口执行时再次校验当前状态。

## 11. Windows 本机目录选择

R6 允许 Dashboard 独立登记本机作品，R7 选择 Native：

- 页面提供“选择作品目录”。
- 由受控 Windows 桌面能力打开系统目录选择对话框。
- 目录选择模块必须运行在当前交互式用户会话；不能假设后台服务/Windows Service 能显示桌面 UI。
- 若当前服务进程无法交互，采用最小本机辅助进程/桌面桥接，不把整个上传平台迁移成桌面 GUI。
- 桌面桥接只返回用户选中的路径，不执行任意命令，不接受远程触发。
- 后端再次验证规范路径、允许目录范围、目录存在、YAML 和素材路径。
- 用户取消不创建作品。
- 保留手动输入路径作为备用；同样执行后端安全校验。
- 不用普通 HTML 文件选择器假装能获取服务器绝对路径。

## 12. iframe 页面与消息协议

### 12.1 路由

至少提供：

- `/embed/accounts`
- `/embed/works`
- `/embed/works/{work_id}`
- `/embed/tasks`
- `/embed/tasks/{task_id}`

独立页面与嵌入页面共享 features 组件，仅布局不同。嵌入模式隐藏重复主导航，适配 iframe 容器。

### 12.2 消息

采用版本化 JSON envelope：

```json
{
  "protocol": "upload-platform-embed",
  "version": 1,
  "type": "ready",
  "payload": {}
}
```

首批消息：子→父 `ready`；父→子 `open-task`、`open-account`；子→父 `task-updated`、`account-updated`。

| 消息 | payload |
|---|---|
| `ready` | `{}` |
| `open-task` | `{"task_id":"<任务 UUID>"}` |
| `open-account` | `{"account_id":"<账号 UUID>"}` |
| `task-updated` / `account-updated` | `{"id":"<资源 UUID>","status":"<状态>"}` |

消息只传 ID、状态摘要和必要展示上下文。校验 origin、source、schema、版本和类型。父子通信不是授权来源。

### 12.3 首次打开与刷新

在全新的浏览器配置中直接访问面板和从 MuMu 打开 iframe，都应直接显示目标页面。刷新后按原路由和资源 ID 重新查询状态。账号页可以找回本版本中的 B 站扫码操作，打开页面本身不发起扫码。阶段 0 验证首次加载、CSP 和消息握手，正式页面完成后再验实际操作与刷新。

## 13. 定时管理

复用现有 Schedule/Run 服务和每日/每周能力：创建、读取、更新、启停；expected_version 冲突处理；展示下一次计划和 Run 结果。手动提交、自动场次提交和 Schedule 不得各自复制 Work/Task 创建逻辑。Run 成功只表示规则执行/任务创建结果，不表示上传成功。

## 14. 实施阶段

### 阶段 0：验证面板、投稿和双路径契约

在 Windows 仓外临时目录使用新 SQLite 库、合成素材和 Fake 目标。先核对 `manifest-v1`、Registration、Submission、`manifest.py`、`root_path/asset_info` 和已有幂等事务，再通过最小原型验证下列场景。

| 场景 | 操作和通过条件 |
|---|---|
| 第一次打开 | 用干净的浏览器分别打开独立面板和 MuMu 来源的内嵌样页。首次及刷新均直接显示目标内容，平台不弹登录或授权确认。内嵌样页能完成消息握手。 |
| 两个独立调用方 | 一个本机程序和一个在真实浏览器中打开的其他站点网页，各用不同的作品编号、幂等键提交一份作品。每份作品含一个单元、一段视频和一个启用目标。网页直接发出请求并读到回执。两个调用方均免登记，各得到一个 Work 和一个 Task，库内合计两个任务。 |
| 断网与版本变化 | 让其中一单在服务端提交后丢回包，再移走 YAML，按原 Key 只读查回及重放，均得到同一 Task，任务数仍为二。同键换正文或版本得到冲突。另用一份尚未受理的请求，在首次送达前修改 YAML，期望版本冲突且不建单。只读查回不存在的 Key 也不建单。 |
| 配置与素材分处两地 | 在 `sessions/<work-uuid>/upload.yaml` 按指定顺序引用两个 Job 目录的同名文件。检查快照中的顺序和实际素材位置。分别让配置文件或素材越出其允许范围，均应拒绝且不建单。 |

记录实际浏览器与预检结果、Work/Task 编号和数据库断言。浏览器自身的本机网络访问提示单独记录。网页未能读到回执时记录阻断原因，独立程序的成功只计入该程序的结果。上述场景通过后冻结接口草案。

2026-11-20 已使用 Windows、Python 3.11.1、Chrome 157.0.8103.80、仓外空 SQLite、合成素材和 Fake 目标验证。原型复用 `Service.mutate`、`submit_prepared` 与真实 SQLite 事务。证据位于仓外 `../fileUploadPlatform_tmp/dashboard-stage0-c5c9262aaa9b42398ed8edb38405eb77/attempt-6/`。同一父目录保留所有失败日志及原型脚本。

| 场景 | 实际结果 |
|---|---|
| 第一次打开 | 两个全新 Chrome 上下文通过独立/内嵌首次访问与刷新，Cookie 数为零；内嵌通过 `ready` 和 `open-account`，其他来源被 CSP 拦截。系统已有进程监听 8000，保留其运行，测试 Chrome 仅在内部将 8000 转接到测试服务，页面仍以准确的 8000 来源执行。 |
| 两个调用方 | 独立网站用 Fetch 预览、提交、查回并读取 404 业务错误。真实预检允许 JSON 与幂等头；单独预检前后 Work、Task 和幂等记录均为零。程序产生 Work `321fadbf-bd0d-444d-91c2-1a223b1e0210`、Task `eab4483b-e887-4fc3-801b-da5aee25c721`；网页产生 Work `82d7ad48-0a2e-49e5-a160-558edab8dc55`、Task `56100b35-c8df-402d-ae14-644945b9e878`。 |
| 断线和版本变化 | 在实际 HTTP 响应发送前、SQLite 提交后断开客户端，移走 YAML 后查回及重放返回原 Task。同键换正文/版本及新请求携旧版本均返回 409。空查回不写库，三项记录数仍各为 2。 |
| 双路径 | 场次 Work `b0cbe3e0-d186-4ae9-b033-55774d7fa3c0` 仅创建 Task `52351264-2ad6-4130-8311-10cf242d4318`。快照和文件内容均证实顺序为 `job-2/video.mp4`、`job-1/video.mp4`；配置越界、素材根越界、素材相对路径穿越均返回 422。增加该场次后三项记录各为 3。 |

Chrome 公开地址空间模拟单独验证了浏览器权限：全新配置报告 `prompt`，拒绝后 Fetch 失败，授予后读取同一回执。该测试使用 Chromium 的地址空间模拟，没有部署公网网站。实际外部站点应按浏览器规则使用安全上下文，向回环发请求时声明 `targetAddressSpace: "loopback"`。见 [Chrome 本机网络访问说明](https://developer.chrome.com/docs/modern-web-guidance/guides/security/local-network-access)。

前五轮保留为失败证据，依次处理端口占用、夹具表名、浏览器夹具来源、严格 DTO 的字符串解析和权限模拟上下文。第六轮通过后冻结第 8.3、9.1 节约定，进入阶段 1。阶段 0 的假数据与最小样页只验证原型。

### 阶段 1：Web 安全入口与统一提交

由 FastAPI 提供面板静态文件。按第 5 节分别接入公开投稿和面板管理操作，落实预览版本、请求查回与重放，补齐作品和任务响应。

统一提交需要服务启动时配置允许的配置目录与素材根。预览有效期为 10 分钟，存于 API 进程内，重启后由用户重新预览；已受理的回执保存在 SQLite。统一入口的新作品保存配置路径、配置版本与可选来源。SQLite 空库版本升为 3，使用独立运行目录，旧库继续由初始化校验只读拒绝。CLI 及桥接接口保留原请求契约。

首轮后端检查通过 22 项，包括原 REST/CLI 契约、新接口与真实 SQLite 回滚/并发、Windows junction 越界和管理来源校验。结果见仓外本轮目录 `phase1-test-1.log`。正式页面和实际浏览器流程验证随以下阶段完成。

### 阶段 2：前端基础和 Registry

Vue 工程、路由、独立/嵌入布局、API 客户端、SSE、错误处理、操作权限展示、Registry、平台能力查询、Bilibili/Fake 注册项。

前端产物由 `npm --prefix web run build` 生成，API 同源提供静态文件。成功构建包括 TypeScript 检查；生成文件留在 `web/dist`，依赖版本由锁文件维护。

### 阶段 3：Dashboard 页面

实现 Accounts、Works、Tasks、Schedules、Overview、Settings；Works 支持只读 YAML 预览和本机登记。

真实 Chrome 已打开六个页面。独立浏览器流程通过仅登记、按预览提交、排队取消、重新提交、Fake Worker 完成、任务筛选、每日改每周及启停。新浏览器上下文下，准确的 MuMu 来源测试页通过 iframe 握手、跳转任务以及刷新查回。扫码页面使用独立合成协议进程验证二维码显示、刷新恢复同一会话、取消、重开和账号回填。该轮没有请求真实平台。

### 阶段 4：Windows 原生目录选择与集成测试

实现交互式目录选择桥接和手动路径备用。验证本机目录准入、独立运行、内嵌页面首次打开及刷新。

通过实际 Windows Shell 选择器从作品页选中带中文的仓外目录，页面回填准确路径。再次打开后取消，路径仍保留，SQLite 保持零 Work、零 Task。手动输入在浏览器提交链路中通过；选择器返回越界目录时由接口拒绝。证据见仓外本轮目录 `native-browser.log`、`native-browser-selected.png`，初始原生实现的可见性与路径编码失败保留在 `native-*.log`。

### 阶段 5：MuMu 接入准备

发布稳定 OpenAPI；使用 MuMu 普通作品 YAML 和场次 Generated YAML 做契约测试；验证跨 Job 多片相对路径和一个 multipart Task；确认幂等和查回。

以实际路由生成 [OpenAPI](../../schemas/openapi.json)，接入说明见 [Dashboard 操作说明](../../docs/operations/dashboard.md)。按 MuMu 第 5 节约定自动生成普通单片和场次 YAML，使用带 Bilibili 配置与合成账号的空库验证。两种配置各创建一个 Task，场次按明确的跨 Job 顺序引用同名素材。移走 YAML 后，按旧键查回和重放均返回原结果。此次使用约定结构生成的合成输入，MuMu 实际生成及 Direct 切换留待双项目联调。

### 阶段 6：双项目联调与发布

MuMu 按配套方案 Direct 切换，双方使用新版创建的任务联调。2026-11-20 先交付并验收 Windows 联调包，2026-11-21 完成台式机整场自动投稿续测。结果见[实机验收结论](#2026-11-21-实机验收结论)。

#### Windows 联调包收尾

2026-11-20 用户明确要求补齐部署标识、恢复作品页原请求，并交付经过实际解压验证的 Windows ZIP。此次修改仅在上传平台仓库进行，使用 MuMu 现有客户端及清单生成代码作为联调验收入口。

1. SQLite 初始化写入唯一部署 UUID，健康接口返回。收到可选部署请求头时，服务先校验部署，再进入业务处理或回执查回。旧库继续遵守现有只读拒绝规则。
2. 作品页在提交前把部署、原请求、幂等键及回执核对字段保存到浏览器本地。同一来源下刷新或离开后可恢复待查请求。查询只读取，重试沿用保存的正文和键。
3. Windows x64 联调 ZIP 包含 Python 运行环境、后端依赖、已构建的 Dashboard、启动脚本和接口契约。解压至全新目录，使用包内 API 和 Worker 及新库验收。用 MuMu 的实际客户端预览并发送合成稿件，等服务器提交后注入回包丢失，再按原键查回并核对 SQLite 和模拟执行回执。另验证同库重启、新库切换、错部署拒绝及浏览器刷新与导航恢复。

2026-11-20 已验收 `fileUploadPlatform-0.2.0-windows-x64.zip`，位置为仓外 `../fileUploadPlatform_tmp/releases/20261120-0.2.0/verified/`。包内为 Windows x64 Python 3.13.17，SQLite schema 4。旧库保留，按新数据目录启动。实际命令见 [Windows 联调包验收](../../tests/README.md#windows-联调包验收)。

| 检查 | 实际结果 |
|---|---|
| 干净解压与启动 | ZIP CRC 检查通过。从带中文的全新解压路径分别运行包内 `Start.ps1 server` 和 `Start.ps1 worker`。Chrome 打开概览并显示 Worker 心跳正常。 |
| 部署绑定 | 同库跨 API 进程重启保持 `d6616da4-40cc-4618-95f6-ce395c54fc05`。新库为 `0a9c6ae6-a79a-4868-9bef-d6a04b412c4a`。对错部署的提交与查回均返回 409，错部署请求前后作品、任务、共享提交记录均为零。 |
| MuMu 客户端与模拟执行 | 使用 MuMu 当前工作区客户端和清单生成器，基线 `4fe8603`，包含当时的未提交客户端文件。预览已核对作品 ID 与版本。服务器返回 201 后在客户端传输层丢弃回包，客户端报告断线。移走 YAML、重启 API，再查回和重放，都返回 Task `e185dd2d-09d7-444b-87fe-48feed10ecad`。原库只有 1 个 Work、1 个 Task、1 条共享提交记录。Worker 以 `job-b`、`job-a` 顺序交接两段模拟素材，模拟执行器只留下 1 份投稿记录。 |
| 作品页恢复 | 真实 Chrome 测试在截断受理回包前核对待查记录已写入。刷新后用只读查回取得原 Task。另一单离开页面后返回并重试，正文、幂等键与部署头都和首次发送一致。每单各 1 个 Task。 |
| 回归 | 后端契约、SQLite、旧入口与部署绑定检查 53 项通过。Chrome 用例 3 项通过。最终 ZIP 端到端 1 项通过。 |

日志位于上述发布目录的父目录，分别为 `build-final.log`、`verify-backend-final.log`、`verify-browser-final.log`、`verify-package-final.log`。解压启动日志、概览截图和任务回执 JSON 保存在 `verify-package-final/`。首轮旧健康响应断言失败，以及验包夹具缺少 MuMu 依赖和测试稿件使用空值两次失败均保留。在隔离目录补装 MuMu 依赖并使用其现有参数模型后复验通过。

上表记录联调包的隔离验收。后续实际 MuMu 控制台和真实 B 站项目见[实机验收结论](#2026-11-21-实机验收结论)。

## 15. 测试与验收

### 15.1 后端自动化

- 统一提交及查回覆盖无浏览器头的程序和不同 Origin 的网页。验证预检无副作用、受理响应可读、业务错误可读。对错误 Host、转发头和越界路径分别验证拒绝原因。
- 针对账号维护等管理写操作验证来源和防跨站请求校验，针对嵌入页面验证 CSP。原 CLI 与 GUI 桥接路由按各自的现行契约回归。
- Manifest 重复键、未知字段、无效 YAML、越界路径、符号链接/重解析、UNC、路径竞态。
- manifest path 与 asset root 分离、场次 YAML 引用多个 Job 目录。
- 尚未受理时，旧预览对应的 YAML 变化应返回冲突。已受理后修改 YAML，原任务快照和原请求回执均保持可查。
- 同 Key 同正文返回同结果；同 Key 不同正文冲突；响应丢失查回不重复建单。
- Work 登记、多 upload unit/target、multipart 和 independent 任务数量正确。
- Task 分页筛选、available_actions 和状态竞争。
- SSE Last-Event-ID、断线恢复、重复事件去重。
- Schedule daily/weekly、expected_version、Run 摘要。
- 在本版本测试库中新建作品，按原 REST 契约登记及提交，核对请求重放和业务响应。

### 15.2 前端自动化

六大页面加载、空/错误状态、分页筛选；Registry 能力驱动；Bilibili 扫码生命周期；Works YAML 只读预览和版本冲突；Task 时间线、Attempt、Event、Receipt、UNCERTAIN 核验；Schedule 启停和版本冲突；独立/嵌入布局共用业务状态。

### 15.3 Windows 与真实平台

- 不启动 MuMu，上传平台可独立使用。
- MuMu iframe 可加载账号和任务页面。
- 初次进入和刷新 iframe 都能打开目标页。B 站扫码操作进行中时，重开账号页能找回同一次操作。
- 非白名单页面不能嵌入；伪造 postMessage 不改变权限。
- Windows 原生目录选择器在实际服务启动方式下工作；取消、非法路径和手动备用路径正确。
- Worker 可访问原始素材路径，平台不复制/移动视频。
- API/Worker 分进程启动、重启后状态一致。
- 使用授权 Bilibili 测试账号验收单片、多 P、仅自己可见、实际回放、BV 号、异常重试和结果不确定查回。Fake 通过不能代替真实平台验收。

### 实际验收记录

以下为 2026-11-20 隔离验证结果，使用本方案阶段 0 的同一仓外证据目录。每轮测试均使用新的 SQLite 库及合成数据。

| 检查 | 结果与证据 |
|---|---|
| Core 全套回归 | 234 通过，42 跳过。跳过项需另行构建的 Rust 协议 Fixture 和 HTTP 测试模块。本机现有引擎为非 Fixture 构建，未用于测试。详见 `phase1-regression.log`。 |
| 发布候选契约与定时 | 41 通过。覆盖正式统一提交、真实 SQLite 并发与回滚、版本与查回、目录边界、多单元多目标的五任务展开、旧接口、MuMu 合成配置及日程。详见 `final-contract-3.log`。 |
| 执行与恢复补测 | 19 通过。覆盖调整后的远端查询前置检查、SQLite 版本校验、CLI、独立 API/Worker、Windows 执行和 SSE 续接。详见 `final-regression.log`。 |
| 正式页面与 API | 前端类型检查与构建通过。2 个 Chrome 端到端用例通过，覆盖六页、iframe、跨来源真实 Fetch 预检及回执、API 进程重启后查回、独立 Worker 模拟执行，以及合成扫码会话。详见 `web-build-3.log`、`browser-4.log`；早期浏览器夹具对预检事件的观察失败已保留，最终用 Chrome DevTools 协议观察。 |
| Windows 原生对话框 | 实际从 Chrome 页面触发，选中中文仓外目录并正确回填；随后取消保留原路径，数据仍为空。详见 `native-browser.log`。 |
| 文档与静态检查 | 相对链接、标题锚点、生成 OpenAPI 及本次 Python 文件格式检查通过。全仓 Ruff 格式检查报告基线文件 `tests/integration/test_bilibili_real_engine.py` 的三个已有排版差异。 |
| 双项目和真实 Bilibili | 本段隔离验证未覆盖。2026-11-21 续测见下方。 |

### 2026-11-21 实机验收结论

MuMu 与 fileUploadPlatform 均为 0.2.0，B 站引擎为 `bue-0.2.0-dev6`。以下结论来自 MuMu 提交 `86b2809` 的真实使用测试记录，以及用户对创作中心的确认。[MuMu 整场自动投稿真实使用测试记录](../../docs/测试/报告/20261120-MuMu整场自动投稿真实使用测试记录.md)

| 项目 | 结论与证据 |
| --- | --- |
| 真实扫码与账号设置 | 通过。首次二维码到期后，续测扫码成功，平台显示 1 个可用账号，账号在主播设置中可选、可保存。已打开的下拉框是否自动更新仍待验。 |
| 调度与整场自动交付 | 通过。MuMu 自动开录并记录场次标题。对本场主动停录后，首片封口，MuMu 自动关联原上传任务。生成的 `upload.yaml` 与平台快照在单 P、分区 `21`、转载及 `self_only` 上一致。 |
| 真实收稿与可见范围 | 通过。台账为 1 个 Work、1 个 Task，首次尝试即 `SUCCEEDED`，回执为 `BV1TEyszVErf`。用户在 B 站创作中心确认该稿件存在，显示「仅自己可见」。访客获取公开详情时返回 `62012`。 |
| 控制台关联与远端查回 | 通过。重新打开主播页和切片页，两处「查看任务」均加载任务 `b4206a92-1056-4a60-9046-c67fe771ed17`。在该页执行远端查询，返回 `query_verified=true` 和同一 BV。 |
| 本地录像音画 | 通过。对约 21 分 21 秒的正式录像分头、中、尾解码，三处均有画面及音频信号。 |
| B 站原稿回放及其余实机项目 | 待验。包括投稿账号实际回放音画、创作中心数量及投稿信息核对、手动单片、真实多 P、扫码后原下拉框自动更新、实际部署重启后的前端回显和真实执行异常场景。完成条件已转入[后续待办](../../docs/待办.md#dashboard-与-mumu-验收余项)。 |

## 16. 交付物与完成定义

交付免登录面板、支持不同调用方的统一提交与查回接口、Manifest 双路径契约、Preview/version、幂等恢复、OpenAPI、Vue Dashboard 六大页面、Registry、iframe、Windows 原生目录选择、任务操作 DTO、Schedule Run 摘要、自动化报告和真实 B 站验收记录。

完成标准：

- 上传平台可独立运行，不依赖 MuMu。
- 独立和嵌入面板首次均直接可用。本机程序和其他来源的网页均可按公开契约提交并读到回执，管理操作及现有 CLI/桥接接口各自通过对应的入口验证。
- 普通作品和自动场次作品均由独立 YAML 表达。
- Manifest path 与素材 asset root 安全且通过真实 Windows 测试。
- Preview 与提交严格版本一致，Task Snapshot 不受 YAML 后续变化影响。
- MuMu 通过统一提交 API 创建 Work/Task，超时不重复建单。
- Dashboard 六大页面完成；账号和任务可 iframe 嵌入。
- 每日/每周 Schedule 可管理。
- Bilibili 和 Fake 使用统一后端框架；不声称百度网盘已实现。
- 自动化、浏览器、Windows 原生目录选择、隔离 Core 和真实 B 站验收有可追溯证据。

## 文档同步与修订记录

2026-11-20 已将本次访问、投稿与历史范围同步到[需求说明](../../docs/需求说明.md)、[设计说明](../../docs/设计说明.md)和[账号术语](../../CONTEXT.md#术语)。本方案、相关正文和方案入口的内容、相对链接、标题锚点及差异检查通过，两份 JSON 示例可解析。此记录只覆盖文档修订。

同日核对时，MuMu 配套方案第 8.1 节仍列有到原部署查询旧任务的验收。上传平台的阶段 0 和交付验收采用本方案第 1 节的版本范围。产品实施时按实际路由更新公开契约，并将可运行的验证入口和使用方式同步到测试与操作说明。

## 17. 归档结论

- 归档日期为 2026-11-21，类型为「转交」。用户确认 fileUploadPlatform 0.2.0 本轮联调交付可以收尾。
- 本轮 Windows 联调包及已实测的整场单 P 自动私密投稿链路通过。实机结果、创作中心确认范围与证据位置见[实机验收结论](#2026-11-21-实机验收结论)及[抄录报告](../../docs/测试/报告/20261120-MuMu整场自动投稿真实使用测试记录.md#续作和证据)。
- 未实测项目由[上传平台待办](../../docs/待办.md#dashboard-与-mumu-验收余项)承接，其中跨项目事项对应 MuMu 待办 T16、T23。余项继续独立验收，按用户确认不阻塞 0.2.0 本轮联调交付归档。
- 归档检查覆盖 20 份变更文档，375 处仓内链接、其中 217 处标题锚点，以及 12 处跨仓库提交链接均可定位。抄录报告与 MuMu 原文一致，差异为来源说明和两处跨仓库引用。本次归档沿用既有验包与台式机实测证据。
