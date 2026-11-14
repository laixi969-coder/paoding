# 接入状态与协议（v0.2）

所有密钥、账号、服务与本机依赖统一见 [配置总清单](configuration.md)。此页区分已实现代码与真实账户验收。

## 已实现

- 原生采集：YouTube yt-dlp 下载 720p 视频及字幕；Pinterest gallery-dl；TikHub 抖音/小红书详情与账号分页；微信公开文章清洗；WeWe-RSS/RSSHub 账号订阅。
- WorkBuddy：监控标准文件与 `.paoding.json` 清单，保留导出来源、正文和媒体快照。
- 文档：基础 PDF；MinerU 本地/云端上传、任务恢复、Markdown/图表归档；Jina/Firecrawl 网页读取。
- 视频：FFmpeg 720p 标准化、音轨提取、分段、原片时间轴恢复；Gemini Files API 上传与清理；Qwen/豆包原生 video_url；可选语音转写。
- Bot：Telegram、飞书（含加密事件、富文本和完成卡片）、企微（加密握手/事件/被动回复/完成通知）；附件异步下载、事件去重和失败通知。
- 进化：画像、跨界元规则、趋势比较、同义标签归并、分类树和别名检索、变更日志、旧分类重定向、权重衰减。
- 工具：REST/MCP/CLI，书签一键收录页，Drop，模型自动同步，向量回填，PM2 登录恢复安装脚本，配置自检。

真实厂商调用、登录态及消息回调仍需用户配置后联调。本次测试采用协议模拟和本地真实媒体/数据库，并未使用用户账户发送消息或调用收费模型。Windows 已提供 CI 与安装脚本，本次实际运行主机为 macOS。

## Collector Bridge（可选覆盖原生采集）

设置 `PAODING_COLLECTOR_URL` 后，优先将采集交给自己的服务。请求使用 `Authorization: Bearer <PAODING_COLLECTOR_TOKEN>`。

`POST /v1/resolve`：

```json
{"url":"https://www.xiaohongshu.com/explore/CONTENT_ID"}
```

```json
{"url":"https://www.xiaohongshu.com/explore/CONTENT_ID","title":"标题","text":"正文","modality":"image","media_urls":["https://cdn.example/image.jpg"],"metrics":{"likes":1000,"engagement_rate":0.08},"published_at":"2026-11-14T10:00:00Z"}
```

`POST /v1/accounts/items`：

```json
{"url":"https://www.douyin.com/user/CREATOR_ID","cursor":null,"limit":20,"sort":"engagement_desc"}
```

```json
{"items":[{"url":"https://www.douyin.com/video/CONTENT_ID","title":"标题","modality":"video","metrics":{}}],"next_cursor":"DURABLE_CURSOR","has_more":false,"scan_complete":true,"scanned_count":100}
```

冷启动须返回历史 Top 20，后续使用 `published_asc` 增量，最多每页 100 项。水位必须持久稳定，翻页必须推进。引擎在一个事务中先保存内容任务再提交游标。`resolved=true` 表示条目正文/媒体已完整解析，可直接使用此次采集快照；不要将只有标题的列表摘要标成 resolved。

媒体 URL 必须公开可下载；本机文件须导出到 Drop。`local_media_paths` 仅允许指向引擎管理的 `data/media`，不能借接口读取任意主机文件。没有可靠分母时应省略 engagement_rate。

## WorkBuddy 清单

在导出文件同目录创建 `case.paoding.json`：

```json
{"url":"https://mp.weixin.qq.com/s/ARTICLE","title":"案例标题","text":"正文或字幕","files":["image.png","article.pdf"],"instruction":"拆解营销结构"}
```

最多 8 个相对路径，必须位于清单所在目录内。导入前会保存不可变快照。清单与附件同目录时，Drop 按清单成组导入；可先复制临时目录，完成后再移入监控目录。

## 模型与限制

- 路由发现与能力授权分离：sync 发现模型，models.yaml 决定模态与顺序。健康探针是文本生成验证，不等于完整模态验收。
- 图像 HEX 来自原始像素算法；模型无法重写测量值。多图卡片会注明色卡以首图为准。
- Qwen-VL 等无音频能力的路由不会生成未验证的前 3 秒台词和 BGM。豆包按账户实际视频模型/接入点配置。
- 大视频会切成带偏移的短片段，分段分析后合并。Gemini 超过 inline 阈值时使用可恢复上传协议，完成后清理临时云文件。
- RSS 历史范围、互动字段和平台登录限制由上游决定。视频号支持 WorkBuddy/标准文件/自有采集网关，不直接控制微信桌面客户端或抓包解密工具。

## 协议依据

[官方 yt-dlp](https://github.com/yt-dlp/yt-dlp)、[官方 gallery-dl](https://github.com/mikf/gallery-dl)、[TikHub 官方 SDK 端点表](https://github.com/TikHub/TikHub-API-Python-SDK/blob/main/docs/reference.md)、[MinerU API](https://mineru.net/doc/docs/index_en/)、[Gemini Files](https://ai.google.dev/api/files)、[Qwen OpenAI-compatible 视频参数](https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions)、[飞书事件验签与解密](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/encrypt-key-encryption-configuration-case)、[企业微信官方回调实现](https://github.com/sbzhu/weworkapi_python/blob/master/callback/WXBizMsgCrypt.py)。
