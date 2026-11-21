# Bilibili 本机运行与验收

mumu 本机账号页已通过真实扫码登录验收。当前构建支持只读验证、显式续期、账号启停、一次交接上传和主动稿件查询。当前项目的本机普通入口已开放单 P 和多 P 的公开、仅自己可见和空间列表隐藏设置。[普通入口开启记录](../../archive/方案/20261116-Bilibili集成与自维护方案-v1.2.md#普通投稿开放私密和空间列表隐藏) · [扫码验收记录](../../archive/方案/20261117-mumu界面扫码登录接入方案.md#14-本机真实扫码验收)

## 部署边界

引擎源码独立保存在私有仓库 [bilibili-upload-engine](https://github.com/kachekakaka/bilibili-upload-engine)。构建 `cargo build --locked -p bilibili-upload-engine`，固定一个版本目录放置 EXE 和其 `capabilities` 原始输出。生产构建不含 `protocol-fixture` feature；测试构建不得用于真实账号。运行中不替换该目录内 EXE，更新使用新版本目录并核对排队快照兼容性。

沿用[Core 安装说明](core-mvp.md#安装与启动)。本版本数据库 schema 为 2，只接受空库或完全匹配的本版本库；旧版库在只读检查阶段拒绝，不迁移或修改其 journal。新建独立 Core 数据目录，保留旧库原样。

`UPLOAD_PLATFORM_BILIBILI_CONFIG` 指向仓外 JSON 文件，内容如下（全部换为自己的绝对路径）：

```json
{
  "executable": "D:/runtime/engines/bue-0.2.0-dev6/bilibili-upload-engine.exe",
  "build_id": "bue-0.2.0-dev6",
  "state_root": "D:/runtime/bilibili-private",
  "capabilities_file": "D:/runtime/engines/bue-0.2.0-dev6/capabilities.json",
  "allow_self_only": true,
  "allow_hide_in_space": true
}
```

`allow_self_only` 和 `allow_hide_in_space` 分别让本机普通入口接受引擎支持的私密和空间列表隐藏选项。省略时沿用引擎的已验证声明。Server 和 Worker 都应读取同一份配置，变更后重启相关进程。能力文件保留引擎原始输出。

私有状态根必须在 Core 数据目录外，所有本机 Worker/维护入口共用同一根。引擎创建限制为 Owner Rights、SYSTEM、Administrators 的 Windows ACL；凭据按 MID 与操作 UUID 发布为独立版本，旧版本保留。Core 只保存版本引用，不读取、复制或记录 Cookie。备份必须包含数据库、维护结果与其引用的凭据版本；不要只复制单个新凭据后删除旧版。暂不提供自动凭据清理。

## 使用成品登录

在上传平台 Dashboard 打开「账号」，或从 MuMu 0.2.0 主播设置进入「自动投稿 → 管理投稿账号」。点「添加 Bilibili 账号」或「重新登录」，再开始扫码。账号登记在扫码时使用的 Core 库里，凭据保存在对应引擎的私有目录。[Dashboard 操作说明](dashboard.md#面板操作)

### 终端登录

Server、Worker、CLI 使用同一 `UPLOAD_PLATFORM_DATA_DIR`、`UPLOAD_PLATFORM_BILIBILI_CONFIG`、`UPLOAD_PLATFORM_API_URL`。在自己的 PowerShell 7 终端运行：

```text
upload-platform server
```

另一个终端运行以下命令。扫码只显示在当前登录终端，不写普通日志；无需将 Cookie 发给协作者：

```text
upload-platform account login
upload-platform account list
upload-platform account validate <account-id>
upload-platform account login --account-id <account-id>
upload-platform account login --import-file <绝对路径的凭据JSON>
upload-platform account renew <account-id>
upload-platform account disable <account-id>
upload-platform account enable <account-id>
```

导入选项接受与该引擎 `LoginInfo` 相容的完整 JSON（含 cookie_info、token_info），不把任意浏览器 Cookie 字符串当作有效凭据；引擎仍独立核验 MID，Core 只记录本地来源路径和最终版本引用。优先使用扫码登录，不需要手工导出凭据。

Worker 单独运行 `upload-platform worker`。它启动时为已有 Bilibili 账号安排一次只读验证；凭据失效时安排一次非交互续期，不让上传进程隐式刷新，也不循环扫码。续期失败可使用重新登录。登录维护会暂停新领取、等待旧执行及普通查询结束；不会停止已有上传。成功或失败后的旧凭据验证均保留手动停用状态。

首次登录发现已登记 MID 时，CLI 将引擎核验的身份交给 Core，先进入原账号维护域，等待退出后才允许发布版本，不增加第二套上传名额。登录取消或失败后，只有独立验证旧凭据仍有效且 MID 正确，才解除本次暂停；否则保留凭据阻塞并允许新的显式登录。

CLI 显示维护操作 ID。终端或服务中断后使用 `upload-platform account resume <operation-id>` 继续核验原操作，不再盲目启动扫码。若操作卡在启动窗口且缺少可靠进程身份，保持暂停；不能以 PID 不存在、文件存在或租约超时解除。命名 Job 已丢失且无持久退出证明时，需先确认原机器重启所形成的退出证据后再恢复。服务返回的操作结果与失败原因保留，恢复不覆盖原失败。

## 界面扫码接口（Core / 引擎侧）

新版本 Core 已提供托管扫码接口，要求引擎固定能力含 `login_interaction.version=1`、`terminal/machine` 模式和 `image/png`，对应新构建 `bue-0.2.0-dev1`。旧引擎仍可使用原终端登录，但能力查询会报告图形登录不可用。升级使用新的 EXE/能力版本目录和匹配的配置；不要覆盖活动版本或直接切换仍有旧版本维护/执行的部署。已有业务库不迁移；本轮未自动切换此前仓外运行目录的引擎配置。

接口均在 `/api/v1` 下，由同机受信任客户端调用，仍拒绝浏览器 Origin/Sec-Fetch 请求。现有 `cli-v1` 可用于联调；新增 `X-Upload-Platform-Client: gui-bridge-v1` 仅允许会话入口和 health/capabilities/accounts summary 只读入口。客户端标识不是认证，不据此开放远程访问。所有修改请求需要 UUID `Idempotency-Key` 和 `application/json`。

| 操作 | 接口 |
| --- | --- |
| 查询兼容能力 | `GET /system/capabilities` |
| 新登记或重登 | `POST /account-login-sessions`，正文 `{"platform":"bilibili","account_id":null}`；重登填已有账号 ID |
| 查询/找回会话 | `GET /account-login-sessions/{id}`；`GET /account-login-sessions?active=true`（最多 100 项） |
| 读取 PNG | GET 状态返回的 `challenge.image_path`；尚未就绪为 409，失效为 410 |
| 请求取消 | `POST /account-login-sessions/{id}/cancel`，正文 `{}` |
| 恢复原操作 | `POST /account-login-sessions/{id}/resume`，正文 `{}` |
| 刷新账号摘要 | `GET /accounts?view=summary` |

创建返回 202，随后轮询状态；`outcome=succeeded` 才表示凭据和 MID 已核验并提交，`account_summary.availability` 继续区分 ready/disabled/blocked。创建响应丢失时复用原幂等 Key。修改接口重放可返回同一会话的最新安全视图；查询使用 GET，不重复创建操作。低层维护 start/process/identified/complete 不允许外部推进托管扫码会话。

二维码只在内存中保存；图片/会话响应 `no-store`，客户端不得落盘、缓存或记录 PNG/扫码 URL。终态、取消请求或失效立即停止提供图片。当前平台适配只将已证实的未确认状态显示为等待确认；未知平台码、网络错误与本地超时用稳定错误码，未声称能可靠区分“已扫码/平台已过期”。180 秒为本机扫码截止，不是平台授权撤销保证。

关闭页面不发取消，重新进入可找回原会话；刷新二维码需要原会话安全结束后以新 Key 新建。取消 202 仅表示已受理，可靠成功已形成时仍可能返回成功。取消尚未启动的重登不会停止旧上传，验证旧凭据后解除本次暂停；身份或退出证据不全时保持 recovering，不能以取消按钮强行解除。

Core 重启后通过原操作恢复已发布的凭据版本。旧进程身份不明时保留暂停，界面停止提供旧二维码。再次扫码由用户显式发起。登录只需 Core API 在线。MuMu 0.2.0 从上传平台内嵌账号页管理扫码，账号状态以原部署为准。[Dashboard 操作说明](dashboard.md#面板操作) · [会话契约](../../schemas/README.md) · [旧版桥接验收](../../archive/方案/20261117-mumu界面扫码登录接入方案.md#14-本机真实扫码验收)

## B18 受控投稿入口

普通部署沿用引擎的发布能力。B18 专用部署另用一份本机清单，指定一个 MID 和两份作品。单 P 占一个名额，两段合成一份稿件的多 P 占另一个。该入口已用本地模拟引擎验证，指定两份作品的真实试投已获授权。

1. **准备清单。** 在两个作品目录外的仓外专用目录保存一份[清单示例](b18-allowlist.example.json)。示例中的账号、编号和路径均为占位值。用本轮固定的 UUID 填 `run_id`，填入专用 Core 数据目录的实际绝对路径、固定引擎构建和目标 MID。对两份作品分别填写 `work prepare` 生成的作品编号、实际目录、上传单元编号、最终标题、简介、标签、封面相对路径及投稿选项。逐个填写视频编号、相对路径和分 P 标题，多 P 的数组顺序就是投稿顺序。文件按本地路径引用，验收期间应保留核对过的素材。
2. **只在专用部署启用。** 在该部署的引擎配置 JSON 中增加一项 `"b18_allowlist_file": "D:/b18-example/private/allowlist.json"`，替换成实际清单绝对路径。Server 和 Worker 使用同一部署配置、数据目录和引擎私有状态根。Core 在处理新的登记、预览和提交时重新读取清单。若清单缺失、无效或核对不符，返回 `409 b18_not_permitted`。重放已完成的同一请求可取回原结果。受控 Worker 只领取 B18 任务，普通 Worker 只领取普通任务。
3. **按顺序使用。** 通过原有 CLI 登记并预览两份作品。单 P 只允许手动立即提交，参数为 `--max-attempts 1`。单 P 获得引擎回执后，执行 `task query <单 P 任务编号>`。最新一次独立查询命中该稿件后，才可手动提交多 P。现场远端内容的核对项目见[B18 验收步骤](../../archive/方案/20261116-Bilibili集成与自维护方案-v1.2.md#214-b18-真实投稿验收)。定时、周期、重新投稿均不能使用这两个名额。并发提交、重开服务或更换请求编号都会沿用原任务。任务取消、失败或结果不明也保留已用名额。
4. **结束或暂停。** 将清单移出配置指向的位置即可阻止后续交接。Worker 在把请求交给引擎的事务中会再读一次清单，也会再次核对多 P 的前置查询。已经交接的任务按原任务号查结果。确认收尾后，从专用部署配置移除 `b18_allowlist_file` 并重启对应 Server 和 Worker。保留原数据库、任务记录和本轮编号。

经明确授权可在原清单中加 `supplement` 对象，填入 `single_task_id`、`failed_task_id` 和 `failed_attempt_id`，并绑定本次编译的引擎版本。该入口核对原多 P 的 `VIDEO_UPLOAD_FAILED` 明确未投稿证据和原单 P 的独立回查，再用 `upload-platform task reupload <failed-task-id> --max-attempts 1` 创建唯一后继任务。请求响应丢失时沿用原请求编号恢复。两项原名额和历史继续保留。现场前提及验证见[多 P 补测](../../archive/方案/20261116-Bilibili集成与自维护方案-v1.2.md#2310-b18-多-p-补测)。

私密试投用独立清单，设置 `self_only_trial: true`，只填写一份 `single`，其中 `publish_mode` 为 `self_only`。引擎须声明 `supports_self_only`。使用 `--max-attempts 1` 手动提交，按同一稿件号回查创作中心、投稿账号播放和未登录访问，然后撤下清单。[私密试投验收](../../archive/方案/20261119-B18仅自己可见验收.md)

经单独授权只试投一份公开两 P 时，用独立清单设置 `public_multi_trial: true`，只填写 `multi`，选择 `public` 并关闭空间列表隐藏。使用 `--max-attempts 1` 手动提交，核对同一稿件的回执及两个分段后撤下清单。[候选版公开多 P 验收](../../archive/方案/20261119-B18公开多P线路修复与补测.md)

## 投稿配置与查询

Target 必须显式选择 `platform: bilibili`、`account: bilibili:<MID>`，以及 `category_id`、`copyright`、`publish_mode`。`copyright=2` 要求非空 `source`，自制稿的来源留空。Core 会拒绝字幕引用、未知选项、超限标题及标签。公开选 `publish_mode: public`，仅自己可见选 `publish_mode: self_only`。两种方式均适用于单 P 和多 P。执行前按账号查询分区能力。

通过 `upload.yaml` 或 API 在 Bilibili Target 的 `options` 填 `hide_in_space: true`，表示在个人空间的投稿列表中隐藏，默认关闭。它与 `publish_mode` 独立。选择公开并开启隐藏时，稿件仍按公开范围发布，通过直链仍可访问。引擎须支持所选项，普通入口同时依据部署的放行配置和引擎的已验证声明判断。[本机设置与验证范围](../../archive/方案/20261116-Bilibili集成与自维护方案-v1.2.md#普通投稿开放私密和空间列表隐藏)

本地只引用文件；引擎打开实际句柄后校验最终路径，按快照顺序上传，不 glob、不复制、删除或扫描媒体内容。单视频分片并发为 1，同 MID 最多三个 Task，最终投稿持账号 OS 锁串行。Core 不给 Bilibili 上传或等锁设置总时限。

```text
upload-platform task query <task-id>
upload-platform task queries <task-id>
upload-platform task status <task-id>
```

主动查询保存审核状态和时间；失败保留上次可靠状态及本次错误，查询代次阻止迟到结果覆盖。审核退回不改写原稿件已受理事实。UNCERTAIN 可做一次有界查询；没有 ID 时只按时间窗查候选并核对远端视频键，最多三页、二十候选、九十秒。无记录、多候选、查询不全或失败均不能证明未投稿。

当当前执行的结构化证据记载 B 站最终提交返回业务码 `601`，且任务处于「结果待核验」，Dashboard 原任务页提示「B 站尚未确认收稿，可能需要本人验证。请先查回原任务，避免重复投稿。」先从同一任务核对收稿。现有回查和重试规则继续适用。

交接事务提交前可以取消；提交后 API 返回 `409 cancellation_not_supported_after_handoff`，不发送 cancel。只有完整受控 `not_submitted` 证据和进程树退出证明，才按原重试预算安排下一 Attempt。响应丢失、异常退出和未知结果不重发 add，不切换投稿 API。

人工核验后仍可用既有 `task resolve --conclusion failed --reason ... --evidence ...` 确认未成功，要求旧树已退出并保留依据。结论标记人工来源，原 Attempt、异常、阶段和历史保留。普通任务可通过显式 `task reupload` 创建新投稿。B18 任务继续占用原名额。

## 日志和故障记录

查询请求中断时，使用 CLI 返回的 `request_id` 执行 `request resume`，核验原查询进程退出并读取其持久结果；不要新开查询来掩盖未结束的查询占用。登录等待阶段通过只读状态查询等待，不每秒生成新的维护启动请求。

Core 的执行目录、维护目录、查询目录分别保存结构化结果和进程身份。引擎原始 stderr 可能含第三方诊断，因此非交互运行只保存“收到 stderr”的有界标记并持续排空；具体错误取结构化错误码。终端模式二维码直接显示于用户终端；机器模式由专用 PNG 接口提供，不写入这些日志。保留失败现场，不以删除执行目录、旧凭据或旧库来恢复任务。

包含分 P 诊断的引擎可用 `upload-platform --json task status <task-id>` 查看 `attempt.result.error.code`，其中 `P2` 按快照顺序指第二 P。`PREUPLOAD_FAILED` 指申请上传，`VIDEO_UPLOAD_FAILED` 后的 `INIT`、`READ`、`CHUNK`、`COMPLETE` 分别指初始化上传、打开读取流、读取或传输分片、确认该 P 上传完成。末尾的 `HTTP_503`、`TIMEOUT` 等为安全错误分类。旧引擎只保存原有通用错误码。验证和部署状态见[多 P 传输故障排查](../../archive/方案/20261116-Bilibili集成与自维护方案-v1.2.md#239-多-p-传输故障排查与本地修复)。
