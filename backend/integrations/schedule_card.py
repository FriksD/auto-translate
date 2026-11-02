"""
群里预约比赛：机器人命令解析和赛程卡片。

在群里 @机器人（或私聊机器人）：
  赛程 / 赛程 明天 / 赛程 全部 / 赛程 2星   → 回复一张赛程卡片，可以切换日期、星级，每场比赛一个「预约」按钮
  赛程 vitality / 预约 小蜜蜂                → 按队名（或赛事名、术语表里的别名）搜索未来几天的比赛
  预约 <HLTV 比赛链接>                       → 直接预约这场比赛
赛程卡片上的筛选和预约都在原卡片上更新。
"""
import re
import time
from typing import Any, Dict, List, Optional, Tuple

DAY_ALL = "all"
DAY_WORDS = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2}
WEEKDAYS = "一二三四五六日"
STAR_OPTIONS = [0, 1, 2, 3]
MAX_ROWS = 25

HELP_TEXT = (
    "可以这样用（群里先 @我）：\n"
    "· 赛程 —— 今天的比赛，卡片上可以切换日期、星级，点「预约」\n"
    "· 赛程 明天 / 赛程 全部 / 赛程 2星\n"
    "· 预约 vitality —— 按队名或赛事名搜索未来几天的比赛（支持术语表里的别名，如 小蜜蜂）\n"
    "· 预约 <HLTV 比赛链接> —— 直接预约"
)


# ── 命令 ─────────────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    """去掉 @ 占位符（@_user_1）和多余空白。"""
    return re.sub(r"\s+", " ", re.sub(r"@_user_\d+", " ", text or "")).strip()


def parse_command(text: str) -> Tuple[str, Dict[str, Any]]:
    """
    返回 (命令, 参数)：
      ("schedule", {"day": 0|1|...|"all"|None, "stars": int, "q": str})
      ("reserve_url", {"url": str})
      ("help", {}) / ("unknown", {"text": str})
    """
    text = clean_text(text)
    if text.lower() in ("", "帮助", "help", "?", "？", "菜单"):
        return "help", {}
    match = re.match(r"^(赛程|比赛|schedule|预约|reserve)\s*(.*)$", text, re.I)
    if not match:
        return "unknown", {"text": text}
    verb, arg = match.group(1).lower(), match.group(2).strip()
    if verb in ("预约", "reserve") and re.search(r"hltv\.org/matches/\d+", arg):
        return "reserve_url", {"url": arg}
    view: Dict[str, Any] = {"day": None, "stars": 0, "q": ""}
    stars = re.fullmatch(r"(\d)\s*星\+?", arg)
    if not arg:
        pass
    elif arg in DAY_WORDS:
        view["day"] = DAY_WORDS[arg]
    elif arg in ("全部", "所有", "all"):
        view["day"] = DAY_ALL
    elif stars:
        view["stars"] = int(stars.group(1))
    else:
        view["q"] = arg
        view["day"] = DAY_ALL
    return "schedule", view


def resolve_query(q: str, aliases: Dict[str, str]) -> List[str]:
    """搜索词，加上术语表里别名对应的规范名（小蜜蜂 → Vitality）。"""
    q = q.strip().lower()
    if not q:
        return []
    terms = [q]
    for alias, target in aliases.items():
        if alias.lower() == q and target.lower() not in terms:
            terms.append(target.lower())
    return terms


# ── 赛程卡片 ─────────────────────────────────────────────────────────────────

def _text(content: str) -> Dict[str, Any]:
    return {"tag": "plain_text", "content": content}


def _date_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def day_label(date_key: str, now: float) -> str:
    target = time.mktime(time.strptime(date_key, "%Y-%m-%d"))
    today = time.mktime(time.strptime(_date_key(now), "%Y-%m-%d"))
    offset = round((target - today) / 86400)
    names = {0: "今天", 1: "明天", 2: "后天"}
    weekday = WEEKDAYS[time.localtime(target).tm_wday]
    short = time.strftime("%m-%d", time.localtime(target))
    return f"{names[offset]} {short}" if offset in names else f"{short} 周{weekday}"


def resolve_day(view: Dict[str, Any], dates: List[str], now: float) -> str:
    """view["day"]：日期字符串、相对今天的天数、"all" 或空（默认今天，今天没有比赛就取最近有比赛的一天）。"""
    day = view.get("day")
    if day == DAY_ALL:
        return DAY_ALL
    if isinstance(day, int):
        return _date_key(now + day * 86400)
    if isinstance(day, str) and day:
        return day
    today = _date_key(now)
    return today if today in dates or not dates else min(dates)


def _match_teams(match: Dict[str, Any]) -> str:
    return " vs ".join((t or {}).get("name") or "TBD" for t in (match.get("team1"), match.get("team2")))


def filter_matches(matches: List[Dict[str, Any]], day: str, stars: int, terms: List[str]) -> List[Dict[str, Any]]:
    result = []
    for match in matches:
        if (match.get("stars") or 0) < stars:
            continue
        if day != DAY_ALL and not match.get("live"):
            if not match.get("unix_ms") or _date_key(match["unix_ms"] / 1000) != day:
                continue
        if terms:
            haystack = f"{_match_teams(match)} {(match.get('event') or {}).get('name') or ''}".lower()
            if not any(term in haystack for term in terms):
                continue
        result.append(match)
    return sorted(result, key=lambda m: (not m.get("live"), m.get("unix_ms") or 0))


def _button(label: str, value: Dict[str, Any], kind: str = "default") -> Dict[str, Any]:
    return {"tag": "button", "text": _text(label), "type": kind, "size": "small",
            "behaviors": [{"type": "callback", "value": value}]}


def _button_row(buttons: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "6px",
            "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in buttons]}


def _match_row(match: Dict[str, Any], view: Dict[str, Any], show_date: bool) -> Dict[str, Any]:
    if match.get("live"):
        when = "🔴 LIVE"
    elif match.get("unix_ms"):
        when = time.strftime("%m-%d %H:%M" if show_date else "%H:%M", time.localtime(match["unix_ms"] / 1000))
    else:
        when = "时间待定"
    stars = "⭐" * (match.get("stars") or 0)
    event = (match.get("event") or {}).get("name") or ""
    detail = " · ".join(p for p in (event, (match.get("format") or "").upper()) if p)
    left = {"tag": "markdown", "content": f"`{when}` **{_match_teams(match)}** {stars}\n<font color='grey'>{detail}</font>"}
    if match.get("task_id") and match.get("task_state") in ("reserved", "watching"):
        # 开打前没有进度卡片，取消预约在这里
        right = _button("取消预约", {"action": "unreserve", "task_id": match["task_id"], "view": view}, "danger")
        right["confirm"] = {"title": _text("取消预约？"), "text": _text(f"{_match_teams(match)} 将不再监视和录制")}
    elif match.get("task_id"):
        right = {"tag": "markdown", "content": "✅ 已预约", "text_align": "right"}
    else:
        right = _button("预约", {"action": "reserve", "match_id": match["id"], "view": view}, "primary")
    return {
        "tag": "column_set",
        "horizontal_spacing": "8px",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "elements": [left]},
            {"tag": "column", "width": "auto", "vertical_align": "center", "elements": [right]},
        ],
    }


def build_schedule_card(matches: List[Dict[str, Any]], view: Dict[str, Any], aliases: Dict[str, str],
                        fetched_at: Optional[float] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """matches：/api/matches 的结果（含 task_id）；view：{day, stars, q}。"""
    now = now or time.time()
    dates = sorted({_date_key(m["unix_ms"] / 1000) for m in matches if m.get("unix_ms")})
    day = resolve_day(view, dates, now)
    stars = int(view.get("stars") or 0)
    q = (view.get("q") or "").strip()
    current = {"day": day, "stars": stars, "q": q}
    picked = filter_matches(matches, day, stars, resolve_query(q, aliases))

    elements: List[Dict[str, Any]] = []
    day_buttons = [_button(day_label(d, now), {"action": "schedule", "view": {**current, "day": d}},
                           "primary_filled" if d == day else "default") for d in dates]
    day_buttons.append(_button("全部", {"action": "schedule", "view": {**current, "day": DAY_ALL}},
                               "primary_filled" if day == DAY_ALL else "default"))
    elements.append(_button_row(day_buttons))
    star_buttons = [_button("不限星级" if s == 0 else f"{'⭐' * s}+", {"action": "schedule", "view": {**current, "stars": s}},
                            "primary_filled" if s == stars else "default") for s in STAR_OPTIONS]
    elements.append(_button_row(star_buttons))
    if q:
        elements.append({"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "8px", "columns": [
            {"tag": "column", "width": "auto", "vertical_align": "center",
             "elements": [{"tag": "markdown", "content": f"搜索：**{q}**"}]},
            {"tag": "column", "width": "auto",
             "elements": [_button("清除搜索", {"action": "schedule", "view": {**current, "q": ""}})]},
        ]})
    elements.append({"tag": "hr"})

    if not picked:
        elements.append({"tag": "markdown", "content": "没有符合条件的比赛"})
    for match in picked[:MAX_ROWS]:
        elements.append(_match_row(match, current, show_date=day == DAY_ALL))
    if len(picked) > MAX_ROWS:
        elements.append({"tag": "markdown",
                         "content": f"<font color='grey'>还有 {len(picked) - MAX_ROWS} 场没有显示，提高星级、换一天或搜索队名</font>"})

    elements.append({"tag": "hr"})
    elements.append(_button_row([_button("刷新", {"action": "schedule", "view": current, "refresh": True})]))
    updated = time.strftime("%H:%M", time.localtime(fetched_at)) if fetched_at else ""
    title = f"CS2 赛程 · {day_label(day, now) if day != DAY_ALL else '未来几天'}"
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "summary": {"content": title}},
        "header": {
            "title": _text(title),
            "subtitle": _text(f"HLTV · {len(picked)} 场" + (f" · 更新于 {updated}" if updated else "")),
            "template": "indigo",
        },
        "body": {"elements": elements},
    }
