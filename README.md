# CS访谈视频自动翻译新闻稿系统

自动将CS相关的YouTube访谈视频翻译成中文新闻稿的桌面应用程序。

## 功能特点

- 🎥 **YouTube视频下载**: 自动下载视频音频
- 🎤 **语音识别**: 使用faster-whisper进行高精度转录
- 👥 **说话人分离**: 自动区分不同说话人
- 🌐 **多LLM支持**: 支持OpenAI、Claude、OpenRouter等
- 📊 **HLTV数据集成**: 自动获取战队和选手信息
- 📝 **新闻稿生成**: 智能翻译并改写成专业新闻稿

## 系统要求

- Node.js 20+
- Python 3.10+
- CUDA支持的GPU (推荐，用于加速语音识别)

## 安装

### 1. 安装Node.js依赖

```bash
npm install
```

### 2. 安装Python依赖

```bash
cd python
pip install -r requirements.txt
```

### 3. 配置环境变量

复制 `.env.example` 为 `.env` 并填入你的API密钥：

```bash
cp .env.example .env
```

### 4. 配置说话人分离（本地优先）

默认支持两种模式：

1. **本地模型模式（推荐）**：设置 `DIARIZATION_MODEL_PATH` 为本地模型目录，运行时不依赖 HuggingFace 在线服务
2. **模型 ID 模式**：不设置 `DIARIZATION_MODEL_PATH` 时，使用 `DIARIZATION_MODEL_ID`（默认 `pyannote/speaker-diarization-3.1`），此时通常需要 `HF_TOKEN`

示例：

```env
DIARIZATION_MODEL_PATH=C:/models/pyannote/speaker-diarization-3.1
# DIARIZATION_MODEL_ID=pyannote/speaker-diarization-3.1
# HF_TOKEN=hf_xxx
```

## 运行

### 开发模式

```bash
# 启动前端开发服务器
npm run dev

# 在另一个终端启动Python服务
cd python && python server.py

# 或者同时启动
npm run electron:dev
```

### 构建生产版本

```bash
npm run build
```

## 使用流程

1. **输入视频**: 粘贴YouTube视频链接
2. **自动处理**: 系统自动下载音频、转录、分离说话人
3. **标注说话人**: 为识别出的说话人输入名字
4. **补充信息**: 可选添加HLTV战队/选手信息
5. **生成新闻稿**: LLM自动翻译并生成新闻稿
6. **编辑导出**: 编辑并导出最终稿件

## 配置

在设置页面可以配置：

- **LLM提供商**: OpenAI / Claude / OpenRouter / 本地模型
- **Whisper模型**: tiny / small / medium / large-v3
- **输出格式**: Markdown / HTML / 纯文本

## 项目结构

```
├── electron/           # Electron主进程
│   ├── main.ts        # 入口
│   ├── preload.ts     # 预加载脚本
│   └── services/      # 后端服务
├── src/               # React前端
│   ├── components/    # UI组件
│   ├── pages/         # 页面
│   └── stores/        # 状态管理
└── python/            # Python处理服务
    ├── server.py      # FastAPI服务
    └── services/      # 核心处理模块
```

## 技术栈

- **前端**: React + TypeScript + Tailwind CSS
- **桌面**: Electron
- **后端**: Python + FastAPI
- **AI**: faster-whisper + pyannote.audio + OpenAI/Claude API

## 许可证

MIT
