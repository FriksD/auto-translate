"""
路径常量与运行时设置。

设置保存在 SQLite 的 settings 表中（见 db.py），这里只定义默认值和合并逻辑。
"""
import copy
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("ATN_DATA_DIR", PROJECT_ROOT / "data"))
TASKS_DIR = DATA_DIR / "tasks"
DB_PATH = DATA_DIR / "app.db"
WEB_DIST_DIR = PROJECT_ROOT / "dist"
LOCAL_WHISPER_MODEL = PROJECT_ROOT / "models" / "whisper"
BUNDLED_BIN_DIR = PROJECT_ROOT / "resources" / "bin"
# 飞书通知里附带的任务详情链接
PUBLIC_URL = os.environ.get("ATN_PUBLIC_URL", f"http://127.0.0.1:{os.environ.get('ATN_PORT', '8000')}")


def _default_whisper_model() -> str:
    if (LOCAL_WHISPER_MODEL / "model.bin").exists():
        return str(LOCAL_WHISPER_MODEL)
    return "large-v3"


DEFAULT_SETTINGS: Dict[str, Any] = {
    # HLTV、拉流、LLM 统一使用的代理；留空表示直连
    "proxy": "http://127.0.0.1:7897",
    "ffmpeg_path": "",
    "hltv": {
        # 有头模式才能在 Cloudflare 需要人工验证时手动点击；无头模式更安静但更容易被拦
        "headless": False,
    },
    "whisper": {
        "model": _default_whisper_model(),
        "device": "cuda",
        # int8_float16 显存占用少一半（约 3GB），速度和转写结果与 float16 基本一样
        "compute_type": "int8_float16",
        "language": "",  # 留空自动检测
        "beam_size": 5,
    },
    "diarization": {
        "enabled": False,
        "model_path": "",
        "hf_token": "",
    },
    "llm": {
        # 每项：{id, name, protocol: "openai"|"anthropic"|"gemini", base_url, api_key, model, use_proxy}
        "providers": [],
        "clean": {"provider_id": "", "model": ""},
        "write": {"provider_id": "", "model": ""},
        # 直播采访定位：每 30 秒调用一次，选响应快的模型；不选则用 clean 的设置
        "detect": {"provider_id": "", "model": ""},
        "temperature": 0.3,
        "extra_instructions": "",
    },
    "live": {
        # 比分数据源的尝试顺序，前一个失败自动用下一个：bo3（bo3.gg 公开接口）、hltv（浏览器访问 HLTV）
        "sources": ["bo3", "hltv"],
        # 开赛前多少分钟开始监视
        "pre_match_minutes": 15,
        # 轮询间隔（秒）：非决胜图 / 决胜图领先方 < mid_at；领先方达到 mid_at；达到 late_at 或加时；录制中
        "intervals": {"idle": 600, "mid": 300, "late": 120, "recording": 60},
        "mid_at": 8,
        "late_at": 11,
        # 决胜图领先方回合数达到多少开始录制（HLTV 比分通常比直播画面快 1～3 分钟）；
        "record_at": 10,
        # 决胜图没有回合比分时（bo3.gg 普通比赛，且 HLTV 比分板也读不到）：决胜图开打多少分钟后开始录制。
        # 一张图最快（13:0）也要 20 分钟以上，采访在比赛结束后才开始
        "no_score_record_minutes": 15,
        # 录音分段时长（秒）：越短，采访结束后越快被发现
        "chunk_seconds": 30,
        # 每隔多少秒存一帧画面，交给采访定位那一步的模型按镜头判断采访起止；0 = 只录音
        "frame_interval": 5,
        # 录制期间每隔多少秒让 LLM 判断一次采访起止（有新转写时才判断）
        "detect_interval": 30,
        # 比赛结束后多久还没发现采访就提醒（分钟）
        "alert_no_interview_minutes": 10,
        # 比赛结束后多久仍未发现采访就判定为无采访（分钟）
        "max_post_match_minutes": 30,
        # 采访开始后多久仍未判定结束就直接截取（分钟）
        "max_interview_minutes": 10,
        # 截取采访时前后各留的余量（秒）
        "padding_seconds": 10,
        # 采访结束后多久还没出初稿就提醒（分钟）
        "draft_deadline_minutes": 15,
    },
    "feishu": {
        "enabled": False,
        "app_id": "",
        "app_secret": "",
        "receive_id_type": "chat_id",  # chat_id 或 open_id
        "receive_id": "",
        # 内网穿透的公网地址（如 https://xxx.ngrok-free.dev），填了才有在线精校的链接
        "public_url": "",
    },
}


def merge_settings(defaults: Dict[str, Any], stored: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并：stored 覆盖 defaults，新增的默认字段会自动补上。"""
    merged = copy.deepcopy(defaults)
    for key, value in (stored or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_settings(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_ffmpeg(settings: Dict[str, Any]) -> str:
    configured = (settings.get("ffmpeg_path") or "").strip()
    if configured:
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    bundled = BUNDLED_BIN_DIR / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled)
    raise RuntimeError("找不到 ffmpeg，请安装 ffmpeg 或在设置中填写 ffmpeg 路径")


def proxy_or_none(settings: Dict[str, Any]) -> Optional[str]:
    proxy = (settings.get("proxy") or "").strip()
    return proxy or None


def use_data_dir(path: Path) -> None:
    """切换数据目录（测试用）；需要在 db.init_db() 之前调用。"""
    global DATA_DIR, TASKS_DIR, DB_PATH
    DATA_DIR = Path(path)
    TASKS_DIR = DATA_DIR / "tasks"
    DB_PATH = DATA_DIR / "app.db"


def task_dir(task_id: int) -> Path:
    path = TASKS_DIR / str(task_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
