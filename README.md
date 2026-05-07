# CS Interview Video Auto-Translate

自动将CS相关的YouTube访谈视频翻译成中文新闻稿的桌面应用程序。

A desktop application that automatically downloads CS-related YouTube interview videos, performs speech-to-text transcription (faster-whisper), speaker diarization (pyannote.audio), translates the content, and generates polished Chinese news articles using LLM APIs.

## 功能特点

-  **YouTube视频下载**: 自动下载视频音频
-  **语音识别**: 使用faster-whisper进行高精度转录
-  **说话人分离**: 自动区分不同说话人
-  **多LLM支持**: 支持OpenAI、Claude、OpenRouter、阿里百炼等
-  **HLTV数据集成**: 自动获取战队和选手信息
-  **新闻稿生成**: 智能翻译并改写成专业新闻稿

## 环境要求

- **Node.js** 20+
- **Python** 3.10+
- **CUDA支持的GPU**（推荐，用于加速语音识别）
- **Git**

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/FriksD/auto-translate.git
cd auto-translate
```

### 2. 安装 Node.js 依赖

```bash
npm install
```

### 3. 安装 Python 依赖

```bash
cd python
pip install -r requirements.txt
cd ..
```

> **Windows 用户注意**：部分 Python 包（如 torch、pyannote.audio）需要 Microsoft Visual C++ 运行时。如安装失败，请参考下方[常见问题](#常见问题)。

### 4. 下载运行时二进制文件

```bash
node scripts/download-binaries.js
```

此脚本会自动下载 `yt-dlp.exe` 到 `resources/bin/`。ffmpeg 需手动下载：

1. 访问 https://www.gyan.dev/ffmpeg/builds/
2. 下载 `ffmpeg-release-essentials.zip`
3. 将 `ffmpeg.exe` 和 `ffprobe.exe` 解压到 `resources/bin/`

### 5. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 文件，填入你的 API 密钥：

| 变量 | 必填 | 说明 |
|---|---|---|
| `BAILIAN_API_KEY` | 推荐 | 阿里百炼 API Key（推荐国内用户使用） |
| `OPENAI_API_KEY` | 否 | OpenAI API Key |
| `ANTHROPIC_API_KEY` | 否 | Anthropic Claude API Key |
| `OPENROUTER_API_KEY` | 否 | OpenRouter API Key |
| `DIARIZATION_MODEL_PATH` | 推荐 | 本地 pyannote 说话人分离模型路径 |
| `HF_TOKEN` | 可选 | HuggingFace Token（仅远程模式需要） |
| `PYTHON_SERVER_PORT` | 否 | Python 服务端口（默认 8000） |

> 至少需要配置一个 LLM 提供商的 API Key。

### 6. 配置说话人分离（推荐本地模型）

支持两种模式：

**本地模型模式（推荐）**：下载模型后不依赖 HuggingFace 在线服务，启动更快、更稳定。

```env
DIARIZATION_MODEL_PATH=C:/models/pyannote/speaker-diarization-3.1
```

**远程模型 ID 模式**：不设置 `DIARIZATION_MODEL_PATH` 时，使用 `DIARIZATION_MODEL_ID`（默认 `pyannote/speaker-diarization-3.1`），通常需要 `HF_TOKEN`。

```env
# DIARIZATION_MODEL_ID=pyannote/speaker-diarization-3.1
# HF_TOKEN=hf_xxx
```

## 运行

### 开发模式

一键启动（前端 + Python 后端）：

```bash
npm run electron:dev
```

或分别启动（两个终端）：

```bash
# 终端 1：前端开发服务器
npm run dev

# 终端 2：Python 服务
cd python && python server.py
```

### 构建生产版本

```bash
npm run build:full
```

此命令会：
1. 使用 PyInstaller 将 Python 后端打包为独立可执行文件
2. 使用 Vite + electron-builder 构建桌面应用

构建产物位于 `release/` 目录，包含：
- `CS Interview Translator-portable-1.0.0.exe` — 便携版
- `CS Interview Translator-setup-1.0.0.exe` — 安装版

## 使用流程

1. **输入视频**: 粘贴YouTube视频链接
2. **自动处理**: 系统自动下载音频、转录、分离说话人
3. **标注说话人**: 为识别出的说话人输入名字
4. **补充信息**: 可选添加HLTV战队/选手信息
5. **生成新闻稿**: LLM自动翻译并生成新闻稿
6. **编辑导出**: 编辑并导出最终稿件

## 配置

在设置页面可以配置：

- **LLM提供商**: OpenAI / Claude / OpenRouter / 阿里百炼 / 自定义
- **Whisper模型**: tiny / small / medium / large-v3
- **输出格式**: Markdown / HTML / 纯文本

## 项目结构

```
├── electron/              # Electron 主进程
│   ├── main.ts           # 入口，窗口管理，IPC
│   ├── preload.ts        # 预加载脚本（contextBridge）
│   └── services/         # 后端桥接服务
│       ├── python-bridge.ts  # Python FastAPI 通信
│       ├── store.ts          # electron-store 封装
│       └── youtube.ts        # yt-dlp 音频下载
├── src/                  # React 前端
│   ├── main.tsx          # React 入口
│   ├── App.tsx           # 路由配置
│   ├── components/       # UI 组件
│   │   ├── layout/       # 布局组件
│   │   ├── project/      # 项目流程组件
│   │   └── ui/           # 基础 UI 组件
│   ├── pages/            # 页面
│   ├── stores/           # Zustand 状态管理
│   └── lib/              # 工具函数
├── python/               # Python 处理服务
│   ├── server.py         # FastAPI 服务入口
│   └── services/         # 核心处理模块
│       ├── whisper_service.py    # faster-whisper 转录
│       ├── diarization.py        # pyannote 说话人分离
│       ├── llm_service.py        # 多LLM翻译
│       └── hltv_scraper.py       # HLTV数据抓取
├── scripts/              # 构建脚本
│   ├── build-backend.js        # PyInstaller 打包
│   └── download-binaries.js    # 下载 yt-dlp/ffmpeg
├── build/                # PyInstaller 配置
│   ├── backend.spec            # PyInstaller spec
│   └── hook-torch-dll.py       # torch DLL 钩子
├── tests/                # 测试文件
└── resources/
    └── bin/              # 运行时二进制文件（需下载）
```

## 技术栈

- **前端**: React + TypeScript + Tailwind CSS
- **桌面**: Electron
- **后端**: Python + FastAPI
- **AI**: faster-whisper + pyannote.audio + OpenAI/Claude/Bailian API

## 常见问题

### Python 依赖安装失败

安装 Microsoft Visual C++ Build Tools：
https://visualstudio.microsoft.com/visual-cpp-build-tools/

### CUDA 显存不足（Out of Memory）

在设置页面将 Whisper 模型切换为 `tiny` 或 `small`，或将计算设备切换为 CPU。

### 端口冲突

修改 `.env` 中的 `PYTHON_SERVER_PORT` 为其他端口（如 `8001`）。

### ffmpeg 未找到

运行 `node scripts/download-binaries.js` 并手动下载 ffmpeg（见[步骤 4](#4-下载运行时二进制文件)）。

### 启动后白屏

确保先执行 `npm run dev` 构建前端，再启动 Electron。或直接使用 `npm run electron:dev` 一键启动。

## 安全说明

- `.env` 文件包含 API 密钥，已通过 `.gitignore` 排除，**切勿提交到仓库**
- `.env.example` 仅包含占位符，可安全提交
- 仓库不含任何真实的 API 密钥或 Token

## 许可证

MIT
