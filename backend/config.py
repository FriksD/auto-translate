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
    # 直播录制和采访定位（直播间监视用）
    "live": {
        # 录音分段时长（秒）：越短，采访结束后越快被发现
        "chunk_seconds": 30,
        # 每隔多少秒存一帧画面，交给采访定位那一步的模型按镜头判断采访起止；0 = 只录音
        "frame_interval": 5,
        # 录制期间每隔多少秒让 LLM 判断一次采访起止（有新转写时才判断）
        "detect_interval": 30,
        # 采访开始后多久仍未判定结束就直接截取（分钟）
        "max_interview_minutes": 10,
        # 截取采访时前后各留的余量（秒）
        "padding_seconds": 10,
    },
    # 直播间监视：一直录、一直转写和识别画面，发现采访就截下来生成任务。
    # 分段时长、抽帧间隔、判断间隔、采访最长时长、截取余量沿用上面 live 里的设置
    "monitor": {
        # 预约比赛 = 给赛事的直播间加一段时间窗：开赛前多少分钟开始、开赛后监视多少小时
        "pre_match_minutes": 15,
        "window_hours": 5,
        # 未开播时多久探测一次是否开播（秒）
        "offline_poll_seconds": 60,
        # 录制中多久确认一次还在播（秒；B 站查房间接口，其他平台断流时才探测）
        "live_probe_seconds": 120,
        # 录音、画面、转写保留多久（小时）：截下来的采访已经复制到任务目录，旧的录音可以删
        "retain_hours": 6,
        # 转写里出现这些话（不区分大小写）就让 LLM 看一下是不是采访；采访镜头也会触发
        "trigger_phrases": [
            "joined by", "joining me", "join me", "with me now", "i'm here with", "i am here with",
            "congratulations", "congrats", "how does it feel", "how are you feeling", "how do you feel",
            "thank you for your time", "thanks for your time", "thanks for joining", "thank you for joining",
        ],
    },
    # 投稿信息（推荐标题、摘要、封面）：初稿发出后在后台生成
    "package": {
        # 标题范例：最近发过的标题，新的在前；在线精校定稿时选用的标题会自动加到最前面
        "title_examples": [
            "xertioN赛后采访：即便Vitality不是主力阵容，我们依然想击败他们",
            "molodoy赛后采访：眼睛的手术对我帮助很大，我可以不用再分心去关注这件事了",
        ],
        # 摘要范例：同上
        "summary_examples": [
            "PGL布加勒斯特大师赛-2026今日正式落下帷幕，MOUZ以2-0的比分击败Vitality，以季军的名次结束了他们本次赛事的征程；"
            "比赛结束后，xertioN接受了BanKs的采访，谈论了球队对阵Vitality时的心态与后续的目标。（听译，可能存在出入）",
            "PGL布加勒斯特大师赛-2026今日继续，就在刚刚，FURIA以2-1的比分击败MOUZ，成功晋级决赛；"
            "比赛结束后，molodoy接受了BanKs的采访，谈论了这场比赛的关键回合与自己的状态。（听译，可能存在出入）",
        ],
        # 采访主持人，一行一个：「赛事关键词=主持人」按赛事名匹配（不区分大小写），只写名字的帮助 LLM 从转写里认人
        "hosts": ["PGL=BanKs"],
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


def monitor_dir(monitor_id: int, stream_key: str) -> Path:
    path = DATA_DIR / "monitors" / str(monitor_id) / stream_key
    path.mkdir(parents=True, exist_ok=True)
    return path


def task_dir(task_id: int) -> Path:
    path = TASKS_DIR / str(task_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
