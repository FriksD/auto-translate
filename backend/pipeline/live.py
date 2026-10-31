"""
直播环节：录音 → 滚动转写 → 采访定位 → 截取采访音频 → 交给后处理流水线。

每个处于 recording / interview_detecting 状态的任务有一个 LiveSession：
- Recorder 子进程持续把直播音频切成分段（chunks/）。
- 工作线程把新写完的分段逐个转写，追加到 live_transcript.jsonl，时间为"相对录制开始的秒数"。
- 录制期间每有新的转写、且距上次判断超过 detect_interval 秒，就把最近 15 分钟的转写交给 LLM，
  判断采访是否开始、是否结束。不等"比赛结束"信号：数据源慢了或挂了也不耽误发现采访。
- 连续两次判断都认为采访已结束（结束时间一致）后截取 interview.wav，停止录音，从「获取音频」开始跑流水线；
  人工标注起止、或采访超时也会截取。

时效（采访结束 → 开始处理）主要由分段时长和判断间隔决定：默认 30 秒一段、30 秒判断一次，
LLM 需要看到采访之后的内容才能判断结束、再加一次确认，一般 1.5～3 分钟内截取；后处理本身 1～3 分钟。

运行状态保存在任务目录的 live.json，服务重启后会接着录。
"""
import json
import logging
import threading
import time
import wave
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config, db
from ..integrations.task_cards import cards
from ..runner import State
from . import prompts
from .recorder import SAMPLE_RATE, Recorder, chunk_duration, chunk_start, frame_time, read_pcm

logger = logging.getLogger(__name__)

LIVE_STATES = (State.RECORDING, State.INTERVIEW_DETECTING)
DETECT_WINDOW = 15 * 60
PROVIDER_RANK = {"twitch": 0, "youtube": 1, "kick": 2}
# 同为官方流时的来源优先级：HLTV 列表 > 同一赛事以前用过的 > bo3.gg
SOURCE_RANK = {"hltv": 0, "memory": 1, "bo3": 2}
# LLM 连续失败多少次发提醒
LLM_ALERT_FAILURES = 3
# 两次判断给出的采访结束时间相差多少秒以内算一致
ENDING_TOLERANCE = 45
# 判定的结束点离转写末尾至少多少秒才算数
ENDING_MARGIN = 60
# 一直收不到比赛结束信号时，录制最长多久
MAX_RECORDING_HOURS = 6
# 人工点「本场无采访」时的原因（不用再提醒）
MANUAL_NO_INTERVIEW = "人工确认本场无采访"

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

def is_video_link(url: Optional[str]) -> bool:
    """
    指向某一场具体直播（而不是频道）的 YouTube 链接：watch?v=、youtu.be/、/live/<id>。
    这种链接在直播结束后就变成回放；实测 HLTV 预约时给的 PGL 链接到第二天已是前一天的回放，
    streamlink 会从头下载回放，录到的是昨天的比赛。
    """
    url = (url or "").lower()
    if "youtu.be/" in url or "watch?v=" in url:
        return True
    return "youtube.com/live/" in url


def pick_stream(streams: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    选官方直播流：HLTV 上官方流的国家标记是 "Other"（地球图标），其余基本是各国二路解说；
    bo3.gg 的官方英文流也记为 "Other"。同等条件下 Twitch 优先（有纯音频流）、再看来源、观看人数；
    指向具体某场直播的 YouTube 链接（可能已是回放）排在频道链接之后。
    频道名和赛事主办方对得上的（event_match，见 candidate_streams）最优先：
    HLTV 的 "Other" 也会标在二路解说上（实测 DFRAG 的比赛标给了个人主播 MominPK，官方是 DFragTV）。
    """
    candidates = [s for s in streams if s.get("url") and s.get("provider") in PROVIDER_RANK]
    if not candidates:
        return None
    return min(candidates, key=lambda s: (
        not s.get("event_match"), s.get("country") != "Other", is_video_link(s["url"]),
        PROVIDER_RANK[s["provider"]], SOURCE_RANK.get(s.get("source") or "hltv", 0), -(s.get("viewers") or 0)))


def remembered_streams(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """同一赛事以前的直播任务实际用过的直播源（赛事的官方流基本不变）。"""
    event = ((task.get("context") or {}).get("match") or {}).get("event")
    if not event:
        return []
    found = []
    for other in db.list_tasks():
        if other["id"] == task["id"] or other["source_type"] != "live":
            continue
        if ((other.get("context") or {}).get("match") or {}).get("event") != event:
            continue
        stream = load_state(other["id"]).get("stream")
        # 具体某场直播的链接下次就是回放了，只记频道链接
        if stream and stream.get("url") and stream.get("provider") in PROVIDER_RANK and not is_video_link(stream["url"]):
            found.append({**stream, "country": "Other", "source": "memory", "viewers": 0})
    return found


# 赛事名里不能代表主办方的词
EVENT_STOPWORDS = {"the", "cup", "major", "minor", "invitational", "masters", "series", "season", "episode",
                   "league", "pro", "open", "championship", "championships", "qualifier", "finals", "final",
                   "world", "global", "stage", "playoffs", "group", "cs2", "counter", "strike", "esports", "rmr", "ranked"}


def _alnum(text: Any) -> str:
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def event_keywords(event: Optional[str]) -> List[str]:
    """赛事名里可能是主办方/品牌的词：DFRAG Invitational 2026 → [dfrag]；PGL Masters Bucharest → [pgl, bucharest]"""
    words = [_alnum(w) for w in str(event or "").split()]
    return [w for w in words if len(w) >= 3 and not w.isdigit() and w not in EVENT_STOPWORDS]


def stream_matches_event(stream: Dict[str, Any], keywords: List[str]) -> bool:
    channel = _alnum(stream.get("name")) + " " + _alnum((stream.get("url") or "").rstrip("/").rsplit("/", 1)[-1])
    return any(k in channel for k in keywords)


def _stream_key(url: Optional[str]) -> str:
    return (url or "").rstrip("/").lower()


def candidate_streams(task: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    可选的直播源：当前数据源给的 + 预约时 HLTV 页面上的 + 同一赛事用过的，按 URL 去重。
    录制时发现不是直播（回放）的直播源记在 bad_streams 里，不再选。
    """
    match = (task.get("context") or {}).get("match") or {}
    keywords = event_keywords(match.get("event"))
    merged, seen = [], {_stream_key(url) for url in state.get("bad_streams") or []}
    for stream in list(state.get("streams") or []) + (match.get("streams") or []) + remembered_streams(task):
        key = _stream_key(stream.get("url"))
        if key and key not in seen:
            seen.add(key)
            merged.append({**stream, "event_match": stream_matches_event(stream, keywords)})
    return merged


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


def detection_window(lines: List[Dict[str, Any]], detected_start: Optional[float]) -> List[Dict[str, Any]]:
    """交给 LLM 的转写范围：最近 15 分钟；已发现采访开始时，从开始前 1 分钟起。"""
    if not lines:
        return []
    begin = lines[-1]["end"] - DETECT_WINDOW
    if detected_start is not None:
        begin = min(begin, detected_start - 60)
    return [line for line in lines if line["end"] >= begin]


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


def make_sheet(frames: List[Tuple[Path, float]]) -> bytes:
    """把最多 9 帧拼成 3×3 的图，每格左上角标时间（mm:ss，相对录制开始）。"""
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont

    images = [Image.open(path).convert("RGB") for path, _ in frames]
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


def read_shots(task_id: int) -> List[Dict[str, Any]]:
    path = config.task_dir(task_id) / "shots.jsonl"
    if not path.exists():
        return []
    shots = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return sorted(shots, key=lambda shot: shot["t"])


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


# ── 会话 ─────────────────────────────────────────────────────────────────────

class LiveSession:
    def __init__(self, manager: "LiveManager", task_id: int, stream_url: str):
        settings = db.get_settings()
        self.manager = manager
        self.task_id = task_id
        self.workdir = config.task_dir(task_id)
        self.recorder = Recorder(
            stream_url, self.workdir / "chunks", config.resolve_ffmpeg(settings), config.proxy_or_none(settings),
            int(settings["live"]["chunk_seconds"]), on_event=self._on_recorder_event,
            on_not_live=self._on_not_live, frame_interval=int(settings["live"].get("frame_interval", 0)),
        )
        # 转写一个分段（含写入转写稿）和丢弃回放分段互斥
        self._chunk_lock = threading.Lock()
        self._shot_failures = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.recorder.start()
        self._thread = threading.Thread(target=self._loop, name=f"live-{self.task_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.recorder.stop()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=60)

    def _on_recorder_event(self, level: str, message: str) -> None:
        db.add_event(self.task_id, message, level="error" if level == "error" else level)
        if level == "error":
            self.manager.alert(self.task_id, "录制异常", f"{message}，可以在卡片里填一个直播链接换源")

    def _on_not_live(self, url: str, connected_at: float) -> Optional[str]:
        """拉到的是回放：丢掉这次连接录到的内容，换下一个候选直播源；没有可换的就停止录制。"""
        self._discard_since(connected_at)

        def mark(s: Dict[str, Any]) -> None:
            bad = s.setdefault("bad_streams", [])
            if url not in bad:
                bad.append(url)
        state = mutate_state(self.task_id, mark)
        task = db.get_task(self.task_id)
        replacement = pick_stream(candidate_streams(task, state))
        message = f"直播源不是直播（下载比实时快，可能是回放），已弃用：{url}"
        if replacement:
            patch_state(self.task_id, stream=replacement)
            db.add_event(self.task_id, f"{message}；改录 {replacement.get('name') or ''} {replacement['url']}", level="warning")
            self.manager.alert(self.task_id, "直播源不是直播", f"{url} 可能是回放，已改录 {replacement['url']}，请确认")
            return replacement["url"]
        db.add_event(self.task_id, f"{message}；没有其他可用的直播源，录制已停止，请在任务页填写直播链接", level="error")
        self.manager.alert(self.task_id, "直播源不是直播",
                           f"{url} 可能是回放，没有其他直播源可换，录制已停止，请在卡片里填写直播链接")
        return None

    def _discard_since(self, connected_at: float) -> None:
        """把这次连接录到的分段挪到 rejected/，并从滚动转写里删掉对应内容。"""
        rejected = self.workdir / "rejected"
        rejected.mkdir(exist_ok=True)
        with self._chunk_lock:
            names = []
            for chunk in sorted(self.recorder.chunk_dir.glob("c_*.wav")):
                if chunk_start(chunk) >= connected_at - 1:
                    chunk.replace(rejected / chunk.name)
                    names.append(chunk.name)
            cutoff = connected_at - load_state(self.task_id)["recording"]["started_at"] - 1
            kept = [line for line in read_transcript(self.task_id) if line["start"] < cutoff]
            (self.workdir / "live_transcript.jsonl").write_text(
                "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in kept), encoding="utf-8")
            mutate_state(self.task_id, lambda s: s.update(
                transcribed=[n for n in s.get("transcribed") or [] if n not in names]))
            for frame in sorted(self.recorder.frame_dir.glob("f_*.jpg")):
                if frame_time(frame) >= connected_at - 1:
                    frame.replace(rejected / frame.name)
            shots = [shot for shot in read_shots(self.task_id) if shot["t"] < cutoff]
            (self.workdir / "shots.jsonl").write_text(
                "".join(json.dumps(shot, ensure_ascii=False) + "\n" for shot in shots), encoding="utf-8")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._transcribe_new()
                if self._stop.is_set():
                    break
                self._classify_frames()
                self._detect()
                self._check_timeouts()
            except Exception:
                logger.exception("任务 %s 直播处理出错", self.task_id)
            self._stop.wait(5)

    # 镜头时间轴
    def _classify_frames(self) -> None:
        """把还没打标签的画面每 9 帧拼一张图交给模型。最新一帧可能还没写完，不算。"""
        from . import stages

        frame_dir = self.recorder.frame_dir
        if not frame_dir.exists():
            return
        state = load_state(self.task_id)
        done = {shot["file"] for shot in read_shots(self.task_id)}
        pending = [f for f in sorted(frame_dir.glob("f_*.jpg"))[:-1] if f.name not in done]
        if not pending:
            return
        settings = db.get_settings()
        timeline = frame_timeline(pending, state["recording"]["started_at"])
        for begin in range(0, len(timeline), SHEET_SIZE):
            batch = timeline[begin:begin + SHEET_SIZE]
            # 不满 9 帧的最后一批等凑齐再判断，除非已经等了很久（录制停了）
            if len(batch) < SHEET_SIZE and self.recorder.running:
                return
            if self._stop.is_set():
                return
            try:
                answer = stages._parse_json_object(stages._llm_call(
                    settings, detect_step(settings), prompts.SHOT_SYSTEM, prompts.build_shot_prompt(len(batch)),
                    images=[make_sheet(batch)]))
                labels = parse_shot_labels(answer, len(batch))
                self._shot_failures = 0
            except Exception as error:
                self._shot_failures += 1
                logger.warning("任务 %s 画面识别失败（第 %s 次）：%s", self.task_id, self._shot_failures, error)
                if self._shot_failures == LLM_ALERT_FAILURES:
                    db.add_event(self.task_id, f"画面识别连续失败 {self._shot_failures} 次，只按转写定位采访：{error}",
                                 level="warning")
                if self._shot_failures < LLM_ALERT_FAILURES:
                    return
                labels = ["other"] * len(batch)  # 一直失败就跳过这批，不卡住后面的
            with open(self.workdir / "shots.jsonl", "a", encoding="utf-8") as fh:
                for (frame, t), label in zip(batch, labels):
                    fh.write(json.dumps({"file": frame.name, "t": round(t, 1), "label": label}) + "\n")

    # 滚动转写
    def _transcribe_new(self) -> None:
        from . import stages

        state = load_state(self.task_id)
        origin = state["recording"]["started_at"]
        done = set(state.get("transcribed") or [])
        pending = [c for c in self.recorder.completed_chunks() if c.name not in done]
        if not pending:
            return
        settings = db.get_settings()
        task = db.get_task(self.task_id)
        canonical, aliases = stages._names(task)
        initial_prompt = prompts.whisper_prompt(canonical + [t for t in aliases.values() if t not in canonical])
        timeline = {c.name: (begin, duration) for c, begin, duration in
                    chunk_timeline(self.recorder.completed_chunks(), origin)}
        whisper = stages._get_whisper(settings)
        for chunk in pending:
            if self._stop.is_set():
                return
            begin, _ = timeline[chunk.name]
            with self._chunk_lock:
                if not chunk.exists():  # 刚被判定为回放丢掉了
                    continue
                with stages.gpu_lock:
                    result = whisper.transcribe(
                        str(chunk), language=settings["whisper"]["language"] or None,
                        initial_prompt=initial_prompt or None, beam_size=1,
                    )
                lines = [{"start": round(begin + s["start"], 2), "end": round(begin + s["end"], 2), "text": s["text"]}
                         for s in result["segments"] if s["text"]]
                with open(self.workdir / "live_transcript.jsonl", "a", encoding="utf-8") as fh:
                    for line in lines:
                        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
                mutate_state(self.task_id, lambda s: s.setdefault("transcribed", []).append(chunk.name))

    # 采访定位
    def _detect(self) -> None:
        from . import stages

        task = db.get_task(self.task_id)
        if task["state"] not in LIVE_STATES:
            return
        settings = db.get_settings()
        state = load_state(self.task_id)
        detection = state.get("detection") or {}
        lines = read_transcript(self.task_id)
        if not lines:
            return
        until = lines[-1]["end"]
        if until <= detection.get("checked_until", -1):
            return
        if time.time() - detection.get("checked_at", 0) < int(settings["live"]["detect_interval"]):
            return

        origin = state["recording"]["started_at"]
        over_at = state.get("match_over_at")
        window = detection_window(lines, detection.get("start") if detection.get("started") else None)
        runs = [run for run in interview_runs(read_shots(self.task_id)) if run["end"] >= window[0]["start"]]
        user = prompts.build_interview_detect_prompt(window, task["context"], over_at - origin if over_at else None,
                                                     prompts.format_shot_runs(runs))
        (self.workdir / "prompt_detect.txt").write_text(user, encoding="utf-8")
        try:
            answer = stages._parse_json_object(
                stages._llm_call(settings, detect_step(settings), prompts.INTERVIEW_DETECT_SYSTEM, user))
        except Exception as error:
            failures = detection.get("failures", 0) + 1
            db.add_event(self.task_id, f"采访定位调用 LLM 失败（第 {failures} 次），稍后重试：{error}", level="warning")
            mutate_state(self.task_id, lambda s: s.setdefault("detection", {}).update(
                checked_at=time.time(), failures=failures))
            if failures == LLM_ALERT_FAILURES:
                self.manager.alert(self.task_id, "采访定位失败", f"LLM 已连续 {failures} 次调用失败：{str(error)[:200]}"
                                   f"\n请检查 LLM 设置，或在任务页手动标注采访起止")
            return

        start = prompts.parse_clock(answer.get("start"))
        if answer.get("started") and start is None:
            # "录音开始时采访已经在进行"：沿用之前判定的起点，没有就取窗口开头
            start = detection.get("start") if detection.get("started") else window[0]["start"]
        end = prompts.parse_clock(answer.get("end"))
        if end is not None:
            end = line_end_at(lines, end)
        started = bool(answer.get("started")) and start is not None
        # 结束点之后至少还要有 ENDING_MARGIN 秒的转写，才算"看到了采访之后的内容"。实测 Nemiga vs Vitality：
        # 换人接受采访时 LLM 两次都把转写的最后一行当成结束（810、840 秒，互相印证），截掉了后半段
        ended = (started and bool(answer.get("ended")) and end is not None and end > start
                 and until - end >= ENDING_MARGIN)
        newly_started = started and not detection.get("started")
        # 之前判定开始了、这次（窗口里有起点前后的内容）说没有采访：撤回。实测一次判断把赛间播放的
        # 预录选手专访当成了采访开始，之后一直说没有采访，但起点没撤回，10 分钟后超时截了一段无关内容
        retracted = bool(detection.get("started")) and not answer.get("started")
        # "已结束"要被下一次判断（看到了更多内容）确认才截取：实测 LLM 偶尔会把采访中途的
        # 过渡句当成结束，过早截取会停掉录音、丢掉后半段采访
        pending = detection.get("pending_end")
        confirmed = ended and pending is not None and abs(end - pending) <= ENDING_TOLERANCE
        if confirmed:
            end = max(end, pending)
        # 画面：和转写起点对得上的采访镜头段。镜头还在采访（或明显比转写判定的结束更晚才切走）时，
        # 不按转写的"已结束"截取；镜头已经切走就直接按镜头截取，不用等转写那边两次确认
        run = matching_run(runs, start) if started else None
        if run and (run["open"] or (ended and run["end"] > end + SHOT_END_AFTER)):
            ended = confirmed = False
        by_camera = bool(run and not run["open"] and not confirmed)

        def update(s: Dict[str, Any]) -> None:
            d = s.setdefault("detection", {})
            d.update(checked_at=time.time(), checked_until=until, last=answer, failures=0,
                     pending_end=end if ended and not confirmed else None)
            if started:
                d.update(started=True, start=start, interviewee=answer.get("interviewee"))
                d.setdefault("started_at", time.time())
            elif retracted:
                d["started"] = False
                for key in ("start", "started_at", "interviewee"):
                    d.pop(key, None)
        mutate_state(self.task_id, update)

        if retracted:
            db.add_event(self.task_id, f"撤回之前判定的采访开始（{prompts.format_timestamp(detection.get('start') or 0)}）："
                                       f"{answer.get('note') or '本次判断认为没有采访'}", level="warning")

        if newly_started:
            who = answer.get("interviewee") or "选手"
            db.add_event(self.task_id, f"检测到采访开始：{who}，约 {prompts.format_timestamp(start)}")
        if by_camera:
            db.add_event(self.task_id, f"画面显示采访镜头 {prompts.format_timestamp(run['start'])}–"
                                       f"{prompts.format_timestamp(run['end'])}，已切走")
            self.manager.capture(self.task_id, min(start, run["start"]), run["end"], "auto")
        elif confirmed:
            self.manager.capture(self.task_id, start, end, "auto")
        elif ended:
            db.add_event(self.task_id, f"初步判断采访在 {prompts.format_timestamp(end)} 结束，等下一次判断确认")

    def _check_timeouts(self) -> None:
        task = db.get_task(self.task_id)
        if task["state"] not in LIVE_STATES:
            return
        cfg = db.get_settings()["live"]
        state = load_state(self.task_id)
        detection = state.get("detection") or {}
        now = time.time()
        if detection.get("started"):
            if now - detection.get("started_at", now) > int(cfg["max_interview_minutes"]) * 60:
                lines = read_transcript(self.task_id)
                end = lines[-1]["end"] if lines else detection["start"] + 60
                db.add_event(self.task_id, "采访开始后长时间未判定结束，直接截取到当前位置", level="warning")
                self.manager.capture(self.task_id, detection["start"], end, "timeout")
            return
        over_at = state.get("match_over_at")
        if task["state"] == State.RECORDING or not over_at:
            if now - state["recording"]["started_at"] > MAX_RECORDING_HOURS * 3600:
                self.manager.no_interview(self.task_id, f"录制超过 {MAX_RECORDING_HOURS} 小时仍未收到比赛结束信号")
            return
        waited = now - over_at
        notified = state.get("notified") or {}
        if waited > int(cfg["alert_no_interview_minutes"]) * 60 and not notified.get("no_interview_yet"):
            def mark(s: Dict[str, Any]) -> None:
                s.setdefault("notified", {})["no_interview_yet"] = True
            mutate_state(self.task_id, mark)
            db.add_event(self.task_id, f"比赛结束 {waited / 60:.0f} 分钟仍未发现采访", level="warning")
            self.manager.alert(self.task_id, "尚未发现采访", f"比赛结束 {waited / 60:.0f} 分钟仍未检测到采访，"
                               f"请留意直播；如已开始可在任务页手动标注")
        if waited > int(cfg["max_post_match_minutes"]) * 60:
            self.manager.no_interview(self.task_id, "比赛结束后长时间未检测到采访")


class LiveManager:
    def __init__(self) -> None:
        self.sessions: Dict[int, LiveSession] = {}
        self._lock = threading.RLock()
        self._enqueue: Optional[Callable[[int, str], None]] = None

    def bind(self, enqueue: Callable[[int, str], None]) -> None:
        """enqueue(task_id, from_stage)：线程安全地把任务放进后处理队列。"""
        self._enqueue = enqueue

    @staticmethod
    def task_url(task_id: int) -> str:
        return f"{config.PUBLIC_URL}/tasks/{task_id}"

    @staticmethod
    def alert(task_id: int, kind: str, body: str = "") -> None:
        """需要人处理的事：在任务的飞书卡片下回复话题。其余进展只更新卡片，不单独发消息。"""
        cards.alert(task_id, kind, body)

    def is_recording(self, task_id: int) -> bool:
        session = self.sessions.get(task_id)
        return bool(session and session.recorder.running)

    def start(self, task_id: int, stream: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """开始录制；已在录制时切换直播源。stream 为空时用 live.json 里记录的直播源。"""
        with self._lock:
            state = load_state(task_id)
            stream = stream or state.get("stream")
            if not stream or not stream.get("url"):
                raise RuntimeError("没有可用的直播源，请在任务页填写直播链接")
            session = self.sessions.get(task_id)
            if session and session.recorder.running:
                if stream["url"] != session.recorder.url:
                    patch_state(task_id, stream=stream)
                    session.recorder.switch(stream["url"])
                return load_state(task_id)
            if session:  # 录音已停（直播源不是直播又没有可换的），换新会话前先停掉旧的转写线程
                self.sessions.pop(task_id, None)
                session.stop()

            def init(s: Dict[str, Any]) -> None:
                s["stream"] = stream
                recording = s.setdefault("recording", {})
                recording.setdefault("started_at", time.time())
                recording["active"] = True
                recording.pop("stopped_at", None)
            mutate_state(task_id, init)
            session = LiveSession(self, task_id, stream["url"])
            self.sessions[task_id] = session
            session.start()
            db.add_event(task_id, f"开始录制直播：{stream.get('name') or ''} {stream['url']}")
            return load_state(task_id)

    def stop(self, task_id: int, reason: str = "") -> None:
        with self._lock:
            session = self.sessions.pop(task_id, None)
        if session:
            session.stop()
            db.add_event(task_id, f"已停止录制{('：' + reason) if reason else ''}")

        def mark(s: Dict[str, Any]) -> None:
            if s.get("recording"):
                s["recording"]["active"] = False
                s["recording"].setdefault("stopped_at", time.time())
        if _state_path(task_id).exists():
            mutate_state(task_id, mark)

    def stop_all(self) -> None:
        for task_id in list(self.sessions):
            session = self.sessions.pop(task_id)
            session.stop()

    def resume_all(self) -> None:
        """服务重启后，接着录制上次没录完的任务。"""
        for task in db.list_tasks():
            if task["state"] in LIVE_STATES:
                try:
                    self.start(task["id"])
                    db.add_event(task["id"], "服务重启，继续录制", level="warning")
                except Exception as error:
                    db.add_event(task["id"], f"服务重启后无法继续录制：{error}", level="error")

    def mark_match_over(self, task_id: int) -> None:
        patch_state(task_id, match_over_at=time.time())

    def capture(self, task_id: int, start: float, end: float, source: str) -> float:
        """截取采访音频并开始后处理。start/end 为相对录制开始的秒数（不含余量）。"""
        task = db.get_task(task_id)
        if task["state"] in (State.QUEUED, State.PROCESSING):
            raise RuntimeError("任务正在处理中")
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")
        # 先停止录音，保证最后一个分段写完整
        self.stop(task_id, "采访已截取")
        state = load_state(task_id)
        if not state.get("recording"):
            raise RuntimeError("这个任务还没有录音")
        pad = float(db.get_settings()["live"]["padding_seconds"])
        timeline = chunk_timeline(sorted((self.chunk_dir(task_id)).glob("c_*.wav")), state["recording"]["started_at"])
        if not timeline:
            raise RuntimeError("没有录到音频")
        clip_start = max(0.0, start - pad)
        clip_end = min(end + pad, timeline[-1][1] + timeline[-1][2])
        duration = cut_audio(timeline, clip_start, clip_end, config.task_dir(task_id) / "interview.wav")
        def record(s: Dict[str, Any]) -> None:
            s["interview"] = {"start": start, "end": end, "source": source, "captured_at": time.time()}
            s.setdefault("notified", {}).pop("deadline", None)  # 重新截取后重新计时
        mutate_state(task_id, record)
        label = {"auto": "自动判定", "manual": "人工标注", "timeout": "超时截取"}.get(source, source)
        span = f"{prompts.format_timestamp(start)}–{prompts.format_timestamp(end)}"
        db.add_event(task_id, f"采访音频已截取（{label}）：{span}，共 {duration:.0f} 秒")
        if self._enqueue:
            self._enqueue(task_id, "ingest")
        return duration

    def no_interview(self, task_id: int, reason: str) -> None:
        self.stop(task_id, reason)
        db.update_task(task_id, state=State.NO_INTERVIEW)
        db.add_event(task_id, f"{reason}，判定为无采访；如有采访可在任务页手动标注起止", level="warning")
        if reason != MANUAL_NO_INTERVIEW:
            self.alert(task_id, "未发现采访", f"{reason}，已停止录制。如有采访请在任务页手动标注起止")

    @staticmethod
    def chunk_dir(task_id: int) -> Path:
        return config.task_dir(task_id) / "chunks"

    def status(self, task_id: int, transcript_tail: int = 200) -> Dict[str, Any]:
        state = load_state(task_id)
        chunks = sorted(self.chunk_dir(task_id).glob("c_*.wav")) if self.chunk_dir(task_id).exists() else []
        session = self.sessions.get(task_id)
        frame_dir = config.task_dir(task_id) / "frames"
        shots = read_shots(task_id)
        recorded = 0.0
        if state.get("recording") and chunks:
            timeline = chunk_timeline(chunks, state["recording"]["started_at"])
            recorded = timeline[-1][1] + timeline[-1][2]
        return {
            **state,
            "recording_active": bool(session and session.recorder.running),
            "connected": bool(session and session.recorder.connected_since),
            "chunk_count": len(chunks),
            "recorded_seconds": recorded,
            "transcript": read_transcript(task_id)[-transcript_tail:],
            "frame_count": len(list(frame_dir.glob("f_*.jpg"))) if frame_dir.exists() else 0,
            "shot_count": len(shots),
            "shot_runs": interview_runs(shots),
        }


manager = LiveManager()
