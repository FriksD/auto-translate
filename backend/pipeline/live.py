"""
直播录制的公用部分（直播间监视 monitor.py 用）：

- 任务目录里的 live.json：截取采访时写下的来源、录音起点、采访起止和识别结果。
- 录音分段时间轴、截取音频、转写读写。
- 画面：抽帧拼成 3×3 拼图交给视觉模型打标签，得到镜头时间轴，找出采访镜头段。
"""
import json
import logging
import threading
import wave
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config
from . import prompts
from .recorder import SAMPLE_RATE, chunk_duration, chunk_start, frame_time, read_pcm

logger = logging.getLogger(__name__)

# LLM 连续失败多少次发提醒
LLM_ALERT_FAILURES = 3
# 两次判断给出的采访结束时间相差多少秒以内算一致
ENDING_TOLERANCE = 45
# 判定的结束点离转写末尾至少多少秒才算数
ENDING_MARGIN = 60
# 采访期间截高清画面（封面候选，存在 stills/）：间隔、最多留几张（超过就删最早的）、连续失败几次就不再截。
# 不能截满就停：实测 Legacy vs The MongolZ，比赛中途几次误判的"采访镜头"就把名额用完了，真正的采访一张没截到
STILL_INTERVAL = 25
MAX_STILLS = 40
STILL_MAX_FAILURES = 3
# 镜头识别出的采访画面在多少秒以内，算"现在是采访镜头"
STILL_SHOT_RECENT = 60

_state_locks: Dict[int, threading.Lock] = defaultdict(threading.Lock)


# ── live.json ────────────────────────────────────────────────────────────────

def _state_path(task_id: int) -> Path:
    return config.task_dir(task_id) / "live.json"


def load_state(task_id: int) -> Dict[str, Any]:
    path = _state_path(task_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def mutate_state(task_id: int, fn: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    with _state_locks[task_id]:
        state = load_state(task_id)
        fn(state)
        path = _state_path(task_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        return state


def patch_state(task_id: int, **fields: Any) -> Dict[str, Any]:
    return mutate_state(task_id, lambda state: state.update(fields))


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def detect_step(settings: Dict[str, Any]) -> str:
    """采访定位用的 LLM 设置：单独选了就用 detect，否则沿用英文清洗那一步。"""
    return "detect" if (settings["llm"].get("detect") or {}).get("provider_id") else "clean"


def chunk_timeline(chunks: List[Path], origin: float) -> List[Tuple[Path, float, float]]:
    """
    每个分段相对录制开始的 (分段, 起点秒数, 时长)。
    文件名里的时刻只精确到秒，同一次连接里连续的分段按前一段的结束时间推算，避免误差累积；
    断流重连后的第一个分段以文件名时刻为准。
    """
    timeline: List[Tuple[Path, float, float]] = []
    for chunk in sorted(chunks):
        named = chunk_start(chunk) - origin
        duration = chunk_duration(chunk)
        if timeline:
            _, prev_start, prev_duration = timeline[-1]
            expected = prev_start + prev_duration
            if abs(named - expected) <= 2:
                named = expected
        timeline.append((chunk, named, duration))
    return timeline


def cut_audio(timeline: List[Tuple[Path, float, float]], start: float, end: float, output: Any) -> float:
    """把 [start, end] 区间（相对录制开始的秒数）拼接成一个 wav，缺失部分补静音。返回时长。"""
    start = max(0.0, start)
    total = int((end - start) * SAMPLE_RATE)
    if total <= 0:
        raise ValueError("截取区间为空")
    buffer = bytearray(total * 2)
    for chunk, chunk_begin, duration in timeline:
        if chunk_begin + duration <= start or chunk_begin >= end:
            continue
        data = read_pcm(chunk)
        frames = len(data) // 2
        src = max(0, int((start - chunk_begin) * SAMPLE_RATE))
        dst = max(0, int((chunk_begin - start) * SAMPLE_RATE))
        count = min(frames - src, total - dst)
        if count > 0:
            buffer[dst * 2:(dst + count) * 2] = data[src * 2:(src + count) * 2]
    with wave.open(str(output) if isinstance(output, Path) else output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(buffer))
    return total / SAMPLE_RATE


def read_transcript(task_id: int) -> List[Dict[str, Any]]:
    path = config.task_dir(task_id) / "live_transcript.jsonl"
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            lines.append(json.loads(raw))
        except ValueError:
            continue
    return sorted(lines, key=lambda line: line["start"])


def line_end_at(lines: List[Dict[str, Any]], timestamp: float) -> float:
    """LLM 给出的结束时间是最后一句的开头，换成这句话的结束时间。"""
    candidates = [line for line in lines if line["start"] <= timestamp + 1]
    if not candidates:
        return timestamp
    line = max(candidates, key=lambda l: l["start"])
    return max(timestamp, line["end"]) if timestamp - line["start"] < 60 else timestamp


# ── 画面：镜头时间轴 ──────────────────────────────────────────────────────────
# 思路来自 claude-real-video 这类"让 LLM 看视频"的工具：按时间抽帧，拼成带时间标签的 3×3 拼图，
# 一张图让视觉模型给 9 帧分别打标签（采访 / 解说台 / 比赛 / 其他），得到一条镜头时间轴。
# 采访镜头（主持人拿话筒和选手站在一起）很好认，镜头切走就是采访结束，不会像只看转写那样在换人提问时误判结束。

SHEET_COLUMNS = 3
SHEET_SIZE = SHEET_COLUMNS * SHEET_COLUMNS
# 采访镜头之间夹着的非采访画面不超过这么多秒，算同一段（插一个回放、切一下近景）
SHOT_GAP = 15
# 采访镜头至少持续这么久才算一段采访
SHOT_MIN_RUN = 20
# 采访镜头之后连续这么久都不是采访，才算采访结束
SHOT_END_AFTER = 30


def frame_timeline(frames: List[Path], origin: float) -> List[Tuple[Path, float]]:
    return [(frame, frame_time(frame) - origin) for frame in sorted(frames)]


def make_sheet(frames: List[Tuple[Path, float]], max_width: Optional[int] = None) -> bytes:
    """把最多 9 帧拼成 3×3 的图，每格左上角标时间（mm:ss，相对录制开始）。max_width：先把每帧缩到这个宽度以内。"""
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont

    images = [Image.open(path).convert("RGB") for path, _ in frames]
    if max_width:
        for image in images:
            image.thumbnail((max_width, max_width))
    width = max(image.width for image in images)
    height = max(image.height for image in images)
    rows = (len(images) + SHEET_COLUMNS - 1) // SHEET_COLUMNS
    sheet = Image.new("RGB", (width * min(len(images), SHEET_COLUMNS), height * rows), "black")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=max(14, height // 9))
    except TypeError:
        font = ImageFont.load_default()
    for index, (image, (_, t)) in enumerate(zip(images, frames)):
        x, y = (index % SHEET_COLUMNS) * width, (index // SHEET_COLUMNS) * height
        sheet.paste(image, (x, y))
        label = f"{index + 1}  {prompts.format_timestamp(t)}"
        box = draw.textbbox((x + 4, y + 4), label, font=font)
        draw.rectangle((box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2), fill="black")
        draw.text((x + 4, y + 4), label, fill="yellow", font=font)
    buffer = BytesIO()
    sheet.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


def parse_shot_labels(answer: Dict[str, Any], count: int) -> List[str]:
    labels = [str(label).strip().lower() for label in answer.get("labels") or []]
    if len(labels) != count:
        raise ValueError(f"模型返回了 {len(labels)} 个标签，应为 {count} 个")
    return [label if label in prompts.SHOT_LABELS else "other" for label in labels]


def interview_runs(shots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    镜头时间轴里的采访段落：{start, end, open}。
    start 是第一帧采访画面；end 是采访画面之后第一帧非采访画面（结束点精确到抽帧间隔）。
    之后还没有连续 SHOT_END_AFTER 秒的非采访画面时 open=True（采访可能还在继续）。
    """
    runs: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for shot in shots:
        if shot["label"] == "interview":
            if current and shot["t"] - current["last"] <= SHOT_GAP:
                current["last"] = shot["t"]
            else:
                current = {"start": shot["t"], "last": shot["t"]}
                runs.append(current)
    last_t = shots[-1]["t"] if shots else 0.0
    result = []
    for run in runs:
        if run["last"] - run["start"] < SHOT_MIN_RUN and run is not runs[-1]:
            continue
        after = next((shot["t"] for shot in shots if shot["t"] > run["last"]), None)
        is_open = last_t - run["last"] < SHOT_END_AFTER
        if run["last"] - run["start"] < SHOT_MIN_RUN and not is_open:
            continue
        result.append({"start": run["start"], "end": after if after is not None else run["last"], "open": is_open})
    return result


def matching_run(runs: List[Dict[str, Any]], start: float) -> Optional[Dict[str, Any]]:
    """和转写判定的采访起点对得上的采访镜头段：起点落在镜头段内，或在镜头段开始前 2 分钟内（主持人先口头交接）。"""
    return next((run for run in runs if run["start"] - 120 <= start <= run["end"]), None)
