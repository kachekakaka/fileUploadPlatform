# 当前公开契约

由实际模型和 FastAPI 路由生成，不复制参考包样例。更新入口为 [tools/export_contracts.py](../tools/export_contracts.py)，运行见[测试入口](../tests/README.md#契约再生成)。

- [openapi.json](openapi.json)：REST 请求及实际路由。修改请求需 UUID Idempotency-Key（只读 preview 除外），Host/来源和 `X-Upload-Platform-Client: cli-v1` 按[运行说明](../docs/operations/core-mvp.md)限制。
- [manifest-v1.json](manifest-v1.json)：字段、模式与引用类型。跨字段唯一性、引用存在性、最终元数据及文件边界由共享 Application 校验；未知字段拒绝。
- [fake-options-v1.json](fake-options-v1.json)：当前唯一 Adapter 的模拟选项。
- [host-request-v1.json](host-request-v1.json)、[host-command-v1.json](host-command-v1.json)、[host-event-v1.json](host-event-v1.json)、[host-result-v1.json](host-result-v1.json)：版本化 NDJSON，任务、Attempt、随机执行标识与 generation 必须匹配。Result 含连续事件链及模拟回执，恢复核验同一证据，不凭文件存在或退出码认定成功。

REST 错误为 `{code,message,request_id,details}`。CLI 的 operation_result 是原操作确认，重放后的 current_tasks 是重新查询的状态；REST 本身返回业务对象，由 CLI 包装请求标识。时间请求为带偏移 RFC3339，查询中的数据库时间为 UTC Unix 秒。列表 `{items,next_cursor}` 默认 50、最大 200；事件 `{items,next_sequence}`，SSE Last-Event-ID 为同一持久序号。JSON Schema 无法表达的状态、文件及事务不变量由行为测试验证。
