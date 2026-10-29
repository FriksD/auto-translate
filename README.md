# Auto-Translate-to-News

CS2 赛后采访自动翻译的本地服务：音频 → Whisper 转写 → LLM 两段式翻译润色 → 名词规范化 → Word 初稿 → 飞书发送，人工精校出终稿。

设计文档：[docs/superpowers/specs/2026-10-29-live-interview-pipeline-design.md](docs/superpowers/specs/2026-10-29-live-interview-pipeline-design.md)

当前是**第一阶段**：手动提供音频（上传文件或视频链接），自动跑完后处理全流程。HLTV 比赛列表、预约、直播录制、采访自动定位会在后续阶段加入。

## 环境要求

- Python 3.10+
- Node.js 20+（只用于构建前端）
- ffmpeg（在 PATH 中，或在设置页填写路径）
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

## 启动

```bash
python -m backend
```

浏览器打开 http://127.0.0.1:8000 。

开发前端时可以另开一个终端运行 `npm run dev`（http://localhost:5173 ，API 自动转发到 8000 端口）。

## 首次配置（设置页）

1. **代理**：默认 `http://127.0.0.1:7897`，HLTV、视频下载、LLM 共用；飞书始终直连。
2. **LLM**：添加自定义供应商（OpenAI 兼容或 Anthropic 兼容，填写 Base URL、API Key、模型），点「测试连接」确认可用，然后为「英文清洗」和「中文成稿」两步分别选择供应商。
3. **飞书**（可选）：填写自建应用的 App ID / App Secret，选择发送到群聊（chat_id）或个人（open_id）。应用需要开通机器人能力和 `im:message`、`im:resource` 权限，并被拉进目标群。

## 使用

1. 「任务」页新建任务：填写两支队伍（用于从 HLTV 抓取阵容），选择视频链接或上传本地文件；视频较长时可以填写截取的起止时间。
2. 流水线自动执行：抓取背景信息 → 获取音频 → 语音转写 → 英文清洗 → 中文成稿 → 生成 Word → 飞书发送。
3. 在任务详情页查看和修改每一步的结果：
   - **中文稿**：修改后点「保存并重新生成 Word」。
   - **英文清洗稿**：修改后点「保存并重新翻译」。
   - **背景信息**：队名、阵容、教练就是稿件的规范写法，抓取失败或不全时在这里补。
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
3. 成稿后确定性替换：大小写、leet 数字、空格或符号不同的写法（`monesy`、`Mo Nesy`、`Zywo0`）都会被替换成规范写法；拼写相近但无法确定的会列入待核对。

「术语表」页可以添加别名（如 `NAVI → Natus Vincere`、`小蜜蜂 → Vitality`）和术语的固定译法。

## 数据

所有数据都在 `data/` 目录下（可用环境变量 `ATN_DATA_DIR` 修改）：

- `app.db`：任务、日志、术语表、设置
- `tasks/<id>/`：每个任务的音频、转写、各步中间稿、提示词和 Word 文件

## 测试

```bash
python -m unittest discover -s tests
```
