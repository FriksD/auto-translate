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


def _default_whisper_model() -> str:
    if (LOCAL_WHISPER_MODEL / "model.bin").exists():
        return str(LOCAL_WHISPER_MODEL)
    return "large-v3"


DEFAULT_SETTINGS: Dict[str, Any] = {
    # HLTV、拉流、LLM 统一使用的代理；留空表示直连
    "proxy": "http://127.0.0.1:7897",
    "ffmpeg_path": "",
    "whisper": {
        "model": _default_whisper_model(),
        "device": "cuda",
        "compute_type": "float16",
        "language": "",  # 留空自动检测
        "beam_size": 5,
    },
    "diarization": {
        "enabled": False,
        "model_path": "",
        "hf_token": "",
    },
    "llm": {
        # 每项：{id, name, protocol: "openai"|"anthropic", base_url, api_key, model, use_proxy}
        "providers": [],
        "clean": {"provider_id": "", "model": ""},
        "write": {"provider_id": "", "model": ""},
        "temperature": 0.3,
        "extra_instructions": "",
    },
    "feishu": {
        "enabled": False,
        "app_id": "",
        "app_secret": "",
        "receive_id_type": "chat_id",  # chat_id 或 open_id
        "receive_id": "",
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


def task_dir(task_id: int) -> Path:
    path = TASKS_DIR / str(task_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
