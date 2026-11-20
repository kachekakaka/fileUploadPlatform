# 文件上传平台 Windows x64 联调包

包内含后端、Python 运行环境、已构建的 Dashboard 和 `schemas/openapi.json`。运行需要 Windows x64 与 PowerShell 7。

在解压目录下打开 PowerShell 7。为联调选择新的绝对数据目录，并设置获准的配置和素材目录。

```powershell
.\Start.ps1 server -DataDirectory 'D:\UploadPlatformData\integration' -ManifestRoots 'D:\Recordings\sessions' -AssetRoots 'D:\Recordings'
```

在另一个 PowerShell 7 窗口中，从同一个解压目录启动 Worker。两个进程必须使用同一个数据目录。

```powershell
.\Start.ps1 worker -DataDirectory 'D:\UploadPlatformData\integration'
```

打开 `http://127.0.0.1:8765/`。MuMu 使用同一个地址连接 API，通过 `GET /api/v1/system/health` 获取数据库对应的 `deployment_id`。预览、提交、查回等 API 可带 `X-Upload-Platform-Deployment` 校验部署。两窗口可用 Ctrl+C 退出。

联调包使用 SQLite 版本 4。旧运行目录保留在原处。包内没有测试数据、真实账号凭据或 Bilibili 执行器。需要启用 Bilibili 时，在启动两个进程前设置同一个 `UPLOAD_PLATFORM_BILIBILI_CONFIG` 本机配置路径。基础链路可以用内置 Fake 执行。

浏览器作品页把本次投稿的原请求与幂等键保存在本机浏览器中。回包丢失后，返回作品页即可选择「查回原请求」，确需重试时选择「重试原请求」。
