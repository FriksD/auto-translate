"""
比赛监视器：按比分自适应轮询已预约的比赛，接近结束时开始录制，比赛结束后转入采访监听。

决胜图：任一方再赢下当前这张图就赢下整场（BO1 唯一一张图；BO3 某方已赢 1 张；BO5 某方已赢 2 张）。
轮询间隔按决胜图领先方的回合数提高（默认 10 / 5 / 2 分钟，录制中 1 分钟）。
主数据源没有回合比分（bo3.gg 的普通比赛）时：决胜图开打后改从 HLTV 比分板读回合比分，照常按比分判断；
HLTV 也读不到时，决胜图开打 no_score_record_minutes 分钟后开始录制（开打时间来自 bo3.gg，没有就用第一次看到的时刻）。

比分从 match_sources 里按设置的顺序读取（默认 bo3.gg → HLTV），前一个失败自动换下一个。
全部失败时：提醒人工；开赛后持续失败超过 SOURCE_DOWN_RECORD 秒，保险起见直接开始录制
（采访定位在录制期间一直在跑，不依赖"比赛结束"信号）。

另外检查采访的时效：采访结束后超过 draft_deadline_minutes 还没出初稿就提醒。
"""
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from .. import config, db
from ..runner import PipelineRunner, State
from ..sources.hltv import HLTVClient
from .live import LiveManager, candidate_streams, load_state, mutate_state, patch_state, pick_stream
from .match_sources import SOURCE_LABELS, AllSourcesFailed, SourceChain, brief, live_map_score

logger = logging.getLogger(__name__)

TICK = 10
RETRY_AFTER_ERROR = 60
SOURCE_DOWN_ALERT = 5 * 60
SOURCE_DOWN_RECORD = 10 * 60
# 已开始录音但还没收到"比赛结束"的任务（比如采访已截取、正在处理），继续跟踪比分到这么久
FOLLOW_AFTER_RECORDING = 6 * 3600
WATCH_STATES = (State.RESERVED, State.WATCHING, State.RECORDING)


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def assess(match: Dict[str, Any], cfg: Dict[str, Any], recording: bool = False,
           now: Optional[float] = None) -> Dict[str, Any]:
    maps = match.get("maps") or []
    wins = [sum(1 for m in maps if m.get("winner") == side) for side in (1, 2)]
    # bo3.gg 的大比分比每张图的胜方更新得早（实测图打完后图的胜方要过一阵才填上），有就以它为准
    if match.get("source") == "bo3":
        series = [match["team1"].get("score"), match["team2"].get("score")]
        if None not in series:
            wins = [max(w, int(s)) for w, s in zip(wins, series)]
    best_of = match.get("best_of") or len(maps) or 1
    need = best_of // 2 + 1
    status = match["status"]
    # 一方已经拿够图数（比如对手投降）时，HLTV 的状态可能还停在 LIVE
    if status == "live" and max(wins) >= need:
        status = "over"
    live = (match["live"] if "live" in match else live_map_score(match)) if status == "live" else None
    has_round_score = match.get("has_round_score", True)
    deciding = status == "live" and max(wins) >= need - 1
    current_map = next((m for m in maps if m.get("status") == "live"), None)
    # 只看赛点一方（再赢一张就拿下系列赛）的回合分：BO3 1:0 时第二张图由落后方领先，
    # 打完是 1:1、还要打第三张；按两边较高分判断会提前一整张图开录
    match_point = [side for side in (0, 1) if wins[side] == need - 1]
    leader = max(live[("team1", "team2")[side]] for side in match_point) if live and deciding else None
    overtime = bool(live and min(live["team1"], live["team2"]) >= 12)

    intervals = cfg["intervals"]
    no_score = deciding and not has_round_score and current_map is not None
    no_score_record, wait_left = False, 0.0
    if no_score:
        began = current_map.get("began_at")
        wait = float(cfg.get("no_score_record_minutes", 15)) * 60
        elapsed = (now or time.time()) - began if began else None
        # 不知道这张图什么时候开打的：保险起见直接录
        no_score_record = elapsed is None or elapsed >= wait
        wait_left = 0.0 if elapsed is None else wait - elapsed
    if recording:
        interval, reason = intervals["recording"], "录制中，等待比赛结束"
    elif status == "over":
        interval, reason = intervals["recording"], "比赛已结束"
    elif not deciding:
        interval, reason = intervals["idle"], "未开赛" if status != "live" else "非决胜图"
    elif no_score_record:
        interval, reason = intervals["recording"], "决胜图进行中（没有回合比分，按开打时长开始录制）"
    elif no_score:
        interval = max(30, min(int(intervals["mid"]), int(wait_left) + 5))
        reason = (f"决胜图 {current_map['name']} 已开打 {(wait - wait_left) / 60:.0f} 分钟"
                  f"（没有回合比分，开打 {wait / 60:.0f} 分钟后录制）")
    elif leader is None:
        interval, reason = intervals["idle"], "决胜图未开始或换图中"
    elif overtime or leader >= int(cfg["late_at"]):
        interval, reason = intervals["late"], "决胜图加时" if overtime else f"决胜图赛点方 {leader} 分"
    elif leader >= int(cfg["mid_at"]):
        interval, reason = intervals["mid"], f"决胜图赛点方 {leader} 分"
    else:
        interval, reason = intervals["idle"], f"决胜图赛点方 {leader} 分"

    should_record = (status == "over" or no_score_record
                     or bool(deciding and leader is not None and (leader >= int(cfg["record_at"]) or overtime)))
    return {
        "status": status,
        "best_of": best_of,
        "map_wins": wins,
        "live": live,
        "deciding": deciding,
        "overtime": overtime,
        "interval": int(interval),
        "reason": reason,
        "should_record": should_record,
    }


def series_continued(record_wins: Optional[List[int]], assessment: Dict[str, Any]) -> bool:
    """自动开录后有图打完了、系列赛却没结束（开录时的"决胜图"被另一方拿下）。人工开录的没有 record_wins，不管。"""
    return (record_wins is not None and assessment["status"] == "live"
            and sum(assessment["map_wins"]) > sum(record_wins))


def _map_key(name: Any) -> str:
    """地图名比较用：de_dust2 / Dust2 / Dust 2 → dust2"""
    text = str(name or "").lower()
    text = text[3:] if text.startswith("de_") else text
    return "".join(ch for ch in text if ch.isalnum())


def describe(match: Dict[str, Any], assessment: Dict[str, Any]) -> str:
    t1, t2 = match["team1"].get("name") or "TBD", match["team2"].get("name") or "TBD"
    text = f"{t1} {assessment['map_wins'][0]}:{assessment['map_wins'][1]} {t2}"
    live = assessment.get("live")
    if live:
        text += f"（{live['map']} {live['team1']}:{live['team2']}）"
    else:
        current = next((m for m in match.get("maps") or [] if m.get("status") == "live"), None)
        if current and assessment["status"] == "live":
            text += f"（正在打 {current['name']}）"
    return text


def interview_end_wall(state: Dict[str, Any]) -> Optional[float]:
    """采访结束的真实时刻（录制开始时刻 + 采访结束在录音里的位置）。"""
    interview, recording = state.get("interview"), state.get("recording")
    if not interview or not recording:
        return None
    return recording["started_at"] + interview["end"]


# ── 监视循环 ─────────────────────────────────────────────────────────────────

class MatchWatcher:
    def __init__(self, hltv: HLTVClient, live: LiveManager, runner: PipelineRunner):
        self.sources = SourceChain(hltv)
        self.live = live
        self.runner = runner
        self._next_check: Dict[int, float] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def check_soon(self, task_id: int) -> None:
        self._next_check[task_id] = 0

    async def _alert(self, task_id: int, kind: str, body: str = "", mention_all: bool = False) -> None:
        await asyncio.to_thread(self.live.alert, task_id, kind, body, mention_all)

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("比赛监视出错")
            await asyncio.sleep(TICK)

    def _should_follow(self, task: Dict[str, Any]) -> bool:
        if task["source_type"] != "live" or task["context"].get("demo"):
            return False  # 演示任务（回放录好的直播）由回放脚本推进，不读比分
        if task["state"] in WATCH_STATES:
            return True
        if task["state"] in (State.CANCELLED, State.DONE, State.NO_INTERVIEW):
            return False
        # 录制期间就截取了采访（比赛结束信号还没到）：继续跟踪到比赛结束，用来更新最终比分
        state = load_state(task["id"])
        recording = state.get("recording")
        return bool(recording and not state.get("match_over_at")
                    and time.time() - recording["started_at"] < FOLLOW_AFTER_RECORDING)

    async def _tick(self) -> None:
        now = time.time()
        cfg = db.get_settings()["live"]
        followed = set()
        for task in db.list_tasks():
            await self._check_deadline(task, cfg)
            if not self._should_follow(task):
                continue
            followed.add(task["id"])
            if task["state"] == State.RESERVED:
                start = task["match_time"]
                if start and now < start - int(cfg["pre_match_minutes"]) * 60:
                    continue
                db.update_task(task["id"], state=State.WATCHING)
                db.add_event(task["id"], "开始监视比赛")
                task["state"] = State.WATCHING
                self._next_check[task["id"]] = 0
                if len(task["context"].get("teams") or []) < 2:
                    # 预约时队伍还没确定（TBD），开赛前重新抓取阵容
                    context = {k: v for k, v in task["context"].items() if k not in ("teams", "match")}
                    db.update_task(task["id"], context=context)
                    self.runner.enqueue(task["id"], "context", "context")
                    continue
            if now < self._next_check.get(task["id"], 0):
                continue
            try:
                await self._check(task, cfg)
            except AllSourcesFailed as error:
                await self._on_sources_down(task, cfg, str(error))
            except Exception as error:
                logger.exception("监视任务 %s 出错", task["id"])
                db.add_event(task["id"], f"比赛监视出错，{RETRY_AFTER_ERROR} 秒后重试：{brief(error)}", level="warning")
                self._next_check[task["id"]] = time.time() + RETRY_AFTER_ERROR
        # 不再监视的比赛关掉标签页
        for task_id in [t for t in self._next_check if t not in followed]:
            self._next_check.pop(task_id, None)
            task = db.get_task(task_id)
            if task:
                await self.sources.release(task)

    async def _check(self, task: Dict[str, Any], cfg: Dict[str, Any]) -> None:
        task_id = task["id"]
        match = await self.sources.read(task, cfg)
        recording = task["state"] == State.RECORDING
        now = time.time()
        previous = load_state(task_id)
        live_since = dict(previous.get("map_live_since") or {})
        for m in match["maps"]:
            if m.get("status") == "live":
                live_since.setdefault(m["name"], now)
                m["began_at"] = m.get("began_at") or live_since[m["name"]]
        assessment = assess(match, cfg, recording=recording, now=now)
        if not recording and assessment["deciding"] and assessment["live"] is None and not match.get("has_round_score", True):
            match = await self._with_hltv_round_score(task, match, cfg)
            assessment = assess(match, cfg, recording=recording, now=now)
        summary = describe(match, assessment)
        self._next_check[task_id] = now + assessment["interval"]

        if previous.get("source_down_since"):
            db.add_event(task_id, f"比分数据源已恢复（{SOURCE_LABELS[match['source']]}）")
        for failure in match.get("failures") or []:
            logger.info("任务 %s 数据源 %s 失败：%s", task_id, failure["source"], failure["error"])
        state = patch_state(task_id, source_down_since=None, snapshot={
            **assessment, "summary": summary, "maps": match["maps"], "checked_at": now,
            "next_check_at": self._next_check[task_id], "loaded_at": match.get("loaded_at"),
            "source": match["source"], "source_url": match.get("url"), "failures": match.get("failures") or [],
            "round_source": match.get("round_source"),
        }, streams=match.get("streams") or [], map_live_since=live_since)
        notified = dict(state.get("notified") or {})

        if assessment["status"] in ("live", "over") and not notified.get("start"):
            notified["start"] = True
            db.add_event(task_id, f"比赛已开始：{summary}")

        if assessment["should_record"] and task["state"] == State.WATCHING and not self.live.is_recording(task_id):
            if await self._start_recording(task, state, summary, notified):
                task["state"] = State.RECORDING
                patch_state(task_id, record_wins=assessment["map_wins"])
        elif recording and series_continued(state.get("record_wins"), assessment):
            await self._pause_recording(task, summary)

        if assessment["status"] == "over" and not state.get("match_over_at"):
            await self._on_match_over(task, match, summary)
            notified["over"] = True
        patch_state(task_id, notified=notified)

    async def _with_hltv_round_score(self, task: Dict[str, Any], match: Dict[str, Any],
                                     cfg: Dict[str, Any]) -> Dict[str, Any]:
        """主数据源没有回合比分、决胜图正在打：从 HLTV 比分板补回合比分。读不到就原样返回。"""
        current = next((m for m in match["maps"] if m.get("status") == "live"), None)
        if current is None or "hltv" not in self.sources.order(cfg) or not task.get("match_hltv_id"):
            return match
        try:
            hltv = await self.sources.sources["hltv"].read(task)
        except Exception as error:
            failures = [*(match.get("failures") or []), {"source": "hltv", "error": f"读取回合比分失败：{brief(error)}"}]
            return {**match, "failures": failures}
        score = hltv.get("live")
        # HLTV 页面可能还停在上一张图
        if not score or _map_key(score.get("map")) != _map_key(current["name"]):
            return match
        return {**match, "live": score, "has_round_score": True, "round_source": "hltv"}

    async def _pause_recording(self, task: Dict[str, Any], summary: str) -> None:
        """自动开录的那张图打完了、系列赛还没结束：停止录制，回到监视，等下一张决胜图。"""
        task_id = task["id"]
        await asyncio.to_thread(self.live.stop, task_id, f"系列赛未结束（{summary}），等下一张决胜图再录")
        db.update_task(task_id, state=State.WATCHING)

        def reset(s: Dict[str, Any]) -> None:
            s.pop("record_wins", None)
            s.pop("detection", None)  # 图间的转写不会有赛后采访，判断从头开始
        mutate_state(task_id, reset)
        db.add_event(task_id, f"暂停录制：{summary}，系列赛还没结束，等下一张决胜图再录")

    async def _start_recording(self, task: Dict[str, Any], state: Dict[str, Any], summary: str,
                               notified: Dict[str, Any]) -> bool:
        task_id = task["id"]
        stream = state.get("stream") or pick_stream(candidate_streams(task, state))
        if stream is None:
            db.add_event(task_id, "需要开始录制，但没有找到官方直播源，请在任务页填写直播链接", level="error")
            if not notified.get("no_stream"):
                notified["no_stream"] = True
                await self._alert(task_id, "找不到直播源", f"{summary}\n请在卡片里填写直播链接")
            return False
        await asyncio.to_thread(self.live.start, task_id, stream)
        db.update_task(task_id, state=State.RECORDING)
        return True

    async def _on_sources_down(self, task: Dict[str, Any], cfg: Dict[str, Any], error: str) -> None:
        task_id, now = task["id"], time.time()
        self._next_check[task_id] = now + RETRY_AFTER_ERROR
        state = load_state(task_id)
        since = state.get("source_down_since")
        if not since:
            patch_state(task_id, source_down_since=now)
            db.add_event(task_id, f"所有比分数据源都读取失败，{RETRY_AFTER_ERROR} 秒后重试：{error}", level="warning")
            return
        notified = dict(state.get("notified") or {})
        down = now - since
        if down >= SOURCE_DOWN_ALERT and not notified.get("source_down"):
            notified["source_down"] = True
            db.add_event(task_id, f"比分数据源已连续 {down / 60:.0f} 分钟不可用：{error}", level="error")
            await self._alert(task_id, "比分数据源不可用", f"已连续 {down / 60:.0f} 分钟读不到比分：{error}\n"
                              f"开赛后会直接开始录制并监听采访；比赛结束后可在卡片上点「比赛已结束」")
        started = task.get("match_time") and now >= task["match_time"]
        if (down >= SOURCE_DOWN_RECORD and started and task["state"] == State.WATCHING
                and not self.live.is_recording(task_id)):
            db.add_event(task_id, "比分数据源长时间不可用，保险起见直接开始录制", level="warning")
            await self._start_recording(task, state, "比分未知", notified)
        patch_state(task_id, notified=notified)

    async def _on_match_over(self, task: Dict[str, Any], match: Dict[str, Any], summary: str) -> None:
        task_id = task["id"]
        self.live.mark_match_over(task_id)
        # 背景信息里的比赛结果更新为最终比分，写稿时用得上
        context = dict(task["context"])
        if context.get("match"):
            context["match"] = {**context["match"], "status": "over", "maps": match["maps"],
                                "score": [match["team1"].get("score"), match["team2"].get("score")]}
        db.update_task(task_id, context=context)
        await self.sources.release(task)
        if task["state"] not in (State.WATCHING, State.RECORDING):
            db.add_event(task_id, f"比赛结束：{summary}")
            return
        recording = self.live.is_recording(task_id)
        db.update_task(task_id, state=State.INTERVIEW_DETECTING if recording else State.NO_INTERVIEW)
        if recording:
            db.add_event(task_id, f"比赛结束：{summary}，继续监听采访")
            await self._alert(task_id, "比赛结束", f"{summary}，正在监听赛后采访", mention_all=True)
        else:
            db.add_event(task_id, f"比赛结束：{summary}，但没有在录制（找不到直播源），请手动上传采访音频", level="error")
            await self._alert(task_id, "比赛结束但没有录到直播", f"{summary}\n请在任务页手动上传采访音频", mention_all=True)

    async def mark_over(self, task_id: int) -> None:
        """人工确认比赛已结束（数据源不可用时）。"""
        task = db.get_task(task_id)
        state = load_state(task_id)
        maps = (state.get("snapshot") or {}).get("maps") or (task["context"].get("match") or {}).get("maps") or []
        match = {"maps": maps, "team1": {"name": task["team1"]}, "team2": {"name": task["team2"]}}
        db.add_event(task_id, "人工确认比赛已结束")
        await self._on_match_over(task, match, "人工确认")

    async def _check_deadline(self, task: Dict[str, Any], cfg: Dict[str, Any]) -> None:
        """采访结束后超过时限还没出初稿就提醒（处理失败另有失败通知）。"""
        if task["source_type"] != "live" or task["state"] not in (State.QUEUED, State.PROCESSING):
            return
        state = load_state(task["id"])
        ended = interview_end_wall(state)
        captured = (state.get("interview") or {}).get("captured_at")
        if ended is not None and captured and captured - ended > 3600:
            ended = captured  # 事后补截的旧录音：从截取时刻开始算
        if ended is None or (state.get("notified") or {}).get("deadline"):
            return
        if any(config.task_dir(task["id"]).glob("*.docx")):
            return  # 初稿已经出过，现在是人工重跑

        late = time.time() - ended
        if late < int(cfg["draft_deadline_minutes"]) * 60:
            return

        def mark(s: Dict[str, Any]) -> None:
            s.setdefault("notified", {})["deadline"] = True
        mutate_state(task["id"], mark)
        stage = task.get("stage") or ""
        db.add_event(task["id"], f"采访结束已 {late / 60:.0f} 分钟，初稿还没完成（当前：{stage}）", level="error")
        await self._alert(task["id"], "初稿超时", f"采访结束已 {late / 60:.0f} 分钟，初稿还没完成（当前阶段：{stage}）")
