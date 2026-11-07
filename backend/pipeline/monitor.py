"""
直播间监视：以直播间为单位一直录，发现现场采访就截下来，每段采访生成一个任务。

不再绑定比赛：赛前采访、图间采访、一场后连续采访好几个人、和最近一场无关的采访都能截到。
一个直播间监视（monitors 表）对应一个赛事，最多两条直播链接，每条链接一个 StreamSession：

- 开播探测：未开播时每 offline_poll_seconds 秒探测一次（stream_status.probe），开播了才开始录；
  录制中下播就停录回到等待。未开播、下播都不提醒，只有「还在播但拉不到」才算录制异常。
- 录制：Recorder 把音频切成分段（chunks/），同时每 frame_interval 秒存一帧低清画面（frames/）。
- 第一层（一直跑）：新分段逐个用 Whisper 转写（live_transcript.jsonl）；画面每 9 帧拼一张图交给视觉模型
  打标签（shots.jsonl，interview / desk / game / other）。
- 第二层（触发才跑）：出现采访镜头、或转写里出现 trigger_phrases，才把这一段转写交给 LLM，
  列出其中所有现场采访（起止、受访者、战队）。不出现就不调 LLM，一直开着也花不了多少钱。
- 截取：两次判断给出的结束点一致、下一段采访已经开始、或采访镜头已切走，就截取这段采访，
  新建任务从「抓取背景信息」开始跑流水线。截取不停录，接着找下一段。
- 采访期间每 25 秒截一张高清画面（stills/），截取时复制到任务目录，做投稿封面的候选。

时间都是相对 origin（这一轮录制开始的时刻）的秒数，存在每条链接目录的 monitor.json 里；
目录里没有保留的分段时（隔天开播），重新计时。录音、画面、转写超过 retain_hours 小时就删。
"""
import hashlib
import json
import logging
import shutil
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config, db
from ..integrations.task_cards import cards
from ..runner import State
from ..sources import stream_status
from . import prompts
from .live import (ENDING_MARGIN, ENDING_TOLERANCE, LLM_ALERT_FAILURES, MAX_STILLS, SHEET_SIZE, SHOT_END_AFTER,
                   STILL_INTERVAL, STILL_MAX_FAILURES, STILL_SHOT_RECENT, chunk_timeline, cut_audio, detect_step,
                   frame_timeline, interview_runs, line_end_at, make_sheet, matching_run, parse_shot_labels)
from .recorder import FRAME_PATTERN, Recorder, chunk_start, frame_time, grab_still

logger = logging.getLogger(__name__)

MAX_STREAMS = 2
# 交给 LLM 的转写最长多少秒
SCAN_WINDOW = 15 * 60
# 从触发点（或采访起点）往前多给 LLM 看多少秒
SCAN_LEAD = 120
# 两次「没有采访」的判断之后，镜头也不是采访，就不再看这一段
IDLE_CHECKS = 2
# 两段采访的起点相差多少秒以内算同一段
SAME_START = 60
# 多久清理一次旧录音
PRUNE_INTERVAL = 600
# 没有开播信息时（探测出错）也试着录：最多每隔多久试一次
LOOP_SECONDS = 5

_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def stream_key(url: str) -> str:
    """链接 → 目录名（同一个链接一直用同一个目录）。"""
    return hashlib.sha1((url or "").strip().rstrip("/").lower().encode("utf-8")).hexdigest()[:10]


def normalize_streams(streams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去掉空链接、重复链接，补上默认的是否走代理；最多两条。"""
    result, seen = [], set()
    for stream in streams or []:
        url = str(stream.get("url") or "").strip()
        if not url or stream_key(url) in seen:
            continue
        seen.add(stream_key(url))
        proxy = stream.get("proxy")
        item = {"url": url, "label": str(stream.get("label") or "").strip(),
                "proxy": stream_status.default_proxy(url) if proxy is None else bool(proxy)}
        # 转写语言：国内直播间是中文解说夹着英文采访（常有现场翻译），默认自动识别；其他沿用设置
        language = stream.get("language")
        if language is None and not stream_status.default_proxy(url):
            language = ""
        if language is not None:
            item["language"] = str(language)
        result.append(item)
    if len(result) > MAX_STREAMS:
        raise ValueError(f"一个直播间监视最多 {MAX_STREAMS} 条直播链接")
    return result


def is_on(monitor: Dict[str, Any], now: float) -> bool:
    """手动开着，或者在预约产生的时间窗里。"""
    if monitor.get("enabled"):
        return True
    return any(float(w.get("start") or 0) <= now <= float(w.get("end") or 0) for w in monitor.get("windows") or [])


def find_trigger(lines: List[Dict[str, Any]], runs: List[Dict[str, Any]], seen_until: float,
                 phrases: List[str]) -> Optional[float]:
    """seen_until 之后最早出现采访迹象的时刻：采访镜头开始，或转写里的采访用语。"""
    times = [run["start"] for run in runs if run["start"] > seen_until or (run["open"] and run["end"] > seen_until)]
    lowered = [p.lower() for p in phrases if p]
    times += [line["start"] for line in lines
              if line["start"] > seen_until and any(p in line["text"].lower() for p in lowered)]
    return min(times) if times else None


def scan_window(lines: List[Dict[str, Any]], begin: float, keep_from: Optional[float] = None) -> List[Dict[str, Any]]:
    """交给 LLM 的转写：从 begin 起，最长 SCAN_WINDOW 秒（太长时保留最新的，但不丢掉 keep_from 之后的）。"""
    if not lines:
        return []
    until = lines[-1]["end"]
    floor = max(begin, until - SCAN_WINDOW)
    if keep_from is not None:
        floor = min(floor, keep_from)
    floor = max(floor, begin)
    return [line for line in lines if line["end"] >= floor]


def parse_interviews(answer: Dict[str, Any], lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """LLM 的回答 → 按起点排序的采访列表（结束时间换成最后一句的结束）。"""
    result = []
    for item in answer.get("interviews") or []:
        if not isinstance(item, dict):
            continue
        start = prompts.parse_clock(item.get("start"))
        if start is None:
            continue
        end = prompts.parse_clock(item.get("end"))
        if end is not None:
            end = line_end_at(lines, end)
        names = item.get("interviewees") or []
        if isinstance(names, str):
            names = [names]
        result.append({
            "start": start, "end": end, "ended": bool(item.get("ended")),
            "interviewees": [str(n).strip() for n in names if str(n).strip()],
            "team": (str(item.get("team")).strip() or None) if item.get("team") else None,
            "live": item.get("live") is not False,
            "note": str(item.get("note") or ""),
        })
    return sorted(result, key=lambda item: item["start"])


def step_detection(det: Dict[str, Any], interviews: List[Dict[str, Any]], runs: List[Dict[str, Any]],
                   until: float, now: float) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Tuple[str, str]]]:
    """
    根据一次 LLM 判断推进检测状态。返回 (新的检测状态, 要截取的采访, 事件 [(level, message)])。

    det：floor（已截取到哪里，之前的不再管）、seen_until（看过、确认没有采访的位置）、trigger_at、
    active（正在进行的采访 {start, interviewees, team, started_wall, pending_end}）、idle_checks。
    """
    det = dict(det)
    floor = float(det.get("floor") or 0)
    events: List[Tuple[str, str]] = []
    captures: List[Dict[str, Any]] = []
    active = dict(det["active"]) if det.get("active") else None
    live = [item for item in interviews if item["live"] and item["start"] >= floor - 5]

    if not live:
        if active:
            events.append(("warning", f"撤回之前判定的采访开始（{prompts.format_timestamp(active['start'])}）：本次判断认为没有采访"))
        det["active"] = None
        det["idle_checks"] = int(det.get("idle_checks") or 0) + 1
        open_run = any(run["open"] and run["end"] >= floor for run in runs)
        if det["idle_checks"] >= IDLE_CHECKS and not open_run:
            det.update(trigger_at=None, seen_until=until, idle_checks=0)
        return det, captures, events

    det["idle_checks"] = 0
    first = live[0]
    following = live[1] if len(live) > 1 else None
    start = first["start"]
    if active is None or abs(active["start"] - start) > SAME_START:
        active = {"start": start, "interviewees": first["interviewees"], "team": first["team"],
                  "started_wall": now, "pending_end": None}
        who = "、".join(first["interviewees"]) or "选手"
        events.append(("info", f"检测到采访开始：{who}，约 {prompts.format_timestamp(start)}"))
    else:
        active.update(interviewees=first["interviewees"] or active.get("interviewees") or [],
                      team=first["team"] or active.get("team"))

    end = first["end"]
    ended = first["ended"] and end is not None and end > start
    # 下一段采访已经开始：这一段的结束点就确定了，不用等第二次判断，也不看镜头（换人时镜头一直是采访画面）
    split = bool(ended and following and following["start"] >= end - 5)
    confirmed = split
    if ended and not split:
        if until - end < ENDING_MARGIN:
            ended = False
        else:
            pending = active.get("pending_end")
            confirmed = pending is not None and abs(end - pending) <= ENDING_TOLERANCE
            if confirmed:
                end = max(end, pending)
    run = None if split else matching_run(runs, start)
    if run and (run["open"] or (ended and run["end"] > end + SHOT_END_AFTER)):
        ended = confirmed = False
    by_camera = bool(run and not run["open"] and not confirmed and run["end"] > start)

    if confirmed or by_camera:
        if by_camera:
            start, end = max(floor, min(start, run["start"])), run["end"]
        captures.append({"start": start, "end": end, "source": "auto" if confirmed else "camera",
                         "interviewees": active.get("interviewees") or [], "team": active.get("team")})
        det.update(floor=end, seen_until=end, active=None, recheck=True,
                   trigger_at=following["start"] if following else None)
        return det, captures, events

    active["pending_end"] = end if ended else None
    if ended:
        events.append(("info", f"初步判断采访在 {prompts.format_timestamp(end)} 结束，等下一次判断确认"))
    det["active"] = active
    return det, captures, events


def capture_title(monitor: Dict[str, Any], capture: Dict[str, Any], wall_start: float) -> str:
    who = " / ".join(capture.get("interviewees") or []) or "选手"
    event = monitor.get("event_name") or monitor.get("title") or "直播"
    return f"{event} {who} 采访 {time.strftime('%m-%d %H:%M', time.localtime(wall_start))}"


# ── 每条链接的运行状态（monitor.json）────────────────────────────────────────

def _jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    items = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            items.append(json.loads(raw))
        except ValueError:
            continue
    return items


def _write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items), encoding="utf-8")


class StreamFiles:
    """一条直播链接的目录：data/monitors/<monitor_id>/<stream_key>/"""

    def __init__(self, monitor_id: int, url: str):
        self.dir = config.monitor_dir(monitor_id, stream_key(url))
        self.chunk_dir = self.dir / "chunks"
        self.frame_dir = self.dir / "frames"
        self.still_dir = self.dir / "stills"
        self.state_path = self.dir / "monitor.json"
        self.transcript_path = self.dir / "live_transcript.jsonl"
        self.shots_path = self.dir / "shots.jsonl"

    def load(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def mutate(self, fn: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        with _locks[str(self.state_path)]:
            state = self.load()
            fn(state)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)
            return state

    def patch(self, **fields: Any) -> Dict[str, Any]:
        return self.mutate(lambda state: state.update(fields))

    def transcript(self) -> List[Dict[str, Any]]:
        return sorted(_jsonl(self.transcript_path), key=lambda line: line["start"])

    def shots(self) -> List[Dict[str, Any]]:
        return sorted(_jsonl(self.shots_path), key=lambda shot: shot["t"])

    def chunks(self) -> List[Path]:
        return sorted(self.chunk_dir.glob("c_*.wav")) if self.chunk_dir.exists() else []

    def reset(self, origin: float) -> None:
        """新一轮录制（目录里没有保留的录音）：重新计时，清掉旧的转写、镜头和检测状态。"""
        for path in (self.transcript_path, self.shots_path):
            if path.exists():
                path.unlink()
        for folder in (self.frame_dir, self.still_dir):
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)

        def init(state: Dict[str, Any]) -> None:
            for key in ("transcribed", "det"):
                state.pop(key, None)
            state["origin"] = origin
        self.mutate(init)

    def prune(self, before: float) -> None:
        """删掉 before（本地时间戳）之前的录音、画面，以及对应的转写和镜头记录。"""
        removed = set()
        for chunk in self.chunks():
            if chunk_start(chunk) < before:
                chunk.unlink(missing_ok=True)
                removed.add(chunk.name)
        for folder in (self.frame_dir, self.still_dir):
            if folder.exists():
                for frame in folder.glob("f_*.jpg"):
                    if frame_time(frame) < before:
                        frame.unlink(missing_ok=True)
        state = self.load()
        origin = state.get("origin")
        if origin is None:
            return
        cutoff = before - origin
        lines = self.transcript()
        if lines and lines[0]["start"] < cutoff:
            _write_jsonl(self.transcript_path, [line for line in lines if line["start"] >= cutoff])
        shots = self.shots()
        if shots and shots[0]["t"] < cutoff:
            _write_jsonl(self.shots_path, [shot for shot in shots if shot["t"] >= cutoff])
        if removed:
            self.mutate(lambda s: s.update(transcribed=[n for n in s.get("transcribed") or [] if n not in removed]))


# ── 会话 ─────────────────────────────────────────────────────────────────────

class StreamSession:
    """一条直播链接：等开播 → 录制 → 转写、识别画面、找采访。"""

    def __init__(self, manager: "MonitorManager", monitor_id: int, stream: Dict[str, Any]):
        self.manager = manager
        self.monitor_id = monitor_id
        self.stream = stream
        self.url = stream["url"]
        self.files = StreamFiles(monitor_id, self.url)
        self.recorder: Optional[Recorder] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_probe = 0.0
        self._last_prune = 0.0
        self._shot_failures = 0
        self._still_thread: Optional[threading.Thread] = None
        self._still_failures = 0
        self._still_last = 0.0
        self._outage_alerted = False

    # 生命周期
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"monitor-{self.monitor_id}-{self.files.dir.name}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._stop_recorder()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=60)
        self.files.patch(status="stopped", status_at=time.time())

    @property
    def recording(self) -> bool:
        return bool(self.recorder and self.recorder.running)

    def _event(self, message: str, level: str = "info") -> None:
        label = self.stream.get("label") or stream_status.platform(self.url)
        db.add_monitor_event(self.monitor_id, f"[{label}] {message}", level)

    def _proxy(self, settings: Dict[str, Any]) -> Optional[str]:
        return config.proxy_or_none(settings) if self.stream.get("proxy") else None

    def _set_status(self, status: str, **extra: Any) -> None:
        state = self.files.load()
        if state.get("status") != status:
            self.files.patch(status=status, status_at=time.time(), **extra)
        elif extra:
            self.files.patch(**extra)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._watch_live()
                if self._stop.is_set():
                    break
                if self.files.load().get("origin") is not None:
                    self._transcribe_new()
                    self._classify_frames()
                    self._grab_still()
                    self._detect()
                    self._check_timeout()
                    self._prune()
            except Exception:
                logger.exception("直播间监视 %s 出错：%s", self.monitor_id, self.url)
            self._stop.wait(LOOP_SECONDS)

    # 开播探测
    def _watch_live(self) -> None:
        cfg = db.get_settings()["monitor"]
        now = time.time()
        if self.recording:
            # B 站的房间接口很便宜，定期确认还在播；其他平台断流时才探测（见 _on_recorder_event）
            if stream_status.bilibili_room(self.url) and now - self._last_probe >= int(cfg["live_probe_seconds"]):
                self._probe_and_stop_if_offline()
            return
        if now - self._last_probe < int(cfg["offline_poll_seconds"]):
            return
        self._last_probe = now
        result = stream_status.probe(self.url, self._proxy(db.get_settings()))
        if result["live"] is False:
            self._set_status("offline", title=result.get("title") or "")
            return
        if result["live"] is None:
            self._set_status("unknown", error=result.get("error") or "")
        self._start_recorder(result.get("title") or "")

    def _probe_and_stop_if_offline(self) -> bool:
        self._last_probe = time.time()
        result = stream_status.probe(self.url, self._proxy(db.get_settings()))
        if result["live"] is False:
            self._stop_recorder()
            self._set_status("offline", title=result.get("title") or "")
            self._event("直播间已下播，开播后自动继续录")
            return True
        return False

    def _start_recorder(self, title: str) -> None:
        settings = db.get_settings()
        if not self.files.chunks():
            self.files.reset(time.time())
        self.recorder = Recorder(
            self.url, self.files.chunk_dir, config.resolve_ffmpeg(settings), self._proxy(settings),
            int(settings["live"]["chunk_seconds"]), on_event=self._on_recorder_event,
            on_not_live=self._on_not_live, frame_interval=int(settings["live"].get("frame_interval", 0)),
        )
        self.recorder.start()
        self._outage_alerted = False
        self._set_status("recording", title=title, error="")
        self._event(f"开播了，开始录制{('：' + title) if title else ''}")

    def _stop_recorder(self) -> None:
        recorder, self.recorder = self.recorder, None
        if recorder:
            recorder.stop()

    def _on_recorder_event(self, level: str, message: str) -> None:
        if level == "recovered":
            self._outage_alerted = False
            self._event(message)
            return
        if level in ("warning", "error") and not stream_status.bilibili_room(self.url):
            # 断流：先看是不是下播了；下播就安静地回到等待
            result = stream_status.probe(self.url, self._proxy(db.get_settings()))
            self._last_probe = time.time()
            if result["live"] is False:
                threading.Thread(target=self._stop_recorder, daemon=True).start()
                self._set_status("offline")
                self._event("直播间已下播，开播后自动继续录")
                return
        if level == "error" and not self._outage_alerted:
            self._outage_alerted = True
            self._event(f"{message}（直播间还在播，但拉不到流）", "error")
        elif level != "error":
            logger.info("直播间监视 %s：%s", self.monitor_id, message)

    def _on_not_live(self, url: str, connected_at: float) -> Optional[str]:
        """拉到的是回放：停掉这次录制（分段留着，没关系，采访定位会认出不是现场），过一会儿再探测。"""
        self._event(f"拉到的不是直播（下载比实时快，可能是回放）：{url}，稍后重试", "warning")
        self._set_status("replay")
        return None

    # 滚动转写
    def _transcribe_new(self) -> None:
        from . import stages

        state = self.files.load()
        origin = state["origin"]
        done = set(state.get("transcribed") or [])
        completed = self._completed_chunks()
        pending = [c for c in completed if c.name not in done]
        if not pending:
            return
        settings = db.get_settings()
        aliases, _ = stages._glossary()
        names = sorted(set(aliases.values()) | set(self.manager.known_names(self.monitor_id)))
        initial_prompt = prompts.whisper_prompt(names)
        language = self.stream.get("language", settings["whisper"]["language"]) or None
        timeline = {c.name: begin for c, begin, _ in chunk_timeline(completed, origin)}
        whisper = stages._get_whisper(settings)
        for chunk in pending:
            if self._stop.is_set():
                return
            with stages.gpu_lock:
                result = whisper.transcribe(str(chunk), language=language, initial_prompt=initial_prompt or None,
                                            beam_size=1)
            begin = timeline[chunk.name]
            lines = [{"start": round(begin + s["start"], 2), "end": round(begin + s["end"], 2), "text": s["text"]}
                     for s in result["segments"] if s["text"]]
            with open(self.files.transcript_path, "a", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            self.files.mutate(lambda s: s.setdefault("transcribed", []).append(chunk.name))

    def _completed_chunks(self) -> List[Path]:
        if self.recorder:
            return self.recorder.completed_chunks()
        return [c for c in self.files.chunks() if c.stat().st_size > 44]

    # 镜头时间轴
    def _classify_frames(self) -> None:
        from . import stages

        if not self.files.frame_dir.exists():
            return
        state = self.files.load()
        done = {shot["file"] for shot in self.files.shots()}
        frames = sorted(self.files.frame_dir.glob("f_*.jpg"))
        if self.recording:
            frames = frames[:-1]  # 最新一帧可能还没写完
        pending = [f for f in frames if f.name not in done]
        if not pending:
            return
        settings = db.get_settings()
        timeline = frame_timeline(pending, state["origin"])
        for begin in range(0, len(timeline), SHEET_SIZE):
            batch = timeline[begin:begin + SHEET_SIZE]
            if len(batch) < SHEET_SIZE and self.recording:
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
                logger.warning("直播间监视 %s 画面识别失败（第 %s 次）：%s", self.monitor_id, self._shot_failures, error)
                if self._shot_failures == LLM_ALERT_FAILURES:
                    self._event(f"画面识别连续失败 {self._shot_failures} 次，只按转写找采访：{error}", "warning")
                if self._shot_failures < LLM_ALERT_FAILURES:
                    return
                labels = ["other"] * len(batch)
            with open(self.files.shots_path, "a", encoding="utf-8") as fh:
                for (frame, t), label in zip(batch, labels):
                    fh.write(json.dumps({"file": frame.name, "t": round(t, 1), "label": label}) + "\n")

    # 封面候选
    def _grab_still(self) -> None:
        if (self._still_thread and self._still_thread.is_alive()) or self._still_failures >= STILL_MAX_FAILURES:
            return
        if time.time() - self._still_last < STILL_INTERVAL or not self.recording:
            return
        state = self.files.load()
        shots = self.files.shots()
        latest = shots[-1] if shots else None
        on_camera = bool(latest and latest["label"] == "interview"
                         and time.time() - state["origin"] - latest["t"] < STILL_SHOT_RECENT)
        if not (on_camera or (state.get("det") or {}).get("active")):
            return
        self._still_last = time.time()
        self.files.still_dir.mkdir(exist_ok=True)
        for old in sorted(self.files.still_dir.glob("f_*.jpg"))[:-(MAX_STILLS - 1)]:
            old.unlink()
        output = self.files.still_dir / time.strftime(FRAME_PATTERN)
        settings = db.get_settings()

        def grab() -> None:
            try:
                grab_still(self.url, output, config.resolve_ffmpeg(settings), self._proxy(settings))
                self._still_failures = 0
            except Exception as error:
                self._still_failures += 1
                logger.warning("直播间监视 %s 截取高清画面失败（第 %s 次）：%s", self.monitor_id, self._still_failures, error)
                if self._still_failures == STILL_MAX_FAILURES:
                    self._event(f"截取高清画面连续失败，不再截取（封面只能用 HLTV 的图）：{error}", "warning")

        self._still_thread = threading.Thread(target=grab, name=f"still-m{self.monitor_id}", daemon=True)
        self._still_thread.start()

    # 采访定位
    def _detect(self) -> None:
        from . import stages

        settings = db.get_settings()
        state = self.files.load()
        det = state.get("det") or {}
        lines = self.files.transcript()
        if not lines:
            return
        until = lines[-1]["end"]
        floor = float(det.get("floor") or 0)
        runs = [run for run in interview_runs(self.files.shots()) if run["end"] >= floor]
        seen = max(floor, float(det.get("seen_until") or 0))
        trigger = find_trigger(lines, runs, seen, settings["monitor"]["trigger_phrases"])
        if not det.get("active") and det.get("trigger_at") is None:
            if trigger is None:
                return  # 没有采访迹象，不调 LLM
            det["trigger_at"] = trigger
            self.files.mutate(lambda s: s.setdefault("det", {}).update(trigger_at=trigger))
        recheck = bool(det.get("recheck"))
        if until <= float(det.get("checked_until") or -1) and not recheck:
            return
        if not recheck and time.time() - float(det.get("checked_at") or 0) < int(settings["live"]["detect_interval"]):
            return

        active = det.get("active")
        anchor = active["start"] if active else det["trigger_at"]
        window = scan_window(lines, max(floor, anchor - SCAN_LEAD), keep_from=anchor - 60)
        if not window:
            return
        monitor = db.get_monitor(self.monitor_id) or {}
        user = prompts.build_interview_scan_prompt(
            window, monitor.get("event_name") or monitor.get("title") or "",
            self.manager.teams_text(self.monitor_id), prompts.format_shot_runs(runs))
        (self.files.dir / "prompt_detect.txt").write_text(user, encoding="utf-8")
        try:
            answer = stages._parse_json_object(
                stages._llm_call(settings, detect_step(settings), prompts.INTERVIEW_SCAN_SYSTEM, user))
        except Exception as error:
            failures = int(det.get("failures") or 0) + 1
            self.files.mutate(lambda s: s.setdefault("det", {}).update(checked_at=time.time(), failures=failures,
                                                                       recheck=False))
            if failures == LLM_ALERT_FAILURES:
                self._event(f"采访定位调用 LLM 连续失败 {failures} 次：{str(error)[:200]}，请检查 LLM 设置", "error")
            return

        interviews = parse_interviews(answer, lines)
        new_det, captures, events = step_detection(det, interviews, runs, until, time.time())
        new_det.update(checked_at=time.time(), checked_until=until, failures=0, last=answer)
        if not captures:
            new_det["recheck"] = False
        self.files.mutate(lambda s: s.update(det=new_det))
        for level, message in events:
            self._event(message, level)
        for capture in captures:
            self.capture(capture)

    def _check_timeout(self) -> None:
        det = self.files.load().get("det") or {}
        active = det.get("active")
        if not active:
            return
        limit = int(db.get_settings()["live"]["max_interview_minutes"]) * 60
        if time.time() - float(active.get("started_wall") or time.time()) <= limit:
            return
        lines = self.files.transcript()
        end = lines[-1]["end"] if lines else active["start"] + 60
        self._event("采访开始后长时间未判定结束，直接截取到当前位置", "warning")
        self.files.mutate(lambda s: s.setdefault("det", {}).update(
            floor=end, seen_until=end, active=None, trigger_at=None))
        self.capture({"start": active["start"], "end": end, "source": "timeout",
                      "interviewees": active.get("interviewees") or [], "team": active.get("team")})

    def _prune(self) -> None:
        if time.time() - self._last_prune < PRUNE_INTERVAL:
            return
        self._last_prune = time.time()
        hours = float(db.get_settings()["monitor"]["retain_hours"])
        self.files.prune(time.time() - hours * 3600)

    # 截取 → 新任务
    def capture(self, capture: Dict[str, Any]) -> Dict[str, Any]:
        """把一段采访截成新任务。capture：{start, end, source, interviewees, team}，时间相对 origin。"""
        state = self.files.load()
        origin = state["origin"]
        start, end = float(capture["start"]), float(capture["end"])
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")
        timeline = chunk_timeline(self.files.chunks(), origin)
        if not timeline:
            raise RuntimeError("没有录到音频")
        pad = float(db.get_settings()["live"]["padding_seconds"])
        clip_start = max(0.0, start - pad)
        clip_end = min(end + pad, timeline[-1][1] + timeline[-1][2])
        monitor = db.get_monitor(self.monitor_id) or {"id": self.monitor_id}
        team = capture.get("team")
        task = db.create_task(
            title=capture_title(monitor, capture, origin + start), state=State.DRAFT, source_type="live",
            source_url=self.url, team1=team,
            context={"monitor": {"id": self.monitor_id, "event_name": monitor.get("event_name"),
                                 "event_id": monitor.get("event_id")}},
        )
        task_id = task["id"]
        workdir = config.task_dir(task_id)
        duration = cut_audio(timeline, clip_start, clip_end, workdir / "interview.wav")
        if self.files.still_dir.exists():
            stills = workdir / "stills"
            stills.mkdir(exist_ok=True)
            for still in self.files.still_dir.glob("f_*.jpg"):
                if clip_start - 15 <= frame_time(still) - origin <= clip_end + 15:
                    shutil.copy2(still, stills / still.name)
        transcript = [line for line in self.files.transcript() if clip_start <= line["start"] <= clip_end]
        _write_jsonl(workdir / "live_transcript.jsonl", transcript)
        names = capture.get("interviewees") or []
        live_state = {
            "monitor_id": self.monitor_id,
            "stream": {"url": self.url, "label": self.stream.get("label") or "",
                       "provider": stream_status.platform(self.url)},
            "recording": {"started_at": origin, "active": False},
            "interview": {"start": start, "end": end, "clip_start": clip_start, "source": capture["source"],
                          "captured_at": time.time()},
            "detection": {"started": True, "start": start, "interviewee": names[0] if names else None,
                          "interviewees": names, "team": team},
        }
        (workdir / "live.json").write_text(json.dumps(live_state, ensure_ascii=False, indent=1), encoding="utf-8")
        label = {"auto": "自动判定", "camera": "镜头切走", "manual": "人工标注", "timeout": "超时截取"}.get(
            capture["source"], capture["source"])
        span = f"{prompts.format_timestamp(start)}–{prompts.format_timestamp(end)}"
        db.add_event(task_id, f"从直播间截取采访（{label}）：{self.url} {span}，共 {duration:.0f} 秒")
        self._event(f"截取采访 {span}（{label}）→ 任务 #{task_id}：{task['title']}")
        cards.track(task_id)
        self.manager.enqueue(task_id)
        return db.get_task(task_id)

    def status(self, transcript_tail: int = 0) -> Dict[str, Any]:
        state = self.files.load()
        shots = self.files.shots()
        chunks = self.files.chunks()
        result = {
            "url": self.url, "label": self.stream.get("label") or "", "proxy": bool(self.stream.get("proxy")),
            "platform": stream_status.platform(self.url),
            "status": state.get("status") or "starting", "status_at": state.get("status_at"),
            "title": state.get("title") or "", "error": state.get("error") or "",
            "recording": self.recording, "connected": bool(self.recorder and self.recorder.connected_since),
            "origin": state.get("origin"), "chunk_count": len(chunks),
            "shot_runs": interview_runs(shots)[-10:],
            "detection": state.get("det") or {},
        }
        if transcript_tail:
            result["transcript"] = self.files.transcript()[-transcript_tail:]
        return result


# ── 管理 ─────────────────────────────────────────────────────────────────────

class MonitorManager:
    """按 monitors 表和时间窗开关各条链接的 StreamSession。"""

    RECONCILE_SECONDS = 30

    def __init__(self) -> None:
        self.sessions: Dict[Tuple[int, str], StreamSession] = {}
        self._lock = threading.RLock()
        self._enqueue: Optional[Callable[[int, str], None]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()

    def bind(self, enqueue: Callable[[int, str], None]) -> None:
        self._enqueue = enqueue

    def enqueue(self, task_id: int) -> None:
        if self._enqueue:
            self._enqueue(task_id, "context")

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="monitor-manager", daemon=True)
        self._thread.start()

    def stop_all(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.stop()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception:
                logger.exception("直播间监视调度出错")
            self._wake.wait(self.RECONCILE_SECONDS)
            self._wake.clear()

    def reconcile(self, now: Optional[float] = None) -> None:
        """该开的开、该关的关；链接或代理设置变了就重开。"""
        now = time.time() if now is None else now
        wanted: Dict[Tuple[int, str], Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        for monitor in db.list_monitors():
            if is_on(monitor, now):
                for stream in monitor["streams"]:
                    wanted[(monitor["id"], stream_key(stream["url"]))] = (monitor, stream)
        with self._lock:
            for key, session in list(self.sessions.items()):
                if key not in wanted or wanted[key][1] != session.stream:
                    self.sessions.pop(key)
                    threading.Thread(target=session.stop, daemon=True).start()
                    db.add_monitor_event(key[0], f"停止监视：{session.url}")
            for key, (monitor, stream) in wanted.items():
                if key not in self.sessions:
                    session = StreamSession(self, monitor["id"], stream)
                    self.sessions[key] = session
                    session.start()
                    db.add_monitor_event(monitor["id"], f"开始监视：{stream['url']}"
                                                        f"（{'走代理' if stream.get('proxy') else '直连'}）")

    def session(self, monitor_id: int, index: int) -> Optional[StreamSession]:
        monitor = db.get_monitor(monitor_id)
        if not monitor or index >= len(monitor["streams"]):
            return None
        return self.sessions.get((monitor_id, stream_key(monitor["streams"][index]["url"])))

    def status(self, monitor: Dict[str, Any], transcript_tail: int = 0) -> Dict[str, Any]:
        streams = []
        for stream in monitor["streams"]:
            session = self.sessions.get((monitor["id"], stream_key(stream["url"])))
            if session:
                streams.append(session.status(transcript_tail))
            else:
                state = StreamFiles(monitor["id"], stream["url"]).load()
                streams.append({**stream, "platform": stream_status.platform(stream["url"]), "status": "stopped",
                                "recording": False, "title": state.get("title") or ""})
        return {**monitor, "on": is_on(monitor, time.time()), "stream_status": streams,
                "events": db.recent_monitor_events(monitor["id"], 30)}

    # 给转写和采访定位用的名字：这个直播间截到的采访任务里已经识别出的队伍和选手
    @staticmethod
    def _monitor_tasks(monitor_id: int) -> List[Dict[str, Any]]:
        return [t for t in db.list_tasks()[:200]
                if ((t.get("context") or {}).get("monitor") or {}).get("id") == monitor_id]

    def known_names(self, monitor_id: int) -> List[str]:
        names = []
        for task in self._monitor_tasks(monitor_id):
            for team in task["context"].get("teams") or []:
                names += [team.get("name")] + list(team.get("players") or [])
        return [n for n in names if n]

    def known_teams(self, monitor_id: int, exclude: Optional[int] = None) -> List[str]:
        names = []
        for task in self._monitor_tasks(monitor_id):
            if task["id"] != exclude:
                names += [t.get("name") for t in task["context"].get("teams") or [] if t.get("name")]
        return list(dict.fromkeys(names))

    def teams_text(self, monitor_id: int) -> str:
        seen, lines = set(), []
        for task in self._monitor_tasks(monitor_id):
            for team in task["context"].get("teams") or []:
                if team.get("name") and team["name"] not in seen and team.get("players"):
                    seen.add(team["name"])
                    lines.append(f"- {team['name']}: {', '.join(team['players'])}")
        return "\n".join(lines)


manager = MonitorManager()
