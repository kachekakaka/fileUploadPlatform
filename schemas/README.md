# 当前公开契约

由实际模型和 FastAPI 路由生成，不复制参考包样例。更新入口为 [tools/export_contracts.py](../tools/export_contracts.py)，运行见[测试入口](../tests/README.md#契约再生成)。

- [openapi.json](openapi.json) 由 REST 路由生成。统一投稿及原单查回携带 UUIDv4 `Idempotency-Key`，也可携带健康接口提供的 `X-Upload-Platform-Deployment` 校验目标数据库。使用方法见 [Dashboard 接入说明](../docs/operations/dashboard.md#程序和网页投稿)。CLI 客户端标识和 Host 规则见 [Core 运行说明](../docs/operations/core-mvp.md)。
- [manifest-v1.json](manifest-v1.json)：字段、模式与引用类型。跨字段唯一性、引用存在性、最终元数据及文件边界由共享 Application 校验；未知字段拒绝。
- [fake-options-v1.json](fake-options-v1.json)：Fake Adapter 的模拟选项；[bilibili-options-v1.json](bilibili-options-v1.json) 为明确分区、稿件类型、转载来源和发布选择。模式还必须通过部署能力验证，schema 枚举不代表真实平台能力已验收。
- [bilibili-event-v1.json](bilibili-event-v1.json)、[bilibili-result-v1.json](bilibili-result-v1.json)：独立 Bilibili 协议，使用 MID、构建 ID、执行身份及阶段证据；不复用 Fake GO 或模拟回执。
- [login-request-v1.json](login-request-v1.json)、[login-session-v1.json](login-session-v1.json)、[account-summary-v1.json](account-summary-v1.json)：Core 托管扫码的请求、安全视图及账号摘要；会话时间为 UTC RFC3339，列表为至多 100 项的 `{items}`，不沿用 Task 分页形状。
- [login-event-v1.json](login-event-v1.json)：引擎机器扫码交互；PNG 只经敏感内存通道传递，禁止进入普通日志或持久事件。GUI 桥接标识仅在[运行说明](../docs/operations/bilibili.md)列出的入口生效，其他入口仍使用原客户端规则。
- [host-request-v1.json](host-request-v1.json)、[host-command-v1.json](host-command-v1.json)、[host-event-v1.json](host-event-v1.json)、[host-result-v1.json](host-result-v1.json)：版本化 NDJSON，任务、Attempt、随机执行标识与 generation 必须匹配。Result 含连续事件链及模拟回执，恢复核验同一证据，不凭文件存在或退出码认定成功。

REST 错误为 `{code,message,request_id,details}`。CLI 的 operation_result 是原操作确认，重放后的 current_tasks 是重新查询的状态；REST 本身返回业务对象，由 CLI 包装请求标识。时间请求为带偏移 RFC3339，查询中的数据库时间为 UTC Unix 秒。列表 `{items,next_cursor}` 默认 50、最大 200；事件 `{items,next_sequence}`，SSE Last-Event-ID 为同一持久序号。JSON Schema 无法表达的状态、文件及事务不变量由行为测试验证。
