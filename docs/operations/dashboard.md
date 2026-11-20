# 本机 Dashboard 与统一提交

Dashboard 和 API 共用 `http://127.0.0.1:8765`。Worker 作为另一个进程运行，使用同一运行目录。直接打开 Dashboard 即可进入页面；管理 Bilibili 投稿账号时在「账号」页发起扫码。Bilibili 引擎配置见[操作说明](bilibili.md)。

## Windows 联调包

Windows x64 ZIP 包含后端、Python 和已构建的 Dashboard。解压后使用包内的 `README.md` 和 `Start.ps1`，分别启动 API 和 Worker。两者使用同一个新的仓外数据目录。无需安装 Python 或 Node.js。

从源码制作同样的包，在仓库根目录使用 PowerShell 7 执行 `pwsh -NoProfile -File tools/build_windows_zip.ps1 -OutputDirectory <仓外输出目录>`。验证命令见 [测试运行入口](../../tests/README.md#windows-联调包验收)。

## 从源码构建与启动

使用 PowerShell 7，在仓库根目录执行。Python 环境见 [Core 安装说明](core-mvp.md#安装与启动)。前端构建验证使用 Node.js 24.18.0。

```powershell
npm --prefix web ci
npm --prefix web run build

$env:UPLOAD_PLATFORM_API_URL = 'http://127.0.0.1:8765'
$env:UPLOAD_PLATFORM_DATA_DIR = '<新的仓外运行目录>'
$env:UPLOAD_PLATFORM_MANIFEST_ROOTS = '<允许读取配置的本机目录>'
$env:UPLOAD_PLATFORM_ASSET_ROOTS = '<允许引用素材的本机目录>'
python -m upload_platform server
```

另开 PowerShell，设置同一个 `UPLOAD_PLATFORM_DATA_DIR`。配置 Bilibili 时，两个进程使用相同的引擎配置。启动 Worker。

```powershell
python -m upload_platform worker
```

`UPLOAD_PLATFORM_DATA_DIR` 使用空目录初始化 SQLite 版本 4。旧库保留原处，服务发现旧结构时会拒绝启动。允许目录必须是已存在的本机绝对路径，可用分号列出多个。两项均为空时，统一提交和预览没有可读目录。可用 `UPLOAD_PLATFORM_DASHBOARD_DIR` 指向构建出的 `web/dist`。

## 面板操作

- 「概览」显示数据库、平台及 Worker 心跳，以及排队、执行、重试和待核验的任务。
- 「账号」管理 Bilibili 投稿账号。扫码进行中刷新页面后，会恢复显示活动会话。
- 「作品」可录入允许的目录并预览 `upload.yaml`。配置与媒体分开时，分别输入配置路径与素材根。「仅登记作品」只保存作品，「提交上传任务」按预览配置受理投稿。等待回执时刷新或离开页面，可回到作品页选择「查回原请求」或「重试原请求」。这依赖同一浏览器配置和可用的本地存储。原生目录选择器需要从当前交互式 Windows 会话启动 API。服务无法显示选择器时可手动输入路径。
- 「任务」支持服务端筛选、事件时间线、回执、取消、重提、远端查询和人工核验。显示的操作随任务状态和平台变化。结果待核验时需要先确认远端结果。
- 「定时规则」支持每日、每周和启停。最近运行摘要报告作品发现、任务创建、跳过和失败；投稿是否成功见任务详情。
- 「设置」可查看配置目录、Worker 心跳及引擎诊断。

MuMu 的父页面来源使用 `http://127.0.0.1:8000`，嵌入路径为 `/embed/accounts`、`/embed/works`、`/embed/tasks` 及作品、任务详情。页面首次打开和刷新都直接载入数据。消息格式见[嵌入协议](../../archive/方案/20261120-fileUploadPlatform-Dashboard与统一提交实施方案.md#12-iframe-页面与消息协议)。

## 程序和网页投稿

请求字段和响应以 [OpenAPI](../../schemas/openapi.json) 为准。配置文件名为 `upload.yaml`，使用 UTF-8，大小最多 1 MiB。自动投稿按原始 YAML 字节计算 `hashlib.blake2b(path.read_bytes(), digest_size=32).hexdigest()`，连同 `manifest_path`、`asset_root` 和 `expected_version` 发送。手动投稿先调用 `POST /api/v1/submissions/preview`，提交时传入返回的 `preview_id`。预览有效期为 10 分钟，重启 API 后可重新预览。

1. 为本次投稿生成随机 UUIDv4，持久保存键和请求正文。
2. 将正文通过 JSON 发送到 `POST /api/v1/submissions`，在头部传入 `Idempotency-Key`。
3. 响应丢失时，用同一个头部请求 `GET /api/v1/submissions/result`。找到即用返回的原 Work 和 Task 编号。只读查回返回 404 时，根据当前投稿意图决定是否重放原正文。需要重放时继续用同一键。
4. 首次受理前若版本冲突，重新预览或冻结新版本后，以新键提交。历史回执无需读取 YAML。

MuMu 先从 `GET /api/v1/system/health` 取得 `deployment_id`，随后在请求里携带 `X-Upload-Platform-Deployment`。同库重启沿用编号，新库生成新编号。该头可省略，传错则返回 409 `binding`，请求不会进入业务处理。已保存的投稿需要回到原部署查回。

公开投稿请求无需面板 Cookie。跨来源网页使用 `credentials: "omit"`，预检允许 `Content-Type`、`Idempotency-Key` 和 `X-Upload-Platform-Deployment`。Chrome 等浏览器从公网来源访问回环地址时还可能要求授予本机网络权限。发起网页需使用安全上下文，并按浏览器支持情况在 Fetch 指定 `targetAddressSpace: "loopback"`。浏览器拒绝访问时，请求可能尚未到达 API。[Chrome 本机网络访问说明](https://developer.chrome.com/docs/modern-web-guidance/guides/security/local-network-access)
