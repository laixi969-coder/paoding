# 本次验证记录

## v0.2 复核

在 macOS Apple Silicon 上，Python 3.12 与一次性 PostgreSQL 16/pgvector 容器执行全量测试：**83 项通过**，包含本地 FFmpeg 实际处理、PostgreSQL 分类关联、外部 API 协议模拟、飞书/企微加密回调、附件和失败通知。Alembic 无迁移漂移；Ruff、Compose 配置、依赖锁定和 v0.2 安装包构建通过。测试期间有 3 条第三方依赖弃用提醒。

真实平台密钥、Bot、服务账户和 Windows 主机尚待配置后联调，具体准备事项见 [配置总清单](configuration.md)。以下为 v0.1 的历史验证记录。

环境：macOS Apple Silicon，Python 3.12.12；PostgreSQL 16 + pgvector 在独立的一次性 Docker 容器中运行。未读取或使用真实模型 API Key，未向用户的聊天账号发送通知。

| 检查 | 结果 |
|---|---|
| 全量 pytest（含真实 PostgreSQL） | 47 项通过，0 失败，0 跳过 |
| PostgreSQL 并发领取 | 20 项任务，多线程领取，无重复 |
| PostgreSQL 并发投喂与模型健康写入 | 相同任务去重，健康记录原子 upsert |
| PostgreSQL JSONB / pgvector | 标签过滤、1536 维写入、cosine 检索通过 |
| 数据库迁移 | 在一次性数据库完成 downgrade base → upgrade head，Alembic check 无漂移 |
| 反馈并发去重 | 12 次同事件并发提交，仅加分一次 |
| 崩溃与双写恢复 | 租约回收、旧 Worker 隔离、文件失败后重试通过 |
| 账号与进化 | 冷启动、游标失败不推进、旧翻页不回退、画像阈值、Map-Reduce 来源链通过 |
| 模型协议 | OpenAI-compatible 回退、Schema 错误回退、Gemini/Claude 请求载荷通过 MockTransport 验证 |
| REST / MCP | 鉴权、幂等、校验错误、真实 FastMCP 内存客户端工具调用通过 |
| 实际服务启动 | 独立启动 Uvicorn，health、OpenAPI、stats、nodes、atoms 六个 HTTP 入口均返回 200，检查后停止 |
| 外部源保护 | 内网地址、重定向、DNS 固定连接、下载限额、保留签名编码通过 |
| CLI demo | 实际创建数据库和 Vault 文件；明确带 DEMO 标记 |
| 安装包 | sdist/wheel 构建成功；wheel 在隔离环境执行 demo 成功；含迁移和默认模型配置，无 pycache |
| Ruff | lint / format 检查通过 |
| 部署配置 | docker compose config 与 uv lock --check 通过；测试容器已停止 |

全量测试覆盖率报告为 74%（pytest-cov，不含单独执行的 CLI 安装包与启动冒烟流程）。剩余覆盖主要是命令行入口、真实网络异常组合和常驻文件/cron 循环，不代表所有第三方平台已经完成验收。

测试期间有 3 条来自 FastAPI/Starlette、Authlib 的依赖弃用提醒，没有项目自身的失败或被隐藏的警告。

尚未完成真实厂商验收的能力：各模型账户的实际可用 ID/模态权限，TikHub/MediaCrawler 等用户采集服务，Telegram/飞书真实回调，Windows 主机运行。上述边界另见 [接入状态](integrations.md)。
