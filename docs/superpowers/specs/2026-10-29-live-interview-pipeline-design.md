# CS2 赛后采访全自动翻译流水线 — 设计文档

日期：2026-10-29
状态：第一、二阶段已实现

## 1. 目标

把现有 Electron 桌面应用（手动输入 YouTube 链接 → 转录 → 翻译）改造成一个**本地常驻服务**：

1. 拉取 HLTV 未来 5 天的比赛列表，人工勾选需要翻译的比赛（接单制，不做自动筛选）。
2. 预约时自动抓取比赛背景信息（队伍、阵容、选手、近期战绩、图池、相关新闻）。
3. 比赛进行中按比分自适应轮询 HLTV，接近结束时开始录制官方直播流的音频。
4. 边录边转录，自动定位赛后采访片段，采访结束后截取。
5. LLM 结合背景信息与术语表，产出中文翻译润色初稿。
6. 通过飞书通知人工（比赛结束、采访截取完成、初稿就绪），初稿以 Word 文件发送，人工精校得到终稿。

核心诉求是**即时性**：比赛结束后尽快出稿，因此只使用直播流，不依赖赛后上传的采访视频。

非目标：安全性、多用户、云端部署、自动筛选比赛。

## 2. 总体架构

```
┌──────────────────────── 本地服务 (FastAPI, 单进程) ────────────────────────┐
│                                                                           │
│  Web UI (React 静态资源)  ←→  REST API  ←→  Task 状态机 (SQLite 持久化)        │
│                                                │                          │
│        ┌───────────────┬───────────────┬───────┴───────┬──────────────┐   │
│        ▼               ▼               ▼               ▼              ▼   │
│   HLTV 浏览器层     比赛监视器        录音器          转录/切片        LLM 写稿  │
│   (Playwright)    (自适应轮询)   (streamlink+ffmpeg)  (faster-whisper)  (两段式) │
│        │                                                              │   │
│        └──── 实体缓存 / 术语表 (SQLite) ─────────────────────────────────┘   │
│                                                                           │
│  飞书适配器 (lark-oapi, 长连接) ── 群通知 / 交互卡片 / 文件发送                     │
└───────────────────────────────────────────────────────────────────────────┘
```

- **进程模型**：一个 Python 进程，FastAPI + APScheduler（AsyncIOScheduler）。CPU/GPU 密集的转录放线程池；录音是 ffmpeg/streamlink 子进程。
- **去掉 Electron**：前端保留 React + Vite，构建产物由 FastAPI 以静态文件托管，浏览器访问 `http://localhost:8000`。
- **存储**：SQLite 存元数据；音频、转录、稿件等大文件存 `data/tasks/<task_id>/` 目录。

## 3. 任务状态机

每个预约的比赛对应一个 Task。任何状态都允许人工**重试 / 跳过 / 注入**（上传音频、填写直播 URL、手动标注采访起止、直接粘贴转录文本），这是"人工兜底"在工程上的落点。

```
RESERVED ──(开赛前)──▶ WATCHING ──(决胜图接近结束)──▶ RECORDING
                                                        │
          ┌─────────────────────────────────────────────┘
          ▼
   (比赛结束 → 飞书通知)  ──▶  INTERVIEW_DETECTING ──▶ INTERVIEW_CAPTURED
                                     │ 超时/未发现                │
                                     ▼                          ▼
                                NO_INTERVIEW               PROCESSING ──▶ REVIEW ──▶ DONE
                                                                   (发 Word)   (人工确认终稿)
任意状态 ──▶ FAILED（记录原因，可从失败步骤重试）
任意状态 ──▶ CANCELLED
```

手动创建的任务（上传音频或填写视频链接）从 DRAFT 直接进入 PROCESSING。PROCESSING 内部按阶段（stage）串行执行：
`context`（背景信息）→ `ingest`（获取音频）→ `transcribe`（转写）→ `clean`（英文清洗）→ `write`（中文成稿 + 名词规范化）→ `export`（Word）→ `deliver`（飞书发送）。
每个阶段只读写任务目录里的文件，因此可以从任意阶段重跑，也可以只跑某一段（如只重新抓取背景信息）。

`task_events` 表记录每次状态迁移和日志，Web UI 与飞书卡片均以此为数据源。

## 4. 模块设计

### 4.1 HLTV 浏览器层（解决 Cloudflare）——已实现

旧版用 curl-cffi 伪造 TLS 指纹，挂代理时高概率被 Cloudflare 拦截。

**决定：用 Playwright 驱动真实 Chrome，不用 LLM Agent。**
Cloudflare 拦截看的是浏览器指纹和行为，不是"会不会看网页"。LLM Agent 解决不了指纹问题，还会带来延迟、费用和不确定性。确定性的浏览器自动化加人工兜底就够了。

实现（`backend/sources/browser.py`）：
- `playwright` + 本机 Chrome（`channel="chrome"`），默认**有头模式**，**持久化用户目录**（`data/browser-profile/`），通过代理启动。`cf_clearance` 等 cookie 持久保存。
- 所有浏览器操作在一个专用线程中串行执行，两次访问至少间隔 3 秒加随机抖动。空闲 10 分钟自动关闭浏览器。
- 遇到验证页先自动等待 20 秒；仍未通过就发飞书通知"HLTV 需要人工验证"，在弹出的 Chrome 窗口里点一下即可，最多再等 5 分钟。
- 解析是纯函数（`backend/sources/hltv_parse.py`），测试样本裁剪自真实页面（`tests/fixtures/hltv/`）。HLTV 改版时先看这里。

实测（2026-10）：`/matches`、比赛页、战队页、搜索页都能直接通过；`/stats/...` 统计页的验证更严，需要人工点击。因此**图池数据改从战队页的「Map win statistics past 3 months」读取**，不访问统计页。

对外接口（`HLTVClient`，带内存缓存）：
- `list_matches()`：比赛列表（id、时间、星级、双方、赛事、阶段、BO 数、是否进行中）
- `get_match(id)`：比赛页（当时的阵容含真名/国籍/替补、排名、BP、各图比分、双方近期战绩、交手记录、直播源）
- `get_team(id)`：战队页（教练、当前阵容、排名、近 3 个月图池、最近 5 场、相关新闻）
- `search_team(name)`：按队名找战队 ID（手动任务未关联比赛时使用）

Liquipedia 备用数据源暂未实现：实测 HLTV 浏览器层足够稳定，确有需要时再加。

### 4.2 比赛列表与预约——已实现

- Web UI「比赛」页显示 HLTV 未来 5 天的比赛，按日期分组，可按队名/赛事搜索、按星级筛选。列表缓存 10 分钟，可手动刷新。
- 点「预约」创建 Task（状态 RESERVED，标题自动生成），立即抓取背景信息（只跑 `context` 阶段，完成后回到 RESERVED）。
- 手动任务也可以填 HLTV 比赛链接，按这场比赛抓取背景信息。
- 第三、四阶段完成前，预约的任务在比赛结束后由人工上传采访音频，上传后自动走完后处理。

### 4.3 背景信息抓取与知识库——已实现

以**比赛页**为主，**战队页**补充：

| 内容 | 来源 | 用途 |
|---|---|---|
| 比赛当时的阵容、真名、国籍、替补 | 比赛页 | ASR 提示词、LLM 名单、名词规范化 |
| 赛事、阶段、BO、时间、BP、各图比分、交手记录 | 比赛页 | 稿件背景 |
| 双方近期战绩（含对手队名） | 比赛页 | 稿件背景；对手队名也加入规范名单 |
| 教练（含全名） | 战队页 | 规范名单 |
| 图池（近 3 个月胜率、选图/禁图率） | 战队页 | 稿件背景 |
| 相关新闻标题（近 14 天） | 战队页 | 稿件背景 |

战队页是"当前"数据：只有比赛发生在最近 30 天内才使用；更早的比赛只用比赛页（阵容以比赛当时为准），并在日志中提示。

每个字段独立抓取、独立失败。失败字段在 UI 和飞书卡片中标红，可人工补填，不阻塞主流程。

**知识库**（初期不做 RAG）：
- `glossary` 表：CS 术语固定译法、选手和队伍的中文社区惯用称呼，Web UI 可编辑。
- `style_examples`：2～3 篇历史终稿，作为 few-shot 风格示例。
- 每篇终稿与初稿的 diff 会存档，用于后续人工迭代术语表和 prompt。

### 4.4 比赛监视器（自适应轮询）

开赛前 15 分钟进入 WATCHING。核心是判断**决胜图**：任一方再赢下当前这张图即可赢下整场（BO1 的唯一一张图；BO3 中某方已赢 1 张图；BO5 中某方已赢 2 张图）。

轮询间隔（可配置）：

| 状态 | 间隔 |
|---|---|
| 未开赛 / 非决胜图 | 10 分钟 |
| 决胜图，领先方回合数 < 8 | 10 分钟 |
| 决胜图，领先方回合数 8～10 | 5 分钟 |
| 决胜图，领先方回合数 ≥ 11，或进入加时 | 2 分钟 |
| 已开始录制后 | 1 分钟（只为尽快确认结束） |

- **开始录制的条件**：决胜图领先方回合数 ≥ 10（阈值可配置）。HLTV 比分通常领先直播画面 1～3 分钟，这样留有足够余量。
- **比赛结束**：HLTV 状态变为 finished 时，发飞书通知"比赛结束，采访监听中"，状态进入 INTERVIEW_DETECTING，录音继续。
- 优化项（实现时验证）：HLTV 比赛页带实时比分推送。如果可行，就在浏览器里常开该比赛页并直接读取 DOM，不必重复请求，能进一步降低 Cloudflare 风险；不可行时退回上表的整页刷新。

### 4.5 录音器

- 用 `streamlink`（Twitch/YouTube 直播稳定，Twitch 加 `--twitch-disable-ads`）把流输出到 stdout，再由 `ffmpeg` 转成 16kHz 单声道，按 **60 秒分段**写入 `data/tasks/<id>/chunks/NNNN.wav`。`yt-dlp` 作为备选拉流工具。
- 断流时自动重连，最多重试 N 次；仍失败就发飞书通知，可在 UI 中换一个流 URL 继续录。
- 停止条件：采访截取完成；或比赛结束后超过 `max_post_match_minutes`（默认 30 分钟）；或人工停止。

### 4.6 滚动转录与采访定位

为了即时性，**边录边转**，不等录音结束：

1. 每生成一个 60 秒分段，就用 faster-whisper 转录（复用 `whisper_service.py`）。`initial_prompt` 中注入本场双方选手 ID、队名、教练名、地图名，提升专有名词识别率。语言自动检测（处理俄语、中文、带翻译的采访）。
2. 比赛结束后，每新增约 2 分钟转录，就把最近 15 分钟的带时间戳文本发给 LLM，判断：采访是否已开始、起点在哪、是否已结束、终点在哪。LLM 输出 JSON：`{started, start_ts, ended, end_ts, confidence}`。
3. 检测到采访开始时，发飞书卡片"检测到采访开始（选手 X）"；检测到采访结束时，按起止时间加前后各 10 秒余量截取并拼接音频，状态进入 INTERVIEW_CAPTURED，同时停止录音。
4. 飞书卡片和 Web UI 都提供"手动标注起止"和"本场无采访"按钮，可随时覆盖自动判断。
5. 对截出的采访片段，用 large 模型重新转录一遍以提高精度，再做 pyannote 说话人分离（片段短，耗时可控）。说话人按"主持人 / 选手 / 翻译"标注，由 LLM 根据内容推断。

### 4.7 LLM 写稿（两段式）

复用 `llm_service.py` 的多供应商调用层，把 prompt 拆成两步：

1. **英文清洗**：输入转录、说话人、选手名单、术语表；输出规范化的问答稿，修正拼错的选手名和术语、去掉口头禅、标注听不清的部分 `[?]`。
2. **中文成稿**：输入清洗后的问答稿、比赛背景（比分、图池、近期战绩、新闻）、风格示例、交稿要求；输出中文新闻稿（标题 + 导语 + 问答正文）。

两步的中间结果都保存，人工可以从任一步重新生成。

### 4.8 飞书适配器

使用飞书**自建应用** + `lark-oapi` Python SDK，事件和卡片回调走**长连接模式**，本地服务不需要公网 IP。

通知（发到指定群）：

| 时机 | 内容 | 交互 |
|---|---|---|
| 预约成功 | 比赛信息、背景信息抓取结果（失败项标红） | 打开 Web UI |
| 开始录制 | 比分、直播源 | 停止 / 换流 |
| **比赛结束** | 最终比分，提醒人工关注 | 标记无采访 / 手动停止 |
| 检测到采访开始 / 结束 | 起止时间、前几句转录 | 确认 / 手动修正起止 |
| 初稿就绪 | Word 文件 + 待核对名词列表 | 标记完成 |
| 异常 | 失败步骤、原因 | 重试 / 跳过 |
| HLTV 需人工验证 | — | — |

初稿以 Word 文件（见 4.9）的形式通过飞书消息直接发送到群聊或个人，人工下载后在 Word 里精校。终稿可以在 Web UI 上传回来存档，并和初稿做 diff（可选）。

### 4.9 Word 稿件格式

用 `python-docx` 生成，规则如下：

- 正文为问答体。主持人提问为一个段落：`Q：<问题>`，**整段加粗**。
- 选手回答为一个段落：`A：<回答>`，不加粗。
- 全文字号统一为小四（12pt），字体使用 Word 默认字体，除 Q 段落外任何地方都不加粗。
- 如果 LLM 输出了标题或导语，作为普通段落放在问答之前，同样不加粗。
- 文件名：`<日期>_<队伍1>_vs_<队伍2>_<选手>.docx`。

LLM 第二步输出约定为纯文本行（`Q：...` / `A：...` / 普通段落），由程序转换成 docx，不让 LLM 直接控制格式。

### 4.10 专有名词规范化（选手名、战队名与 HLTV 一致）

这是硬性要求，靠 prompt 不够，用"词典 + 确定性替换 + 校验"三层保证：

1. **规范词典**：预约时从 HLTV 抓取的双方阵容、教练、队名，以 HLTV 页面上的写法作为规范形式（如 `m0NESY`、`ZywOo`、`Natus Vincere`）。每个规范名自动生成一个"匹配键"：转小写，再把 leet 字符归一（`0→o`、`1→i`、`3→e`、`4→a`、`5→s`、`7→t`、`$→s`），去掉空格和符号。例如 `m0NESY`、`monesy`、`Mo Nesy` 的匹配键都是 `monesy`。术语表中可人工添加别名（如 `NAVI → Natus Vincere`、中文音译 → 规范名）。
2. **三处使用**：
   - ASR：规范名写入 Whisper `initial_prompt`。
   - LLM：两步 prompt 都附带规范名单，并要求严格使用。
   - 后处理：对 LLM 每一步的输出做确定性替换。扫描所有拉丁字母/数字组成的词（含相邻的 1～3 个词组合），匹配键命中词典就替换为规范形式。
3. **校验**：替换后再扫一遍，对编辑距离接近规范名但未命中的词（可能是识别错误的变体）在 Web UI 和飞书通知中列出，提醒人工核对。

### 4.11 Web UI（调试与设置）

只做必要页面：
- **比赛列表**：未来 5 天比赛，一键预约。
- **任务列表 / 详情**：状态时间线、日志、实时比分、录音分段、滚动转录、采访起止标注、背景信息（可编辑）、各步重试和注入入口、稿件预览。
- **设置**：代理、LLM 供应商和模型、Whisper 模型、飞书凭据和群 ID、轮询阈值、术语表、风格示例。

## 5. 数据模型（SQLite）

```
matches(id, hltv_id, start_time_utc, team1, team2, event, best_of, streams_json, fetched_at)
tasks(id, match_hltv_id, state, stream_url, notes, context_json, interview_start, interview_end,
      feishu_doc_url, created_at, updated_at)
task_events(id, task_id, ts, from_state, to_state, level, message, payload_json)
artifacts(id, task_id, kind, path, created_at)   -- chunk/transcript/interview_audio/clean_en/draft_zh/final_zh
entities(id, kind, name, aliases_json, data_json, source, fetched_at)  -- team/player 缓存
glossary(id, term, translation, note)
settings(key, value_json)
```

时间统一以 UTC 存储，界面和飞书通知按 Asia/Shanghai 显示。

## 6. 目录结构

```
backend/
  __main__.py            # python -m backend 启动
  app.py                 # FastAPI 入口、REST 路由、静态资源托管
  config.py              # 路径常量、默认设置
  db.py                  # SQLite：任务、事件、术语表、设置
  runner.py              # 任务状态机与流水线执行器
  llm.py                 # 自定义 LLM 供应商调用（OpenAI / Anthropic 兼容）
  sources/browser.py     # Playwright 驱动的 Chrome，处理 Cloudflare
  sources/hltv.py        # HLTV 客户端（缓存）
  sources/hltv_parse.py  # HLTV 页面解析
  pipeline/context.py    # 从 HLTV 组装任务背景信息
  pipeline/stages.py     # 后处理各阶段实现
  pipeline/whisper.py    # faster-whisper 封装
  pipeline/diarization.py
  pipeline/prompts.py    # 两段式写稿提示词
  pipeline/names.py      # 专有名词规范化
  pipeline/docx_export.py
  integrations/feishu.py
  # 后续阶段新增：pipeline/watcher.py、recorder.py、segmenter.py
src/                     # React 前端（npm run build 输出到 dist/，由后端托管）
data/                    # 运行时数据（gitignore）
models/                  # 本地 Whisper 模型（gitignore）
```

## 7. 分阶段实施

按"先打通后处理，再做前置自动化"的顺序推进，每个阶段都能独立使用：

1. ✅ **骨架 + 后处理链路**：新目录结构、SQLite、状态机、FastAPI 托管前端；手动上传音频或输入 URL → 转录 → 两段式 LLM → 名词规范化 → Word → 飞书发送。可以用历史采访录像离线测试。
2. ✅ **HLTV 浏览器层 + 比赛列表 + 预约 + 背景信息抓取**，以及术语表（预置常见战队简称）。
3. **比赛监视器 + 飞书通知**（含比赛结束通知、卡片交互）。
4. **直播录音 + 滚动转录 + 采访定位**：自动化程度最高、最需要实战调参的部分，要在真实比赛中迭代。
5. 打磨：UI 细节、diff 存档、prompt 迭代。

## 8. 已确认事项（2026-10-29）

1. **硬件**：测试机为 RTX 3060 Ti 8GB，跑通后迁移到 RTX 5070。faster-whisper 使用 `float16`/`int8_float16`：滚动转录默认 `large-v3-turbo`，采访片段精转默认 `large-v3`。模型、计算精度可在设置中切换。Whisper 与 pyannote 串行占用 GPU，避免 8GB 显存溢出。
2. **LLM**：不设默认供应商。设置页提供"自定义供应商"列表：每项含名称、协议类型（OpenAI 兼容 / Anthropic 兼容）、Base URL、API Key、模型名，可添加多个；两段式写稿的每一步可以分别指定使用哪个供应商和模型。
3. **代理**：HLTV、直播拉流、LLM 请求统一走同一个代理，默认 `http://127.0.0.1:7897`，可在设置中修改或关闭。
4. **飞书**：使用自建应用，通知目标可配置为群聊（chat_id）或个人（open_id）。
5. **稿件格式**：产出 Word 文档（.docx），见 4.9。
6. **专有名词**：选手名、战队名必须与 HLTV 完全一致（大小写、数字都一样，如 `m0NESY`），见 4.10。
