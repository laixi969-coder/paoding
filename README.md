# 庖丁 PaoDing Engine

依据 [PRD v1.0](docs/PRD.md) 实现的 Local-First Headless 知识引擎。重点是可扩展、可恢复的底层框架，以及“投喂 → 结构化解剖 → 原子入库 → Obsidian 导出 → REST/MCP 检索”的完整闭环。

**当前交付：v0.2，已实现知识库工作台、PRD 的主要后端模块和采集适配器。** 抖音/小红书使用 TikHub 等采集服务，微信/RSS 与视频号导出通道按配置启用。未配置时任务会明确失败，不会自动变成模拟结果。密钥和服务统一见 [配置总清单](docs/configuration.md)，真实账户验收范围见 [接入状态与边界](docs/integrations.md)。

## 立即验证（不需要 Docker 或模型密钥）

需要 Python 3.12/3.13 和 uv。在项目目录执行：

```sh
uv sync --frozen
uv run paoding demo
```

会在 `data/demo/` 创建独立 SQLite 数据库、示例 Markdown、完整 Obsidian 目录和演示卡片。所有合成内容标记 `[DEMO]`，命令返回真实导出路径。演示模式禁止连接 PostgreSQL，防止污染正式库。

## 正式本地运行

本机启动后打开 [知识库工作台](http://127.0.0.1:8010/)，浏览、搜索和筛选卡片、查看拆解详情、反馈知识元件、管理追踪账号和查看进化结果；通过 [后台设置](http://127.0.0.1:8010/settings) 配置模型、采集服务和 Bot。已有密钥不会回显；单模型生成测试会发送真实请求，可能产生费用。

```sh
uv sync --frozen
cp .env.example .env
docker compose up -d --wait postgres
uv run paoding init
```

Windows PowerShell 可执行 `./scripts/dev.ps1`；macOS 可执行 `bash scripts/dev.sh`。

1. 在 `.env` 填写需要使用的厂商密钥；在 `config/models.yaml` 配置实际可用模型与优先顺序。
2. `uv run paoding models sync` 同步可见模型，`uv run paoding models test` 执行小额真实生成探针。
3. 分别在两个终端启动 API 和 Worker：

```sh
uv run paoding serve
uv run paoding worker
```

API 默认 `127.0.0.1:8010`，交互文档：[Swagger](http://127.0.0.1:8010/docs)。`/health/live` 是存活检查，`/health/ready` 检查数据库和迁移。API 不会代替你启动 Worker；投喂后需要 Worker 才会执行。

```sh
uv run paoding ingest "https://example.com/article" --instruction "提炼营销策略和跨界基因"
uv run paoding ingest "./case.pdf"
uv run paoding worker --drain
```

或者：

```sh
curl -X POST http://127.0.0.1:8010/api/v1/ingest \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: my-case-001' \
  -d '{"url":"https://example.com/article","priority":"normal"}'
```

响应返回 `job_id`。依次查询 `/api/v1/jobs/{job_id}` 和 `/api/v1/nodes/{node_id}`。`job.status=succeeded` 表示数据库已提交；`node.vault_status=synced` 表示 Markdown 已写入。不要把这两个状态混为一谈。

## 后台服务和 MCP

```sh
uv run paoding scheduler
uv run paoding watch-drop
uv run paoding mcp
uv run paoding doctor
uv run paoding taxonomy --run
uv run paoding reindex
uv run paoding bookmarklet
```

`scheduler` 按 `PAODING_TIMEZONE` 每日 02:00 巡检、每周一 03:00 生成趋势报告，重启会补当前周期的任务。`watch-drop` 监听文件稳定后入库，支持 WorkBuddy 导出的标准文件。

安装 PM2 后可运行 `pm2 start ecosystem.config.cjs`、`pm2 save`。使用 `scripts/install-startup.sh` / `scripts/install-startup.ps1` 注册登录恢复；Docker Desktop 也应启用登录时启动。

MCP 客户端配置示例（把路径替换成实际项目绝对路径）：

```json
{
  "mcpServers": {
    "paoding": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/Paoding", "run", "paoding-mcp"]
    }
  }
}
```

提供 PRD 中的五个工具：`paoding_query_playbook`、`paoding_fetch_atoms`、`paoding_scan_trends`、`paoding_summon_creator`、`paoding_feedback_utility`。反馈工具额外要求 `event_id`，用来保证网络重试不重复加分。

## 框架与工程目录

```text
src/paoding/
  api/          FastAPI 入口、鉴权、Webhook、任务状态
  core/         环境配置、业务错误
  domain/       强类型内容模型、采集/分析扩展协议
  ingestion/    原生采集、RSS、MinerU/Jina/Firecrawl、标准化、目录监听
  integrations/ Bot 加密回调、附件下载、消息卡片、服务客户端
  media/        FFmpeg 视频、音轨与分段处理
  models/       厂商协议、动态路由、健康探针、故障转移、隔离演示
  services/     事务队列、Worker、账号巡检、Map-Reduce、检索与反馈
  storage/      SQLAlchemy、JSONB/pgvector、Obsidian 原子写入
  cli.py        paoding 命令入口
  mcp.py        FastMCP 五个工具
migrations/     独立版本化数据库迁移
config/         可热重载模型路由
tests/          业务、恢复、协议、API/MCP、真实 PostgreSQL 测试
docs/           架构决策、接入合同、运维说明、原始 PRD
```

详细说明：[PRD 对照与待验收事项](docs/prd-status.md) · [架构](docs/architecture.md) · [平台和模型接入](docs/integrations.md) · [API](docs/api.md) · [运维](docs/operations.md)。

## 验证

```sh
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run pytest -q
uv build
```

真实 PostgreSQL 测试通过 `PAODING_TEST_POSTGRES_URL` 指向**独立、可清空的测试库**（库名必须包含 `test`）。测试会清空该库中的庖丁表。CI 包含 macOS/Windows/Linux 和 PostgreSQL 两组任务；本地已执行的检查记录在 [验证记录](docs/verification.md)。
