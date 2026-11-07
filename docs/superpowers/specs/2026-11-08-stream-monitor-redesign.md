# 直播间监视重构方案（替代「预约比赛 → 看比分 → 找赛后采访」）

## 背景

现在的直播环节以「一场比赛」为单位：按 HLTV/bo3.gg 比分决定何时录、何时找采访，采访必须是这场比赛的赛后采访。问题：

- 比分有延迟、HLTV 常访问不了、bo3 匹配会错，采访就漏了；
- 赛前采访、一场后连续采访多人、和最近一场无关的采访，在「一场比赛一个采访」的模型里放不下。

目标：以**直播间**为单位持续监视，画面 + 转写由模型判断采访，**每段采访生成一个任务**；受访者、战队、对应哪场比赛都事后识别。

用户已确认：
- 一个赛事一般最多 2 个直播间，链接手动贴（B 站 / YouTube / Twitch / Kick 都行），国内链接不走代理；
- 「比赛」页和飞书「赛程/预约」保留，改成直播间监视的开关入口；
- 摘要里的比分：能确认是哪场比赛才写，否则不写、提示核对。

## 新结构

### 1. 直播间监视（Monitor）

新表 `monitors`：`id, event_id, event_name, title, streams_json, enabled, auto_windows_json, created_at`。

- `streams`：最多 2 条 `{url, label, proxy}`；`proxy` 默认按域名判断（bilibili/huya/douyu/douyin → 不走代理，其余走设置里的代理），可手动改。
- 开关：手动开/关；或由预约产生的时间窗自动开关（见第 5 节）。
- 工作目录 `data/monitors/<id>/<stream_key>/`：`chunks/`、`frames/`、`stills/`、`live_transcript.jsonl`、`shots.jsonl`、`monitor.json`（运行状态、检测状态）。
- 磁盘：16k 单声道约 115MB/小时，超过 `retain_hours`（默认 6）且没被任何采访引用的分段和画面自动删除。

**直播间暂未开播 / 中途下播**（例：B 站 IEM 北京主舞台 `live.bilibili.com/35`、副舞台 `/34`）：

- 不能用「streamlink 拉不拉得到流」判断开播：实测副舞台 `live_status=0` 时 streamlink 仍然给出一路流（推测是轮播或占位画面），会录一整天的假内容。
- 所以每条链接先探测开播状态：B 站用房间接口 `api.live.bilibili.com/room/v1/Room/get_info?room_id=<短号或长号>`，只有 `live_status == 1` 才算开播（0 未开播、2 轮播都不录）；其他平台用 `streamlink --json`，没有可播放的流就是未开播。
- 未开播：每 `offline_poll_seconds`（默认 60）探测一次，不提醒，状态显示「未开播」；开播后自动开始录。
- 录制中：B 站每 `live_probe_seconds`（默认 120）查一次房间状态，下播就停录回到等待；其他平台断流时先探测，确认下播就安静地回到等待，只有「还在播但拉不到」才按录制异常提醒。
- 时间轴：直播间目录里没有保留的分段时（比如隔天开播），从这次开播重新计时，旧的转写、镜头记录一起清掉。

复用：`recorder.py` 的 `Recorder` 原样用，只是 `proxy` 改成按链接传；`live.py` 的转写、拼图识别、截图、`chunk_timeline`/`cut_audio`/`interview_runs`/`make_sheet` 都搬到新的 `StreamSession`（每条链接一个）。

### 2. 两层检测

- **第一层（一直跑）**：画面识别，沿用 `SHOT_SYSTEM`，每 5 秒一帧、9 帧一批 → 每条流约 45 秒一次视觉模型调用（`frame_interval` 可调）。滚动转写也一直跑（GPU 本地，不花钱）。
- **触发**：出现采访镜头段（≥ 2 帧 interview），或转写里出现采访特征词（"thank you for joining", "congratulations on the win", "how does it feel" 等，小表），才进入第二层。
- **第二层（触发后才跑）**：把采访镜头段前后的转写 + 镜头段交给 LLM（新提示词，不再带"这场比赛"的假设），输出采访列表：
  `[{start, end|null, ended, interviewees:[…], team|null, live: bool, note}]`
  - `live=false`：预录专访、集锦 → 忽略；
  - 换人接受采访（主持人叫下一位）→ 拆成两段；两人同台 → 一段。
- **确认截取**：沿用现在的规则——两次判断结束点一致，或镜头已切走；`ENDING_MARGIN`、撤回逻辑保留。去掉所有依赖比赛结束的逻辑（`match_over_at`、`EARLY_START`、`hold`、`team=="other"`）。
- 截取**不停录**，录音继续找下一段；同一时间段不重复截取（按已截区间去重）。
- 采访开始后 `max_interview_minutes` 没判定结束 → 截到当前位置（超时）。

### 3. 采访 → 任务

截取时新建任务：`source_type="live"`，`state=queued`，标题先用「<赛事> 采访 HH:MM <受访者猜测>」；
任务目录写 `interview.wav`、截取区间内的 `stills/`、`live.json = {monitor_id, stream, interview{start,end,wall_start,…}, detection{interviewees, team}}`，
然后从 `context` 阶段开始跑流水线（原来直播任务从 `ingest` 开始）。

### 4. 背景信息改成事后识别（`stage_context` 新分支）

直播采访任务没有 `match_hltv_id` 时：

1. **候选战队**：该赛事在 HLTV `/matches` 列表里的队伍（`list_matches()` 按 `event.id` 过滤，缓存）＋本直播间之前识别出的队伍；队伍阵容按需 `get_team()`（缓存一天）。
2. **LLM 识别**：采访转写（截取片段的滚动转写）＋候选战队阵容 → `{interviewees, team, opponent|null, about_match: "just_played"|"upcoming"|"general"}`。
3. **补背景**：受访者战队（和对手，如有）走现有 `context.build_from_team_names`。
4. **对应比赛**：`about_match=just_played` 时，用 bo3.gg（不走浏览器）找这支队 3 小时内打完的比赛，对手和 LLM 说的对得上才写进 `context.match`（比分、赛制）；否则只写 `event`，摘要不写比分，并在任务里提示「未确认对应比赛，摘要未写比分」。
5. 识别不出战队时：任务照常出稿，名词规范化只用术语表，提示人工在背景信息里补。

`package.match_facts` 改成读 `context.match` 和 `live.json.detection`，不再读 `snapshot`/`match_over_at`。

### 5. 预约 → 监视开关

- 「比赛」页和飞书赛程卡片上的「预约」改成：给这场比赛所属赛事的直播间加一个时间窗（开赛前 15 分钟 → 开赛后 `window_hours`，默认 5 小时；同一赛事多场预约合并时间窗）。
- 这个赛事还没有直播间：自动建一个，链接先填 HLTV 比赛页的官方流，在飞书里提醒「请确认/替换直播链接（国内可换 B 站）」。
- 「取消预约」= 删掉这一段时间窗。
- 预约不再生成任务；任务只来自截到的采访。

### 6. 飞书

- **直播间卡片**（每个直播间一张）：开关状态、链接、录制/断流状态、最近一次画面判断、今天截到的采访列表（链接到各任务）；按钮：开始/停止监视、换链接（表单，可选是否走代理）。断流、画面识别/LLM 连续失败 → 话题里提醒（沿用现有提醒逻辑）。
- **采访任务卡片**：沿用现有卡片，进度改成「采访 → 初稿 → 精校」，去掉比分、检查、比赛已结束、本场无采访等按钮。
- 命令：`赛程`、`预约` 保留（语义按第 5 节）；新增 `直播间`（列出直播间卡片）、`监视 <链接>`（给最近预约的赛事加/换链接）。

### 7. 前端

- 新「直播间」页：列表 + 新建（赛事、1～2 个链接、是否走代理）、开关；详情里是现在 `LivePanel` 的录制状态、滚动转写、镜头时间轴、试听和**手动标注起止 → 生成采访任务**。
- 「比赛」页：预约按钮语义按第 5 节，已预约显示时间窗和对应直播间。
- 任务页：去掉直播标签里的比分/检查/录制控件，只显示采访来自哪个直播间、什么时间。

### 8. 删除

`backend/pipeline/watcher.py`、`backend/pipeline/match_sources.py`、比分相关设置（`sources/intervals/record_at/...`）、HLTV 比分板常开页（`watch_match`）、任务状态 `reserved/watching/recording/interview_detecting/no_interview`（旧库里这些状态的任务迁移成 `cancelled` 并记事件）。`bo3.py` 保留（用于第 4 节查比分）。相关测试改写或删除。

## 实施顺序（每步可单独提交、测试通过）

1. 后端核心：`monitors` 表 + `StreamSession`（从 `live.py` 搬）+ 两层检测 + 截取建任务 + 磁盘清理；单元测试覆盖检测/去重/拆段/截取。
2. `stage_context` 事后识别 + `package.match_facts` 改造 + 测试。
3. API + 「直播间」页 + 任务页调整。
4. 飞书直播间卡片、任务卡片精简、命令。
5. 预约 → 时间窗；删除 watcher/比分源/旧状态与迁移；README。

## 验证

- `python -m pytest -q tests` 全部通过。
- 用一段已录好的直播（或 B 站回放链接当成直播源跑，关掉回放检测）端到端跑：确认一次会截出多段采访、各自生成任务、受访者和战队识别正确、摘要比分按规则写/不写。
- 真实直播：B 站链接（不走代理）+ YouTube 链接（走代理）各监视一段，确认录制、画面识别调用频率和磁盘清理。
