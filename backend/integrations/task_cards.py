"""
飞书群里"一个任务一张卡片"。

- 任务创建（直播间截到采访、新建任务）时登记，卡片服务在后台发出卡片，记下 message_id。
- 卡片内容完全由任务当前状态算出来（build_card 是纯函数），后台每隔几秒重算一次，
  内容有变化就整张替换。更新卡片不会提醒群成员，所以主聊天里一个采访始终只有一条消息。
- 需要人处理的事（处理失败、初稿就绪……）用 alert() 在卡片下面以话题回复，会提醒。
- 直播间本身的卡片见 monitor_cards.py。
- 卡片超过 14 天不能再改（或被撤回）时，重新发一张。
"""
import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config, db
from ..runner import STAGE_LABELS, State
from . import feishu

logger = logging.getLogger(__name__)

POLL_SECONDS = 3
MENTION_ALL = '<at user_id="all"></at>'
# 飞书只允许修改 14 天内发出的卡片，留点余量
CARD_MAX_AGE = 13.5 * 86400

# 状态 → (标签, 标题栏颜色, 标签颜色)
STATE_STYLE = {
    State.QUEUED: ("排队中", "wathet", "wathet"),
    State.PROCESSING: ("处理中", "wathet", "wathet"),
    State.REVIEW: ("待精校", "orange", "orange"),
    State.DONE: ("已完成", "green", "green"),
    State.FAILED: ("失败", "carmine", "carmine"),
    State.CANCELLED: ("已取消", "grey", "neutral"),
    State.DRAFT: ("待开始", "grey", "neutral"),
}
# 这些状态下卡片只在任务记录变化时才需要重算
QUIET_STATES = (State.REVIEW, State.DONE, State.FAILED, State.CANCELLED, State.DRAFT)
# 服务启动时给还没有卡片的这些任务补发卡片
ACTIVE_STATES = (State.QUEUED, State.PROCESSING)
# 直播间截下来的采访：截到就有任务，进度从初稿开始
INTERVIEW_STEPS = ["采访", "初稿", "精校"]
INTERVIEW_STEP_INDEX = {State.DRAFT: 1, State.QUEUED: 1, State.PROCESSING: 1, State.REVIEW: 2, State.DONE: 3}
MANUAL_STEPS = ["创建", "处理", "精校"]
MANUAL_STEP_INDEX = {State.DRAFT: 0, State.QUEUED: 1, State.PROCESSING: 1, State.REVIEW: 2, State.DONE: 3}
# 卡片"最新动态"里不显示的流水线流水账
EVENT_NOISE = ("开始：", "完成：", "加入队列")
SOURCE_LABELS = {"auto": "自动判定", "camera": "镜头切走", "manual": "人工标注", "timeout": "超时截取"}
KIND_LABELS = {"just_played": "赛后采访", "between_maps": "图间采访", "upcoming": "赛前采访",
               "general": "和具体比赛无关"}


# ── 卡片内容（纯函数） ─────────────────────────────────────────────────────────

def _clock(ts: Optional[float], with_date: bool = False) -> str:
    if not ts:
        return ""
    return time.strftime("%m-%d %H:%M" if with_date else "%H:%M", time.localtime(ts))


def _mmss(seconds: Optional[float]) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _text(content: str) -> Dict[str, Any]:
    return {"tag": "plain_text", "content": content}


def _markdown(content: str) -> Dict[str, Any]:
    return {"tag": "markdown", "content": content}


def is_interview(task: Dict[str, Any]) -> bool:
    """直播间截下来的采访任务。"""
    return task["source_type"] == "live"


def step_track(task: Dict[str, Any], state: Dict[str, Any]) -> List[Tuple[str, str]]:
    """进度条：[(步骤名, done|current|failed|stopped|todo)]"""
    steps, indexes, stopped_at = ((INTERVIEW_STEPS, INTERVIEW_STEP_INDEX, 1) if is_interview(task)
                                  else (MANUAL_STEPS, MANUAL_STEP_INDEX, 1))
    index = indexes.get(task["state"])
    marker = "current"
    if index is None:
        index, marker = stopped_at, "failed" if task["state"] == State.FAILED else "stopped"
    return [(name, "done" if i < index else marker if i == index else "todo") for i, name in enumerate(steps)]


def _track_line(track: List[Tuple[str, str]]) -> str:
    icons = {"done": "✅", "current": "🔵", "failed": "❌", "stopped": "⛔", "todo": "⚪"}
    parts = []
    for name, mark in track:
        label = f"**{name}**" if mark in ("current", "failed", "stopped") else name
        parts.append(f"{icons[mark]} {label}")
    return " → ".join(parts)


def _subtitle(task: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> str:
    context = task.get("context") or {}
    match = context.get("match") or {}
    if is_interview(task):
        origin = ((state or {}).get("recording") or {}).get("started_at")
        interview = (state or {}).get("interview") or {}
        at = origin + interview["start"] if origin is not None and "start" in interview else task.get("created_at")
        parts = [match.get("event") or (context.get("monitor") or {}).get("event_name"), _clock(at, with_date=True)]
        return " · ".join(p for p in parts if p) or f"任务 #{task['id']}"
    start = (match.get("unix_ms") or 0) / 1000 or task.get("match_time")
    parts = [match.get("event"), match.get("format"), _clock(start, with_date=True)]
    return " · ".join(p for p in parts if p) or f"任务 #{task['id']}"


def _interview_lines(task: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    """采访任务：来自哪个直播间、什么时间，识别出的受访者。"""
    context = task.get("context") or {}
    lines = []
    origin = (state.get("recording") or {}).get("started_at")
    interview = state.get("interview") or {}
    if origin is not None and interview:
        monitor = context.get("monitor") or {}
        where = f"[{monitor.get('event_name') or '直播间'}]({config.PUBLIC_URL}/monitors/{monitor.get('id')})"
        lines.append(f"**采访** {where} {_clock(origin + interview['start'])}–{_clock(origin + interview['end'])}"
                     f"（{SOURCE_LABELS.get(interview.get('source'), interview.get('source') or '')}）")
    identified = context.get("identified")
    if identified:
        who = "、".join(identified.get("interviewees") or []) or "受访者未知"
        parts = [who + (f"（{identified['team']}）" if identified.get("team") else "")]
        if identified.get("about_match") in KIND_LABELS:
            parts.append(KIND_LABELS[identified["about_match"]])
        if identified.get("opponent"):
            parts.append(f"对手 {identified['opponent']}")
        lines.append("**识别** " + " · ".join(parts))
    return lines


def _status_lines(task: Dict[str, Any], state: Dict[str, Any], progress: Optional[Dict[str, Any]],
                  names_report: Optional[Dict[str, Any]], cfg: Dict[str, Any]) -> List[str]:
    status = task["state"]
    lines: List[str] = []
    if status == State.QUEUED:
        lines.append("排队等待处理")
    elif status == State.PROCESSING:
        line = f"正在{STAGE_LABELS.get(task.get('stage'), '处理')}"
        if progress and progress.get("message"):
            line += f"：{progress['message']}"
            if progress.get("percent") is not None:
                line += f" {progress['percent']:.0f}%"
        lines.append(line)
    elif status == State.REVIEW:
        lines.append("初稿已完成，等待精校")
    elif status == State.FAILED:
        lines.append(f"❗ {task.get('error') or '处理失败'}")

    if status in (State.REVIEW, State.DONE) and names_report:
        suspects = names_report.get("suspects") or []
        if suspects:
            lines.append("**待核对名词** " + "、".join(
                f"{s['token']}（可能是 {'/'.join(s['candidates'])}）" for s in suspects))
    return lines


def _event_lines(events: List[Dict[str, Any]], limit: int = 3) -> List[str]:
    icons = {"warning": "⚠️ ", "error": "❗ "}
    picked = [e for e in events if not e["message"].startswith(EVENT_NOISE)][-limit:]
    lines = []
    for event in picked:
        message = event["message"].replace("\n", " ")
        if len(message) > 90:
            message = message[:88] + "…"
        lines.append(f"<font color='grey'>{_clock(event['ts'])}</font> {icons.get(event['level'], '')}{message}")
    return lines


def _button(label: str, action: str, task_id: int, kind: str = "default",
            confirm: Optional[str] = None) -> Dict[str, Any]:
    button = {
        "tag": "button",
        "text": _text(label),
        "type": kind,
        "size": "small",
        "behaviors": [{"type": "callback", "value": {"action": action, "task_id": task_id}}],
    }
    if confirm:
        button["confirm"] = {"title": _text(f"{label}？"), "text": _text(confirm)}
    return button


def _link_button(label: str, url: str, kind: str = "default") -> Dict[str, Any]:
    return {"tag": "button", "text": _text(label), "type": kind, "size": "small",
            "behaviors": [{"type": "open_url", "default_url": url}]}


def review_url(task_id: int, cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """在线精校页的公网地址；没配置内网穿透地址时没有。"""
    feishu_cfg = cfg if cfg is not None else db.get_settings()["feishu"]
    base = (feishu_cfg.get("public_url") or "").strip().rstrip("/")
    return f"{base}/review/{task_id}" if base else None


def _buttons(task: Dict[str, Any], state: Dict[str, Any], public_review: Optional[str] = None) -> List[Dict[str, Any]]:
    task_id, status = task["id"], task["state"]
    buttons = []
    if public_review and status in (State.REVIEW, State.DONE):
        buttons.append(_link_button("在线精校", public_review, "primary" if status == State.REVIEW else "default"))
    if status == State.FAILED:
        buttons.append(_button("从失败处重试", "retry", task_id, "primary"))
    elif status == State.REVIEW:
        buttons.append(_button("标记完成", "done", task_id, "default" if public_review else "primary"))
    buttons.append(_link_button("打开任务页", f"{config.PUBLIC_URL}/tasks/{task_id}"))
    return buttons


def build_card(task: Dict[str, Any], state: Dict[str, Any], progress: Optional[Dict[str, Any]] = None,
               events: Optional[List[Dict[str, Any]]] = None, names_report: Optional[Dict[str, Any]] = None,
               cfg: Optional[Dict[str, Any]] = None, reserved_by: Optional[str] = None,
               public_review: Optional[str] = None) -> Dict[str, Any]:
    """
    task：db.get_task 的结果；state：live.json（非直播任务为空）；progress：stages.progress 里的阶段进度；
    events：最近的任务事件；names_report：names_report.json；cfg：settings["live"]；
    reserved_by：在飞书里预约的人的 open_id；public_review：在线精校页的公网地址。
    """
    label, template, tag_color = STATE_STYLE.get(task["state"], (task["state"], "grey", "neutral"))
    facts = _interview_lines(task, state) if is_interview(task) else []
    sections = [
        [_track_line(step_track(task, state))]
        + ([f"<font color='grey'>预约人</font> <at id={reserved_by}></at>"] if reserved_by else []),
        facts + _status_lines(task, state, progress, names_report, cfg or {}),
        _event_lines(events or []),
    ]
    elements: List[Dict[str, Any]] = []
    for lines in sections:
        if lines:
            if elements:
                elements.append({"tag": "hr"})
            elements.append(_markdown("\n".join(lines)))
    elements.append({
        "tag": "column_set",
        "flex_mode": "flow",
        "horizontal_spacing": "8px",
        "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in _buttons(task, state, public_review)],
    })
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "summary": {"content": f"{label}｜{task['title']}"}},
        "header": {
            "title": _text(task["title"]),
            "subtitle": _text(_subtitle(task, state)),
            "template": template,
            "text_tag_list": [{"tag": "text_tag", "text": _text(label), "color": tag_color}],
        },
        "body": {"elements": elements},
    }


def digest(card: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(card, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# 示例卡片的任务 ID：按钮回调收到它时只回复"已连通"
DEMO_TASK_ID = 0


def demo_card() -> Dict[str, Any]:
    """设置页「测试」发送的示例卡片：一个待精校的采访，用来检查卡片显示和按钮回调是否连通。"""
    now = time.time()
    task = {"id": DEMO_TASK_ID, "title": "测试卡片 · ZywOo (Vitality) 赛后采访", "state": State.REVIEW, "stage": None,
            "source_type": "live", "error": None, "created_at": now,
            "context": {"monitor": {"id": 0, "event_name": "示例赛事"}, "match": {"event": "示例赛事"},
                        "identified": {"interviewees": ["ZywOo"], "team": "Vitality", "opponent": "MOUZ",
                                       "about_match": "just_played"}}}
    state = {"recording": {"started_at": now - 900}, "interview": {"start": 300, "end": 600, "source": "auto"}}
    events = [{"ts": now, "level": "info", "message": "这是一张测试卡片，点按钮会提示回调是否连通"}]
    return build_card(task, state, events=events, cfg={})


# ── 卡片服务 ──────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except ValueError:
        return None


def render(task: Dict[str, Any], reserved_by: Optional[str] = None) -> Dict[str, Any]:
    from ..pipeline import live, stages

    state = live.load_state(task["id"]) if task["source_type"] == "live" else {}
    report = _read_json(config.task_dir(task["id"]) / "names_report.json")
    settings = db.get_settings()
    return build_card(task, state, stages.progress.get(task["id"]), db.recent_events(task["id"], 30), report,
                      settings["live"], reserved_by, review_url(task["id"], settings["feishu"]))


class TaskCardService:
    def __init__(self) -> None:
        self._locks: Dict[int, threading.Lock] = defaultdict(threading.Lock)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # 生命周期
    def start(self) -> None:
        settings = db.get_settings()
        if feishu.enabled(settings):
            for task in db.list_tasks():
                if task["state"] in ACTIVE_STATES and db.get_card(task["id"]) is None:
                    db.track_card(task["id"])
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="feishu-cards", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def track(self, task_id: int, reserved_by: Optional[str] = None) -> None:
        """这个任务要一张卡片（未启用飞书时不登记）。reserved_by：在飞书里预约的人。"""
        if feishu.enabled(db.get_settings()):
            db.track_card(task_id, reserved_by)
            self.wake()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_all()
            except Exception:
                logger.exception("同步飞书卡片出错")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def sync_all(self) -> None:
        if not feishu.enabled(db.get_settings()):
            return
        for row in db.list_cards():
            task = db.get_task(row["task_id"])
            if task is None:
                continue
            if (row["message_id"] and task["state"] in QUIET_STATES and row["rendered_at"]
                    and task["updated_at"] < row["rendered_at"]):
                continue
            if row["sent_at"] and time.time() - row["sent_at"] > CARD_MAX_AGE and task["state"] in QUIET_STATES:
                continue  # 早就结束的任务，卡片过期了也不再重发
            try:
                self.sync(task["id"])
            except Exception as error:
                logger.warning("任务 %s 飞书卡片同步失败：%s", task["id"], error)

    def sync(self, task_id: int) -> Optional[str]:
        """
        把卡片更新到任务当前状态（还没发就发出），返回 message_id；任务没登记卡片时返回 None。
        """
        with self._locks[task_id]:
            row = db.get_card(task_id)
            task = db.get_task(task_id)
            if row is None or task is None:
                return None
            card = render(task, row.get("reserved_by"))
            card_digest = digest(card)
            now = time.time()
            message_id = row["message_id"]
            if message_id and row["digest"] == card_digest:
                db.update_card(task_id, rendered_at=now)
                return message_id
            client = feishu.client_for(db.get_settings())
            expired = row["sent_at"] and now - row["sent_at"] > CARD_MAX_AGE
            if message_id and not expired:
                try:
                    client.update_card(message_id, card)
                    db.update_card(task_id, digest=card_digest, rendered_at=now)
                    return message_id
                except feishu.FeishuError as error:
                    if error.code not in feishu.CARD_GONE_CODES:
                        raise
            message_id = client.send_card(card)
            db.update_card(task_id, message_id=message_id, sent_at=now, digest=card_digest, rendered_at=now)
            return message_id

    # 提醒
    def alert(self, task_id: int, kind: str, body: str = "", mention_all: bool = False) -> None:
        """
        需要人处理的事：在任务卡片下回复话题（会提醒群成员）。mention_all：同时 @所有人（比赛结束、初稿就绪）。
        任务没有卡片或回复失败时，退回发一条普通文本消息。
        """
        settings = db.get_settings()
        if not feishu.enabled(settings):
            return
        text = f"【{kind}】{body}".rstrip()
        try:
            message_id = self.sync(task_id)
            if message_id:
                client = feishu.client_for(settings)
                if mention_all:
                    try:
                        client.reply_text(message_id, f"{MENTION_ALL} {text}")
                        return
                    except feishu.FeishuError as error:
                        # 群里设置了只有群主能 @所有人 等情况：去掉 @ 再发
                        logger.warning("任务 %s @所有人 失败，改为普通回复：%s", task_id, error)
                client.reply_text(message_id, text)
                return
        except Exception as error:
            logger.warning("任务 %s 卡片话题回复失败，改发普通消息：%s", task_id, error)
        task = db.get_task(task_id)
        title = task["title"] if task else f"任务 #{task_id}"
        feishu.notify(settings, f"【{kind}】{title}\n{body}\n{config.PUBLIC_URL}/tasks/{task_id}".replace("\n\n", "\n"))

    def send_file(self, task_id: int, path: Path) -> None:
        """把文件发到任务卡片的话题里；没有卡片时直接发到群里。失败时抛出异常。"""
        client = feishu.client_for(db.get_settings())
        message_id = self.sync(task_id)
        if message_id:
            client.reply_file(message_id, path)
        else:
            client.send_file(path)

    def send_image(self, task_id: int, path: Path) -> None:
        """同 send_file，发图片。"""
        client = feishu.client_for(db.get_settings())
        message_id = self.sync(task_id)
        if message_id:
            client.reply_image(message_id, path)
        else:
            client.send_image(path)

    def send_text(self, task_id: int, text: str) -> None:
        """在话题里发一条纯文本（不加【】前缀，方便长按整条复制）。失败时抛出异常。"""
        client = feishu.client_for(db.get_settings())
        message_id = self.sync(task_id)
        if message_id:
            client.reply_text(message_id, text)
        else:
            client.send_text(text)


cards = TaskCardService()
