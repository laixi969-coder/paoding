# API 与调用合同

默认服务地址 `http://127.0.0.1:8010`，完整字段与类型见 `/docs`、`/openapi.json`。设置 `PAODING_API_TOKEN` 后，所有常规 `/api/v1` 路由必须携带 `Authorization: Bearer <token>`；Webhook 使用独立的厂商校验凭据。

| 方法与路径 | 用途 |
|---|---|
| POST `/api/v1/ingest` | URL 入队；主页转为账号巡检任务；返回 202 |
| GET `/api/v1/jobs` | 按 status 过滤，limit/offset 分页 |
| GET `/api/v1/jobs/{id}` | 状态、次数、结果、错误码 |
| POST `/api/v1/jobs/{id}/retry` | 人工重试失败任务 |
| GET `/api/v1/nodes` | 知识卡片列表，可按平台过滤 |
| GET `/api/v1/nodes/{id}` | 完整解剖、来源、模型信息、Vault 状态 |
| GET `/api/v1/atoms` | 元件类型/标签/目标平台过滤；q 可做语义/关键词检索 |
| POST `/api/v1/atoms/{id}/feedback` | 幂等采纳反馈 |
| GET `/api/v1/playbooks` | domain、target_platform 查询元规则 |
| GET `/api/v1/trends` | days、platforms 查询爆款样本与标签占比变化 |
| POST `/api/v1/evolution/run` | 入队当前周趋势任务；周期幂等 |
| GET `/api/v1/creators` | 账号、游标巡检时间、画像进度 |
| GET `/api/v1/creators/{name-or-id}/persona` | 数字分身和代表作 |
| POST `/api/v1/creators/{id}/pause?paused=true` | 暂停账号；false 恢复 |
| GET `/api/v1/models` | 已发现模型和已测健康状态 |
| POST `/api/v1/models/sync` | 同步可见模型，不自动改路由 |
| POST `/api/v1/models/test` | 真实小额生成探针 |
| GET `/api/v1/stats` | 库容量和任务状态统计 |

PRD 的 GET `/api/v1/models/sync` 作为 deprecated 别名保留；推荐有写入语义的 POST。

## 一次单条内容处理

```json
{
  "url": "https://example.com/article",
  "instruction": "聚焦可复用的营销执行步骤",
  "priority": "normal"
}
```

```json
{"kind":"item","job_id":"UUID","status":"queued"}
```

`priority` 仅接受 low/normal/high。接口不接受本地路径或任意文件读取参数；本地文件只能由同机 CLI/Drop 导入。链接识别不等于成功下载，访问权限或采集器问题在任务中返回。

任务成功后：

```json
{
  "id":"UUID",
  "kind":"ingest",
  "status":"succeeded",
  "attempts":1,
  "result":{"node_id":"NODE_UUID","export_job_id":"EXPORT_JOB_UUID"},
  "error":null
}
```

若没有模型配置：`status=failed`、`error.code=configuration_required`。配置完成后可对原 job 重试，不必重复提交。若是网络/限流则按策略进入 retrying。

## 检索和反馈

```text
GET /api/v1/atoms?atom_type=visual&tags=极简&target_platform=xiaohongshu&limit=10
GET /api/v1/atoms?q=先呈现问题&limit=10
```

多个 tags 参数为 AND 匹配。`target_platform` 支持 douyin、wechat_video、youtube、short_video、xiaohongshu、wechat。响应有 `transfer_advice` 和 `retrieval_mode`；不会将关键词搜索标成向量搜索。

```json
{"is_adopted":true,"event_id":"my-agent:run-123:step-4"}
```

相同 event_id 的网络重试只处理一次；同一个 event_id 改写内容返回 409。采纳 +1，未采纳 -0.25；反馈同时刷新最近调用时间。

## 错误与边界

业务错误统一形状：

```json
{"error":{"code":"idempotency_conflict","message":"Idempotency key was already used with different input"}}
```

参数 Schema 错误使用 FastAPI 标准 422 `detail`。401 表示鉴权失败，404 表示对象不存在，409 表示状态/幂等冲突，503 表示配置或暂时不可用。HTTP 202 只表示任务已被数据库接受，不保证上游平台可访问。

响应可能包含用户本机 Vault 路径、来源内容、已签名的原链接，因此远程访问必须保留鉴权。原始 URL 签名参数用于采集，不为统一去重而擅自删除。

## v0.2 新增接口

- GET `/capture`：同源收录页，书签可一键提交 URL 与标题；配置令牌的主机先在页面填写。
- GET `/api/v1/taxonomy`：活动分类树、别名与来源数量。
- POST `/api/v1/taxonomy/run`：手动入队分类进化任务。
- GET `/api/v1/evolution/logs`：查询画像、元规则、趋势与分类变更记录。
- GET `/api/v1/atoms?category=UUID`：按分类检索，旧分类 UUID 自动重定向。
- `/ingest` 增加可选 title；标签检索识别已验证的同义分类别名。
- 三种 Bot 回调统一位于 `/api/v1/webhooks/{telegram,feishu,wecom}`；企微额外支持 GET 加密握手。

Bot 回调仅做验证和持久化入队，附件下载和模型分析在 Worker 中执行。飞书和企微的完整配置见 [配置总清单](configuration.md)。
