# Auto-Translate-to-News

CS2 赛后采访自动翻译的本地服务：音频 → Whisper 转写 → LLM 两段式翻译润色 → 名词规范化 → Word 初稿 → 飞书发送，人工精校出终稿。

设计文档：[docs/superpowers/specs/2026-10-29-live-interview-pipeline-design.md](docs/superpowers/specs/2026-10-29-live-interview-pipeline-design.md)

当前完成了**第一、二阶段**：
- 从 HLTV 浏览未来 5 天的比赛并预约，自动抓取比赛当时的阵容、BP、比分、近期战绩、图池、新闻。
- 手动提供采访音频（上传文件或视频链接），自动跑完后处理全流程。

比赛监视、直播录制、采访自动定位会在后续阶段加入。

## 环境要求

- Python 3.10+
- Node.js 20+（只用于构建前端）
- ffmpeg（在 PATH 中，或在设置页填写路径）
- Google Chrome（访问 HLTV 用；通过 Playwright 驱动本机 Chrome，不需要另外下载浏览器）
- NVIDIA GPU（推荐）。faster-whisper 使用 CTranslate2 自带的 CUDA 支持，需要 CUDA 12 的 cuBLAS 和 cuDNN 9；不可用时会自动退回 CPU。

## 安装

```bash
pip install -r requirements.txt
```

```bash
npm install
```

```bash
npm run build
```

Whisper 模型：把 faster-whisper 格式的模型放在 `models/whisper/`（含 `model.bin`），或在设置页直接填写模型名（如 `large-v3`、`large-v3-turbo`），首次运行会自动下载。

说话人分离模型（推荐本地）：把 pyannote `speaker-diarization-3.1` 的 `config.yaml` 以及 `segmentation-3.0`、`wespeaker-voxceleb-resnet34-LM` 两个 `pytorch_model.bin` 放进 `models/pyannote/speaker-diarization-3.1/`，并把 `config.yaml` 里的两个模型改成本地文件的绝对路径，然后在设置页填写该目录。

## 启动

```bash
python -m backend
```

浏览器打开 http://127.0.0.1:8000 。

开发前端时可以另开一个终端运行 `npm run dev`（http://localhost:5173 ，API 自动转发到 8000 端口）。

## 首次配置（设置页）

1. **代理**：默认 `http://127.0.0.1:7897`，HLTV、视频下载、LLM 共用；飞书始终直连。
   访问 HLTV 时会弹出一个 Chrome 窗口（空闲 10 分钟自动关闭）。偶尔遇到需要人工点击的 Cloudflare 验证，会发飞书提醒，在窗口里点一下即可。
2. **LLM**：添加自定义供应商（OpenAI 兼容或 Anthropic 兼容，填写 Base URL、API Key、模型），点「测试连接」确认可用，然后为「英文清洗」和「中文成稿」两步分别选择供应商。
3. **飞书**（可选）：填写自建应用的 App ID / App Secret，选择发送到群聊（chat_id）或个人（open_id）。应用需要开通机器人能力和 `im:message`、`im:resource` 权限，并被拉进目标群。

## 使用

1. 两种方式创建任务：
   - 「比赛」页找到要翻译的比赛，点「预约」。会立即抓取背景信息；比赛结束后在任务的「任务信息」里上传采访音频。
   - 「任务」页新建任务：填 HLTV 比赛链接（推荐，阵容以比赛当时为准）或两支队伍名，选择视频链接或上传本地文件；视频较长时可以填写截取的起止时间。
2. 流水线自动执行：抓取背景信息 → 获取音频 → 语音转写 → 英文清洗 → 中文成稿 → 生成 Word → 飞书发送。
3. 在任务详情页查看和修改每一步的结果：
   - **中文稿**：修改后点「保存并重新生成 Word」。
   - **英文清洗稿**：修改后点「保存并重新翻译」。
   - **背景信息**：队名、阵容、教练就是稿件的规范写法，抓取失败或不全时在这里补；也能看到排名、近期战绩、图池和新闻。
   - **名词报告**：自动替换了哪些名字，哪些需要人工核对。
   - 任意阶段失败后，可以选择从该阶段重跑。
4. 下载 Word（或在飞书中接收），精校后点「标记完成」。

### 稿件格式

- `Q：` 开头的主持人问题整段加粗；`A：` 开头的选手回答不加粗。
- 全文小四（12pt），A4，字体为 Word 默认。

### 专有名词

选手名、战队名必须与 HLTV 完全一致（如 `m0NESY`、`ZywOo`），通过三层保证：

1. 阵容写入 Whisper 提示词，提高识别率。
2. LLM 两步都附带规范名表。
3. 成稿后逐级替换：拼写变体（`monesy`、`Mo Nesy`、`Zywo0`）直接归一；发音相近的（`Monessy`、`Manasi`）按发音推断；剩下的结合上下文交给 LLM 判断（如 `Shearer` → `sh1ro`）。推断出的替换在名词报告里标黄，推断不出的列入待核对。

「术语表」页可以添加别名（如 `NAVI → Natus Vincere`、`小蜜蜂 → Vitality`）和术语的固定译法。

## 数据

所有数据都在 `data/` 目录下（可用环境变量 `ATN_DATA_DIR` 修改）：

- `app.db`：任务、日志、术语表、设置
- `tasks/<id>/`：每个任务的音频、转写、各步中间稿、提示词和 Word 文件
- `browser-profile/`：访问 HLTV 用的 Chrome 配置（保存 Cloudflare 验证状态）

## 测试

```bash
python -m unittest discover -s tests
```
