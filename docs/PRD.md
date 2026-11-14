# 庖丁 (PaoDing) —— 全模态导演/操盘手级自进化知识引擎 PRD (v1.0)

> **典故出处：** 《庄子·养生主》：“庖丁为文惠君解牛……彼节者有间，而刀刃者无厚；以无厚入有间，恢恢乎其于游刃必有余地矣。”  
> **产品哲学：** 庸人看内容，只见整牛（表象皮囊）；**「庖丁」**看内容，直入筋骨缝隙（前 3 秒钩子、色彩 HEX 配比、Bento 网格线、分镜节拍与操盘 SOP）。顺应底层规律一键拆解，化整为零，入屉归档，随取随用。

| 文档属性 | 详细内容 |
| :--- | :--- |
| **产品中文名** | **庖丁** |
| **产品英文/代号** | **PaoDing**（CLI 命令：`paoding` / 服务名：`paoding-engine`） |
| **产品形态** | Headless 知识底座（FastAPI + FastMCP Server）+ 本地双写库（PostgreSQL + Obsidian `.md`） |
| **核心定位** | 将全网单点内容（视频/图文/长文/PDF）与持续追踪的创作者账号，通过多模型动态路由执行“导演与操盘手级”解构，沉淀为可自我进化、跨领域相通的结构化基因库，直接赋能下游 AI Agent、Web 产品与移动端 App。 |
| **运行环境** | Local-First 跨平台架构（原生支持 macOS Apple Silicon 如 Mac mini/MacBook，以及 Windows x86/CUDA 主机，支持 Tailscale 异地组网与 Syncthing 实时同步）。 |

---

## 一、 痛点诊断与产品破局逻辑（第一性原理）

1. **破局“收藏即吃灰，传统 RAG 切片碎”：**
   传统知识库只做原文保存与粗暴向量切片，喂给 Agent 全是冗余废话。**「庖丁」**在入库前强制执行**“专家级结构化解剖（目无全牛）”**，把原始素材直接提纯为**前 3 秒黄金钩子、像素级 HEX 色卡、Bento 网格版式、分镜拉片表、营销 SOP 公式**。
2. **破局“国内平台封闭围墙与单一模型偏科”：**
   微信（公众号/视频号）、抖音、小红书各自筑墙，单一模型无法通吃。本系统采用**“采集与认知彻底解耦 + 多模型动态路由”**，利用本地住宅 IP 与专属通道（如 WorkBuddy、TikHub）把素材洗成标准本地文件，再按模态动态分配给 Gemini、Claude、DeepSeek、Qwen-VL、豆包等最优模型。
3. **破局“跨平台知识孤岛与越存越乱”：**
   实施**“皮骨分离（通用底层基因 vs 平台容器适配）”**与**“自进化飞轮（Map-Reduce 蒸馏）”**，让抖音拆出的神级钩子能无缝套用到小红书和公众号，并定期合并同义套路、动态长出新分类、淘汰过时热梗。

---

## 二、 系统全局数据流（PaoDing Pipeline）

```text
[极客无感投喂层] ──► (1) 手机端 Telegram/企微/飞书 Bot 转发链接即走
                    (2) Mac/Win 端 Raycast 快捷键 (paoding ingest) / 浏览器书签脚本一键吸取
                    (3) 本地 PaoDing_Drop 文件夹拖拽 PDF/音视频即入库
                           │
                           ▼
[路由与监听层]   ──► 识别 URL 类型 ──┬─► 【单条内容】直接进入异步解剖队列
                                   └─► 【博主主页】写入 Watcher 表，抓取历史 Top 20 冷启动 + 每日凌晨 Cron 增量巡检
                           │
                           ▼
[破墙与标准化层] ──► 微信生态(WorkBuddy/WeWe-RSS) │ 抖音小红书(TikHub/本地Cookie) │ 海外与网页(yt-dlp/Gallery-dl/Jina/MinerU)
                    统一洗为本地标准件：无水印视频(.mp4) + 高清图集(.jpg) + 纯净文档(.md)
                           │
                           ▼
[多模型动态路由] ──► 动态模型注册表 (自动同步最新模型 + Ping 测速 + 故障自动 Fallback)
                    ├─► 第一刀(视听流) ──► Gemini 2.5 Pro / Qwen-VL-Max / 豆包视频 ──► 输出《导演级视听分镜 JSON》
                    ├─► 第二刀(视觉流) ──► Colorgram算法吸色 + Claude 3.7 Sonnet   ──► 输出《版式网格与用色 JSON》
                    └─► 第三刀(策略流) ──► DeepSeek-R1 / V3                        ──► 输出《营销操盘 SOP JSON》
                           │
                           ▼
[双写存储与进化] ──► 1. 写入 PostgreSQL (JSONB + pgvector 向量) + 本地 PaoDing_Vault (Obsidian .md 目录树)
                    2. 进化引擎 (PaoDing Evolver) 每周执行 Map-Reduce：提炼跨界元规则 (Meta-Playbook) + 更新博主数字分身
                           │
                           ▼
[多端赋能网关]   ──► PaoDing MCP Server (供视频/营销/趋势 Agent 调用) + RESTful API (供 Web 工作台与 App 调用)
```

---

## 三、 核心功能模块需求规格（Functional Specifications）

### 模块 1：极客无感投喂与账号持续追踪（Ingest & Watcher）

| 功能点 | 优先级 | 需求规格描述 |
| :--- | :---: | :--- |
| **统一投喂接口** | **P0** | 提供 `POST /api/v1/ingest`，接收参数：`url` (必填)、`instruction` (可选附加指令，如 `#只拆配色`)、`priority` (默认 normal)。 |
| **URL 智能识别器** | **P0** | 正则自动判定输入链接属于**单点内容 (Item)** 还是**创作者主页 (Creator Profile)**，并自动识别所属平台（小红书/抖音/公众号/视频号/YouTube/X/Pinterest/数英网/PDF）。 |
| **移动端 Bot 投喂** | **P0** | 对接飞书/企微/Telegram Webhook。用户转发链接或图文至 Bot，系统毫秒级回复“🔪 庖丁已接单”，解析完成后推送结构化摘要卡片与入库路径。 |
| **桌面端极客快捷流** | **P0** | 1. **Raycast / 全局快捷键脚本 (`paoding grab`)**：一键抓取当前活动浏览器 Tab 的 URL 与标题送入后台。<br>2. **本地拖拽监控夹 (`PaoDing_Drop`)**：基于 `watchdog` 监听本地目录，拖入 PDF、长图或原片自动触发解剖。 |
| **账号增量巡检引擎** | **P0** | 针对入库的博主主页：首次入库拉取历史高赞 Top 20 内容建立基座；随后由后台定时任务（每日凌晨 02:00）比对 `last_content_id` 或发布时间戳，仅抓取新增内容入库。 |

---

### 模块 2：跨生态破墙采集与标准化预处理（Normalizer）

| 平台类别 | 目标平台 | 采集与标准化方案 |
| :--- | :--- | :--- |
| **微信封闭生态** | 微信公众号、视频号 | 1. **WorkBuddy 联动通道**：通过本地监控目录读取腾讯 WorkBuddy 自动导出的公众号/视频号数据。<br>2. **备用通道**：公众号接入本地 `WeWe-RSS`；视频号配合本地抓包解密工具提取无水印 MP4。 |
| **国内强风控社媒** | 小红书、抖音 | 优先调用商业中转接口（如 **TikHub API**）或本地挂载持久登录态的 `MediaCrawler`，提取无水印视频直链、高清多图原图数组、正文文案及互动指标（赞/藏/评/转发）。 |
| **海外社媒与审美库** | YouTube、X、Pinterest | YouTube 采用 `yt-dlp` 下载 720p 视频与音轨；Pinterest 采用 `gallery-dl` 抓取高清原图与画板元数据；X 采用 RSSHub 或 Scraper API 抓取推文线程。 |
| **行业案例与 PDF** | 数英网、普通网页、本地 PDF | 网页采用 **Jina Reader (`r.jina.ai`)** 或 `Firecrawl` 转为干净 Markdown；复杂多栏 PDF 与全案 PPT 采用 **MinerU** 提取结构化 Markdown 与图表。 |

---

### 模块 3：动态模型网关与健康探针（Dynamic Model Registry）

1. **多厂商统一纳管：** 原生支持 **DeepSeek**、**Qwen (通义千问 VL/Max)**、**Doubao (豆包多模态)**、**Gemini**、**Claude**、**OpenAI**。
2. **动态同步最新模型 (`paoding models sync` / `GET /api/v1/models/sync`)：**
   * 自动轮询各厂商 `/v1/models` 接口，拉取官方最新发布的模型 ID（如 DeepSeek 新版推理模型、Qwen 最新视觉模型等），支持热切换。
3. **连接测速与自动故障转移 (Auto-Fallback)：**
   * 提供 `paoding models test` 命令与 API 探针，实时检测各模型 API Key 有效性、响应延迟（ms）与健康度。
   * **降级策略：** 若视频拆解首选的 `Gemini 2.5 Pro` 遇到网络波动，路由器在 1 秒内自动将该视频任务无缝切换给国内低延迟的 `qwen-vl-max-latest` 或 `doubao-pro-vision` 完成拉片。

---

### 模块 4：“庖丁三刀”专业解剖引擎与跨界基因提取（Cleave Engine）

系统拒绝笼统摘要，根据模态强制输出包含**“专业解剖参数 + 跨领域通用基因 (`cross_domain_dna`)”**的强类型 JSON：

#### 第一刀：导演级视听分镜解剖刀（针对 抖音 / 视频号 / YouTube 短片）
* **核心提取字段：**
  1. `pacing_metrics`：前 3 秒硬切镜头数（Cuts）、全片平均镜头时长（ASL）、BGM 情绪转折与骤停卡点逻辑。
  2. `golden_hook_0_to_3s`：前 3 秒视觉奇观画面 + 听觉痛点台词 + 触发的底层心理学机制（如损失厌恶、反常识证伪）。
  3. `beat_sheet`（分镜拉片表）：按时间轴切分叙事节拍，标注景别（ECU/CU/MS/WS）、运镜（推/拉/摇/移/手持呼吸感）、画面描述与反推的 AI 视频生成英文 Prompt。
  4. `reusable_script_formula`：剥离具体业务后的填空式爆款脚本骨架。

#### 第二刀：视觉与版式网格解剖刀（针对 Pinterest / 小红书高赞图文 / 空间设计图）
* **算法前置：** 使用 `colorgram.py` 提取图片像素级真实 HEX 色卡与占比。
* **核心提取字段：**
  1. `color_system`：60%主色调、30%辅助色、10%视觉焦点色（Accent）的 HEX 值、冷暖对比法则与情绪氛围词。
  2. `layout_and_grid`：底层版式骨架（如不对称 Bento Grid、黄金螺旋、瑞士网格）、第一视觉落点与扫视动线、留白率百分比。
  3. `typography`：标题与正文字重极差对比、衬线/无衬线混搭、主体与文字的穿插遮挡（Depth Interleaving）技巧。
  4. `reusable_mj_prompt`：包含光影质感、表面材质肌理与构图色值的 Midjourney / 即梦 标准生图 Prompt。

#### 第三刀：营销操盘与策略解剖刀（针对 数英网 / 公众号深度长文 / 研报 PDF）
* **核心提取字段：**
  1. `core_pain_point` & `consumer_insight`：剥离公关自嗨后的真实商业困局与客群底层心理洞察。
  2. `big_idea` & `super_symbol`：核心创意概念、嫁接的大众文化母体、视觉锤与语言钉。
  3. `execution_sop`：按“冷启动预热 $\rightarrow$ 节点引爆 $\rightarrow$ 社交裂变/H5机制 $\rightarrow$ 空间动线/私域转化”还原执行步骤。

#### 跨领域基因打通层（皮骨分离：所有模态必填）
* 每张拆解卡片均包含 `cross_domain_dna` 对象，打破平台次元壁：
  * `universal_principle`：一句话提炼剥离平台载体后的底层通用规律（如文案节奏、情绪对比、留白美学）。
  * `transfer_to_short_video`：将该规律迁移至抖音/视频号分镜与口播时的变形建议。
  * `transfer_to_xiaohongshu`：迁移至小红书首图视觉与图文笔记时的排版/标题建议。
  * `transfer_to_wechat_article`：迁移至公众号长文或全案策略时的叙事结构建议。

---

### 模块 5：知识库自进化飞轮（PaoDing Evolver）

内置后台定时进化引擎，实现知识库的“新陈代谢”与“越用越薄、越用越精”：

1. **模式 B（跨账号趋势雷达扫描）：**
   * 每周自动扫描过去 7 天所有追踪账号入库的增量卡片，按互动率分位数筛选 Top 10% 爆款，对比提炼出正在崛起的视觉风格、高频话术与选题异动，生成《周度爆款趋势雷达报告》。
2. **模式 C（博主数字分身 Map-Reduce 合成）：**
   * 当单个追踪账号累积满 $N$ 篇拆解卡片（或每新增 5 篇），自动触发 Reduce 任务，更新该博主的 `Creator_Profile.md`（含：核心叙事法则、分镜与用色癖好、高频金句结构、禁忌清单），随时可作为 System Prompt 注入给下游 Agent 进行风格复刻。
3. **同义基因蒸馏与分类树动态生长：**
   * 当某个原子标签下卡片满 20 张时，自动触发推理模型（DeepSeek-R1）去重合并，升级为顶层《跨界元规则手册 (`00_Meta_Playbooks`)》。
   * 自动聚类观察池中的高频新兴概念，动态在目录树中长出新的二级分类节点。
4. **实战调用权重反哺（RLHF 优胜劣汰）：**
   * 每条原子知识附带 `utility_score`。被下游 Agent 成功调用并获采纳则加权置顶；时效性网感热梗超 6 个月未调用则自动降权沉底（底层美学与心理学法则标记为 `evergreen` 永久保鲜）。

---

### 模块 6：存储架构与多端赋能网关（Storage & Serving）

#### 6.1 本地 Markdown (`PaoDing_Vault`) + PostgreSQL 双写目录树
```text
PaoDing_Vault/
├── 00_Meta_Playbooks_跨界元规则/       # 自进化蒸馏出的最高优跨平台通用法则
├── 00_Creators_DNA_博主数字分身/       # 持续追踪账号的方法论画像与风格Prompt
├── 01_Trend_Radar_趋势雷达简报/        # 跨账号爆款异动周期报告
├── 02_Atom_Library_原子弹药库/         # 跨平台打通的四大元件抽屉
│   ├── Hooks_黄金前3秒与文案钩子/
│   ├── Visual_Specs_色彩与版式网格/
│   ├── Storyboards_分镜调度与节奏/
│   └── Marketing_SOP_营销战役与符号/
└── 03_Raw_Archive_原始底稿与媒体映射/  # 完整JSON、原视频时间戳与本地无水印媒体路径
```

#### 6.2 核心数据库表结构（PostgreSQL 16 + `pgvector`）
1. `paoding_watched_accounts`：订阅账号表（平台、主页URL、博主ID、最后巡检游标 `last_cursor`、巡检状态、数字分身 `persona_json`）。
2. `paoding_knowledge_nodes`：主内容节点表（节点ID、来源平台、原始URL、本地媒体路径、模态类型、L1摘要、L2结构化解剖 `JSONB`、跨界基因 `JSONB`、效用权重 `utility_score`）。
3. `paoding_atom_elements`：原子元件与向量检索表（元件ID、所属节点ID、元件类型 `hook/visual/storyboard/sop`、标签数组、内容文本、1536维语义向量 `embedding`）。
4. `paoding_evolution_logs`：自进化日志与分类树动态变更记录表。

#### 6.3 暴露给下游 AI Agent 的 FastMCP 工具集 (`paoding-mcp`)
通过 MCP 协议与 REST API 暴露以下 5 个标准武器，供视频工作台、趋势平台、Web/App 及 Claude Code 直接挂载调用：
1. `paoding_query_playbook(domain, target_platform)`：优先调取自进化提纯后的跨界元规则。
2. `paoding_fetch_atoms(atom_type, tags, target_platform, limit)`：**【模式A 创作弹药】** 按风格标签检索色彩配比、Bento 版式、前3秒钩子或分镜模板，并自动附带目标平台的转换建议。
3. `paoding_scan_trends(days, platforms)`：**【模式B 趋势雷达】** 获取近期追踪账号群的爆款趋势异动与高频新套路。
4. `paoding_summon_creator(creator_name)`：**【模式C 数字分身】** 提取指定博主的数字分身 System Prompt 与代表作范例。
5. `paoding_feedback_utility(atom_id, is_adopted)`：下游 Agent 反馈某条知识元件是否被采纳，驱动知识库优胜劣汰。

---

## 四、 跨平台本地部署规范（Mac mini / Windows 兼容）

1. **混合部署模式（Hybrid Local Architecture）：**
   * **中间件容器化：** `PostgreSQL (pgvector)` 与 `Redis` 统一封装在 `docker-compose.yml` 中，在 macOS (Apple Silicon ARM64) 与 Windows (x86_64) 上一键拉起，消除环境配置差异。
   * **主引擎宿主机原生运行：** FastAPI 主服务、采集器与文件监控跑在宿主机原生 Python 环境，以便直接调用 Mac 的 `Apple Silicon MPS/MLX` 或 Windows 的 `NVIDIA CUDA` 硬件加速，并无缝读取本地浏览器 Cookie 与桌面端 WorkBuddy 目录。
2. **跨平台工程铁律：**
   * 代码中严禁硬编码 `/` 或 `\` 路径拼接，100% 强制使用 `pathlib.Path`。
   * 进程常驻统一使用跨平台的 `PM2` 管理（`pm2 start paoding-server`），支持 Mac mini 与 Windows 主机断电重启后自动后台恢复服务。
   * 配合 **Tailscale** 实现异地手机、随身笔记本与家中主机加密直连；配合 **Syncthing** 实现多设备间 `PaoDing_Vault` 知识库文件夹毫秒级同步。

---

## 五、 研发迭代里程碑（MVP 落地排期）

* **Milestone 1：底座与“庖丁三刀”单点解剖闭环（Day 1 - Day 3）**
  * 完成跨平台环境初始化（Docker PG + 本地 `PaoDing_Vault` 目录树生成）与 `DynamicModelRegistry`（打通 DeepSeek、Qwen、豆包、Gemini、Claude 并通过 `paoding models test` 连接测试）。
  * 跑通 `POST /api/v1/ingest` 单链接全流程：丢入一条 Pinterest/小红书链接或抖音/YouTube 视频，自动生成本地 Obsidian `.md` 拆解卡片并落库。
* **Milestone 2：极客投喂流与 `paoding-mcp` 赋能（Day 4 - Day 6）**
  * 配置 Mac 端 Raycast 快捷键脚本、本地 `PaoDing_Drop` 拖拽文件夹监控与手机 Bot 转发接口。
  * 上线 `PaoDing MCP Server`，将知识库挂载至下游视频创作与营销 Agent，跑通“跨平台调取原子元件生成新分镜/新方案”的实战闭环。
* **Milestone 3：账号持续监听与自进化飞轮（Day 7 - Day 10）**
  * 接入账号定时巡检队列（Watcher）与 WorkBuddy 微信生态目录同步。
  * 上线每周自动运行的 `PaoDing Evolver`（Map-Reduce 提炼跨界元规则、更新博主数字分身画像、生成爆款趋势雷达周报）。