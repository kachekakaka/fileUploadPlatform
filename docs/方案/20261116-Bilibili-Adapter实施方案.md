---
创建日期：2026-11-16
更新日期：2026-11-16
状态：待确认
关联需求：Bilibili Adapter 首版接入
关联设计：Adapter 边界、Credential Manager、Task / Attempt / Lease / Recovery、UploadWork / UploadUnit
实施授权：尚未取得本方案范围的实施指令
---

# Bilibili Adapter 实施方案 v1.0

## 1. 目标与已确认决策

为现有 Upload Platform 增加首个真实平台 Adapter：Bilibili 网页投稿后端。方案以现有 Core 为基础，不重新实现队列、调度、Lease、Worker 和通用恢复。

| 项目 | 已确认决定 |
|---|---|
| 登录 | 网页扫码登录，获取并持久保存 Cookie / 必要 Token |
| 上传协议 | 首版采用网页投稿协议，不以开放平台 OAuth 为前置条件 |
| 开源参考 | 深入研究 biliup 源码，自主实现 |
| 投稿范围 | 单 P + 多 P |
| 并发 | 同一账号最多 3 个视频上传执行单元；单视频分片并发首版为 1 |
| 上传前检查 | 基本检查，其余交由平台响应判定 |
| 重复投稿 | 默认阻止，用户显式发起重新上传 |
| 可见范围 | 每个作品/上传单元可配置 |
| 成功定义 | 投稿受理并取得 AID/BVID；审核状态独立跟踪 |
| 字幕 | 首版不实现上传，保留素材关联扩展点 |

网页扫码登录、网页投稿协议与 B 站开放平台 OAuth 是不同机制。本方案首版采用网页登录态和网页投稿协议，不把 Cookie 当成 OAuth Token，也不把 OAuth 授权当成网页投稿登录态。

本方案是待确认设计，不代表已验证接口长期可用，也不授权开始代码实施。

## 2. 范围

### 2.1 本轮包含

- 将现有 Fake 专用执行契约演进为平台无关 Adapter Execution Contract，保留 Fake Adapter 兼容。
- Bilibili 账号添加、网页二维码获取/展示/轮询、扫码结果校验和账号身份绑定。
- Cookie / Token 安全持久化、账号验证、可用时的凭据刷新、失效和重新登录流程。
- B 站网页投稿所需 HTTP Client、请求封装、响应解析和错误分类。
- UPOS 预上传、分片读取和上传、完成确认、视频资源信息取得。
- 单 P 投稿、多 P 投稿和封面上传。
- Work / UploadUnit / Video 元数据到 B 站投稿模型的映射。
- 投稿可见范围配置。
- 单账号最多 3 个视频上传执行并发；单视频分片顺序上传。
- 上传 checkpoint、恢复核验、投稿 UNCERTAIN 处置。
- AID/BVID 回执及稿件审核状态查询（以接口可用性为准）。
- HTTP 响应、字段、会话过期、重复投稿、进程崩溃测试和专用测试账号验收。

### 2.2 本轮明确不做

- 不接入 B 站开放平台 OAuth 后端；未来可独立实现。
- 不实现字幕上传、延时发布和高级投稿选项。
- 不实现密码登录、短信登录或导入浏览器全部 Cookie。
- 不实现绕过验证码、风控、设备验证、账号安全校验或平台限制。
- 不将 biliup 作为运行时依赖、子进程后端或强制安装组件。
- 不复制 biliup 的 App Key、App Secret、签名密钥或设备身份参数。
- 不将网页投稿接口宣称为稳定、正式或长期受支持的 API。
- 投稿超时后不盲目重复创建稿件。
- 不把“稿件已受理”描述成“审核已通过”。

## 3. biliup 源码研究与来源边界

本方案研究 biliup/biliup 当前仓库的相关实现，重点文件：

- crates/stream-gears/src/login.rs
- crates/biliup/src/uploader/credential.rs
- crates/biliup/src/uploader/bilibili.rs
- crates/biliup/src/uploader/line/upos.rs
- crates/stream-gears/src/uploader.rs
- crates/biliup/src/client.rs

可借鉴的设计：
1. 登录凭据不是单一 Cookie 字符串，模型包含 Cookie、Token 信息、用户 MID 和平台信息。
2. 扫码包含二维码获取、轮询、结果解析、Cookie 装载等步骤。
3. 凭据刷新、原子写入、并发访问锁和敏感信息脱敏值得借鉴。
4. UPOS 流程拆分为预上传、分片上传、完成上传；可按偏移量读取原文件。
5. 投稿模型将整稿字段与有序 Video 列表分离，表达多 P。
6. 封面上传与投稿提交是不同阶段。

### 3.1 重要发现与限制

biliup 当前源码中可见 BiliTV / Android AppKeyStore、签名逻辑及特定平台身份参数。源码存在不代表这些参数是我们可合法使用的通用公开凭据。

因此只研究请求流程、数据模型、错误处理和测试方法；不复制或复用上游硬编码 App Secret、第三方应用凭据、设备身份或签名参数；不通过伪装官方客户端或绕过平台安全机制实现登录。

首先确认我们可合法、稳定使用的扫码登录流程及其接口条件。如果可用扫码流程依赖无法合法取得或使用的应用凭据，登录实现必须暂停并重新评估授权方案，不能偷用上游密钥绕过。

### 3.2 许可证

本次检查到 biliup 仓库根目录 LICENSE 为 MIT License。自主实现不复制代码时仍记录研究来源；若后续提出移植代码，必须逐文件核对版权头、依赖和第三方来源，保留 MIT 版权与许可声明并重新审查。用户选择自主实现，因此本方案不计划代码移植。

## 4. 模块结构

建议新增：

~~~text
src/upload_platform/adapters/bilibili/
├── adapter.py
├── models.py
├── account/
│   ├── service.py
│   ├── identity.py
│   └── credential.py
├── auth/
│   ├── qr_session.py
│   ├── qr_client.py
│   └── polling.py
├── client/
│   ├── web_client.py
│   ├── request.py
│   ├── response.py
│   └── errors.py
├── upload/
│   ├── preupload.py
│   ├── upos.py
│   ├── chunk_reader.py
│   ├── chunk_uploader.py
│   └── completion.py
├── submission/
│   ├── mapping.py
│   ├── cover.py
│   ├── single.py
│   ├── multipart.py
│   └── verification.py
└── recovery/
    ├── checkpoint.py
    └── reconciliation.py
~~~

依赖规则：
- Adapter 通过 Core Adapter Protocol 接收执行上下文、文件引用、凭据引用和 fencing generation。
- Adapter 不直接访问 Core Repository 或写 Core 数据库。
- 账号与登录管理通过 Application Service / Credential Port 操作。
- HTTP Client 不包含投稿业务决策；Submission Service 负责投稿模型映射。
- UPOS Client 与网页投稿 Client 分离。
- Bilibili 代码不得反向依赖 Fake Adapter。

## 5. 通用 Adapter Protocol 演进

当前 Core 执行协议仍包含 Fake 专用类型，例如 FakeOptions 和 fake=true。真实平台接入前必须先泛化。

### 5.1 Request

至少携带 protocol_version、task_id、attempt_id、execution_id、generation、platform、account_id、按平台 schema 校验的 options、有序 Asset references、opaque credential reference、checkpoint reference、result/evidence reference 和 cancel signal。

Cookie / Token 明文不得进入请求 JSON、命令行参数、事件或普通日志。

### 5.2 Event

建议统一事件：
- phase_changed
- progress
- upload_session_created
- part_started / part_completed
- video_completed
- cover_completed
- submission_started / submission_accepted
- verification_updated
- warning
- cancel_acknowledged

事件绑定 task_id、attempt_id、execution_id、generation 和单调 sequence。重复事件可幂等处理；相同 sequence 内容冲突必须拒绝。

### 5.3 Result

结果类型：
- success：获得可验证远端稿件标识；
- retryable_failure：有证据证明可安全重试；
- permanent_failure：明确不可重试；
- uncertain：远端副作用可能已发生，不能自动重发；
- cancelled：确认安全取消。

Fake Adapter 迁移到通用协议；Fake 专属场景只放在 Fake options/schema 中。

## 6. 账号与扫码登录

### 6.1 账号模型

支持多个 B 站账号。每个 Account 至少保存内部 account_id、platform=bilibili、B 站 MID、显示名称、账号状态、credential reference、credential version、最近验证时间和 concurrency_limit（默认 3）。

同一平台下 MID 应有唯一约束，避免重复添加。

### 6.2 登录会话

建议状态：CREATED、QR_READY、WAITING_SCAN、SCANNED_WAITING_CONFIRM、AUTHENTICATED、EXPIRED、CANCELLED、FAILED。

流程：
1. 客户端请求创建登录会话。
2. 服务端创建短时 session 并请求二维码。
3. 客户端展示二维码和过期倒计时。
4. 登录执行器按已确认可用的流程轮询状态。
5. 用户扫码/确认后解析登录结果。
6. 验证 Cookie 和账号身份，确认 MID。
7. Credential Manager 安全保存凭据。
8. 创建或更新 Account，返回 account_id、昵称和状态。
9. 登录会话终止，临时二维码数据失效。

支持二维码过期、用户取消、扫码未确认、重复轮询、网络超时、账号已存在、身份不一致等情况。

### 6.3 网页登录与 OAuth 区分

网页扫码登录产生网页/客户端登录态及 Cookie/Token。开放平台 OAuth 产生开发者应用授权 Token 和 Scope。两者流程、凭据、权限范围不同，不允许互相替代，也不在首版混用。

### 6.4 登录可行性门槛

biliup 源码展示了 TV QR auth_code/poll 流程，也包含 Web Cookie 辅助确认二维码的流程。源码存在不能证明我们拥有可直接使用的应用凭据或平台授权。

B2 开发前必须确认可用二维码接口、所需应用身份、平台使用条件、登录结果是否可持续用于网页投稿、刷新机制是否可用。若无法在不复用未获授权密钥、不伪装或绕过安全校验的前提下完成，暂停该登录实现并向用户提出替代方案。

## 7. 凭据安全与生命周期

- 凭据存入 Credential Manager / Secret Store，数据库只保存 opaque reference 和版本/状态。
- Windows 首选评估当前用户 DPAPI 或系统凭据保护能力；具体实现需在 B2 前确认。
- 凭据不进入 Git、日志、异常详情、事件、CLI 输出或崩溃转储。
- 更新采用原子替换或 Secret Store 版本切换。
- 多任务共享账号时，Token refresh 必须串行化，避免 refresh token 竞态。
- 凭据刷新成功后递增 credential_version；旧版本不能覆盖新版本。
- 账号验证区分有效、过期可刷新、需重新登录、权限不足和平台异常。
- 删除账号撤销本地凭据引用并处理运行中任务；除非接口明确支持，不声称能撤销 B 站服务器端会话。
- Cookie/Token 不作为子进程命令行参数；通过受限 IPC 或短期受控凭据句柄提供。

## 8. 投稿模型与元数据

### 8.1 单 P

一个 independent UploadUnit 的一个视频，对一个 B 站 target 生成一个 Task 和一个投稿。

### 8.2 多 P

一个 multipart UploadUnit 的有序视频集合，对一个 B 站 target 生成一个 Task 和一个稿件。每个视频形成一个 P，按 manifest 顺序上传和组装。

### 8.3 字段映射

投稿级字段：title、description、tags、partition/tid、copyright、source、cover、dynamic、visibility 等。

分 P 级字段：每 P 标题、文件资源、顺序。

沿用 Work → UploadUnit → Video 元数据继承规则。multipart 的总标题/简介/标签/封面与每 P 标题分开；independent 每个视频形成独立投稿快照。不支持的字段必须在预览/提交前报告，不得静默丢弃。

### 8.4 可见范围

每个作品/上传单元可配置可见范围。manifest 未配置时采用用户确认的全局默认值；默认值未确认前，不允许静默假定公开。平台不支持指定可见性时，在创建 Task 前拒绝或要求用户确认降级。

## 9. 上传流程

### 9.1 Basic Preflight

只做必要基本检查：
- Asset 引用存在且可读取；
- 文件为普通文件；
- 文件大小可取得且大于零；
- 扩展名/媒体类型可识别；
- 视频数量和投稿字段满足本地 schema 基本要求；
- 当前账号状态可用。

不做完整转码、全文件内容扫描或强制 SHA-256。平台侧拒绝映射为清晰错误。

### 9.2 UPOS

每个视频独立执行：
1. 获取文件大小和必要文件属性。
2. 请求预上传信息，解析 endpoint、auth、chunk_size、upload_id、业务标识及协议参数。
3. 使用文件句柄按偏移量读取，不创建整文件副本。
4. 首版 chunk_concurrency=1，按协议顺序上传分片。
5. 记录分片序号、长度、状态和协议要求的 ETag/确认信息。
6. 所有分片完成后调用完成/合片接口。
7. 验证响应并取得投稿使用的视频资源信息。

分片大小按预上传响应/线路协议确定，不把固定值写死为所有线路通用规则。请求设置连接和整体超时，错误按 HTTP、业务码、认证、限流和网络分类。

### 9.3 账号并发

- Account concurrency_limit 默认 3，表示同一账号同时运行的视频上传执行单元数量。
- 单视频分片首版顺序执行。
- multipart Task 内首版按 P 顺序逐个上传；全部完成后一次性投稿。
- 同账号不同 Task 的并发由 Core Account Slot Lease 限制，Adapter 不得另建绕过 Core 的并发池。
- 最终投稿操作单独做账号级互斥/限流，避免并发最终提交冲突。

## 10. Checkpoint 与恢复

Bilibili 专属执行数据通过 Adapter 自己的存储端口保存，不直接写 Core DB。至少包含：
- session_id / upload_id；
- endpoint 和必要上传会话参数（敏感字段加密或 opaque reference）；
- asset_id 和路径引用；
- file_size、chunk_size、total_chunks；
- 已确认分片序号及协议回执；
- 当前视频序号和上传阶段；
- 视频完成后的远端资源信息；
- 投稿提交 intent 和提交结果。

恢复规则：
1. Core 先确认旧 Adapter Host 退出或已隔离。
2. 验证 task/attempt/generation 身份。
3. 检查凭据有效。
4. 检查源 Asset 路径可读，文件大小与 checkpoint 一致。
5. 通过协议核验远端上传会话/分片状态；不能只信任本地 checkpoint。
6. 远端状态与本地证据一致时才续传。
7. UPOS 会话失效时按协议判断能否重新申请会话；重新上传视频不等于重新投稿。
8. 视频资源已完成时不得无故重复上传。
9. 投稿请求发送前持久化 submission intent。
10. 投稿响应丢失时进入 UNCERTAIN，先查询远端稿件；证据不足则人工处理，不自动再次提交。

调用方必须在上传/恢复期间保持源文件可用且内容稳定。平台不复制、不扫描内容、不保证文件未被替换；文件大小或必要属性与 checkpoint 不一致时，禁止沿用旧分片进度。

## 11. 投稿结果与状态

任务成功定义：B 站受理投稿并返回可验证 AID/BVID，且本地成功保存 Receipt。

Receipt 至少包含 platform、account_id/MID、AID、BVID、稿件标题、提交时间、响应证据摘要、核验来源和当前审核状态（若可查询）。

Task SUCCEEDED 表示投稿已受理；审核状态独立表示 PENDING / APPROVED / REJECTED / UNKNOWN。审核状态更新不反向改写 Task 成功事实；审核失败产生独立事件，不自动重新投稿。

## 12. 重复投稿保护

- 默认重复提交同一 Work 展开项 + Bilibili Account 时，不创建第二个投稿 Task。
- 用户必须显式执行重新上传/重新投稿。
- 显式重新投稿前检查原 Task；UNCERTAIN、RECOVERY_PENDING 或投稿结果未明时禁止。
- HTTP 重试、Worker 恢复和用户显式重新投稿是不同语义。
- 投稿提交前保存稳定业务关联与 submission intent；请求超时不能直接当作失败。
- 远端稿件列表无法可靠匹配时保持 UNCERTAIN，要求人工核验。

## 13. 错误分类

至少区分 AUTH_EXPIRED、AUTH_REQUIRED、PERMISSION_DENIED、RATE_LIMITED、UPLOAD_SESSION_EXPIRED、CHUNK_RETRYABLE、FILE_REJECTED、SUBMISSION_REJECTED、SUBMISSION_UNCERTAIN、REMOTE_TEMPORARY_ERROR、ADAPTER_PROTOCOL_ERROR。

仅有证据证明没有产生不可逆远端副作用的错误才自动重试。认证失效进入账号修复流程；限流使用退避；投稿结果不明进入 UNCERTAIN。

## 14. 实施 Batch

### B1 — 通用 Adapter Protocol
- 移除通用协议对 FakeOptions / fake=true 的硬编码。
- 定义通用 Request、Event、Result、凭据和 checkpoint 引用。
- Fake Adapter 迁移并保持现有测试。
- 增加平台 options schema registry。

验收：Fake 回归通过；通用协议无平台专属字段；不破坏 Recovery/Lease/取消。

### B2 — Bilibili QR Account
- 先确认扫码接口可行性与使用条件。
- 实现 QR Session、二维码展示、轮询、过期和取消。
- 实现登录结果解析、MID 校验。
- 实现 Credential Manager 安全保存、验证、刷新或重新登录。
- 实现账号列表、验证和删除接口/CLI。

验收：专用测试账号可扫码登录；重启后凭据仍有效或能正确提示重新登录；多账号隔离；日志和异常不泄漏凭据。

### B3 — 单视频 UPOS 上传
- 实现 HTTP Client 和响应模型。
- 实现预上传、顺序分片、分片回执、完成上传。
- 用 Fake HTTP Server / 脱敏固定响应夹具做契约测试。
- 用专用账号上传小型测试视频并验证远端资源。

验收：真实视频上传完成并取得投稿前资源信息；文件未被复制；进度事件正确。

### B4 — 上传恢复
- 保存上传会话和分片 checkpoint。
- 注入分片前、分片中、分片后崩溃。
- 验证远端分片状态后续传。
- 会话失效时按规则重新申请或转人工处理。

验收：不重复确认已完成分片；不混用不同 Asset/账号/会话；无法确认时不盲目续传。

### B5 — 单 P 投稿
- 实现投稿字段映射、可见范围、封面。
- 实现 submission intent、投稿提交和结果核验。
- 取得 AID/BVID 并写入 Receipt。

验收：真实测试账号创建单 P 投稿；重复请求不重复创建；响应丢失进入 UNCERTAIN 并核验。

### B6 — 多 P 投稿
- 按 manifest 顺序上传多个视频。
- 每个 P 映射正确标题。
- 所有视频资源准备好后执行一次整稿提交。
- 失败时保留已完成视频资源，不重新投稿。

验收：真实测试账号创建多 P 投稿，P 顺序和标题正确。

### B7 — 真实验收与运维
- 多账号隔离与账号并发 3；
- Cookie 过期、刷新和重新扫码；
- 限流、网络断开、服务重启；
- 重复投稿和 UNCERTAIN 人工处理；
- 凭据保护、备份和删除说明；
- 网页接口兼容性与变更记录。

验收：完整测试记录、已知限制、运行说明和接口变更处理流程。

## 15. 验收总标准

1. 多个 B 站账号可独立添加、验证和停用。
2. 网页扫码成功后凭据安全持久保存；重启后不要求无条件重新登录。
3. 一个作品可创建单 P 或多 P 投稿。
4. Work / Unit / Video 元数据映射准确。
5. 同账号最多 3 个视频上传执行单元；单视频分片首版串行。
6. 上传按原文件引用流式读取，不默认复制或计算 SHA-256。
7. 分片进度、阶段、失败和恢复可查询。
8. Worker 崩溃后先确认旧执行停止，再恢复或进入 UNCERTAIN。
9. 投稿成功必须取得并保存 AID/BVID。
10. 审核状态独立于 Task SUCCEEDED。
11. 默认阻止重复投稿；显式重新投稿必须经过状态检查。
12. Cookie、Token、UPOS Auth 等敏感信息不进入日志、事件、命令行或 Git。
13. Fake Adapter 在通用协议改造后通过回归。
14. 不把网页投稿接口宣称为稳定官方 API，不实现安全机制绕过。

## 16. 剩余未决问题

### U1 — 网页扫码登录的具体流程
biliup 源码中可见 TV QR auth_code/poll 流程，也存在 Web Cookie 辅助确认二维码的流程。首版采用哪种具体流程，需要确认可用性、所需应用身份和平台允许条件。不得复用上游硬编码 App Secret 或伪装客户端身份。若没有合规可用的直接扫码路径，是否接受改为用户手动导入 Cookie？建议先保留此决策门槛。

### U2 — Windows 凭据保护
推荐当前 Windows 用户 DPAPI。是否接受凭据仅对创建它的 Windows 用户可解密？未来若使用服务账号/多用户运行，再设计服务级 Secret Store。

### U3 — Cookie / Token 刷新
建议仅采用已确认可用且符合平台规则的刷新流程；刷新失败后要求重新扫码。是否接受？

### U4 — 多 P 内视频顺序
建议一个 multipart Task 内按 P 顺序逐个上传，全部完成后一次投稿；账号 3 并发用于不同 Task 的视频上传。是否接受？

### U5 — 默认可见范围
未配置时，是拒绝提交并要求明确选择，还是使用公开作为默认？建议默认不自动公开，要求明确配置或经用户确认的全局默认值。

### U6 — 默认分区
是否支持每个 B 站账号设置默认分区 tid，作品可覆盖？建议支持账号默认值 + Work/Unit 覆盖；无有效分区时阻止投稿。

### U7 — 默认版权类型
原创/转载是否每个作品显式指定，还是允许账号默认值？建议账号默认值 + Work/Unit 覆盖；转载时来源字段按平台要求校验。

### U8 — 封面策略
建议 multipart 使用一个整稿封面；independent 每个视频可单独覆盖封面，未覆盖时继承 UploadUnit/Work 封面。是否符合预期？

### U9 — 账号并发范围
确认 concurrency_limit=3 是同一账号同时运行不同视频上传执行单元数，不等于同一账号允许同时发起 3 次最终投稿。最终投稿建议账号级串行或低频限流。

### U10 — 上传线路
首版是否只使用预上传响应指定的 UPOS endpoint，不提供线路手动选择/测速？建议首版如此。

### U11 — 显式重新投稿
用户显式重新投稿时，是否允许保留原稿件并创建新稿件，还是需先由用户确认旧稿件处理方式？建议首版只创建新 Task，不自动删除或修改远端稿件。

### U12 — 测试账号与素材
需要用户提供可用于真实测试的专用 B 站账号及非敏感测试素材；不要在聊天、Git 或日志中提供 Cookie。登录应由用户在本机交互完成。

### U13 — 高级投稿字段
首版是否只支持标题、简介、分区、标签、版权、来源、封面、可见范围和分 P 标题？动态、充电、杜比、Hi-Res、精选评论等建议后续按需求增加。

## 17. 文档与实施授权

本方案确认后，按项目文档规范同步已确认需求和设计，并标记为待实现。Bilibili Adapter 代码、数据库扩展、真实账号操作均须等待用户明确实施指令。

当前状态：待确认。尚未取得本方案范围的实施指令。

## 18. 结果记录

- 已实现：
- 测试结果：
- 真实账号验证：
- 已知接口限制：
- 未解决问题：
- 架构偏离 / ADR：
- 完成状态：
