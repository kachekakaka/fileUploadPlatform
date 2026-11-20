# Core MVP 本机运行

本文介绍 Windows 本机 Core 的通用运行和 Fake 上传闭环。API 与 Worker 是独立进程，共用本机可靠文件系统上的 SQLite；CLI 的业务操作只走 HTTP。六页面、统一投稿和空库启动见 [Dashboard 操作说明](dashboard.md)。Bilibili 引擎、账号操作及验收边界见 [Bilibili 操作说明](bilibili.md)。百度网盘、SDK、网页文件接收、Cron、远程访问和历史库升级尚未实现。Fake 验收证据见[实施结果](../../archive/方案/20261116-Core-MVP实施方案.md#产品实施结果)。

## 安装与启动

在仓库根目录使用 PowerShell 7（`pwsh`）及 Python 3.11 或更高版本；本次实测 3.11.1。环境和数据放仓外，以下路径可换为自己的本机绝对路径：

```powershell
$runtimeRoot = Join-Path (Split-Path (Get-Location) -Parent) 'fileUploadPlatform_datas'
$venvRoot = Join-Path $runtimeRoot 'venv'
python -m venv $venvRoot
$pythonExe = Join-Path $venvRoot 'Scripts/python.exe'
& $pythonExe -m pip install -r requirements-dev.lock
& $pythonExe -m pip install --no-deps -e .
$env:UPLOAD_PLATFORM_DATA_DIR = Join-Path $runtimeRoot 'core-mvp'
& $pythonExe -m upload_platform server
```

另开终端，设置同一个 `UPLOAD_PLATFORM_DATA_DIR`，用同一虚拟环境执行 `python -m upload_platform worker`。安装后的等价入口为 `upload-platform`。第三个终端执行 CLI，例如 `python -m upload_platform doctor`。API/数据库 ready 与 Worker active 分开返回；只有 API 启动时任务可以排队，但不会执行。

server 固定绑定 `127.0.0.1`，拒绝转发头和错误 Host。CLI 使用本机程序入口，浏览器按 Dashboard 和公开投稿入口各自的规则访问。当前只支持本机使用。来源限制不用于识别恶意本机进程。[Dashboard 与网页接入](dashboard.md#程序和网页投稿)

## 配置

API 与 Worker 的数据目录必须相同；更换环境使用新目录，不把未经验证的旧数据库当空库覆盖。

| 环境变量 | 默认值/要求 | 用途 |
| --- | --- | --- |
| `UPLOAD_PLATFORM_DATA_DIR` | 必填，本机绝对路径 | 数据库、执行结果和日志根目录 |
| `UPLOAD_PLATFORM_API_URL` | `http://127.0.0.1:8765` | API 端口与 CLI 目标，必须含端口 |
| `UPLOAD_PLATFORM_REQUESTS_DIR` | DATA_DIR 下的 `requests` | 请求日志；可指定另一仓外绝对目录 |
| `UPLOAD_PLATFORM_HEARTBEAT_SECONDS` | 10 | Supervisor 心跳秒数 |
| `UPLOAD_PLATFORM_LEASE_SECONDS` | 30 | 租期，须大于心跳周期 |
| `UPLOAD_PLATFORM_HANDSHAKE_TIMEOUT` | 10 | 握手等待秒数 |
| `UPLOAD_PLATFORM_CANCEL_TIMEOUT` | 10 | 取消及退出等待秒数 |
| `UPLOAD_PLATFORM_EXECUTION_TIMEOUT` | 300 | 每次 Fake 执行总时限秒数 |
| `UPLOAD_PLATFORM_POLL_SECONDS` | 0.25 | Worker 轮询周期 |

数据库为 `core.sqlite3`，使用 WAL、外键、synchronous=FULL、5 秒 busy timeout。首次空库初始化创建 schema 和唯一 `fake-default` 账号；重启核验版本、结构与外键，不重建或清空旧记录。版本或结构不支持时停止并保留原库，迁移见[待办](../待办.md)。

## 准备、登记与提交作品

每个作品目录有自己的 `upload.yaml`。`upload-platform work prepare --dir <作品目录>` 生成待编辑模板及固定 UUID，重复运行复用编号，模板 ready 为 false。已有编号非法、重复 YAML 键或独占锁均会报错，不替换身份或抢占锁。

以下示例中，编号应换为 prepare 生成的实际值，素材相对于作品目录：

```yaml
version: 1
work:
  id: "6e4f1a80-6b25-4f30-9d0c-12b38ad78591"
  ready: false
  title: "示例作品"
assets:
  videos:
    - id: part-1
      path: first.mp4
    - id: part-2
      path: second.mp4
upload_units:
  - id: full-episode
    mode: multipart
    videos: [part-1, part-2]
    targets:
      - platform: fake
        account: fake-default
```

`multipart` 将视频组成一份投稿；`independent` 每个视频分别创建任务。封面引用 covers ID，字幕通过视频 subtitles 引用。覆盖、清空与分 P 规则见[设计 D02](../设计说明.md#d02-领域与数据约束)和[契约](../../schemas/README.md)。未知字段、重复逻辑 ID、目标能力错误或越界文件引用会拒绝整个作品的新提交。

```text
upload-platform work ready --dir <作品目录>
upload-platform work register --dir <作品目录>
upload-platform work preview <work-id>
upload-platform work upload <work-id>
upload-platform work upload <work-id> --at 2026-12-01T08:00:00+08:00
upload-platform work status <work-id>
upload-platform task status <task-id>
```

`register --manifest <作品目录/upload.yaml>` 等价于 `--dir`。register 不创建任务；手动 upload 不要求 ready，周期选材要求 ready=true。prepare/register 不替准备方声明就绪。

文件只保存目录引用、相对路径与属性；API/Worker 打开时复核最终路径、普通文件和可读性。不复制媒体、不扫描内容或比较变化、不删除原件。执行中须保持路径可用；移动后登记同一编号，新任务采用新映射，既有任务仍指向原位置。

上传前展示投稿信息、分 P、来源层级及 `create_new_submission` / `reuse_current`。改变单元 ID、mode、视频逻辑 ID 或目标可能生成新投稿，普通标题修改不改变身份；已有任务返回原快照。`--json` 可放在命令任意位置，修改命令输出 request_id 和 operation_result；预览写 stderr，stdout 保留单个 JSON。

## 查询、取消与再次提交

```text
upload-platform task list --limit 50
upload-platform task attempts <task-id>
upload-platform task events <task-id>
upload-platform task events <task-id> --follow --after-sequence 0
upload-platform task cancel <task-id>
upload-platform task reupload <task-id>
upload-platform task reschedule <task-id> --at 2026-12-02T08:00:00+08:00
```

列表用 next_cursor 和 `--cursor` 续页；事件用任务内持久序号续接，SSE 至少一次投递，消费者按 event ID 去重。Event 是持久来源，Outbox 发布时间是投递进度，不决定重放资格。

取消返回“已取消”“取消请求已记录”“已成功，取消未生效”或“结果不确定”等实际状态。reupload/reschedule 仅允许结果明确且仍为当前轮次的终态任务，保留旧任务/Attempt/回执；普通 upload 与周期触发关联当前轮次。

上传和重提支持 `--max-attempts`、`--retry-delays-seconds 30,120`；关闭自动重试用 `--max-attempts 1 --retry-delays-seconds ""`。间隔数须为总尝试数减一。定时操作另支持 `--max-lateness-seconds`，默认 86400；立即执行不能提供该参数。安全失败才等待重试，永久失败/不确定结果不自动重传。

## 每日、每周规则

```text
upload-platform schedule create --dir <父目录> --kind daily --time 08:00:00
upload-platform schedule create --dir <父目录> --kind weekly --weekday 1 --time 08:00:00 --timezone Asia/Shanghai
upload-platform schedule list
upload-platform schedule show <schedule-id>
upload-platform schedule update <schedule-id> --time 09:00:00
upload-platform schedule disable <schedule-id>
upload-platform schedule enable <schedule-id>
upload-platform schedule runs <schedule-id>
upload-platform schedule run <run-id>
```

只选父目录的直接子目录作品，不递归、不后台补号或标记 ready，也拒绝重解析后逃出直接子目录边界的候选。清单落库后本轮固定；作品错误独立记录，数据库故障停止剩余派发，恢复后接续。手动、每日和每周共用投稿去重记录。派发完成不等于上传成功，run 同时返回任务当前状态。

默认时区 Asia/Shanghai；weekday 使用 ISO 1—7。迟到默认 24 小时，等于上限仍执行；错过多轮只补最近计划点的一轮。夏令时不存在时刻跳过、重复时刻取第一次。修改使用 expected_version，冲突不覆盖；改时/重新启用从操作后的下一计划点开始，仅改重试等配置保留原计划点。停用不取消既有任务或已物化触发。

## 请求重放与人工处置

修改命令先可靠保存 `requests/<request-id>.json`，再发送 HTTP。响应丢失时用 `upload-platform request resume <request-id>`，保持原 Key、路由和正文，不重新读取已修改/移走的 manifest；分别显示原操作响应和当前任务。日志保存失败不发送，服务不可达不退回直接写库。退出码 0 表示命令成功，2 表示校验/状态冲突或本地输入错误，3 表示服务/请求日志不可用。上传结果须看任务状态及回执。

UNCERTAIN 先查详情、events、attempts。Fake 的 `observation.json` 是模拟观察记录；详情的 evidence_ref 为 `fake://<execution-id>/observation`。只有依据可查、身份/结论匹配且旧执行确认退出才可终结：

```text
upload-platform task resolve <task-id> --conclusion succeeded --reason "核对模拟观察记录" --evidence <evidence-ref>
upload-platform task resolve <task-id> --conclusion failed --reason "模拟记录显示未成功" --evidence <evidence-ref>
upload-platform task resolve <task-id> --conclusion uncertain --reason "仍无法确认"
upload-platform task resolutions <task-id>
```

原 UNCERTAIN Attempt 不改写，人工回执标记 manual 和 fake；确认未成功不会自动再传，须显式重提。该流程不连接真实平台。

## 进程与排查

每个 Attempt 独占 `executions/<attempt-id>/`，保存结果、观察记录、脱敏 stderr 及适用的协议错误记录。Worker 先写启动意图，挂起创建宿主并加入禁止 breakaway 的命名 Job，再保存 PID/创建时间、恢复线程；握手后先持久化许可再发送 GO。Windows 启动身份使用本机名与 WMI LastBootUpTime，无法查询则不启动 Worker。

租约过期只触发恢复接管。身份不符或启动窗口缺少可靠进程身份时，保持 RECOVERY_PENDING 和占用名额，并提供 recovery.blocked 原因；这是当前明确的保守行为。不要手改数据库释放名额或强改成功，应保留数据库、执行目录与进程证据排查。确认旧启动期结束或整个 Job/宿主退出后，才分别判断结果与释放名额；停止进程不证明远端动作未发生。

`job_exit_unconfirmed` 表示 Job 已无法打开且没有可复用的退出证据；宿主退出或结果文件存在均不能排除存活子进程，因此继续占用名额。正常监督会在关闭 Job 句柄前持久保存核验后的退出证据及 `attempt.exit_confirmed` 事件；若此后崩溃，恢复可以复用证据而不重新执行。退出确认事件本身不表示上传完成，仍应查询任务结论和回执。

关闭前停止新提交、等待活动任务结束，再关闭 Worker/API。Ctrl+C 时已启动监督会等待其受限执行结束；异常终止后下一 Worker 按租约与证据恢复。只结束自己启动且身份已核验的进程，不按名称清理。数据库、执行结果、请求日志需一起保留；本轮未提供备份恢复或历史库迁移命令。

## Fake 场景

target.options 支持 scenario、duration_seconds（0—300）、failures_before_success。默认 success、0.2 秒；retryable 至少首次安全失败，可配置连续失败次数。场景包括 success、retryable、permanent、crash、cancel_safe、cancel_uncertain、invalid_result、late_result、child、hang、no_handshake、crash_before_go。故障场景只用于隔离测试；child 在 Job 中启动子进程，hang 用于超时边界；late_result 忽略取消并在 duration_seconds 后写入真实模拟成功结果，用于验证取消竞争及恢复接管后的迟到结果。若进程先被超时终止，则不会产生结果。回执与观察记录都是模拟，不宣称真实平台 exactly-once。
