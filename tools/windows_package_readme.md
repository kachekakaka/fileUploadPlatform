# 文件上传平台 Windows x64

包内含后端、Python 运行环境、已构建的 Dashboard 和 `schemas/openapi.json`。

## MuMu 便携版

如果包内有 `启动上传平台.cmd` 和 `release.json`，请将 ZIP 完整解压到新文件夹，再双击 `启动上传平台.cmd`。便携入口自带 Python 和正式 B 站引擎，直接用 Windows x64 桌面启动。

双击后自动启动。新装使用 `%LOCALAPPDATA%\fileUploadPlatform\MuMu\data`，设置文件保存在其上一级。替换旧包前先在旧窗口退出上传平台，新包会沿用已保存的数据目录、账号和部署编号。看到「MuMu 可以连接」后，在同机 MuMu 使用 `http://127.0.0.1:8765`。首次投稿从 MuMu 的「自动投稿 → 管理投稿账号」扫码。

MuMu 总入口通过 `--data-dir` 指定上传数据目录，已有绑定时同时传 `--expected-deployment`。从整合包换包时使用 MuMu 的工作目录。内嵌面板使用工作区保存的部署编号，刷新后沿用该编号。

关闭状态窗口，API 和 Worker 继续运行。要停止服务，在已核对当前部署的窗口点击「退出上传平台」，等待任务收尾。出现端口冲突或原数据库问题时，按窗口显示的路径核对。后台日志位于数据目录的 `logs`。

## 分角色联调和排查

需要手动分角色运行时，以下 `Start.ps1` 用法需要 PowerShell 7。

在解压目录下打开 PowerShell 7。为联调选择新的绝对数据目录。

```powershell
.\Start.ps1 server -DataDirectory 'D:\UploadPlatformData\integration'
```

在另一个 PowerShell 7 窗口中，从同一个解压目录启动 Worker。两个进程必须使用同一个数据目录。

```powershell
.\Start.ps1 worker -DataDirectory 'D:\UploadPlatformData\integration'
```

打开 `http://127.0.0.1:8765/`。MuMu 使用同一个地址连接 API，通过 `GET /api/v1/system/health` 获取数据库对应的 `deployment_id`。预览、提交、查回等 API 可带 `X-Upload-Platform-Deployment` 校验部署。两窗口可用 Ctrl+C 退出。

包使用上传平台数据库结构版本 4。为联调选择新的数据目录，旧运行目录保留在原处。分角色启用 Bilibili 时，两个进程使用同一个 `UPLOAD_PLATFORM_BILIBILI_CONFIG` 本机配置路径。基础链路可以用内置 Fake 执行。

预览和自动投稿由调用方提供本次 `manifest_path` 和 `asset_root`。清单可与素材分开保存，素材路径相对于该根目录。浏览器作品页把本次投稿的原请求与幂等键保存在本机浏览器中。回包丢失后，返回作品页即可选择「查回原请求」，确需重试时选择「重试原请求」。
