# 密钥与服务配置总清单（v0.2）

API Key 等配置保存在项目根目录 `.env`，模板是 `.env.example`；网页登录令牌保存在 `data/oauth`。模型 ID 和回退顺序可通过设置页配置，默认值在 `config/models.yaml`，账号与 RSS 映射写在 `config/feeds.yaml`。不需要把密钥粘贴进聊天。

也可以打开本机 [后台设置](http://127.0.0.1:8010/settings)，在每个服务商的 Base URL 和 API Key 输入框里配置。点击该服务商的“测试连接”或“同步最新模型”会先保存当前服务商的改动，再将其 API Key 发送到所填写的地址；请确认地址可信。同步结果是账户可见模型列表；点击“选为文字/图片/视频”会将该模型设为对应路由的首选并保留其他备用项，需确认模型支持所选模态。同步本身不会自动更改模型路由。

### 网页授权、自定义服务与逐模型测试

- **ChatGPT 登录**：通过官方本地应用授权流程登录，需账号具备相关方案使用权限并在授权时同意使用权限。该方式使用公开 Responses API；目前在庖丁内支持文字与图片，视频请选择其他支持视频输入的服务。
- **Google Gemini 登录**：在 [Google Cloud Console](https://console.cloud.google.com/) 选择或创建项目，[启用 Generative Language API](https://console.cloud.google.com/flows/enableapi?apiid=generativelanguage.googleapis.com)，配置 OAuth 同意屏幕及测试用户，并创建“桌面应用”类型的 OAuth 客户端。在后台的“Google 授权”填写 Client ID、Client Secret 和项目 ID 后点击“使用 Google 授权 Gemini”。此方式请求 `cloud-platform` 和 `generative-language.retriever` 权限，调用使用所填项目的配额和计费，与 Gemini 网页订阅不同；详见 [Google 官方指南](https://ai.google.dev/gemini-api/docs/oauth)。
- **OpenRouter 网页授权**：使用 OpenRouter 自身账户授权。可选择该账户能使用的 GPT、Qwen、GLM 等模型，计费和额度属于 OpenRouter；这不是这些厂商原生账户的登录或订阅授权。
- 其他厂商的网页版登录不会自动变成模型 API 凭据；需要其官方开放适用于第三方应用的授权接口。当前可配置 API Key 或通过支持的中转服务接入。
- “添加自定义服务商”可选 OpenRouter、Atlas Cloud 预设，或填写其他 OpenAI-compatible Base URL 和密钥。Atlas Cloud 的聊天接口使用其 `/v1` Base URL；图像/视频生成接口属于不同 API，不能简单视为聊天模型。
- 每个服务商可同步列表，或手动添加模型 ID（例如 `openai/gpt-4o`）。每行“测试此模型”只对该模型发送一次简短文字请求，可能产生费用；成功不等于图片或视频能力已通过测试。
- “选为文字/图片/视频”会把该模型放在对应路由首位并保留其余备用项；需要自行确认模型和服务接口支持该模态。

授权登录使用官方 OAuth、PKCE 和单次状态校验。ChatGPT 和 Google 身份签名及权限会验证，令牌保存在本机受限权限文件中并在需要时刷新。OpenRouter 登录会生成并保存该账户授权的 API Key。请勿将项目的 `.env` 或 `data/oauth` 发送给他人。

## 最小启动组合

1. PostgreSQL：运行 `docker compose up -d --wait postgres`，再运行 `uv run paoding init`。
2. 文本分析：DeepSeek、Qwen、Gemini、OpenAI 中至少配置一个，并在 text 路由中填写账户实际可用模型。
3. 图片分析：配置 Qwen、Gemini、Claude 中至少一个可用视觉模型。
4. 视频分析：配置 Gemini 或 Qwen 的可用视频模型；本机安装 FFmpeg/FFprobe。
5. 社媒采集、Bot、复杂 PDF 等按下面需要的功能配置，不必购买全部服务。

推荐先用 DeepSeek（文本）+ Qwen（图像/视频回退）+ Gemini（完整视频视听）验证三刀。模型示例 ID 需通过自己的账户验证，不代表最新版本。

## 模型与语音/向量

| 用途 | `.env` 变量 | 其他配置 |
|---|---|---|
| DeepSeek 文本/推理 | `DEEPSEEK_API_KEY` | models.yaml 的 deepseek 与 text 路由 |
| Qwen 图像/视频/文本 | `QWEN_API_KEY` | protocol=qwen；video 路由选支持视频的模型 |
| 豆包图像/视频/文本 | `DOUBAO_API_KEY` | protocol=doubao；填实际模型或推理接入点 ID |
| Gemini 原生视频/图像/文本 | `GEMINI_API_KEY` | protocol=gemini；支持 Files API 上传 |
| Claude 图像/文本 | `ANTHROPIC_API_KEY` | protocol=anthropic |
| OpenAI 文本/图像/语音/向量 | `OPENAI_API_KEY` | protocol=openai |
| 音频转写 | `PAODING_TRANSCRIPTION_PROVIDER`、`PAODING_TRANSCRIPTION_MODEL` | 例如 openai / whisper-1；需该厂商 Key；接口须支持 `/audio/transcriptions` |
| 向量检索 | `PAODING_EMBEDDING_PROVIDER`、`PAODING_EMBEDDING_MODEL` | 例如 openai / text-embedding-3-small；输出必须为 1536 维 |

纯音频投喂必须配置语音转写；视频可直接由 Gemini 理解视听内容，转写为可选增强。Qwen-VL 等仅看画面的路由会清空未验证的台词与 BGM 字段。只有确认模型能够理解音频时才在 provider 中配置 `video_audio: true`。

`uv run paoding models sync` 同步可见模型，不会自动改生产路由。`uv run paoding models test` 会执行真实小额生成。调度器每天自动同步模型目录。

更换向量模型后运行 `uv run paoding reindex` 回填旧记录；`--force` 重建全部。模型 Key 不会出现在 doctor 输出、数据库或 Vault 中。

## 平台采集与文档

| 功能 | 配置 | 要准备的服务/文件 |
|---|---|---|
| 抖音、小红书 | `PAODING_TIKHUB_TOKEN` | TikHub 账户和余额；默认 URL https://api.tikhub.io |
| YouTube | 通常无 Key；`PAODING_COOKIES_FILE` 可选 | yt-dlp 随依赖安装；本机 FFmpeg；部分作品需要 Netscape Cookie 文件 |
| Pinterest | `PAODING_COOKIES_FILE` 可选 | gallery-dl 随依赖安装；需要登录时提供 Cookie 文件 |
| X | `PAODING_RSSHUB_URL`，或 Cookie + yt-dlp | RSSHub 可提供推文正文；yt-dlp 用于其支持的视频 |
| 公众号持续追踪 | `config/feeds.yaml` 的主页→feed_url；可设 `PAODING_WEWE_RSS_URL` | 自己运行并登录的 WeWe-RSS；相对 feed_url 会拼到该地址 |
| 视频号/WorkBuddy | `PAODING_WORKBUDDY_DIR` 或默认 Drop | WorkBuddy 导出原片、正文或 `.paoding.json` 清单；不需要单独模型 Key 以外的 WorkBuddy API |
| 普通网页 | `PAODING_WEBPAGE_READER=local` | 默认无需外部服务 |
| Jina Reader | `PAODING_WEBPAGE_READER=jina`、`PAODING_JINA_TOKEN` 可选 | Jina 服务；无 Key 时受公共额度限制 |
| Firecrawl | `PAODING_WEBPAGE_READER=firecrawl`、`PAODING_FIRECRAWL_TOKEN` | 可用的 Firecrawl 服务；自托管可改 URL |
| 复杂/扫描 PDF、Word、PPT | `PAODING_MINERU_TOKEN`（云端），或 `PAODING_MINERU_MODE=local` | MinerU 云账户，或单独安装本地 mineru 命令及其模型 |
| 自有采集网关 | `PAODING_COLLECTOR_URL`、`PAODING_COLLECTOR_TOKEN` | 可选，配置后优先走统一 Collector Bridge |

国内平台采集优先使用 TikHub；需要自行运行 MediaCrawler 时，可让自己的服务适配统一 Collector Bridge，也可把采集文件导出到 Drop。无须同时搭建 TikHub 和 MediaCrawler。

账号冷启动会扫描历史后取点赞 Top 20。`PAODING_COLLECTOR_MAX_PAGES` 默认为 20；遇到分页上限会明确报错，增大上限后重试。RSS 本身可能仅保留近期条目，也可能没有互动数，相关限制会写入巡检结果，不会冒充全平台历史排名。

## 三种 Bot

| Bot | 必填变量 | 开放平台配置 |
|---|---|---|
| Telegram | `PAODING_TELEGRAM_BOT_TOKEN`、`PAODING_TELEGRAM_SECRET` | webhook 指向 `https://你的入口/api/v1/webhooks/telegram`，注册时 secret_token 与后者一致 |
| 飞书 | `PAODING_FEISHU_APP_ID`、`PAODING_FEISHU_APP_SECRET`、`PAODING_FEISHU_VERIFICATION_TOKEN`；启用加密时再填 `PAODING_FEISHU_ENCRYPT_KEY` | 订阅消息接收事件；授予机器人发送消息、读取消息图片/文件资源权限；回调 `/api/v1/webhooks/feishu` |
| 企业微信自建应用 | `PAODING_WECOM_CORP_ID`、`PAODING_WECOM_AGENT_ID`、`PAODING_WECOM_CORP_SECRET`、`PAODING_WECOM_TOKEN`、`PAODING_WECOM_ENCODING_AES_KEY` | 接收消息 URL `/api/v1/webhooks/wecom`，Token/EncodingAESKey 与应用后台一致；设置可信出口 IP 和可见成员范围 |

Bot 支持链接、正文、图片和文件；语音需配置转写。飞书支持加密事件、富文本、附件和完成卡片；企微支持加密 URL 验证、被动接单回复和主动完成通知。用户无需同时开通三个 Bot。公网平台回调需要可达的 HTTPS 入口，单纯内网 Tailscale 地址通常无法接收平台推送。

## 主机与运行

| 项目 | 配置/操作 |
|---|---|
| Python | 3.12/3.13，`uv sync --frozen` |
| 数据库 | Compose 自带 PostgreSQL 16 + pgvector；Redis 当前为可选 cache profile |
| FFmpeg | macOS `brew install ffmpeg`；Windows 用发行包/包管理器安装并把 ffmpeg、ffprobe 加入 PATH |
| API 令牌 | `PAODING_API_TOKEN`；开放网络监听时必填 |
| 时区 | `PAODING_TIMEZONE`，默认 Asia/Shanghai |
| 监控目录 | `PAODING_DROP_DIR`、可选 `PAODING_WORKBUDDY_DIR` |
| 登录后恢复 | 安装 PM2；执行 `scripts/install-startup.sh` 或 `scripts/install-startup.ps1`；Docker Desktop 也需登录时启动 |
| Tailscale | 安装、登录自己的 tailnet；通过受控设备访问 API；本程序不代替用户登录 |
| Syncthing | 安装并配对设备；同步 Vault/媒体；不要同步运行中的数据库目录 |

## 配置后统一验收

```sh
uv run paoding doctor
uv run paoding init
uv run paoding models sync
uv run paoding models test
uv run paoding serve
# 另一个终端
uv run paoding worker
# 可选常驻
uv run paoding scheduler
uv run paoding watch-drop
```

Doctor 只显示配置是否存在，不回显密钥，也不会调用收费模型。厂商账户权限、实际模型 ID、登录态和回调入口必须在填完上述配置后进行真实联调。
