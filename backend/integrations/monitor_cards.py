"""
飞书群里"一个直播间一张卡片"。

- 直播间开始监视时发出卡片，之后后台每隔几秒重算一次，内容有变化就整张替换（不会提醒群成员）。
- 卡片上：开关状态、每条链接的录制/断流状态、正在进行的采访、最近的采访镜头、今天截到的采访（链接到任务）、
  最新动态；按钮：开始/停止监视、打开直播间页；表单：换链接（可选是否走代理）。
- 断流、LLM 连续失败等需要人处理的事用 alert() 在卡片下面以话题回复，会提醒。
- 采访任务自己的进度卡片还是由 task_cards 发，一个采访一张。
"""
import hashlib
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .. import config, db
from . import feishu
from .task_cards import CARD_MAX_AGE, STATE_STYLE, _button, _clock, _link_button, _markdown, _text

logger = logging.getLogger(__name__)

POLL_SECONDS = 10

PLATFORM_LABELS = {"bilibili": "B站", "youtube": "YouTube", "twitch": "Twitch", "kick": "Kick", "huya": "虎牙",
                   "douyu": "斗鱼", "douyin": "抖音", "other": "直播"}
STREAM_STATUS = {"recording": "🔴 录制中", "offline": "⚪ 未开播", "starting": "正在探测…",
                 "replay": "⚠️ 拉到的是回放，稍后重试", "unknown": "⚠️ 探测失败，尝试直接录制", "stopped": "没在监视"}
PROXY_OPTIONS = [("auto", "按域名自动（国内直连）"), ("on", "走代理"), ("off", "直连")]
# 卡片"最新动态"里不显示的流水账
EVENT_NOISE = ("开始监视：", "停止监视：")


# ── 换链接（纯函数） ──────────────────────────────────────────────────────────

def replace_stream(streams: List[Dict[str, Any]], url: str, slot: Optional[str] = None,
                   proxy: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    把 url 放进直播链接列表。slot：要替换的第几条（"0"/"1"）或 "new"；不填时：已有这条就只改代理，
    不到两条就加一条，否则替换同平台的那条（没有同平台的就替换最后一条）。
    proxy："on" / "off" / "auto"（按域名默认）。返回新列表，交给 normalize_streams 补默认值。
    """
    from ..sources import stream_status
    from ..pipeline.monitor import MAX_STREAMS, stream_key

    url = url.strip()
    item: Dict[str, Any] = {"url": url}
    if proxy in ("on", "off"):
        item["proxy"] = proxy == "on"
    result = [dict(s) for s in streams]
    same = next((i for i, s in enumerate(result) if stream_key(s["url"]) == stream_key(url)), None)
    if same is not None and slot in (None, "", "new"):
        result[same] = {**result[same], **item}
        if proxy == "auto":
            result[same].pop("proxy", None)
        return result
    if slot not in (None, "", "new") and str(slot).isdigit() and int(slot) < len(result):
        index = int(slot)
    elif len(result) < MAX_STREAMS:
        return result + [item]
    else:
        platform = stream_status.platform(url)
        index = next((i for i, s in enumerate(result) if stream_status.platform(s["url"]) == platform),
                     len(result) - 1)
    result[index] = {"url": url, "label": result[index].get("label") or "", **item}
    return [s for i, s in enumerate(result) if i == index or stream_key(s["url"]) != stream_key(url)]


# ── 卡片内容（纯函数） ─────────────────────────────────────────────────────────

def _stream_name(stream: Dict[str, Any]) -> str:
    return stream.get("label") or PLATFORM_LABELS.get(stream.get("platform") or "other", "直播")


def _stream_lines(status: Dict[str, Any]) -> List[str]:
    lines = []
    for stream in status.get("stream_status") or []:
        label = STREAM_STATUS.get(stream["status"], stream["status"])
        if stream.get("recording") and not stream.get("connected"):
            label = "🟠 断流，正在重连"
        parts = [f"**{_stream_name(stream)}** {label}"]
        if stream.get("title"):
            parts.append(stream["title"][:40])
        parts.append("走代理" if stream.get("proxy") else "直连")
        lines.append(" · ".join(parts) + f"\n<font color='grey'>{stream['url']}</font>")
    return lines or ["还没有直播链接，在下面填一个"]


def _detection_lines(status: Dict[str, Any]) -> List[str]:
    lines = []
    for stream in status.get("stream_status") or []:
        origin = stream.get("origin")
        if origin is None:
            continue
        name = _stream_name(stream)
        det = stream.get("detection") or {}
        active = det.get("active")
        if active:
            who = "、".join(active.get("interviewees") or []) or "选手"
            team = f"（{active['team']}）" if active.get("team") else ""
            lines.append(f"🎙️ **{name}** 正在采访 {who}{team}，{_clock(origin + active['start'])} 开始")
        elif det.get("trigger_at") is not None:
            lines.append(f"👀 **{name}** {_clock(origin + det['trigger_at'])} 出现采访迹象，正在判断")
        runs = stream.get("shot_runs") or []
        if runs:
            run = runs[-1]
            until = "现在" if run.get("open") else _clock(origin + run["end"])
            lines.append(f"<font color='grey'>{name} 最近的采访镜头 {_clock(origin + run['start'])}–{until}</font>")
    return lines


def _task_lines(status: Dict[str, Any], now: float) -> List[str]:
    day_start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    today = [t for t in status.get("tasks") or [] if t["created_at"] >= day_start]
    if not today:
        return []
    lines = [f"**今天截到的采访**（{len(today)}）"]
    for task in today[:8]:
        label = STATE_STYLE.get(task["state"], (task["state"],))[0]
        lines.append(f"· [{task['title']}]({config.PUBLIC_URL}/tasks/{task['id']}) <font color='grey'>{label}</font>")
    if len(today) > 8:
        lines.append(f"<font color='grey'>还有 {len(today) - 8} 个，见直播间页</font>")
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


def _monitor_button(label: str, action: str, monitor_id: int, kind: str = "default",
                    confirm: Optional[str] = None) -> Dict[str, Any]:
    button = _button(label, action, 0, kind, confirm)
    button["behaviors"][0]["value"] = {"action": action, "monitor_id": monitor_id}
    return button


def _option(value: str, label: str) -> Dict[str, Any]:
    return {"text": _text(label), "value": value}


def _stream_form(status: Dict[str, Any]) -> Dict[str, Any]:
    streams = status.get("streams") or []
    slots = [_option(str(i), f"替换第 {i + 1} 条（{PLATFORM_LABELS.get(s.get('platform') or 'other', '直播')}）")
             for i, s in enumerate(status.get("stream_status") or [])]
    if len(streams) < 2:
        slots.insert(0, _option("new", "添加一条"))
    return {
        "tag": "form",
        "name": f"monitor_{status['id']}",
        "elements": [
            {"tag": "input", "name": "url", "placeholder": _text("直播链接（B 站 / YouTube / Twitch …）")},
            {"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "8px", "columns": [
                {"tag": "column", "width": "auto", "elements": [
                    {"tag": "select_static", "name": "slot", "placeholder": _text("放在哪"),
                     "initial_option": slots[0]["value"], "options": slots}]},
                {"tag": "column", "width": "auto", "elements": [
                    {"tag": "select_static", "name": "proxy", "placeholder": _text("是否走代理"),
                     "initial_option": "auto", "options": [_option(v, label) for v, label in PROXY_OPTIONS]}]},
                {"tag": "column", "width": "auto", "elements": [
                    {"tag": "button", "name": "submit", "text": _text("保存链接"), "type": "default", "size": "small",
                     "form_action_type": "submit",
                     "behaviors": [{"type": "callback",
                                    "value": {"action": "monitor_stream", "monitor_id": status["id"]}}]}]},
            ]},
        ],
    }


def _subtitle(status: Dict[str, Any], now: float) -> str:
    parts = [status.get("event_name") or ""]
    if status.get("enabled"):
        parts.append("手动开启")
    else:
        current = [w for w in status.get("windows") or [] if float(w["start"]) <= now <= float(w["end"])]
        upcoming = sorted(float(w["start"]) for w in status.get("windows") or [] if float(w["start"]) > now)
        if current:
            parts.append(f"按预约监视到 {_clock(max(float(w['end']) for w in current))}")
        elif upcoming:
            parts.append(f"{_clock(upcoming[0], with_date=True)} 按预约开始监视")
    return " · ".join(p for p in parts if p) or f"直播间 #{status['id']}"


def build_monitor_card(status: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """status：monitor.manager.status(monitor) 的结果。"""
    now = time.time() if now is None else now
    on = bool(status.get("on"))
    label, template, color = ("监视中", "red", "red") if on else ("已停止", "grey", "neutral")
    sections = [_stream_lines(status), _detection_lines(status) if on else [], _task_lines(status, now),
                _event_lines(status.get("events") or [])]
    elements: List[Dict[str, Any]] = []
    for lines in sections:
        if lines:
            if elements:
                elements.append({"tag": "hr"})
            elements.append(_markdown("\n".join(lines)))
    elements.append(_stream_form(status))
    buttons = [
        _monitor_button("停止监视", "monitor_off", status["id"], "danger", "停止录制这个直播间，预约的时间窗也一起清掉")
        if on else _monitor_button("开始监视", "monitor_on", status["id"], "primary"),
        _link_button("打开直播间页", f"{config.PUBLIC_URL}/monitors/{status['id']}"),
    ]
    elements.append({"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "8px",
                     "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in buttons]})
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "summary": {"content": f"{label}｜{status['title']}"}},
        "header": {
            "title": _text(f"📺 {status['title']}"),
            "subtitle": _text(_subtitle(status, now)),
            "template": template,
            "text_tag_list": [{"tag": "text_tag", "text": _text(label), "color": color}],
        },
        "body": {"elements": elements},
    }


def digest(card: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(card, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# ── 卡片服务 ──────────────────────────────────────────────────────────────────

def render(monitor_id: int) -> Optional[Dict[str, Any]]:
    from ..pipeline import monitor

    found = db.get_monitor(monitor_id)
    return build_monitor_card(monitor.manager.status(found)) if found else None


class MonitorCardService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="feishu-monitor-cards", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_all()
            except Exception:
                logger.exception("同步飞书直播间卡片出错")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def sync_all(self) -> None:
        if not feishu.enabled(db.get_settings()):
            return
        from ..pipeline import monitor

        now = time.time()
        for found in db.list_monitors():
            row = db.get_monitor_card(found["id"])
            on = monitor.is_on(found, now)
            # 开着的直播间都要有卡片；关掉的只更新已经发出的那张（显示已停止）
            if not on and not (row and row.get("message_id")):
                continue
            if not on and row.get("sent_at") and now - row["sent_at"] > CARD_MAX_AGE:
                continue
            try:
                self.sync(found["id"])
            except Exception as error:
                logger.warning("直播间 %s 飞书卡片同步失败：%s", found["id"], error)

    def sync(self, monitor_id: int, resend: bool = False) -> Optional[str]:
        """把卡片更新到直播间当前状态（还没发就发出），返回 message_id。resend：发一张新的（旧的不再更新）。"""
        with self._lock:
            card = render(monitor_id)
            if card is None:
                return None
            row = db.get_monitor_card(monitor_id) or {}
            card_digest = digest(card)
            now = time.time()
            message_id = None if resend else row.get("message_id")
            if message_id and row.get("digest") == card_digest:
                return message_id
            client = feishu.client_for(db.get_settings())
            expired = row.get("sent_at") and now - row["sent_at"] > CARD_MAX_AGE
            if message_id and not expired:
                try:
                    client.update_card(message_id, card)
                    db.save_monitor_card(monitor_id, digest=card_digest)
                    return message_id
                except feishu.FeishuError as error:
                    if error.code not in feishu.CARD_GONE_CODES:
                        raise
            message_id = client.send_card(card)
            db.save_monitor_card(monitor_id, message_id=message_id, sent_at=now, digest=card_digest)
            return message_id

    def alert(self, monitor_id: int, text: str) -> None:
        """需要人处理的事：在直播间卡片下回复话题；没有卡片或回复失败时发普通消息。"""
        settings = db.get_settings()
        if not feishu.enabled(settings):
            return
        try:
            message_id = self.sync(monitor_id)
            if message_id:
                feishu.client_for(settings).reply_text(message_id, text)
                return
        except Exception as error:
            logger.warning("直播间 %s 卡片话题回复失败，改发普通消息：%s", monitor_id, error)
        found = db.get_monitor(monitor_id)
        title = found["title"] if found else f"直播间 #{monitor_id}"
        feishu.notify(settings, f"{text}\n{title}：{config.PUBLIC_URL}/monitors/{monitor_id}")


monitor_cards = MonitorCardService()
