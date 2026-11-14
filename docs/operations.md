# 运行与恢复

## 进程职责

- API：短请求入队、查询、Webhook 验证。
- Worker：长任务执行，可以启动多个实例共享同一个 PostgreSQL。
- Scheduler：每日/每周任务扇出；任务有周期幂等，可安全重启。
- Drop：宿主机目录监听和文件快照。
- MCP：由下游客户端按需启动的 stdio 进程。

`ecosystem.config.cjs` 使用项目内虚拟环境，自动区分 macOS/Linux 的 bin 与 Windows 的 Scripts。先完成 `uv sync`、`.env`、数据库迁移，再启动 PM2。不要把服务放进容器后仍假设它可以读取宿主机浏览器或 Drop 路径。

`docker compose up -d postgres` 只启动必需数据库；需要预留缓存服务时用 `docker compose --profile cache up -d`。Redis 当前不是队列权威存储。

## 部署配置

`.env.example` 提供开发环境 PostgreSQL 凭据，端口绑定本机。修改数据库密码时同时更新 `POSTGRES_PASSWORD` 与 `PAODING_DATABASE_URL`。已有 Docker volume 的密码不会因修改环境变量自动改变。

修改 `PAODING_HOST` 开放给 Tailscale 地址或全部接口前必须配置长随机 `PAODING_API_TOKEN`。应用校验默认阻止无 token 的非本机绑定。Tailscale 的设备授权与路由由用户管理。公共 Bot webhook 需要可信 HTTPS 入口。

可用 `scripts/install-startup.sh` / `scripts/install-startup.ps1` 注册当前用户登录后的 PM2 恢复；对应 uninstall 脚本可移除注册。

时间戳在数据库以 UTC 存储；调度时区来自 `PAODING_TIMEZONE`（默认 Asia/Shanghai，可改为 America/Los_Angeles）。恢复后补当前日/周周期，不无限追赶所有历史 cron。

## 故障恢复

1. `/health/ready` 503：确认数据库在线，执行 `uv run paoding init`；该命令做版本化迁移，不删除已有库。
2. 任务一直 queued：检查 Worker 是否在运行。
3. running 进程退出：等待租约到期（默认 300 秒），其他 Worker 可接管；旧 token 不能提交结果。
4. configuration_required：补齐模型/采集器配置，再 POST `/api/v1/jobs/{id}/retry`。
5. 分析成功但 Vault 未同步：查看 ingest result 中 `export_job_id`。修复磁盘/权限后重试导出任务。
6. 本地 Markdown 被删除：`uv run paoding vault rebuild`，然后让 Worker 消费导出队列。此操作会用数据库版本覆盖导出的卡片。
7. 模型探针 unavailable：确认路由 ID、Key 和 base URL；不要直接把尚未验证能力的新 ID 扔进 video 路由。

`worker --drain` 处理**当前可领取**的任务后退出，不等待未来的退避重试。常驻使用 `paoding worker`。

队列是至少一次执行。数据库/文件导出靠幂等控制达到稳定效果；Telegram 完成通知在发送成功后确认，若进程在发送和确认之间崩溃可能重复通知，这是外部副作用的真实边界。

## 备份与同步

PostgreSQL、`data/media`、Vault 是三个不同的资产：

- PostgreSQL 用 `pg_dump` 备份；原始媒体目录用文件备份工具保存。
- Vault 可从 PostgreSQL 重建，但本地原始媒体不能仅从拆解 JSON 重建。
- Syncthing 可同步 Vault 和媒体归档；不要同步运行中的 PostgreSQL 数据目录。
- 多设备若要共享实时任务/查询，连接同一主数据库/API。各机独立数据库之间尚无合并协议。
- Markdown 中的媒体路径是写入主机的绝对路径；不同设备应采用一致的目录映射，或后续扩展媒体服务 URL。

## 验证命令

```sh
uv sync --frozen
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run pytest -q
uv run paoding demo
uv build
```

运行 PostgreSQL 集成测试，使用一次性数据库：

```sh
docker run --rm --name paoding-test-postgres \
  -e POSTGRES_USER=paoding -e POSTGRES_PASSWORD=paoding -e POSTGRES_DB=paoding_test \
  -p 127.0.0.1:55439:5432 -d pgvector/pgvector:pg16
PAODING_TEST_POSTGRES_URL=postgresql+psycopg://paoding:paoding@127.0.0.1:55439/paoding_test uv run pytest -q -m postgres
docker stop paoding-test-postgres
```

测试 fixture 会清空目标库的庖丁表，必须指向名称含 test 的独立数据库。开发/生产库均不应作为这个测试目标。

## v0.2 运维入口

- `paoding doctor` 检查配置存在性，不打印密钥。
- `paoding reindex` 回填缺失/旧模型向量；`--force` 全量回填。
- `paoding taxonomy --run` 触发分类树演化；不带参数查看分类。
- `paoding bookmarklet` 输出跨浏览器收录书签；`/capture` 是同源收录页。
- `paoding watch-drop` 同时监听 Drop 和可选 WorkBuddy 目录，支持 `.paoding.json` 清单。
- 完整准备事项见 [配置总清单](configuration.md)。
